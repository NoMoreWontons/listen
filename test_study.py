"""POST /study/generate: scope filtering + dispatch to quiz/cards/cheatsheet.
Stubs app.sb with a tiny in-memory fake and app.claude with a canned reply —
no real Supabase/network. Run: python test_study.py"""
import os

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app


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
        return FakeQuery([], store=self.quizzes if name == "quizzes" else self.cards)


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


def test_flashcards():
    app.sb = FakeSB([mkrow("Cells", "Mitosis")])
    app.claude.messages.create = fake_cards_msg
    out = app.study_generate({"kind": "flashcards", "class": "Biology", "scopes": [{"unit": "Cells"}]})
    assert out == {"count": 1}, out
    assert app.sb.cards[0]["unit"] == "Cells"
    print("ok: flashcards -> generate+insert path, count returned")


def test_cheatsheet():
    app.sb = FakeSB([mkrow("Cells", "Mitosis", summary="Mitosis is cell division.")])
    out = app.study_generate({"kind": "cheatsheet", "class": "Biology", "scopes": [{"unit": "Cells"}]})
    assert out["filename"] == "Biology Cells cheatsheet.md", out
    assert "# Biology" in out["markdown"] and "Mitosis is cell division." in out["markdown"], out
    print("ok: cheatsheet returns filename + markdown")


if __name__ == "__main__":
    test_scope_rows()
    test_validation_errors()
    test_no_summaries_error()
    test_quiz_single_unit()
    test_quiz_mixed_units_null()
    test_flashcards()
    test_cheatsheet()
    print("test_study: OK")
