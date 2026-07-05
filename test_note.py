"""Smoke check: write_note files to class/unit/topic and re-files on relabel."""
import os
import tempfile
import pathlib

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app


def test_write_and_refile():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        row = {
            "id": "abcdef12-0000", "title": "L1", "created_at": "2026-07-04T10:00:00",
            "transcript": "hello world", "summary": "- point", "semester": "Fall 26",
            "class": "Biology", "unit": "Cells", "topic": "Mitosis", "obsidian_path": None,
        }
        p1 = app.write_note(row)
        assert p1 == str(pathlib.Path(d) / "Fall 26" / "Biology" / "Cells" / "Mitosis.md"), p1
        assert pathlib.Path(p1).exists()

        # relabel -> new path, old file gone
        row.update(obsidian_path=p1, unit="Division", topic="Meiosis")
        p2 = app.write_note(row)
        assert p2 == str(pathlib.Path(d) / "Fall 26" / "Biology" / "Division" / "Meiosis.md"), p2
        assert pathlib.Path(p2).exists()
        assert not pathlib.Path(p1).exists(), "stale note should be removed on re-file"

        # empty transcript -> no note
        assert app.write_note({**row, "transcript": "  "}) is None

        # collision from a different recording -> disambiguated filename
        other = {**row, "id": "999999-xx", "obsidian_path": None}
        p3 = app.write_note(other)
        assert p3 != p2 and pathlib.Path(p3).exists(), p3
    print("ok: write_note files, re-files, skips empty, disambiguates")


if __name__ == "__main__":
    test_write_and_refile()
