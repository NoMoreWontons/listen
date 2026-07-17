# Tree multi-select + right-click study material — design

Date: 2026-07-17. Approved approach: **B** (new dispatch endpoint).

## What

In the folder tree (`#tree`, index.html): multi-select units/topics within ONE
class, right-click for a context menu that generates study material from the
selection (quiz / test / flashcards / cheat sheet), plus Rename and Delete when
exactly one item is selected.

## Backend — `POST /study/generate` (app.py)

```json
{
  "kind": "quiz" | "test" | "flashcards" | "cheatsheet",
  "semester": "Fall 26",        // optional filter, same as /quiz/generate
  "class": "Biology",           // required
  "scopes": [                   // required, non-empty
    {"unit": "Cells"},                      // whole unit
    {"unit": "Cells", "topic": "Mitosis"}   // one topic
  ]
}
```

Row gathering: one query — recordings `status=done`, `class` (+`semester` if
given), select `topic,title,summary,unit`. Filter in Python: row matches any
scope (`unit` equal AND (`topic` absent from scope OR equal)). No summaries →
`{"error": "no filed notes for that scope yet"}`.

Dispatch (reuse existing generators/persistence verbatim — do NOT duplicate
their logic, call them):

- `quiz`/`test` → `generate_quiz(rows, kind)`, insert into `quizzes` exactly
  like `/quiz/generate` (`unit` = the single unit if all scopes share one,
  else `null`), return the inserted row.
- `flashcards` → same generate+insert path as `/cards/generate` (`unit` same
  single-or-null rule), return `{"count": N}`.
- `cheatsheet` → `build_cheatsheet(rows, cls, unit_label)` where `unit_label`
  is the single unit or `""`; return `{"filename": "<slug> cheatsheet.md",
  "markdown": md}` (frontend downloads via blob; the GET `/cheatsheet`
  endpoint stays untouched).

Errors mirror `/quiz/generate` style: `{"error": ...}`, never raise to client.

Tests: `test_study.py`, plain asserts, FakeSB pattern from `test_note.py`,
Claude call monkeypatched. Cover: scope filtering (unit, topic, overlap
dedupe—unit scope + topic inside it doesn't duplicate rows), unit single/null
rule, no-notes error, bad kind/class errors.

## Frontend — index.html

**Selection.** Module-level `treeSel` = Set of keys (`sem||cls||unit` for
units, `sem||cls||unit||topic` for topics) + `selClass` (the `sem||cls` the
selection lives in). Ctrl-click (or meta-click) on a unit `<summary>` or topic
row toggles selection, `.sel` highlight class; ctrl-click in a different class
clears the selection and starts there. Plain click keeps today's behavior.
`renderTree` reapplies `.sel` from `treeSel` after rewrite (same trick as the
open-`details` set). Selecting a unit implies its topics; backend dedupes.

**Context menu.** `contextmenu` on unit summaries / topic rows:
`preventDefault`; if the target isn't in the selection, selection becomes just
it. Menu is one absolutely-positioned div appended to `body` (survives the 5s
`refresh()` rewrite of `#tree`), closed on click-away / Esc / scroll. Items:
Quiz, Test, Flashcards, Cheat sheet — always; Rename, Delete — only when
selection is exactly one item.

**Actions.**
- Quiz/Test: POST `/study/generate`, on success open the quiz in the existing
  quiz UI (reuse whatever `/quiz/generate`'s current button does with its
  response).
- Flashcards: POST, then toast/alert `N cards added` (match existing pattern).
- Cheat sheet: POST, download `markdown` as a blob with `filename`.
- Rename (single): `prompt()` for new name; unit → `POST /merge_units`
  `{from_unit: old, to_unit: new}`; topic → `POST /merge_topics` same
  unit/new topic. Empty/unchanged name → no-op. (Merge with a fresh
  destination IS rename; notes re-file automatically.)
- Delete (single): reuse the existing tree-del handlers (topic: bulk
  `deleteRec` over its rids; unit: existing unit-delete endpoint/handler).

**Constraint.** Selection never spans two classes (enforced by the clear-on-
other-class rule). Menu generation actions send the current selection's class.

## Out of scope

Shift-click ranges; selecting whole classes/semesters; renaming classes or
semesters; touching the existing Practice card UI.
