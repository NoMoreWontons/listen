"""Smoke check: a note's '## Review of last class' links the note of that
class's previous recording, and skips the link when there is nothing to link
(no review, first lecture, or last class shares this topic)."""
import os
import pathlib
import tempfile

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app

V = app.OBSIDIAN_VAULT = pathlib.Path(tempfile.mkdtemp())
ROWS = [
    {"semester": "Fall 26", "class": "MATH", "unit": "U", "topic": "Old", "status": "done",
     "created_at": "2026-09-01T10:00:00", "obsidian_path": str(V / "Fall 26/MATH/U/Old.md")},
    {"semester": "Fall 26", "class": "MATH", "unit": "U", "topic": "Prev", "status": "done",
     "created_at": "2026-09-03T10:00:00", "obsidian_path": str(V / "Fall 26/MATH/U/Prev.md")},
    {"semester": "Fall 26", "class": "PHYS", "unit": "U", "topic": "Other", "status": "done",
     "created_at": "2026-09-04T10:00:00", "obsidian_path": str(V / "Fall 26/PHYS/U/Other.md")},
    # a split lecture: two segments, one timestamp; the first's note was moved by hand
    {"semester": "Fall 26", "class": "MATH", "unit": "U", "topic": "Gone", "status": "done",
     "created_at": "2026-09-03T10:00:00", "obsidian_path": str(V / "Fall 26/MATH/U/Gone.md")},
    {"semester": "Fall 26", "class": "MATH", "unit": "S", "topic": "Syllabus", "status": "done",
     "source": "syllabus", "created_at": "2026-09-04T09:00:00",
     "obsidian_path": str(V / "Fall 26/MATH/S/Syllabus.md")},
]
ROWS.insert(1, ROWS.pop(3))  # stale segment listed before the live one
for r in ROWS:
    if r["topic"] != "Gone":
        pathlib.Path(r["obsidian_path"]).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(r["obsidian_path"]).write_text("x")


class Q:
    def __init__(self):
        self.rows = ROWS

    def select(self, *a, **k):
        return self

    def eq(self, k, v):
        self.rows = [r for r in self.rows if r.get(k) == v]
        return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


app.sb.table = lambda name: Q()

SUMMARY = "## Review of last class\n\n- recap\n\n## Key points\n\n- new"
today = {"semester": "Fall 26", "class": "MATH", "unit": "U", "topic": "Today",
         "created_at": "2026-09-05T10:00:00", "summary": SUMMARY}

link = app._prev_class_link(today)
assert link == "[[Fall 26/MATH/U/Prev|Prev]]", link  # latest earlier MATH lecture: not PHYS, not the syllabus, not the stale segment
out = app._link_review(SUMMARY, link)
assert out.startswith("## Review of last class\n\nPrevious class: [[Fall 26/MATH/U/Prev|Prev]]\n\n- recap"), out

assert app._prev_class_link({**today, "summary": "## Key points\n\n- x"}) == ""   # no review section
assert app._prev_class_link({**today, "created_at": "2026-08-01T00:00:00"}) == ""  # first lecture
assert app._prev_class_link({**today, "topic": "Prev"}) == ""                      # same note
assert app._link_review("## Key points", link) == "## Key points"
print("ok")
