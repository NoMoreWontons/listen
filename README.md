# listen

Personal lecture/meeting transcriber. Record in the browser → transcribe locally
with Whisper → store transcript + summary in Supabase. See `CONTEXT.md` and
`docs/adr/0001-*.md` for the design.

## Setup

1. **Install Python 3.11+** (https://python.org — not the Windows Store stub).
2. `pip install -r requirements.txt`
3. Create the table: paste `schema.sql` into the Supabase SQL editor and run it.
4. `cp .env.example .env` and fill in:
   - `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (Project settings → API → service_role key)
   - `ANTHROPIC_API_KEY`
5. `uvicorn app:app` then open http://127.0.0.1:8000

## Notes

- **GPU:** uses CUDA if available, else falls back to CPU automatically. For GPU on
  the RTX 5070 Ti you need CUDA 12 + cuDNN libs; without them it runs on CPU (slower).
- **Audio** is local-only under `audio/`, auto-deleted after `RETENTION_DAYS` (swept
  on launch). Transcripts live in Supabase forever.
- **Check:** `python test_sweep.py` verifies the retention sweep.

## Not built yet (deferred)

Auth / multi-user, online-call (system audio) capture, speaker labels, timestamps
in the transcript, search.
