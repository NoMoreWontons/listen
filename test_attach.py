"""Uploaded pages are kept in the vault and embedded in the note, so a misread
diagram is correctable against the original instead of being replaced by it.

Pure: no Supabase, no Claude, no network. Only the vault filesystem, pointed at
a tempdir."""
import json
import os
import pathlib
import tempfile

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app

RID = "a3f9c2e1-1111-2222-3333-444455556666"
OTHER = "b7d40000-9999-8888-7777-666655554444"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def row(rid=RID, **kw):
    r = {"id": rid, "created_at": "2026-08-25T10:00:00+00:00", "semester": "Bridge",
         "class": "Physics", "unit": "Newton's Laws", "topic": "Free Body Diagrams",
         "title": "Free Body Diagrams", "summary": "Sum of forces.",
         "transcript": "so the normal force", "source": "local"}
    r.update(kw)
    return r


def test_ext_gate():
    """Only what Obsidian embeds gets stored -- ![[x.docx]] is a broken link."""
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        assert app.save_attachment(RID, "notes.docx", b"PK\x03\x04") is None
        assert app.save_attachment(RID, "notes.txt", b"hi") is None
        assert app.save_attachment(RID, "no-extension", b"hi") is None
        assert app.save_attachment(RID, "page.PDF", b"%PDF-1.4") is not None, \
            "extension match must be case-insensitive"
        assert not (pathlib.Path(d) / app.ATTACH_DIR / "notes.docx").exists()
    print("ok  ext gate rejects non-embeddable types")


def test_naming_and_collision():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        a = app.save_attachment(RID, "Lecture Notes.png", PNG)
        b = app.save_attachment(RID, "Lecture Notes.png", PNG)
        # _slug only strips filesystem-illegal chars, so the name stays readable
        assert a == "Lecture Notes-a3f9c2e1-1.png", a
        assert b == "Lecture Notes-a3f9c2e1-2.png", b
        assert a != b, "second upload must not overwrite the first"
        assert (pathlib.Path(d) / app.ATTACH_DIR / a).read_bytes() == PNG
    print("ok  readable names, collisions increment instead of overwriting")


def test_attachments_scoped_and_ordered():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        for _ in range(11):
            app.save_attachment(RID, "p.png", PNG)
        app.save_attachment(OTHER, "p.png", PNG)
        got = app._attachments(RID)
        assert len(got) == 11, got
        assert all(OTHER[:8] not in x for x in got), "another recording's pages leaked in"
        # a plain lexical sort puts -10 and -11 ahead of -2
        assert got[1] == "p-a3f9c2e1-2.png", got[:3]
        assert got[-1] == "p-a3f9c2e1-11.png", got[-3:]
        assert app._attachments("") == []
        assert app._attachments(OTHER) == ["p-b7d40000-1.png"]
    print("ok  pages are scoped per recording and ordered numerically")


def test_note_embeds_single():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        assert "Source pages" not in app._note_md([row()]), \
            "no attachments must mean no empty section"
        name = app.save_attachment(RID, "fbd.png", PNG)
        md = app._note_md([row()])
        assert "## Source pages" in md
        assert f"![[{name}]]" in md
        # the drawing has to sit with the summary, above the raw transcript
        assert md.index("## Source pages") < md.index("## Transcript")
    print("ok  single-recording note embeds its pages above the transcript")


def test_note_embeds_per_row():
    """Combined topic notes: each dated section carries its own pages, at H3."""
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        mine = app.save_attachment(RID, "mine.png", PNG)
        theirs = app.save_attachment(OTHER, "theirs.png", PNG)
        md = app._note_md([row(), row(rid=OTHER, created_at="2026-08-26T10:00:00+00:00")])
        assert md.count("### Source pages") == 2, md.count("### Source pages")
        assert f"![[{mine}]]" in md and f"![[{theirs}]]" in md
        # each row's pages belong to its own section, not pooled at the end
        assert md.index(mine) < md.index(theirs) < md.index("## Transcripts")
    print("ok  combined note keeps each recording's pages in its own section")


def test_drop_attachments():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        app.save_attachment(RID, "a.png", PNG)
        app.save_attachment(RID, "b.png", PNG)
        keep = app.save_attachment(OTHER, "c.png", PNG)
        app.drop_attachments(RID)
        assert app._attachments(RID) == []
        assert app._attachments(OTHER) == [keep], "deleting one recording took another's pages"
        app.drop_attachments(RID)  # idempotent: delete_recording can retry
    print("ok  delete removes only that recording's pages, and repeats safely")


def test_attachments_is_not_a_semester():
    """write_graph_config scans vault-root folders as semesters. Attachments/
    sitting there must not become a phantom semester in the graph."""
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        (pathlib.Path(d) / "Bridge" / "Physics" / "Newton's Laws").mkdir(parents=True)
        app.save_attachment(RID, "fbd.png", PNG)
        app.write_graph_config()
        cfg = json.loads((pathlib.Path(d) / ".obsidian" / "graph.json").read_text(encoding="utf-8"))
        queries = [g["query"] for g in cfg["colorGroups"]]
        assert not any(app.ATTACH_DIR in q for q in queries), queries
        assert any("Bridge/Physics" in q for q in queries), queries
    print("ok  Attachments/ is excluded from the semester scan")


if __name__ == "__main__":
    test_ext_gate()
    test_naming_and_collision()
    test_attachments_scoped_and_ordered()
    test_note_embeds_single()
    test_note_embeds_per_row()
    test_drop_attachments()
    test_attachments_is_not_a_semester()
    print("\nall attachment checks passed")
