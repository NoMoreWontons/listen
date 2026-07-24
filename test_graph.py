"""write_graph_config: Obsidian colorGroups from the vault's folder tree.
Run: python test_graph.py"""
import json
import os
import pathlib
import tempfile

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app


def build(d, tree):
    """tree: {sem: {cls: [unit, ...]}} — makes the folders write_graph_config reads."""
    for sem, classes in tree.items():
        for cls, units in classes.items():
            for u in units:
                (pathlib.Path(d) / sem / cls / u).mkdir(parents=True, exist_ok=True)


def groups_of(d):
    app.OBSIDIAN_VAULT = pathlib.Path(d)
    app.write_graph_config()
    cfg = json.loads((pathlib.Path(d) / ".obsidian" / "graph.json").read_text(encoding="utf-8"))
    return cfg["colorGroups"]


def test_prep_dir_is_not_a_unit():
    """Exam Prep holds study material, not lectures. Counting it as a unit gave
    it a hub-note query for a file that never exists, and — worse — shifted the
    hue of every real unit in the class, so colors moved whenever a quiz or cheat
    sheet created the folder."""
    with tempfile.TemporaryDirectory() as d:
        build(d, {"Bridge": {"Physics": ["Kinematics", "Rotational Motion"]}})
        before = groups_of(d)

        # a generated cheat sheet creates Exam Prep folders at both levels
        (pathlib.Path(d) / "Bridge" / "Physics" / app.PREP_DIR).mkdir()
        (pathlib.Path(d) / "Bridge" / "Physics" / "Kinematics" / app.PREP_DIR).mkdir()
        after = groups_of(d)

        assert before == after, "Exam Prep folders changed the palette"
        queries = [g["query"] for g in after]
        assert not any(app.PREP_DIR in q for q in queries), queries
    print("ok: Exam Prep folders are not units — no query, no hue shift")


def test_groups_cover_units_topics_classes_semesters():
    with tempfile.TemporaryDirectory() as d:
        build(d, {"Bridge": {"Physics": ["Kinematics"], "Calculus": ["Derivatives"]}})
        queries = [g["query"] for g in groups_of(d)]
        # unit hub note, unit's topic notes, class hub, semester hub
        assert 'path:"Bridge/Physics/Kinematics/Kinematics.md"' in queries, queries
        assert 'path:"Bridge/Physics/Kinematics"' in queries, queries
        assert 'path:"Bridge/Physics"' in queries, queries
        assert 'path:"Bridge"' in queries, queries
        # every class gets a distinct hue, so no two class hubs share a color
        hubs = {g["query"]: g["color"]["rgb"] for g in groups_of(d)}
        assert hubs['path:"Bridge/Physics"'] != hubs['path:"Bridge/Calculus"']
    print("ok: color groups cover unit hubs, topics, class hubs, semesters")


def test_other_graph_settings_preserved():
    """Only colorGroups is regenerated — the user's zoom/forces stay put."""
    with tempfile.TemporaryDirectory() as d:
        build(d, {"Bridge": {"Physics": ["Kinematics"]}})
        cfg_p = pathlib.Path(d) / ".obsidian" / "graph.json"
        cfg_p.parent.mkdir(parents=True)
        cfg_p.write_text(json.dumps({"scale": 0.379, "linkDistance": 250, "colorGroups": []}),
                         encoding="utf-8")
        groups_of(d)
        cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
        assert cfg["scale"] == 0.379 and cfg["linkDistance"] == 250, cfg
        assert cfg["colorGroups"], cfg
    print("ok: regeneration leaves other graph.json settings untouched")


if __name__ == "__main__":
    test_prep_dir_is_not_a_unit()
    test_groups_cover_units_topics_classes_semesters()
    test_other_graph_settings_preserved()
    print("test_graph: OK")
