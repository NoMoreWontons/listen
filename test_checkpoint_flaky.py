"""Regression: a transient DB failure on a progress checkpoint must not kill the
transcription. Supabase's gateway 504'd one PATCH out of a ~1/s burst of progress
writes (2026-09-11 19:05:01Z, 5031ms), and process()'s except turned that into
status="error" -- 10 minutes of decoded lecture thrown away for a blip."""
import os

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app


class Gateway504(Exception):
    """Shape of what supabase-py raises: {'code': 504, 'message': 'Gateway Timeout'}."""


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, sb):
        self.sb = sb
        self.mode = "select"
        self.payload = None

    def select(self, *a, **k):
        return self

    def update(self, payload, **kw):   # real .update() takes returning="minimal"
        self.mode, self.payload = "update", payload
        return self

    def limit(self, n):
        self.cap = n
        return self

    def eq(self, *a):
        return self

    def single(self):
        return self

    def execute(self):
        if self.mode != "update":
            return FakeResult(dict(self.sb.row))
        self.sb.writes += 1
        if self.sb.writes in self.sb.fail_on:
            raise Gateway504("Gateway Timeout")
        self.sb.row.update(self.payload)
        return FakeResult([self.sb.row])


class FakeSB:
    """fail_on: 1-indexed write attempts that 504 instead of committing."""
    def __init__(self, row, fail_on=()):
        self.row, self.fail_on, self.writes = row, set(fail_on), 0

    def table(self, name):
        assert name == "recordings"
        return FakeQuery(self)


class FakeSeg:
    def __init__(self, s, e, t):
        self.start, self.end, self.text = s, e, t


class FakeInfo:
    duration = 10.0


SCRIPT = [FakeSeg(0.0, 1.0, " one"), FakeSeg(1.0, 2.0, " two"),
          FakeSeg(2.0, 3.0, " three"), FakeSeg(3.0, 10.0, " four")]


class FakeModel:
    def transcribe(self, path, clip_timestamps="0", **kw):
        return iter(SCRIPT), FakeInfo()


def _run(fail_on):
    row = {"id": "r1", "status": "transcribing"}
    app.sb = FakeSB(row, fail_on=fail_on)
    app.get_model = lambda: FakeModel()
    app.audio_path = lambda rid: "fake.webm"
    finals = []
    app.finalize = lambda rid, transcript, created_at=None: finals.append(transcript)
    app._transcribe_gates.clear()
    app.process("r1")
    return row, finals


def test_retry_absorbs_a_single_flaky_write():
    """_set retries once, so a lone 504 never even reaches the checkpoint guard."""
    calls = []
    sb = FakeSB({"id": "r1"}, fail_on=[1])
    app.sb = sb
    app._set("r1", progress=50)
    assert sb.writes == 2, sb.writes           # failed once, retried, committed
    assert sb.row["progress"] == 50, sb.row
    print("ok: _set retries a transient write failure instead of raising")


def test_set_still_raises_when_the_db_is_really_down():
    sb = FakeSB({"id": "r1"}, fail_on=[1, 2])
    app.sb = sb
    try:
        app._set("r1", progress=50)
    except Gateway504:
        print("ok: _set gives up after the retry rather than swallowing a real outage")
    else:
        raise AssertionError("a persistently failing write must surface to the caller")


def test_checkpoint_failure_does_not_kill_the_job():
    """Both attempts on a progress write 504. The decode must keep going and the
    lecture must still finalize -- not land in status='error'."""
    # writes 1-2 = stage/progress preamble, 3-4 = the first progress checkpoint
    # (attempt + retry), so failing 3 and 4 kills one checkpoint outright.
    row, finals = _run(fail_on=[3, 4])
    assert row["status"] != "error", row
    assert finals == ["one two three four"], finals
    assert [e["t"] for e in row["segments"]] == ["one", "two", "three", "four"], row["segments"]
    print("ok: a dead progress checkpoint is logged and skipped, the lecture completes")


def test_a_real_transcription_failure_still_errors():
    """The guard must not swallow genuine failures -- a model blowup still errors."""
    class Exploding:
        def transcribe(self, *a, **kw):
            raise RuntimeError("cuda fell over")

    row = {"id": "r1", "status": "transcribing"}
    app.sb = FakeSB(row)
    app.get_model = lambda: Exploding()
    app.audio_path = lambda rid: "fake.webm"
    app.finalize = lambda *a, **kw: None
    app._transcribe_gates.clear()
    app.process("r1")
    assert row["status"] == "error", row
    assert "cuda fell over" in row["summary"], row["summary"]
    print("ok: a genuine transcription failure still marks the row errored")


if __name__ == "__main__":
    test_retry_absorbs_a_single_flaky_write()
    test_set_still_raises_when_the_db_is_really_down()
    test_checkpoint_failure_does_not_kill_the_job()
    test_a_real_transcription_failure_still_errors()
    print("all ok")
