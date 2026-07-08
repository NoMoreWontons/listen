# Study Tools Implementation Plan (homework upload · quiz generator · YouTube supplements)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three features to the `listen` app: (A) homework upload (PDF/photo) auto-categorized and linkable to syllabus due dates, (B) a saved quiz/test generator with mixed MCQ + Claude-graded short answers, (C) YouTube URL intake — store as a reference note or transcribe through the existing whisper pipeline.

**Architecture:** Single-file FastAPI backend (`app.py`, ~1015 lines) + single-page frontend (`index.html`, ~830 lines). Supabase is the datastore (tables `recordings`, `assignments`; new table `quizzes`). Notes are filed to an Obsidian vault by `write_note`. All Claude calls use `claude-haiku-4-5`. New code follows the existing patterns exactly — raw-body uploads (no multipart), background `threading.Thread` workers, `_set(rid, **fields)` for row updates, JSON-with-fence-stripping for Claude responses.

**Tech Stack:** Python 3.11, FastAPI, supabase-py, anthropic SDK, faster-whisper, httpx (all already installed). One new dependency: `yt-dlp` (Task 5 only).

## Global Constraints

- Working directory for ALL tasks: `C:\Users\savag\.aiWorkspace\listen-features` (a git worktree, branch `feat/study-tools`). Never touch `C:\Users\savag\.aiWorkspace\listen`.
- Python interpreter for running tests: `C:\Users\savag\.aiWorkspace\listen\.venv\Scripts\python.exe` (the main checkout's venv — the worktree has none). Run tests from inside the worktree directory so `import app` picks up the worktree's `app.py`.
- `.env` already exists in the worktree (copied in; it is gitignored — never commit it).
- Claude model for every new API call: `claude-haiku-4-5` (match existing calls in `app.py`).
- No new dependencies except `yt-dlp` in Task 5.
- Do NOT start the uvicorn server in any task. Tests import `app` directly (safe: whisper is lazy, no server starts on import).
- Tests are plain `assert`-based scripts named `test_*.py` run with `python test_x.py` — NO pytest, no fixtures (repo convention, see existing `test_sweep.py`).
- Do not run the Supabase migration (Task 1 updates `schema.sql` text only; the operator applies SQL separately).
- Frontend re-render rule: `refresh()` runs every 5 s and rewrites `#list` innerHTML (skipped while focus is inside `#list`). Any new UI that must survive ticks either lives OUTSIDE the containers `refresh()` writes, or is guarded the same way (see `renderMerge` in `index.html`).
- Commit after every task with the exact message given in the task's final step. Commit messages end with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- Line numbers below refer to the files as of commit `ae7975d` (worktree HEAD at Task 1). Later tasks shift them — anchor by the quoted code, not the number.

---

### Task 1: Schema + homework upload backend

**Files:**
- Modify: `schema.sql`
- Modify: `app.py` (`/upload` at line 341, `analyze_pdf` at line 684, new endpoints after `assignments_ics` ~line 681)
- Test: `test_hw.py` (create)

**Interfaces:**
- Consumes: existing `office_text(data, ext=None)`, `process_pdf(rid)`, `sb`, `_set`.
- Produces: `_doc_block(data)` → dict (Claude content block) used inside `analyze_pdf`; `GET /assignments_open` → JSON list of assignment rows ordered by `due_on`; `POST /assignments/{aid}/complete` body `{"recording_id": "<uuid>"}` → `{"ok": true}`; `/upload?kind=homework` accepted. Task 2's UI calls all three.

- [ ] **Step 1: Add migration lines to `schema.sql`**

Append to the end of `schema.sql`:

```sql
-- Homework uploads link to a due date; quizzes store generated practice sets.
alter table assignments add column if not exists status text default 'open';       -- open | submitted
alter table assignments add column if not exists homework_id uuid references recordings(id) on delete set null;

create table if not exists quizzes (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz default now(),
  kind text default 'quiz',              -- quiz (10 q) | test (20 q)
  semester text,
  class text not null,
  unit text,                             -- null = whole class
  questions jsonb not null,              -- [{type:'mcq'|'short', q, choices?, answer, explanation}]
  answers jsonb,                         -- graded per-question results, null until submitted
  score numeric                          -- 0-100, null until submitted
);
```

Also update the comment at line 25 of `schema.sql` from:
```sql
-- source also takes 'upload_audio' | 'pdf' | 'syllabus' for uploaded files.
```
to:
```sql
-- source also takes 'upload_audio' | 'pdf' | 'syllabus' | 'homework' | 'youtube' for uploads.
```

- [ ] **Step 2: Extract `_doc_block` helper and add image support in `app.py`**

In `analyze_pdf` (line 684), the current block-choosing code is:

```python
    import base64
    if pdf_bytes[:4] == b"%PDF":
        doc_block = {"type": "document",
                     "source": {"type": "base64", "media_type": "application/pdf",
                                "data": base64.b64encode(pdf_bytes).decode()}}
    else:  # pptx/docx (zip magic) — extracted text stands in for the document
        doc_block = {"type": "text", "text": "The document's extracted text:\n\n" + office_text(pdf_bytes)}
```

Replace those lines inside `analyze_pdf` with:

```python
    doc_block = _doc_block(pdf_bytes)
```

and add this new module-level function directly ABOVE `def analyze_pdf(...)`:

```python
def _doc_block(data):
    """Claude content block for uploaded course material, sniffed from magic
    bytes: PDF -> native document block, pptx/docx (zip) -> extracted text,
    jpeg/png/gif/webp photo (e.g. homework snapshot) -> image block."""
    import base64
    if data[:4] == b"%PDF":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                               "data": base64.b64encode(data).decode()}}
    if data[:2] == b"PK":  # pptx/docx are zips — extracted text stands in for the document
        return {"type": "text", "text": "The document's extracted text:\n\n" + office_text(data)}
    media = ("image/jpeg" if data[:2] == b"\xff\xd8" else
             "image/png" if data[:4] == b"\x89PNG" else
             "image/gif" if data[:4] in (b"GIF8",) else
             "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None)
    if media:
        return {"type": "image", "source": {"type": "base64", "media_type": media,
                                            "data": base64.b64encode(data).decode()}}
    raise ValueError("unsupported file type — upload a PDF, pptx, docx, or a jpg/png/gif/webp photo")
```

- [ ] **Step 3: Accept `kind=homework` in `/upload`**

In the `upload` endpoint (line 341), change:

```python
    if kind not in ("audio", "pdf", "syllabus"):
        return {"error": f"unknown kind '{kind}'"}
```

to:

```python
    if kind not in ("audio", "pdf", "syllabus", "homework"):
        return {"error": f"unknown kind '{kind}'"}
```

No other change — the existing `else:` branch (`pdf_path(rid).write_bytes(data)` → `process_pdf` thread) already handles it, and `source` is already set to the kind string. `process_pdf`/`analyze_pdf` now accept photos via `_doc_block`.

- [ ] **Step 4: Add assignment endpoints**

Add directly AFTER the `assignments_ics` endpoint (after line 681, before `def analyze_pdf`):

```python
@app.get("/assignments_open")
def assignments_open():
    """Open (not yet submitted) assignments, soonest due first — the homework
    card's link dropdown."""
    return (sb.table("assignments").select("*").eq("status", "open")
            .order("due_on").execute().data)


@app.post("/assignments/{aid}/complete")
def assignment_complete(aid: str, payload: dict = Body(...)):
    """Mark an assignment submitted, remembering which uploaded homework
    recording fulfilled it."""
    sb.table("assignments").update(
        {"status": "submitted", "homework_id": payload.get("recording_id")}
    ).eq("id", aid).execute()
    return {"ok": True}
```

- [ ] **Step 5: Write `test_hw.py`**

```python
"""_doc_block magic-byte sniffing + /upload kind gate. Run: python test_hw.py"""
import io
import zipfile

import app


def demo():
    # PDF magic -> native document block
    b = app._doc_block(b"%PDF-1.7 rest of file")
    assert b["type"] == "document" and b["source"]["media_type"] == "application/pdf"

    # jpeg / png / webp photos -> image blocks
    assert app._doc_block(b"\xff\xd8\xff\xe0 jpeg body")["source"]["media_type"] == "image/jpeg"
    assert app._doc_block(b"\x89PNG\r\n\x1a\n png body")["source"]["media_type"] == "image/png"
    assert app._doc_block(b"RIFF\x00\x00\x00\x00WEBPVP8 ")["source"]["media_type"] == "image/webp"

    # docx (zip magic) -> extracted text block
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:t>homework text</w:t></w:document>")
    b = app._doc_block(buf.getvalue())
    assert b["type"] == "text" and "homework text" in b["text"]

    # junk -> ValueError, not a crash later inside the Claude call
    try:
        app._doc_block(b"\x00\x01\x02garbage")
        assert False, "expected ValueError"
    except ValueError:
        pass

    print("test_hw: OK")


if __name__ == "__main__":
    demo()
```

- [ ] **Step 6: Run the test**

Run from inside `C:\Users\savag\.aiWorkspace\listen-features`:
```
C:\Users\savag\.aiWorkspace\listen\.venv\Scripts\python.exe test_hw.py
```
Expected output: `test_hw: OK`

- [ ] **Step 7: Commit**

```bash
git add schema.sql app.py test_hw.py
git commit -m "feat: homework upload kind + image support + assignment link endpoints

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Homework upload UI

**Files:**
- Modify: `index.html` (Upload deck-group ~line 385, `uploadFile` ~line 591, `refresh` ~line 658)

**Interfaces:**
- Consumes: `POST /upload?kind=homework&filename=...` (raw body), `GET /assignments_open` → `[{id, title, due_on, klass, kind, status, ...}]`, `POST /assignments/{aid}/complete` body `{"recording_id": rid}` (all from Task 1).
- Produces: nothing later tasks use.

- [ ] **Step 1: Add the Homework button to the Upload deck-group**

In the `<div class="deck-group">` containing `uploadBtn` (~line 385-389), after the existing `<input type="file" id="fileInput" ...>` line, add:

```html
      <button id="hwBtn" class="upload-btn" title="Upload homework (PDF or photo) — auto-filed, then link it to a due date">&#8686; Homework</button>
      <input type="file" id="hwInput" accept=".pdf,image/*" hidden>
```

- [ ] **Step 2: Support a forced kind in `uploadFile` and wire the button**

Change the `uploadFile` function (~line 591) from:

```js
async function uploadFile(f) {
  if (!f) return;
  let kind = 'audio';
  if (/\.(pdf|pptx|docx)$/i.test(f.name)) {
```

to:

```js
async function uploadFile(f, forceKind) {
  if (!f) return;
  let kind = forceKind || 'audio';
  if (!forceKind && /\.(pdf|pptx|docx)$/i.test(f.name)) {
```

(the rest of the function is unchanged). Then, next to the existing `fileInput.onchange` wiring (~line 586), add:

```js
const hwInput = document.getElementById('hwInput');
document.getElementById('hwBtn').onclick = () => hwInput.click();
hwInput.onchange = () => { uploadFile(hwInput.files[0], 'homework'); hwInput.value = ''; };
```

- [ ] **Step 3: Fetch open assignments and render the link dropdown on homework cards**

In `refresh()` (~line 658), after the line `const rows = await (await fetch('/recordings')).json();` add:

```js
  openAssignments = rows.some(r => r.source === 'homework' && r.status === 'done')
    ? await (await fetch('/assignments_open')).json() : [];
```

and declare next to the other top-level `let` declarations (~line 435):

```js
let openAssignments = [];
```

In the card template inside `refresh()` (the big template literal ~line 664), after the line
`${r.status !== 'error' ? labelEditor(r) : ''}` add:

```js
      ${r.source === 'homework' && r.status === 'done' ? hwLinker(r) : ''}
```

Then add these functions near `saveLabels` (~line 634):

```js
// homework card: pick which due date this upload fulfills (soonest first = default)
function hwLinker(r) {
  const opts = openAssignments.filter(a => !r.class || !a.klass || a.klass === r.class);
  if (!opts.length) return '';
  return `<div class="merge-manual"><span class="merge-into">fulfills</span>
    <select id="hw-${r.id}">${opts.map(a =>
      `<option value="${escAttr(a.id)}">${esc(a.title)} — due ${esc(a.due_on)}</option>`).join('')}</select>
    <button onclick="linkHw('${r.id}')">Mark submitted</button></div>`;
}

async function linkHw(rid) {
  const aid = document.getElementById('hw-' + rid).value;
  await fetch(`/assignments/${aid}/complete`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ recording_id: rid }),
  });
  document.activeElement?.blur(); // clicked button holds focus in #list — guard would skip re-render
  refresh();
}
```

(`.merge-manual` / `.merge-into` CSS already exists — reuse, no new styles.)

- [ ] **Step 4: Syntax check**

Run from inside the worktree:
```
node -e "const s=require('fs').readFileSync('index.html','utf8');const m=s.match(/<script>([\s\S]*)<\/script>/);new Function(m[1]);console.log('index.html script parses OK')"
```
Expected: `index.html script parses OK`. If `node` is unavailable, use:
```
C:\Users\savag\.aiWorkspace\listen\.venv\Scripts\python.exe -c "import re,pathlib; s=pathlib.Path('index.html').read_text(encoding='utf-8'); assert 'hwBtn' in s and 'hwLinker' in s and 'assignments_open' in s; print('markers OK')"
```

- [ ] **Step 5: Commit**

```bash
git add index.html
git commit -m "feat: homework upload button + due-date link dropdown

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Quiz generator backend

**Files:**
- Modify: `app.py` (new functions + endpoints; put everything directly AFTER the `assignment_complete` endpoint from Task 1)
- Test: `test_quiz.py` (create)

**Interfaces:**
- Consumes: `sb`, `claude`, `re`, `json` module-level names in `app.py`.
- Produces:
  - `grade_mcq(questions, answers)` → `(results, mcq_points, mcq_total)` — pure, no I/O. `questions` is the stored jsonb list; `answers` is a same-length list (MCQ answers are the chosen choice index, short answers are strings). `results` is a same-length list of dicts: MCQ → `{"answer": a, "correct": bool}`, short → `{"answer": a}` (graded later).
  - `grade_short(questions, results)` → float points earned; mutates short entries in `results` adding `score` (0 / 0.5 / 1) and `feedback` (str).
  - `generate_quiz(rows, kind)` → list of question dicts (raises on unparseable Claude output).
  - `POST /quiz/generate` body `{"semester": "", "class": "...", "unit": "", "kind": "quiz"|"test"}` → full quiz row or `{"error": ...}`.
  - `POST /quiz/{qid}/submit` body `{"answers": [...]}` → `{"score": int, "results": [...], "questions": [...]}`.
  - `GET /quizzes` → list (no questions payload); `GET /quiz/{qid}` → full row. Task 4's UI calls all four endpoints.

- [ ] **Step 1: Write the failing test `test_quiz.py`**

```python
"""grade_mcq scoring math. Pure — no Claude, no Supabase writes.
Run: python test_quiz.py"""
import app


def demo():
    questions = [
        {"type": "mcq", "q": "2+2?", "choices": ["3", "4", "5", "6"], "answer": 1},
        {"type": "short", "q": "Explain gravity.", "answer": "Masses attract."},
        {"type": "mcq", "q": "Capital of France?", "choices": ["Rome", "Paris"], "answer": 1},
    ]
    answers = [1, "stuff falls down", 0]  # right, (ungraded here), wrong

    results, pts, total = app.grade_mcq(questions, answers)
    assert (pts, total) == (1, 2), f"got {pts}/{total}"
    assert results[0] == {"answer": 1, "correct": True}
    assert results[2] == {"answer": 0, "correct": False}
    assert results[1] == {"answer": "stuff falls down"}  # short: passthrough, no 'correct' key

    # string-typed index from the browser still matches
    results, pts, total = app.grade_mcq(questions, ["1", "", "1"])
    assert (pts, total) == (2, 2)

    # short answers left blank / answers list shorter than questions -> no crash
    results, pts, total = app.grade_mcq(questions, [None])
    assert total == 1 and pts == 0 and len(results) == 1

    print("test_quiz: OK")


if __name__ == "__main__":
    demo()
```

- [ ] **Step 2: Run it — must fail**

```
C:\Users\savag\.aiWorkspace\listen\.venv\Scripts\python.exe test_quiz.py
```
Expected: `AttributeError: module 'app' has no attribute 'grade_mcq'`

- [ ] **Step 3: Implement the quiz backend in `app.py`**

Add after `assignment_complete` (Task 1):

```python
# --- practice quiz / test generator ---

def generate_quiz(rows, kind="quiz"):
    """One Claude call: mixed MCQ + short-answer questions from the filed
    notes' summaries. Returns the parsed question list; raises if the model
    returns unparseable JSON (caller surfaces it as an error)."""
    n_mcq, n_short = (7, 3) if kind == "quiz" else (14, 6)
    material = "\n\n".join(
        f"## {r.get('topic') or r.get('title') or ''}\n{r.get('summary') or ''}"
        for r in rows if (r.get("summary") or "").strip())
    msg = claude.messages.create(
        model="claude-haiku-4-5", max_tokens=6000,
        messages=[{"role": "user", "content": (
            f"Create a practice {kind} from these college lecture notes: "
            f"{n_mcq} multiple-choice questions and {n_short} short-answer questions. "
            "Return ONLY a JSON array; each item is one of:\n"
            '- {"type":"mcq","q":"...","choices":["...","...","...","..."],'
            '"answer":<0-3 index of the correct choice>,"explanation":"one sentence"}\n'
            '- {"type":"short","q":"...","answer":"model answer, 1-3 sentences","explanation":""}\n'
            "Cover the breadth of the material; make wrong choices plausible.\n\n"
            "Notes:\n" + material)}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


def grade_mcq(questions, answers):
    """Pure MCQ grading: (results, points, mcq_total). results aligns with the
    zipped prefix of questions/answers; short answers pass through ungraded."""
    results, pts, total = [], 0, 0
    for q, a in zip(questions, answers):
        if q.get("type") == "mcq":
            total += 1
            ok = str(a).strip() == str(q.get("answer")).strip()
            pts += ok
            results.append({"answer": a, "correct": bool(ok)})
        else:
            results.append({"answer": a})
    return results, pts, total


def grade_short(questions, results):
    """One Claude call grades all short answers; mutates their entries in
    results with score (0/0.5/1) + feedback. Returns points earned."""
    shorts = [(i, q, results[i].get("answer")) for i, q in enumerate(questions)
              if q.get("type") == "short" and i < len(results)]
    if not shorts:
        return 0.0
    listing = "\n\n".join(
        f"Question {i}: {q.get('q')}\nExpected: {q.get('answer', '')}\n"
        f"Student answer: {a or '(blank)'}" for i, q, a in shorts)
    msg = claude.messages.create(
        model="claude-haiku-4-5", max_tokens=1500,
        messages=[{"role": "user", "content": (
            "Grade these short-answer quiz responses. Return ONLY a JSON array with one "
            'object per question, in order: {"i": <question number>, "score": <0, 0.5 or 1>, '
            '"feedback": "one sentence"}. Full credit for capturing the idea in the '
            "student's own words; half for partially right.\n\n" + listing)}],
    )
    raw = re.sub(r"^```(?:json)?|```$", "", msg.content[0].text.strip(), flags=re.MULTILINE).strip()
    try:
        by_i = {g.get("i"): g for g in json.loads(raw)}
    except (json.JSONDecodeError, AttributeError, TypeError):
        by_i = {}
    pts = 0.0
    for i, q, a in shorts:
        g = by_i.get(i) or {"score": 0, "feedback": "grading failed — compare with the model answer"}
        try:
            s = min(max(float(g.get("score") or 0), 0.0), 1.0)
        except (TypeError, ValueError):
            s = 0.0
        results[i]["score"] = s
        results[i]["feedback"] = str(g.get("feedback") or "")
        pts += s
    return pts


@app.post("/quiz/generate")
def quiz_generate(payload: dict = Body(...)):
    sem = (payload.get("semester") or "").strip()
    cls = (payload.get("class") or "").strip()
    unit = (payload.get("unit") or "").strip()
    kind = payload.get("kind") if payload.get("kind") in ("quiz", "test") else "quiz"
    if not cls:
        return {"error": "class required"}
    q = (sb.table("recordings").select("topic,title,summary")
         .eq("status", "done").eq("class", cls))
    if sem:
        q = q.eq("semester", sem)
    if unit:
        q = q.eq("unit", unit)
    rows = q.execute().data
    if not any((r.get("summary") or "").strip() for r in rows):
        return {"error": "no filed notes for that scope yet"}
    try:
        questions = generate_quiz(rows, kind)
    except Exception as e:
        return {"error": f"generation failed: {e}"}
    return sb.table("quizzes").insert({
        "kind": kind, "semester": sem or None, "class": cls, "unit": unit or None,
        "questions": questions,
    }).execute().data[0]


@app.post("/quiz/{qid}/submit")
def quiz_submit(qid: str, payload: dict = Body(...)):
    quiz = sb.table("quizzes").select("*").eq("id", qid).single().execute().data
    questions = quiz["questions"] or []
    answers = payload.get("answers") or []
    answers += [None] * (len(questions) - len(answers))  # pad skipped questions
    results, pts, mcq_total = grade_mcq(questions, answers)
    pts += grade_short(questions, results)
    total = mcq_total + sum(1 for q in questions if q.get("type") == "short")
    score = round(pts / max(total, 1) * 100)
    sb.table("quizzes").update({"answers": results, "score": score}).eq("id", qid).execute()
    return {"score": score, "results": results, "questions": questions}


@app.get("/quizzes")
def quizzes():
    return (sb.table("quizzes").select("id,created_at,kind,semester,class,unit,score")
            .order("created_at", desc=True).execute().data)


@app.get("/quiz/{qid}")
def quiz_get(qid: str):
    return sb.table("quizzes").select("*").eq("id", qid).single().execute().data
```

- [ ] **Step 4: Run the test — must pass**

```
C:\Users\savag\.aiWorkspace\listen\.venv\Scripts\python.exe test_quiz.py
```
Expected: `test_quiz: OK`

- [ ] **Step 5: Commit**

```bash
git add app.py test_quiz.py
git commit -m "feat: quiz/test generator backend — generate, grade, persist

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Quiz UI (Practice card)

**Files:**
- Modify: `index.html` (side column ~line 403-431, script)

**Interfaces:**
- Consumes (from Task 3): `POST /quiz/generate` `{semester, class, unit, kind}` → quiz row `{id, kind, class, unit, questions: [...]}` or `{error}`; `POST /quiz/{id}/submit` `{answers: [...]}` → `{score, results, questions}`; `GET /quizzes` → `[{id, created_at, kind, class, unit, score}]`; `GET /quiz/{id}` → full row (has `questions`, `answers`, `score`).
- Question shapes: MCQ `{type:'mcq', q, choices: [4 strings], answer: <int index>, explanation}`; short `{type:'short', q, answer: <model answer>, explanation}`. Graded result entries: MCQ `{answer, correct}`, short `{answer, score, feedback}`.
- Produces: nothing later tasks use.

**Refresh rule (critical):** the quiz being taken renders into `#quizArea`, which `refresh()` NEVER writes — an in-progress quiz survives the 5 s tick. Only the Practice card's selects and past-quiz list re-render, with the same focus guard `renderMerge` uses.

- [ ] **Step 1: Add the Practice card and quiz area to the HTML**

In the `.side` div, directly after the `</section>` that closes `#graphCard` (~line 430), add:

```html
    <section class="folders" id="practice">
      <span class="deck-label">Practice</span>
      <div class="merge-manual">
        <select id="pClass" onchange="pClassChanged()"></select>
        <select id="pUnit"></select>
        <select id="pKind">
          <option value="quiz">Quiz — 10 q</option>
          <option value="test">Test — 20 q</option>
        </select>
        <button onclick="genQuiz(this)">Generate</button>
      </div>
      <div id="pastQuizzes"></div>
    </section>
```

In the main column, inside `<section class="sessions">` directly BEFORE `<div id="list"></div>` (~line 401), add:

```html
    <div id="quizArea"></div>
```

- [ ] **Step 2: Add the quiz CSS**

Add to the `<style>` block, after the `.merge-manual select` rule (~line 214):

```css
  /* ---- practice quizzes ---- */
  .pq-row { display: flex; justify-content: space-between; gap: .5rem; padding: .4rem 0;
    border-bottom: 1px solid #efe9db; font-size: .82rem; cursor: pointer; }
  .pq-row:hover { background: #f1ecdf; }
  .pq-score { font-family: var(--font-mono); font-size: .72rem; color: var(--ink-soft); }
  .quiz { background: var(--card); border: 1px solid var(--card-edge);
    border-radius: var(--radius-card); padding: 1rem 1.15rem; margin: 0 0 .85rem; }
  .quiz .q { margin: .8rem 0; }
  .quiz .q p { margin: 0 0 .35rem; font-weight: 600; }
  .quiz label { display: block; font-size: .88rem; padding: .15rem 0; cursor: pointer; }
  .quiz textarea { width: 100%; padding: .35rem .5rem; font-size: .82rem; font-family: inherit;
    border: 1px solid #d8d2c2; border-radius: 6px; background: #fffdf8; resize: vertical; }
  .quiz .verdict { font-size: .8rem; margin: .25rem 0 0; }
  .quiz .verdict.good { color: #226e5b; }
  .quiz .verdict.bad { color: #8f2323; }
  .quiz .quiz-head { display: flex; justify-content: space-between; align-items: baseline; }
  .quiz .quiz-head button { border: none; background: none; cursor: pointer; color: var(--ink-soft); }
  .quiz button.submit { padding: .35rem .8rem; font-size: .78rem; border: 0; border-radius: 6px;
    cursor: pointer; background: var(--amber); color: #3a2c00; font-weight: 600; }
```

- [ ] **Step 3: Add the quiz JS**

Add before the final `setInterval(refresh, 5000);` line:

```js
// ---- practice quizzes: selects + past list re-render on refresh (focus-guarded);
// ---- the quiz being taken lives in #quizArea, which refresh() never touches.
let quizRows = [];

function renderPractice(rows) {
  const card = document.getElementById('practice');
  if (document.activeElement && card.contains(document.activeElement)) return;
  const classes = [...new Set(rows.filter(r => r.status === 'done' && r.class).map(r => r.class))].sort();
  const sel = document.getElementById('pClass');
  const cur = sel.value;
  sel.innerHTML = classes.map(c => `<option value="${escAttr(c)}">${esc(c)}</option>`).join('');
  if (classes.includes(cur)) sel.value = cur;
  quizRows = rows;
  pClassChanged();
  loadPastQuizzes();
}

function pClassChanged() {
  const cls = document.getElementById('pClass').value;
  const units = [...new Set(quizRows.filter(r => r.status === 'done' && r.class === cls && r.unit)
    .map(r => r.unit))].sort();
  document.getElementById('pUnit').innerHTML = '<option value="">whole class</option>'
    + units.map(u => `<option value="${escAttr(u)}">${esc(u)}</option>`).join('');
}

let pastLoaded = 0;
async function loadPastQuizzes() {
  if (Date.now() - pastLoaded < 30000) return; // past list barely changes — poll it gently
  pastLoaded = Date.now();
  const qs = await (await fetch('/quizzes')).json();
  document.getElementById('pastQuizzes').innerHTML = qs.map(q => `
    <div class="pq-row" onclick="openQuiz('${q.id}')">
      <span>${esc(q.kind)} · ${esc(q.class || '')}${q.unit ? ' / ' + esc(q.unit) : ''}</span>
      <span class="pq-score">${q.score === null ? 'unfinished' : q.score + '%'} · ${(q.created_at || '').slice(0, 10)}</span>
    </div>`).join('');
}

async function genQuiz(btn) {
  const body = {
    class: document.getElementById('pClass').value,
    unit: document.getElementById('pUnit').value,
    kind: document.getElementById('pKind').value,
  };
  if (!body.class) { alert('record and file some lectures first'); return; }
  btn.disabled = true; btn.textContent = 'generating…';
  try {
    const q = await (await fetch('/quiz/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })).json();
    if (q.error) { alert(q.error); return; }
    pastLoaded = 0;
    renderQuizForm(q);
  } finally { btn.disabled = false; btn.textContent = 'Generate'; document.activeElement?.blur(); }
}

function renderQuizForm(q) {
  document.getElementById('quizArea').innerHTML = `<div class="quiz" data-qid="${q.id}">
    <div class="quiz-head"><b>${esc(q.kind)} — ${esc(q.class || '')}${q.unit ? ' / ' + esc(q.unit) : ''}</b>
      <button onclick="closeQuiz()" title="Close">&#10005;</button></div>
    ${q.questions.map((qq, i) => `<div class="q" data-i="${i}">
      <p>${i + 1}. ${esc(qq.q)}</p>
      ${qq.type === 'mcq'
        ? qq.choices.map((c, ci) => `<label><input type="radio" name="q${i}" value="${ci}"> ${esc(c)}</label>`).join('')
        : `<textarea rows="2" placeholder="your answer"></textarea>`}
    </div>`).join('')}
    <button class="submit" onclick="submitQuiz(this, '${q.id}')">Submit</button>
  </div>`;
  document.getElementById('quizArea').scrollIntoView({ behavior: 'smooth' });
}

async function submitQuiz(btn, qid) {
  const quiz = document.querySelector(`.quiz[data-qid="${qid}"]`);
  const answers = [...quiz.querySelectorAll('.q')].map(div => {
    const r = div.querySelector('input[type="radio"]:checked');
    if (r) return +r.value;
    const t = div.querySelector('textarea');
    return t ? t.value : null;
  });
  btn.disabled = true; btn.textContent = 'grading…';
  try {
    const g = await (await fetch(`/quiz/${qid}/submit`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answers }),
    })).json();
    pastLoaded = 0;
    renderQuizResults(qid, g.questions, g.results, g.score);
  } finally { btn.disabled = false; }
}

function renderQuizResults(qid, questions, results, score) {
  document.getElementById('quizArea').innerHTML = `<div class="quiz" data-qid="${qid}">
    <div class="quiz-head"><b>Score: ${score}%</b>
      <button onclick="closeQuiz()" title="Close">&#10005;</button></div>
    ${questions.map((qq, i) => {
      const r = results[i] || {};
      let verdict;
      if (qq.type === 'mcq') {
        const ok = r.correct;
        verdict = `<p class="verdict ${ok ? 'good' : 'bad'}">${ok ? '✓ correct' :
          `✗ ${r.answer === null || r.answer === undefined || r.answer === '' ? 'skipped' : 'you picked: ' + esc(String(qq.choices[r.answer] ?? r.answer))} — answer: ${esc(String(qq.choices[qq.answer] ?? qq.answer))}`}
          ${qq.explanation ? '<br>' + esc(qq.explanation) : ''}</p>`;
      } else {
        const s = r.score ?? 0;
        verdict = `<p class="verdict ${s >= 1 ? 'good' : 'bad'}">${s >= 1 ? '✓' : s > 0 ? '½' : '✗'} ${esc(r.feedback || '')}
          <br>Model answer: ${esc(qq.answer || '')}</p>
          <p class="verdict">You wrote: ${esc(String(r.answer || '(blank)'))}</p>`;
      }
      return `<div class="q"><p>${i + 1}. ${esc(qq.q)}</p>${verdict}</div>`;
    }).join('')}
  </div>`;
}

async function openQuiz(qid) {
  const q = await (await fetch('/quiz/' + qid)).json();
  document.activeElement?.blur();
  if (q.answers) renderQuizResults(qid, q.questions, q.answers, q.score);
  else renderQuizForm(q);
  document.getElementById('quizArea').scrollIntoView({ behavior: 'smooth' });
}

function closeQuiz() { document.getElementById('quizArea').innerHTML = ''; }
```

And in `refresh()`, after the existing `renderMerge(rows);` call, add:

```js
  renderPractice(rows);
```

- [ ] **Step 4: Syntax check**

```
node -e "const s=require('fs').readFileSync('index.html','utf8');const m=s.match(/<script>([\s\S]*)<\/script>/);new Function(m[1]);console.log('index.html script parses OK')"
```
Expected: `index.html script parses OK`. (Python marker fallback as in Task 2 if node is missing.)

- [ ] **Step 5: Commit**

```bash
git add index.html
git commit -m "feat: practice quiz UI — generate, take, grade, revisit

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: YouTube intake backend

**Files:**
- Modify: `requirements.txt`, `app.py` (new endpoint + worker after the quiz endpoints from Task 3)
- Test: `test_yt.py` (create)

**Interfaces:**
- Consumes: `httpx`, `sb`, `_set`, `process(rid)`, `audio_path(rid)`, `AUDIO_DIR`, `threading`, `re`.
- Produces: `_is_yt(url)` → bool (pure); `POST /upload_yt` body `{"url": "...", "mode": "store"|"transcribe"}` → `{"id": rid}` or `{"error": ...}`. Task 6's UI calls it.
- Behavior contract: `store` creates an already-`done` row (title from YouTube oEmbed, link kept in transcript+summary; user files it via the existing label editor — `/label` → `write_note` handles the Obsidian side, no new code). `transcribe` downloads bestaudio via `yt-dlp`, renames to the `.webm` path, and reuses `process(rid)` verbatim.

- [ ] **Step 1: Add the dependency**

Append to `requirements.txt`:

```
yt-dlp
```

Install into the shared venv:
```
C:\Users\savag\.aiWorkspace\listen\.venv\Scripts\pip.exe install yt-dlp
```

- [ ] **Step 2: Write the failing test `test_yt.py`**

```python
"""_is_yt URL gate. Pure — no network. Run: python test_yt.py"""
import app


def demo():
    ok = ["https://www.youtube.com/watch?v=dQw4w9WgXcQ",
          "https://youtube.com/watch?v=abc123",
          "http://youtu.be/abc123",
          "https://www.youtube.com/shorts/xyz"]
    bad = ["https://vimeo.com/12345",
           "https://example.com/youtube.com/fake",
           "not a url at all",
           "",
           "javascript:alert(1)//youtu.be"]
    for u in ok:
        assert app._is_yt(u), f"should accept {u}"
    for u in bad:
        assert not app._is_yt(u), f"should reject {u}"
    print("test_yt: OK")


if __name__ == "__main__":
    demo()
```

- [ ] **Step 3: Run it — must fail**

```
C:\Users\savag\.aiWorkspace\listen\.venv\Scripts\python.exe test_yt.py
```
Expected: `AttributeError: module 'app' has no attribute '_is_yt'`

- [ ] **Step 4: Implement in `app.py`**

Add after the quiz endpoints (Task 3):

```python
# --- YouTube intake: reference link or full transcription ---

def _is_yt(url):
    return bool(re.match(r"https?://(www\.)?(youtube\.com|youtu\.be)/", url or ""))


def _yt_title(url):
    """Video title via YouTube's public oEmbed endpoint — no API key."""
    r = httpx.get("https://www.youtube.com/oembed",
                  params={"url": url, "format": "json"}, timeout=15)
    r.raise_for_status()
    return r.json().get("title") or url


def _yt_process(rid, url):
    """Download bestaudio with yt-dlp, then hand off to the normal whisper
    pipeline. yt-dlp picks the extension; rename to the .webm path process()
    expects (ffmpeg sniffs the real container from content)."""
    try:
        import yt_dlp
        opts = {"format": "bestaudio", "quiet": True, "noprogress": True,
                "outtmpl": str(AUDIO_DIR / f"{rid}.%(ext)s")}
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        for f in AUDIO_DIR.glob(f"{rid}.*"):
            if f.suffix != ".webm":
                f.rename(audio_path(rid))
        process(rid)
    except Exception as e:
        _set(rid, status="error", stage=None, progress=None, summary=f"[error: {e}]")


@app.post("/upload_yt")
def upload_yt(payload: dict = Body(...)):
    """mode='store': file the link as a supplemental reference note (label it
    via the editor to send it to Obsidian). mode='transcribe': download the
    audio and run the whisper pipeline like any recording."""
    url = (payload.get("url") or "").strip()
    mode = payload.get("mode") if payload.get("mode") in ("store", "transcribe") else "store"
    if not _is_yt(url):
        return {"error": "not a YouTube URL"}
    try:
        title = _yt_title(url)
    except Exception:
        title = url  # oEmbed down/video private — keep the raw link as the title
    if mode == "store":
        row = sb.table("recordings").insert({
            "title": title, "status": "done", "source": "youtube",
            "transcript": f"Supplemental video: [{title}]({url})",
            "summary": f"- Reference video — [{title}]({url})",
        }).execute().data[0]
        return {"id": row["id"]}
    row = sb.table("recordings").insert({
        "title": title, "status": "transcribing", "source": "youtube",
        "notes": f"Source video: {url}",  # analyze() weaves the link into the summary
    }).execute().data[0]
    _set(row["id"], stage="loading_model")
    threading.Thread(target=_yt_process, args=(row["id"], url), daemon=True).start()
    return {"id": row["id"]}
```

Note: `sweep_old_audio` globs `*.webm` and `*.pdf` — the renamed download is covered by retention automatically.

- [ ] **Step 5: Run the test — must pass**

```
C:\Users\savag\.aiWorkspace\listen\.venv\Scripts\python.exe test_yt.py
```
Expected: `test_yt: OK`

- [ ] **Step 6: Commit**

```bash
git add requirements.txt app.py test_yt.py
git commit -m "feat: YouTube intake — store link or transcribe via yt-dlp

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: YouTube UI + README

**Files:**
- Modify: `index.html` (Upload deck-group), `README.md`

**Interfaces:**
- Consumes: `POST /upload_yt` `{url, mode}` → `{id}` or `{error}` (Task 5).
- Produces: nothing later tasks use.

- [ ] **Step 1: Add the YouTube button**

In the Upload deck-group (next to `hwBtn` from Task 2), add:

```html
      <button id="ytBtn" class="upload-btn" title="Add a YouTube video — save the link as a reference note, or transcribe it like a lecture">&#9654; YouTube</button>
```

And next to the `hwInput.onchange` wiring, add:

```js
document.getElementById('ytBtn').onclick = async () => {
  const url = prompt('YouTube URL:');
  if (!url) return;
  const mode = confirm('Transcribe the video?\nOK = transcribe like a lecture\nCancel = just save the link as a reference note')
    ? 'transcribe' : 'store';
  const resp = await (await fetch('/upload_yt', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, mode }),
  })).json();
  if (resp.error) alert(resp.error);
  refresh();
};
```

- [ ] **Step 2: Update README.md**

In `README.md`:

1. After the "How it works" numbered list (after line 22, `Raw audio stays local...`), add:

```markdown
7. **Uploads** beyond live recording: audio files, lecture PDFs/slides (`pdf`),
   syllabi (`syllabus` — due dates land in an `.ics` you can import), homework
   (`homework` — PDF or photo, auto-categorized, then link it to a syllabus due
   date to mark it submitted), and YouTube links (save as a reference note, or
   transcribe the audio like a lecture).
8. **Practice quizzes:** generate a 10-question quiz or 20-question test from any
   class/unit's filed notes — multiple choice graded instantly, short answers
   graded by Claude. Past quizzes are saved with scores.
```

2. In the "Not built yet (deferred)" section (line 94-98), no change needed unless it mentions these features — it doesn't.

3. In the checks list in "Notes" (~line 90-92), extend the sentence to include the new checks:

```markdown
- **Checks:** `python test_sweep.py` (retention), `python test_note.py` (Obsidian
  filing), `python test_notion.py` (Notion block flatten), `python test_model_stall.py`
  (model warm-up), `python test_hw.py` (upload block sniffing), `python test_quiz.py`
  (MCQ grading), `python test_yt.py` (YouTube URL gate).
```

- [ ] **Step 3: Syntax check**

```
node -e "const s=require('fs').readFileSync('index.html','utf8');const m=s.match(/<script>([\s\S]*)<\/script>/);new Function(m[1]);console.log('index.html script parses OK')"
```
Expected: `index.html script parses OK`.

- [ ] **Step 4: Run the full check suite**

From the worktree:
```
C:\Users\savag\.aiWorkspace\listen\.venv\Scripts\python.exe test_hw.py
C:\Users\savag\.aiWorkspace\listen\.venv\Scripts\python.exe test_quiz.py
C:\Users\savag\.aiWorkspace\listen\.venv\Scripts\python.exe test_yt.py
C:\Users\savag\.aiWorkspace\listen\.venv\Scripts\python.exe test_sweep.py
```
Expected: each prints its OK line (test_sweep prints its own pass output).

- [ ] **Step 5: Commit**

```bash
git add index.html README.md
git commit -m "feat: YouTube button + README for homework, quizzes, YouTube

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Post-plan (operator, not agents)

1. Apply the Task 1 SQL to Supabase (MCP `apply_migration` or SQL editor).
2. Code review the branch (`git diff master...feat/study-tools`).
3. Manual QA: start the server from the worktree, exercise homework upload, quiz generate/submit, YouTube store + transcribe.
4. Merge `feat/study-tools` into `master`, remove the worktree.
