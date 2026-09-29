"""Smoke check: order_class writes each lecture note's timeline seq in the
model's learning order, keeps course numbering and hand-locked seqs, touches
only the seq line, and survives a junk reply."""
import os
import pathlib
import tempfile

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app

# _fix_order: junk dropped, missing appended by date, numbered notes keep course order
notes = [{"date": "2026-09-03", "ordinal": (1, 2)}, {"date": "2026-09-01", "ordinal": None},
         {"date": "2026-09-02", "ordinal": (1, 1)}, {"date": "2026-09-04", "ordinal": None}]
assert app._fix_order([0, 0, 9, "x", 2], notes) == [2, 0, 1, 3]
assert app._fix_order(None, notes) == [1, 2, 0, 3]
# a numbered note the model misplaced moves next to its neighbour, not into a far slot
far = [{"date": "d", "ordinal": (1, 1)}, {"date": "d", "ordinal": (1, 2)},
       {"date": "d", "ordinal": (4, 1)}, {"date": "d", "ordinal": None}]
assert app._fix_order([0, 3, 2, 1], far) == [0, 1, 3, 2]

# _fm_set / _fm_get
doc = '---\ntopic: "A"\ndate: 2026-09-01\n---\n\nbody seq: no\n'
assert app._fm_get(doc, "seq") is None
doc2 = app._fm_set(doc, "seq", '"0010"')
assert app._fm_get(doc2, "seq") == '"0010"' and doc2.endswith("\n---\n\nbody seq: no\n")
assert app._fm_set(app._fm_set(doc2, "seq", '"0020"'), "seq", '"0020"').count("seq:") == 2  # frontmatter + body

# order_class on a temp vault
app.OBSIDIAN_VAULT = V = pathlib.Path(tempfile.mkdtemp())
cls_dir = V / "Fall 26" / "MATH"
(cls_dir / "U").mkdir(parents=True)
(cls_dir / "MATH.md").write_text("# MATH\n")  # hub: no lecture tag, never ordered


def note(name, date, extra=""):
    p = cls_dir / "U" / f"{name}.md"
    p.write_text(f'---\ntopic: "{name}"\nunit: "U"\nsummary: "s"\ndate: {date}\n'
                 f'tags: [lecture, local]{extra}\n---\n\n# {name}\n', encoding="utf-8")
    return p


a, b, c = note("Apply", "2026-09-01"), note("Basics", "2026-09-02"), note("Pinned", "2026-09-03", '\nseq: "0015"\nseq_lock: true')


class Q:
    def select(self, *a, **k): return self
    def eq(self, *a): return self
    def execute(self): return type("R", (), {"data": []})()


app.sb.table = lambda name: Q()
calls = []


def reply(text):
    def create(**kw):
        calls.append(kw)
        return type("M", (), {"content": [type("B", (), {"type": "text", "text": text})()]})()
    return create


app.claude.messages.create = reply('{"order": [1, 2, 0]}')  # Basics, Pinned, Apply
app.order_class("Fall 26", "MATH")
seq = lambda p: app._fm_val(p.read_text(encoding="utf-8"), "seq")
assert (seq(b), seq(c), seq(a)) == ("0010", "0015", "0020"), (seq(a), seq(b), seq(c))
assert (cls_dir / "MATH.md").read_text() == "# MATH\n"
assert b.read_text(encoding="utf-8").endswith("\n---\n\n# Basics\n")

app.order_class("Fall 26", "MATH")
assert len(calls) == 1  # same note set: no second call

note("New", "2026-09-04")
app.claude.messages.create = reply("sorry, no JSON")  # junk: fall back to date order
app.order_class("Fall 26", "MATH")
assert (seq(a), seq(b), seq(c)) == ("0010", "0020", "0015")
print("ok")
assert app._section_no("The normal distribution and z-scores (4.3)") == (4, 3)
assert app._section_no("Boxplots (1.4) review") is None and app._section_no("") is None
print("ok: section numbers")
