"""Both halves of the row/file bookkeeping, which a real lecture fell through.

Run: ./.venv/Scripts/python.exe test_orphan_audio.py
No network, no Supabase, no whisper — app.py's module-level clients would need
all three, so the two functions under test are re-read from source and executed
against fakes. That keeps the check honest about ORDER and ADOPTION without
standing up the app.
"""
import ast
import datetime
import pathlib
import tempfile

SRC = pathlib.Path(__file__).with_name("app.py").read_text(encoding="utf-8")


def _func(name):
    """The source of one top-level function in app.py, compiled standalone."""
    tree = ast.parse(SRC)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(SRC, node)
    raise AssertionError(f"{name}() is gone from app.py")


class FakeTable:
    def __init__(self, db, log):
        self.db, self.log, self._sel = db, log, None

    def select(self, *a):
        self._sel = a
        return self

    def insert(self, row):
        self.db[row["id"]] = row
        self.log.append(("insert", row["id"]))
        return self

    def delete(self):
        self.log.append(("delete_row", None))
        self._del = True
        return self

    def eq(self, _col, val):
        self._id = val
        return self

    def in_(self, _col, _vals):
        self._in = True
        return self

    def execute(self):
        if getattr(self, "_del", False):
            self.db.pop(getattr(self, "_id", None), None)
            self._del = False
            return type("R", (), {"data": []})()
        return type("R", (), {"data": [{"id": k} for k in self.db]})()


class FakeSB:
    def __init__(self):
        self.db, self.log = {}, []

    def table(self, _name):
        return FakeTable(self.db, self.log)


def test_delete_unlinks_before_dropping_the_row():
    """A failing unlink must not leave the file unreachable."""
    log = []

    class Boom:
        def unlink(self, missing_ok=False):
            log.append(("unlink_audio", None))
            raise PermissionError("file is open by an in-flight /chunk")

    sb = FakeSB()
    sb.db["r1"] = {"id": "r1"}
    ns = {
        "sb": sb,
        "audio_path": lambda rid: Boom(),
        "pdf_path": lambda rid: type("P", (), {"unlink": lambda s, missing_ok=False: None})(),
        "drop_attachments": lambda rid: None,
        "_refile_group": lambda *a: None,
        "app": type("A", (), {"delete": lambda s, p: (lambda f: f)})(),
    }
    exec(_func("delete_recording"), ns)
    try:
        ns["delete_recording"]("r1")
    except PermissionError:
        pass  # the unlink blew up, which is the scenario

    kinds = [k for k, _ in log] + [k for k, _ in sb.log]
    assert "unlink_audio" in kinds, "audio unlink never attempted"
    assert "delete_row" not in kinds, (
        "row was deleted despite the unlink failing — that is exactly the orphan bug"
    )
    assert "r1" in sb.db, "row vanished; the recording is now unreachable"


def test_adopt_gives_orphan_audio_a_row_and_queues_it():
    with tempfile.TemporaryDirectory() as d:
        audio = pathlib.Path(d)
        orphan = audio / "6a588d97-cbbd-431c-9b37-c9f0dea9b8c7.webm"
        orphan.write_bytes(b"x" * 4096)
        known = audio / "11111111-1111-1111-1111-111111111111.webm"
        known.write_bytes(b"x" * 4096)
        (audio / "22222222-2222-2222-2222-222222222222.webm").write_bytes(b"")  # empty

        sb = FakeSB()
        sb.db[known.stem] = {"id": known.stem}
        started = []
        ns = {
            "sb": sb,
            "AUDIO_DIR": audio,
            "datetime": datetime,
            "process": lambda rid: None,
            "threading": type("T", (), {
                "Thread": lambda **kw: type("H", (), {
                    "start": lambda s: started.append(kw["args"][0])
                })()
            }),
            "print": lambda *a, **k: None,
        }
        exec(_func("adopt_orphan_audio"), ns)
        ns["adopt_orphan_audio"]()

        assert orphan.stem in sb.db, "orphan audio did not get a row"
        assert started == [orphan.stem], f"expected only the orphan queued, got {started}"
        assert sb.db[orphan.stem]["status"] == "transcribing"
        assert "(recovered)" in sb.db[orphan.stem]["title"]


def test_adopt_survives_a_junk_filename():
    with tempfile.TemporaryDirectory() as d:
        audio = pathlib.Path(d)
        (audio / "not-a-uuid.webm").write_bytes(b"x" * 10)
        good = audio / "33333333-3333-3333-3333-333333333333.webm"
        good.write_bytes(b"x" * 10)

        sb = FakeSB()
        real_insert = FakeTable.insert

        def picky(self, row):
            if "-" not in row["id"] or len(row["id"]) != 36:
                raise ValueError("invalid input syntax for type uuid")
            return real_insert(self, row)

        FakeTable.insert = picky
        try:
            started = []
            ns = {
                "sb": sb, "AUDIO_DIR": audio, "datetime": datetime,
                "process": lambda rid: None,
                "threading": type("T", (), {
                    "Thread": lambda **kw: type("H", (), {
                        "start": lambda s: started.append(kw["args"][0])
                    })()
                }),
                "print": lambda *a, **k: None,
            }
            exec(_func("adopt_orphan_audio"), ns)
            ns["adopt_orphan_audio"]()
        finally:
            FakeTable.insert = real_insert

        assert started == [good.stem], "a junk filename must not stop the good adoption"


def test_startup_calls_adopt():
    assert "adopt_orphan_audio()" in SRC.split("async def lifespan")[1][:600], (  # window, not an exact position — lifespan grew a try/except
        "adopt_orphan_audio() is not wired into lifespan startup"
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nall passed")
