# Multi-topic split — plan

One lecture recording can cover several distinct topics (e.g. calc: "derivatives of inverse functions" then "applications of derivatives"). Today `analyze()` forces one class/unit/topic per recording. Feature: detect topic segments, ask the user before splitting, then file one note per topic.

Approved design (user): split on any distinct topic; **ask before splitting**; one DB row + one Obsidian note per approved segment.

## Contract (both tasks depend on this — do not change it)

- `recordings.pending_segments` (jsonb, nullable): array of `{"class": str, "unit": str, "topic": str, "summary": str}`. Set only while `status = 'split_pending'`.
- New status value: `split_pending` (row transcribed + analyzed, waiting on user).
- `POST /split/{rid}` with JSON body `{"approve": true|false}`:
  - `approve: true` — original row becomes segment 1 (update in place; it keeps the audio file), each further segment inserted as a NEW row: same `created_at`/`semester`/`source`, full shared `transcript`, its own `class/unit/topic/summary`, `status='done'`. Write an Obsidian note per row (`write_note`). Clear `pending_segments`.
  - `approve: false` — file as ONE note, today's shape: labels from first segment, summary = all segment summaries concatenated with `## <topic>` headers. Clear `pending_segments`.
  - Response: `{"ok": true, "rows": [ids...]}` or `{"error": ...}`.

## Task 1 — backend (`app.py`, `schema.sql`)

1. `analyze(transcript, notes="")` — change the prompt to return
   `{"segments": [{class, unit, topic, summary}, ...]}`. Instruct: a normal lecture is ONE segment; emit multiple only for clearly distinct topics, each substantial enough for its own note (no 2-minute fragments). Keep the existing summary formatting rules (key points bullets, Questions & answers section) PER segment. New return shape: `(segments, tokens_in, tokens_out)` where segments is a non-empty list of dicts. Keep the fallback path (unparseable JSON) returning one segment with raw text as summary, blank labels — mirror current fallback behavior.
2. `finalize(rid, transcript, created_at=None)` — sole caller of `analyze`. If 1 segment: identical behavior to today (user pre-labels win via `keep`, write note). If 2+ segments: `_set(rid, status="split_pending", stage=None, progress=None, transcript=..., tokens_in=..., tokens_out=..., semester=keep(...), pending_segments=json list, source=...)`; do NOT write a note yet. User pre-labels (`pre` class/unit/topic): if the user set a class, apply it to every segment.
3. `POST /split/{rid}` endpoint per contract above. Guard: 404-style `{"error": "not pending"}` if row isn't `split_pending`.
4. `schema.sql`: append `alter table recordings add column if not exists pending_segments jsonb;`
5. `process_pdf` / other paths: untouched (they don't call `analyze`).
6. Check: extend or add `test_split.py` — pure tests, no network: (a) analyze JSON parsing for 1-segment and 2-segment replies (factor the parse into a small pure helper if needed), (b) the keep-as-one summary concatenation. Follow existing `test_*.py` style (plain asserts, run with project venv `.venv/Scripts/python.exe -m pytest -q test_split.py` or plain script style matching siblings).

## Task 2 — frontend (`index.html`)

1. Recording list: rows with `status === 'split_pending'` render a proposal card instead of the normal label editor: list each segment (`<b>topic</b> — class / unit` + first ~120 chars of summary), then two buttons: `Split into N notes` and `Keep as one`, calling `POST /split/${id}` with `{approve: true|false}`. After click: disable button, `document.activeElement?.blur()` (the 5s refresh guard skips re-render while focus is inside `#list` — same pattern as existing handlers), then let the next refresh pick up the result.
2. Status badge: `split_pending` should be visibly distinct (reuse existing `.status` styling with a `data-s` rule; amber-ish).
3. Keep it inside the row rendering that `refresh()` rewrites — the card is derived from row state, so refresh redrawing it is correct.
4. Check: `node --check` on the extracted inline script (pattern used before: extract `<script>` body to temp file, `node --check`).

## Out of scope

- No re-analysis of existing recordings.
- No transcript slicing — child rows share the full transcript; per-topic summaries are what quiz/flashcard/cheat-sheet read, so scoping stays correct.
- No timestamps/audio splitting.
