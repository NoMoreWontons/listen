import os
import re
import json
import time
import threading
import datetime
import pathlib
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from supabase import create_client
import anthropic

load_dotenv()
HERE = pathlib.Path(__file__).parent
AUDIO_DIR = HERE / "audio"
AUDIO_DIR.mkdir(exist_ok=True)
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "7"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3-turbo")
# ponytail: this is a *stall* timeout, not a total-time budget — any download
# progress resets the clock, so a slow-but-moving multi-hour download is fine,
# only a truly dead one throws.
MODEL_LOAD_STALL_S = int(os.getenv("MODEL_LOAD_STALL_S", "300"))
OBSIDIAN_VAULT = pathlib.Path(
    os.getenv("OBSIDIAN_VAULT", str(pathlib.Path.home() / "College Lectures"))
)
# Notion fallback: user records lectures with Notion's AI meeting-note taker into
# a dedicated database; we poll it, pull transcripts, run the same Claude pipeline.
# Off unless NOTION_TOKEN is set (an internal-integration secret shared with the DB).
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_DB_ID = os.getenv("NOTION_DB_ID", "658a290d-8e82-4cb5-bfcc-79f19a2724aa")
NOTION_POLL_MIN = int(os.getenv("NOTION_POLL_MIN", "10"))

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
claude = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY


def audio_path(rid):
    return AUDIO_DIR / f"{rid}.webm"


# --- Whisper: load the model once, lazily, GPU with CPU fallback ---
_model = None
_model_future = None
_model_lock = threading.Lock()
_model_loader = ThreadPoolExecutor(max_workers=1)
_model_progress = {"pct": None}  # download %, set by the tqdm hook below
# ponytail: CTranslate2 isn't safe for concurrent transcribe() calls on one
# model instance — reuse this lock to serialize them too, not just loading.
_transcribe_lock = threading.Lock()


def _download_progress_tqdm():
    """faster_whisper hardcodes tqdm_class=disabled_tqdm for its download —
    that's why a slow download used to look identical to a hang. Bypass it by
    downloading the snapshot ourselves with a tqdm that reports into
    _model_progress, then hand WhisperModel the local path (no download)."""
    from tqdm import tqdm as _tqdm

    class ProgressTqdm(_tqdm):
        _last = -1

        def update(self, n=1):
            r = super().update(n)
            total = self.total or 0
            if total > 50_000_000:  # ignore small metadata files, track model.bin
                pct = int(self.n / total * 100)
                if pct != ProgressTqdm._last:
                    ProgressTqdm._last = pct
                    _model_progress["pct"] = pct
            return r

    return ProgressTqdm


def _load_model():
    from faster_whisper import WhisperModel
    from faster_whisper.utils import _MODELS
    from huggingface_hub import snapshot_download

    repo_id = WHISPER_MODEL if re.match(r".*/.*", WHISPER_MODEL) else _MODELS[WHISPER_MODEL]
    model_path = snapshot_download(
        repo_id,
        allow_patterns=["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"],
        tqdm_class=_download_progress_tqdm(),
    )
    _model_progress["pct"] = None  # download done; loading into memory now
    try:
        model = WhisperModel(model_path, device="cuda", compute_type="float16")
        # cuBLAS/cuDNN only load on first inference, not at construction — probe
        # now so a broken CUDA install falls back to CPU here instead of failing
        # every transcribe with "Library cublas64_12.dll is not found".
        import numpy as np
        next(model.transcribe(np.zeros(16000, dtype=np.float32))[0], None)
        return model
    except Exception:
        # ponytail: CPU fallback when CUDA libs aren't present; slower but works
        return WhisperModel(model_path, device="cpu", compute_type="int8")


def _poll_until_ready(future, poll_s=5):
    """Waits on future, raising TimeoutError if _model_progress['pct'] hasn't
    changed for MODEL_LOAD_STALL_S despite polling every poll_s seconds."""
    last_pct, stalled_for = _model_progress["pct"], 0
    while True:
        try:
            return future.result(timeout=poll_s)
        except TimeoutError:
            stalled_for += poll_s
            if _model_progress["pct"] != last_pct:
                last_pct, stalled_for = _model_progress["pct"], 0
            if stalled_for >= MODEL_LOAD_STALL_S:
                raise TimeoutError(
                    f"Whisper model '{WHISPER_MODEL}' load stalled — no progress for "
                    f"{MODEL_LOAD_STALL_S}s. Check network connection / GPU drivers."
                )


def get_model():
    """Loads (downloading first if needed) on first call. Raises TimeoutError if
    load progress genuinely stalls for MODEL_LOAD_STALL_S — the background load
    keeps running either way, so a later call just picks up the finished model."""
    global _model, _model_future
    with _model_lock:
        if _model is not None:
            return _model
        if _model_future is None:
            _model_future = _model_loader.submit(_load_model)
        future = _model_future

    model = _poll_until_ready(future)
    with _model_lock:
        _model = model
    return _model


# --- Notion fallback: pull AI-meeting-note transcripts into the same pipeline ---
import httpx

_NOTION_API = "https://api.notion.com/v1"


def _notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def _notion_block_text(block_id, depth=0):
    """Recursively concatenate all rich-text under a block/page. Meeting-note
    transcripts nest under child blocks, so recursion picks them up as text."""
    if depth > 6:  # ponytail: guard pathological nesting; lectures never go this deep
        return ""
    out, cursor = [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        r = httpx.get(f"{_NOTION_API}/blocks/{block_id}/children",
                      headers=_notion_headers(), params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for b in data["results"]:
            body = b.get(b["type"], {})
            for rt in body.get("rich_text", []):
                out.append(rt.get("plain_text", ""))
            if body.get("rich_text"):
                out.append("\n")
            if b.get("has_children"):
                out.append(_notion_block_text(b["id"], depth + 1))
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return "".join(out)


def _notion_new_pages():
    """Database rows whose notion_id isn't already in Supabase."""
    seen = {r["notion_id"] for r in
            sb.table("recordings").select("notion_id").not_.is_("notion_id", "null").execute().data}
    r = httpx.post(f"{_NOTION_API}/databases/{NOTION_DB_ID}/query",
                   headers=_notion_headers(), json={"page_size": 100}, timeout=30)
    r.raise_for_status()
    pages = []
    for p in r.json()["results"]:
        if p["id"] in seen:
            continue
        title = "".join(t.get("plain_text", "")
                        for prop in p["properties"].values() if prop["type"] == "title"
                        for t in prop["title"]) or "Untitled lecture"
        pages.append({"id": p["id"], "title": title, "created_time": p["created_time"]})
    return pages


def import_notion_once():
    if not NOTION_TOKEN:
        return
    try:
        pages = _notion_new_pages()
    except Exception as e:
        print(f"[listen] notion poll failed: {e}")
        return
    for pg in pages:
        try:
            transcript = _notion_block_text(pg["id"]).strip()
            if not transcript:
                continue  # empty note, nothing to import yet
            row = sb.table("recordings").insert({
                "title": pg["title"], "status": "transcribing",
                "source": "notion", "notion_id": pg["id"],
            }).execute().data[0]
            finalize(row["id"], transcript, created_at=pg["created_time"], source="notion")
            print(f"[listen] imported notion lecture '{pg['title']}'")
        except Exception as e:
            print(f"[listen] notion import '{pg['title']}' failed: {e}")


def _notion_poll_loop():
    while True:
        import_notion_once()
        time.sleep(NOTION_POLL_MIN * 60)


def sweep_old_audio(directory=AUDIO_DIR, days=RETENTION_DAYS):
    cutoff = time.time() - days * 86400
    for f in directory.glob("*.webm"):
        if f.stat().st_mtime < cutoff:
            f.unlink()


def resume_stuck():
    # ponytail: a prior crash/hang can leave rows stuck in "transcribing"
    # with their audio still on disk — restart resolves the hang, so retry them.
    stuck = sb.table("recordings").select("id").eq("status", "transcribing").execute().data
    for row in stuck:
        if audio_path(row["id"]).exists():
            threading.Thread(target=process, args=(row["id"],), daemon=True).start()


def _warm_model():
    try:
        get_model()
        print(f"[listen] whisper model '{WHISPER_MODEL}' ready")
    except TimeoutError as e:
        print(f"[listen] whisper model warm-up: {e}")


@asynccontextmanager
async def lifespan(app):
    sweep_old_audio()  # ponytail: sweep on launch, not a cron — the tool is launched to be used
    # ponytail: warm the model in the background so the first recording
    # doesn't cold-start the download; server still starts immediately.
    threading.Thread(target=_warm_model, daemon=True).start()
    resume_stuck()
    if NOTION_TOKEN:
        threading.Thread(target=_notion_poll_loop, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.post("/start")
def start(title: str = ""):
    title = title or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    row = sb.table("recordings").insert({"title": title, "status": "recording"}).execute().data[0]
    return {"id": row["id"]}


@app.post("/chunk/{rid}")
async def chunk(rid: str, request: Request):
    data = await request.body()
    # ponytail: appends in arrival order. On localhost the 10s gap between chunks
    # serialises them in practice; if chunks ever race, switch the client to a
    # sequential sender.
    with open(audio_path(rid), "ab") as f:
        f.write(data)
    return {"ok": True}


@app.post("/stop/{rid}")
def stop(rid: str):
    sb.table("recordings").update({"status": "transcribing"}).eq("id", rid).execute()
    threading.Thread(target=process, args=(rid,), daemon=True).start()
    return {"ok": True}


@app.get("/recordings")
def recordings():
    return (
        sb.table("recordings")
        .select("id,title,created_at,status,transcript,summary,stage,progress,"
                "tokens_in,tokens_out,semester,class,unit,topic,obsidian_path,source")
        .order("created_at", desc=True)
        .execute()
        .data
    )


@app.post("/label/{rid}")
def label(rid: str, semester: str = "", klass: str = "", unit: str = "", topic: str = ""):
    """User-corrected labels: update the row, then re-file the Obsidian note
    (write_note moves it off the old path)."""
    _set(rid, **{"semester": semester, "class": klass, "unit": unit, "topic": topic})
    row = sb.table("recordings").select("*").eq("id", rid).single().execute().data
    path = write_note(row)
    _set(rid, obsidian_path=path)
    return {"ok": True, "obsidian_path": path}


def _set(rid, **fields):
    sb.table("recordings").update(fields).eq("id", rid).execute()


def _report_model_progress(rid, stop_event):
    last = None
    while not stop_event.wait(2):
        pct = _model_progress["pct"]
        if pct != last:
            _set(rid, progress=pct)
            last = pct


def process(rid):
    try:
        _set(rid, stage="loading_model", progress=_model_progress["pct"])
        stop = threading.Event()
        threading.Thread(target=_report_model_progress, args=(rid, stop), daemon=True).start()
        try:
            model = get_model()
        finally:
            stop.set()

        _set(rid, stage="transcribing", progress=0)
        with _transcribe_lock:
            segments, info = model.transcribe(str(audio_path(rid)))
            duration = max(info.duration, 1)
            parts, last_pct = [], -1
            for seg in segments:
                parts.append(seg.text)
                pct = min(99, int(seg.end / duration * 100) // 5 * 5)
                if pct != last_pct:
                    _set(rid, progress=pct)
                    last_pct = pct
        transcript = "".join(parts).strip()
        finalize(rid, transcript, source="local")
    except Exception as e:
        _set(rid, status="error", stage=None, progress=None, summary=f"[error: {e}]")


def finalize(rid, transcript, created_at=None, source="local"):
    """Shared tail for any transcript source (whisper or Notion): summarize +
    label with Claude, write the row done, file the Obsidian note."""
    _set(rid, stage="summarizing", progress=100)
    summary, cls, unit, topic, tokens_in, tokens_out = analyze(transcript)

    created_at = created_at or datetime.datetime.now().isoformat()
    fields = {
        "status": "done", "stage": None, "progress": None,
        "transcript": transcript, "summary": summary,
        "semester": _semester(created_at),
        "class": cls, "unit": unit, "topic": topic,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "source": source,
    }
    _set(rid, **fields)

    # auto-file to Obsidian; user can re-file later via /label
    row = {"id": rid, "created_at": created_at, **fields}
    path = write_note(row)
    if path:
        _set(rid, obsidian_path=path)


def analyze(transcript):
    """One Claude call: summary + college-lecture labels (class/unit/topic).
    Returns (summary, class, unit, topic, tokens_in, tokens_out)."""
    if not transcript:
        return "", "", "", "", 0, 0
    msg = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=800,
        messages=[
            {
                "role": "user",
                "content": (
                    "This is a college lecture transcript. Return ONLY a JSON object "
                    "with keys: class, unit, topic, summary.\n"
                    "- class: the course subject (e.g. 'Biology', 'US History')\n"
                    "- unit: the broader unit/module this lecture belongs to\n"
                    "- topic: the specific topic of THIS lecture (used as the note title)\n"
                    "- summary: concise key points and any action items, as a markdown "
                    "bullet list\n\nTranscript:\n" + transcript
                ),
            }
        ],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        d = json.loads(raw)
        summary = d.get("summary", "")
        cls, unit, topic = d.get("class", ""), d.get("unit", ""), d.get("topic", "")
    except (json.JSONDecodeError, AttributeError):
        # ponytail: model ignored the JSON ask — keep its text as the summary,
        # drop into an Unsorted bucket the user can re-file from the UI.
        summary, cls, unit, topic = raw, "Unsorted", "Unsorted", "Untitled"
    return summary, cls, unit, topic, msg.usage.input_tokens, msg.usage.output_tokens


def _semester(iso_date):
    """'Fall 26' / 'Spring 27' / 'Summer 26' from an ISO datetime string.
    Claude can't infer the term from lecture content, so default from the
    recording date; the user can correct it in the UI.
    SEMESTER_OVERRIDE wins when set (e.g. 'Bridge' for the pre-term pilot)."""
    override = os.getenv("SEMESTER_OVERRIDE", "").strip()
    if override:
        return override
    try:
        dt = datetime.datetime.fromisoformat((iso_date or "").replace("Z", ""))
    except ValueError:
        dt = datetime.datetime.now()
    term = "Spring" if dt.month <= 5 else "Summer" if dt.month <= 7 else "Fall"
    return f"{term} {dt:%y}"


def _slug(s):
    # ponytail: strip only chars Windows/Obsidian reject in a filename; cap length.
    s = re.sub(r'[<>:"/\\|?*\n\r]', "", (s or "").strip())
    return (s or "Untitled")[:80]


def _note_md(row):
    source = row.get("source") or "local"
    fm = {
        "semester": row.get("semester") or "",
        "class": row.get("class") or "",
        "unit": row.get("unit") or "",
        "topic": row.get("topic") or "",
        "date": (row.get("created_at") or "")[:10],
        "source": source,
        "tags": f"[lecture, {source}]",  # Obsidian reads this inline-list as tags
    }
    front = "\n".join(f"{k}: {v}" for k, v in fm.items())
    return (
        f"---\n{front}\n---\n\n"
        f"# {row.get('topic') or row.get('title') or 'Lecture'}\n\n"
        f"## Summary\n\n{row.get('summary') or ''}\n\n"
        f"## Transcript\n\n{row.get('transcript') or ''}\n"
    )


def write_note(row):
    """Writes/moves the lecture note to <vault>/<class>/<unit>/<topic>.md.
    Deletes the note at the row's old obsidian_path if the labels changed the
    destination. Returns the new absolute path (str), or None if no transcript."""
    if not (row.get("transcript") or "").strip():
        return None
    folder = (
        OBSIDIAN_VAULT / _slug(row.get("semester"))
        / _slug(row.get("class")) / _slug(row.get("unit"))
    )
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{_slug(row.get('topic'))}.md"

    old = row.get("obsidian_path")
    old_p = pathlib.Path(old) if old else None
    # a different recording already owns this filename -> disambiguate with rid
    if dest.exists() and (old_p is None or old_p.resolve() != dest.resolve()):
        dest = folder / f"{_slug(row.get('topic'))} ({row['id'][:6]}).md"
    if old_p and old_p != dest and old_p.exists():
        old_p.unlink()  # labels moved the note; drop the stale file

    dest.write_text(_note_md(row), encoding="utf-8")
    return str(dest)
