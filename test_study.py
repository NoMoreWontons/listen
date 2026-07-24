"""POST /study/generate: scope filtering + dispatch to quiz/cards/cheatsheet.
Stubs app.sb with a tiny in-memory fake and app.claude with a canned reply —
no real Supabase/network. Run: python test_study.py"""
import os
import pathlib
import tempfile

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app

# study_generate files notes as a side effect (quizzes included, since they're
# written at generation) — pin the vault to a throwaway dir so a test that
# doesn't care about paths can't write into the real one.
app.OBSIDIAN_VAULT = pathlib.Path(tempfile.mkdtemp(prefix="listen-test-vault-"))


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    """Chainable stand-in covering select/eq/execute (recordings) and
    insert/execute (quizzes, cards)."""
    def __init__(self, rows, store=None):
        self.rows = rows
        self.store = store
        self.filters = []
        self.mode = "select"
        self.payload = None

    def select(self, *a, **k):
        return self

    def eq(self, k, v):
        self.filters.append((k, v))
        return self

    def insert(self, payload):
        self.mode, self.payload = "insert", payload
        return self

    def execute(self):
        if self.mode == "insert":
            rows = self.payload if isinstance(self.payload, list) else [self.payload]
            inserted = [dict(r, id=f"id{i}") for i, r in enumerate(rows)]
            self.store.extend(inserted)
            return FakeResult(inserted)
        matched = [r for r in self.rows if all(r.get(k) == v for k, v in self.filters)]
        return FakeResult(matched)


class FakeSB:
    def __init__(self, rows):
        self.recordings = rows
        self.quizzes = []
        self.cards = []

    def table(self, name):
        if name == "recordings":
            return FakeQuery(self.recordings)
        if name == "cards":  # selectable too — write_cards_note reads the deck back
            return FakeQuery(self.cards, store=self.cards)
        return FakeQuery([], store=self.quizzes)


def mkrow(unit, topic, summary="notes here", cls="Biology", status="done"):
    return {"class": cls, "unit": unit, "topic": topic, "title": topic,
            "summary": summary, "status": status}


class FakeMsg:
    def __init__(self, text):
        self.content = [type("T", (), {"text": text})()]


def fake_quiz_msg(**kw):
    return FakeMsg('[{"type":"mcq","q":"q1","choices":["a","b"],"answer":0,"explanation":""}]')


def fake_cards_msg(**kw):
    return FakeMsg('[{"front":"f1","back":"b1"}]')


# --- _scope_rows: pure, no DB ---

def test_scope_rows():
    rows = [
        mkrow("Cells", "Mitosis"),
        mkrow("Cells", "Meiosis"),
        mkrow("Genetics", "Alleles"),
    ]
    # whole-unit scope
    assert app._scope_rows(rows, [{"unit": "Cells"}]) == rows[:2]
    # topic scope
    assert app._scope_rows(rows, [{"unit": "Cells", "topic": "Mitosis"}]) == [rows[0]]
    # unit scope + a topic scope inside it -> no duplicate rows
    got = app._scope_rows(rows, [{"unit": "Cells"}, {"unit": "Cells", "topic": "Mitosis"}])
    assert got == rows[:2], got
    # no match
    assert app._scope_rows(rows, [{"unit": "Nope"}]) == []
    print("ok: _scope_rows filters by unit/topic and dedupes overlap")


# --- validation errors (no DB rows needed) ---

def test_validation_errors():
    app.sb = FakeSB([])
    assert app.study_generate({"kind": "nope", "class": "Biology", "scopes": [{"unit": "Cells"}]}) \
        == {"error": "bad kind"}
    assert app.study_generate({"kind": "quiz", "class": "", "scopes": [{"unit": "Cells"}]}) \
        == {"error": "class required"}
    assert app.study_generate({"kind": "quiz", "class": "Biology", "scopes": []}) \
        == {"error": "scopes required"}
    assert app.study_generate({"kind": "quiz", "class": "Biology", "scopes": [{"topic": "x"}]}) \
        == {"error": "scopes required"}  # missing unit
    assert app.study_generate({"kind": "quiz", "class": "Biology", "scopes": "nope"}) \
        == {"error": "scopes required"}
    print("ok: bad kind / missing class / empty or malformed scopes rejected")


def test_no_summaries_error():
    app.sb = FakeSB([mkrow("Cells", "Mitosis", summary="")])
    out = app.study_generate({"kind": "quiz", "class": "Biology", "scopes": [{"unit": "Cells"}]})
    assert out == {"error": "no filed notes for that scope yet"}, out
    print("ok: empty summaries -> no filed notes error")


# --- unit single-vs-mixed rule + insert dispatch ---

def test_quiz_single_unit():
    app.sb = FakeSB([mkrow("Cells", "Mitosis"), mkrow("Cells", "Meiosis")])
    app.claude.messages.create = fake_quiz_msg
    out = app.study_generate({"kind": "quiz", "class": "Biology", "scopes": [{"unit": "Cells"}]})
    assert out["unit"] == "Cells", out
    assert out["kind"] == "quiz" and out["class"] == "Biology"
    assert len(app.sb.quizzes) == 1
    print("ok: single-unit scope -> quiz row carries that unit")


def test_quiz_mixed_units_null():
    app.sb = FakeSB([mkrow("Cells", "Mitosis"), mkrow("Genetics", "Alleles")])
    app.claude.messages.create = fake_quiz_msg
    out = app.study_generate({
        "kind": "test", "class": "Biology",
        "scopes": [{"unit": "Cells"}, {"unit": "Genetics"}],
    })
    assert out["unit"] is None, out
    assert out["kind"] == "test"
    print("ok: mixed-unit scopes -> unit null")


def test_quiz_format_passthrough():
    """The tree right-click menu sends format like the Practice builder does —
    it has to reach generate_quiz's prompt, and anything else must fall to mcq."""
    seen = {}

    def spy(**kw):
        seen["prompt"] = kw["messages"][0]["content"]
        return fake_quiz_msg(**kw)

    app.claude.messages.create = spy
    for fmt, want in [("frq", "free-response"), ("mcq", "multiple-choice"),
                      (None, "multiple-choice"), ("bogus", "multiple-choice")]:
        app.sb = FakeSB([mkrow("Cells", "Mitosis")])
        body = {"kind": "quiz", "class": "Biology", "scopes": [{"unit": "Cells"}]}
        if fmt is not None:
            body["format"] = fmt
        app.study_generate(body)
        assert want in seen["prompt"], (fmt, seen["prompt"][:200])
    print("ok: format reaches the quiz prompt, bad/absent format -> mcq")


def test_flashcards():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        app.sb = FakeSB([mkrow("Cells", "Mitosis")])
        app.claude.messages.create = fake_cards_msg
        out = app.study_generate({"kind": "flashcards", "class": "Biology", "scopes": [{"unit": "Cells"}]})
        assert out["count"] == 1, out
        assert app.sb.cards[0]["unit"] == "Cells"
        # deck snapshot lands in the unit's Exam Prep folder, alongside quizzes/exams
        p = pathlib.Path(out["path"])
        assert p == pathlib.Path(d) / "Untitled" / "Biology" / "Cells" / app.PREP_DIR / "Flashcards.md", p
        assert "**f1** — b1" in p.read_text(encoding="utf-8")
    print("ok: flashcards -> generate+insert, deck note filed under Exam Prep")


def test_cheatsheet():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        app.sb = FakeSB([mkrow("Cells", "Mitosis", summary="Mitosis is cell division.")])
        out = app.study_generate({"kind": "cheatsheet", "class": "Biology", "scopes": [{"unit": "Cells"}]})
        assert out["filename"] == "Biology Cells cheatsheet.md", out
        assert "# Biology" in out["markdown"] and "Mitosis is cell division." in out["markdown"], out
        p = pathlib.Path(out["path"])
        assert p == (pathlib.Path(d) / "Untitled" / "Biology" / "Cells" / app.PREP_DIR
                     / "Biology Cells cheatsheet.md"), p
        assert p.read_text(encoding="utf-8") == out["markdown"]
        # in-app view opens the same note in Obsidian
        assert out["obsidian"].startswith("obsidian://open"), out
    print("ok: cheatsheet returns markdown and files a copy under Exam Prep")


def test_quiz_files_ungraded():
    """A generated-but-not-yet-graded quiz/test still lands under Exam Prep,
    marked ungraded — matching cheatsheets/flashcards, which file at generation.
    Grading later overwrites the same file (stable name from created_at)."""
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        app.sb = FakeSB([mkrow("Cells", "Mitosis", summary="Mitosis is cell division.")])
        app.claude.messages.create = fake_quiz_msg
        out = app.study_generate({"kind": "test", "class": "Biology", "scopes": [{"unit": "Cells"}]})
        assert out["kind"] == "test", out
        notes = list(pathlib.Path(d).rglob("test *.md"))
        assert len(notes) == 1, notes
        assert notes[0].parent.name == app.PREP_DIR, notes[0]
        assert "ungraded" in notes[0].read_text(encoding="utf-8")
    print("ok: generated-but-ungraded test files under Exam Prep")


def test_semester_falls_back_to_rows():
    """'All semesters' (the scope selectors' default) sends semester='' — the
    notes must still file under the real semester, not a root 'Untitled'."""
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        rows = [dict(mkrow("Cells", "Mitosis", summary="Mitosis is cell division."),
                     semester="Fall 26")]
        app.sb = FakeSB(rows)
        out = app.study_generate({"kind": "cheatsheet", "class": "Biology",
                                  "scopes": [{"unit": "Cells"}]})
        assert pathlib.Path(out["path"]).parent == (
            pathlib.Path(d) / "Fall 26" / "Biology" / "Cells" / app.PREP_DIR), out["path"]

        app.sb = FakeSB(rows)
        app.claude.messages.create = fake_quiz_msg
        q = app.study_generate({"kind": "quiz", "class": "Biology", "scopes": [{"unit": "Cells"}]})
        assert q["semester"] == "Fall 26", q  # quiz row too — write_quiz_note reads it back
    print("ok: blank semester resolves from the scope's rows")


def test_migrate_prep_dirs():
    """Old Practice folders get renamed; a pre-existing Exam Prep folder absorbs
    them instead of leaving two side by side."""
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        plain = pathlib.Path(d) / "Fall 26" / "Biology" / "Cells" / "Practice"
        plain.mkdir(parents=True)
        (plain / "quiz.md").write_text("q", encoding="utf-8")
        clash = pathlib.Path(d) / "Fall 26" / "Physics" / "Practice"
        clash.mkdir(parents=True)
        (clash / "Midterm 1.md").write_text("m", encoding="utf-8")
        (clash.with_name(app.PREP_DIR)).mkdir()

        app._migrate_prep_dirs()

        assert not plain.exists() and not clash.exists()
        assert (plain.with_name(app.PREP_DIR) / "quiz.md").read_text(encoding="utf-8") == "q"
        assert (clash.with_name(app.PREP_DIR) / "Midterm 1.md").read_text(encoding="utf-8") == "m"
        app._migrate_prep_dirs()  # idempotent
    print("ok: Practice folders migrate to Exam Prep, merging into an existing one")


if __name__ == "__main__":
    test_scope_rows()
    test_validation_errors()
    test_no_summaries_error()
    test_quiz_single_unit()
    test_quiz_mixed_units_null()
    test_quiz_format_passthrough()
    test_flashcards()
    test_cheatsheet()
    test_quiz_files_ungraded()
    test_semester_falls_back_to_rows()
    test_migrate_prep_dirs()
    print("test_study: OK")
