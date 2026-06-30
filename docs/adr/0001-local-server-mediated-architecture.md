# 1. Local-server-mediated architecture

Date: 2026-06-30

## Status

Accepted

## Context

`listen` records lectures/meetings in the browser, transcribes them locally
with Whisper, stores transcripts in Supabase, and summarises them with Claude.
Three forces pull on the architecture:

1. **Whisper can't run in the browser.** Transcription is Python
   (faster-whisper on the GPU). The browser captures audio but cannot transcribe.
2. **Supabase credentials must not ship to the browser.** The simple, safe way
   to talk to Supabase is the service key — which must stay server-side. Using
   it from the browser would require the anon key plus correct RLS policies,
   which is more setup than a solo localhost tool warrants.
3. **Recordings are long (1–3 h) and jobs are slow (minutes).** Capture must
   survive a tab crash, and transcription must survive a dropped connection.

Alternative considered: the browser talks to Supabase directly via the JS
client (anon key + RLS) and only sends audio to a local process for Whisper.
This is closer to the eventual multi-user design but front-loads RLS/auth setup
the solo tool doesn't need.

## Decision

A single **local FastAPI app** mediates everything. The browser talks **only to
`localhost`**; FastAPI is the only thing that touches Supabase (via
`supabase-py` with the service key).

- **Capture:** `MediaRecorder.start(timeslice)` emits ~10 s chunks; the browser
  POSTs each chunk and FastAPI appends to a file on disk. A crash loses only the
  last chunk, and the audio is complete on disk when recording stops.
- **Transcription as a background job, tracked by row status.** On stop, FastAPI
  inserts the Supabase row with `status='transcribing'` and runs Whisper in the
  background. The browser polls the recordings list it already fetches; the row
  flips to `done` with the transcript. The recordings table *is* the job
  tracker — no separate job/poll endpoint.
- **Summary:** after transcription, one Claude Haiku 4.5 call adds key points;
  written to the same row.
- **Audio is local-only** with a default 7-day retention (auto-deleted by a
  daily/on-launch sweep). Only the transcript + summary live in Supabase.

## Consequences

**Good**
- Supabase service key never leaves the machine; no browser auth/RLS needed yet.
- One language and one process server-side; fewest moving parts.
- Crash-resilient capture and disconnect-resilient transcription fall out of the
  chunked-upload + status-column design rather than extra machinery.
- Reusing the list read path as the job tracker avoids a whole endpoint.

**Costs / deferred**
- Not multi-user. Adding "friends" later means moving DB reads to the browser
  with the Supabase JS client + anon key + RLS, and adding auth — a real
  refactor, consciously deferred (see [[wonton-security-deferred]] for the
  pattern of deferring hardening to launch).
- Requires the user's machine to be running to transcribe; no cloud fallback.
- Online-meeting audio (remote voices) is out of scope — mic captures the room
  only.
