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
