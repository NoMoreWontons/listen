"""Smoke check: the retention sweep deletes only audio older than the window."""
import os
import time
import tempfile
import pathlib

# Dummy creds so importing app (which builds the clients at import) doesn't need real ones.
os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app
from app import sweep_old_audio


def test_resume_stuck_recovers_crashed_recordings():
    """A row stuck in 'recording' (PC died before /stop) with audio on disk
    gets flipped to transcribing and processed; rows with no audio are left."""
    with tempfile.TemporaryDirectory() as d:
        app.AUDIO_DIR = pathlib.Path(d)
        (app.AUDIO_DIR / "crashed.webm").write_bytes(b"x")

        class FakeTable:
            def select(self, *a): return self
            def in_(self, col, vals):
                assert "recording" in vals and "transcribing" in vals
                return self
            def execute(self):
                return type("R", (), {"data": [{"id": "crashed"}, {"id": "no-audio"}]})()
        app.sb = type("SB", (), {"table": lambda self, n: FakeTable()})()

        updated, processed = [], []
        app._set = lambda rid, **kw: updated.append((rid, kw))
        app.threading.Thread = lambda target, args, daemon: type(
            "T", (), {"start": lambda self: processed.append(args[0])})()

        app.resume_stuck()

        assert updated == [("crashed", {"status": "transcribing"})], updated
        assert processed == ["crashed"], processed
    print("ok: resume_stuck recovers crashed recordings, skips rows with no audio")


def test_transcribe_pause_toggle():
    """POST toggles the gate; GET reports without toggling; workers waiting on
    the gate actually block while paused and wake on resume."""
    assert app.transcribe_paused() == {"paused": False}
    assert app.transcribe_pause() == {"paused": True}
    assert app.transcribe_paused() == {"paused": True}
    assert not app._transcribe_gate.wait(timeout=0.05), "gate should block while paused"
    assert app.transcribe_pause() == {"paused": False}
    assert app._transcribe_gate.wait(timeout=0.05), "gate should open on resume"
    print("ok: transcribe pause toggles and gates waiting workers")


def test_sweep_deletes_only_old():
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        old = d / "old.webm"
        new = d / "new.webm"
        old.write_bytes(b"x")
        new.write_bytes(b"x")
        eight_days_ago = time.time() - 8 * 86400
        os.utime(old, (eight_days_ago, eight_days_ago))

        sweep_old_audio(directory=d, days=7)

        assert not old.exists(), "old audio should be deleted"
        assert new.exists(), "fresh audio should be kept"
    print("ok")


if __name__ == "__main__":
    test_resume_stuck_recovers_crashed_recordings()
    test_transcribe_pause_toggle()
    test_sweep_deletes_only_old()
