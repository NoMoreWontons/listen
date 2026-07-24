"""_parse_segments JSON parsing (1-seg / 2-seg / fallback) + the keep-as-one
summary concatenation. Pure — no network, no DB. Run: python test_split.py"""
import os

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app


def test_parse_segments_one():
    raw = '{"segments":[{"class":"Bio","unit":"Cells","topic":"Mitosis","summary":"- pts"}]}'
    segs = app._parse_segments(raw)
    assert segs == [{"class": "Bio", "unit": "Cells", "topic": "Mitosis", "summary": "- pts"}], segs
    print("ok: single-segment JSON parses")


def test_parse_segments_two():
    raw = (
        '{"segments":['
        '{"class":"Calc","unit":"Derivatives","topic":"Inverse fn derivatives","summary":"- a"},'
        '{"class":"Calc","unit":"Derivatives","topic":"Applications","summary":"- b"}'
        ']}'
    )
    segs = app._parse_segments(raw)
    assert len(segs) == 2, segs
    assert segs[0]["topic"] == "Inverse fn derivatives", segs
    assert segs[1]["topic"] == "Applications", segs
    print("ok: two-segment JSON parses")


def test_parse_segments_fallback():
    # unparseable JSON -> one segment, raw text as summary, Unsorted/Untitled labels
    segs = app._parse_segments("not json at all")
    assert segs == [{"class": "Unsorted", "unit": "Unsorted", "topic": "Untitled",
                      "summary": "not json at all"}], segs

    # valid JSON but no/empty segments -> same fallback
    segs = app._parse_segments('{"segments":[]}')
    assert segs[0]["class"] == "Unsorted" and segs[0]["summary"] == '{"segments":[]}', segs
    print("ok: unparseable/empty JSON falls back to one Unsorted segment")


def test_parse_segments_trailing_prose():
    # valid JSON object followed by trailing prose the model appended after
    # the closing brace -> parses the JSON, does NOT fall back to Unsorted,
    # and LaTeX braces in the summary survive intact.
    raw = ('{"segments":[{"class":"Physics","unit":"Work and Energy",'
           '"topic":"Work, energy, and power","summary":"$\\\\frac{1}{2}mv^2$"}]}'
           '\n\n---\n\n**Note on transcript quality:** garbled.')
    segs = app._parse_segments(raw)
    assert segs == [{"class": "Physics", "unit": "Work and Energy",
                      "topic": "Work, energy, and power",
                      "summary": "$\\frac{1}{2}mv^2$"}], segs
    print("ok: trailing prose after valid JSON doesn't break segment parsing")


def test_parse_exams_trailing_prose():
    raw = ('{"segments":[{"class":"Physics","unit":"Work and Energy",'
           '"topic":"Work, energy, and power","summary":"$\\\\frac{1}{2}mv^2$"}],'
           '"exams":[{"title":"Midterm","due_date":"2026-08-01"}]}'
           '\n\n---\n\n**Note on transcript quality:** garbled.')
    exams = app._parse_exams(raw)
    assert exams == [{"title": "Midterm", "due_date": "2026-08-01"}], exams
    print("ok: trailing prose after valid JSON doesn't break exams parsing")


def test_segments_summary_concat():
    segments = [
        {"topic": "Inverse fn derivatives", "summary": "- point a"},
        {"topic": "Applications", "summary": "- point b"},
    ]
    out = app._segments_summary(segments)
    assert out == "## Inverse fn derivatives\n\n- point a\n\n## Applications\n\n- point b", out
    print("ok: keep-as-one summary concatenates segments under topic headers")


if __name__ == "__main__":
    test_parse_segments_one()
    test_parse_segments_two()
    test_parse_segments_fallback()
    test_parse_segments_trailing_prose()
    test_parse_exams_trailing_prose()
    test_segments_summary_concat()
