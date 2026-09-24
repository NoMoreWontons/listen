"""Smoke check: _sync_base folds a legacy ![[Timeline.base#Timeline]] embed
into an inline ```base block, keeps that block current when the template moves
on, and leaves everything the user wrote around it alone."""
import os
import pathlib
import tempfile

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app

REL = "Fall 26/ME 1100 Tech Comms for Engineers"
LEGACY = (
    "# ME 1100 Tech Comms for Engineers\n\n"
    f"![[{REL}/Timeline.base#Timeline]]\n\n"
    "Semester: [[Fall 26/Fall 26|Fall 26]]\n"
)


def hub(body):
    d = pathlib.Path(tempfile.mkdtemp())
    p = d / "hub.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_folds_legacy_embed():
    p = hub(LEGACY)
    app._sync_base(p, REL)
    out = p.read_text(encoding="utf-8")
    assert "Timeline.base" not in out, out          # the embed is gone
    assert "```base" in out and f'inFolder("{REL}")' in out, out
    assert out.startswith("# ME 1100"), out          # heading kept
    assert "Semester: [[Fall 26/Fall 26|Fall 26]]" in out, out   # tail kept
    assert out.count("```") == 2, out                # one fence, opened and closed


def test_idempotent_and_refreshes():
    p = hub(LEGACY)
    app._sync_base(p, REL)
    once = p.read_text(encoding="utf-8")
    app._sync_base(p, REL)
    assert p.read_text(encoding="utf-8") == once, "second sync changed the note"
    # a block written for the wrong folder gets rewritten, not duplicated
    app._sync_base(p, "Bridge/Physics")
    out = p.read_text(encoding="utf-8")
    assert out.count("```base") == 1 and 'inFolder("Bridge/Physics")' in out, out


def test_user_text_survives_and_missing_block_appended():
    p = hub(LEGACY + "\nMy own notes about this class.\n")
    app._sync_base(p, REL)
    assert "My own notes about this class." in p.read_text(encoding="utf-8")
    # a hub with no block at all gets one rather than being left bare
    p = hub("# Bare hub\n")
    app._sync_base(p, REL)
    out = p.read_text(encoding="utf-8")
    assert out.startswith("# Bare hub") and "```base" in out, out


def test_ensure_hubs_writes_inline_and_no_base_file():
    root = pathlib.Path(tempfile.mkdtemp())
    old, app.OBSIDIAN_VAULT = app.OBSIDIAN_VAULT, root
    try:
        app.ensure_hubs({"semester": "Fall 26", "class": "ME 1100", "unit": "Week 1"})
        cls_p = root / "Fall 26" / "ME 1100" / "ME 1100.md"
        unit_p = root / "Fall 26" / "ME 1100" / "Week 1" / "Week 1.md"
        for p in (cls_p, unit_p):
            assert "```base" in p.read_text(encoding="utf-8"), p
        assert not list(root.rglob("*.base")), "a Timeline.base file was written"
    finally:
        app.OBSIDIAN_VAULT = old


if __name__ == "__main__":
    test_folds_legacy_embed()
    test_idempotent_and_refreshes()
    test_user_text_survives_and_missing_block_appended()
    test_ensure_hubs_writes_inline_and_no_base_file()
    print("ok")
