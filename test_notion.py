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
    # a page with no Transcript block at all (plain typed note)
    "flat": {"results": [
        {"id": "f1", "type": "paragraph",
         "paragraph": {"rich_text": [{"plain_text": "Only text."}]}, "has_children": False},
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


def test_split_transcript_from_notes():
    app.httpx.get = fake_get
    app.NOTION_TOKEN = "x"
    transcript, notes = app._notion_page_split("root")
    assert "Nested transcript body." in transcript, transcript
    assert "Nested transcript body." not in notes, notes
    assert "Intro line." in notes, notes
    # no Transcript block -> whole page is the transcript, notes empty
    transcript, notes = app._notion_page_split("flat")
    assert "Only text." in transcript and notes == "", (transcript, notes)
    print("ok: notion page splits transcript toggle from user notes")


if __name__ == "__main__":
    test_flattens_nested_text()
    test_split_transcript_from_notes()
