# FRQ practice format -- summary

## What changed

- `app.py`
  - `generate_quiz(rows, kind, fmt)`: new `fmt` param. `"mcq"` -> 10/20 all-MCQ
    questions (no more mixed short-answer for new quizzes). `"frq"` -> 4/8
    free-response questions with a rubric. One Claude call, `claude-haiku-4-5`.
  - `grade_frq(questions, files)`: new. One `claude-sonnet-5` vision call
    grades photographed/scanned FRQ pages against each question's rubric.
    Reuses the existing `_doc_block()` helper (already sniffs jpeg/png/webp/pdf
    into Claude content blocks -- no new file-type logic needed). Same
    fence-strip + `by_i` fallback parsing as `grade_short`; missing index ->
    score 0, "grading failed" feedback; score clamped to [0,1].
  - `write_quiz_note(quiz, questions, results, score)`: new. Files a graded
    quiz under `<vault>/<sem>/<cls>/Practice/<kind> <date> <time> -- <score>%.md`,
    mirroring `write_exam_note`'s frontmatter/graph-anchor style. Per-question
    body: MCQ shows choices with correct/picked tags, FRQ shows rubric +
    transcribed answer, legacy `short` questions still render (old quizzes).
  - `/quiz/generate`: accepts `payload["format"]` (`"mcq"`/`"frq"`, default
    `"mcq"`), passed through to `generate_quiz`. No schema change -- format is
    derived from `questions[0]["type"]` on read.
  - `/quiz/{qid}/submit`: now calls `write_quiz_note` after grading, wrapped
    in try/except (Obsidian failure doesn't lose the grade).
  - `/quiz/{qid}/submit_frq`: new. Takes photographed/scanned answer pages,
    saves them to `FRQ_UPLOAD_DIR` (`frq_uploads/`, new dir alongside
    `audio/`), grades with `grade_frq`, persists `answers`/`score` exactly
    like MCQ submit, writes the Obsidian note (best-effort).

- `index.html`
  - `#pFormat` select (MCQ / FRQ) next to `#pKind`; `genQuiz()` sends `format`.
  - `renderQuizForm`: branches on `questions[0].type === 'frq'` -> numbered
    questions only, upload note, `<input type="file" multiple accept="image/*,.pdf"
    capture="environment">`, submit -> `submitFrq`. MCQ/short path unchanged.
  - `submitFrq(qid, btn)`: new. Reads files via `FileReader` -> base64, POSTs
    JSON to `/quiz/{qid}/submit_frq`, reuses `renderQuizResults`.
  - `renderQuizResults`: the non-MCQ branch now distinguishes `frq` (Rubric /
    Transcribed) from legacy `short` (Model answer / You wrote) -- both still
    render for `openQuiz`'s history view.

- `test_quiz.py`: extended (kept the original `grade_mcq` test, renamed to
  `test_grade_mcq`) with `test_generate_quiz_mcq`, `test_generate_quiz_frq`,
  `test_grade_frq_parse`, `test_write_quiz_note`. Added the
  `os.environ.setdefault(...)` block (matching `test_note.py`) so the file
  imports standalone.

## Deviation from PLAN.md: JSON body instead of multipart (C2)

The plan specified `multipart`/`UploadFile`/`File(...)` for `/quiz/{qid}/submit_frq`.
Deviated: the endpoint takes a JSON body `{"files":[{"name":..., "data":<base64>}]}`
instead. Reasons:
- `app.py`'s own `/upload` endpoint has a standing comment explaining it
  deliberately avoids multipart ("multipart would drag in python-multipart")
  and uses raw request bodies instead.
- `python-multipart` is not installed in the test venv and not in
  `requirements.txt` -- adding it would be a new dependency for something a
  few lines of base64 already solves.
- Every other endpoint in the app takes a JSON `Body(...)` -- this keeps the
  new endpoint consistent with existing style, and Claude's vision API needs
  base64 anyway.
- Frontend uses `FileReader.readAsDataURL` instead of `FormData`.

No other deviations. `FRQ_UPLOAD_DIR` mkdir happens at module load (matching
the existing `AUDIO_DIR` pattern) rather than lazily "on use" as the plan's
generic phrasing suggested -- same effect, matches established code style.

## Verify by hand

- **Real Sonnet grading**: generate an FRQ quiz, photograph/scan a real
  handwritten answer (or a legible printed one), submit via `submitFrq`, and
  check the returned transcription/score/feedback are sane -- the parsing
  logic is unit-tested but never called `claude-sonnet-5` for real here.
- **Phone camera upload**: `capture="environment"` on the file input -- take a
  photo directly from a phone browser (not just file picker) and confirm the
  JPEG round-trips through `FileReader` -> base64 -> `_doc_block` correctly
  (test only exercises a canned JPEG magic-byte stub).
- **Obsidian note**: open `<vault>/<sem>/<cls>/Practice/` after a real submit
  and eyeball the rendered `.md` (wikilink resolves, frontmatter looks right
  in Obsidian's properties pane).
- **Multi-file PDF**: an FRQ answer scanned as a single multi-page PDF instead
  of several JPEGs -- `_doc_block` handles it, untested end-to-end here.
