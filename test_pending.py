"""Pages staged before their lecture exists: stored, proofread, then attached.

Pure -- read_page, label and sb are stubbed, so no Claude call and no Supabase.
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
ROW = {"id": RID, "notes": "already here", "semester": "Bridge", "class": "Physics",
       "unit": "Kinematics and Motion", "topic": "Angles and Inclined Planes"}


class _Tbl:
    """Enough of the supabase builder for pending_attach's single lookup."""
    def __init__(self, rows):
        self.rows = rows

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


def stub(monkey_rows=(ROW,), text="typed up"):
    app.read_page = lambda data, filename: {"text": text}
    app.sb = type("SB", (), {"table": staticmethod(lambda name: _Tbl(list(monkey_rows)))})()
    calls = []
    app.label = lambda rid, payload: (calls.append((rid, payload)),
                                      {"ok": True, "obsidian_path": "note.md"})[1]
    return calls


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


def test_attach_files_page_and_appends_notes():
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as v:
        app.PENDING_DIR = pathlib.Path(d)
        app.OBSIDIAN_VAULT = pathlib.Path(v)
        calls = stub(text="board work Claude never heard")
        name = run(_add("Incline.png"))["name"]
        out = app.pending_attach(name, {"rid": RID})
        assert out["ok"], out
        # the page itself is now in the vault, findable by the note writer
        stored = app._attachments(RID)
        assert len(stored) == 1 and stored[0].startswith("Incline-"), stored
        # its text was appended to the existing notes, not replacing them
        rid_seen, payload = calls[0]
        assert rid_seen == RID
        assert payload["notes"] == "already here\n\nboard work Claude never heard", payload["notes"]
        # labels passed straight back so only the notes move
        assert payload["topic"] == "Angles and Inclined Planes"
        assert payload["klass"] == "Physics"
        # and the staging pair is cleared
        assert app.pending_list() == []
    print("ok  attach stores the page, appends its text, and clears the staging pair")


def test_attach_needs_a_real_target():
    with tempfile.TemporaryDirectory() as d:
        app.PENDING_DIR = pathlib.Path(d)
        stub(monkey_rows=())
        name = run(_add("p.png"))["name"]
        assert app.pending_attach(name, {"rid": ""})["ok"] is False
        out = app.pending_attach(name, {"rid": RID})
        assert out["ok"] is False and "no longer exists" in out["error"], out
        assert (pathlib.Path(d) / name).exists(), "failed attach must not eat the page"
    print("ok  attaching to a missing recording fails without losing the page")


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
    test_attach_files_page_and_appends_notes()
    test_attach_needs_a_real_target()
    test_drop()
    print("\nall pending-page checks passed")
