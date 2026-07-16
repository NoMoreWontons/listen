"""grade_mcq scoring math, quiz/FRQ generation, FRQ grading, and the Obsidian
practice note. Stubs app.claude for generation/grading calls — no real
Supabase/network. Run: python test_quiz.py"""
import os
import tempfile
import pathlib

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app


def test_grade_mcq():
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
    print("ok: grade_mcq scoring, string-index tolerance, short-answer passthrough")


def test_generate_quiz_mcq():
    captured = {}

    class FakeMsg:
        content = [type("T", (), {
            "text": '[{"type":"mcq","q":"2+2?","choices":["3","4"],"answer":1,"explanation":""}]'})()]

    def fake_create(**kw):
        captured.clear()
        captured.update(kw)
        return FakeMsg()

    app.claude.messages.create = fake_create
    rows = [{"topic": "Math", "summary": "Addition basics."}]

    qs = app.generate_quiz(rows, "quiz", "mcq")
    assert captured["model"] == "claude-haiku-4-5", captured["model"]
    assert "10 multiple-choice questions" in captured["messages"][0]["content"]
    assert qs == [{"type": "mcq", "q": "2+2?", "choices": ["3", "4"], "answer": 1, "explanation": ""}], qs

    assert "Bias toward harder" not in captured["messages"][0]["content"]  # quizzes stay breadth-first
    app.generate_quiz(rows, "test", "mcq")
    assert "20 multiple-choice questions" in captured["messages"][0]["content"]
    assert "Bias toward harder" in captured["messages"][0]["content"]  # tests skew hard
    print("ok: generate_quiz builds mcq prompt/counts (10/20 questions), test kind skews hard")


def test_generate_quiz_frq():
    captured = {}

    class FakeMsg:
        content = [type("T", (), {
            "text": '```json\n[{"type":"frq","q":"Explain X","rubric":"- a\\n- b"}]\n```'})()]

    def fake_create(**kw):
        captured.clear()
        captured.update(kw)
        return FakeMsg()

    app.claude.messages.create = fake_create
    rows = [{"topic": "Physics", "summary": "Newton's laws."}]

    qs = app.generate_quiz(rows, "quiz", "frq")
    assert "4 free-response questions" in captured["messages"][0]["content"]
    assert qs == [{"type": "frq", "q": "Explain X", "rubric": "- a\n- b"}], qs

    app.generate_quiz(rows, "test", "frq")
    assert "8 free-response questions" in captured["messages"][0]["content"]
    print("ok: generate_quiz builds frq prompt/counts (4/8 questions), strips code fences")


def test_grade_frq_parse():
    class FakeMsg:
        content = [type("T", (), {"text": (
            '```json\n[{"i":0,"score":1,"feedback":"Nailed it.","transcription":"F=ma"},'
            '{"i":2,"score":1.5,"feedback":"over","transcription":"x"}]\n```'
        )})()]

    captured = {}

    def fake_create(**kw):
        captured.clear()
        captured.update(kw)
        return FakeMsg()

    app.claude.messages.create = fake_create
    questions = [
        {"type": "frq", "q": "State Newton's 2nd law.", "rubric": "- F=ma"},
        {"type": "frq", "q": "Define momentum.", "rubric": "- p=mv"},
        {"type": "frq", "q": "State Newton's 3rd law.", "rubric": "- equal/opposite"},
    ]
    jpeg = b"\xff\xd8\xff\xe0 fake jpeg bytes"
    results = app.grade_frq(questions, [jpeg])

    assert captured["model"] == "claude-sonnet-5", captured["model"]
    content = captured["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg", content[0]
    assert len(results) == 3, results
    assert results[0] == {"answer": "F=ma", "score": 1.0, "feedback": "Nailed it."}, results[0]
    assert results[1]["score"] == 0.0 and "grading failed" in results[1]["feedback"], results[1]  # missing i=1
    assert results[2]["score"] == 1.0, results[2]  # clamped: model said 1.5
    print("ok: grade_frq (sonnet) aligns results to questions, fills missing index, clamps score")


def test_write_quiz_note():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        quiz = {"semester": "Fall 26", "class": "Biology", "unit": "Cells", "kind": "quiz"}
        questions = [
            {"type": "mcq", "q": "2+2?", "choices": ["3", "4"], "answer": 1, "explanation": ""},
            {"type": "frq", "q": "Explain mitosis.", "rubric": "- phases\n- purpose"},
        ]
        results = [
            {"answer": 1, "correct": True},
            {"answer": "cells divide", "score": 0.5, "feedback": "partial"},
        ]
        path = app.write_quiz_note(quiz, questions, results, 75)
        p = pathlib.Path(path)
        assert p.parent == pathlib.Path(d) / "Fall 26" / "Biology" / "Cells" / "Practice", path
        assert p.name.startswith("quiz ") and p.name.endswith("— 75%.md"), path
        text = p.read_text(encoding="utf-8")
        assert "class: Biology" in text and "kind: quiz" in text, text
        assert "score: 75" in text and "tags: [practice]" in text, text
        assert "### Q1" in text and "2+2?" in text, text
        assert "(correct, your pick)" in text, text  # mcq: correct choice was also the student's pick
        assert "### Q2" in text and "Explain mitosis." in text, text
        assert "**Rubric:** - phases" in text, text
        assert "**Transcribed answer:** cells divide" in text, text
        assert "Score: 0.5 — partial" in text, text
        assert "Class: [[Fall 26/Biology/Biology|Biology]]" in text, text  # graph anchor
    print("ok: write_quiz_note writes frontmatter, mcq/frq bodies, and the graph anchor")


if __name__ == "__main__":
    test_grade_mcq()
    test_generate_quiz_mcq()
    test_generate_quiz_frq()
    test_grade_frq_parse()
    test_write_quiz_note()
