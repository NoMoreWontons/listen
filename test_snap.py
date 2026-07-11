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

print("snap OK")
