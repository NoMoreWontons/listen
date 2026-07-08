import app
ics = app.build_cards_ics(["2026-07-10", "2026-07-10", "2026-07-12"], "20260708T000000Z")
assert "BEGIN:VCALENDAR" in ics and ics.rstrip().endswith("END:VCALENDAR")
assert ics.count("BEGIN:VEVENT") == 2                       # two distinct dates
assert "SUMMARY:2 flashcards due" in ics                    # 2026-07-10 bucket
assert "SUMMARY:1 flashcard due" in ics                     # 2026-07-12 bucket (singular)
assert "DTSTART;VALUE=DATE:20260710" in ics
assert "UID:cards-2026-07-10@listen" in ics                 # stable per-date UID
print("test_cards_ics OK")
