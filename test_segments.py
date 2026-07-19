"""Smoke check: whisper segment timestamps -- the pure _seg_entries shaping/
truncation logic, and the /audio + /segments endpoints. FakeSB copied from
test_note.py, extended with real .single() unwrap semantics (needed here,
unlike test_note.py's callers)."""
import os
import json
import pathlib
import tempfile

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    """Chainable stand-in for supabase-py's query builder. Unlike test_note.py's
    copy, .single() here actually unwraps matched rows to one dict/None --
    get_segments relies on that, and none of test_note's exercised callers do."""
    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.mode = "select"
        self.payload = None
        self.want_single = False

    def select(self, *a, **k):
        return self

    def eq(self, k, v):
        self.filters.append(("eq", k, v))
        return self

    def single(self):
        self.want_single = True
        return self

    def _match(self, r):
        return all(r.get(k) == v for _, k, v in self.filters)

    def execute(self):
        matched = [r for r in self.rows if self._match(r)]
        data = (matched[0] if matched else None) if self.want_single else matched
        return FakeResult(data)


class FakeSB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        assert name == "recordings"
        return FakeQuery(self.rows)


class FakeSeg:
    def __init__(self, s, e, t):
        self.start, self.end, self.text = s, e, t


def test_seg_entries_shapes_and_rounds():
    entries = app._seg_entries([FakeSeg(0.04, 1.06, "  hello world  "), FakeSeg(1.1, 2.0, "next")])
    assert entries == [
        {"s": 0.0, "e": 1.1, "t": "hello world"},
        {"s": 1.1, "e": 2.0, "t": "next"},
    ], entries
    print("ok: _seg_entries rounds timestamps to .1s and strips text")


def test_seg_entries_truncates_past_2000():
    small = [FakeSeg(i, i + 1, "x" * 300) for i in range(2000)]
    capped_small = app._seg_entries(small)
    assert len(capped_small) == 2000 and len(capped_small[0]["t"]) == 300, "at the line, no truncation yet"

    big = [FakeSeg(i, i + 1, "x" * 300) for i in range(2001)]
    capped_big = app._seg_entries(big)
    assert len(capped_big) == 2001, "every entry kept, none dropped"
    assert all(len(e["t"]) == 200 for e in capped_big), "text truncated to 200 chars"
    print("ok: _seg_entries keeps every entry past 2000, truncates text to 200 chars")


def test_get_segments_endpoint():
    rows = [{"id": "r1", "segments": [{"s": 0.0, "e": 1.0, "t": "hi"}]}]
    app.sb = FakeSB(rows)
    assert app.get_segments("r1") == [{"s": 0.0, "e": 1.0, "t": "hi"}]
    assert app.get_segments("r1-missing") == []  # no row at all
    rows.append({"id": "r2", "segments": None})
    assert app.get_segments("r2") == []  # row exists, segments never set
    print("ok: /segments/{rid} returns stored segments, [] when missing/unset")


def test_get_audio_serves_file_or_404():
    with tempfile.TemporaryDirectory() as d:
        app.AUDIO_DIR = pathlib.Path(d)
        path = app.audio_path("r1")
        path.write_bytes(b"fake-webm-bytes")

        resp = app.get_audio("r1")
        assert isinstance(resp, app.FileResponse)
        assert pathlib.Path(resp.path) == path
        assert resp.media_type == "audio/webm"

        missing = app.get_audio("nope")
        assert missing.status_code == 404
        assert json.loads(missing.body)["error"] == "audio expired"
    print("ok: /audio/{rid} serves the file with Range-friendly FileResponse, 404s with JSON when gone")


if __name__ == "__main__":
    test_seg_entries_shapes_and_rounds()
    test_seg_entries_truncates_past_2000()
    test_get_segments_endpoint()
    test_get_audio_serves_file_or_404()
