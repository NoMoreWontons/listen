"""Smoke check: transcription checkpoint/resume. A shutdown mid-decode must leave
the decoded segments in the DB, and the next process() run must restart whisper at
the last checkpointed second instead of re-decoding the whole lecture."""
import os

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    """select/update stand-in for supabase-py's builder, backed by one row dict."""
    def __init__(self, row):
        self.row = row
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
        if self.mode == "update":
            self.row.update(self.payload)
            return FakeResult([self.row])
        return FakeResult(dict(self.row))


class FakeSB:
    def __init__(self, row):
        self.row = row

    def table(self, name):
        assert name == "recordings"
        return FakeQuery(self.row)


class FakeSeg:
    def __init__(self, s, e, t):
        self.start, self.end, self.text = s, e, t


class FakeInfo:
    duration = 10.0  # full file, both runs -- clip_timestamps doesn't shrink it


SCRIPT = [FakeSeg(0.0, 1.0, " one"), FakeSeg(1.0, 2.0, " two"),
          FakeSeg(2.0, 3.0, " three"), FakeSeg(3.0, 10.0, " four")]


class FakeModel:
    """Yields the script from clip_timestamps onward. die_after=N raises mid-decode
    to stand in for the laptop shutting down."""
    def __init__(self, die_after=None):
        self.die_after = die_after
        self.clip = None

    def transcribe(self, path, clip_timestamps="0", **kw):
        self.clip = clip_timestamps
        self.kw = kw
        start = float(clip_timestamps)

        def gen():
            n = 0
            for seg in SCRIPT:
                if seg.start < start:
                    continue
                if self.die_after is not None and n == self.die_after:
                    raise RuntimeError("power lost")
                n += 1
                yield seg
        return gen(), FakeInfo()


def _run(row, model):
    app.sb = FakeSB(row)
    app.get_model = lambda: model
    app.audio_path = lambda rid: "fake.webm"
    finals = []
    app.finalize = lambda rid, transcript, created_at=None: finals.append(transcript)
    app._transcribe_gates.clear()
    app.process("r1")
    return finals


def test_checkpoints_during_decode():
    row = {"id": "r1", "status": "transcribing"}
    finals = _run(row, FakeModel(die_after=2))
    assert finals == [], "crashed run must not finalize"
    assert [e["t"] for e in row["segments"]] == ["one", "two"], row.get("segments")
    print("ok: decoded segments are checkpointed to the DB before the crash")


def test_resume_picks_up_where_it_left_off():
    row = {"id": "r1", "status": "transcribing",
           "segments": [{"s": 0.0, "e": 1.0, "t": "one"}, {"s": 1.0, "e": 2.0, "t": "two"}]}
    model = FakeModel()
    finals = _run(row, model)
    assert model.clip == "2.0", model.clip  # whisper restarts at the checkpoint
    # loop guard for muffled audio — a hallucinated line must not seed the next window
    assert model.kw.get("condition_on_previous_text") is False, model.kw
    assert finals == ["one two three four"], finals  # prefix + tail, in order, no dupes
    assert [e["t"] for e in row["segments"]] == ["one", "two", "three", "four"]
    print("ok: resumed run seeks to the checkpoint and merges prefix + new tail")


def test_fresh_row_starts_at_zero():
    row = {"id": "r1", "status": "transcribing"}
    model = FakeModel()
    finals = _run(row, model)
    assert model.clip == "0", model.clip
    assert finals == ["one two three four"], finals
    print("ok: a row with no checkpoint transcribes from 0 as before")


def test_progress_stays_absolute_on_resume():
    """seg.end is absolute and info.duration is full-file, so % is monotonic
    and picks up above the checkpoint rather than restarting at 0."""
    row = {"id": "r1", "status": "transcribing",
           "segments": [{"s": 0.0, "e": 2.0, "t": "two"}]}
    seen = []
    real_set = app._set

    def spy(rid, **f):
        if "progress" in f and f.get("stage") is None and "stage" not in f:
            seen.append(f["progress"])
        real_set(rid, **f)
    app._set = spy
    try:
        _run(row, FakeModel())
    finally:
        app._set = real_set
    decode_pcts = [p for p in seen if p]  # drop the stage resets at 0
    assert decode_pcts == sorted(decode_pcts), seen
    assert max(decode_pcts) == 99, seen  # 10.0/10.0 capped at 99 until finalize
    print("ok: progress on a resumed run is monotonic and absolute")


def test_cap_entries_does_not_mutate_source():
    entries = [{"s": 0.0, "e": 1.0, "t": "x" * 300} for _ in range(2001)]
    capped = app._cap_entries(entries)
    assert len(capped[0]["t"]) == 200 and len(entries[0]["t"]) == 300
    print("ok: _cap_entries truncates the DB copy, leaves the transcript text intact")


if __name__ == "__main__":
    test_checkpoints_during_decode()
    test_resume_picks_up_where_it_left_off()
    test_fresh_row_starts_at_zero()
    test_progress_stays_absolute_on_resume()
    test_cap_entries_does_not_mutate_source()
