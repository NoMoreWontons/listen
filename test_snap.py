"""_snap_labels: segments snap to existing labels; unrelated ones don't."""
import app

rows = [
    {"class": "Calculus", "unit": "Derivatives", "topic": "Implicit Differentiation"},
    {"class": "Calculus", "unit": "Applications of Derivatives", "topic": "Related Rates Problems"},
    {"class": "Physics", "unit": "Newton's Laws and Dynamics", "topic": "Friction Forces & Applications"},
]

# continuation lecture: 'part 2' phrasing and reordered words snap to existing labels
segs = app._snap_labels([{"class": "Calculus", "unit": "Derivative Applications",
                          "topic": "Implicit Differentiation Part 2", "summary": ""}], rows)
assert segs[0]["unit"] == "Applications of Derivatives", segs[0]["unit"]
# topic scoped to the snapped unit — Implicit Differentiation lives under Derivatives, so no snap here
assert segs[0]["topic"] == "Implicit Differentiation Part 2", segs[0]["topic"]

segs = app._snap_labels([{"class": "Calculus", "unit": "Derivatives",
                          "topic": "Implicit Differentiation Continued", "summary": ""}], rows)
assert segs[0]["topic"] == "Implicit Differentiation", segs[0]["topic"]

# genuinely new topic stays untouched
segs = app._snap_labels([{"class": "Calculus", "unit": "Derivatives",
                          "topic": "Chain Rule", "summary": ""}], rows)
assert segs[0]["topic"] == "Chain Rule", segs[0]["topic"]

# unknown class: nothing to snap to, labels pass through
segs = app._snap_labels([{"class": "US History", "unit": "Civil War",
                          "topic": "Gettysburg", "summary": ""}], rows)
assert segs[0] == {"class": "US History", "unit": "Civil War", "topic": "Gettysburg", "summary": ""}

# empty labels never snap (empty word set scores 0)
segs = app._snap_labels([{"class": "", "unit": "", "topic": "", "summary": ""}], rows)
assert segs[0]["class"] == "" and segs[0]["topic"] == ""

# --- the reported bug: a class folder scaffolded from the syllabus carries the
# catalogue name; Claude, hearing only the lecture, writes the plain one. Those
# score 0.5 on raw Jaccard — under the 0.6 bar — so the vault grew a second
# 'Multivariable Calculus' folder beside 'MATH 2110Q Multivariable Calculus'.
vault = ["ENGR 2001 AI Literacy", "MATH 2110Q Multivariable Calculus"]
segs = app._snap_labels([{"class": "Multivariable Calculus", "unit": "Vectors",
                          "topic": "Dot Product", "summary": ""}], [], vault)
assert segs[0]["class"] == "MATH 2110Q Multivariable Calculus", segs[0]["class"]

# course code on the incoming side too, and a class with no recordings yet still snaps
segs = app._snap_labels([{"class": "ENGR 2001", "unit": "", "topic": "", "summary": ""}],
                        [], ["AI Literacy"])
assert segs[0]["class"] == "ENGR 2001", segs[0]["class"]  # code-only strips to nothing, never snaps

# a genuinely different course sharing one word must NOT be swallowed — this is why
# _closest stays on Jaccard and not an overlap coefficient
segs = app._snap_labels([{"class": "Calculus", "unit": "", "topic": "", "summary": ""}],
                        [], ["MATH 2110Q Multivariable Calculus"])
assert segs[0]["class"] == "Calculus", segs[0]["class"]

# abbreviation is NOT the same as a course code and stays a miss — the strip fixes
# the catalogue-name case only, it does not loosen the 0.6 bar
segs = app._snap_labels([{"class": "Multivariable Calc", "unit": "", "topic": "", "summary": ""}],
                        [], ["MATH 2110Q Multivariable Calculus"])
assert segs[0]["class"] == "Multivariable Calc", segs[0]["class"]

# recordings and vault folders are both candidates, and the vault one wins on merit
segs = app._snap_labels([{"class": "Multivariable Calculus", "unit": "", "topic": "", "summary": ""}],
                        [{"class": "Physics", "unit": "", "topic": ""}], vault)
assert segs[0]["class"] == "MATH 2110Q Multivariable Calculus", segs[0]["class"]

# the vault as it stands today: the fork already happened, so both folders are
# candidates and both normalize identically. The tie must break the same way every
# run -- toward the catalogue name -- or lectures scatter between the two forever.
forked = ["MATH 2110Q Multivariable Calculus", "Multivariable Calculus"]
for order in (forked, forked[::-1]):
    segs = app._snap_labels([{"class": "Multivariable Calculus", "unit": "", "topic": "", "summary": ""}],
                            [], order)
    assert segs[0]["class"] == "MATH 2110Q Multivariable Calculus", segs[0]["class"]

# a unit/topic label that merely looks like a code is untouched by the strip
assert app._norm_words("Unit 3 Vectors") == app._norm_words("Unit 3 Vectors")
assert "vector" in app._norm_words("Unit 3 Vectors")

print("snap OK")
