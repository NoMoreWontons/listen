"""Smoke check: _norm_sem folds the four-digit spelling of a term onto the
two-digit one _semester() mints, so a syllabus that says "Fall 2026" and a
recording dated in the fall can't mint two semester folders for one term.
Pure function — no app import needed beyond the module."""
import os

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app


def test_norm_sem():
    # the bug: Claude read "Fall 2026" off a syllabus, _semester() mints "Fall 26"
    assert app._norm_sem("Fall 2026") == "Fall 26"
    assert app._norm_sem("Spring 2027") == "Spring 27"
    assert app._norm_sem("Summer 2026") == "Summer 26"
    assert app._norm_sem("Winter 2026") == "Winter 26"
    # already normal, and _semester()'s own output, stay put
    assert app._norm_sem("Fall 26") == "Fall 26"
    assert app._norm_sem(app._semester("2026-09-01T00:00:00")) == "Fall 26"
    # anything that isn't <Term> <4-digit year> passes through — SEMESTER_OVERRIDE
    # is free-form, and "Bridge" is a real semester folder in the vault
    assert app._norm_sem("Bridge") == "Bridge"
    assert app._norm_sem("") == ""
    assert app._norm_sem(None) == ""
    # a bare year is not a term and must not be rewritten
    assert app._norm_sem("2026") == "2026"


if __name__ == "__main__":
    test_norm_sem()
    print("ok")
