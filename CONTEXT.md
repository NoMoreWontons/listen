# Context: listen

A personal lecture/meeting transcriber. Records or ingests audio, transcribes
it locally with Whisper, and keeps searchable transcripts. Built primarily as a
learning project (Whisper + storage + LLM summaries).

## Glossary

### Recording
A single captured lecture or meeting — one audio input and everything derived
from it. The unit a user thinks in ("my 9am lecture"). Moves through a
lifecycle: **recording → transcribing → done** (see Status).

### Status
Where a Recording is in its lifecycle. `recording` (audio still being
captured), `transcribing` (audio captured, Whisper running), `done`
(transcript ready). This is what lets the user close the app mid-transcription
and find the finished transcript later.

### Audio file
The raw captured sound for a Recording. **Ephemeral**: kept only for a
retention window, then auto-deleted. Not the product — just the source material
the Transcript is derived from.

### Transcript
The text Whisper produces from a Recording's Audio file, with timestamps.
**Permanent**: the thing the user actually keeps and reads. Cheap to store
forever.

### Retention window
How long an Audio file is kept before auto-deletion (default 7 days). Applies to
audio only; the Transcript is never auto-deleted. Lets the user re-listen or
verify a quote shortly after, without paying storage for old audio forever.

### Summary
An LLM-generated condensation of a Transcript (key points / action items).
Derived, regenerable, not authoritative.

## Scope (current)
- **Solo** user for now; "friends" sharing is a possible later phase, not built.
- **Batch** transcription (record then process), not live/real-time.
- **In-person only**: mic captures the room. Online-call audio (Zoom/Meet
  remote voices) is out of scope until system/tab capture is added.
- Transcription runs **locally** (faster-whisper on the user's GPU), ~$0.
- Summaries run via Claude Haiku 4.5, ~$0.01 per Recording.
