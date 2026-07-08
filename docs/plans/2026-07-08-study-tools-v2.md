# Study Tools v2 Implementation Plan (flashcards + SM-2 · cheat-sheet export · cards-due reminders)

> **For agentic workers:** Each task below is dispatched to one Opus agent working the shared worktree. Execute your assigned task ONLY, verbatim, TDD where a pure function exists. Commit with the exact message in the task's final step. Do not touch other tasks' scope.

**Goal:** Add three study features to the `listen` app, extending the v1 study-tools set (homework/quiz/YouTube, shipped `4097010`):
- **(1) Flashcards + spaced repetition** — generate flashcards from filed lecture summaries; review them on an SM-2 schedule.
- **(4) Cheat-sheet export** — one-page **markdown** study sheet per class/unit, assembled from filed summaries.
- **(5) Cards-due calendar reminders** — `.ics` feed of upcoming flashcard due-dates, mirroring the existing syllabus `.ics`.

**Architecture:** Single-file FastAPI backend (`app.py`, ~1237 lines) + single-page frontend (`index.html`, ~1032 lines). Supabase is the datastore (existing tables `recordings`, `assignments`, `quizzes`; new table `cards`). All Claude calls use `claude-haiku-4-5`. New code follows existing patterns EXACTLY — raw-body uploads, `_set`-style row updates, one Haiku call with JSON-array + fence-stripping (see `generate_quiz` at `app.py:704`), pure grader/builder functions that are unit-tested (see `grade_mcq` at `app.py:729`).

**Tech stack:** Python 3.11, FastAPI, supabase-py, anthropic SDK (all installed). **NO new dependencies** — cheat-sheet is markdown only (no PDF library; the browser prints to PDF).

## Global Constraints

- Working directory for ALL tasks: `C:\Users\savag\.aiWorkspace\listen-features` (git worktree, branch `feat/study-tools-v2`). NEVER touch `C:\Users\savag\.aiWorkspace\listen`.
- Python interpreter for tests: `C:\Users\savag\.aiWorkspace\listen\.venv\Scripts\python.exe` (main checkout's venv — the worktree has none). This venv is **uv-managed, no pip bootstrapped**; you should not need to install anything (no new deps). Run tests from inside the worktree so `import app` picks up the worktree's `app.py`.
- `.env` already exists in the worktree (copied in; gitignored — NEVER commit it).
- Claude model for every new API call: `claude-haiku-4-5`.
- Do NOT start the uvicorn server. Tests `import app` directly (safe: whisper is lazy, no server on import).
- Tests are plain `assert`-based scripts named `test_*.py`, run with `python test_x.py` — NO pytest, no fixtures (repo convention, see `test_quiz.py`).
- **Do NOT apply the Supabase migration.** Tasks edit `schema.sql` text only. The orchestrator applies the `cards` DDL via the Supabase MCP after Task 1 is verified. (The v1 migration — quizzes + assignments columns — is ALREADY applied in the live DB.)
- Frontend re-render rule: `refresh()` runs every 5 s and rewrites `#list` (skipped while focus is inside `#list`). Any new UI that must survive ticks either lives OUTSIDE the containers `refresh()` writes (like `#quizArea`), or is focus-guarded (see `renderPractice` at `index.html:909`: `if (document.activeElement && card.contains(document.activeElement)) return;`).
- **Refresh-path fetch guard (load-bearing — this caused a real bug last cycle):** any fetch made from inside `refresh()` (e.g. a due-count badge) MUST tolerate a non-array/error response without throwing — else it blanks the recordings list every 5 s. Wrap in `try/catch`, default to `[]`, and `Array.isArray()`-check before `.filter`/`.map`. See the guard added at `index.html:735`.
- Commit after your task with the EXACT message given. Commit messages end with:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- Line numbers refer to files at commit `4097010` (worktree HEAD at Task 1). Later tasks shift them — anchor by quoted code, not the number.

---

### Task 1: Flashcards backend + SM-2 scheduler  (feature 1, backend)

**Files:**
- Modify: `schema.sql` (append `cards` table)
- Modify: `app.py` (new `# --- flashcards / spaced repetition ---` section immediately after `quiz_get` at `app.py:829`, before the YouTube section at `:832`)
- Test: `test_cards.py` (create) — TDD

**Interfaces:**
- Consumes: `claude` client, `sb`, the quiz-scope query pattern in `quiz_generate` (`app.py:781`).
- Produces:
  - Pure `sm2(interval, reps, ease, quality)` → `(interval, reps, ease)`.
  - `generate_cards(rows, n=15)` → `[{"front","back"}]` (one Haiku call, mirrors `generate_quiz`).
  - `POST /cards/generate` body `{semester?, class, unit?}` → `{"count": N}` (or `{"error": ...}`).
  - `GET /cards/due` → JSON list of due card rows (`due_at <= now()`), soonest first.
  - `POST /cards/{id}/review` body `{"grade": "again|hard|good|easy"}` → `{"due_at","interval"}`.
  - Task 2 (UI) and Task 4 (.ics) consume these.

- [ ] **Step 1: Append `cards` table to `schema.sql`**

```sql
-- Flashcards with SM-2 spaced-repetition scheduling.
create table if not exists cards (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz default now(),
  semester text,
  class text not null,
  unit text,                              -- null = whole class
  front text not null,                    -- prompt (term / question)
  back text not null,                     -- answer
  due_at timestamptz default now(),       -- when next due
  interval int default 0,                 -- days to next review
  reps int default 0,                     -- consecutive successful reviews
  ease numeric default 2.5                -- SM-2 ease factor (floor 1.3)
);
alter table cards enable row level security;   -- match quizzes/recordings; app uses the service key
```

- [ ] **Step 2 (RED): Write `test_cards.py` for `sm2` before implementing it**

`sm2` is classic SuperMemo-2. `quality` maps from the review grade: `again=0, hard=3, good=4, easy=5`. Rules: `quality < 3` is a lapse — reset `reps=0`, `interval=1`; otherwise `reps==0 → interval=1`, `reps==1 → interval=6`, else `interval=round(interval*ease)`, and `reps+=1`. Ease updates EVERY review: `ease = max(1.3, ease + (0.1 - (5-quality)*(0.08 + (5-quality)*0.02)))`.

Write these assertions (cover growth, lapse-reset, and the ease floor — not just happy path):

```python
import app

# happy-path interval growth on "good" (q=4)
i, r, e = app.sm2(0, 0, 2.5, 4);  assert (i, r) == (1, 1), (i, r)          # first success -> 1 day
i, r, e = app.sm2(1, 1, 2.5, 4);  assert (i, r) == (6, 2), (i, r)          # second -> 6 days
i, r, e = app.sm2(6, 2, 2.5, 4);  assert (i, r) == (15, 3), (i, r)         # third -> round(6*2.5)
assert abs(e - 2.5) < 1e-9, e                                              # q=4 leaves ease unchanged

# lapse resets reps+interval and lowers ease
i, r, e = app.sm2(15, 3, 2.5, 0)
assert (i, r) == (1, 0), (i, r)
assert abs(e - 1.7) < 1e-9, e                                             # 2.5 + (0.1 - 5*(0.08+5*0.02)) = 1.7

# ease never drops below 1.3
i, r, e = app.sm2(1, 0, 1.3, 0);  assert e == 1.3, e

print("test_cards OK")
```

Run it, watch it FAIL (`AttributeError: module 'app' has no attribute 'sm2'`).

- [ ] **Step 3 (GREEN): Implement in `app.py`** (new section after `quiz_get`, `app.py:829`)

```python
# --- flashcards / spaced repetition (SM-2) ---

_GRADE_Q = {"again": 0, "hard": 3, "good": 4, "easy": 5}

def sm2(interval, reps, ease, quality):
    """Classic SuperMemo-2 update. Returns (interval_days, reps, ease).
    quality<3 is a lapse (reset reps+interval); ease floors at 1.3."""
    if quality < 3:
        reps, interval = 0, 1
    else:
        interval = 1 if reps == 0 else 6 if reps == 1 else round(interval * ease)
        reps += 1
    ease = max(1.3, ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))
    return interval, reps, ease


def generate_cards(rows, n=15):
    """One Claude call: front/back flashcards from filed-note summaries.
    Mirrors generate_quiz. Raises on unparseable JSON (caller surfaces it)."""
    material = "\n\n".join(
        f"## {r.get('topic') or r.get('title') or ''}\n{r.get('summary') or ''}"
        for r in rows if (r.get("summary") or "").strip())
    msg = claude.messages.create(
        model="claude-haiku-4-5", max_tokens=4000,
        messages=[{"role": "user", "content": (
            f"Create {n} study flashcards from these college lecture notes. "
            "Return ONLY a JSON array; each item is "
            '{"front":"a term or question","back":"the answer, 1-2 sentences"}. '
            "Cover the breadth of the material; keep each side concise.\n\n"
            "Notes:\n" + material)}],
    )
    raw = re.sub(r"^```(?:json)?|```$", "", msg.content[0].text.strip(), flags=re.MULTILINE).strip()
    return json.loads(raw)


@app.post("/cards/generate")
def cards_generate(payload: dict = Body(...)):
    sem = (payload.get("semester") or "").strip()
    cls = (payload.get("class") or "").strip()
    unit = (payload.get("unit") or "").strip()
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
        cards = generate_cards(rows)
    except Exception as e:
        return {"error": f"generation failed: {e}"}
    sb.table("cards").insert([
        {"semester": sem or None, "class": cls, "unit": unit or None,
         "front": c.get("front", ""), "back": c.get("back", "")}
        for c in cards if c.get("front") and c.get("back")
    ]).execute()
    return {"count": len(cards)}


@app.get("/cards/due")
def cards_due():
    """Cards whose due_at has passed, soonest first — the review queue."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return (sb.table("cards").select("*").lte("due_at", now)
            .order("due_at").execute().data)


@app.post("/cards/{cid}/review")
def cards_review(cid: str, payload: dict = Body(...)):
    quality = _GRADE_Q.get(payload.get("grade"), 4)
    card = sb.table("cards").select("interval,reps,ease").eq("id", cid).single().execute().data
    interval, reps, ease = sm2(card["interval"] or 0, card["reps"] or 0,
                               float(card["ease"] or 2.5), quality)
    due = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(days=interval)).isoformat()
    sb.table("cards").update(
        {"interval": interval, "reps": reps, "ease": ease, "due_at": due}
    ).eq("id", cid).execute()
    return {"due_at": due, "interval": interval}
```

(`re`, `json`, `datetime`, `Body` are already imported at the top of `app.py`.)

- [ ] **Step 4: Run `test_cards.py`, confirm it PASSES.** Then run `test_quiz.py`, `test_hw.py`, `test_yt.py` to confirm no regression, and `python -c "import app"` to confirm the module imports.

- [ ] **Step 5: Commit**

```
git add schema.sql app.py test_cards.py
git commit -m "$(cat <<'EOF'
feat: flashcard backend + SM-2 spaced-repetition scheduler

cards table, generate/due/review endpoints, pure sm2() with test
covering interval growth, lapse reset, and the 1.3 ease floor.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Flashcard review UI  (feature 1, frontend) — depends on Task 1

**Files:**
- Modify: `index.html` (new `<section id="review">` in the side column next to `#practice` at `:455`; a `#reviewArea` sibling of `#quizArea` at `:424`; JS near the practice-quiz block at `:905`)

**Interfaces:**
- Consumes: `GET /cards/due`, `POST /cards/{id}/review` (Task 1). `esc`/`escAttr` helpers already exist.
- The review loop lives in `#reviewArea` (which `refresh()` never rewrites — same rule as `#quizArea`). The side-column card (due count + Start) is focus-guarded and re-rendered from `refresh()`.

- [ ] **Step 1: Add `#reviewArea`** next to `#quizArea` (`index.html:424`):
```html
<div id="reviewArea"></div>
```

- [ ] **Step 2: Add the Review section** in the side column, right after the `#practice` section (`:467`):
```html
<section class="folders" id="review">
  <span class="deck-label">Review</span>
  <div class="merge-manual">
    <select id="rClass" onchange="rClassChanged()"></select>
    <select id="rUnit"></select>
    <button onclick="genCards(this)">Make cards</button>
  </div>
  <div id="dueCount"></div>
</section>
```

- [ ] **Step 3: Add JS** near the practice block (`:905`). `renderReview(rows)` is called from `refresh()` (add that call next to `renderPractice(rows)` at `:758`). Populate the class/unit selects the same way `renderPractice`/`pClassChanged` do. Fetch `/cards/due` **guarded** (try/catch, `Array.isArray`) so a failure never throws inside refresh:

```javascript
// ---- flashcard review: due count re-renders on refresh (focus-guarded);
// ---- the review session lives in #reviewArea, which refresh() never touches.
let cardRows = [];
async function renderReview(rows) {
  const card = document.getElementById('review');
  if (document.activeElement && card.contains(document.activeElement)) return;
  const classes = [...new Set(rows.filter(r => r.status === 'done' && r.class).map(r => r.class))].sort();
  const sel = document.getElementById('rClass'), cur = sel.value;
  sel.innerHTML = classes.map(c => `<option value="${escAttr(c)}">${esc(c)}</option>`).join('');
  if (classes.includes(cur)) sel.value = cur;
  cardRows = rows; rClassChanged();
  let due = [];
  try { const d = await (await fetch('/cards/due')).json(); if (Array.isArray(d)) due = d; } catch {}
  const el = document.getElementById('dueCount');
  el.innerHTML = due.length
    ? `<button class="submit" onclick='startReview(${JSON.stringify(due).replace(/'/g, "&#39;")})'>Review ${due.length} due</button>`
    : '<p class="gs-note">No cards due.</p>';
}
function rClassChanged() {
  const cls = document.getElementById('rClass').value;
  const units = [...new Set(cardRows.filter(r => r.status === 'done' && r.class === cls && r.unit).map(r => r.unit))].sort();
  document.getElementById('rUnit').innerHTML = '<option value="">whole class</option>'
    + units.map(u => `<option value="${escAttr(u)}">${esc(u)}</option>`).join('');
}
async function genCards(btn) {
  const body = { class: document.getElementById('rClass').value, unit: document.getElementById('rUnit').value };
  if (!body.class) { alert('record and file some lectures first'); return; }
  btn.disabled = true; btn.textContent = 'making…';
  try {
    const r = await (await fetch('/cards/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })).json();
    alert(r.error ? r.error : `Made ${r.count} cards.`);
  } finally { btn.disabled = false; btn.textContent = 'Make cards'; document.activeElement?.blur(); }
}
let reviewQueue = [];
function startReview(due) { reviewQueue = due; showCard(); }
function showCard() {
  const area = document.getElementById('reviewArea');
  if (!reviewQueue.length) { area.innerHTML = '<div class="quiz"><b>Done reviewing.</b> <button onclick="closeReview()">✕</button></div>'; return; }
  const c = reviewQueue[0];
  area.innerHTML = `<div class="quiz" data-cid="${c.id}">
    <div class="quiz-head"><b>${esc(c.class || '')}${c.unit ? ' / ' + esc(c.unit) : ''}</b>
      <button onclick="closeReview()" title="Close">✕</button></div>
    <div class="q"><p>${esc(c.front)}</p>
      <p class="cardback" style="display:none">${esc(c.back)}</p></div>
    <button class="submit" id="flipBtn" onclick="flipCard()">Show answer</button>
    <div id="grades" style="display:none">
      ${['again', 'hard', 'good', 'easy'].map(g => `<button class="submit" onclick="gradeCard('${g}')">${g}</button>`).join(' ')}
    </div>
  </div>`;
  document.getElementById('reviewArea').scrollIntoView({ behavior: 'smooth' });
}
function flipCard() {
  document.querySelector('#reviewArea .cardback').style.display = '';
  document.getElementById('flipBtn').style.display = 'none';
  document.getElementById('grades').style.display = '';
}
async function gradeCard(grade) {
  const cid = document.querySelector('#reviewArea .quiz').dataset.cid;
  try {
    await fetch(`/cards/${cid}/review`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ grade }),
    });
  } catch {}
  reviewQueue.shift(); showCard();
}
function closeReview() { document.getElementById('reviewArea').innerHTML = ''; reviewQueue = []; }
```

- [ ] **Step 4: Wire `renderReview` into `refresh()`** — next to the existing `renderPractice(rows);` call (`index.html:758`), add `renderReview(rows);`.

- [ ] **Step 5: Sanity check** — no dedicated test (pure UI). Run `python -c "import app"` (unchanged; still imports). Confirm the JSON.stringify-into-onclick escaping handles a card `back` containing quotes/apostrophes without breaking the handler (the `.replace(/'/g, "&#39;")` guards the attribute).

- [ ] **Step 6: Commit**
```
git add index.html
git commit -m "$(cat <<'EOF'
feat: flashcard review UI — make cards, flip, SM-2 grade

Review section (due count, focus-guarded) + flip-card session in
#reviewArea (survives the 5s refresh). Due-count fetch is guarded so
a failure never blanks the list.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Cheat-sheet export  (feature 4) — independent

**Files:**
- Modify: `app.py` (new endpoint + pure builder, place after `cards_review` / the flashcard section)
- Modify: `index.html` (a download link in the `#practice` or `#review` section)
- Test: `test_cheatsheet.py` (create) — TDD the pure builder

**Interfaces:**
- Produces: pure `build_cheatsheet(rows, cls, unit)` → markdown string; `GET /cheatsheet?class=&unit=&semester=` → `text/markdown` attachment download.
- Markdown ONLY — no PDF library. The user prints to PDF from the browser.

- [ ] **Step 1 (RED): `test_cheatsheet.py`** — assert the builder assembles one section per row from summaries, headed by class/unit, and tolerates blank summaries:
```python
import app
rows = [
    {"topic": "Limits", "summary": "A limit is the value f approaches."},
    {"topic": "Derivatives", "summary": "Rate of change."},
    {"topic": "Empty", "summary": ""},          # skipped
]
md = app.build_cheatsheet(rows, "Calc I", "Unit 1")
assert md.startswith("# Calc I"), md[:40]
assert "Unit 1" in md
assert "## Limits" in md and "approaches" in md
assert "## Derivatives" in md
assert "## Empty" not in md                      # blank summary omitted
print("test_cheatsheet OK")
```
Run, watch it FAIL.

- [ ] **Step 2 (GREEN): implement** in `app.py`:
```python
# --- cheat-sheet export (one-page markdown) ---

def build_cheatsheet(rows, cls, unit):
    """One markdown study sheet from filed-note summaries. Pure/testable."""
    head = f"# {cls}" + (f" — {unit}" if unit else "")
    body = "\n\n".join(
        f"## {r.get('topic') or r.get('title') or 'Untitled'}\n\n{(r.get('summary') or '').strip()}"
        for r in rows if (r.get("summary") or "").strip())
    return f"{head}\n\n{body}\n"


@app.get("/cheatsheet")
def cheatsheet(**_):
    from fastapi import Request  # not needed; see query params below
```
Then replace the endpoint stub with a real query-param handler that mirrors `quiz_generate`'s scoping (accept `class`, `unit`, `semester` as query params via function args), returns 200 with the markdown as a `text/markdown` attachment, or a plain-text message if no notes:
```python
@app.get("/cheatsheet")
def cheatsheet(**params):
    cls = (params.get("class") or "").strip()
    sem = (params.get("semester") or "").strip()
    unit = (params.get("unit") or "").strip()
    if not cls:
        return Response("class required", media_type="text/plain", status_code=400)
    q = (sb.table("recordings").select("topic,title,summary")
         .eq("status", "done").eq("class", cls))
    if sem:
        q = q.eq("semester", sem)
    if unit:
        q = q.eq("unit", unit)
    rows = q.execute().data
    md = build_cheatsheet(rows, cls, unit)
    fname = _slug(f"{cls} {unit}".strip()) + " cheatsheet.md"
    return Response(md, media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})
```
> **NOTE for the agent:** FastAPI does not populate `**params` from the query string. Use explicit typed params instead: `def cheatsheet(class_: str = Query("", alias="class"), unit: str = "", semester: str = ""):` — import `Query` from `fastapi` if not already imported (`from fastapi import Query`), and read the existing import line at the top of `app.py` first. Verify with `python -c "import app"` that the route registers. The `build_cheatsheet` pure function and its test are the contract; the endpoint wiring is yours to make correct against the installed FastAPI version.

- [ ] **Step 3: UI** — add a cheat-sheet download link in the `#review` (or `#practice`) section that hits `/cheatsheet` with the selected class/unit. Simplest: a small button that builds the URL and opens it:
```html
<button onclick="dlCheat()">Cheat sheet</button>
```
```javascript
function dlCheat() {
  const cls = document.getElementById('rClass').value;
  const unit = document.getElementById('rUnit').value;
  if (!cls) { alert('pick a class'); return; }
  const qs = new URLSearchParams({ class: cls, unit }).toString();
  window.location = '/cheatsheet?' + qs;
}
```
(Place the button + `dlCheat` alongside the Review controls; reuse `rClass`/`rUnit`. If Task 2 has not landed yet in your branch, use `pClass`/`pUnit` from the Practice section instead — read the file to see which selects exist.)

- [ ] **Step 4: Run `test_cheatsheet.py` (PASS), `python -c "import app"`, and the other `test_*.py` (no regression).**

- [ ] **Step 5: Commit**
```
git add app.py index.html test_cheatsheet.py
git commit -m "$(cat <<'EOF'
feat: cheat-sheet export — one-page markdown per class/unit

build_cheatsheet() assembles filed summaries into a printable sheet;
GET /cheatsheet downloads it. Markdown only (browser prints to PDF).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Cards-due calendar reminders  (feature 5) — depends on Task 1

**Files:**
- Modify: `app.py` (new endpoint + pure builder, mirroring `assignments_ics` at `:660`)
- Modify: `index.html` (a `.ics` download link in the `#review` section)
- Test: `test_cards_ics.py` (create) — TDD the pure `.ics` builder

**Interfaces:**
- Produces: pure `build_cards_ics(due_dates, stamp)` → VCALENDAR string with one all-day VEVENT per distinct future due-date, `SUMMARY:"N flashcards due"`; `GET /cards_due.ics` → `text/calendar` download.
- Reuses `_ics_escape` (`app.py:656`).

- [ ] **Step 1 (RED): `test_cards_ics.py`** — group by date, one VEVENT per date with the count, stable UID per date so re-import updates rather than duplicates:
```python
import app
ics = app.build_cards_ics(["2026-07-10", "2026-07-10", "2026-07-12"], "20260708T000000Z")
assert "BEGIN:VCALENDAR" in ics and ics.rstrip().endswith("END:VCALENDAR")
assert ics.count("BEGIN:VEVENT") == 2                       # two distinct dates
assert "SUMMARY:2 flashcards due" in ics                    # 2026-07-10 bucket
assert "SUMMARY:1 flashcard due" in ics                     # 2026-07-12 bucket (singular)
assert "DTSTART;VALUE=DATE:20260710" in ics
assert "UID:cards-2026-07-10@listen" in ics                 # stable per-date UID
print("test_cards_ics OK")
```
Run, watch it FAIL.

- [ ] **Step 2 (GREEN): implement** in `app.py` (after the flashcard section):
```python
def build_cards_ics(due_dates, stamp):
    """All-day VEVENTs, one per distinct due-date, summarising how many cards
    are due. Pure/testable. due_dates: list of 'YYYY-MM-DD' strings."""
    from collections import Counter
    counts = Counter(d for d in due_dates if d)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//listen//EN"]
    for date in sorted(counts):
        n = counts[date]
        noun = "flashcard" if n == 1 else "flashcards"
        lines += [
            "BEGIN:VEVENT",
            f"UID:cards-{date}@listen",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{date.replace('-', '')}",
            f"SUMMARY:{_ics_escape(f'{n} {noun} due')}",
            "CATEGORIES:flashcards",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@app.get("/cards_due.ics")
def cards_due_ics():
    """Upcoming flashcard due-dates as a calendar feed (today onward)."""
    today = datetime.date.today().isoformat()
    rows = (sb.table("cards").select("due_at").gte("due_at", today)
            .execute().data)
    dates = [(r.get("due_at") or "")[:10] for r in rows]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Response(build_cards_ics(dates, stamp), media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="cards_due.ics"'})
```

- [ ] **Step 3: UI** — add a `.ics` link in the `#review` section (mirrors the syllabus `.ics` link at `index.html:750`):
```html
<p><a href="/cards_due.ics">&#128197; Flashcard due dates (.ics)</a></p>
```
Place it inside the `#review` section (static link, no JS needed). If Task 2 has not landed in your branch, add a minimal `#review` section or place the link in `#practice`; read the file first to see what exists.

- [ ] **Step 4: Run `test_cards_ics.py` (PASS), `python -c "import app"`, other `test_*.py` (no regression).**

- [ ] **Step 5: Commit**
```
git add app.py index.html test_cards_ics.py
git commit -m "$(cat <<'EOF'
feat: flashcard due-date calendar feed (.ics)

build_cards_ics() groups upcoming card due-dates into all-day events
(one per date, stable UID); GET /cards_due.ics downloads the feed.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Orchestrator steps (NOT delegated — done by the main thread)

1. **After Task 1 verified:** apply the `cards` DDL to the live DB via the Supabase MCP (`apply_migration`, project `trjormdredokdxzpwdgc`) — the exact `create table if not exists cards (...)` + `enable row level security` from Task 1 Step 1. Idempotent.
2. **After all 4 tasks:** run every `test_*.py`, review `git diff master...feat/study-tools-v2`, and run the cross-task audit (below).
3. **Audit lens (the real risk is interaction, not per-task correctness):**
   - Any refresh-path fetch (`/cards/due` in `renderReview`) guarded so it can't blank the list.
   - `renderReview` wired into `refresh()` without breaking `renderPractice` (both focus-guarded, both re-render side-column cards).
   - `cards` table coexists with existing tables; new endpoints don't collide with existing routes.
   - Cheat-sheet endpoint param binding actually works against the installed FastAPI (the agent had to adapt the `**params` stub).
   - SM-2 `ease` stored as numeric round-trips through Supabase without precision drift breaking the floor.
4. **Then:** merge `feat/study-tools-v2` → master, remove the worktree (`git worktree remove --force` — `.env` lives in it), delete the branch, update the memory handoff.
