import os
import re
import json
import time
import threading
import datetime
import pathlib
import subprocess
import traceback
import tempfile
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
PENDING_DIR = HERE / "pending"   # pages uploaded before their lecture exists
PENDING_DIR.mkdir(exist_ok=True)
FRQ_UPLOAD_DIR = HERE / "frq_uploads"  # photographed/scanned FRQ answer pages, kept for the record
FRQ_UPLOAD_DIR.mkdir(exist_ok=True)
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "7"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3-turbo")
# ponytail: this is a *stall* timeout, not a total-time budget — any download
# progress resets the clock, so a slow-but-moving multi-hour download is fine,
# only a truly dead one throws.
MODEL_LOAD_STALL_S = int(os.getenv("MODEL_LOAD_STALL_S", "300"))
OBSIDIAN_VAULT = pathlib.Path(
    os.getenv("OBSIDIAN_VAULT", str(pathlib.Path.home() / "College Lectures"))
)
# Every study artifact — announced exams, graded quizzes/tests, cheat sheets,
# flashcard decks — files under this folder inside the unit (class-level when
# the scope spans units). Was "Practice"; _migrate_prep_dirs renames old ones.
PREP_DIR = "Exam Prep"

# Original uploaded pages (ink notes, diagrams, scanned handouts) live here so
# the drawing survives Claude's transcription of it. Vault root, not beside the
# note: wikilinks resolve by filename anywhere in the vault, and notes MOVE when
# units/topics merge -- a per-unit folder would orphan its attachments. Excluded
# from the semester scan in write_graph_config, or it reads as a semester.
ATTACH_DIR = "Attachments"
# Only what Obsidian actually embeds. A .docx would write an ![[x.docx]] that
# renders as a broken link, so those stay text-only through /ocr.
ATTACH_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif")

# Shared by every prompt that may draw a diagram (lecture summaries, cheat
# sheets). Two forms, because they answer different questions: a flowchart for
# how ideas connect, a drawing for what something looks like. A flowchart of a
# boxplot ("summary -> draw rectangle -> done") says nothing the prose did not.
# Obsidian renders both natively -- mermaid fences and inline SVG -- and so does
# the in-app view, which strips the SVG down to the element and attribute lists
# below before showing it. A syntax error renders as an error box, which is
# worse than the prose it replaced, so both syntaxes are kept deliberately
# narrow. Widen only if the model proves it can stay valid.
DIAGRAM_RULES = (
    "You may draw TWO kinds of figure. Pick by what the content is.\n"
    "\n"
    "1. RELATIONSHIPS between ideas -- concept maps, hierarchies, process steps, "
    "'which method do I use' decision trees -- as a ```mermaid fenced code block "
    "using `flowchart TD` or `flowchart LR`.\n"
    "Mermaid syntax is strict. Every node label MUST be a double-quoted string: "
    'A["kinetic friction"] --> B["opposes sliding"]. Never put LaTeX, backticks, '
    "or a semicolon inside a label; keep labels under 40 characters and use plain "
    "words rather than math notation. Use `-->` for arrows and `-- \"text\" -->` "
    "for a labelled arrow. Do not use subgraphs, styling, or click handlers.\n"
    "\n"
    "2. ANYTHING WITH A SHAPE, AXIS, OR POSITION -- a boxplot with its whiskers "
    "and outlier dots, a histogram, a skewed distribution with the mean and median "
    "marked, a conic section, vectors and the angle between them, a projection, 3D "
    "axes, a free-body diagram, a number line, a labelled triangle or circle -- as "
    "a raw inline <svg> element on its own line, NOT inside a code fence.\n"
    "Draw it to scale from the actual numbers in the lecture whenever there are "
    "any: a boxplot's quartiles go where the data puts them. A figure that "
    "contradicts the numbers printed beside it is worse than no figure.\n"
    "Quote every SVG attribute with SINGLE quotes -- <svg viewBox='0 0 W H'>. "
    "The figure travels back inside a JSON string, where a double quote has to be "
    "escaped and one missed backslash loses the whole reply.\n"
    "Write it as <svg viewBox='0 0 W H' width='W' height='H'> with W at most "
    "620. Use only these elements: g, line, rect, circle, ellipse, path, polyline, "
    "polygon, text, tspan. Use only these attributes: viewBox, width, height, x, "
    "y, dx, dy, x1, y1, x2, y2, cx, cy, r, rx, ry, d, points, transform, fill, "
    "stroke, stroke-width, stroke-dasharray, stroke-linecap, fill-opacity, "
    "opacity, text-anchor, font-size, font-weight, font-style. Anything else is "
    "stripped before the figure is shown, so a figure that depends on it renders "
    "broken. Never use style=, href, <use>, <image>, <script>, or url(...).\n"
    "Set stroke='currentColor' and fill='none' so the figure reads on both a "
    "light and a dark background; for a filled region use fill='currentColor' "
    "with fill-opacity='0.12'. Draw arrowheads as a <polygon>, never a marker. "
    "Labels go in <text> at font-size 10-12 as plain words and numbers -- no "
    "LaTeX, no HTML entities. Leave ~20 units of margin inside the viewBox so "
    "nothing clips.\n"
)

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
claude = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY


def _text(msg):
    """The reply text. content[0] is not always it: Sonnet leads with a
    ThinkingBlock (no .text at all) and a web-search call interleaves result
    blocks — so join the text blocks and skip everything else."""
    return "".join(b.text for b in msg.content if getattr(b, "type", "text") == "text").strip()


def audio_path(rid):
    return AUDIO_DIR / f"{rid}.webm"


def pdf_path(rid):
    return AUDIO_DIR / f"{rid}.pdf"


# --- app-wide settings (currently just the live-transcript toggle) ---
APP_SETTINGS_FILE = pathlib.Path(__file__).with_name("app_settings.json")
APP_SETTINGS_DEFAULTS = {"live_transcript": False}


def app_settings():
    try:
        saved = json.loads(APP_SETTINGS_FILE.read_text(encoding="utf-8"))
        return {**APP_SETTINGS_DEFAULTS,
                **{k: bool(saved[k]) for k in APP_SETTINGS_DEFAULTS if k in saved}}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return dict(APP_SETTINGS_DEFAULTS)


# --- Whisper: load the model once, lazily, GPU with CPU fallback ---
_model = None
_model_future = None
_model_lock = threading.Lock()
_model_loader = ThreadPoolExecutor(max_workers=1)
_model_progress = {"pct": None}  # download %, set by the tqdm hook below
# ponytail: CTranslate2 isn't safe for concurrent transcribe() calls on one
# model instance — reuse this lock to serialize them too, not just loading.
_transcribe_lock = threading.Lock()
# Battery saver: one gate per recording, cleared = paused. process() checks
# its gate between whisper segments (the generator decodes lazily, so blocking
# there stops the CPU/GPU burn); decoding resumes where it left off. Entries
# are popped when process() ends, so a restart always comes up unpaused.
# ponytail: a row paused mid-decode holds _transcribe_lock, so rows queued
# behind it wait too — single-user, whisper is serialized anyway.
_transcribe_gates = {}


def _gate(rid):
    e = threading.Event()
    e.set()
    return _transcribe_gates.setdefault(rid, e)


def _spawn_process(rid):
    """Start a transcription worker, claiming the gate before the thread exists.
    The gate is what tells a running job from a stranded row (see
    _retry_if_stranded), and _gate is a setdefault -- so claiming it here closes
    the window between the status write and the thread's first line, where a
    sweep would otherwise start a second decode of the same recording."""
    _gate(rid)
    threading.Thread(target=process, args=(rid,), daemon=True).start()


_swept = set()  # rids the stranded-row retry has already taken once this run


def _retry_if_stranded(row, gate):
    """A worker that dies before it can write status='error' -- its own error
    write can fail too -- leaves the row reading 'transcribing' for ever, with
    nothing running and nothing shown (2026-09-16: a 76-minute lecture sat like
    that for an hour). The gate is held for the whole of process(), so
    'transcribing' + no gate + audio on disk means stranded: retry it. Once per
    server run, because a worker that dies instantly would respawn on every poll.
    ponytail: rides the list poll instead of a timer thread -- it only heals
    while the page is open, which is where a stranded row is noticed anyway.
    PDF and still-downloading YouTube rows have no audio file and are skipped.
    """
    rid = row["id"]
    if (row["status"] != "transcribing" or gate or rid in _swept
            or not audio_path(rid).exists()):
        return
    _swept.add(rid)
    print(f"[listen] {rid} stranded in 'transcribing' with no worker - retrying", flush=True)
    _spawn_process(rid)


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
    # ctranslate2 loads cublas/cudnn with plain LoadLibrary, which searches PATH
    # and ignores os.add_dll_directory — so the pip nvidia-*-cu12 wheels are
    # invisible unless their bin dirs are on PATH before the first CUDA call.
    import sys, glob
    dlls = glob.glob(os.path.join(sys.prefix, "Lib", "site-packages", "nvidia", "*", "bin"))
    os.environ["PATH"] = os.pathsep.join(dlls + [os.environ["PATH"]])

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
    except Exception as e:
        # ponytail: CPU fallback when CUDA libs aren't present; slower but works
        print(f"[whisper] CUDA unavailable ({e}); falling back to CPU", flush=True)
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


import httpx  # used by the youtube oembed lookup below


def sweep_old_audio(directory=AUDIO_DIR, days=RETENTION_DAYS):
    cutoff = time.time() - days * 86400
    for pattern in ("*.webm", "*.pdf"):
        for f in directory.glob(pattern):
            if f.stat().st_mtime < cutoff:
                f.unlink()


def resume_stuck():
    # ponytail: a prior crash/hang can leave rows stuck in "transcribing"
    # with their audio still on disk — restart resolves the hang, so retry them.
    # Rows stuck in "recording" mean the PC/browser died mid-lecture before
    # /stop: the 10s chunk uploads mean the audio up to the crash is already
    # on disk, so treat the crash as the stop and transcribe what we have
    # (ffmpeg tolerates the truncated final chunk). The server is single-user
    # and just started, so no recording can actually be live right now.
    stuck = (sb.table("recordings").select("id")
             .in_("status", ["transcribing", "recording"]).execute().data)
    for row in stuck:
        if audio_path(row["id"]).exists():
            _set(row["id"], status="transcribing")
            _spawn_process(row["id"])


def adopt_orphan_audio():
    """The mirror of resume_stuck: audio on disk with no row at all.

    /start creates the row before any chunk arrives, so a file can never
    legitimately outlive its row — an orphan is always bookkeeping damage
    (a delete whose unlink failed, or a crash between write and insert).
    The UI lists rows, so an orphan is a lecture nobody can reach; adopting
    it costs one insert and is the difference between a silent loss and a
    recording that just shows up transcribed.
    """
    known = {r["id"] for r in sb.table("recordings").select("id").execute().data}
    for f in sorted(AUDIO_DIR.glob("*.webm")):
        rid = f.stem
        if rid in known or not f.stat().st_size:
            continue
        mt = datetime.datetime.fromtimestamp(f.stat().st_mtime, datetime.timezone.utc)
        try:
            sb.table("recordings").insert({
                "id": rid,  # a non-uuid filename fails here, which is the point
                "title": mt.astimezone().strftime("%Y-%m-%d %H:%M") + " (recovered)",
                "status": "transcribing",
                "source": "local",
                "created_at": mt.isoformat(),
            }).execute()
        except Exception as e:
            print(f"[listen] orphan audio {f.name} not adopted: {e}")
            continue
        print(f"[listen] adopted orphan audio {rid} ({f.stat().st_size / 1e6:.0f} MB)")
        _spawn_process(rid)


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
    try:  # a dead DB must not kill startup
        resume_stuck()
        adopt_orphan_audio()  # after the sweep, so nothing retention just dropped comes back
    except Exception as e:
        print(f"[listen] startup housekeeping skipped -- database unreachable: {e}")
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    # no-cache, not no-store: keeps the etag round-trip (304 when unchanged) but
    # forbids serving a stale copy without asking. Matters most for the iPad
    # home-screen web app, which has no address bar and so no way to force a
    # reload -- without this it can sit on old JS with no visible fix.
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/start")
def start(title: str = ""):
    title = title or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    row = sb.table("recordings").insert({"title": title, "status": "recording"}).execute().data[0]
    if app_settings()["live_transcript"]:
        threading.Thread(target=live_preview, args=(row["id"],), daemon=True).start()
    return {"id": row["id"]}


@app.get("/app_settings")
def get_app_settings():
    return {**app_settings(), "retention_days": RETENTION_DAYS}  # read-only, for the "audio expired" message


@app.post("/app_settings")
async def set_app_settings(request: Request):
    posted = await request.json()
    st = app_settings()
    for k in APP_SETTINGS_DEFAULTS:
        if k in posted and isinstance(posted[k], bool):
            st[k] = posted[k]
    APP_SETTINGS_FILE.write_text(json.dumps(st, indent=2), encoding="utf-8")
    return {"ok": True, **st}


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
    _spawn_process(rid)
    return {"ok": True}


@app.post("/transcribe/pause/{rid}")
def transcribe_pause(rid: str):
    """Battery saver toggle for one recording: its whisper decode blocks
    between segments and picks up where it left off on resume — nothing is
    lost or restarted."""
    g = _gate(rid)
    if g.is_set():
        g.clear()
    else:
        g.set()
    return {"paused": not g.is_set()}


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
        _spawn_process(rid)
    else:
        pdf_path(rid).write_bytes(data)  # survives a crash; recovery = re-upload
        # A syllabus is administrative -- nothing to look at later. Course
        # material and homework can carry diagrams worth keeping verbatim.
        if kind != "syllabus":
            save_attachment(rid, filename, data)
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


LIST_PAGE = 200   # cards per poll; "show older" asks for more


@app.get("/labels")
def labels():
    """Every recording, labels only -- what the vault tree, the merge panel, the
    scope pickers and the datalists read. Split out of /recordings because those
    panels need the whole library while the card list only needs a page of it:
    on the poll, the full set is the one payload that still grew forever.
    Clients fetch this once and refetch only when a label or status actually moves.
    """
    rows = (sb.table("recordings")
            .select("id,created_at,status,semester,class,unit,topic,obsidian_path,source,title")
            .order("created_at", desc=True).execute().data)
    for r in rows:
        r["obsidian_uri"] = _obsidian_uri(r.get("obsidian_path"))
    return rows


@app.get("/recordings")
def recordings(limit: int = LIST_PAGE):
    rows = (
        sb.table("recordings")
        # transcript (2 MB table-wide) and summary (267 kB) are deliberately NOT
        # selected: pulling them on every 5s tick burned the whole Supabase egress
        # quota in an afternoon (402 exceed_egress_quota, 2026-09-14) and the project
        # got restricted. Both are lazy-loaded per row -- /transcript/{rid} when the
        # <details> opens, /summary/{rid} once per page load -- like /segments/{rid}.
        # pending_segments is out for the same reason and stays on disk in the DB:
        # it's 8.6 kB of proposed splits sitting on rows that resolved theirs long
        # ago, and only a split_pending card ever reads it (/pending_split/{rid}).
        .select("id,title,created_at,status,stage,progress,"
                "tokens_in,tokens_out,semester,class,unit,topic,obsidian_path,source,notes,"
                "live_transcript")
        .order("created_at", desc=True)
        .limit(max(1, limit))   # a page of cards; /labels carries the rest of the library
        .execute()
        .data
    )
    for r in rows:
        r["obsidian_uri"] = _obsidian_uri(r.get("obsidian_path"))
        g = _transcribe_gates.get(r["id"])
        r["paused"] = bool(g) and not g.is_set()
        _retry_if_stranded(r, g)
        # Pages attached to a still-transcribing recording are otherwise invisible
        # until the note is filed -- the upload looked like it did nothing.
        r["attachments"] = _attachments(r["id"])
    return rows


def _audio_mime(path):
    """Sniff the real container from magic bytes — uploads keep a .webm
    filename whatever the actual format (see /upload), so the extension lies
    and a wrong Content-Type makes browsers refuse to play a valid file."""
    try:
        head = open(path, "rb").read(12)
    except OSError:
        return "audio/webm"
    if head[:4] == b"\x1aE\xdf\xa3":
        return "audio/webm"
    if head[:4] == b"OggS":
        return "audio/ogg"
    if head[:4] == b"RIFF":
        return "audio/wav"
    if head[4:8] == b"ftyp":
        return "audio/mp4"
    if head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    return "audio/webm"


@app.get("/audio/{rid}")
def get_audio(rid: str):
    """Serves the raw recording for <audio> playback -- FileResponse handles
    Range requests so seeking works. 404s once sweep_old_audio has reaped it."""
    path = audio_path(rid)
    if not path.exists():
        return Response(json.dumps({"error": "audio expired"}), status_code=404, media_type="application/json")
    return FileResponse(path, media_type=_audio_mime(path))


@app.get("/segments/{rid}")
def get_segments(rid: str):
    """Timestamped transcript segments -- split out of /recordings since it's
    a big payload the 5s poll doesn't need."""
    rows = sb.table("recordings").select("segments").eq("id", rid).limit(1).execute().data
    return (rows[0] if rows else {}).get("segments") or []


@app.get("/transcript/{rid}")
def get_transcript(rid: str):
    """Plain transcript text -- split out of /recordings for the same reason as
    /segments: it's the biggest column in the table and the 5s poll never shows it."""
    rows = sb.table("recordings").select("transcript").eq("id", rid).limit(1).execute().data
    return {"transcript": (rows[0] if rows else {}).get("transcript") or ""}


@app.get("/pending_split/{rid}")
def get_pending_split(rid: str):
    """The proposed split for one split_pending row. Off the poll for the same
    reason as transcript and summary: resolved rows keep their old proposals, so
    the column is mostly dead weight the card list never renders."""
    rows = sb.table("recordings").select("pending_segments").eq("id", rid).limit(1).execute().data
    return {"pending_segments": (rows[0] if rows else {}).get("pending_segments") or []}


@app.post("/summaries")
def get_summaries(ids: list[str] = Body(...)):
    """Summaries (and error messages) for the rows the client hasn't cached yet.
    Batched, not per-row: the page needs every finished row's summary at once, and
    97 separate round-trips would just trade egress for latency."""
    rows = sb.table("recordings").select("id,summary").in_("id", ids).execute().data or []
    return {r["id"]: r.get("summary") or "" for r in rows}


@app.post("/label/{rid}")
def label(rid: str, payload: dict = Body(...)):
    """User-corrected labels + notes (JSON body — notes can exceed URL limits):
    update the row, re-integrate the summary if the notes changed on a finished
    recording, then re-file the Obsidian note (write_note moves it off the old
    path). Notes are saved before the Claude call so a failed re-summarize
    never loses them."""
    old = (sb.table("recordings").select("status,transcript,notes,created_at,"
                                         "semester,class,unit")
           .eq("id", rid).single().execute().data or {})
    was = (old.get("semester"), old.get("class"), old.get("unit"))  # read before _set overwrites
    notes = payload.get("notes", "")
    _set(rid, **{"semester": _norm_sem(payload.get("semester", "")), "class": payload.get("klass", ""),
                 "unit": payload.get("unit", ""), "topic": payload.get("topic", ""),
                 "notes": notes})
    error = None
    if (old.get("status") == "done"
            and (old.get("transcript") or "").strip()
            and notes.strip() != (old.get("notes") or "").strip()):
        try:
            # exams intentionally dropped here — re-summarize doesn't re-run the
            # calendar/vault exam pipeline (finalize()'s job on first pass only)
            segments, _exams, tokens_in, tokens_out = analyze(
                old["transcript"], notes, old.get("created_at"))
            summary = _resummarized(rid, old, payload.get("topic", ""), segments)
            if summary is None:
                # a split row whose segment could not be identified — leaving the
                # summary it already has beats overwriting it with the whole lecture
                error = "notes saved, but the summary was left alone: could not tell " \
                        "which half of the split lecture this row is"
            else:
                _set(rid, summary=summary, tokens_in=tokens_in, tokens_out=tokens_out)
        except Exception as e:
            error = f"notes saved, but re-summarize failed: {e}"
    row = sb.table("recordings").select("*").eq("id", rid).single().execute().data
    path = write_note(row)
    _set(rid, obsidian_path=path)
    now = (row.get("semester"), row.get("class"), row.get("unit"))
    if all(was) and was != now:
        # write_note moved the note out; the unit it left behind keeps its hub
        # note and folder forever otherwise. Guarded on an actual label change --
        # every notes edit lands here too, via the textarea's onblur.
        _cleanup_unit_dir(*was)
    return {"ok": error is None, "obsidian_path": path, "error": error}


@app.post("/addendum/{rid}")
def addendum(rid: str, payload: dict = Body(...)):
    """Appends a dated correction/addition to a finished recording and
    rewrites its Obsidian note in place. No Claude call — the summary is
    untouched; the text shows verbatim under 'Corrections & additions'."""
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "nothing to append"}
    row = sb.table("recordings").select("*").eq("id", rid).single().execute().data
    if not row or row.get("status") != "done":
        return {"ok": False, "error": "recording isn't finished yet — use the notes box instead"}
    stamp = datetime.datetime.now().strftime("%Y-%m-%d")
    combined = f"{(row.get('addendum') or '').rstrip()}\n\n**{stamp}:** {text}".strip()
    _set(rid, addendum=combined)  # saved before the file write — a failed write never loses it
    row["addendum"] = combined
    path = write_note(row)
    _set(rid, obsidian_path=path)
    return {"ok": True, "obsidian_path": path}


def _resummarized(rid, old, topic, segments):
    """The summary a re-analyzed row should keep, or None to leave it alone.

    A lecture that was split covers several topics but every row keeps the WHOLE
    transcript, so analyze() re-proposes every segment on a re-summarize. Folding
    those together -- which is what this used to do -- hands each sibling the
    entire lecture back and quietly undoes the split: the dot-product note grows
    a cross-product section, and the cross-product note grows a dot-product one.
    A row that shares its created_at with another is one of those halves, so pick
    the segment its topic names and never fold. Returns None when the segment
    cannot be identified: keeping a stale summary beats overwriting it with one
    covering material that belongs to a sibling note."""
    if len(segments) == 1:
        return segments[0]["summary"]
    split = (sb.table("recordings").select("id")
             .eq("created_at", old.get("created_at") or "").neq("id", rid).execute().data)
    if not split:
        return _segments_summary(segments)  # genuinely one row, one note, several topics
    # ranked, not thresholded: these are the segments of ONE lecture, so which
    # of them a topic is closest to is meaningful even when the absolute overlap
    # is low ('Cross product, determinants, applications' scores only 0.5 against
    # 'Cross product in 3D', which _closest's 0.6 bar would reject outright).
    ranked = sorted(((_overlap(topic or "", s.get("topic") or ""), s.get("summary") or "")
                     for s in segments), key=lambda x: x[0], reverse=True)
    clear = ranked[0][0] > 0 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0])
    return ranked[0][1] if clear else None


def _cleanup_unit_dir(sem, cls, unit):
    """Removes a unit's hub note, Timeline base and folder if relabeling/deleting
    emptied it — shared tail for merge_units, merge_topics (cross-unit),
    delete_unit."""
    old_dir = OBSIDIAN_VAULT / _slug(sem) / _slug(cls) / _slug(unit)
    hub, base = old_dir / f"{_slug(unit)}.md", old_dir / TIMELINE
    try:
        # Check BEFORE unlinking: a cross-unit merge that moves one recording out
        # of a unit that still holds notes must not take that unit's hub with it.
        # Every remaining note's "Unit:" link points at the hub, and the rmdir
        # below fails too late to put it back.
        if any(f not in (hub, base) for f in old_dir.iterdir()):
            return
        for f in (hub, base):
            if f.exists():
                f.unlink()  # both, or the rmdir below never sees an empty folder
        old_dir.rmdir()  # only succeeds if empty
        write_graph_config()
    except OSError:
        pass  # folder still has user files — leave it


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
    _cleanup_unit_dir(sem, cls, src)
    return {"ok": True, "moved": len(rows)}


@app.post("/merge_topics")
def merge_topics(payload: dict = Body(...)):
    """Merges one topic into another within a semester/class, optionally
    across units: relabels every matching recording and re-files its note
    (write_note combines it into the destination topic's one note — see
    write_note). Cross-unit merges clean up the vacated unit's hub/folder,
    same as merge_units."""
    sem, cls = payload.get("semester"), payload.get("class")
    from_unit, from_topic = payload.get("from_unit"), payload.get("from_topic")
    to_unit, to_topic = payload.get("to_unit"), payload.get("to_topic")
    if not all([sem, cls, from_unit, from_topic, to_unit, to_topic]):
        return {"ok": False, "error": "bad merge args"}
    if from_unit == to_unit and from_topic == to_topic:
        return {"ok": False, "error": "bad merge args"}
    rows = (sb.table("recordings").select("*").eq("semester", sem).eq("class", cls)
            .eq("unit", from_unit).eq("topic", from_topic).execute().data)
    for row in rows:
        row["unit"], row["topic"] = to_unit, to_topic
        _set(row["id"], unit=to_unit, topic=to_topic)
        path = write_note(row)  # None for rows without a transcript
        _set(row["id"], obsidian_path=path)
    if from_unit != to_unit:
        _cleanup_unit_dir(sem, cls, from_unit)
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


def read_page(data, filename):
    """Turn one uploaded page into markdown. Shared by /ocr (attach now) and
    /pending (attach later). Returns {"text": ...} or {"error": ...} -- never
    raises, since both callers hand the result straight back to the browser."""
    import base64
    ext = pathlib.Path(filename or "").suffix.lower().lstrip(".")
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
                "lists as lists; mark anything illegible as [illegible].\n\n"
                "Do NOT reproduce or infer diagrams. Where the page has one, write a "
                "single placeholder line naming it and listing only labels you can "
                "actually read, e.g. '[diagram: free-body diagram — labels: mg, N, f]'. "
                "The original page is stored beside these notes and renders next to "
                "them, so an honest placeholder is more useful than a guessed "
                "description. Never state a value, direction, angle or relationship "
                "the drawing does not clearly show.\n\n"
                "Return ONLY the transcription."
            )}]}],
        )
        return {"text": _text(msg)}
    except Exception as e:
        return {"error": f"transcription failed: {e}"}


def _pending_path(name):
    """Resolve a staged page by name, refusing anything that escapes the
    staging dir. `name` arrives from the browser, so treat it as hostile."""
    p = (PENDING_DIR / name).resolve()
    if p.parent != PENDING_DIR.resolve() or not p.name:
        raise ValueError("bad pending name")
    return p


def _pending_row(page):
    """One staged page as the browser sees it: the file plus its .json sidecar
    (original filename + the typed-up text the user may have corrected)."""
    meta = {}
    side = page.with_name(page.name + ".json")
    if side.exists():
        try:
            meta = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"name": page.name,
            "filename": meta.get("filename") or page.name,
            "uploaded_at": meta.get("uploaded_at")
            or datetime.datetime.fromtimestamp(page.stat().st_mtime).isoformat(),
            "text": meta.get("text") or ""}


@app.post("/pending")
async def pending_add(request: Request, filename: str = ""):
    """Stage a page that has no lecture to attach to yet — notes taken on the
    iPad reach the laptop long before (or without) the audio. Transcribed now
    so it can be proofread while the lecture is fresh; filed later from the
    Pages waiting list once the recording exists."""
    data = await request.body()
    if not data:
        return {"error": "empty upload"}
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = pathlib.Path(filename or "").suffix.lower()
    stem = _slug(pathlib.Path(filename or "page").stem)
    i = 1
    while (PENDING_DIR / f"{stamp}-{i}-{stem}{ext}").exists():
        i += 1
    page = PENDING_DIR / f"{stamp}-{i}-{stem}{ext}"
    page.write_bytes(data)
    got = read_page(data, filename)
    if got.get("error"):
        page.unlink(missing_ok=True)  # nothing readable to attach later
        return got
    page.with_name(page.name + ".json").write_text(json.dumps(
        {"filename": filename or page.name, "text": got.get("text") or "",
         "uploaded_at": datetime.datetime.now().isoformat()}), encoding="utf-8")
    return {"name": page.name, "text": got.get("text") or ""}


@app.get("/pending")
def pending_list():
    """Staged pages, newest first. The .json sidecars are metadata, not pages."""
    if not PENDING_DIR.is_dir():
        return []
    pages = [p for p in PENDING_DIR.iterdir() if p.is_file() and p.suffix != ".json"]
    return sorted((_pending_row(p) for p in pages),
                  key=lambda r: r["uploaded_at"], reverse=True)


@app.post("/pending/{name}")
def pending_edit(name: str, payload: dict = Body(...)):
    """Save the proofread text. Corrections happen here, before the page is
    ever folded into a lecture summary."""
    try:
        page = _pending_path(name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not page.exists():
        return {"ok": False, "error": "that page is no longer waiting"}
    row = _pending_row(page)
    row["text"] = payload.get("text", "")
    page.with_name(page.name + ".json").write_text(json.dumps(row), encoding="utf-8")
    return {"ok": True}


@app.post("/pending/{name}/attach")
def pending_attach(name: str, payload: dict = Body(...)):
    """File a staged page onto a recording: store the page in the vault, append
    its text to that recording's notes, and let label() re-integrate the summary
    and rewrite the note. The attachment is saved FIRST so the note write picks
    up its embed in the same pass."""
    rid = (payload.get("rid") or "").strip()
    if not rid:
        return {"ok": False, "error": "no recording chosen"}
    try:
        page = _pending_path(name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not page.exists():
        return {"ok": False, "error": "that page is no longer waiting"}
    rows = sb.table("recordings").select("*").eq("id", rid).execute().data
    if not rows:
        return {"ok": False, "error": "that recording no longer exists"}
    row = rows[0]
    meta = _pending_row(page)
    save_attachment(rid, meta["filename"], page.read_bytes())
    text = (meta.get("text") or "").strip()
    notes = "\n\n".join(x for x in [(row.get("notes") or "").strip(), text] if x)
    # label() owns the re-summarize + refile path; reuse it rather than repeat it.
    # Existing labels are passed back unchanged so only the notes actually move.
    out = label(rid, {"semester": row.get("semester") or "", "klass": row.get("class") or "",
                      "unit": row.get("unit") or "", "topic": row.get("topic") or "",
                      "notes": notes})
    page.unlink(missing_ok=True)
    page.with_name(page.name + ".json").unlink(missing_ok=True)
    return {"ok": out.get("ok", True), "error": out.get("error"),
            "obsidian_path": out.get("obsidian_path")}


@app.delete("/pending/{name}")
def pending_drop(name: str):
    try:
        page = _pending_path(name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    page.unlink(missing_ok=True)
    page.with_name(page.name + ".json").unlink(missing_ok=True)
    return {"ok": True}


@app.post("/ocr")
async def ocr(request: Request, filename: str = "", rid: str = ""):
    """Photo/PDF of handwritten or printed notes -> markdown text for the notes
    box. Raw body like /upload. Returns the text; the browser appends it to the
    notes textarea so the user reviews before saving. rid attaches the page
    itself to that recording; without one, use /pending instead."""
    data = await request.body()
    # Store the page before transcribing it. Claude can misread a diagram, and
    # without the original there is nothing to check the transcription against.
    # Silent when rid is absent (the caller isn't attaching to a recording) or
    # the type isn't embeddable.
    attached = save_attachment(rid, filename, data) if rid else None
    got = read_page(data, filename)
    if got.get("error"):
        return got
    return {"text": got.get("text") or "", "attached": attached}


@app.delete("/recordings/{rid}")
def delete_recording(rid: str):
    """Delete the row, its audio/pdf, and its Obsidian note. The note may be a
    combined file shared with other recordings on the same topic — rewrite it
    from whoever's left instead of unlinking, unless no one remains."""
    rows = (sb.table("recordings").select("obsidian_path,semester,class,unit,topic")
            .eq("id", rid).execute().data)
    row = rows[0] if rows else {}
    # ponytail: files first, row last. Windows won't unlink a file an in-flight
    # /chunk still holds open, and dropping the row first turned that failure
    # into audio no query could reach. This way a failed unlink leaves the row
    # in place, so the recording stays visible and the delete can be retried.
    audio_path(rid).unlink(missing_ok=True)
    pdf_path(rid).unlink(missing_ok=True)
    sb.table("recordings").delete().eq("id", rid).execute()
    drop_attachments(rid)  # nothing else references them; they'd sit in the vault forever
    if row.get("obsidian_path"):
        # _refile_group rebuilds from whatever remains under these labels (row
        # is already gone from the DB), or removes the file — vault check included
        _refile_group(row.get("semester"), row.get("class"), row.get("unit"), row.get("topic"))
    return {"ok": True}


@app.post("/delete_unit")
def delete_unit(payload: dict = Body(...)):
    """Deletes every recording filed under a unit (row + audio/pdf + note,
    via delete_recording), then removes the unit's hub note + folder if that
    emptied it — same tail as merge_units."""
    sem, cls, unit = payload.get("semester"), payload.get("class"), payload.get("unit")
    if not all([sem, cls, unit]):
        return {"ok": False, "error": "bad args"}
    rows = (sb.table("recordings").select("id").eq("semester", sem)
            .eq("class", cls).eq("unit", unit).execute().data)
    for row in rows:
        delete_recording(row["id"])
    _cleanup_unit_dir(sem, cls, unit)
    return {"ok": True, "deleted": len(rows)}


def _set(rid, **fields):
    # ponytail: one retry — PostgREST's gateway returns a 504 on an occasional
    # slow write (measured 2026-09-11: a 5031ms PATCH among ~1/s progress
    # writes), and every caller here is a single small row update worth
    # retrying. Two attempts, not a backoff ladder; if the DB is really down
    # the caller should hear about it.
    for attempt in range(2):
        try:
            # returning="minimal": PostgREST echoes the whole updated row by default,
            # so every progress checkpoint used to drag this row's transcript and
            # segments back over the wire. No caller reads the result.
            return (sb.table("recordings").update(fields, returning="minimal")
                    .eq("id", rid).execute())
        except Exception:
            if attempt:
                raise
            time.sleep(1)


def _checkpoint(rid, **fields):
    """Best-effort progress write. Losing one costs a little resume accuracy,
    never the job — so a transient DB failure here must not reach process()'s
    except, which would mark the whole transcription errored and strand every
    segment decoded so far. Never raises."""
    try:
        _set(rid, **fields)
    except Exception as e:
        print(f"[listen] checkpoint for {rid} failed, continuing: {e}", flush=True)


def _report_model_progress(rid, stop_event):
    last = None
    while not stop_event.wait(2):
        pct = _model_progress["pct"]
        if pct != last:
            _checkpoint(rid, progress=pct)
            last = pct


def _cap_entries(entries):
    """ponytail: past ~2000 entries (very long lectures) truncate text to 200
    chars instead of dropping entries -- keeps every timestamp, just bounds
    jsonb size. Returns copies so the caller's working list keeps full text."""
    if len(entries) > 2000:
        return [{**e, "t": e["t"][:200]} for e in entries]
    return entries


def _seg_entries(segs):
    """Whisper segments -> compact {s,e,t} timestamp dicts for the `segments`
    jsonb column."""
    return _cap_entries([{"s": round(s.start, 1), "e": round(s.end, 1), "t": s.text.strip()}
                         for s in segs])


def _resume_at(entries):
    """Second to restart whisper from, given already-checkpointed segments."""
    return entries[-1]["e"] if entries else 0


def process(rid):
    try:
        _gate(rid).wait()  # paused -> don't even start the model load
        _set(rid, stage="loading_model", progress=_model_progress["pct"])
        stop = threading.Event()
        threading.Thread(target=_report_model_progress, args=(rid, stop), daemon=True).start()
        try:
            model = get_model()
        finally:
            stop.set()

        # ponytail: crash/shutdown recovery is just the checkpoint below read back —
        # `segments` holds every decoded entry so far, so restart resumes at the last
        # one's end instead of re-decoding the whole lecture. clip_timestamps keeps
        # seg.end and info.duration absolute/full-file, so the progress math is unchanged.
        # ponytail: a >2000-segment lecture that crashes rebuilds its prefix from the
        # DB copy, whose text _cap_entries truncated to 200 chars -- resumed transcript
        # for those is lossy. Store the untruncated text elsewhere if that ever bites.
        done = (sb.table("recordings").select("segments").eq("id", rid)
                .single().execute().data or {}).get("segments") or []
        _set(rid, stage="transcribing", progress=0)
        with _transcribe_lock:
            # condition_on_previous_text=False: whisper's default feeds each
            # window its own last output, so on muffled/narrowband audio one
            # hallucinated line seeds a loop that runs for minutes. Measured on
            # the 2026-07-23 lecture: repeated sentences 12 -> 2 (first 4 min)
            # and 37 -> 4 (last 4.5 min). Costs a little cross-window context.
            # vad_filter=True cuts the loops further but drops real quiet speech
            # on this audio — not worth it.
            segments, info = model.transcribe(str(audio_path(rid)),
                                              clip_timestamps=str(_resume_at(done)),
                                              condition_on_previous_text=False)
            duration = max(info.duration, 1)
            entries, last_pct = list(done), -1
            for seg in segments:
                _gate(rid).wait()  # paused -> block before decoding further
                entries.append({"s": round(seg.start, 1), "e": round(seg.end, 1),
                                "t": seg.text.strip()})
                pct = min(99, int(seg.end / duration * 100) // 5 * 5)
                if pct != last_pct:
                    _checkpoint(rid, progress=pct, segments=_cap_entries(entries))
                    last_pct = pct
        transcript = " ".join(e["t"] for e in entries).strip()
        _set(rid, segments=_cap_entries(entries))
        finalize(rid, transcript)
    except Exception as e:
        traceback.print_exc()
        try:
            _set(rid, status="error", stage=None, progress=None,
                 summary=f"[error: {e}]", live_transcript=None)
        except Exception:
            # Marking the failure failed too: the row still reads 'transcribing'
            # and only _retry_if_stranded will pick it up. Leave a line behind so
            # the console says what happened.
            print(f"[listen] {rid} failed AND could not be marked errored", flush=True)
    finally:
        _transcribe_gates.pop(rid, None)  # restart/finish always comes up unpaused


def _append_live(old, new):
    """Appends freshly-decoded preview text onto the running live_transcript.
    Pure/testable — no dedupe of overlapping tail decodes, this is preview-quality."""
    return ((old or "") + " " + new).strip()


def live_preview(rid):
    """Optional battery-costing loop (see app_settings): every 45s, ffmpeg-extracts
    the unprocessed tail of the in-progress recording and runs whisper on just that
    slice, appending the text to live_transcript. Exits when the row leaves
    'recording', the audio file disappears, or ffmpeg isn't on PATH."""
    model = None
    offset = 0.0
    carried = ""   # our own running copy of live_transcript -- see the read below
    while True:
        time.sleep(45)
        try:
            # No .single(): PostgREST answers a missing row with 406/PGRST116, which
            # raises into the except below instead of returning None -- a row deleted
            # mid-recording used to leave this thread polling a dead id every 45s for
            # the life of the process. Status only: we are the sole writer of
            # live_transcript, so re-reading our own growing text every tick was
            # egress spent to learn what `carried` already holds.
            rows = (sb.table("recordings").select("status").eq("id", rid)
                    .limit(1).execute().data)
            if not rows or rows[0]["status"] != "recording":
                return
            path = audio_path(rid)
            if not path.exists():
                return
            tmp_path = None
            try:
                # ffmpeg runs OUTSIDE the lock — it doesn't touch the model, and
                # holding the lock through a reseek of a long recording would stall
                # a concurrent recording's real transcription for the whole decode.
                fd, tmp_path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-ss", str(offset), "-i", str(path),
                     "-ac", "1", "-ar", "16000", tmp_path],
                    check=True,
                )
                if not _transcribe_lock.acquire(blocking=False):
                    continue  # real transcription is using the model — skip this tick
                try:
                    if model is None:  # load lazily -- only after ffmpeg's confirmed present
                        model = get_model()
                    segments, info = model.transcribe(tmp_path, condition_on_previous_text=False)
                    # segments is a lazy generator — drain it INSIDE the lock,
                    # decoding is the actual model work
                    text = " ".join(seg.text.strip() for seg in segments).strip()
                finally:
                    _transcribe_lock.release()
                # ponytail: offset advances by the tail's *decoded* duration, not
                # real elapsed time -- approximate tail boundary, ceiling is fine
                # because this is preview-quality; process() re-decodes the whole
                # file from scratch when the recording actually stops.
                offset += max(info.duration, 0.0)
                if text:
                    carried = _append_live(carried, text)
                    _set(rid, live_transcript=carried)
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
        except FileNotFoundError:
            print("[listen] live preview needs ffmpeg on PATH")
            return
        except Exception as e:
            print(f"[listen] live preview tick failed: {e}")


def finalize(rid, transcript, created_at=None):
    """Shared tail for any transcript source (whisper or upload):
    summarize + label with Claude, then either write the row done and file
    the Obsidian note (single topic), or park it split_pending with the
    proposed segments for the user to confirm via /split (multiple topics).
    The row's own source value is preserved."""
    _set(rid, stage="summarizing", progress=100, live_transcript=None)
    # honor labels/notes the user set live/before stop; Claude only fills the blanks
    pre = (sb.table("recordings").select("semester,class,unit,topic,notes,source,title")
           .eq("id", rid).single().execute().data or {})
    keep = lambda k, v: (pre.get(k) or "").strip() or v
    created_at = created_at or datetime.datetime.now().isoformat()
    unit_no = _split_ordinal(pre.get("title"))[0]  # 'M1-2' -> this lecture is module 1
    # the semester decides which vault classes are candidates, so resolve it first
    semester = keep("semester", _semester(created_at))
    known = vault_tree().get(semester, [])
    # fetched before the Claude call rather than after, because the class hint
    # is derived from it — so _snap_labels now works off a snapshot taken a few
    # seconds earlier than it used to. The only difference is another recording
    # finishing inside that window, which would not have been snapped to anyway.
    done = (sb.table("recordings").select("class,unit,topic,created_at,source,semester")
            .eq("status", "done").execute().data)
    segments, exams, tokens_in, tokens_out = analyze(
        transcript, pre.get("notes") or "", created_at, known,
        _slot_class(created_at, done, semester))

    pre_class = (pre.get("class") or "").strip()
    if pre_class:  # user already told us the class — it applies to every segment
        for seg in segments:
            seg["class"] = pre_class
    # snap to existing labels so 'part 2' lectures land in the same unit/topic
    segments = _snap_labels(segments, done, known)

    if len(segments) > 1:
        _set(rid, status="split_pending", stage=None, progress=None,
             transcript=transcript, tokens_in=tokens_in, tokens_out=tokens_out,
             semester=semester,
             pending_segments=segments, source=pre.get("source") or "local")
        if exams:
            # exams are lecture-level, not per-segment, and split labels aren't
            # confirmed yet — file under the best guess now (pre_class, else
            # segment 1's class). save_assignments upserts on (klass,title) and
            # write_exam_note overwrites by slug, so a later /split relabel
            # just re-files instead of duplicating.
            klass = pre_class or segments[0]["class"]
            save_assignments(rid, klass, exams)
            for exam in exams:
                write_exam_note(semester, klass, exam)
        return

    seg = segments[0]
    fields = {
        "status": "done", "stage": None, "progress": None,
        "transcript": transcript, "summary": seg["summary"],
        "semester": semester,
        "class": keep("class", seg["class"]),
        "unit": keep("unit", _module_unit(keep("class", seg["class"]), unit_no) or seg["unit"]),
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

    if exams:
        save_assignments(rid, fields["class"], exams)  # dated ones only -> calendar
        for exam in exams:  # dated or not -> vault note either way
            write_exam_note(fields["semester"], fields["class"], exam)


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


def _known_units(rows, semester):
    """{class: [units]} already filed in a semester — the labels an upload should
    reuse instead of forking a near-duplicate folder. _snap_labels only catches
    near-identical wording ('Multivariable Calculus' against the filed
    'Differentiation of Multivariable Functions' scores 0.2 and does not snap),
    so the model gets the list up front instead of being corrected after."""
    out = {}
    for r in rows:
        if (r.get("semester") or "") != semester:
            continue
        cls, unit = (r.get("class") or "").strip(), (r.get("unit") or "").strip()
        if cls and unit and unit not in out.setdefault(cls, []):
            out[cls].append(unit)
    return out


def process_pdf(rid):
    """Summarize + label an uploaded PDF and file it like a lecture: one Claude
    call on the raw document, key points into the transcript column, then the
    same write_note tail. A document covering several topics parks as
    split_pending for the user to confirm, exactly like a multi-topic lecture."""
    try:
        pre = (sb.table("recordings").select("semester,class,unit,topic,notes,source,created_at")
               .eq("id", rid).single().execute().data or {})
        syllabus = pre.get("source") == "syllabus"
        keep = lambda k, v: (pre.get(k) or "").strip() or v
        created_at = pre.get("created_at") or datetime.datetime.now().isoformat()
        # the document may name its own semester, but the label candidates have to
        # be picked before the call -- resolve the term without it (the full
        # precedence chain runs on the reply below)
        filed = sb.table("recordings").select("class,unit,topic,semester").eq("status", "done").execute().data
        segments, sem, key_points, assignments, tokens_in, tokens_out = analyze_pdf(
            pdf_path(rid).read_bytes(), pre.get("notes") or "", syllabus=syllabus,
            homework=pre.get("source") == "homework",
            known_units=_known_units(filed, keep("semester", os.getenv("SEMESTER_OVERRIDE", "").strip()
                                                or _semester(created_at))))
        # precedence: user label > SEMESTER_OVERRIDE > semester stated in the
        # document > recording date (_semester applies the override itself)
        sem_default = os.getenv("SEMESTER_OVERRIDE", "").strip() or _norm_sem(sem) or _semester(created_at)
        semester = keep("semester", sem_default)
        # snap before keep(), same as the lecture path: an upload labelled off the
        # document alone forks a near-duplicate class/unit folder otherwise. A label
        # the user typed still wins -- keep() is applied to the snapped value.
        # transcript is what write_note files on -- an empty one files nothing at all,
        # so a reply that folded its key_points into the segments still leaves a note
        key_points = key_points.strip() or _segments_summary(segments)
        segments = _snap_labels(segments, filed, vault_tree().get(semester, []))
        pre_class = (pre.get("class") or "").strip()
        if pre_class:  # user already told us the class -- it applies to every segment
            for s in segments:
                s["class"] = pre_class
        # A handout covering several topics splits like a multi-topic lecture, and
        # through the same confirm-then-file path (see finalize()). A topic the user
        # typed is the exception: they named ONE note, so honour it and file single.
        if len(segments) > 1 and not (pre.get("topic") or "").strip():
            _set(rid, status="split_pending", stage=None, progress=None,
                 transcript=key_points, tokens_in=tokens_in, tokens_out=tokens_out,
                 semester=semester, pending_segments=segments,
                 source=pre.get("source") or "pdf")
            return
        seg = segments[0]
        # one note after all (single topic, or a topic the user named): a multi-topic
        # document still keeps every segment's summary, under '## <topic>' headers
        summary = seg.get("summary", "") if len(segments) == 1 else _segments_summary(segments)
        fields = {
            "status": "done", "stage": None, "progress": None,
            "transcript": key_points, "summary": summary,
            "semester": semester,
            "class": keep("class", seg["class"]), "unit": keep("unit", seg["unit"]),
            "topic": keep("topic", seg["topic"]),
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
            for a in assignments or []:
                if a.get("kind") in ("exam", "quiz"):
                    write_exam_note(fields["semester"], fields["class"], a)
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
    # A syllabus routinely lists several deliverables under one name ("Quiz",
    # "WebAssign HW"), but the table is unique on (klass, title) -- and Postgres
    # refuses an upsert whose own batch hits the same key twice ("ON CONFLICT DO
    # UPDATE command cannot affect row a second time"), which errored the whole
    # upload. Date-suffix only the titles that actually collide: each keeps its
    # own row, ordinary syllabi are untouched, and re-uploading the same syllabus
    # still upserts in place because the suffix is derived from the due date.
    count = {}
    for r in rows:
        count[r["title"]] = count.get(r["title"], 0) + 1
    for r in rows:
        if count[r["title"]] > 1:
            r["title"] = f'{r["title"]} ({r["due_on"]})'
    # same title AND same date is a real duplicate, not two deliverables -- keep one
    rows = list({r["title"]: r for r in rows}.values())
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
    # on the poll: only the columns hwLinker's <option> actually reads
    return (sb.table("assignments").select("id,title,due_on,klass").eq("status", "open")
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

def generate_quiz(rows, kind="quiz", fmt="mcq"):
    """One Claude call: builds a practice quiz/test from the filed notes'
    summaries. fmt='mcq' -> all multiple-choice (today's shape); fmt='frq' ->
    free-response questions with a grading rubric (answers are handwritten on
    paper, photographed, and graded later by grade_frq). Returns the parsed
    question list; raises if the model returns unparseable JSON (caller
    surfaces it as an error)."""
    material = "\n\n".join(
        f"## {r.get('topic') or r.get('title') or ''}\n{r.get('summary') or ''}"
        for r in rows if (r.get("summary") or "").strip())
    # tests simulate the real thing — skew hard; quizzes stay a breadth check
    hard = ("Bias toward harder questions: multi-step reasoning, applying "
            "concepts to unfamiliar scenarios, and combining ideas across "
            "topics — minimize pure recall.\n" if kind == "test" else "")
    if fmt == "frq":
        n = 4 if kind == "quiz" else 8
        ask = (
            f"Create a practice {kind} from these college lecture notes: {n} "
            "free-response questions. Return ONLY a JSON array; each item is "
            '{"type":"frq","q":"...","rubric":"2-4 bullet points of what a '
            'full-credit answer must include","topic":"the exact ## section '
            'header this question came from"}.\n'
            "Cover the breadth of the material.\n" + hard + "\n")
    else:
        n = 10 if kind == "quiz" else 20
        ask = (
            f"Create a practice {kind} from these college lecture notes: {n} "
            "multiple-choice questions. Return ONLY a JSON array; each item is "
            '{"type":"mcq","q":"...","choices":["...","...","...","..."],'
            '"answer":<0-3 index of the correct choice>,"explanation":"one sentence",'
            '"topic":"the exact ## section header this question came from"}.\n'
            "Cover the breadth of the material; make wrong choices plausible.\n" + hard + "\n")
    msg = claude.messages.create(
        model="claude-haiku-4-5", max_tokens=6000,
        messages=[{"role": "user", "content": ask + "Notes:\n" + material}],
    )
    raw = _text(msg)
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


def weak_topics(quiz_rows):
    """Pure aggregation for the weak-spot study loop: per-topic miss rate
    across graded quizzes. quiz_rows are quiz records with questions/answers/
    score (ungraded rows — score None or no answers — are skipped). Only
    questions carrying a "topic" key count (older quizzes predate the field
    and are silently skipped). Returns worst-first, topics with >=2 attempts
    and >=1 miss only, capped at 5: [{"topic","missed","attempted","rate"}].
    ponytail: topics join by name string — rename a topic in the label editor
    and its old quiz history orphans; join via recording ids if that bites."""
    agg = {}  # topic -> [missed, attempted]
    for row in quiz_rows:
        if row.get("score") is None:
            continue
        answers = row.get("answers") or []
        if not answers:
            continue
        for q, a in zip(row.get("questions") or [], answers):
            topic = (q or {}).get("topic")
            if not topic:
                continue
            a = a or {}
            score = a.get("score")
            # <= : exactly half credit is still a weak spot, not a pass
            miss = a.get("correct") is False or (score is not None and score <= 0.5)
            m, n = agg.get(topic, (0, 0))
            agg[topic] = (m + miss, n + 1)
    out = [{"topic": t, "missed": m, "attempted": n, "rate": round(m / n, 2)}
           for t, (m, n) in agg.items() if n >= 2 and m >= 1]
    out.sort(key=lambda r: -r["rate"])
    return out[:5]


@app.get("/weak_spots")
def weak_spots(cls: str = Query(..., alias="class"), semester: str = ""):
    q = sb.table("quizzes").select("questions,answers,semester,class,score").eq("class", cls)
    if semester:
        q = q.eq("semester", semester)
    # bounded: this reads whole question+answer blobs on a 30s timer, and weak spots
    # from your last 20 quizzes is the useful answer anyway -- a lifetime scan isn't.
    return weak_topics(q.order("created_at", desc=True).limit(20).execute().data)


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
    raw = re.sub(r"^```(?:json)?|```$", "", _text(msg), flags=re.MULTILINE).strip()
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


def grade_frq(questions, files):
    """One Sonnet vision call grades all FRQ answers from photographed/scanned
    pages against each question's rubric. files: list of raw file bytes,
    turned into content blocks via _doc_block. Returns results aligned with
    questions: {"answer": <transcription>, "score": 0/0.5/1, "feedback": ...}."""
    blocks = [_doc_block(f) for f in files]
    listing = "\n\n".join(
        f"Question {i}: {q.get('q')}\nRubric: {q.get('rubric', '')}"
        for i, q in enumerate(questions))
    msg = claude.messages.create(
        model="claude-sonnet-5", max_tokens=3000,
        messages=[{"role": "user", "content": blocks + [{"type": "text", "text": (
            "Grade the handwritten work in the attached pages against these free-response "
            "questions and rubrics. Return ONLY a JSON array with one object per question, "
            'in order: {"i": <question number>, "score": <0, 0.5 or 1>, "feedback": "1-2 '
            'sentences", "transcription": "student\'s answer, briefly"}.\n\n' + listing)}]}],
    )
    raw = re.sub(r"^```(?:json)?|```$", "", _text(msg), flags=re.MULTILINE).strip()
    try:
        by_i = {g.get("i"): g for g in json.loads(raw)}
    except (json.JSONDecodeError, AttributeError, TypeError):
        by_i = {}
    results = []
    for i, q in enumerate(questions):
        g = by_i.get(i) or {"score": 0, "feedback": "grading failed — compare with the rubric",
                             "transcription": ""}
        try:
            s = min(max(float(g.get("score") or 0), 0.0), 1.0)
        except (TypeError, ValueError):
            s = 0.0
        results.append({"answer": str(g.get("transcription") or ""), "score": s,
                         "feedback": str(g.get("feedback") or "")})
    return results


@app.post("/quiz/generate")
def quiz_generate(payload: dict = Body(...)):
    sem = (payload.get("semester") or "").strip()
    cls = (payload.get("class") or "").strip()
    unit = (payload.get("unit") or "").strip()
    kind = payload.get("kind") if payload.get("kind") in ("quiz", "test") else "quiz"
    fmt = payload.get("format") if payload.get("format") in ("mcq", "frq") else "mcq"
    if not cls:
        return {"error": "class required"}
    q = (sb.table("recordings").select("topic,title,summary,semester")
         .eq("status", "done").eq("class", cls))
    if sem:
        q = q.eq("semester", sem)
    if unit:
        q = q.eq("unit", unit)
    rows = q.execute().data
    sem = sem or _rows_semester(rows)
    topics = payload.get("topics")
    if topics:
        rows = [r for r in rows if r.get("topic") in topics]
    if not any((r.get("summary") or "").strip() for r in rows):
        return {"error": "no filed notes for that scope yet"}
    try:
        questions = generate_quiz(rows, kind, fmt)
    except Exception as e:
        return {"error": f"generation failed: {e}"}
    return sb.table("quizzes").insert({
        "kind": kind, "semester": sem or None, "class": cls, "unit": unit or None,
        "questions": questions,
    }).execute().data[0]


@app.post("/ask")
def ask_notes(payload: dict = Body(...)):
    """One Claude call: answers a question using only filed lecture-note
    summaries in scope. Returns the answer plus obsidian links for whichever
    topics the model says it drew on."""
    question = (payload.get("question") or "").strip()
    if not question:
        return {"error": "question required"}
    sem = (payload.get("semester") or "").strip()
    cls = (payload.get("class") or "").strip()
    # class required, like quiz/cards/cheatsheet: without it every filed summary in
    # the library went into one Sonnet prompt. The picker always has a class
    # selected, so this only closes the direct-API path -- the cap below is what
    # keeps a long-running class from turning one question into a 100 kB prompt.
    if not cls:
        return {"error": "class required"}
    q = (sb.table("recordings").select("semester,class,unit,topic,summary,obsidian_path")
         .eq("status", "done").eq("class", cls))
    if sem:
        q = q.eq("semester", sem)
    rows = [r for r in q.execute().data if (r.get("summary") or "").strip()]
    if not rows:
        return {"error": "no filed notes in that scope yet"}
    rows = _ask_scope(question, rows)
    material = "\n\n".join(
        f"## {r.get('topic') or ''}\n{r.get('summary') or ''}" for r in rows)
    ask = (
        "Answer the student's question using ONLY the lecture-note summaries below. "
        "Be concise. If the notes don't cover it, say so. Return ONLY a JSON object "
        '{"answer":"...","topics":["topic names actually used"]}.\n\n'
        f"Question: {question}\n\nNotes:\n" + material)
    try:
        msg = claude.messages.create(
            model="claude-sonnet-5", max_tokens=1000,
            messages=[{"role": "user", "content": ask}],
        )
    except Exception as e:  # match quiz_generate: surface as JSON, not a 500
        return {"error": f"ask failed: {e}"}
    raw = _text(msg)
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(raw)
        answer = str(parsed.get("answer") or "")
        topic_names = parsed.get("topics") or []
    except (json.JSONDecodeError, AttributeError, TypeError):
        answer, topic_names = raw, []
    by_topic = {r["topic"]: r for r in rows if r.get("topic")}
    topics = [{"topic": t, "uri": _obsidian_uri((by_topic.get(t) or {}).get("obsidian_path"))}
              for t in topic_names]
    return {"answer": answer, "topics": topics}


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
    try:
        write_quiz_note(quiz, questions, results, score)
    except Exception as e:
        print(f"[listen] quiz note write failed (grade itself is saved): {e}")
    return {"score": score, "results": results, "questions": questions}


@app.post("/quiz/{qid}/submit_frq")
def quiz_submit_frq(qid: str, payload: dict = Body(...)):
    """FRQ submission: JSON body {"files":[{"name":str,"data":<base64>}]} —
    photographed/scanned answer pages, graded in one Sonnet vision call
    (grade_frq). JSON body rather than multipart: keeps the same request
    style as every other endpoint and skips the python-multipart dependency
    (see /upload's docstring for the same call on audio/PDF uploads)."""
    import base64
    quiz = sb.table("quizzes").select("*").eq("id", qid).single().execute().data
    questions = quiz["questions"] or []
    files_in = payload.get("files") or []
    if not files_in:
        return {"error": "no files uploaded"}
    file_bytes = []
    for n, f in enumerate(files_in):
        try:
            data = base64.b64decode(f.get("data") or "")
        except (ValueError, TypeError):
            return {"error": f"file {n + 1}: invalid upload"}
        suffix = pathlib.Path(f.get("name") or "").suffix or ".jpg"
        (FRQ_UPLOAD_DIR / f"{qid}_{n}{suffix}").write_bytes(data)
        file_bytes.append(data)
    try:
        results = grade_frq(questions, file_bytes)
    except Exception as e:
        return {"error": f"grading failed: {e}"}
    pts = sum(r["score"] for r in results)
    score = round(pts / max(len(questions), 1) * 100)
    sb.table("quizzes").update({"answers": results, "score": score}).eq("id", qid).execute()
    try:
        write_quiz_note(quiz, questions, results, score)
    except Exception as e:
        print(f"[listen] quiz note write failed (grade itself is saved): {e}")
    return {"score": score, "results": results, "questions": questions}


@app.get("/quizzes")
def quizzes():
    # .limit() below: same 30s timer, and nobody reads past the first screen of history
    return (sb.table("quizzes").select("id,created_at,kind,semester,class,unit,score")
            .order("created_at", desc=True).limit(50).execute().data)


@app.get("/quiz/{qid}")
def quiz_get(qid: str):
    return sb.table("quizzes").select("*").eq("id", qid).single().execute().data


# --- study material from tree selection ---

def _scope_rows(rows, scopes):
    """Rows matching any scope (unit match, topic match if given). One pass
    over rows so a unit scope plus a topic scope inside it can't duplicate."""
    return [r for r in rows if any(
        r.get("unit") == s.get("unit") and (not s.get("topic") or r.get("topic") == s.get("topic"))
        for s in scopes)]


@app.post("/study/generate")
def study_generate(payload: dict = Body(...)):
    sem = (payload.get("semester") or "").strip()
    cls = (payload.get("class") or "").strip()
    kind = payload.get("kind")
    fmt = payload.get("format") if payload.get("format") in ("mcq", "frq") else "mcq"
    scopes = payload.get("scopes")
    if kind not in ("quiz", "test", "flashcards", "cheatsheet"):
        return {"error": "bad kind"}
    if not cls:
        return {"error": "class required"}
    if not isinstance(scopes, list) or not scopes or not all(
            isinstance(s, dict) and (s.get("unit") or "").strip() for s in scopes):
        return {"error": "scopes required"}

    q = (sb.table("recordings").select("topic,title,summary,unit,semester")
         .eq("status", "done").eq("class", cls))
    if sem:
        q = q.eq("semester", sem)
    rows = _scope_rows(q.execute().data, scopes)
    sem = sem or _rows_semester(rows)
    if not any((r.get("summary") or "").strip() for r in rows):
        return {"error": "no filed notes for that scope yet"}

    units = {s["unit"] for s in scopes}
    unit = next(iter(units)) if len(units) == 1 else None

    if kind in ("quiz", "test"):
        try:
            questions = generate_quiz(rows, kind, fmt)
        except Exception as e:
            return {"error": f"generation failed: {e}"}
        row = sb.table("quizzes").insert({
            "kind": kind, "semester": sem or None, "class": cls, "unit": unit,
            "questions": questions,
        }).execute().data[0]
        try:  # file the blank test now so it lands under Exam Prep even before grading
            write_quiz_note(row, questions, [], None)
        except Exception as e:
            print(f"[listen] blank quiz note write failed (quiz itself is saved): {e}")
        return row

    if kind == "flashcards":
        try:
            cards = generate_cards(rows)
        except Exception as e:
            return {"error": f"generation failed: {e}"}
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        valid = [
            {"semester": sem or None, "class": cls, "unit": unit,
             "front": c.get("front", ""), "back": c.get("back", ""), "due_at": now}
            for c in cards if c.get("front") and c.get("back")
        ]
        if not valid:
            return {"error": "no usable cards generated"}
        sb.table("cards").insert(valid).execute()
        return {"count": len(valid), "path": write_cards_note(sem, cls, unit or "")}

    try:
        md = build_cheatsheet(rows, cls, unit or "")
    except Exception as e:
        return {"error": f"generation failed: {e}"}
    fname = _slug(f"{cls} {unit or ''}".strip()) + " cheatsheet.md"
    # filed in the vault (opens in Obsidian) as well as returned for the in-app view
    path = write_prep_note(sem, cls, fname, md)
    return {"filename": fname, "markdown": md,
            "path": path, "obsidian": _obsidian_uri(path)}


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
    raw = re.sub(r"^```(?:json)?|```$", "", _text(msg), flags=re.MULTILINE).strip()
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
    sem = sem or _rows_semester(rows)
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
    return {"count": len(valid), "path": write_cards_note(sem, cls, unit)}


@app.get("/cards/due")
def cards_due():
    """Cards whose due_at has passed, soonest first — the review queue."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return (sb.table("cards").select("*").lte("due_at", now)
            .order("due_at").execute().data)


@app.get("/cards/due_count")
def cards_due_count():
    """How many cards are due — all the 5s poll needs to paint the badge.
    The rows themselves cost front+back for every due card and are only read
    when the user actually starts a review, the same split /recordings had to
    make after its poll pulled whole transcripts and cost us the egress quota."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    r = sb.table("cards").select("id", count="exact").lte("due_at", now).limit(1).execute()
    return {"due": r.count or 0}


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
    """One Claude call: a scannable one-page study sheet — tables and diagrams
    rather than the wall of bullets that concatenating every lecture summary
    produced. Raises on API failure so the caller can surface it, matching
    generate_quiz/generate_cards."""
    material = "\n\n".join(
        f"## {r.get('topic') or r.get('title') or 'Untitled'}\n{(r.get('summary') or '').strip()}"
        for r in rows if (r.get("summary") or "").strip())
    head = f"# {cls}" + (f" — {unit}" if unit else "")
    msg = claude.messages.create(
        model="claude-sonnet-5", max_tokens=4000,
        messages=[{"role": "user", "content": (
            "Condense these college lecture notes into ONE cheat sheet a student "
            f"can scan the night before an exam. Start it with the title '{head}'.\n"
            "A cheat sheet SELECTS — it is not a summary of everything. Keep only "
            "what is most likely to be tested; cut recaps, logistics, and anything "
            "that restates a neighbour. Target one printed page: 6 sections at most.\n"
            "Make it visual. Almost every section should be a table or a diagram:\n"
            "- formulas as a table: | Quantity | Formula | When to use |\n"
            "- contrasts as a comparison table, one row per property\n"
            "- relationships, procedures, and method-choice as flowcharts\n"
            "Use bullets only where neither fits, never more than 4 in a row, one "
            "line each. No worked examples and no question-and-answer transcripts.\n"
            "Write math as LaTeX ($...$ inline, $$...$$ display).\n" + DIAGRAM_RULES +
            "Return ONLY the cheat sheet markdown, starting with the title. Do not "
            "wrap your reply in a code fence.\n\nNotes:\n" + material)}],
    )
    return _text(msg) + "\n"


@app.get("/cheatsheet")
def cheatsheet(class_: str = Query("", alias="class"), unit: str = "", semester: str = ""):
    cls = class_.strip()
    sem = semester.strip()
    unit = unit.strip()
    if not cls:
        return Response("class required", media_type="text/plain", status_code=400)
    q = (sb.table("recordings").select("topic,title,summary,semester")
         .eq("status", "done").eq("class", cls))
    if sem:
        q = q.eq("semester", sem)
    if unit:
        q = q.eq("unit", unit)
    rows = q.execute().data
    sem = sem or _rows_semester(rows)
    try:
        md = build_cheatsheet(rows, cls, unit)
    except Exception as e:
        return Response(f"generation failed: {e}", media_type="text/plain", status_code=502)
    fname = _slug(f"{cls} {unit}".strip()) + " cheatsheet.md"
    write_prep_note(sem, cls, fname, md)  # file it, then serve the download
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
    _gate(row["id"])  # _yt_process downloads into audio_path before process() claims it
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


def analyze_pdf(pdf_bytes, notes="", syllabus=False, homework=False, known_units=None):
    """One Claude call on a PDF (native document block — no OCR dependency,
    scanned pages included). Returns (segments, semester, key_points,
    assignments, tokens_in, tokens_out): segments is one {class, unit, topic,
    summary} per topic the document covers, same shape the lecture path uses, so
    a multi-topic handout files as several notes. assignments is [] unless
    syllabus=True. homework=True swaps the distill-the-slides framing for a
    readable per-problem write-up — a syllabus and a problem set are each one
    document by definition, so both stay single-segment."""
    doc_block = _doc_block(pdf_bytes)
    notes_part = (
        "\nThe student took their own notes on this material. Integrate them "
        "into the summary, giving weight to anything they flagged:\n"
        + notes.strip() + "\n"
    ) if (notes or "").strip() else ""
    syllabus_part = (
        "- assignments: an array of every dated deliverable in the document, each "
        "{title, due_date, kind, format, topics}. due_date is 'YYYY-MM-DD' (resolve "
        "relative dates like 'Week 3' from dates stated in the document; omit items "
        "with no resolvable date). kind is one of: assignment, exam, quiz, project. "
        "format and topics are optional — for exam/quiz rows especially, include "
        "format (free text, e.g. '50 multiple choice, closed book') and topics "
        "(array of strings) when the document states them; omit otherwise.\n"
    ) if syllabus else ""
    units_part = (
        "\nUnits the student already files under, by class: "
        + "; ".join(f"'{c}': " + ", ".join(f"'{u}'" for u in us)
                    for c, us in sorted((known_units or {}).items()))
        + ". If this document belongs under one of them, write that string EXACTLY "
        "as `unit`, character for character. Only name a new unit when it fits "
        "none of them.\n"
    ) if known_units else ""
    # A packet of lecture notes routinely covers several topics -- the lecture path
    # has split them into their own notes since 2026-07-10, the upload path filed
    # them as one note called 'Tangent Planes and Chain Rule'. Same 'and' test,
    # same cap. A syllabus and a problem set are each one document by definition.
    split_part = "" if (syllabus or homework) else (
        "If the document covers more than ONE topic, replace the class, unit, topic "
        "and summary keys with a 'segments' key: an array of one "
        "{class, unit, topic, summary} object per topic, each filled in by the rules "
        "above and below. key_points and semester stay at the TOP LEVEL either way — "
        "one copy for the whole document, never inside a segment. "
        "Test: if the topic label you would write needs an 'and' to "
        "cover the document (e.g. 'Tangent planes and chain rule'), that is TWO "
        "topics, not one -- split it and give each half its own label and summary. "
        "Worked examples are not topics: several problems practising one concept all "
        "belong to that concept. Usually 1-2, sometimes 3 -- NEVER more than 4.\n"
    )
    content_part = (
        "- key_points: the assignment rewritten as clean, readable markdown "
        "(this stands in for a transcript): one '### Problem <number or short "
        "name>' section per problem, each with the full problem statement, the "
        "student's work if visible, a step-by-step solution, and a final "
        "'**Answer:**' line (LaTeX $...$ for math)\n"
        "- summary: a short plain-English overview — what the assignment covers, "
        "which concepts it practices, and anything incomplete or worth "
        "revisiting, as a markdown bullet list\n"
        "Write ALL math notation as LaTeX ($...$ inline, $$...$$ for display "
        "equations) — never plain-text approximations like x^2, sqrt(), or "
        "unicode fractions.\n"
    ) if homework else (
        "- key_points: the document's content distilled as thorough markdown "
        "notes (this stands in for a transcript)\n"
        "- summary: concise key points and any action items, as a markdown "
        "bullet list. If the document works through example problems, append "
        "a '## Worked examples' section: one '### <short problem name>' per "
        "problem with the problem statement, key solution steps, and final "
        "answer (LaTeX $...$ for math); omit if none\n"
        "Write ALL math notation as LaTeX ($...$ inline, $$...$$ for display "
        "equations) — never plain-text approximations like x^2, sqrt(), or "
        "unicode fractions.\n"
    )
    msg = claude.messages.create(
        model="claude-haiku-4-5",
        # 3000 truncated homework per-problem write-ups mid-JSON (unparseable -> raw blob stored);
        # 8000 then did the same to a real syllabus, which pays twice -- key_points restates the
        # whole document AND every dated deliverable follows it, so the assignments array is what
        # gets cut. haiku-4-5 allows 64000 out; 16000 is the ceiling that still returns inside the
        # SDK's non-streaming HTTP timeout. Past that, switch this call to .stream().
        max_tokens=16000 if syllabus else 8000,
        messages=[{
            "role": "user",
            "content": [
                doc_block,
                {"type": "text", "text": (
                    ("This is a homework assignment from a college class (typed or "
                     "a photo of handwritten work). "
                     if homework else
                     "This is course material from a college class (lecture slides, "
                     "handout, syllabus, or reading). ")
                    + "Return ONLY a JSON object with keys: "
                    "class, unit, topic, semester, key_points, summary"
                    + (", assignments" if syllabus else "") + ".\n"
                    "- class: the course subject (e.g. 'Biology', 'US History')\n"
                    "- unit: the broader unit/module this material belongs to\n"
                    "- topic: the specific topic of THIS document, 5 words max "
                    "(used as the note title)\n"
                    "- semester: the term if stated in the document (e.g. 'Fall 26'), else \"\"\n"
                    + units_part + split_part + content_part + syllabus_part + notes_part
                )},
            ],
        }],
    )
    raw = _text(msg)
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    def txt(v):
        # model sometimes returns a bullet list as a JSON array — flatten to markdown
        return "\n".join(f"- {x}" for x in v) if isinstance(v, list) else (v or "")

    def segs(d):
        """The reply's topics as lecture-shaped segments. A single-topic document
        answers with flat class/unit/topic/summary keys (and older replies always
        did), so that shape stays the fallback."""
        listed = [s for s in (d.get("segments") or []) if isinstance(s, dict)]
        if not listed:
            listed = [d]
        return [{"class": txt(s.get("class")), "unit": txt(s.get("unit")),
                 "topic": txt(s.get("topic")), "summary": txt(s.get("summary"))}
                for s in listed]

    try:
        d = json.loads(raw)
        parts = (segs(d), txt(d.get("semester")), txt(d.get("key_points")),
                 d.get("assignments", []))
    except (json.JSONDecodeError, AttributeError):
        # ponytail: model ignored the JSON ask — keep its text, Unsorted bucket
        parts = ([{"class": "Unsorted", "unit": "Unsorted", "topic": "Untitled",
                   "summary": raw}], "", raw, [])
    if syllabus or homework:
        parts = (parts[0][:1], *parts[1:])  # one document, one note — never split
    return (*parts, msg.usage.input_tokens, msg.usage.output_tokens)


def _escape_stray_quotes(raw):
    """Backslashes double quotes the model left raw inside a JSON string.
    Haiku writes prose like `(e.g., "Project A117 update" not "update")` inside
    a summary value -- fine as English, fatal to the parse, and the whole
    lecture then lands in the Unsorted bucket with the raw reply as its summary.
    A quote really closes a string only when the next non-space character is one
    of ,:}] or the text ends; anything else means the model is still mid-
    sentence, so escape it and read on.

    ponytail: a heuristic, not a JSON grammar -- prose that ends a quotation
    right before a comma (`he said "hello", then left`) still fools it. Swap for
    the API's structured-output/tool-use JSON mode if that ever shows up."""
    out, in_str, esc = [], False, False
    for i, c in enumerate(raw):
        if esc:
            out.append(c)
            esc = False
            continue
        if c == "\\":
            out.append(c)
            esc = in_str
            continue
        if c == '"':
            if not in_str:
                in_str = True
            elif next((d for d in raw[i + 1:] if not d.isspace()), "") in ",:}]":
                in_str = False
            else:
                out.append("\\")
        out.append(c)
    return "".join(out)


def _json_obj(raw):
    """The first JSON object in raw, ignoring any prose the model appended
    after it. json.JSONDecoder().raw_decode stops at the end of the object, so
    trailing '---\n**Note on transcript quality:** ...' commentary no longer
    poisons the parse. Returns {} when there is no JSON object at all.
    Deliberately NOT brace-counting/regex brace-matching: summaries contain
    LaTeX with literal braces (\frac{1}{2}, x^{2}) that would break that.
    One salvage attempt on failure, for unescaped quotes inside a value."""
    start = raw.find("{")
    if start == -1:
        return {}
    for text in (raw, _escape_stray_quotes(raw)):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, start)
        except (json.JSONDecodeError, ValueError):
            continue
        return obj if isinstance(obj, dict) else {}
    return {}


def _parse_segments(raw):
    """Parses analyze()'s {"segments": [...]} JSON reply into a list of
    {class, unit, topic, summary} dicts. Pure — no network/DB — so it's
    directly testable. Falls back to one segment holding the raw text as the
    summary, Unsorted/Untitled labels, if the JSON is unparseable or empty —
    mirrors the pre-split single-object fallback."""
    segments = _json_obj(raw).get("segments") or []
    if not segments:
        # ponytail: model ignored the JSON ask — keep its text as the summary,
        # drop into an Unsorted bucket the user can re-file from the UI.
        return [{"class": "Unsorted", "unit": "Unsorted", "topic": "Untitled", "summary": raw}]
    return [
        {"class": s.get("class", ""), "unit": s.get("unit", ""),
         "topic": s.get("topic", ""), "summary": s.get("summary", "")}
        for s in segments
    ]


COURSE_CODE = re.compile(r"^\s*[A-Za-z]{2,4}\s*\d{3,4}[A-Za-z]?\b")


def _norm_words(s):
    """Lowercase word set for fuzzy label matching: drops tiny words,
    strips a trailing 's' so 'Derivatives' matches 'derivative'. A leading
    course code goes too: the vault folder carries the catalogue name off the
    syllabus ('MATH 2110Q Multivariable Calculus') while Claude, hearing only
    the lecture, writes the plain one — without this they score 0.5 and the
    plain name becomes a second folder. A class named by code alone strips to
    an empty word set and simply never snaps."""
    s = COURSE_CODE.sub("", s or "")
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) >= 3}


def _overlap(a, b):
    """Jaccard similarity of two labels' word sets, 0.0 to 1.0."""
    aw, bw = _norm_words(a), _norm_words(b)
    return len(aw & bw) / len(aw | bw) if (aw or bw) else 0.0


ASK_NOTES_CAP = 40   # ~110 kB of summaries; past that a question is a shotgun anyway


def _ask_scope(question, rows):
    """The notes a question actually gets asked against, capped at ASK_NOTES_CAP.

    Ranked by how much of the QUESTION each note covers, not by _overlap: that's
    Jaccard, and a long summary shares few words with a short question, so the
    most detailed lecture in the class would rank last. Rows that survive keep
    their original newest-first order, and a question with no usable words falls
    back to the most recent notes rather than an arbitrary slice.
    """
    if len(rows) <= ASK_NOTES_CAP:
        return rows
    qw = _norm_words(question)
    if not qw:
        return rows[:ASK_NOTES_CAP]

    def covered(r):
        text = " ".join(filter(None, (r.get("topic"), r.get("unit"), r.get("summary"))))
        return len(qw & _norm_words(text)) / len(qw)

    keep = sorted(range(len(rows)), key=lambda i: (-covered(rows[i]), i))[:ASK_NOTES_CAP]
    return [rows[i] for i in sorted(keep)]


def _closest(name, existing):
    """The existing label whose word set best overlaps name's (Jaccard),
    or None when nothing clears the bar — exact matches trivially win.
    Ties break toward the LONGER name: once a class has forked into both
    'MATH 2110Q Multivariable Calculus' and 'Multivariable Calculus' they
    normalize identically and both score 1.0, and picking by set iteration
    order would file each new lecture into whichever folder hashed first.
    The catalogue name is the one to converge on."""
    best, score = None, 0.0
    for e in existing:
        j = _overlap(name, e)
        if (j, len(e)) > (score, len(best or "")):
            best, score = e, j
    return best if score >= 0.6 else None


def _snap_labels(segments, rows, vault_classes=()):
    """Snaps each segment's class/unit/topic to the closest existing label
    (rows = done recordings' class/unit/topic dicts), so a lecture continuing
    a topic in another session files into the same folders/note name instead
    of a near-duplicate ('Implicit Differentiation Part 2' -> 'Implicit
    Differentiation'). vault_classes are this semester's class folders — a
    class scaffolded from the syllabus but not yet recorded into is otherwise
    invisible here, so the first lecture forks a folder beside it. Pure —
    testable without DB. Mutates and returns segments."""
    for seg in segments:
        c = _closest(seg.get("class"),
                     {r["class"] for r in rows if r.get("class")} | set(vault_classes))
        if c:
            seg["class"] = c
        u = _closest(seg.get("unit"), {r["unit"] for r in rows
                                       if r.get("class") == seg["class"] and r.get("unit")})
        if u:
            seg["unit"] = u
        t = _closest(seg.get("topic"), {r["topic"] for r in rows
                                        if r.get("class") == seg["class"]
                                        and r.get("unit") == seg["unit"] and r.get("topic")})
        if t:
            seg["topic"] = t
    return segments


# Record is pressed at the bell or a little after, never before, so a slot needs
# slack for a late start. Less of it across weekdays: a timetable repeats at the
# SAME clock time on its other days, and a wide window there starts colliding
# with a different class that happens to meet near that time on another day.
SLOT_MINUTES = 30
SLOT_MINUTES_OTHER_DAY = 10


def _local(ts):
    """Wall-clock local time for a stored timestamp. The weekly timetable lives
    in local time, so comparing the raw UTC values would drift by an hour at the
    daylight-saving boundary halfway through a semester."""
    try:
        return datetime.datetime.fromisoformat(ts).astimezone()
    except (TypeError, ValueError):
        return None


def _slot_class(created_at, rows, semester=""):
    """The class that already meets in this recording's weekly slot.

    College classes run on a fixed timetable, which makes the start time the
    strongest available signal about which class a lecture belongs to -- and
    nothing in the pipeline was using it. Given only a list of course names,
    the model re-guesses from scratch every lecture, and files 'dot product,
    cross product, determinants' under a data-analysis course because the words
    fit as well as they fit multivariable calculus.

    Matched on time of day rather than weekday, because one class meets on
    several of them (the Monday lecture is what identifies the Wednesday one),
    with the same weekday preferred when it disambiguates two classes sharing a
    start time. Nearest start wins, so a lecture started a few minutes late still
    matches its own slot instead of falling between two of them. Only live
    recordings count: an upload's created_at is when the file was sent, not when
    the class met, and so does only the semester asked for -- a replay over the
    real history had last term's 14:46 physics slot naming this term's 14:46
    communications lecture. Returns None when nothing is close or two classes
    tie -- no hint beats a wrong one. Pure -- no DB, no Claude.

    ponytail: compares minute-of-day, so a slot spanning midnight never matches.
    """
    when = _local(created_at)
    if not when:
        return None
    mins = lambda t: t.hour * 60 + t.minute
    near = []
    for r in rows:
        # 'local' is the only source whose created_at is the real class time
        if r.get("source") != "local" or not r.get("class"):
            continue
        if semester and r.get("semester") != semester:
            continue  # last term's timetable says nothing about this one's
        t = _local(r.get("created_at"))
        if not t:
            continue
        gap, other_day = abs(mins(t) - mins(when)), t.weekday() != when.weekday()
        if gap <= (SLOT_MINUTES_OTHER_DAY if other_day else SLOT_MINUTES):
            near.append((other_day, gap, r["class"]))
    if not near:
        return None
    near.sort(key=lambda c: c[:2])   # same weekday first, then nearest start
    tied = {c[2] for c in near if c[:2] == near[0][:2]}
    return near[0][2] if len(tied) == 1 else None


def _parse_exams(raw):
    """Parses analyze()'s {"exams": [...]} JSON reply defensively — missing
    key, non-list, or unparseable JSON all fall back to no exams detected.
    Pure — no network/DB — so it's directly testable."""
    exams = _json_obj(raw).get("exams")
    return exams if isinstance(exams, list) else []


def analyze(transcript, notes="", created_at=None, known_classes=(), slot_class=""):
    """One Claude call: splits the lecture into topic segments — almost always
    just one — and produces college-lecture labels (class/unit/topic) +
    summary per segment, plus any announced upcoming exams/quizzes. User
    notes, when present, are woven into the summary rather than kept as a
    separate section. created_at (the recording's date) lets the model
    resolve relative dates lecturers say out loud ('next Friday'). Returns
    (segments, exams, tokens_in, tokens_out); segments is a non-empty list of
    {class, unit, topic, summary} dicts, exams a (usually empty) list of
    {title, due_date, kind, format, topics} dicts."""
    if not transcript:
        return [{"class": "", "unit": "", "topic": "", "summary": ""}], [], 0, 0
    notes_part = (
        "\nThe student took their own notes during this lecture. These are sparse "
        "on purpose: they cover what the audio could NOT carry — material the "
        "lecturer wrote on the board without saying aloud, diagrams, and the "
        "correct spelling of technical terms a single-microphone transcript "
        "often garbles. Where the notes and the transcript disagree, TRUST THE "
        "NOTES, especially for names, symbols, numbers and spellings. Integrate "
        "them into the summary, giving weight to anything they flagged. A line "
        "like '[diagram: ...]' is a placeholder for a drawing filed beside this "
        "note — mention what it depicts, never describe detail you were not "
        "given:\n"
        + notes.strip() + "\n"
    ) if (notes or "").strip() else ""
    # the vault folder is the catalogue name off the syllabus; hearing only the
    # lecture, the model writes the plain one and forks a second class folder.
    # _snap_labels is the net under this, but naming it here is what stops it.
    classes_part = (
        "\nThe student already has these classes: "
        + ", ".join(f"'{c}'" for c in known_classes)
        + ". If this lecture belongs to one of them, use that string EXACTLY as "
        "`class`, character for character, including any course code. Only write "
        "a new class name if the lecture genuinely fits none of them.\n"
    ) if known_classes else ""
    # course names alone do not separate two classes that both touch vectors --
    # the timetable does. A hint, not a pre-set: a makeup session or an exam
    # review recorded in the slot has to be able to say otherwise.
    slot_part = (
        f"\nEvery lecture the student has recorded in this same weekly time slot "
        f"has been '{slot_class}'. Classes meet on a fixed timetable, so this one "
        "is very probably that class too — use it unless the content plainly "
        "belongs to a different class in the list above.\n"
    ) if slot_class else ""
    lecture_date = (created_at or "")[:10]
    date_part = (
        f"\nThis lecture was recorded on {lecture_date}. Resolve any relative "
        "dates the lecturer mentions ('next Friday', 'after spring break', "
        "'two weeks from now') against this date.\n"
    ) if lecture_date else ""
    msg = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=6000,  # multi-segment replies carry one full summary per topic
        messages=[
            {
                "role": "user",
                "content": (
                    classes_part
                    + slot_part
                    + "This is a college lecture transcript from a single microphone — "
                    "speakers are not labelled. Infer who is speaking from content: "
                    "the lecturer teaches; students ask questions or answer prompts.\n"
                    "First, check whether the lecture moves between distinct topics: "
                    "a new segment starts when the lecturer finishes one concept and "
                    "moves to a genuinely different one that a student would file as "
                    "its own note (e.g. 'derivatives of inverse functions' then "
                    "'applications of derivatives'). Segments are CONCEPTS, not "
                    "examples: several worked problems, asides, tangents, off-topic "
                    "chatter, or recaps on the same concept all belong to one segment. "
                    "A distinct named technique, method, rule, or theorem that the "
                    "lecturer introduces and teaches in its own right IS its own segment, "
                    "even when it shares a unit with what came before (e.g. "
                    "'antiderivatives' then 'u-substitution' = two segments, both in the "
                    "Integration unit). Test: if the topic label you'd write needs an "
                    "'and' to cover what happened (e.g. 'Antiderivatives and "
                    "u-substitution'), that is TWO segments, not one — split it and give "
                    "each half its own label.\n"
                    "Lectures often OPEN by reviewing the previous class — an opening "
                    "recap is NEVER its own segment, even when its subject differs from "
                    "today's material: fold it into the first new-material segment.\n"
                    "Usually 1-2 segments, sometimes 3 — NEVER more than 4. When torn "
                    "between one compound segment and two clean ones, SPLIT.\n"
                    "Return ONLY a JSON object with keys: segments, exams.\n"
                    "segments is an array of one object per topic segment, each with "
                    "keys class, unit, topic, summary.\n"
                    "- class: the course subject (e.g. 'Biology', 'US History')\n"
                    "- unit: the broader unit/module this segment belongs to\n"
                    "- topic: the specific topic of THIS segment, 5 words max "
                    "(used as the note title)\n"
                    "- summary: markdown. If the segment opens with a recap of the "
                    "previous class, start with a '## Review of last class' section of "
                    "2-4 brief bullets (just enough to jog memory — the detail lives in "
                    "that class's own note); omit the section if there was no recap. "
                    "Then concise key points and any action items "
                    "as a bullet list — built from the LECTURER's material; ignore student "
                    "chatter unless the lecturer engages it. Where a picture carries an "
                    "idea better than a paragraph — how concepts relate, the steps of a "
                    "procedure, a classification, a cause-and-effect chain, what "
                    "something looks like (a shape, a plot, a distribution, a boxplot, "
                    "vectors and an angle), or a diagram the lecturer drew on the "
                    "board — draw it instead of describing it. "
                    "At most two diagrams per summary, and none at all if the material "
                    "genuinely isn't visual.\n" + DIAGRAM_RULES +
                    "Then, if the lecturer worked "
                    "through any in-class problems or examples in this segment, append a "
                    "'## Worked examples' section: one '### <short problem name>' per "
                    "problem with the problem statement, the key solution steps, and the "
                    "final answer (use LaTeX $...$ for math). Skip trivial throwaway "
                    "illustrations; omit the section if there were none. Then, if any "
                    "student questions or lecturer prompts occurred during this segment, "
                    "append a "
                    "'## Questions & answers' section: one bullet per exchange, '**Q:** …' "
                    "then '**A:** …' with the lecturer's response (or '*unanswered*'). Omit "
                    "the section if there were none. Write ALL math notation as LaTeX "
                    "($...$ inline, $$...$$ for display equations) — never plain-text "
                    "approximations like x^2, sqrt(), or unicode fractions.\n"
                    "exams is an array (usually empty) of upcoming tests/quizzes/exams the "
                    "lecturer announces or discusses in this transcript — ignore mentions of "
                    "past exams or hypotheticals. Each item: {title, due_date, kind, format, "
                    "topics}.\n"
                    "- title: short descriptive name (e.g. 'Midterm 1', 'Quiz on derivatives')\n"
                    "- due_date: 'YYYY-MM-DD', or \"\" if it can't be resolved\n"
                    "- kind: 'exam' or 'quiz' (map 'test'/'midterm'/'final' to 'exam')\n"
                    "- format: what the lecturer says about the exam's format (e.g. '50 "
                    "multiple choice, no calculator'), else \"\"\n"
                    "- topics: array of topics/units/chapters the lecturer says it covers, "
                    "else []\n"
                    + notes_part + date_part + "\nTranscript:\n" + transcript
                ),
            }
        ],
    )
    raw = _text(msg)
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    segments = _parse_segments(raw)
    exams = _parse_exams(raw)
    return segments, exams, msg.usage.input_tokens, msg.usage.output_tokens


# "en-M1-2_ The AI Revolution", "M1-2 ...", "1-2 ..." -- lecture files are often
# named <unit>-<topic>. The numbers order the course; they are NOT labels, so they
# never reach the class/unit/topic columns. Anchored at the start and followed by a
# non-word char so "MATH 2110Q" and "2026-08-30" can't match.
_ORDINAL_RE = re.compile(r"""^\W*
    (?:[A-Za-z]{2,3}[-_ ])?                  # language/source prefix, e.g. 'en-'
    (?:M(?:od(?:ule)?)?|U(?:nit)?|W(?:eek)?)?\s*
    (\d{1,2})[-._ ](\d{1,3})
    (?=[^0-9A-Za-z]|$)""", re.X | re.I)


def _split_ordinal(title):
    """(unit_no, topic_no, title without the ordinal) -- (None, None, title)
    when the title carries no <unit>-<topic> prefix."""
    m = _ORDINAL_RE.match(title or "")
    if not m:
        return None, None, (title or "").strip()
    return int(m.group(1)), int(m.group(2)), (title or "")[m.end():].lstrip(" _-.:").strip()


def _module_unit(cls, unit_no):
    """Unit label an earlier lecture of the same module number already used, so
    M1-1 and M1-2 share one unit even when Claude names them differently."""
    if not cls or unit_no is None:
        return ""
    rows = (sb.table("recordings").select("title,unit")
            .eq("class", cls).eq("status", "done").execute().data)
    for r in rows:
        if _split_ordinal(r.get("title"))[0] == unit_no and (r.get("unit") or "").strip():
            return r["unit"].strip()
    return ""


def _norm_sem(s):
    """'Fall 2026' -> 'Fall 26', the shape _semester() mints. Claude reads the
    term off a syllabus in whichever form the document uses, and the two
    spellings mint two semester folders for one term. Anything that isn't
    <Term> <4-digit year> passes through untouched, so 'Bridge' and a
    SEMESTER_OVERRIDE of any shape keep working."""
    return re.sub(r"\b(Spring|Summer|Fall|Winter)\s+\d{2}(\d{2})\b", r"\1 \2",
                  (s or "").strip())


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


def _one_line(s, cap=200):
    """One-line gist of a summary for note frontmatter — the Timeline bases show
    this, and Bases can only read frontmatter, never note bodies. Summaries are
    markdown (## headings, bullets, **bold** labels), so take the first line that
    isn't a heading and strip its markup.
    ponytail: first real line, no model call — this is a card subtitle."""
    skip = False
    for raw in (s or "").splitlines():
        if raw.lstrip().startswith("#"):
            # "## Review of last class" recaps the PREVIOUS lecture — never the gist
            skip = "review" in raw.lower()
            continue
        if skip:
            continue
        # only * and ` — underscores here are LaTeX subscripts ($\mu_k$), not emphasis
        line = re.sub(r"[*`]", "", raw).strip().lstrip("-").strip()
        if not line or (len(line) < 40 and line.endswith(":")):
            continue  # bare section label, e.g. **Key Points:**
        return line[:cap].rsplit(" ", 1)[0] + "…" if len(line) > cap else line
    return ""


def _folded(text, title="Transcript"):
    """Transcript in a collapsed Obsidian callout. The "-" after the type is
    what starts it folded, so opening a note shows the summary rather than a
    wall of raw speech -- native markdown, no plugin. Blank lines still need
    their ">" or the callout ends at the first one."""
    rows = (text or "").splitlines() or [""]
    body = "\n".join(("> " + ln) if ln.strip() else ">" for ln in rows)
    return f"> [!quote]- {title}\n{body}"


# "2026-09-04 08:36", the title /start gives a recording nobody named — and
# " (recovered)" is appended when a dropped upload is recovered.
_STAMP_TITLE = re.compile(r"^\d{4}-\d{2}-\d{2} (?P<time>\d{2}:\d{2})( \(recovered\))?$")


def _note_md(rows):
    """Combined note for every recording sharing one topic (rows: non-empty
    transcripts, oldest first — see _group_rows). Frontmatter/H1/date come
    from the earliest recording. A single recording keeps the original
    Summary/Transcript layout so most notes don't churn; multiple get all
    dated summaries up top, then a Transcripts section with one dated
    subsection per recording."""
    first = rows[0]
    source = first.get("source") or "local"
    fm = {
        "semester": first.get("semester") or "",
        "class": first.get("class") or "",
        "unit": first.get("unit") or "",
        "topic": first.get("topic") or "",
        "source": source,
        "summary": _one_line(first.get("summary")),
    }
    u_no, t_no, _ = _split_ordinal(first.get("title"))
    if u_no is not None:  # zero-padded: Bases sorts it as a string, so "01-10" must follow "01-02"
        fm["seq"] = f"{u_no:02d}-{t_no:02d}"
    # json.dumps is a valid YAML double-quoted scalar. Labels and summaries carry
    # colons, quotes and unicode math; unquoted, one of those breaks the WHOLE
    # frontmatter block and the note drops out of every Timeline base.
    front = "\n".join(f"{k}: {json.dumps(v)}" for k, v in fm.items())
    # date stays UNQUOTED so Obsidian types it as a date, not a string — plain
    # YYYY-MM-DD never needs escaping, and Bases sorts/filters it as a date
    front += (f"\ndate: {(first.get('created_at') or '')[:10]}"
              f"\nlectures: {len(rows)}"
              f"\ntags: [lecture, {source}]")  # Obsidian reads this inline-list as tags
    sem, cls, unit = _slug(first.get("semester")), _slug(first.get("class")), _slug(first.get("unit"))
    head = (
        f"---\n{front}\n---\n\n"
        f"# {first.get('topic') or first.get('title') or 'Lecture'}\n\n"
        # strict hierarchy: topic links unit only; unit links class, class links semester
        f"Unit: [[{sem}/{cls}/{unit}/{unit}|{unit}]]\n\n"
    )
    def extra(r, h):  # user corrections/additions appended after filing — verbatim, never resummarized
        a = (r.get("addendum") or "").strip()
        return f"\n\n{h} Corrections & additions\n\n{a}" if a else ""

    def pages(r, h):
        """Embeds of the uploaded pages. Claude's transcription can misread a
        diagram; the original renders right beside it, so the error is visible
        and fixable instead of silently standing in for the drawing."""
        names = _attachments(r.get("id"))
        if not names:
            return ""
        return f"\n\n{h} Source pages\n\n" + "\n".join(f"![[{x}]]" for x in names)

    if len(rows) == 1:
        return (
            head
            + f"## Summary\n\n{first.get('summary') or ''}"
            + extra(first, "##")
            + pages(first, "##")
            + "\n\n" + _folded(first.get("transcript") or "") + "\n"
        )
    def heading(r):
        """Dated section header for one recording inside a combined note. A
        recording nobody named gets a "%Y-%m-%d %H:%M" stamp for a title, which
        rendered as "2026-09-04 — 2026-09-04 08:36" and said nothing about the
        section. Fall back to the topic, keeping the stamp's time so two
        unnamed recordings on one day still get distinct headers."""
        day = (r.get("created_at") or "")[:10]
        stamp = _STAMP_TITLE.match((r.get("title") or "").strip())
        if stamp:
            return f"{day} {stamp.group('time')} — {r.get('topic') or 'Lecture'}"
        return f"{day} — {r.get('title') or r.get('topic') or 'Lecture'}"
    notes = "\n\n".join(
        f"## {heading(r)}\n\n{r.get('summary') or ''}{extra(r, '###')}{pages(r, '###')}"
        for r in rows)
    transcripts = "\n\n".join(
        _folded(r.get("transcript") or "", heading(r)) for r in rows)
    return head + notes + "\n\n## Transcripts\n\n" + transcripts + "\n"


TIMELINE = "Timeline.base"   # legacy: the block lives in the hub note now,
# but _cleanup_unit_dir still unlinks any left over in an older vault


def _timeline_base(rel):
    """An Obsidian Bases timeline over every lecture note at or under the
    vault-relative folder `rel` — inFolder() matches subfolders, so a class
    timeline picks up all of its units. Bases reads frontmatter only, which is
    why _note_md carries date/summary/lectures.

    One view, and only the properties it shows: inline in a note, Bases renders
    the first view and offers no switcher, so a second view would be dead YAML."""
    return (
        "filters:\n"
        "  and:\n"
        f'    - file.inFolder("{rel}")\n'
        '    - file.hasTag("lecture")\n'
        "properties:\n"
        "  note.date:\n    displayName: Date\n"
        "  note.summary:\n    displayName: Summary\n"
        "  note.seq:\n    displayName: Seq\n"   # not shown, but the cards sort on it
        "views:\n"
        "  - type: cards\n"
        "    name: Timeline\n"
        "    order:\n      - file.name\n      - date\n      - summary\n"
        "    sort:\n      - property: seq\n        direction: ASC\n"
        "      - property: date\n        direction: ASC\n"
    )


def _fence(rel):
    """The timeline as a fenced block, to live inside the hub note rather than
    in a Timeline.base file of its own — one file per class and per unit was
    half the vault."""
    return "```base\n" + _timeline_base(rel) + "```"


# a legacy ![[.../Timeline.base#View]] embed on its own line, or a ```base block
BASE_BLOCK = re.compile(
    r"(?ms)^!\[\[[^\]\n]*Timeline\.base#[^\]\n]*\]\][ \t]*$|^```base\n.*?^```[ \t]*$")


def _sync_base(path, rel):
    """Brings the hub note's timeline block up to date, folding a legacy
    Timeline.base embed into an inline one on the way. Kept separate from the
    hub's first write (which is once-only) because the block is generated: a
    template change has to reach hubs that already exist, the way rewriting
    Timeline.base used to. Anything the user wrote around the block survives."""
    body = path.read_text(encoding="utf-8")
    new, hits = BASE_BLOCK.subn(lambda _m: _fence(rel), body, count=1)
    if not hits:  # hub predates timelines, or the user deleted the block
        new = body.rstrip("\n") + "\n\n" + _fence(rel) + "\n"
    if new != body:
        path.write_text(new, encoding="utf-8")


def ensure_hubs(row):
    """Folder hub notes so the Obsidian graph chains
    lecture -> unit -> class -> semester. Semester hub is title-only; class and
    unit hubs embed that folder's Timeline base and link up the chain — the
    lecture list IS the description, so nothing here goes stale.
    ponytail: hubs are written once and never refreshed — delete a hub file to
    regenerate it."""
    sem, cls = _slug(row.get("semester")), _slug(row.get("class"))
    # _slug("") is "Untitled" -- fine for a note filename, wrong for a folder:
    # an unlabelled unit used to mint a real "Untitled" unit folder + hub.
    unit = _slug(row["unit"]) if (row.get("unit") or "").strip() else ""
    sem_p = OBSIDIAN_VAULT / sem / f"{sem}.md"
    cls_p = OBSIDIAN_VAULT / sem / cls / f"{cls}.md"
    if not sem_p.exists():
        sem_p.parent.mkdir(parents=True, exist_ok=True)
        sem_p.write_text(f"# {sem}\n", encoding="utf-8")
    cls_rel = f"{sem}/{cls}"
    if not cls_p.exists():
        # _ensure_base used to mkdir the class folder on its way to writing
        # Timeline.base in it; nothing else does, so the hub has to
        cls_p.parent.mkdir(parents=True, exist_ok=True)
        cls_p.write_text(
            f"# {cls}\n\n{_fence(cls_rel)}\n\nSemester: [[{sem}/{sem}|{sem}]]\n",
            encoding="utf-8")
    else:
        _sync_base(cls_p, cls_rel)
    if unit:
        unit_rel = f"{sem}/{cls}/{unit}"
        unit_p = OBSIDIAN_VAULT / sem / cls / unit / f"{unit}.md"
        if not unit_p.exists():
            unit_p.parent.mkdir(parents=True, exist_ok=True)
            unit_p.write_text(
                f"# {unit}\n\n{_fence(unit_rel)}\n\nClass: [[{sem}/{cls}/{cls}|{cls}]]\n",
                encoding="utf-8")
        else:
            _sync_base(unit_p, unit_rel)


def _prep_hub(sem, cls):
    """Hub note for a class's Exam Prep folder, so study material is its own
    node in the graph: quiz/cheat sheet -> Exam Prep -> class. One per class,
    never per unit — a prep folder inside every unit sprouted an amber node off
    each unit and the graph stopped reading as the folder tree.
    Written once, like the other hubs. Returns the vault-relative link target."""
    s_sem, s_cls = _slug(sem), _slug(cls)
    hub = OBSIDIAN_VAULT / s_sem / s_cls / PREP_DIR / f"{PREP_DIR}.md"
    if not hub.exists():
        hub.parent.mkdir(parents=True, exist_ok=True)
        hub.write_text(f"# {PREP_DIR}\n\nPractice quizzes and tests, cheat sheets, "
                       f"and flashcard decks.\n\n"
                       f"Class: [[{s_sem}/{s_cls}/{s_cls}|{s_cls}]]\n", encoding="utf-8")
    return f"{s_sem}/{s_cls}/{PREP_DIR}/{PREP_DIR}"


def _prep_hub_link(sem, cls):
    """The `Exam Prep:` line study material carries, creating the hub if needed."""
    return f"\nExam Prep: [[{_prep_hub(sem, cls)}|{PREP_DIR}]]\n"


def _rgb(h, s, v):
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, min(max(s, 0), 1), min(max(v, 0), 1))
    return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)


PREP_COLOR = 0xE0A33C   # amber: study material, distinct from every class hue
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
    # Study material reads as one family vault-wide, so Exam Prep gets a single
    # fixed colour rather than a per-class hue. First in the list: Obsidian takes
    # the first matching group (that's why the unit hub query precedes its
    # folder query below), and the class/topic queries also match these paths.
    # Trailing slash keeps a unit legitimately named "Exam Preparation" out.
    groups = [{"query": f'path:"{PREP_DIR}/"', "color": {"a": 1, "rgb": PREP_COLOR}}]
    sems = sorted(d for d in OBSIDIAN_VAULT.iterdir()
                  if d.is_dir() and not d.name.startswith(".")
                  and d.name != ATTACH_DIR)
    classes = [(sem, c) for sem in sems
               for c in sorted(p for p in sem.iterdir() if p.is_dir())]
    n = max(len(classes), 1)
    for i, (sem, c) in enumerate(classes):
        hue = i / n
        fc = drop(st["class_drop_min"], st["class_drop_max"], hue)  # class S/V, faded off full
        # Exam Prep is study material, not a unit: it has no hub note, and
        # counting it shifts the hue spread of every real unit in the class
        units = sorted(p for p in c.iterdir() if p.is_dir() and p.name != PREP_DIR)
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


@app.get("/vault_tree")
def vault_tree():
    """Semesters and classes that exist as vault folders, recordings or not.
    A semester is scaffolded before anything is filed into it, and the UI
    builds its tree from recording rows alone -- so a fresh term stayed
    invisible in the app until its first recording landed."""
    out = {}
    if not OBSIDIAN_VAULT.is_dir():
        return out
    for sem in sorted(d for d in OBSIDIAN_VAULT.iterdir()
                      if d.is_dir() and not d.name.startswith(".")
                      and d.name != ATTACH_DIR):
        out[sem.name] = sorted(p.name for p in sem.iterdir()
                               if p.is_dir() and p.name != PREP_DIR)
    return out


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


def _group_rows(sem, cls, unit, topic):
    """Recordings sharing (semester, class, unit, topic) with a non-empty
    transcript, oldest first — the set of recordings one combined note holds."""
    rows = (sb.table("recordings").select("*").eq("semester", sem)
            .eq("class", cls).eq("unit", unit).eq("topic", topic)
            .order("created_at").execute().data)
    return [r for r in rows if (r.get("transcript") or "").strip()]


def _refile_group(sem, cls, unit, topic):
    """Builds/rewrites the one combined note file for a (semester, class,
    unit, topic) label tuple from whatever recordings currently carry it
    (queried fresh from the DB), syncing each row's obsidian_path. Removes
    the file instead if the group is now empty. Returns the path (str), or
    None."""
    # the unit hub owns <unit>/<unit>.md -- a topic named after its own unit
    # used to overwrite the hub, leaving a note whose "Unit:" link pointed at itself
    fname = _slug(topic) + (" (lecture)" if _slug(topic) == _slug(unit) else "")
    dest = OBSIDIAN_VAULT / _slug(sem) / _slug(cls) / _slug(unit) / f"{fname}.md"
    group = _group_rows(sem, cls, unit, topic)
    if not group:
        if dest.exists() and dest.is_relative_to(OBSIDIAN_VAULT):
            dest.unlink()
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_note_md(group), encoding="utf-8")
    for r in group:
        old = r.get("obsidian_path")
        if old == str(dest):
            continue
        _set(r["id"], obsidian_path=str(dest))
        # drop r's old file (e.g. a legacy rid-suffixed note) — but never a
        # combined file another row still points at (mid-merge sharing)
        if old:
            old_p = pathlib.Path(old)
            if (old_p != dest and old_p.is_relative_to(OBSIDIAN_VAULT) and old_p.exists()
                    and not (sb.table("recordings").select("id")
                             .eq("obsidian_path", old).neq("id", r["id"]).execute().data)):
                old_p.unlink()
    return str(dest)


def write_note(row):
    """Writes/rewrites the ONE combined note for row's (semester, class,
    unit, topic) — every recording sharing that topic lands in the same file
    (see _refile_group/_note_md); no more per-recording rid-suffixed files.
    Trusts the DB: callers must _set row's new labels before calling this.
    If row just moved off a different topic whose note is still shared with
    other recordings, that note is rewritten from what's left (or removed if
    row was the last one there). Returns the new path (str), or None if row
    has no transcript."""
    if not (row.get("transcript") or "").strip():
        return None
    old = row.get("obsidian_path")  # read before _refile_group can touch this row's own field
    dest = _refile_group(row.get("semester"), row.get("class"),
                          row.get("unit"), row.get("topic"))

    if old and old != dest:
        others = (sb.table("recordings").select("semester,class,unit,topic")
                  .eq("obsidian_path", old).neq("id", row["id"]).execute().data)
        if others:
            o = others[0]
            _refile_group(o.get("semester"), o.get("class"), o.get("unit"), o.get("topic"))
        else:
            old_p = pathlib.Path(old)
            if old_p.is_relative_to(OBSIDIAN_VAULT) and old_p.exists():
                old_p.unlink()

    try:
        ensure_hubs(row)
        write_graph_config()
    except Exception as e:
        print(f"[listen] hub notes/graph config failed (note itself is written): {e}")
    return dest


def _rows_semester(rows):
    """Semester to file under when the caller didn't pick one — every scope
    selector defaults to 'All semesters', and an empty semester would slug to
    'Untitled' and strand the note at the vault root, off the graph."""
    return next((r.get("semester") for r in rows if r.get("semester")), "")


def save_attachment(rid, filename, data):
    """Keep the uploaded page itself in the vault, not just Claude's reading of
    it. A misread diagram is then correctable against the original instead of
    being the only surviving record. Returns the stored filename, or None when
    the type is one Obsidian won't embed.

    Named "<original stem>-<rid8>-<n>.<ext>" so the folder stays skimmable by
    hand while _attachments can still find one recording's pages."""
    ext = pathlib.Path(filename or "").suffix.lower()
    if ext not in ATTACH_EXTS:
        return None
    d = OBSIDIAN_VAULT / ATTACH_DIR
    d.mkdir(parents=True, exist_ok=True)
    stem = _slug(pathlib.Path(filename).stem)  # returns "Untitled" if empty
    i = 1
    while (d / f"{stem}-{rid[:8]}-{i}{ext}").exists():
        i += 1
    dest = d / f"{stem}-{rid[:8]}-{i}{ext}"
    dest.write_bytes(data)
    return dest.name


def _attachments(rid):
    """Stored page filenames for one recording, oldest first. Read off disk
    rather than a DB column: _refile_group rewrites notes wholesale, so the
    embed list is regenerated on every write anyway, and derived-from-disk
    cannot drift from what the vault actually holds."""
    d = OBSIDIAN_VAULT / ATTACH_DIR
    if not rid or not d.is_dir():
        return []
    # sort by length first: a plain sort puts "-10" before "-2"
    return sorted((f.name for f in d.glob(f"*-{rid[:8]}-*") if f.is_file()),
                  key=lambda s: (len(s), s))


def drop_attachments(rid):
    """Unlink one recording's stored pages. Called from delete_recording --
    nothing else references them, so they would sit in the vault forever."""
    d = OBSIDIAN_VAULT / ATTACH_DIR
    for name in _attachments(rid):
        (d / name).unlink(missing_ok=True)


def write_prep_note(sem, cls, filename, body):
    """Writes a study artifact into the class's Exam Prep folder --
    <vault>/<sem>/<cls>/Exam Prep/<filename>. Shared by the cheat-sheet and
    flashcard exports; quizzes/exams build their own filenames but the same
    folder. Unit scope lives in the filename, not the path. Returns the path."""
    body += _prep_hub_link(sem, cls)
    path = OBSIDIAN_VAULT / _slug(sem) / _slug(cls) / PREP_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return str(path)


def write_cards_note(sem, cls, unit):
    """Readable snapshot of a scope's flashcard deck in its Exam Prep folder.
    The DB stays the source of truth for SM-2 scheduling — this is rewritten
    whole on each generation so a second batch doesn't split across notes."""
    q = sb.table("cards").select("front,back").eq("class", cls)
    if sem:
        q = q.eq("semester", sem)
    if unit:
        q = q.eq("unit", unit)
    cards = q.execute().data or []
    body = (f"---\nclass: {cls}\ntags: [flashcards]\n---\n\n"
            f"# Flashcards — {unit or cls}\n\n"
            + "".join(f"- **{c.get('front', '')}** — {c.get('back', '')}\n" for c in cards)
            + f"\nClass: [[{_slug(sem)}/{_slug(cls)}/{_slug(cls)}|{_slug(cls)}]]\n")
    # unit in the filename, not the folder: one Exam Prep per class, so a
    # per-unit deck would otherwise overwrite the class-wide one
    fname = _slug(f"{cls} {unit}".strip()) + " flashcards.md"
    return write_prep_note(sem, cls, fname, body)


def _migrate_prep_dirs():
    """One-time rename of the old 'Practice' and 'Exams' folders to PREP_DIR so
    old and new study notes don't sit in separate folders. _flatten_prep_dirs
    then lifts any that landed inside a unit up to the class.
    ponytail: runs on every import — a no-op once neither name is left under
    the vault."""
    if not OBSIDIAN_VAULT.is_dir():
        return
    for old in ("Practice", "Exams"):
        for p in list(OBSIDIAN_VAULT.rglob(old)):
            if not p.is_dir():
                continue
            dest = p.with_name(PREP_DIR)
            try:
                if dest.exists():
                    for f in p.iterdir():
                        f.replace(dest / f.name)  # same filename = same note, overwrite
                    p.rmdir()
                else:
                    p.rename(dest)
            except OSError as e:
                print(f"[listen] {old} -> {PREP_DIR} migration skipped for {p}: {e}")


def _flatten_prep_dirs():
    """Lifts every unit-level Exam Prep folder into its class's one, then
    removes the emptied folder. Study material used to file per unit, which hung
    an amber Exam Prep node off every single unit; one node per class keeps the
    graph reading as the folder tree it mirrors.
    ponytail: runs on every import — a no-op once no unit holds a prep folder."""
    if not OBSIDIAN_VAULT.is_dir():
        return
    for prep in list(OBSIDIAN_VAULT.glob(f"*/*/*/{PREP_DIR}")):
        sem, cls, unit = prep.relative_to(OBSIDIAN_VAULT).parts[:3]
        dest = OBSIDIAN_VAULT / sem / cls / PREP_DIR
        dest.mkdir(parents=True, exist_ok=True)
        for f in list(prep.iterdir()):
            try:
                if f.name == f"{PREP_DIR}.md":
                    f.unlink()  # the unit's own hub note; the class one replaces it
                    continue
                target = dest / f.name
                if target.exists():  # same filename from two units — keep both
                    target = dest / f"{f.stem} ({unit}){f.suffix}"
                f.replace(target)
            except OSError as e:
                print(f"[listen] prep flatten skipped for {f}: {e}")
        try:
            prep.rmdir()
        except OSError:
            pass  # user files left behind — leave the folder alone


# "- [[Fall 26/Bio/Cells/Cells|Cells]]" -> "Cells";  "- Vectors" -> "Vectors"
_COVERS_BULLET = re.compile(r"^- (?:\[\[[^|\]]+\|)?([^\]]+?)\]{0,2}$", re.M)


def _covers_bullets(topic_names):
    """Covers list for an exam note: plain bullets. These used to wikilink the
    unit each entry resolved to, but a wide exam then fanned an edge out to
    every unit it spanned and the graph stopped reading as the folder tree."""
    return "\n".join(f"- {t}" for t in topic_names)


def _prune_exam_links():
    """Strips the cross-links that used to fan arrows across the graph: the
    `Exam:` line study material carried (one link per exam covering its scope)
    and the unit wikilinks inside an exam's `## Covers` bullets. What's left is
    the containment chain — material -> Exam Prep -> class -> semester — so the
    graph reads as the folder tree. Also repoints hub links a note carried from
    a unit-level prep folder (see _flatten_prep_dirs) and creates any missing
    Exam Prep hub note, the one link the material keeps. ponytail: rewrites in
    place on import, a no-op once every note under a prep folder is plain."""
    if not OBSIDIAN_VAULT.is_dir():
        return
    for prep in OBSIDIAN_VAULT.glob(f"*/*/{PREP_DIR}"):
        sem, cls = prep.relative_to(OBSIDIAN_VAULT).parts[:2]
        hub_line = _prep_hub_link(sem, cls)  # also creates the class's prep node
        for note in prep.glob("*.md"):
            if note.name == f"{PREP_DIR}.md":
                continue  # the hub itself
            try:
                text = note.read_text(encoding="utf-8")
                # the hub link is "Exam Prep: ", which this pattern doesn't touch
                out = re.sub(r"\nExam: [^\n]*\n", "", text)
                # lambda, not a replacement string: the link contains no escapes
                # but re would try to expand any that appear in a class name
                out = re.sub(r"\nExam Prep: \[\[[^\]]*\]\]\n", lambda _: hub_line, out)
                head, sep, covers = out.partition("## Covers")
                if sep:  # unlink the bullets, keeping the exam's own wording
                    out = head + sep + _COVERS_BULLET.sub(r"- \1", covers)
                if out != text:
                    note.write_text(out, encoding="utf-8")
            except OSError as e:
                print(f"[listen] exam-link prune skipped for {note}: {e}")


def write_exam_note(sem, cls, exam):
    """Files a detected/announced exam or quiz under the class's Exam Prep
    folder: <vault>/<sem>/<cls>/Exam Prep/<title>.md. Same title -> same slug
    -> overwrites in place, so re-detecting an exam across lectures/a revised
    syllabus is idempotent. Covers entries stay plain bullets.
    Returns the path (str)."""
    s_sem, s_cls = _slug(sem), _slug(cls)
    # str()-wrap every model-supplied field — same defensiveness as
    # save_assignments' due_date parsing — so malformed JSON from Claude can't
    # crash finalize()/process_pdf() after the row's already marked done.
    title = str(exam.get("title") or "").strip() or "Exam"
    kind = str(exam.get("kind") or "exam")
    due = str(exam.get("due_date") or "").strip()
    fmt = str(exam.get("format") or "").strip()
    topics = exam.get("topics")
    topics = topics if isinstance(topics, list) else []

    cls_dir = OBSIDIAN_VAULT / s_sem / s_cls
    topic_names = [t for t in (str(x).strip() for x in topics) if t]

    fm = {"class": cls, "kind": kind, "date": due or "TBA", "tags": "[exam]"}
    front = "\n".join(f"{k}: {v}" for k, v in fm.items())
    # Class wikilink keeps the exam attached to its class hub in the graph —
    # without it an exam with no matching Covers links free-floats.
    body = (f"---\n{front}\n---\n\n# {title}\n\n**Date:** {due or 'TBA'}\n"
            f"\nClass: [[{s_sem}/{s_cls}/{s_cls}|{s_cls}]]\n")
    if fmt:
        body += f"\n**Format:** {fmt}\n"
    if topic_names:
        body += f"\n## Covers\n\n{_covers_bullets(topic_names)}\n"
    fname = f"{_slug(title)}.md"
    path = cls_dir / PREP_DIR / fname
    body += _prep_hub_link(sem, cls)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return str(path)


def write_quiz_note(quiz, questions, results, score):
    """Files a practice quiz/test under the class's Exam Prep folder,
    <vault>/<sem>/<cls>/Exam Prep/<kind> <date> <time>.md. Called at
    generation (score=None -> blank, ungraded) and again at grade time; the
    filename is derived from the quiz's created_at so both write the same path
    and grading overwrites the blank. Mirrors write_exam_note's structure/graph
    anchor. Returns the path (str)."""
    s_sem, s_cls = _slug(quiz.get("semester")), _slug(quiz.get("class"))
    kind = str(quiz.get("kind") or "quiz")
    ca = quiz.get("created_at")
    try:
        now = datetime.datetime.fromisoformat(ca) if ca else datetime.datetime.now()
    except ValueError:
        now = datetime.datetime.now()
    score_label = "ungraded" if score is None else f"{score}%"

    fm = {"class": quiz.get("class") or "", "kind": kind, "score": score_label,
          "date": now.strftime("%Y-%m-%d"), "tags": "[practice]"}
    front = "\n".join(f"{k}: {v}" for k, v in fm.items())
    body = f"---\n{front}\n---\n\n# {kind.title()} — {score_label}\n"

    for i, q in enumerate(questions):
        r = results[i] if i < len(results) else {}
        body += f"\n### Q{i + 1}\n\n{q.get('q', '')}\n\n"
        if q.get("type") == "mcq":
            choices = q.get("choices") or []
            correct, picked = q.get("answer"), r.get("answer")
            for ci, c in enumerate(choices):
                tags = [t for t, ok in (("correct", ci == correct), ("your pick", ci == picked)) if ok]
                body += f"- {c}" + (f" ({', '.join(tags)})" if tags else "") + "\n"
        elif q.get("type") == "frq":
            body += f"**Rubric:** {q.get('rubric', '')}\n\n**Transcribed answer:** {r.get('answer', '')}\n"
        else:  # short — legacy quizzes
            body += f"**Model answer:** {q.get('answer', '')}\n\n**Your answer:** {r.get('answer', '')}\n"
        if "score" in r:
            body += f"\nScore: {r['score']} — {r.get('feedback', '')}\n"
        elif "correct" in r:
            body += f"\n{'Correct' if r.get('correct') else 'Incorrect'}\n"

    body += f"\nClass: [[{s_sem}/{s_cls}/{s_cls}|{s_cls}]]\n"
    body += _prep_hub_link(quiz.get("semester"), quiz.get("class"))

    path = (OBSIDIAN_VAULT / s_sem / s_cls / PREP_DIR
            / f"{kind} {now:%Y-%m-%d %H%M}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return str(path)


_migrate_prep_dirs()   # import-time, idempotent — see the function docstring
_flatten_prep_dirs()   # ditto — lifts unit-level prep folders up to the class
_prune_exam_links()    # ditto — strips old exam cross-links, keeps the hub chain
