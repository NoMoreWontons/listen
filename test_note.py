"""Smoke check: write_note combines every recording sharing a topic into one
note, refiles cleanly on relabel/delete, and merge_topics moves labels across
units. Stubs app.sb with a tiny in-memory fake — no real Supabase/network."""
import os
import datetime
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
        assert "## Transcripts" in text, text  # multi-recording layout: notes top, transcripts bottom
        assert text.index("summary r2") < text.index("## Transcripts") < text.index("alpha transcript"), text
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
        assert "## Transcripts" not in old_text  # back to single-recording layout

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
        files = sorted(p.name for p in folder.glob("*.md") if p.stem != folder.name)
        assert files == ["Mitosis.md"], files  # hub note aside, the rid-suffixed pair collapsed
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
    assert app.DIAGRAM_RULES in prompt, prompt  # summaries may draw mermaid now
    print("ok: analyze feeds user notes + lecture date into the prompt, parses exams")


def test_analyze_keeps_mermaid_in_summary():
    """analyze strips the ```json wrapper Claude sometimes adds. A summary now
    carries its own ``` fences, and the stripper is MULTILINE — so a diagram
    must survive the round trip intact, wrapper or not."""
    diagram = '```mermaid\\nflowchart TD\\n  A[\\"force\\"] --> B[\\"motion\\"]\\n```'
    body = ('{"segments":[{"class":"C","unit":"U","topic":"T","summary":'
            f'"## Key points\\n\\n- a point\\n\\n{diagram}\\n"}}],"exams":[]}}')

    def reply(text):
        def fake_create(**kw):
            return type("M", (), {
                "content": [type("T", (), {"text": text})()],
                "usage": type("U", (), {"input_tokens": 1, "output_tokens": 2})()})()
        return fake_create

    for raw in (body, f"```json\n{body}\n```"):
        app.claude.messages.create = reply(raw)
        segments, *_ = app.analyze("lecture body")
        summary = segments[0]["summary"]
        assert "```mermaid" in summary and summary.count("```") == 2, summary
        assert 'A["force"] --> B["motion"]' in summary, summary
    print("ok: a mermaid diagram survives analyze's json-fence stripping")


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


def test_write_exam_note_files_under_matching_unit():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        # a unit folder that already exists under this class -> the exam files there
        (pathlib.Path(d) / "Fall 26" / "Biology" / "Cells").mkdir(parents=True)
        exam = {
            "title": "Midterm 1", "kind": "exam", "due_date": "2026-10-01",
            "format": "50 multiple choice, no calculator",
            "topics": ["Cells", "Genetics"],
        }
        path = app.write_exam_note("Fall 26", "Biology", exam)
        # exactly one topic ("Cells") matches an existing unit folder -> unit-level Exam Prep
        expected = pathlib.Path(d) / "Fall 26" / "Biology" / "Cells" / app.PREP_DIR / "Midterm 1.md"
        assert path == str(expected), path
        text = expected.read_text(encoding="utf-8")
        assert "class: Biology" in text and "kind: exam" in text and "tags: [exam]" in text, text
        assert "# Midterm 1" in text, text
        assert "**Date:** 2026-10-01" in text, text
        assert "Class: [[Fall 26/Biology/Biology|Biology]]" in text, text  # graph anchor
        assert "**Format:** 50 multiple choice, no calculator" in text, text
        assert "- Cells" in text and "- Genetics" in text, text  # Covers stay plain bullets
    print("ok: write_exam_note files under the unit its Covers match")


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


def test_exam_covers_match_by_word_overlap():
    """Claude writes Covers as free text, not the labels it filed lectures
    under ('Relative velocity' vs the note 'Relative Motion and Velocity'), so
    entries resolve on shared words. Exact matching hit 1 of 7 on the real vault."""
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        phys = pathlib.Path(d) / "Bridge" / "Physics"
        for unit, notes in {
            "Kinematics and Motion": ["Relative Motion and Velocity", "Projectile Motion"],
            "Rotational Motion": ["Angular velocity and acceleration"],
        }.items():
            (phys / unit).mkdir(parents=True)
            for n in notes + [unit]:
                (phys / unit / f"{n}.md").write_text("x", encoding="utf-8")

        # two units -> files under the better-matching one (2 hits vs 1)
        exam = {"title": "Midterm 1", "kind": "exam", "due_date": "2026-10-01", "format": "",
                "topics": ["Relative velocity", "Projectile motion", "Angular momentum"]}
        path = pathlib.Path(app.write_exam_note("Bridge", "Physics", exam))
        assert path.parent == phys / "Kinematics and Motion" / app.PREP_DIR, path
        text = path.read_text(encoding="utf-8")
        # matching decides the folder only — the bullets keep the exam's wording, unlinked
        assert "- Relative velocity" in text and "- Angular momentum" in text, text
        assert "[[Bridge/Physics/Rotational Motion" not in text, text
    print("ok: exam Covers resolve to units by word overlap, file under the best match")


def test_wide_exam_stays_class_level():
    """A comprehensive final spanning 3+ units isn't about any single unit."""
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        bio = pathlib.Path(d) / "Fall 26" / "Biology"
        for unit in ("Cells", "Genetics", "Ecology"):
            (bio / unit).mkdir(parents=True)
        exam = {"title": "Final", "kind": "exam", "due_date": "", "format": "",
                "topics": ["Cells", "Genetics", "Ecology"]}
        path = pathlib.Path(app.write_exam_note("Fall 26", "Biology", exam))
        assert path.parent == bio / app.PREP_DIR, path
    print("ok: exam spanning 3+ units stays class-level")


def test_exams_and_prep_notes_carry_one_link_each():
    """No fan-out: an exam's Covers bullets are plain and study material carries
    no `Exam:` line, so the only edges are up the containment chain. Filing is
    unaffected — the wide exam still lands class-level, the narrow one in Cells."""
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        bio = pathlib.Path(d) / "Fall 26" / "Biology"
        for u in ("Cells", "Genetics", "Ecology"):
            (bio / u).mkdir(parents=True)
        paths = {}
        for title, topics in (("Midterm 1", ["Cells"]), ("Final", ["Cells", "Genetics", "Ecology"])):
            paths[title] = pathlib.Path(app.write_exam_note("Fall 26", "Biology", {
                "title": title, "kind": "exam", "due_date": "", "format": "", "topics": topics}))
        wide = paths["Final"].read_text(encoding="utf-8")
        assert "- Cells\n- Genetics\n- Ecology" in wide, wide  # plain, no unit wikilinks
        assert wide.count("[[") == 2, wide                     # Class: + Exam Prep: only
        assert paths["Midterm 1"].parent.parent.name == "Cells", paths  # filing still matches

        path = app.write_prep_note("Fall 26", "Biology", "Cells", "sheet.md", "# body\n")
        text = pathlib.Path(path).read_text(encoding="utf-8")
        assert "Exam: " not in text, text
        assert text.count("[[") == 1, text  # the Exam Prep hub link
    print("ok: exams and prep notes link only up the chain — no exam fan-out")


def test_legacy_exams_folder_migrates_and_refiles():
    """Old 'Exams' folders fold into Exam Prep, then class-level exam notes lift
    to the unit they cover. Covers bullets stay plain, and the prune pass strips
    the exam cross-links left in notes written before this."""
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        phys = pathlib.Path(d) / "Bridge" / "Physics"
        (phys / "Kinematics and Motion").mkdir(parents=True)
        (phys / "Kinematics and Motion" / "Projectile Motion.md").write_text("x", encoding="utf-8")
        legacy = phys / "Exams"
        legacy.mkdir()
        (legacy / "Midterm 1.md").write_text(
            "---\nclass: Physics\nkind: exam\ntags: [exam]\n---\n\n# Midterm 1\n\n"
            "## Covers\n\n- Projectile Motion\n- Vectors\n", encoding="utf-8")

        app._migrate_prep_dirs()
        app._refile_exam_notes()

        assert not legacy.exists()
        moved = phys / "Kinematics and Motion" / app.PREP_DIR / "Midterm 1.md"
        assert moved.exists(), list(phys.rglob("*.md"))
        text = moved.read_text(encoding="utf-8")
        assert "- Projectile Motion" in text and "- Vectors" in text, text  # both plain
        assert "|Projectile Motion]]" not in text, text  # no edge out to the unit

        # a note written before the prune existed keeps its Exam: line until the
        # pass runs; the hub link on the line below must survive
        old = phys / "Kinematics and Motion" / app.PREP_DIR / "quiz 2026-07-01 0900.md"
        old.write_text("---\nclass: Physics\ntags: [practice]\n---\n\n# Quiz\n"
                       "\nExam: [[Bridge/Physics/Kinematics and Motion/Exam Prep/Midterm 1|Midterm 1]]\n"
                       "\nExam Prep: [[Bridge/Physics/Kinematics and Motion/Exam Prep/Exam Prep|Exam Prep]]\n",
                       encoding="utf-8")
        # an exam whose bullets were linked before the change gets them unlinked
        linked = phys / app.PREP_DIR / "Final.md"
        linked.parent.mkdir(parents=True, exist_ok=True)
        linked.write_text("---\nclass: Physics\ntags: [exam]\n---\n\n# Final\n\n## Covers\n\n"
                          "- [[Bridge/Physics/Kinematics and Motion/Kinematics and Motion|Projectile Motion]]\n"
                          "- Vectors\n", encoding="utf-8")
        app._prune_exam_links()

        text = old.read_text(encoding="utf-8")
        assert "Exam: [[" not in text, text
        assert "Exam Prep: [[" in text, text
        text = linked.read_text(encoding="utf-8")
        assert "- Projectile Motion\n- Vectors\n" in text and "[[" not in text, text

        before = old.read_text(encoding="utf-8")
        app._migrate_prep_dirs()
        app._refile_exam_notes()
        app._prune_exam_links()  # all idempotent
        assert old.read_text(encoding="utf-8") == before  # nothing left to strip
    print("ok: legacy Exams folders migrate, exams refile, exam cross-links pruned")


def test_addendum_renders_without_resummary():
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        rows = [mkrow("r1", "Mitosis", "2026-07-01T10:00:00",
                      addendum="**2026-07-16:** prof corrected: anaphase before telophase")]
        app.sb = FakeSB(rows)
        text = pathlib.Path(app.write_note(rows[0])).read_text(encoding="utf-8")
        assert "## Corrections & additions" in text, text
        assert "anaphase before telophase" in text, text
        assert text.index("## Summary") < text.index("## Corrections") < text.index("## Transcript"), text
        assert rows[0]["summary"] == "summary r1"  # untouched — no re-summarize

        # multi-recording layout: addendum nests under its own recording's section
        rows.append(mkrow("r2", "Mitosis", "2026-07-02T10:00:00"))
        text = pathlib.Path(app.write_note(rows[0])).read_text(encoding="utf-8")
        assert "### Corrections & additions" in text, text
    print("ok: addendum renders verbatim between Summary and Transcript, summary untouched")


def test_frontmatter_parses_and_hubs_embed_timeline():
    """Frontmatter is the ONLY thing an Obsidian Bases timeline can read, and a
    single unquoted colon or quote breaks the WHOLE block — the note then drops
    out of every timeline silently. Summaries are markdown full of both."""
    import yaml
    with tempfile.TemporaryDirectory() as d:
        app.OBSIDIAN_VAULT = pathlib.Path(d)
        nasty = ('## Key points\n\n**Summary:**\n\n'
                 '- Inverse: $(F^{-1})\'(x) = 1/F\'(F^{-1}(x))$ — he said "watch the sign"\n'
                 '- second bullet\n')
        rows = [mkrow("r1", "Mitosis", "2026-07-01T10:00:00", summary=nasty)]
        app.sb = FakeSB(rows)
        note = pathlib.Path(app.write_note(rows[0]))
        fm = yaml.safe_load(note.read_text(encoding="utf-8").split("---", 2)[1])
        assert fm["topic"] == "Mitosis", fm
        # a real YAML date, not a quoted string — Bases sorts/filters it as one
        assert fm["date"] == datetime.date(2026, 7, 1), repr(fm["date"])
        assert fm["lectures"] == 1 and fm["tags"] == ["lecture", "local"], fm
        assert fm["summary"].startswith("Inverse:"), fm   # heading + bare label skipped
        assert '"watch the sign"' in fm["summary"], fm    # quotes survive
        assert "$(F^{-1})" in fm["summary"], fm           # latex subscripts survive

        unit_d = pathlib.Path(d) / "Fall 26" / "Biology" / "Cells"
        base = unit_d / app.TIMELINE
        assert 'file.inFolder("Fall 26/Biology/Cells")' in base.read_text(encoding="utf-8")
        hub = (unit_d / "Cells.md").read_text(encoding="utf-8")
        assert "![[Fall 26/Biology/Cells/Timeline.base#Timeline]]" in hub, hub

        # emptied unit: the base must go too, or the folder never rmdir's
        note.unlink()
        app._cleanup_unit_dir("Fall 26", "Biology", "Cells")
        assert not unit_d.exists(), sorted(p.name for p in unit_d.iterdir())
    print("ok: frontmatter parses as YAML, hubs embed their base, cleanup removes it")


def test_one_line_skips_recaps():
    assert app._one_line("## Review of last class\n- old stuff\n\n## Key points\n- the real gist") \
        == "the real gist"
    assert app._one_line("") == ""
    assert app._one_line("x" * 300).endswith("…")
    print("ok: _one_line skips recap sections, empty and overlong summaries")


if __name__ == "__main__":
    test_frontmatter_parses_and_hubs_embed_timeline()
    test_one_line_skips_recaps()
    test_single_recording()
    test_second_recording_joins_topic()
    test_relabel_away_and_last_one_out()
    test_empty_transcript_returns_none()
    test_merge_topics_cross_unit()
    test_legacy_rid_suffixed_files_cleaned_up()
    test_delete_recording_rewrites_shared_note()
    test_analyze_integrates_notes()
    test_analyze_keeps_mermaid_in_summary()
    test_analyze_pdf_homework_prompt()
    test_parse_exams_defensive()
    test_write_exam_note_files_under_matching_unit()
    test_write_exam_note_undated_no_format_overwrites()
    test_exam_covers_match_by_word_overlap()
    test_wide_exam_stays_class_level()
    test_exams_and_prep_notes_carry_one_link_each()
    test_legacy_exams_folder_migrates_and_refiles()
    test_addendum_renders_without_resummary()
