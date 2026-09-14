"""_folded wraps a transcript in a callout Obsidian renders collapsed."""
from app import _folded


def demo():
    out = _folded("one line")
    assert out.startswith("> [!quote]- Transcript\n"), out
    assert out.endswith("> one line"), out

    # blank lines keep their '>' -- without it the callout ends at the gap and
    # the rest of the transcript spills into the note body unfolded
    out = _folded("first\n\nsecond")
    assert out.splitlines() == ["> [!quote]- Transcript", "> first", ">", "> second"], out

    # the '-' is what makes it start folded; '> [!quote] ' (no dash) would render open
    assert "[!quote]- " in _folded("x")

    # a custom title is used for the per-recording sections of a combined note
    assert _folded("x", "2026-09-04 - Lecture").startswith("> [!quote]- 2026-09-04 - Lecture\n")

    # empty transcript still produces a well-formed (empty) callout, never bare text
    assert _folded("") == "> [!quote]- Transcript\n>", _folded("")
    assert _folded(None) == "> [!quote]- Transcript\n>"

    print("ok")


if __name__ == "__main__":
    demo()
