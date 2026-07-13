"""Smoke check: write_note combines every recording sharing a topic into one
note, refiles cleanly on relabel/delete, and merge_topics moves labels across
units. Stubs app.sb with a tiny in-memory fake — no real Supabase/network."""
import os
import tempfile
import pathlib

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app

# ensure_hubs would otherwise make real (fake-keyed) network calls to Claude;
# write_note swallows its failures, so make them fail fast instead of hanging.
app.claude.messages.create = lambda **kw: (_ for _ in ()).throw(RuntimeError("no network in tests"))


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    """Chainable stand-in for supabase-py's query builder, backed by a shared
    in-memory list of row dicts. Covers the handful of calls write_note /
    _group_rows / _refile_group / _set / merge_topics / delete_recording
    actually make: select/update/delete/eq/neq/order/single/execute."""
    def __init__(self, rows):
        self.rows = rows
        self.filters = []
        self.mode = "select"
        self.payload = None

    def select(self, *a, **k):
        return self

    def update(self, payload):
        self.mode, self.payload = "update", payload
        return self

    def delete(self):
        self.mode = "delete"
        return self

    def eq(self, k, v):
        self.filters.append(("eq", k, v))
        return self

    def neq(self, k, v):
        self.filters.append(("neq", k, v))
        return self

    def order(self, *a, **k):
        return self

    def single(self):
        return self

    def _match(self, r):
        for op, k, v in self.filters:
            if op == "eq" and r.get(k) != v:
                return False
            if op == "neq" and r.get(k) == v:
                return False
        return True

    def execute(self):
        matched = [r for r in self.rows if self._match(r)]
        if self.mode == "update":
            for r in matched:
                r.update(self.payload)
        elif self.mode == "delete":
            for r in matched:
                self.rows.remove(r)
        return FakeResult(matched)


class FakeSB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        assert name == "recordings"
        return FakeQuery(self.rows)


def mkrow(id, topic, created_at, transcript="hello", **kw):
    row = {
        "id": id, "title": f"L-{id}", "created_at": created_at,
        "transcript": transcript, "summary": f"summary {id}",
        "semester": "Fall 26", "class": "Biology", "unit": "Cells",
        "topic": topic, "obsidian_path": None, "source": "local",
    }
    row.update(kw)
    return row


def test_single_recording():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        rows = [mkrow("r1", "Mitosis", "2026-07-01T10:00:00")]
        app.sb = FakeSB(rows)
        path = app.write_note(rows[0])
        expected = pathlib.Path(d) / "Fall 26" / "Biology" / "Cells" / "Mitosis.md"
        assert path == str(expected), path
        text = expected.read_text(encoding="utf-8")
        assert "## Summary" in text and "## Transcript" in text, text
        assert "### Summary" not in text, text  # single recording keeps old-style layout
        assert rows[0]["obsidian_path"] == str(expected)
    print("ok: single recording -> old-style layout at expected path")


def test_second_recording_joins_topic():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        rows = [mkrow("r1", "Mitosis", "2026-07-01T10:00:00", transcript="alpha transcript")]
        app.sb = FakeSB(rows)
        p1 = app.write_note(rows[0])

        rows.append(mkrow("r2", "Mitosis", "2026-07-03T10:00:00", transcript="beta transcript"))
        p2 = app.write_note(rows[1])

        assert p1 == p2, (p1, p2)
        text = pathlib.Path(p2).read_text(encoding="utf-8")
        assert "alpha transcript" in text and "beta transcript" in text, text
        assert "### Summary" in text, text  # multi-recording layout
        assert rows[0]["obsidian_path"] == p2
        assert rows[1]["obsidian_path"] == p2
    print("ok: second recording on same topic joins one combined file")


def test_relabel_away_and_last_one_out():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        rows = [
            mkrow("r1", "Mitosis", "2026-07-01T10:00:00", transcript="alpha transcript"),
            mkrow("r2", "Mitosis", "2026-07-03T10:00:00", transcript="beta transcript"),
        ]
        app.sb = FakeSB(rows)
        app.write_note(rows[0])
        shared_path = app.write_note(rows[1])

        # relabel r2 away to its own topic
        rows[1]["topic"] = "Meiosis"
        new_path = app.write_note(rows[1])
        assert new_path != shared_path
        assert pathlib.Path(new_path).exists()
        assert rows[1]["obsidian_path"] == new_path

        # old combined file survives, rewritten to hold only r1
        assert pathlib.Path(shared_path).exists(), "shared note should survive while r1 still uses it"
        old_text = pathlib.Path(shared_path).read_text(encoding="utf-8")
        assert "alpha transcript" in old_text and "beta transcript" not in old_text, old_text
        assert "### Summary" not in old_text  # back to single-recording layout

        # the last remaining recording (r1) also leaves
        rows[0]["topic"] = "Cytokinesis"
        app.write_note(rows[0])
        assert not pathlib.Path(shared_path).exists(), "orphaned combined note should be removed"
    print("ok: relabeling away rewrites the shared note, removes it once empty")


def test_empty_transcript_returns_none():
    app.sb = FakeSB([])
    row = mkrow("r1", "Mitosis", "2026-07-01T10:00:00", transcript="   ")
    assert app.write_note(row) is None
    print("ok: empty transcript -> None")


def test_merge_topics_cross_unit():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        rows = [
            mkrow("r1", "Mitosis", "2026-07-01T10:00:00", unit="Cells", transcript="alpha"),
            mkrow("r2", "Mitosis", "2026-07-02T10:00:00", unit="Cells", transcript="beta"),
        ]
        app.sb = FakeSB(rows)
        app.write_note(rows[0])
        app.write_note(rows[1])

        same = app.merge_topics({
            "semester": "Fall 26", "class": "Biology",
            "from_unit": "Cells", "from_topic": "Mitosis",
            "to_unit": "Cells", "to_topic": "Mitosis",
        })
        assert same == {"ok": False, "error": "bad merge args"}, same

        result = app.merge_topics({
            "semester": "Fall 26", "class": "Biology",
            "from_unit": "Cells", "from_topic": "Mitosis",
            "to_unit": "Division", "to_topic": "Cell division",
        })
        assert result == {"ok": True, "moved": 2}, result
        assert rows[0]["unit"] == "Division" and rows[0]["topic"] == "Cell division"
        assert rows[1]["unit"] == "Division" and rows[1]["topic"] == "Cell division"

        dest = pathlib.Path(d) / "Fall 26" / "Biology" / "Division" / "Cell division.md"
        assert dest.exists()
        text = dest.read_text(encoding="utf-8")
        assert "alpha" in text and "beta" in text, text
        assert rows[0]["obsidian_path"] == str(dest)
        assert rows[1]["obsidian_path"] == str(dest)
    print("ok: merge_topics moves labels cross-unit into one combined note; rejects no-op merge")


def test_legacy_rid_suffixed_files_cleaned_up():
    """Pre-fix vaults have 'Topic (rid6).md' files per recording — one refile
    of the topic must collapse them into the single combined note."""
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        folder = pathlib.Path(d) / "Fall 26" / "Biology" / "Cells"
        folder.mkdir(parents=True)
        s1, s2 = folder / "Mitosis (r1x).md", folder / "Mitosis (r2x).md"
        s1.write_text("old1", encoding="utf-8")
        s2.write_text("old2", encoding="utf-8")
        rows = [
            mkrow("r1", "Mitosis", "2026-07-01T10:00:00", transcript="alpha", obsidian_path=str(s1)),
            mkrow("r2", "Mitosis", "2026-07-02T10:00:00", transcript="beta", obsidian_path=str(s2)),
        ]
        app.sb = FakeSB(rows)
        path = app.write_note(rows[0])
        files = sorted(p.name for p in folder.glob("*.md"))
        assert files == ["Mitosis.md"], files
        text = pathlib.Path(path).read_text(encoding="utf-8")
        assert "alpha" in text and "beta" in text, text
        assert rows[0]["obsidian_path"] == path and rows[1]["obsidian_path"] == path
    print("ok: legacy rid-suffixed files collapse into one combined note")


def test_delete_recording_rewrites_shared_note():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        rows = [
            mkrow("r1", "Mitosis", "2026-07-01T10:00:00", transcript="alpha"),
            mkrow("r2", "Mitosis", "2026-07-02T10:00:00", transcript="beta"),
        ]
        app.sb = FakeSB(rows)
        app.write_note(rows[0])
        shared = app.write_note(rows[1])

        app.delete_recording("r1")
        assert pathlib.Path(shared).exists(), "note should survive while r2 still uses it"
        text = pathlib.Path(shared).read_text(encoding="utf-8")
        assert "beta" in text and "alpha" not in text, text

        app.delete_recording("r2")
        assert not pathlib.Path(shared).exists(), "note should be removed once nobody uses it"
    print("ok: delete_recording rewrites/removes the shared note as members leave")


def test_analyze_integrates_notes():
    captured = {}

    class FakeMsg:
        content = [type("T", (), {
            "text": '{"segments":[{"class":"C","unit":"U","topic":"T","summary":"S"}],'
                    '"exams":[{"title":"Midterm","due_date":"2026-10-01","kind":"exam"}]}'})()]
        usage = type("U", (), {"input_tokens": 1, "output_tokens": 2})()

    def fake_create(**kw):
        captured.clear()
        captured.update(kw)
        return FakeMsg()

    app.claude.messages.create = fake_create
    segments, exams, *_ = app.analyze("lecture body", "watch slide 12", "2026-09-20T10:00:00")
    prompt = captured["messages"][0]["content"]
    assert "watch slide 12" in prompt, prompt
    assert "2026-09-20" in prompt, prompt  # lecture date lets the model resolve relative dates
    assert "recap is NEVER its own segment" in prompt, prompt
    assert "Review of last class" in prompt, prompt
    assert segments[0]["summary"] == "S", segments
    assert exams == [{"title": "Midterm", "due_date": "2026-10-01", "kind": "exam"}], exams
    app.analyze("lecture body")  # no notes, no created_at -> no notes/date preamble
    assert "their own notes" not in captured["messages"][0]["content"]
    assert "recorded on" not in captured["messages"][0]["content"]
    print("ok: analyze feeds user notes + lecture date into the prompt, parses exams")


def test_analyze_pdf_homework_prompt():
    captured = {}

    class FakeMsg:
        content = [type("T", (), {
            "text": '{"class":"C","unit":"U","topic":"T","semester":"",'
                    '"key_points":"### Problem 1","summary":"- overview"}'})()]
        usage = type("U", (), {"input_tokens": 1, "output_tokens": 2})()

    def fake_create(**kw):
        captured.clear()
        captured.update(kw)
        return FakeMsg()

    app.claude.messages.create = fake_create
    app.analyze_pdf(b"%PDF-fake", homework=True)
    prompt = captured["messages"][0]["content"][1]["text"]
    assert "homework assignment" in prompt, prompt
    assert "### Problem" in prompt, prompt
    assert "**Answer:**" in prompt, prompt
    app.analyze_pdf(b"%PDF-fake")  # default keeps the course-material framing
    prompt = captured["messages"][0]["content"][1]["text"]
    assert "course material" in prompt, prompt
    assert "Worked examples" in prompt, prompt
    print("ok: analyze_pdf homework flag swaps in the per-problem write-up prompt")


def test_parse_exams_defensive():
    assert app._parse_exams('{"segments":[]}') == []  # missing key
    assert app._parse_exams('{"exams":"not a list"}') == []  # wrong type
    assert app._parse_exams('not json at all') == []  # unparseable
    assert app._parse_exams('{"exams":[{"title":"Midterm"}]}') == [{"title": "Midterm"}]
    print("ok: _parse_exams parses missing/malformed exams defensively")


def test_write_exam_note_links_matching_units():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        # a unit folder that already exists under this class -> Covers should link it
        (pathlib.Path(d) / "Fall 26" / "Biology" / "Cells").mkdir(parents=True)
        exam = {
            "title": "Midterm 1", "kind": "exam", "due_date": "2026-10-01",
            "format": "50 multiple choice, no calculator",
            "topics": ["Cells", "Genetics"],
        }
        path = app.write_exam_note("Fall 26", "Biology", exam)
        expected = pathlib.Path(d) / "Fall 26" / "Biology" / "Exams" / "Midterm 1.md"
        assert path == str(expected), path
        text = expected.read_text(encoding="utf-8")
        assert "class: Biology" in text and "kind: exam" in text and "tags: [exam]" in text, text
        assert "# Midterm 1" in text, text
        assert "**Date:** 2026-10-01" in text, text
        assert "Class: [[Fall 26/Biology/Biology|Biology]]" in text, text  # graph anchor
        assert "**Format:** 50 multiple choice, no calculator" in text, text
        assert "- [[Fall 26/Biology/Cells/Cells|Cells]]" in text, text  # matches existing unit
        assert "- Genetics" in text, text  # no matching folder -> plain bullet
    print("ok: write_exam_note links Covers entries that match existing unit folders")


def test_write_exam_note_undated_no_format_overwrites():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        exam = {"title": "Pop quiz", "kind": "quiz", "due_date": "", "format": "", "topics": []}
        path = app.write_exam_note("Fall 26", "Biology", exam)
        text = pathlib.Path(path).read_text(encoding="utf-8")
        assert "**Date:** TBA" in text, text
        assert "**Format:**" not in text, text
        assert "## Covers" not in text, text

        # re-detecting the same exam overwrites the same file (idempotent)
        exam2 = {"title": "Pop quiz", "kind": "quiz", "due_date": "2026-11-03", "format": "", "topics": []}
        path2 = app.write_exam_note("Fall 26", "Biology", exam2)
        assert path2 == path
        text2 = pathlib.Path(path).read_text(encoding="utf-8")
        assert "**Date:** 2026-11-03" in text2, text2
    print("ok: write_exam_note handles missing date/format/topics, overwrites on re-detection")


if __name__ == "__main__":
    test_single_recording()
    test_second_recording_joins_topic()
    test_relabel_away_and_last_one_out()
    test_empty_transcript_returns_none()
    test_merge_topics_cross_unit()
    test_legacy_rid_suffixed_files_cleaned_up()
    test_delete_recording_rewrites_shared_note()
    test_analyze_integrates_notes()
    test_analyze_pdf_homework_prompt()
    test_parse_exams_defensive()
    test_write_exam_note_links_matching_units()
    test_write_exam_note_undated_no_format_overwrites()
