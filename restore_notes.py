"""One-off recovery: back up every Supabase table to local JSON, then write any
lecture note that's missing from the vault. Read-only against Supabase; never
overwrites or deletes an existing vault file.

Run:  .venv/Scripts/python.exe restore_notes.py [--dry-run]
"""
import datetime
import json
import os
import pathlib
import sys

os.environ.setdefault("ANTHROPIC_API_KEY", "unused-by-restore")  # app.py builds a client at import
import app

DRY = "--dry-run" in sys.argv


def backup():
    out = pathlib.Path.home() / f"listen-backup-{datetime.date.today()}"
    out.mkdir(exist_ok=True)
    for table in ("recordings", "assignments", "quizzes", "cards"):
        rows = app.sb.table(table).select("*").execute().data
        (out / f"{table}.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"backup: {table} -> {len(rows)} rows")
    return out


def main():
    print("backup dir:", backup())
    rows = (app.sb.table("recordings").select("semester,class,unit,topic")
            .eq("status", "done").execute().data)
    groups = {(r["semester"], r["class"], r["unit"], r["topic"]) for r in rows}
    written = skipped = 0
    for sem, cls, unit, topic in sorted(groups, key=lambda g: tuple(x or "" for x in g)):
        dest = app.OBSIDIAN_VAULT / app._slug(sem) / app._slug(cls) / app._slug(unit) / f"{app._slug(topic)}.md"
        # the vault was hand-reorganized (units renamed/merged), so a note that
        # moved to another unit folder of the same class counts as present
        cls_dir = app.OBSIDIAN_VAULT / app._slug(sem) / app._slug(cls)
        if dest.exists() or any(cls_dir.rglob(dest.name)):
            skipped += 1
            continue
        group = app._group_rows(sem, cls, unit, topic)
        if not group:
            continue
        print(("would write " if DRY else "write ") + str(dest.relative_to(app.OBSIDIAN_VAULT)))
        if not DRY:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(app._note_md(group), encoding="utf-8")
        written += 1
    print(f"\n{written} notes {'to write' if DRY else 'written'}, {skipped} already in vault")


if __name__ == "__main__":
    main()
