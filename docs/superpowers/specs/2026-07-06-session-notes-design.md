# Session notes integrated into AI summary

**Date:** 2026-07-06
**Status:** approved

## Goal

Let the user attach their own notes to any recording — typed in the listen UI or
written on the source Notion page — and have Claude integrate those notes into
the AI summary stored in Supabase and filed to Obsidian. Notes are input to
summarization, not a separate display section.

## Data

- New `notes` text column on `recordings` (nullable). Raw user notes always
  preserved here, whatever the summary says.
- `schema.sql` updated; migration applied to Supabase.

## Server (`app.py`)

- `analyze(transcript, notes="")`: when notes are non-empty, the prompt includes
  them with the instruction to weave them into the summary and emphasize what
  the user flagged. Returns the same tuple as today.
- `finalize` reads `notes` from the row (alongside the existing
  semester/class/unit/topic pre-read) and passes them to `analyze`, so notes
  typed before transcription finishes are integrated automatically.
- `/label/{rid}` gains a `notes` param. Saving notes on a `done` row re-runs
  `analyze` (one Claude call, ~$0.01) and updates `summary` + token counts;
  user label corrections are untouched (labels keep their existing
  user-wins merge). The Obsidian note is re-written as today.
- `/recordings` select includes `notes`.

## Notion import

- Split the page: the subtree under the block titled "Transcript"
  (case-insensitive, has children) is the transcript; all other page text is
  notes. No such block → whole page is transcript, notes empty (current
  behavior preserved).
- Extracted notes stored in `notes` and passed to `analyze` at import.

## UI (`index.html`)

- Notes `<textarea>` in the existing labels box on each session card; the
  existing "Save labels / Save to Obsidian" button submits it.
- The 5s refresh already skips re-render while an input inside the list is
  focused, so typing is safe.

## Obsidian note

- Unchanged structure: `## Summary` now contains the integrated summary.
  No separate notes section.

## Error handling

- Re-summarize failure on notes save: surface as the existing error pattern
  (`status="error"` is wrong for a done row — instead return the error in the
  response and leave the row untouched). Notes themselves are saved first, so
  a failed Claude call never loses user input.

## Testing

- Extend `test_note.py` / add small checks: notes flow through `finalize` into
  `analyze` args; Notion splitter separates transcript toggle from notes;
  `/label` with notes on a done row triggers re-summarize (Claude mocked).
