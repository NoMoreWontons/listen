# listen

Personal lecture/meeting transcriber. Record in the browser → transcribe locally
with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) → summarise +
auto-label with Claude → file an [Obsidian](https://obsidian.md) note and store the
row in Supabase.

Everything runs on `127.0.0.1`. The browser never touches Supabase or any API key —
the local FastAPI server holds the service key and does all the work. See
`docs/adr/0001-*.md` for why.

## How it works

1. Browser records audio and streams it in chunks to the local server.
2. On stop, the server transcribes the audio with Whisper (GPU if available, else CPU).
3. Claude summarises the transcript and infers **semester / class / unit / topic**.
4. The row lands in Supabase and a Markdown note is written to your Obsidian vault at
   `<vault>/<semester>/<class>/<unit>/<topic>.md`.
5. You can set semester/class/unit/topic in the web UI **during recording or after**
   — hand-set labels win; Claude only fills the ones you leave blank. Saving after
   `done` re-files the note.
6. Raw audio stays local and is auto-deleted after `RETENTION_DAYS`.

**Notion fallback (optional):** if `NOTION_TOKEN` is set, the server polls one Notion
database for AI meeting-note pages, pulls the transcript, runs the same
summarise + label + file pipeline, and tags those rows `source = notion`.

Supabase is a write-only sync backend (share notes across devices); Obsidian is for
everyday use. Editing a note never writes back to Supabase.

## Setup (user)

1. **Install Python 3.11+** (https://python.org — not the Windows Store stub).
2. **Install [Obsidian](https://obsidian.md)** and open/create a vault (default folder
   name `College Lectures` in your home directory, or point `OBSIDIAN_VAULT` anywhere).
3. Install deps:
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt      # Windows
   # .venv/bin/pip install -r requirements.txt        # macOS/Linux
   ```
4. Paste `schema.sql` into the Supabase SQL editor and run it (creates the
   `recordings` table + indexes).
5. `cp .env.example .env` and fill in your keys (see the table below).
6. Run it:
   - `.venv\Scripts\python -m uvicorn app:app`  (or double-click `start_server.bat` on Windows)
   - open http://127.0.0.1:8000

### Setup (for Claude Code)

Agent-runnable install — assumes `.env` already holds real keys:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # Windows path; use .venv/bin on POSIX
# Apply schema.sql to Supabase (SQL editor, or the Supabase MCP apply_migration).
python test_sweep.py && python test_note.py && python test_notion.py && python test_model_stall.py
.venv/Scripts/python -m uvicorn app:app              # serves http://127.0.0.1:8000
```

First launch downloads the Whisper model named by `WHISPER_MODEL`. `large-v3-turbo`
is a multi-GB download; set `WHISPER_MODEL=tiny` for a fast first run.

## Config (`.env`)

| Var | Required | Notes |
|-----|----------|-------|
| `SUPABASE_URL` | yes | Project settings → API |
| `SUPABASE_SERVICE_KEY` | yes | service_role key — server-side only, never ship to the browser |
| `ANTHROPIC_API_KEY` | yes | console.anthropic.com |
| `WHISPER_MODEL` | no | default `large-v3-turbo`; `tiny` for a quick start |
| `RETENTION_DAYS` | no | default `7` — how long local audio is kept |
| `OBSIDIAN_VAULT` | no | default `~/College Lectures`; folder notes are written to |
| `SEMESTER_OVERRIDE` | no | blank = auto from date; set e.g. `Bridge` to force the semester label on new recordings |
| `NOTION_TOKEN` | no | enables the Notion fallback; empty = feature off |
| `NOTION_DB_ID` | no | the Notion database polled for AI meeting notes |
| `NOTION_POLL_MIN` | no | default `10` — minutes between Notion polls |

**Notion setup:** create an internal integration at
https://www.notion.so/my-integrations, copy its secret into `NOTION_TOKEN`, then share
the target database with that integration (page `···` → Connections). Put the database
id in `NOTION_DB_ID`.

## Notes

- **GPU:** uses CUDA if available, else falls back to CPU automatically. For GPU you
  need CUDA 12 + cuDNN libs installed; without them it runs on CPU (slower).
- **Audio** is local-only under `audio/`, auto-deleted after `RETENTION_DAYS` (swept
  on launch). Transcripts live in Supabase + Obsidian.
- **Checks:** `python test_sweep.py` (retention), `python test_note.py` (Obsidian
  filing), `python test_notion.py` (Notion block flatten), `python test_model_stall.py`
  (model warm-up).

## Not built yet (deferred)

Auth / multi-user, online-call (system audio) capture, speaker labels, timestamps
in the transcript, search, delete-propagation (deleting a Supabase row / note does not
cascade).
