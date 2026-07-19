# Plan: MCQ-or-FRQ practice tests, Sonnet-graded FRQ, Obsidian export

Repo: this worktree (branch `frq-format`). Stack: FastAPI (`app.py`, single file),
vanilla-JS single page (`index.html`), Supabase (`sb` client, `quizzes` table with
JSON `questions`/`answers` columns + `kind`, `semester`, `class`, `unit`, `score`),
Anthropic SDK (`claude` client already constructed). Obsidian vault root:
`OBSIDIAN_VAULT` (pathlib.Path). Tests are plain-python assert scripts
(`test_note.py` pattern: monkeypatch `app.claude`/`app.sb`, `print("ok: ...")`).

Constraints (ponytail):
- NO Supabase schema changes. Format is derivable from `questions[0]["type"]`.
  FRQ upload files live on disk keyed by quiz id.
- Match existing code style: module-level functions, short docstrings, f-strings,
  `_slug()` for vault paths, same error-dict pattern (`{"error": "..."}`).
- Generation stays on `claude-haiku-4-5`. FRQ grading uses `claude-sonnet-5`
  (user requirement — vision grading of handwritten work).

## A. Backend — generation

A1. `generate_quiz(rows, kind, fmt)` — add `fmt` param (`"mcq"` | `"frq"`).
    - `mcq`: 10 (quiz) / 20 (test) questions, all `{"type":"mcq",...}` (same shape as today).
    - `frq`: 4 (quiz) / 8 (test) questions, each
      `{"type":"frq","q":"...","rubric":"2-4 bullet points of what a full-credit answer must include"}`.
    - Prompt returns ONLY a JSON array (keep the existing fence-stripping + json.loads).
A2. `/quiz/generate`: accept `payload["format"]`, default `"mcq"`, validate against
    `("mcq","frq")` same way `kind` is validated. Pass to `generate_quiz`.

## B. Backend — MCQ submit (mostly exists)

B1. Existing `/quiz/{qid}/submit` + `grade_mcq` already handle all-MCQ correctly
    (`grade_short` no-ops with no shorts). Do not restructure.
B2. After grading, call the new `write_quiz_note(quiz, questions, results, score)`
    (see D) and ignore its failure (wrap in try/except — Obsidian down must not
    lose the grade).

## C. Backend — FRQ submit (new)

C1. `FRQ_UPLOAD_DIR = DATA_DIR / "frq_uploads"` (find the existing data/uploads dir
    convention in app.py and follow it; mkdir on use).
C2. `POST /quiz/{qid}/submit_frq` — multipart, `files: list[UploadFile]`
    (FastAPI `File(...)`). Accept images (jpg/png/webp/heic-as-jpeg) and PDF.
    Save each as `frq_uploads/{qid}_{n}{suffix}`.
C3. `grade_frq(questions, files)` — ONE `claude.messages.create` on
    `model="claude-sonnet-5"`, content = [image/document blocks (base64) for each
    file] + text prompt: the numbered questions with rubrics, "grade the
    handwritten work in the attached pages; return ONLY a JSON array
    `{\"i\": n, \"score\": 0|0.5|1, \"feedback\": \"1-2 sentences\", \"transcription\": \"student's answer, briefly\"}`".
    - PDF → `{"type":"document","source":{"type":"base64","media_type":"application/pdf",...}}`;
      images → `{"type":"image",...}`. max_tokens 3000.
    - Parse with the same fence-strip + `by_i` fallback pattern as `grade_short`
      (missing i → score 0, feedback "grading failed — compare with the rubric").
C4. Results align with questions: `results[i] = {"answer": transcription, "score": s, "feedback": ...}`.
    `score = round(pts/total*100)`. Persist exactly like MCQ:
    `sb.table("quizzes").update({"answers": results, "score": score})`.
    Then `write_quiz_note(...)` (try/except). Return `{"score", "results", "questions"}`.

## D. Backend — Obsidian export (new)

D1. `write_quiz_note(quiz, questions, results, score)`:
    - Path: `OBSIDIAN_VAULT / _slug(sem) / _slug(cls) / "Practice" / f"{kind} {YYYY-MM-DD HHMM} — {score}%.md"`
      (sem may be None → skip that segment only if the vault layout requires it;
      check how exam notes handle it and mirror. mkdir parents.)
    - Frontmatter: `class`, `kind`, `score`, `date`, `tags: [practice]`.
    - Body: per question — `### Q{n}`, question text; MCQ: choices with the
      correct one and the student's pick marked; FRQ: rubric + transcribed answer;
      then score + feedback line. End with the graph anchor:
      `Class: [[{s_sem}/{s_cls}/{s_cls}|{s_cls}]]` (same pattern as write_exam_note).
    - Returns path str.

## E. Frontend (`index.html`)

E1. Practice section: add `<select id="pFormat">` with options
    `MCQ` (`value="mcq"`, default) / `FRQ — write on paper` (`value="frq"`),
    next to `#pKind`. genQuiz() sends `format` in the POST body.
E2. `showQuiz(q)`: branch on `q.questions[0].type === 'frq'`:
    - FRQ view: numbered questions only (no inputs), a note
      "Write your answers on paper, then photograph/scan and upload.",
      `<input type="file" id="frqFiles" multiple accept="image/*,.pdf" capture="environment">`,
      submit button → `submitFrq(qid, btn)`.
    - MCQ view: existing rendering unchanged.
E3. `submitFrq`: FormData with all files → `POST /quiz/{qid}/submit_frq`,
    disable button while grading ("Grading…"), then reuse/extend the existing
    graded-view renderer: per-question score, feedback, transcribed answer.
E4. Past-quizzes list + `viewQuiz` must render saved FRQ results correctly
    (they flow through the same `answers` JSON — verify the graded renderer
    handles `frq` type; extend where it assumes mcq/short).

## F. Tests (plain assert scripts, follow test_note.py's monkeypatch style)

F1. `generate_quiz` builds the right prompt/count for (quiz|test) x (mcq|frq)
    — fake `app.claude` returning canned JSON; assert parsed shapes.
F2. `grade_frq` parse: canned Sonnet reply (with fences) → results aligned,
    missing index → score 0 fallback; clamped scores.
F3. `write_quiz_note`: tmp vault, assert path, frontmatter, per-question body,
    `Class: [[...]]` anchor present.
F4. Run existing `test_note.py` + new tests; all must print ok.

## G. Wrap-up

G1. `git add` + commit (conventional message, body explains why), DO NOT push.
G2. Leave a short SUMMARY.md in the worktree root: what changed, what to
    verify by hand (real Sonnet grading call, phone camera upload).

Order: A → C → D → B → E → F → G. Backend testable without UI.
