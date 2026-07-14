"""One-off: rewrite old summaries/notes into the new readable + LaTeX format.

- homework rows with a stored PDF: full re-analysis (new per-problem write-up)
- other PDF rows with a stored PDF: re-analysis with the new course-material prompt
- audio rows (and PDFs whose file is gone): haiku pass rewriting the existing
  summary — math to LaTeX, content unchanged
Labels are never touched; label suggestions from re-analysis are ignored, and
raw audio transcripts are left alone. Reuses app.py's client/prompts/note code.

Run:  .venv/Scripts/python.exe backfill.py [--dry-run]
"""
import sys

import app

DRY = "--dry-run" in sys.argv


def rewrite_md(text):
    msg = app.claude.messages.create(
        model="claude-haiku-4-5", max_tokens=4000,
        messages=[{"role": "user", "content": (
            "Rewrite this markdown to be more readable: keep every fact and the "
            "overall structure, but write ALL math notation as LaTeX ($...$ "
            "inline, $$...$$ for display equations) instead of plain-text "
            "approximations like x^2, sqrt(), or unicode fractions. Return ONLY "
            "the rewritten markdown, no preamble.\n\n" + text)}],
    )
    return msg.content[0].text.strip(), msg.usage.input_tokens, msg.usage.output_tokens


def main():
    rows = (app.sb.table("recordings").select("*").eq("status", "done")
            .order("created_at").execute().data)
    tin = tout = done = failed = 0
    groups = set()  # label tuples whose combined note needs a rewrite
    for r in rows:
        if not (r.get("summary") or "").strip():
            continue
        rid, src = r["id"], r.get("source") or "local"
        pdf = app.pdf_path(rid)
        plan = f"re-analyze {src} PDF" if pdf.exists() else "latexify summary"
        print(f"{r.get('created_at', '')[:10]}  {src:12} {r.get('topic') or r.get('title') or rid}: {plan}")
        if DRY:
            continue
        try:
            if pdf.exists():
                # syllabus=False even for syllabi: assignments were already
                # extracted on first upload; re-asking would duplicate them
                summary, _, _, _, _, key_points, _, i, o = app.analyze_pdf(
                    pdf.read_bytes(), r.get("notes") or "",
                    homework=src == "homework")
                app._set(rid, summary=summary, transcript=key_points)
            else:
                summary, i, o = rewrite_md(r["summary"])
                app._set(rid, summary=summary)
            tin, tout, done = tin + i, tout + o, done + 1
            groups.add((r.get("semester"), r.get("class"), r.get("unit"), r.get("topic")))
        except Exception as e:
            failed += 1
            print(f"  FAILED, row untouched: {e}")
    for g in sorted(groups, key=lambda t: tuple(x or "" for x in t)):
        app._refile_group(*g)
        print(f"note rebuilt: {'/'.join(x or '?' for x in g)}")
    print(f"\n{done} rewritten, {failed} failed, {len(groups)} notes rebuilt, "
          f"{tin} in / {tout} out tokens{' (dry run — nothing written)' if DRY else ''}")


if __name__ == "__main__":
    main()
