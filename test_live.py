"""Smoke check: live-transcript pure parts -- append bookkeeping and
app_settings default/round-trip -- without touching ffmpeg/whisper/Supabase."""
import os
import json
import pathlib
import tempfile

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app


def test_append_live_joins_and_strips():
    assert app._append_live(None, "hello") == "hello"
    assert app._append_live("", "hello") == "hello"
    assert app._append_live("hello", "world") == "hello world"
    assert app._append_live("  hello  ", "world") == "hello   world"  # no inner trimming, just outer
    print("ok: _append_live joins old+new, strips only the ends")


def test_app_settings_default_and_round_trip():
    with tempfile.TemporaryDirectory() as d:
        settings_file = pathlib.Path(d) / "app_settings.json"
        app.APP_SETTINGS_FILE = settings_file

        assert app.app_settings() == {"live_transcript": False}, "default off, no file yet"

        settings_file.write_text(json.dumps({"live_transcript": True}), encoding="utf-8")
        assert app.app_settings() == {"live_transcript": True}

        # non-bool values ignored, like graph_settings ignoring bad floats
        settings_file.write_text(json.dumps({"live_transcript": "yes"}), encoding="utf-8")
        assert app.app_settings() == {"live_transcript": True}, "non-bool coerces via bool(), stays truthy"

        # corrupt file -> default, doesn't throw
        settings_file.write_text("not json", encoding="utf-8")
        assert app.app_settings() == {"live_transcript": False}
    print("ok: app_settings defaults, persists, survives corrupt/garbage files")


def test_set_app_settings_endpoint_rejects_non_bool():
    import asyncio

    with tempfile.TemporaryDirectory() as d:
        app.APP_SETTINGS_FILE = pathlib.Path(d) / "app_settings.json"

        class FakeRequest:
            def __init__(self, payload):
                self.payload = payload
            async def json(self):
                return self.payload

        r1 = asyncio.run(app.set_app_settings(FakeRequest({"live_transcript": True})))
        assert r1 == {"ok": True, "live_transcript": True}

        # a string isn't a bool -- must be ignored, not coerced truthy
        r2 = asyncio.run(app.set_app_settings(FakeRequest({"live_transcript": "false"})))
        assert r2 == {"ok": True, "live_transcript": True}, "non-bool posted value ignored"
    print("ok: /app_settings POST only accepts real booleans")


if __name__ == "__main__":
    test_append_live_joins_and_strips()
    test_app_settings_default_and_round_trip()
    test_set_app_settings_endpoint_rejects_non_bool()
