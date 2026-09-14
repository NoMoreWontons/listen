"""/ask used to send every filed summary in the library to Sonnet in one prompt:
class was an optional filter, so a blank picker meant "the whole vault". These
check the scope guard and the cap that keeps one long class from doing the same.
Run: python test_ask_scope.py
"""
import os

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app


def row(topic, summary, unit=""):
    return {"topic": topic, "unit": unit, "summary": summary}


def test_class_is_required():
    assert app.ask_notes({"question": "what is a dot product?"}) == {"error": "class required"}
    assert app.ask_notes({"question": "x", "semester": "Fall 26"}) == {"error": "class required"}
    # an empty question still loses to its own guard, not this one
    assert app.ask_notes({"question": "  ", "class": "Physics"}) == {"error": "question required"}
    print("ok: /ask refuses to answer across every class at once")


def test_scope_keeps_everything_under_the_cap():
    rows = [row(f"t{i}", "summary " + str(i)) for i in range(app.ASK_NOTES_CAP)]
    assert app._ask_scope("anything", rows) is rows, "no cap, no reordering, no copying"
    print("ok: a class under the cap is passed through untouched")


def test_scope_picks_the_notes_that_cover_the_question():
    filler = [row(f"filler {i}", "unrelated material about scheduling and logistics")
              for i in range(app.ASK_NOTES_CAP + 10)]
    wanted = row("Cross product", "The cross product of two vectors is orthogonal to both")
    picked = app._ask_scope("how does the cross product work?", filler + [wanted])
    assert len(picked) == app.ASK_NOTES_CAP
    assert wanted in picked, "the one note that answers the question must survive the cap"

    # ...and the survivors stay in the order they arrived (newest-first from the query)
    assert picked == [r for r in filler + [wanted] if r in picked]
    print("ok: the cap keeps the notes covering the question, in their original order")


def test_scope_without_usable_question_words_takes_the_recent_ones():
    rows = [row(f"t{i}", f"summary {i}") for i in range(app.ASK_NOTES_CAP + 5)]
    picked = app._ask_scope("?? a", rows)   # every word too short to survive _norm_words
    assert picked == rows[:app.ASK_NOTES_CAP], "falls back to newest-first, not an arbitrary slice"
    print("ok: a question with no usable words falls back to the most recent notes")


def test_ask_notes_actually_applies_the_cap():
    """The cap is only worth anything if /ask routes through it -- the prompt is
    what reaches Sonnet, so count the sections in the prompt, not in _ask_scope."""
    rows = [{"semester": "Fall 26", "class": "Physics", "unit": "u", "topic": f"t{i}",
             "summary": f"summary {i}", "obsidian_path": ""}
            for i in range(app.ASK_NOTES_CAP + 25)]

    class Q:
        def select(self, *a, **k): return self
        def eq(self, *a): return self
        def execute(self): return type("R", (), {"data": rows})()

    class SB:
        def table(self, name): return Q()

    sent = {}

    class Claude:
        class messages:
            @staticmethod
            def create(**kw):
                sent["prompt"] = kw["messages"][0]["content"]
                raise RuntimeError("stop here -- the prompt is all we need")

    app.sb, app.claude = SB(), Claude()
    out = app.ask_notes({"question": "explain summary 3", "class": "Physics"})
    assert "error" in out, out                       # the fake model raised, on purpose
    assert sent["prompt"].count("\n## ") == app.ASK_NOTES_CAP, \
        f'{sent["prompt"].count(chr(10) + "## ")} notes reached the model, cap is {app.ASK_NOTES_CAP}'
    print("ok: /ask sends at most ASK_NOTES_CAP notes to the model")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
