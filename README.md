# listen

Personal lecture/meeting transcriber. Record in the browser → transcribe locally
with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) → store transcript +
summary (via Claude) in Supabase.

Everything runs on `127.0.0.1`. The browser never touches Supabase or any API key —
the local FastAPI server holds the service key and does all the work. See
`docs/adr/0001-*.md` for why.

## How it works

1. Browser records audio and streams it in chunks to the local server.
2. On stop, the server transcribes the audio with Whisper (GPU if available, else CPU).
3. Claude summarises the transcript into key points + action items.
4. Transcript and summary land in Supabase; the raw audio stays local and is
   auto-deleted after `RETENTION_DAYS`.

## Setup

1. **Install Python 3.11+** (https://python.org — not the Windows Store stub).
2. `pip install -r requirements.txt`
3. Paste `schema.sql` into the Supabase SQL editor and run it (creates the
   `recordings` table).
4. `cp .env.example .env` and fill in your keys (see `.env.example` for each).
5. Run it:
   - `uvicorn app:app` (or double-click `start_server.bat` on Windows)
   - open http://127.0.0.1:8000

## Config (`.env`)

| Var | Required | Notes |
|-----|----------|-------|
| `SUPABASE_URL` | yes | Project settings → API |
| `SUPABASE_SERVICE_KEY` | yes | service_role key — server-side only, never ship to the browser |
| `ANTHROPIC_API_KEY` | yes | console.anthropic.com |
| `WHISPER_MODEL` | no | default `large-v3-turbo` |
| `RETENTION_DAYS` | no | default `7` — how long local audio is kept |

## Notes

- **GPU:** uses CUDA if available, else falls back to CPU automatically. For GPU you
  need CUDA 12 + cuDNN libs installed; without them it runs on CPU (slower).
- **Audio** is local-only under `audio/`, auto-deleted after `RETENTION_DAYS` (swept
  on launch). Transcripts live in Supabase.
- **Check:** `python test_sweep.py` verifies the retention sweep.

## Not built yet (deferred)

Auth / multi-user, online-call (system audio) capture, speaker labels, timestamps
in the transcript, search.
