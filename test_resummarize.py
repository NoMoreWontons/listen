"""Re-summarizing one half of a split lecture must not hand it the other half.

A split leaves several rows sharing ONE transcript. Editing the notes on any of
them re-runs analyze() over that whole transcript, so the model proposes every
segment again -- and folding those into a single summary (what /label used to
do) silently undoes the split: the dot-product note grows a cross-product
section and the cross-product note grows a dot-product one. Seen for real on the
2026-09-02 multivariable calculus lecture.

Reuses test_note.py's in-memory Supabase fake -- no network, no Claude.
"""
import os

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app
# the in-memory Supabase fake lives in test_note.py -- shared rather than copied,
# so a change to its query-builder stub can break this file too
from test_note import FakeSB

WHEN = "2026-09-02T19:30:55+00:00"
DOT = "7873ecac"
CROSS = "08b76a08"

SEGS = [{"topic": "Dot product and angle relationships", "summary": "DOT SUMMARY"},
        {"topic": "Cross product in 3D", "summary": "CROSS SUMMARY"}]


def rows(*ids, when=WHEN):
    return [{"id": i, "created_at": when} for i in ids]


def test_split_row_keeps_only_its_own_segment():
    app.sb = FakeSB(rows(DOT, CROSS))
    old = {"created_at": WHEN}
    assert app._resummarized(DOT, old, "Dot product properties and angle", SEGS) == "DOT SUMMARY"
    assert app._resummarized(CROSS, old, "Cross product, determinants, applications", SEGS) == "CROSS SUMMARY"
    print("ok  each half of a split lecture keeps only the segment its topic names")


def test_unsplit_row_still_folds():
    """One row, one note, a lecture that happens to cover two things: folding is
    right there -- there is no sibling note for the second half to live in."""
    app.sb = FakeSB(rows(DOT))
    got = app._resummarized(DOT, {"created_at": WHEN}, "Dot product properties and angle", SEGS)
    assert "DOT SUMMARY" in got and "CROSS SUMMARY" in got, got
    print("ok  a lone row still folds every segment into its one summary")


def test_single_segment_is_untouched():
    app.sb = FakeSB(rows(DOT, CROSS))
    one = [{"topic": "Dot product", "summary": "ONLY"}]
    assert app._resummarized(DOT, {"created_at": WHEN}, "anything", one) == "ONLY"
    print("ok  a single-segment reply is used as is, split or not")


def test_unidentifiable_split_row_keeps_its_summary():
    """None means 'leave the summary alone'. Overwriting a split row with a
    summary covering its sibling's material is worse than a stale one."""
    app.sb = FakeSB(rows(DOT, CROSS))
    got = app._resummarized(DOT, {"created_at": WHEN}, "Photosynthesis", SEGS)
    assert got is None, got
    # ...and the caller must honour that rather than writing None into the row
    import inspect
    src = inspect.getsource(app.label)
    assert "if summary is None:" in src and "_set(rid, summary=summary" in src
    assert src.index("if summary is None:") < src.index("_set(rid, summary=summary"), \
        "the None check has to come before the write"
    print("ok  an unmatched split row keeps the summary it already had")


def test_siblings_are_found_by_created_at():
    """The fake asserts the query shape: eq(created_at) + neq(id). A split child
    is inserted sharing its parent's created_at, which is what makes this work."""
    app.sb = FakeSB(rows(DOT, CROSS, when="2026-01-01T00:00:00+00:00"))
    # same ids, but no row carries WHEN -> no siblings found -> folds
    got = app._resummarized(DOT, {"created_at": WHEN}, "Dot product properties and angle", SEGS)
    assert "CROSS SUMMARY" in got, "a row with no same-time sibling is not a split row"
    print("ok  siblings are matched on created_at, not merely on there being other rows")


if __name__ == "__main__":
    test_split_row_keeps_only_its_own_segment()
    test_unsplit_row_still_folds()
    test_single_segment_is_untouched()
    test_unidentifiable_split_row_keeps_its_summary()
    test_siblings_are_found_by_created_at()
    print("\nall re-summarize checks passed")
