"""Pages staged from the iPad: stored, proofread, then filed as their own note.

Pure -- read_page, sb and the worker thread are stubbed, so no Claude call and no Supabase.
The staging directory is redirected to a tempdir."""
import json
import os
import pathlib
import tempfile

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app

RID = "759d36d6-15fd-4ec1-af08-a69dce96a94c"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class _Tbl:
    """Enough of the supabase builder for pending_note's insert."""
    def __init__(self, rows):
        self.rows = rows
        self.inserted = []

    def insert(self, row):
        self.inserted.append(row)
        self.rows = [{"id": RID, **row}]
        return self

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


def stub(text="typed up"):
    app.read_page = lambda data, filename: {"text": text}
    tbl = _Tbl([])
    app.sb = type("SB", (), {"table": staticmethod(lambda name: tbl)})()
    return tbl


async def _add(filename, data=PNG):
    class Req:
        async def body(self):
            return data
    return await app.pending_add(Req(), filename=filename)


def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_path_traversal_refused():
    """`name` comes from the browser. It must not reach outside the staging dir."""
    with tempfile.TemporaryDirectory() as d:
        app.PENDING_DIR = pathlib.Path(d)
        for bad in ("../app.py", "..\\app.py", "sub/page.png", ""):
            try:
                app._pending_path(bad)
                raise AssertionError(f"accepted {bad!r}")
            except ValueError:
                pass
        assert app._pending_path("page.png").parent == pathlib.Path(d).resolve()
    print("ok  staging paths reject traversal")


def test_add_stores_page_and_text():
    with tempfile.TemporaryDirectory() as d:
        app.PENDING_DIR = pathlib.Path(d)
        stub(text="Sum F = ma")
        out = run(_add("Free Body.png"))
        assert out.get("error") is None, out
        assert out["text"] == "Sum F = ma"
        page = pathlib.Path(d) / out["name"]
        assert page.read_bytes() == PNG, "original page must be kept, not just its text"
        side = json.loads(page.with_name(page.name + ".json").read_text(encoding="utf-8"))
        assert side["filename"] == "Free Body.png", side
        assert side["text"] == "Sum F = ma"
    print("ok  staged page keeps the original bytes and its transcription")


def test_unreadable_page_is_not_staged():
    """A file nothing can read would sit in the list forever with no text."""
    with tempfile.TemporaryDirectory() as d:
        app.PENDING_DIR = pathlib.Path(d)
        app.read_page = lambda data, filename: {"error": "unsupported file type"}
        app.sb = type("SB", (), {"table": staticmethod(lambda n: _Tbl([]))})()
        out = run(_add("notes.zip"))
        assert out.get("error"), out
        assert list(pathlib.Path(d).iterdir()) == [], "failed upload left a file behind"
    print("ok  an unreadable upload leaves nothing staged")


def test_list_newest_first_and_hides_sidecars():
    with tempfile.TemporaryDirectory() as d:
        app.PENDING_DIR = pathlib.Path(d)
        stub()
        a = run(_add("first.png"))["name"]
        b = run(_add("second.png"))["name"]
        rows = app.pending_list()
        assert [r["name"] for r in rows][0] in (b, a) and len(rows) == 2, rows
        assert all(not r["name"].endswith(".json") for r in rows), "sidecar listed as a page"
        assert rows[0]["uploaded_at"] >= rows[1]["uploaded_at"], "not newest first"
    print("ok  waiting list shows pages only, newest first")


def test_edit_saves_corrections():
    with tempfile.TemporaryDirectory() as d:
        app.PENDING_DIR = pathlib.Path(d)
        stub(text="mu = 0.35")
        name = run(_add("p.png"))["name"]
        assert app.pending_edit(name, {"text": "mu_s = 0.35"})["ok"]
        assert app.pending_list()[0]["text"] == "mu_s = 0.35"
        assert (pathlib.Path(d) / name).exists(), "editing text must not drop the page"
        gone = app.pending_edit("nope.png", {"text": "x"})
        assert gone["ok"] is False and "no longer waiting" in gone["error"]
    print("ok  proofread corrections persist, and a missing page errors cleanly")


def test_note_files_page_as_new_row():
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as v:
        app.PENDING_DIR = pathlib.Path(d)
        app.OBSIDIAN_VAULT = pathlib.Path(v)
        tbl = stub(text="board work Claude never heard")
        ran, real = [], app.threading.Thread
        app.threading.Thread = lambda target, args, daemon: type(
            "T", (), {"start": lambda self: ran.append((target, args))})()
        name = run(_add("Incline.png"))["name"]
        try:
            out = app.pending_note(name)
        finally:
            app.threading.Thread = real
        assert out["ok"], out
        # a fresh row, carrying the text before anything can fail
        row = tbl.inserted[0]
        assert row["transcript"] == "board work Claude never heard", row
        assert row["source"] == "ipad_page" and row["title"] == "Incline", row
        # the page itself is in the vault under the NEW row, findable by the note writer
        stored = app._attachments(out["id"])
        assert len(stored) == 1 and stored[0].startswith("Incline-"), stored
        # summarize/label runs in the background on that row
        target, args = ran[0]
        assert target is app._page_note and args[0] == out["id"], ran
        assert app.pending_list() == []
    print("ok  new note makes its own row, stores the page, and clears the staging pair")


def test_note_needs_text():
    with tempfile.TemporaryDirectory() as d:
        app.PENDING_DIR = pathlib.Path(d)
        tbl = stub()
        name = run(_add("p.png"))["name"]
        app.pending_edit(name, {"text": "   "})
        out = app.pending_note(name)
        assert out["ok"] is False and "no text" in out["error"], out
        assert tbl.inserted == [], "empty page still made a row"
        assert (pathlib.Path(d) / name).exists(), "refused page must stay waiting"
    print("ok  a page with no text is refused without losing it")


def test_own_topic_never_shares_a_note():
    """write_note combines every row with the same topic into one note, so a
    page filed as its own note must not keep a topic another row already has."""
    assert app._own_topic("Friction", {"Normal force"}, "2026-09-25") == "Friction"
    assert app._own_topic("Friction", {"Friction"}, "2026-09-25") == "Friction (2026-09-25)"
    taken = {"Friction", "Friction (2026-09-25)"}
    assert app._own_topic("Friction", taken, "2026-09-25") == "Friction (2026-09-25 2)"
    print("ok  a page note's topic never collides with an existing note")


def test_drop():
    with tempfile.TemporaryDirectory() as d:
        app.PENDING_DIR = pathlib.Path(d)
        stub()
        name = run(_add("p.png"))["name"]
        assert app.pending_drop(name)["ok"]
        assert app.pending_list() == []
        assert list(pathlib.Path(d).iterdir()) == [], "sidecar outlived its page"
        assert app.pending_drop(name)["ok"], "discard should be idempotent"
    print("ok  discard removes the page and its sidecar, and repeats safely")


if __name__ == "__main__":
    test_path_traversal_refused()
    test_add_stores_page_and_text()
    test_unreadable_page_is_not_staged()
    test_list_newest_first_and_hides_sidecars()
    test_edit_saves_corrections()
    test_note_files_page_as_new_row()
    test_note_needs_text()
    test_own_topic_never_shares_a_note()
    test_drop()
    print("\nall pending-page checks passed")
