import os
import time
import threading
import datetime
import pathlib

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

sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
claude = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY


def audio_path(rid):
    return AUDIO_DIR / f"{rid}.webm"


# --- Whisper: load the model once, lazily, GPU with CPU fallback ---
_model = None
_model_lock = threading.Lock()
# ponytail: CTranslate2 isn't safe for concurrent transcribe() calls on one
# model instance — reuse this lock to serialize them too, not just loading.
_transcribe_lock = threading.Lock()


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            try:
                _model = WhisperModel(WHISPER_MODEL, device="cuda", compute_type="float16")
            except Exception:
                # ponytail: CPU fallback when CUDA libs aren't present; slower but works
                _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        return _model


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


@asynccontextmanager
async def lifespan(app):
    sweep_old_audio()  # ponytail: sweep on launch, not a cron — the tool is launched to be used
    resume_stuck()
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
        .select("id,title,created_at,status,transcript,summary")
        .order("created_at", desc=True)
        .execute()
        .data
    )


def process(rid):
    try:
        with _transcribe_lock:
            segments, _ = get_model().transcribe(str(audio_path(rid)))
        transcript = "".join(s.text for s in segments).strip()
        summary = summarize(transcript)
        sb.table("recordings").update(
            {"status": "done", "transcript": transcript, "summary": summary}
        ).eq("id", rid).execute()
    except Exception as e:
        sb.table("recordings").update(
            {"status": "error", "summary": f"[error: {e}]"}
        ).eq("id", rid).execute()


def summarize(transcript):
    if not transcript:
        return ""
    msg = claude.messages.create(
        model="claude-haiku-4-5",
        max_tokens=600,
        messages=[
            {
                "role": "user",
                "content": "Summarize this lecture/meeting transcript as concise "
                "key points and any action items:\n\n" + transcript,
            }
        ],
    )
    return msg.content[0].text
