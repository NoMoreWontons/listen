"""The weekly timetable decides which class a lecture belongs to.

Given only a list of course names, Claude re-guesses every lecture from scratch:
a session on dot products, cross products and determinants got filed under
'ENGR 3400 Engineering Data Analysis Tech' instead of 'MATH 2110Q Multivariable
Calculus', because vectors and determinants fit a data-analysis course as well
as they fit multivariable calculus. Both classes were in the list; nothing told
it the 15:30 Wednesday slot had been multivariable calculus all semester.
_snap_labels cannot recover from that -- the wrong class was an exact match, so
it snapped to itself and the unit and topic snapped inside it.

Pure: no Supabase, no Claude, no network, no clock.
"""
import datetime
import os

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import app

MATH = "MATH 2110Q Multivariable Calculus"
ENGR = "ENGR 3400 Engineering Data Analysis Tech"
ME = "ME 1100 Tech Comms for Engineers"


def at(day, hhmm, klass, source="local", semester="Fall 26"):
    """A row as /recordings stores it: an aware UTC timestamp, like Supabase
    hands back. _slot_class converts to local, so the test must not assume the
    machine runs in UTC -- build the stamp from a local wall time and convert."""
    h, m = divmod(hhmm, 100)
    local = datetime.datetime(2026, 9, day, h, m).astimezone()
    return {"created_at": local.astimezone(datetime.timezone.utc).isoformat(),
            "class": klass, "source": source, "semester": semester}


def slot(day, hhmm, rows, semester="Fall 26"):
    return app._slot_class(at(day, hhmm, "")["created_at"], rows, semester)


def test_the_real_miss():
    """Mon 08-31 15:30 was multivariable calculus. Wed 09-02 15:30 is the same
    class -- which is exactly what the model got wrong."""
    # Sep 7 is a Monday and Sep 9 the Wednesday after it -- the same shape as the
    # Aug 31 -> Sep 2 pair that got misfiled, and matching on weekday would miss it
    rows = [at(7, 1530, MATH), at(7, 1530, MATH),          # one lecture, split in two
            at(9, 1046, ME), at(8, 1059, "ENGR 2001 AI Literacy")]
    assert slot(9, 1530, rows) == MATH, slot(9, 1530, rows)
    print("ok  a different weekday in the same time slot still identifies the class")


def test_ignores_uploads():
    """A syllabus/upload row's created_at is when the file was sent, not when a
    class met -- letting those vote would file lectures by upload time."""
    rows = [at(2, 1530, ENGR, source="syllabus"),
            at(2, 1532, ENGR, source="upload_audio"),
            at(2, 1529, ENGR, source="transcript")]
    assert slot(2, 1530, rows) is None, "upload timestamps are not class times"
    rows.append(at(2, 1530, MATH))
    assert slot(2, 1530, rows) == MATH
    print("ok  only live recordings count toward a slot")


def test_late_start_still_matches():
    """Record is pressed at the bell or a bit after, never before. The slack is
    wider on the class's own weekday: across days a timetable repeats at the
    same clock time, so a loose window there only invites collisions."""
    rows = [at(1, 1530, MATH)]                 # day 1 and day 8 are both Tuesdays
    assert slot(8, 1538, rows) == MATH, "eight minutes late is the same class"
    assert slot(8, 1600, rows) == MATH, "half an hour is still inside the window"
    assert slot(8, 1601, rows) is None, "beyond the window, no guess"
    assert slot(3, 1538, rows) == MATH, "the next lecture of the week, started late"
    assert slot(3, 1545, rows) is None, "another weekday gets a tighter window"
    print("ok  a late start matches, wider on the class's own weekday")


def test_nearest_start_wins():
    """Back-to-back classes: 15:30 and 16:00 are both within tolerance of each
    other, so requiring a single unambiguous name would give up on both."""
    rows = [at(1, 1530, MATH), at(1, 1600, ENGR)]
    assert slot(8, 1530, rows) == MATH
    assert slot(8, 1600, rows) == ENGR
    print("ok  the nearer start wins instead of the pair cancelling out")


def test_a_near_miss_on_another_day_stays_quiet():
    """From the real history: ENGR 2001 met Tuesday 10:59 and ME 1100 Wednesday
    10:46. Thirteen minutes apart on different days is two classes, not a late
    start -- and with no Wednesday lecture on record yet, the honest answer is
    no hint rather than the neighbouring class."""
    rows = [at(1, 1059, "ENGR 2001 AI Literacy")]     # day 1 = Tuesday
    assert slot(2, 1046, rows) is None                # day 2 = Wednesday
    print("ok  a near miss on another weekday produces no hint")


def test_weekday_breaks_a_tie():
    """Two classes sharing a start time on different days: the weekday decides."""
    rows = [at(1, 1530, MATH), at(2, 1530, ENGR)]   # day 1 vs day 2 = different weekdays
    assert slot(8, 1530, rows) == MATH, "same weekday as day 1"
    assert slot(9, 1530, rows) == ENGR, "same weekday as day 2"
    print("ok  the weekday disambiguates two classes in one time slot")


def test_last_semester_does_not_vote():
    """Replaying this over the real recording history caught it: a July physics
    lecture at 14:46 hinted 'Physics' for a September communications lecture in
    the same slot. Timetables are rebuilt every term."""
    rows = [at(7, 1446, "Physics", semester="Summer 26")]
    assert slot(9, 1446, rows) is None, "last term's slot must not name this term's class"
    assert slot(9, 1446, rows, semester="Summer 26") == "Physics", "within a term it still works"
    assert app._slot_class(at(9, 1446, "")["created_at"], rows) == "Physics", \
        "no semester asked for = no semester filter"
    print("ok  a slot only votes inside its own semester")


def test_ambiguous_gives_up():
    """No hint beats a wrong one: the model still has the class list and the
    transcript, and a confident wrong slot would override both."""
    rows = [at(1, 1530, MATH), at(1, 1530, ENGR)]  # same day, same time, two classes
    assert slot(8, 1530, rows) is None
    assert slot(1, 900, []) is None, "nothing recorded yet"
    assert app._slot_class(None, rows) is None
    assert app._slot_class("not a timestamp", rows) is None
    assert slot(1, 1530, [{"class": MATH, "source": "local"}]) is None, "row with no timestamp"
    assert slot(1, 1530, [at(1, 1530, "")]) is None, "unlabelled row votes for nothing"
    print("ok  ties and junk rows produce no hint at all")


def test_hint_reaches_the_prompt():
    """The helper is only worth anything if analyze() actually says it."""
    import inspect
    src = inspect.getsource(app.analyze)
    assert "slot_class" in inspect.signature(app.analyze).parameters
    assert "slot_part" in src and "+ slot_part" in src, "built but never sent"
    assert "unless the content plainly" in src, \
        "it must read as a default the transcript can override, not a hard rule"
    assert "_slot_class(created_at, done, semester)" in inspect.getsource(app.finalize), \
        "finalize must scope the slot to the semester it just resolved"
    print("ok  the hint is passed to analyze and worded as an overridable default")


if __name__ == "__main__":
    test_the_real_miss()
    test_ignores_uploads()
    test_late_start_still_matches()
    test_nearest_start_wins()
    test_a_near_miss_on_another_day_stays_quiet()
    test_weekday_breaks_a_tie()
    test_last_semester_does_not_vote()
    test_ambiguous_gives_up()
    test_hint_reaches_the_prompt()
    print("\nall slot checks passed")
