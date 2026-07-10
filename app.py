import os
import re
import json
import time
import threading
import datetime
import pathlib
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request, Body, Query
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
    if kind not in ("audio", "pdf", "syllabus", "homework"):
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


def _obsidian_uri(path):
    """obsidian://open link for a note path inside the vault, else None."""
    if not path:
        return None
    try:
        rel = pathlib.Path(path).relative_to(OBSIDIAN_VAULT)
    except ValueError:
        return None
    return ("obsidian://open?vault=" + urllib.parse.quote(OBSIDIAN_VAULT.name)
            + "&file=" + urllib.parse.quote(rel.with_suffix("").as_posix()))


@app.get("/recordings")
def recordings():
    rows = (
        sb.table("recordings")
        .select("id,title,created_at,status,transcript,summary,stage,progress,"
                "tokens_in,tokens_out,semester,class,unit,topic,obsidian_path,source,notes,"
                "pending_segments")
        .order("created_at", desc=True)
        .execute()
        .data
    )
    for r in rows:
        r["obsidian_uri"] = _obsidian_uri(r.get("obsidian_path"))
    return rows


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
            segments, tokens_in, tokens_out = analyze(old["transcript"], notes)
            # row is already filed — a multi-segment reply here just folds into one summary
            summary = segments[0]["summary"] if len(segments) == 1 else _segments_summary(segments)
            _set(rid, summary=summary, tokens_in=tokens_in, tokens_out=tokens_out)
        except Exception as e:
            error = f"notes saved, but re-summarize failed: {e}"
    row = sb.table("recordings").select("*").eq("id", rid).single().execute().data
    path = write_note(row)
    _set(rid, obsidian_path=path)
    return {"ok": error is None, "obsidian_path": path, "error": error}


@app.post("/merge_units")
def merge_units(payload: dict = Body(...)):
    """Merges one unit into another within a semester/class: relabels every
    matching recording and re-files its note (write_note moves it), then
    removes the old unit's hub note and folder if that emptied it."""
    sem, cls = payload["semester"], payload["class"]
    src, dst = payload["from_unit"], payload["to_unit"]
    if not all([sem, cls, src, dst]) or src == dst:
        return {"ok": False, "error": "bad merge args"}
    rows = (sb.table("recordings").select("*").eq("semester", sem)
            .eq("class", cls).eq("unit", src).execute().data)
    for row in rows:
        row["unit"] = dst
        _set(row["id"], unit=dst)
        path = write_note(row)  # None for rows without a transcript
        _set(row["id"], obsidian_path=path)
    old_dir = OBSIDIAN_VAULT / _slug(sem) / _slug(cls) / _slug(src)
    try:
        hub = old_dir / f"{_slug(src)}.md"
        if hub.exists():
            hub.unlink()
        old_dir.rmdir()  # only succeeds if empty
        write_graph_config()
    except OSError:
        pass  # folder still has user files — leave it
    return {"ok": True, "moved": len(rows)}


def office_text(data, ext=None):
    """pptx/docx are zips of XML — pull the text runs with stdlib only.
    ponytail: regex over Office XML, no python-pptx/docx dep; loses tables and
    layout but the Claude pass restructures to markdown anyway."""
    import io, re, zipfile
    z = zipfile.ZipFile(io.BytesIO(data))
    if ext is None:  # sniff from zip contents when the caller only has bytes
        ext = "docx" if "word/document.xml" in z.namelist() else "pptx"
    if ext == "docx":
        names, tag = ["word/document.xml"], "w:t"
    else:  # pptx: one xml per slide, sort numerically so slide10 follows slide9
        names = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                       key=lambda n: int(re.search(r"\d+", n).group()))
        tag = "a:t"
    parts = []
    for n in names:
        xml = z.read(n).decode("utf-8", "replace")
        parts.append("\n".join(m for m in re.findall(rf"<{tag}[^>]*>([^<]*)</{tag}>", xml) if m.strip()))
    import html
    return html.unescape("\n\n".join(parts))


@app.post("/ocr")
async def ocr(request: Request, filename: str = ""):
    """Photo/PDF of handwritten or printed notes -> markdown text for the notes
    box. Raw body like /upload. Returns the text; the browser appends it to the
    notes textarea so the user reviews before saving."""
    import base64
    data = await request.body()
    ext = pathlib.Path(filename).suffix.lower().lstrip(".")
    media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
             "gif": "image/gif", "webp": "image/webp"}.get(ext)
    if media:
        block = {"type": "image", "source": {"type": "base64", "media_type": media,
                                             "data": base64.b64encode(data).decode()}}
    elif ext == "pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.b64encode(data).decode()}}
    elif ext in ("txt", "md"):
        return {"text": data.decode("utf-8", "replace").strip()}  # already text — no Claude needed
    elif ext in ("pptx", "docx"):
        try:
            block = {"type": "text", "text": office_text(data, ext)}
        except Exception:
            return {"error": f"couldn't read that .{ext} file — is it corrupt?"}
    else:
        return {"error": f"unsupported file type '.{ext}' — use jpg/png/gif/webp/pdf/pptx/docx/txt/md"}
    try:
        msg = claude.messages.create(
            model="claude-haiku-4-5", max_tokens=3000,
            messages=[{"role": "user", "content": [block, {"type": "text", "text": (
                "These are a student's notes (may be handwritten). Transcribe them "
                "to markdown, faithful to the original wording and structure. Keep "
                "lists as lists; mark anything illegible as [illegible]. Return "
                "ONLY the transcription."
            )}]}],
        )
        return {"text": msg.content[0].text.strip()}
    except Exception as e:
        return {"error": f"transcription failed: {e}"}


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
    summarize + label with Claude, then either write the row done and file
    the Obsidian note (single topic), or park it split_pending with the
    proposed segments for the user to confirm via /split (multiple topics).
    The row's own source value is preserved."""
    _set(rid, stage="summarizing", progress=100)
    # honor labels/notes the user set live/before stop; Claude only fills the blanks
    pre = (sb.table("recordings").select("semester,class,unit,topic,notes,source")
           .eq("id", rid).single().execute().data or {})
    keep = lambda k, v: (pre.get(k) or "").strip() or v
    segments, tokens_in, tokens_out = analyze(transcript, pre.get("notes") or "")

    created_at = created_at or datetime.datetime.now().isoformat()

    if len(segments) > 1:
        pre_class = (pre.get("class") or "").strip()
        if pre_class:  # user already told us the class — it applies to every segment
            for seg in segments:
                seg["class"] = pre_class
        _set(rid, status="split_pending", stage=None, progress=None,
             transcript=transcript, tokens_in=tokens_in, tokens_out=tokens_out,
             semester=keep("semester", _semester(created_at)),
             pending_segments=segments, source=pre.get("source") or "local")
        return

    seg = segments[0]
    fields = {
        "status": "done", "stage": None, "progress": None,
        "transcript": transcript, "summary": seg["summary"],
        "semester": keep("semester", _semester(created_at)),
        "class": keep("class", seg["class"]), "unit": keep("unit", seg["unit"]),
        "topic": keep("topic", seg["topic"]),
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "source": pre.get("source") or "local",
    }
    _set(rid, **fields)

    # auto-file to Obsidian; user can re-file later via /label
    row = {"id": rid, "created_at": created_at, **fields}
    path = write_note(row)
    if path:
        _set(rid, obsidian_path=path)


def _segments_summary(segments):
    """Concatenates segment summaries under '## <topic>' headers — the
    'keep as one' note shape for a lecture that proposed a split but the user
    declined it. Pure/testable, mirrors build_cheatsheet's style."""
    return "\n\n".join(f"## {s.get('topic', '')}\n\n{s.get('summary', '')}" for s in segments)


@app.post("/split/{rid}")
def split(rid: str, payload: dict = Body(...)):
    """User's answer to a multi-topic split proposal (see finalize()).
    approve=true: the original row becomes segment 1 (updated in place, keeps
    its audio file); each further segment is inserted as a new row sharing
    created_at/semester/source/transcript, with its own class/unit/topic/
    summary, status='done'. Every row gets its own Obsidian note.
    approve=false: file one note, today's shape — labels from segment 1,
    summaries concatenated under '## <topic>' headers. Either way,
    pending_segments is cleared."""
    rows = sb.table("recordings").select("*").eq("id", rid).execute().data
    row = rows[0] if rows else None
    if not row or row.get("status") != "split_pending":
        return {"error": "not pending"}
    segments = row.get("pending_segments") or []
    seg0 = segments[0] if segments else {}

    if not payload.get("approve"):
        fields = {
            "status": "done", "class": seg0.get("class", ""), "unit": seg0.get("unit", ""),
            "topic": seg0.get("topic", ""), "summary": _segments_summary(segments),
            "pending_segments": None,
        }
        _set(rid, **fields)
        path = write_note({**row, **fields})
        if path:
            _set(rid, obsidian_path=path)
        return {"ok": True, "rows": [rid]}

    fields0 = {
        "status": "done", "class": seg0.get("class", ""), "unit": seg0.get("unit", ""),
        "topic": seg0.get("topic", ""), "summary": seg0.get("summary", ""),
        "pending_segments": None,
    }
    _set(rid, **fields0)
    path = write_note({**row, **fields0})
    if path:
        _set(rid, obsidian_path=path)
    ids = [rid]

    for seg in segments[1:]:
        new_row = sb.table("recordings").insert({
            "title": seg.get("topic", ""),  # list header shows title; new rows have no recording title
            "created_at": row.get("created_at"), "semester": row.get("semester"),
            "source": row.get("source"), "transcript": row.get("transcript"),
            "status": "done", "class": seg.get("class", ""), "unit": seg.get("unit", ""),
            "topic": seg.get("topic", ""), "summary": seg.get("summary", ""),
        }).execute().data[0]
        path = write_note(new_row)
        if path:
            _set(new_row["id"], obsidian_path=path)
        ids.append(new_row["id"])

    return {"ok": True, "rows": ids}


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


@app.get("/assignments_open")
def assignments_open():
    """Open (not yet submitted) assignments, soonest due first — the homework
    card's link dropdown."""
    return (sb.table("assignments").select("*").eq("status", "open")
            .order("due_on").execute().data)


@app.post("/assignments/{aid}/complete")
def assignment_complete(aid: str, payload: dict = Body(...)):
    """Mark an assignment submitted, remembering which uploaded homework
    recording fulfilled it."""
    sb.table("assignments").update(
        {"status": "submitted", "homework_id": payload.get("recording_id")}
    ).eq("id", aid).execute()
    return {"ok": True}


# --- practice quiz / test generator ---

def generate_quiz(rows, kind="quiz"):
    """One Claude call: mixed MCQ + short-answer questions from the filed
    notes' summaries. Returns the parsed question list; raises if the model
    returns unparseable JSON (caller surfaces it as an error)."""
    n_mcq, n_short = (7, 3) if kind == "quiz" else (14, 6)
    material = "\n\n".join(
        f"## {r.get('topic') or r.get('title') or ''}\n{r.get('summary') or ''}"
        for r in rows if (r.get("summary") or "").strip())
    msg = claude.messages.create(
        model="claude-haiku-4-5", max_tokens=6000,
        messages=[{"role": "user", "content": (
            f"Create a practice {kind} from these college lecture notes: "
            f"{n_mcq} multiple-choice questions and {n_short} short-answer questions. "
            "Return ONLY a JSON array; each item is one of:\n"
            '- {"type":"mcq","q":"...","choices":["...","...","...","..."],'
            '"answer":<0-3 index of the correct choice>,"explanation":"one sentence"}\n'
            '- {"type":"short","q":"...","answer":"model answer, 1-3 sentences","explanation":""}\n'
            "Cover the breadth of the material; make wrong choices plausible.\n\n"
            "Notes:\n" + material)}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


def grade_mcq(questions, answers):
    """Pure MCQ grading: (results, points, mcq_total). results aligns with the
    zipped prefix of questions/answers; short answers pass through ungraded."""
    results, pts, total = [], 0, 0
    for q, a in zip(questions, answers):
        if q.get("type") == "mcq":
            total += 1
            ok = str(a).strip() == str(q.get("answer")).strip()
            pts += ok
            results.append({"answer": a, "correct": bool(ok)})
        else:
            results.append({"answer": a})
    return results, pts, total


def grade_short(questions, results):
    """One Claude call grades all short answers; mutates their entries in
    results with score (0/0.5/1) + feedback. Returns points earned."""
    shorts = [(i, q, results[i].get("answer")) for i, q in enumerate(questions)
              if q.get("type") == "short" and i < len(results)]
    if not shorts:
        return 0.0
    listing = "\n\n".join(
        f"Question {i}: {q.get('q')}\nExpected: {q.get('answer', '')}\n"
        f"Student answer: {a or '(blank)'}" for i, q, a in shorts)
    msg = claude.messages.create(
        model="claude-haiku-4-5", max_tokens=1500,
        messages=[{"role": "user", "content": (
            "Grade these short-answer quiz responses. Return ONLY a JSON array with one "
            'object per question, in order: {"i": <question number>, "score": <0, 0.5 or 1>, '
            '"feedback": "one sentence"}. Full credit for capturing the idea in the '
            "student's own words; half for partially right.\n\n" + listing)}],
    )
    raw = re.sub(r"^```(?:json)?|```$", "", msg.content[0].text.strip(), flags=re.MULTILINE).strip()
    try:
        by_i = {g.get("i"): g for g in json.loads(raw)}
    except (json.JSONDecodeError, AttributeError, TypeError):
        by_i = {}
    pts = 0.0
    for i, q, a in shorts:
        g = by_i.get(i) or {"score": 0, "feedback": "grading failed — compare with the model answer"}
        try:
            s = min(max(float(g.get("score") or 0), 0.0), 1.0)
        except (TypeError, ValueError):
            s = 0.0
        results[i]["score"] = s
        results[i]["feedback"] = str(g.get("feedback") or "")
        pts += s
    return pts


@app.post("/quiz/generate")
def quiz_generate(payload: dict = Body(...)):
    sem = (payload.get("semester") or "").strip()
    cls = (payload.get("class") or "").strip()
    unit = (payload.get("unit") or "").strip()
    kind = payload.get("kind") if payload.get("kind") in ("quiz", "test") else "quiz"
    if not cls:
        return {"error": "class required"}
    q = (sb.table("recordings").select("topic,title,summary")
         .eq("status", "done").eq("class", cls))
    if sem:
        q = q.eq("semester", sem)
    if unit:
        q = q.eq("unit", unit)
    rows = q.execute().data
    if not any((r.get("summary") or "").strip() for r in rows):
        return {"error": "no filed notes for that scope yet"}
    try:
        questions = generate_quiz(rows, kind)
    except Exception as e:
        return {"error": f"generation failed: {e}"}
    return sb.table("quizzes").insert({
        "kind": kind, "semester": sem or None, "class": cls, "unit": unit or None,
        "questions": questions,
    }).execute().data[0]


@app.post("/quiz/{qid}/submit")
def quiz_submit(qid: str, payload: dict = Body(...)):
    quiz = sb.table("quizzes").select("*").eq("id", qid).single().execute().data
    questions = quiz["questions"] or []
    answers = payload.get("answers") or []
    answers += [None] * (len(questions) - len(answers))  # pad skipped questions
    results, pts, mcq_total = grade_mcq(questions, answers)
    pts += grade_short(questions, results)
    total = mcq_total + sum(1 for q in questions if q.get("type") == "short")
    score = round(pts / max(total, 1) * 100)
    sb.table("quizzes").update({"answers": results, "score": score}).eq("id", qid).execute()
    return {"score": score, "results": results, "questions": questions}


@app.get("/quizzes")
def quizzes():
    return (sb.table("quizzes").select("id,created_at,kind,semester,class,unit,score")
            .order("created_at", desc=True).execute().data)


@app.get("/quiz/{qid}")
def quiz_get(qid: str):
    return sb.table("quizzes").select("*").eq("id", qid).single().execute().data


# --- flashcards / spaced repetition (SM-2) ---

_GRADE_Q = {"again": 0, "hard": 3, "good": 4, "easy": 5}

def sm2(interval, reps, ease, quality):
    """Classic SuperMemo-2 update. Returns (interval_days, reps, ease).
    quality<3 is a lapse (reset reps+interval); ease floors at 1.3."""
    if quality < 3:
        reps, interval = 0, 1
    else:
        interval = 1 if reps == 0 else 6 if reps == 1 else round(interval * ease)
        reps += 1
    ease = max(1.3, ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))
    return interval, reps, ease


def generate_cards(rows, n=15):
    """One Claude call: front/back flashcards from filed-note summaries.
    Mirrors generate_quiz. Raises on unparseable JSON (caller surfaces it)."""
    material = "\n\n".join(
        f"## {r.get('topic') or r.get('title') or ''}\n{r.get('summary') or ''}"
        for r in rows if (r.get("summary") or "").strip())
    msg = claude.messages.create(
        model="claude-haiku-4-5", max_tokens=4000,
        messages=[{"role": "user", "content": (
            f"Create {n} study flashcards from these college lecture notes. "
            "Return ONLY a JSON array; each item is "
            '{"front":"a term or question","back":"the answer, 1-2 sentences"}. '
            "Cover the breadth of the material; keep each side concise.\n\n"
            "Notes:\n" + material)}],
    )
    raw = re.sub(r"^```(?:json)?|```$", "", msg.content[0].text.strip(), flags=re.MULTILINE).strip()
    return json.loads(raw)


@app.post("/cards/generate")
def cards_generate(payload: dict = Body(...)):
    sem = (payload.get("semester") or "").strip()
    cls = (payload.get("class") or "").strip()
    unit = (payload.get("unit") or "").strip()
    if not cls:
        return {"error": "class required"}
    q = (sb.table("recordings").select("topic,title,summary")
         .eq("status", "done").eq("class", cls))
    if sem:
        q = q.eq("semester", sem)
    if unit:
        q = q.eq("unit", unit)
    rows = q.execute().data
    if not any((r.get("summary") or "").strip() for r in rows):
        return {"error": "no filed notes for that scope yet"}
    try:
        cards = generate_cards(rows)
    except Exception as e:
        return {"error": f"generation failed: {e}"}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()  # app clock; DB default now() can run ahead of /cards/due's clock, hiding fresh cards
    valid = [
        {"semester": sem or None, "class": cls, "unit": unit or None,
         "front": c.get("front", ""), "back": c.get("back", ""), "due_at": now}
        for c in cards if c.get("front") and c.get("back")
    ]
    if not valid:
        return {"error": "no usable cards generated"}
    sb.table("cards").insert(valid).execute()
    return {"count": len(valid)}


@app.get("/cards/due")
def cards_due():
    """Cards whose due_at has passed, soonest first — the review queue."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return (sb.table("cards").select("*").lte("due_at", now)
            .order("due_at").execute().data)


@app.post("/cards/{cid}/review")
def cards_review(cid: str, payload: dict = Body(...)):
    quality = _GRADE_Q.get(payload.get("grade"), 4)
    card = sb.table("cards").select("interval,reps,ease").eq("id", cid).single().execute().data
    interval, reps, ease = sm2(card["interval"] or 0, card["reps"] or 0,
                               float(card["ease"] or 2.5), quality)
    due = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(days=interval)).isoformat()
    sb.table("cards").update(
        {"interval": interval, "reps": reps, "ease": ease, "due_at": due}
    ).eq("id", cid).execute()
    return {"due_at": due, "interval": interval}


def build_cards_ics(due_dates, stamp):
    """All-day VEVENTs, one per distinct due-date, summarising how many cards
    are due. Pure/testable. due_dates: list of 'YYYY-MM-DD' strings."""
    from collections import Counter
    counts = Counter(d for d in due_dates if d)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//listen//EN"]
    for date in sorted(counts):
        n = counts[date]
        noun = "flashcard" if n == 1 else "flashcards"
        lines += [
            "BEGIN:VEVENT",
            f"UID:cards-{date}@listen",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{date.replace('-', '')}",
            f"SUMMARY:{_ics_escape(f'{n} {noun} due')}",
            "CATEGORIES:flashcards",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@app.get("/cards_due.ics")
def cards_due_ics():
    """Upcoming flashcard due-dates as a calendar feed (today onward)."""
    today = datetime.date.today().isoformat()
    rows = (sb.table("cards").select("due_at").gte("due_at", today)
            .execute().data)
    dates = [(r.get("due_at") or "")[:10] for r in rows]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Response(build_cards_ics(dates, stamp), media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="cards_due.ics"'})


# --- cheat-sheet export (one-page markdown) ---

def build_cheatsheet(rows, cls, unit):
    """One markdown study sheet from filed-note summaries. Pure/testable."""
    head = f"# {cls}" + (f" — {unit}" if unit else "")
    body = "\n\n".join(
        f"## {r.get('topic') or r.get('title') or 'Untitled'}\n\n{(r.get('summary') or '').strip()}"
        for r in rows if (r.get("summary") or "").strip())
    return f"{head}\n\n{body}\n"


@app.get("/cheatsheet")
def cheatsheet(class_: str = Query("", alias="class"), unit: str = "", semester: str = ""):
    cls = class_.strip()
    sem = semester.strip()
    unit = unit.strip()
    if not cls:
        return Response("class required", media_type="text/plain", status_code=400)
    q = (sb.table("recordings").select("topic,title,summary")
         .eq("status", "done").eq("class", cls))
    if sem:
        q = q.eq("semester", sem)
    if unit:
        q = q.eq("unit", unit)
    rows = q.execute().data
    md = build_cheatsheet(rows, cls, unit)
    fname = _slug(f"{cls} {unit}".strip()) + " cheatsheet.md"
    return Response(md, media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# --- YouTube intake: reference link or full transcription ---

def _is_yt(url):
    return bool(re.match(r"https?://(www\.)?(youtube\.com|youtu\.be)/", url or ""))


def _yt_title(url):
    """Video title via YouTube's public oEmbed endpoint — no API key."""
    r = httpx.get("https://www.youtube.com/oembed",
                  params={"url": url, "format": "json"}, timeout=15)
    r.raise_for_status()
    return r.json().get("title") or url


def _yt_process(rid, url):
    """Download bestaudio with yt-dlp, then hand off to the normal whisper
    pipeline. yt-dlp picks the extension; rename to the .webm path process()
    expects (ffmpeg sniffs the real container from content)."""
    try:
        import yt_dlp
        opts = {"format": "bestaudio", "quiet": True, "noprogress": True,
                "outtmpl": str(AUDIO_DIR / f"{rid}.%(ext)s")}
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        for f in AUDIO_DIR.glob(f"{rid}.*"):
            if f.suffix != ".webm":
                f.rename(audio_path(rid))
        process(rid)
    except Exception as e:
        _set(rid, status="error", stage=None, progress=None, summary=f"[error: {e}]")


@app.post("/upload_yt")
def upload_yt(payload: dict = Body(...)):
    """mode='store': file the link as a supplemental reference note (label it
    via the editor to send it to Obsidian). mode='transcribe': download the
    audio and run the whisper pipeline like any recording."""
    url = (payload.get("url") or "").strip()
    mode = payload.get("mode") if payload.get("mode") in ("store", "transcribe") else "store"
    if not _is_yt(url):
        return {"error": "not a YouTube URL"}
    try:
        title = _yt_title(url)
    except Exception:
        title = url  # oEmbed down/video private — keep the raw link as the title
    if mode == "store":
        row = sb.table("recordings").insert({
            "title": title, "status": "done", "source": "youtube",
            "transcript": f"Supplemental video: [{title}]({url})",
            "summary": f"- Reference video — [{title}]({url})",
        }).execute().data[0]
        return {"id": row["id"]}
    row = sb.table("recordings").insert({
        "title": title, "status": "transcribing", "source": "youtube",
        "notes": f"Source video: {url}",  # analyze() weaves the link into the summary
    }).execute().data[0]
    _set(row["id"], stage="loading_model")
    threading.Thread(target=_yt_process, args=(row["id"], url), daemon=True).start()
    return {"id": row["id"]}


def _doc_block(data):
    """Claude content block for uploaded course material, sniffed from magic
    bytes: PDF -> native document block, pptx/docx (zip) -> extracted text,
    jpeg/png/gif/webp photo (e.g. homework snapshot) -> image block."""
    import base64
    if data[:4] == b"%PDF":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                               "data": base64.b64encode(data).decode()}}
    if data[:2] == b"PK":  # pptx/docx are zips — extracted text stands in for the document
        return {"type": "text", "text": "The document's extracted text:\n\n" + office_text(data)}
    media = ("image/jpeg" if data[:2] == b"\xff\xd8" else
             "image/png" if data[:4] == b"\x89PNG" else
             "image/gif" if data[:4] in (b"GIF8",) else
             "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None)
    if media:
        return {"type": "image", "source": {"type": "base64", "media_type": media,
                                            "data": base64.b64encode(data).decode()}}
    raise ValueError("unsupported file type — upload a PDF, pptx, docx, or a jpg/png/gif/webp photo")


def analyze_pdf(pdf_bytes, notes="", syllabus=False):
    """One Claude call on a PDF (native document block — no OCR dependency,
    scanned pages included). Returns (summary, semester, class, unit, topic,
    key_points, assignments, tokens_in, tokens_out); assignments is [] unless
    syllabus=True."""
    doc_block = _doc_block(pdf_bytes)
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
                doc_block,
                {"type": "text", "text": (
                    "This is course material from a college class (lecture slides, "
                    "handout, syllabus, or reading). Return ONLY a JSON object with keys: "
                    "class, unit, topic, semester, key_points, summary"
                    + (", assignments" if syllabus else "") + ".\n"
                    "- class: the course subject (e.g. 'Biology', 'US History')\n"
                    "- unit: the broader unit/module this material belongs to\n"
                    "- topic: the specific topic of THIS document, 5 words max "
                    "(used as the note title)\n"
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


def _parse_segments(raw):
    """Parses analyze()'s {"segments": [...]} JSON reply into a list of
    {class, unit, topic, summary} dicts. Pure — no network/DB — so it's
    directly testable. Falls back to one segment holding the raw text as the
    summary, Unsorted/Untitled labels, if the JSON is unparseable or empty —
    mirrors the pre-split single-object fallback."""
    try:
        segments = json.loads(raw).get("segments") or []
        if not segments:
            raise ValueError("empty segments")
        return [
            {"class": s.get("class", ""), "unit": s.get("unit", ""),
             "topic": s.get("topic", ""), "summary": s.get("summary", "")}
            for s in segments
        ]
    except (json.JSONDecodeError, AttributeError, ValueError):
        # ponytail: model ignored the JSON ask — keep its text as the summary,
        # drop into an Unsorted bucket the user can re-file from the UI.
        return [{"class": "Unsorted", "unit": "Unsorted", "topic": "Untitled", "summary": raw}]


def analyze(transcript, notes=""):
    """One Claude call: splits the lecture into topic segments — almost always
    just one — and produces college-lecture labels (class/unit/topic) +
    summary per segment. User notes, when present, are woven into the summary
    rather than kept as a separate section. Returns (segments, tokens_in,
    tokens_out); segments is a non-empty list of {class, unit, topic, summary}
    dicts."""
    if not transcript:
        return [{"class": "", "unit": "", "topic": "", "summary": ""}], 0, 0
    notes_part = (
        "\nThe student took their own notes during this lecture. Integrate them "
        "into the summary, giving weight to anything they flagged:\n"
        + notes.strip() + "\n"
    ) if (notes or "").strip() else ""
    msg = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=6000,  # multi-segment replies carry one full summary per topic
        messages=[
            {
                "role": "user",
                "content": (
                    "This is a college lecture transcript from a single microphone — "
                    "speakers are not labelled. Infer who is speaking from content: "
                    "the lecturer teaches; students ask questions or answer prompts.\n"
                    "First, check whether the lecture moves between distinct topics: "
                    "a new segment starts when the lecturer finishes one concept and "
                    "moves to a genuinely different one that a student would file as "
                    "its own note (e.g. 'derivatives of inverse functions' then "
                    "'applications of derivatives'). Segments are CONCEPTS, not "
                    "examples: several worked problems, asides, or recaps on the same "
                    "concept all belong to one segment, and a run of small topics that "
                    "share a theme is ONE segment covering that theme (e.g. assorted "
                    "application problems = one 'Applications of Derivatives' segment). "
                    "Most lectures are 1 segment, sometimes 2 — NEVER more than 3.\n"
                    "Return ONLY a JSON object with key: segments — an array of one "
                    "object per topic segment, each with keys class, unit, topic, "
                    "summary.\n"
                    "- class: the course subject (e.g. 'Biology', 'US History')\n"
                    "- unit: the broader unit/module this segment belongs to\n"
                    "- topic: the specific topic of THIS segment, 5 words max "
                    "(used as the note title)\n"
                    "- summary: markdown. First, concise key points and any action items "
                    "as a bullet list — built from the LECTURER's material; ignore student "
                    "chatter unless the lecturer engages it. Then, if any student questions "
                    "or lecturer prompts occurred during this segment, append a "
                    "'## Questions & answers' section: one bullet per exchange, '**Q:** …' "
                    "then '**A:** …' with the lecturer's response (or '*unanswered*'). Omit "
                    "the section if there were none.\n"
                    + notes_part + "\nTranscript:\n" + transcript
                ),
            }
        ],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    segments = _parse_segments(raw)
    return segments, msg.usage.input_tokens, msg.usage.output_tokens


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
    sem, cls, unit = _slug(row.get("semester")), _slug(row.get("class")), _slug(row.get("unit"))
    return (
        f"---\n{front}\n---\n\n"
        f"# {row.get('topic') or row.get('title') or 'Lecture'}\n\n"
        # strict hierarchy: topic links unit only; unit links class, class links semester
        f"Unit: [[{sem}/{cls}/{unit}/{unit}|{unit}]]\n\n"
        f"## Summary\n\n{row.get('summary') or ''}\n\n"
        f"## Transcript\n\n{row.get('transcript') or ''}\n"
    )


def _hub_desc(kind, name, context):
    """2-4 sentence description for a class/unit hub note. Claude with web
    search for background; plain Claude if the search tool isn't available."""
    prompt = (
        f"Write a 2-4 sentence encyclopedic description of the college {kind} "
        f"'{name}'. Context from the student's lecture notes:\n{(context or '')[:1500]}\n\n"
        "Search the web if helpful for accurate background. "
        "Return ONLY the description text, no preamble."
    )
    try:
        msg = claude.messages.create(
            model="claude-haiku-4-5", max_tokens=300,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
            messages=[{"role": "user", "content": prompt}])
    except Exception:
        msg = claude.messages.create(
            model="claude-haiku-4-5", max_tokens=300,
            messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def ensure_hubs(row):
    """Folder hub notes so the Obsidian graph chains
    lecture -> unit -> class -> semester. Semester hub is title-only; class
    and unit hubs get a Claude-written description (web search when available)
    plus a wikilink up the chain.
    ponytail: hubs are written once and never refreshed — delete a hub file to
    regenerate it with newer context."""
    sem, cls, unit = _slug(row.get("semester")), _slug(row.get("class")), _slug(row.get("unit"))
    summary = row.get("summary") or ""
    sem_p = OBSIDIAN_VAULT / sem / f"{sem}.md"
    cls_p = OBSIDIAN_VAULT / sem / cls / f"{cls}.md"
    unit_p = OBSIDIAN_VAULT / sem / cls / unit / f"{unit}.md"
    if not sem_p.exists():
        sem_p.parent.mkdir(parents=True, exist_ok=True)
        sem_p.write_text(f"# {sem}\n", encoding="utf-8")
    if not cls_p.exists():
        cls_p.parent.mkdir(parents=True, exist_ok=True)
        desc = _hub_desc("course", row.get("class") or cls, summary)
        cls_p.write_text(
            f"# {cls}\n\n{desc}\n\nSemester: [[{sem}/{sem}|{sem}]]\n", encoding="utf-8")
    if not unit_p.exists():
        unit_p.parent.mkdir(parents=True, exist_ok=True)
        desc = _hub_desc(f"unit of the course '{row.get('class') or cls}'",
                         row.get("unit") or unit, summary)
        unit_p.write_text(
            f"# {unit}\n\n{desc}\n\nClass: [[{sem}/{cls}/{cls}|{cls}]]\n", encoding="utf-8")


def _rgb(h, s, v):
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, min(max(s, 0), 1), min(max(v, 0), 1))
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


GRAPH_SETTINGS_FILE = pathlib.Path(__file__).with_name("graph_settings.json")
GRAPH_DEFAULTS = {                 # all fractions of 1; the app UI shows them as %
    "class_drop_min": 0.20, "class_drop_max": 0.30,  # class hubs: S/V faded this much off full
    "unit_drop_min": 0.10, "unit_drop_max": 0.30, "unit_hue_shift": 0.10,
    "topic_drop_min": 0.10, "topic_drop_max": 0.20, "topic_hue_shift": 0.05,
}


def graph_settings():
    try:
        saved = json.loads(GRAPH_SETTINGS_FILE.read_text(encoding="utf-8"))
        return {**GRAPH_DEFAULTS,
                **{k: min(max(float(saved[k]), 0.0), 1.0) for k in GRAPH_DEFAULTS if k in saved}}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return dict(GRAPH_DEFAULTS)


def write_graph_config():
    """Regenerates Obsidian graph color groups from the vault's folder tree.
    Semester hubs are white; each class gets its own evenly spaced hue at the
    configured S/V; unit hubs fade S and V by unit_drop (scaled by hue warmth —
    warm colors read as more prominent, so they fade harder) with a small hue
    spread; topic notes fade again off their unit. Knobs come from
    graph_settings.json (editable in the app's Graph colors card). Only the
    colorGroups key of .obsidian/graph.json is touched — other settings stay."""
    import math, zlib
    st = graph_settings()
    warmth = lambda h: (math.cos((h % 1.0 - 1 / 12) * 2 * math.pi) + 1) / 2  # 1 at red-orange, 0 at azure
    drop = lambda lo, hi, h: 1 - (lo + (hi - lo) * warmth(h))
    jitter = lambda name, rng: ((zlib.crc32(name.encode()) % 2001) / 1000 - 1) * rng  # stable ±rng
    groups = []
    sems = sorted(d for d in OBSIDIAN_VAULT.iterdir()
                  if d.is_dir() and not d.name.startswith("."))
    classes = [(sem, c) for sem in sems
               for c in sorted(p for p in sem.iterdir() if p.is_dir())]
    n = max(len(classes), 1)
    for i, (sem, c) in enumerate(classes):
        hue = i / n
        fc = drop(st["class_drop_min"], st["class_drop_max"], hue)  # class S/V, faded off full
        units = sorted(p for p in c.iterdir() if p.is_dir())
        m = len(units)
        for j, u in enumerate(units):
            hu = hue + st["unit_hue_shift"] * (2 * j / (m - 1) - 1 if m > 1 else 0)
            f = drop(st["unit_drop_min"], st["unit_drop_max"], hue)
            su, vu = fc * f, fc * f
            rel = f"{sem.name}/{c.name}/{u.name}"
            groups.append({"query": f'path:"{rel}/{u.name}.md"',
                           "color": {"a": 1, "rgb": _rgb(hu, su, vu)}})
            ft = drop(st["topic_drop_min"], st["topic_drop_max"], hu)
            groups.append({"query": f'path:"{rel}"',  # topic notes: another fade off the unit
                           "color": {"a": 1, "rgb": _rgb(hu + jitter(u.name, st["topic_hue_shift"]),
                                                         su * ft, vu * ft)}})
        groups.append({"query": f'path:"{sem.name}/{c.name}"',  # class hub + strays
                       "color": {"a": 1, "rgb": _rgb(hue, fc, fc)}})
    for sem in sems:
        groups.append({"query": f'path:"{sem.name}"',  # semester hubs: white
                       "color": {"a": 1, "rgb": 0xFFFFFF}})
    cfg_p = OBSIDIAN_VAULT / ".obsidian" / "graph.json"
    try:
        cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    cfg["colorGroups"] = groups
    cfg_p.parent.mkdir(parents=True, exist_ok=True)
    cfg_p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


@app.get("/graph_settings")
def get_graph_settings():
    return graph_settings()


@app.post("/graph_settings")
async def set_graph_settings(request: Request):
    posted = await request.json()
    st = graph_settings()
    for k in GRAPH_DEFAULTS:
        if k in posted:
            try:
                st[k] = min(max(float(posted[k]), 0.0), 1.0)
            except (TypeError, ValueError):
                pass
    GRAPH_SETTINGS_FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")
    write_graph_config()
    return {"ok": True, **st}


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
    try:
        ensure_hubs(row)
        write_graph_config()
    except Exception as e:
        print(f"[listen] hub notes/graph config failed (note itself is written): {e}")
    return str(dest)
