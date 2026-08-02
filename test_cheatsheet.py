"""build_cheatsheet: the prompt it sends and the markdown it hands back.
Stubs app.claude — no network. Run: python test_cheatsheet.py"""
import os

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app

SHEET = """# Calc I — Unit 1

| Rule | Formula |
| --- | --- |
| Power | $\\frac{d}{dx}x^n = nx^{n-1}$ |

```mermaid
flowchart TD
  A["is it a product?"] --> B["product rule"]
```
"""

sent = {}


def fake_msg(**kw):
    sent.update(kw)
    return type("M", (), {"content": [type("T", (), {"text": SHEET})()]})()


app.claude.messages.create = fake_msg

rows = [
    {"topic": "Limits", "summary": "A limit is the value f approaches."},
    {"topic": "Derivatives", "summary": "Rate of change."},
    {"topic": "Empty", "summary": ""},          # skipped
]
md = app.build_cheatsheet(rows, "Calc I", "Unit 1")

# the model's markdown passes through untouched — no fence-stripping regex, or
# every ```mermaid block in the sheet would be shredded
assert md.startswith("# Calc I — Unit 1"), md[:40]
assert "```mermaid" in md and 'A["is it a product?"] --> B["product rule"]' in md, md

prompt = sent["messages"][0]["content"]
assert "A limit is the value f approaches." in prompt and "Rate of change." in prompt
assert "## Empty" not in prompt                  # blank summary contributes nothing
assert "# Calc I — Unit 1" in prompt             # title it should open with
assert app.DIAGRAM_RULES in prompt               # mermaid syntax rules travel with it


# Sonnet leads with a ThinkingBlock, which has no .text — content[0] would raise
class Block:
    def __init__(self, type, text=None):
        self.type, self.text = type, text


thinking = type("M", (), {"content": [Block("thinking"), Block("text", "  hi  ")]})()
assert app._text(thinking) == "hi"

print("test_cheatsheet OK")
