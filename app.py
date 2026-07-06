import os
import re
import json
import time
import threading
import datetime
import pathlib
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request, Body
from fastapi.responses import FileResponse, Response
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


def pdf_path(rid):
    return AUDIO_DIR / f"{rid}.pdf"


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
    patterns = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]
    try:
        # ponytail: snapshot_download prints "Downloading (incomplete total...)"
        # on every call regardless of cache hit — skip straight to the cached
        # copy when it's already there so a warm start doesn't look like a redownload.
        model_path = snapshot_download(repo_id, allow_patterns=patterns, local_files_only=True)
    except Exception:
        model_path = snapshot_download(repo_id, allow_patterns=patterns, tqdm_class=_download_progress_tqdm())
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


def _notion_page_split(page_id):
    """(transcript, notes): the subtree under the top-level block titled
    'Transcript' is the transcript; all other page text (the user's own notes,
    Notion's AI summary) is notes. No such block -> whole page is the
    transcript, notes empty (pre-notes behavior)."""
    transcript, other, cursor = [], [], None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        r = httpx.get(f"{_NOTION_API}/blocks/{page_id}/children",
                      headers=_notion_headers(), params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        for b in data["results"]:
            body = b.get(b["type"], {})
            text = "".join(rt.get("plain_text", "") for rt in body.get("rich_text", []))
            if b.get("has_children") and text.strip().lower() == "transcript":
                transcript.append(_notion_block_text(b["id"], 1))
                continue
            if text:
                other.append(text + "\n")
            if b.get("has_children"):
                other.append(_notion_block_text(b["id"], 1))
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    if transcript:
        return "".join(transcript), "".join(other)
    return "".join(other), ""


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
            transcript, notes = _notion_page_split(pg["id"])
            transcript, notes = transcript.strip(), notes.strip()
            if not transcript:
                continue  # empty note, nothing to import yet
            row = sb.table("recordings").insert({
                "title": pg["title"], "status": "transcribing",
                "source": "notion", "notion_id": pg["id"],
                "notes": notes or None,  # finalize reads it back and feeds analyze
            }).execute().data[0]
            finalize(row["id"], transcript, created_at=pg["created_time"])
            print(f"[listen] imported notion lecture '{pg['title']}'")
        except Exception as e:
            print(f"[listen] notion import '{pg['title']}' failed: {e}")


def _notion_poll_loop():
    while True:
        import_notion_once()
        time.sleep(NOTION_POLL_MIN * 60)


def sweep_old_audio(directory=AUDIO_DIR, days=RETENTION_DAYS):
    cutoff = time.time() - days * 86400
    for pattern in ("*.webm", "*.pdf"):
        for f in directory.glob(pattern):
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


@app.post("/upload")
async def upload(request: Request, kind: str = "audio", filename: str = ""):
    """Uploaded course material joins the same pipeline as a live recording.
    Raw request body (like /chunk) — multipart would drag in python-multipart."""
    data = await request.body()
    title = pathlib.Path(filename).stem or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    if kind not in ("audio", "pdf", "syllabus"):
        return {"error": f"unknown kind '{kind}'"}
    source = "upload_audio" if kind == "audio" else kind
    row = sb.table("recordings").insert(
        {"title": title, "status": "transcribing", "source": source}
    ).execute().data[0]
    rid = row["id"]
    if kind == "audio":
        # ponytail: .webm name whatever the real container — ffmpeg sniffs the format
        # from content, and sweep_old_audio's *.webm glob keeps covering the file
        audio_path(rid).write_bytes(data)
        threading.Thread(target=process, args=(rid,), daemon=True).start()
    else:
        pdf_path(rid).write_bytes(data)  # survives a crash; recovery = re-upload
        _set(rid, stage="summarizing")
        threading.Thread(target=process_pdf, args=(rid,), daemon=True).start()
    return {"id": rid}


@app.get("/recordings")
def recordings():
    return (
        sb.table("recordings")
        .select("id,title,created_at,status,transcript,summary,stage,progress,"
                "tokens_in,tokens_out,semester,class,unit,topic,obsidian_path,source,notes")
        .order("created_at", desc=True)
        .execute()
        .data
    )


@app.post("/label/{rid}")
def label(rid: str, payload: dict = Body(...)):
    """User-corrected labels + notes (JSON body — notes can exceed URL limits):
    update the row, re-integrate the summary if the notes changed on a finished
    recording, then re-file the Obsidian note (write_note moves it off the old
    path). Notes are saved before the Claude call so a failed re-summarize
    never loses them."""
    old = (sb.table("recordings").select("status,transcript,notes")
           .eq("id", rid).single().execute().data or {})
    notes = payload.get("notes", "")
    _set(rid, **{"semester": payload.get("semester", ""), "class": payload.get("klass", ""),
                 "unit": payload.get("unit", ""), "topic": payload.get("topic", ""),
                 "notes": notes})
    error = None
    if (old.get("status") == "done"
            and (old.get("transcript") or "").strip()
            and notes.strip() != (old.get("notes") or "").strip()):
        try:
            summary, *_, tokens_in, tokens_out = analyze(old["transcript"], notes)
            _set(rid, summary=summary, tokens_in=tokens_in, tokens_out=tokens_out)
        except Exception as e:
            error = f"notes saved, but re-summarize failed: {e}"
    row = sb.table("recordings").select("*").eq("id", rid).single().execute().data
    path = write_note(row)
    _set(rid, obsidian_path=path)
    return {"ok": error is None, "obsidian_path": path, "error": error}


@app.delete("/recordings/{rid}")
def delete_recording(rid: str):
    """Delete the row, its audio/pdf, and the Obsidian note the app wrote."""
    rows = sb.table("recordings").select("obsidian_path").eq("id", rid).execute().data
    sb.table("recordings").delete().eq("id", rid).execute()
    audio_path(rid).unlink(missing_ok=True)
    pdf_path(rid).unlink(missing_ok=True)
    note = rows[0].get("obsidian_path") if rows else None
    if note:
        p = pathlib.Path(note)
        # only ever remove the single file this app wrote, and only inside the vault
        if p.is_relative_to(OBSIDIAN_VAULT):
            p.unlink(missing_ok=True)
    return {"ok": True}


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
        finalize(rid, transcript)
    except Exception as e:
        _set(rid, status="error", stage=None, progress=None, summary=f"[error: {e}]")


def finalize(rid, transcript, created_at=None):
    """Shared tail for any transcript source (whisper, upload, or Notion):
    summarize + label with Claude, write the row done, file the Obsidian note.
    The row's own source value is preserved."""
    _set(rid, stage="summarizing", progress=100)
    # honor labels/notes the user set live/before stop; Claude only fills the blanks
    pre = (sb.table("recordings").select("semester,class,unit,topic,notes,source")
           .eq("id", rid).single().execute().data or {})
    keep = lambda k, v: (pre.get(k) or "").strip() or v
    summary, cls, unit, topic, tokens_in, tokens_out = analyze(transcript, pre.get("notes") or "")

    created_at = created_at or datetime.datetime.now().isoformat()
    fields = {
        "status": "done", "stage": None, "progress": None,
        "transcript": transcript, "summary": summary,
        "semester": keep("semester", _semester(created_at)),
        "class": keep("class", cls), "unit": keep("unit", unit),
        "topic": keep("topic", topic),
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "source": pre.get("source") or "local",
    }
    _set(rid, **fields)

    # auto-file to Obsidian; user can re-file later via /label
    row = {"id": rid, "created_at": created_at, **fields}
    path = write_note(row)
    if path:
        _set(rid, obsidian_path=path)


def process_pdf(rid):
    """Summarize + label an uploaded PDF and file it like a lecture: one Claude
    call on the raw document, key points into the transcript column, then the
    same write_note tail."""
    try:
        pre = (sb.table("recordings").select("semester,class,unit,topic,notes,source,created_at")
               .eq("id", rid).single().execute().data or {})
        syllabus = pre.get("source") == "syllabus"
        summary, sem, cls, unit, topic, key_points, assignments, tokens_in, tokens_out = analyze_pdf(
            pdf_path(rid).read_bytes(), pre.get("notes") or "", syllabus=syllabus)
        keep = lambda k, v: (pre.get(k) or "").strip() or v
        created_at = pre.get("created_at") or datetime.datetime.now().isoformat()
        # precedence: user label > SEMESTER_OVERRIDE > semester stated in the
        # document > recording date (_semester applies the override itself)
        sem_default = os.getenv("SEMESTER_OVERRIDE", "").strip() or sem or _semester(created_at)
        fields = {
            "status": "done", "stage": None, "progress": None,
            "transcript": key_points, "summary": summary,
            "semester": keep("semester", sem_default),
            "class": keep("class", cls), "unit": keep("unit", unit),
            "topic": keep("topic", topic),
            "tokens_in": tokens_in, "tokens_out": tokens_out,
        }
        if syllabus:
            # klass must be the post-precedence value — it's the upsert dedup key
            saved = save_assignments(rid, fields["class"], assignments)
            if saved:
                fields["summary"] += (
                    "\n\n### Due dates\n\n| Due | What | Kind |\n|---|---|---|\n"
                    + "\n".join(f"| {a['due_on']} | {a['title']} | {a['kind']} |" for a in saved)
                )
        _set(rid, **fields)
        row = {"id": rid, "created_at": created_at, "source": pre.get("source"), **fields}
        path = write_note(row)
        if path:
            _set(rid, obsidian_path=path)
    except Exception as e:
        _set(rid, status="error", stage=None, progress=None, summary=f"[error: {e}]")


def save_assignments(rid, klass, items):
    """Upsert Claude-extracted assignments on (klass, title): re-uploading a
    revised syllabus updates due_on/kind and re-parents rows to the new card
    while preserving row ids (stable ICS UIDs). Returns the saved rows."""
    rows = []
    for a in items or []:
        title = (a.get("title") or "").strip()
        try:
            due = str(datetime.date.fromisoformat(str(a.get("due_date", ""))[:10]))
        except ValueError:
            continue  # unparseable/missing date — skip rather than crash the upload
        if title:
            rows.append({"recording_id": rid, "title": title, "due_on": due,
                         "klass": klass, "kind": (a.get("kind") or "assignment")})
    if rows:
        sb.table("assignments").upsert(rows, on_conflict="klass,title").execute()
    return sorted(rows, key=lambda r: r["due_on"])


def _ics_escape(s):
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


@app.get("/assignments/{rid}.ics")
def assignments_ics(rid: str):
    """All-day VEVENTs for the syllabus card, generated on demand. Stable UIDs
    (assignment row uuid) mean re-importing updates events instead of duplicating."""
    rows = (sb.table("assignments").select("*").eq("recording_id", rid)
            .order("due_on").execute().data)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//listen//EN"]
    for a in rows:
        label = f"{a['title']} ({a['klass']})" if a["klass"] else a["title"]
        lines += [
            "BEGIN:VEVENT",
            f"UID:{a['id']}@listen",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{a['due_on'].replace('-', '')}",
            f"SUMMARY:{_ics_escape(label)}",
            f"CATEGORIES:{_ics_escape(a['kind'] or 'assignment')}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="{rid}.ics"'})


def analyze_pdf(pdf_bytes, notes="", syllabus=False):
    """One Claude call on a PDF (native document block — no OCR dependency,
    scanned pages included). Returns (summary, semester, class, unit, topic,
    key_points, assignments, tokens_in, tokens_out); assignments is [] unless
    syllabus=True."""
    import base64
    notes_part = (
        "\nThe student took their own notes on this material. Integrate them "
        "into the summary, giving weight to anything they flagged:\n"
        + notes.strip() + "\n"
    ) if (notes or "").strip() else ""
    syllabus_part = (
        "- assignments: an array of every dated deliverable in the document, each "
        "{title, due_date, kind}. due_date is 'YYYY-MM-DD' (resolve relative dates "
        "like 'Week 3' from dates stated in the document; omit items with no "
        "resolvable date). kind is one of: assignment, exam, quiz, project.\n"
    ) if syllabus else ""
    msg = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=3000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf",
                            "data": base64.b64encode(pdf_bytes).decode()}},
                {"type": "text", "text": (
                    "This is course material from a college class (lecture slides, "
                    "handout, syllabus, or reading). Return ONLY a JSON object with keys: "
                    "class, unit, topic, semester, key_points, summary"
                    + (", assignments" if syllabus else "") + ".\n"
                    "- class: the course subject (e.g. 'Biology', 'US History')\n"
                    "- unit: the broader unit/module this material belongs to\n"
                    "- topic: the specific topic of THIS document (used as the note title)\n"
                    "- semester: the term if stated in the document (e.g. 'Fall 26'), else \"\"\n"
                    "- key_points: the document's content distilled as thorough markdown "
                    "notes (this stands in for a transcript)\n"
                    "- summary: concise key points and any action items, as a markdown "
                    "bullet list\n" + syllabus_part + notes_part
                )},
            ],
        }],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    def txt(v):
        # model sometimes returns a bullet list as a JSON array — flatten to markdown
        return "\n".join(f"- {x}" for x in v) if isinstance(v, list) else (v or "")

    try:
        d = json.loads(raw)
        parts = (txt(d.get("summary")), txt(d.get("semester")), txt(d.get("class")),
                 txt(d.get("unit")), txt(d.get("topic")), txt(d.get("key_points")),
                 d.get("assignments", []))
    except (json.JSONDecodeError, AttributeError):
        # ponytail: model ignored the JSON ask — keep its text, Unsorted bucket
        parts = (raw, "", "Unsorted", "Unsorted", "Untitled", raw, [])
    return (*parts, msg.usage.input_tokens, msg.usage.output_tokens)


def analyze(transcript, notes=""):
    """One Claude call: summary + college-lecture labels (class/unit/topic).
    User notes, when present, are woven into the summary rather than kept
    as a separate section. Returns (summary, class, unit, topic, tokens_in,
    tokens_out)."""
    if not transcript:
        return "", "", "", "", 0, 0
    notes_part = (
        "\nThe student took their own notes during this lecture. Integrate them "
        "into the summary, giving weight to anything they flagged:\n"
        + notes.strip() + "\n"
    ) if (notes or "").strip() else ""
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
                    "bullet list\n" + notes_part + "\nTranscript:\n" + transcript
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
