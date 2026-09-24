"""_json_obj salvage: a real Haiku reply (fixtures/analyze_stray_quotes.json)
that filed a whole lecture into Unsorted because its summary prose contained
unescaped double quotes. Also checks the repair is a no-op on valid JSON."""
import json
import os
import pathlib

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app

raw = (pathlib.Path(__file__).parent / "fixtures" / "analyze_stray_quotes.json").read_text(encoding="utf-8")

# the input really is broken -- if this ever passes, the fixture stopped being a regression
try:
    json.loads(raw)
    raise AssertionError("fixture parses cleanly; it no longer reproduces the bug")
except json.JSONDecodeError:
    pass

segs = app._parse_segments(raw)
assert len(segs) == 1, segs
assert segs[0]["class"] == "ME 1100 Tech Comms for Engineers", segs[0]["class"]
assert segs[0]["unit"] == "Written Communication", segs[0]["unit"]
assert segs[0]["topic"] == "Effective email structure and strategy", segs[0]["topic"]
assert "Unsorted" not in json.dumps(segs)
assert '"Project A117 update"' in segs[0]["summary"]   # the quotes survive as prose
assert "```mermaid" in segs[0]["summary"]              # diagram fence intact
assert app._parse_exams(raw) == []

# valid JSON is untouched, quotes in prose and all
good = json.dumps({"segments": [{"class": "C", "unit": "U", "topic": "T",
                                 "summary": 'he said "hi" then x'}], "exams": []})
assert app._escape_stray_quotes(good) == good
assert app._json_obj(good)["segments"][0]["summary"] == 'he said "hi" then x'

# no JSON at all still yields the Unsorted fallback
assert app._parse_segments("sorry, I can't help with that")[0]["class"] == "Unsorted"

print("json salvage OK")
