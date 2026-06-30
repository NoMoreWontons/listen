"""Smoke check: the retention sweep deletes only audio older than the window."""
import os
import time
import tempfile
import pathlib

# Dummy creds so importing app (which builds the clients at import) doesn't need real ones.
os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

from app import sweep_old_audio


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
    test_sweep_deletes_only_old()
