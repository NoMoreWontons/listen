"""Smoke check: _notion_block_text flattens nested meeting-note blocks to text."""
import os

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app

# Fake Notion API: a page with a paragraph and a toggle whose transcript text
# lives one level deeper (has_children), which is how meeting notes nest.
PAGES = {
    "root": {"results": [
        {"id": "p1", "type": "paragraph",
         "paragraph": {"rich_text": [{"plain_text": "Intro line."}]}, "has_children": False},
        {"id": "t1", "type": "toggle",
         "toggle": {"rich_text": [{"plain_text": "Transcript"}]}, "has_children": True},
    ], "has_more": False},
    "t1": {"results": [
        {"id": "c1", "type": "paragraph",
         "paragraph": {"rich_text": [{"plain_text": "Nested transcript body."}]}, "has_children": False},
    ], "has_more": False},
}


class FakeResp:
    def __init__(self, data): self._d = data
    def raise_for_status(self): pass
    def json(self): return self._d


def fake_get(url, headers=None, params=None, timeout=None):
    block_id = url.rstrip("/").split("/")[-2]  # .../blocks/<id>/children
    return FakeResp(PAGES[block_id])


def test_flattens_nested_text():
    app.httpx.get = fake_get
    app.NOTION_TOKEN = "x"
    text = app._notion_block_text("root").strip()
    assert "Intro line." in text, text
    assert "Nested transcript body." in text, text
    print("ok: notion block text flattens nested transcript")


if __name__ == "__main__":
    test_flattens_nested_text()
