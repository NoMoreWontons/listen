import app
rows = [
    {"topic": "Limits", "summary": "A limit is the value f approaches."},
    {"topic": "Derivatives", "summary": "Rate of change."},
    {"topic": "Empty", "summary": ""},          # skipped
]
md = app.build_cheatsheet(rows, "Calc I", "Unit 1")
assert md.startswith("# Calc I"), md[:40]
assert "Unit 1" in md
assert "## Limits" in md and "approaches" in md
assert "## Derivatives" in md
assert "## Empty" not in md                      # blank summary omitted
print("test_cheatsheet OK")
