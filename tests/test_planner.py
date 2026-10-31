from datetime import timedelta

import icalendar
import pytest

from duee.planner import free_slots, merge_intervals, prep_estimate
from duee.tracker import parse_dt

from .conftest import NOW


def at(h, m=0, d=0):
    return NOW.replace(hour=h, minute=m) + timedelta(days=d)


def test_prep_estimate_rules():
    full = prep_estimate("exam", 3, 1, None, 0)
    assert full["needed_minutes"] == 480 and full["needs_prep"]
    assert prep_estimate("exam", 5, 1, None, 0)["needed_minutes"] > full["needed_minutes"]
    assert not prep_estimate("exam", 3, 5, None, 0)["needs_prep"]                 # already ready
    assert prep_estimate("exam", 3, 1, None, 470)["remaining_minutes"] == 0        # already prepped
    assert not prep_estimate("event", 3, 1, None, 0)["needs_prep"]                 # plain events: no prep
    assert prep_estimate("event", 3, 1, 2, 0)["remaining_minutes"] == 120          # …unless you say so


def test_free_slots_and_merge():
    busy = [(at(10), at(11)), (at(10, 30), at(12)), (at(14), at(15))]
    assert merge_intervals(busy) == [(at(10), at(12)), (at(14), at(15))]
    assert free_slots(at(9), at(16), busy) == [(at(9), at(10)), (at(12), at(14)), (at(15), at(16))]


def _overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


def test_exam_prep_is_spread_and_avoids_busy_time(due):
    e = due.planner.add_event({"title": "EECS 370 Midterm", "kind": "exam", "at": "2026-11-05T18:00",
                               "duration": 120, "difficulty": 4, "readiness": 2})
    blocks = e["blocks"]
    assert e["prep"]["scheduled_minutes"] >= e["prep"]["remaining_minutes"]
    assert blocks[-1]["title"].startswith("Final review")
    assert len({b["day"] for b in blocks}) >= 3                                   # spaced, not crammed
    cal = [(b.start, b.end) for b in due.radar.blocks]
    for b in blocks:
        s, t = parse_dt(b["start"]), parse_dt(b["end"])
        assert NOW < s and t <= parse_dt(e["at"]) - timedelta(hours=1)
        assert 8 <= s.hour and t.hour <= 23
        assert not any(_overlaps((s, t), c) for c in cal), b["title"]
    per_day = {}
    for b in blocks:
        per_day[b["day"]] = per_day.get(b["day"], 0) + 60
    assert max(per_day.values()) <= 180


def test_prefers_focus_windows(due):
    e = due.planner.add_event({"title": "Quiz", "kind": "exam", "at": "2026-11-03T12:00", "difficulty": 2,
                               "readiness": 3})
    focus = (10, 11, 14, 15, 16, 19, 20, 21)
    by_day = {}
    for b in e["blocks"]:
        by_day.setdefault(b["day"], []).append(parse_dt(b["start"]).hour)
    # Monday has free evening focus time → only focus hours used
    assert by_day["2026-11-02"] and all(h in focus for h in by_day["2026-11-02"]), by_day
    # Tuesday only has the morning before the 11 AM cutoff → the 10 AM focus slot is taken first
    assert 10 in by_day["2026-11-03"], by_day


def test_logged_prep_shrinks_the_plan(due):
    e = due.planner.add_event({"title": "Midterm", "kind": "exam", "at": "2026-11-05T18:00", "difficulty": 3})
    before = e["prep"]["scheduled_minutes"]
    due.tracker.log("studied", minutes=180, event_id=e["id"], end=NOW - timedelta(hours=1))
    due.planner.replan_event(e["id"])
    after = due.planner.event(e["id"])["prep"]
    assert after["logged_minutes"] == 180 and after["scheduled_minutes"] <= before - 120


def test_too_late_to_prep(due):
    e = due.planner.add_event({"title": "Surprise quiz", "kind": "exam", "at": (NOW + timedelta(minutes=30)).isoformat()})
    assert e["blocks"] == [] and e["prep"]["unscheduled_minutes"] > 0


def test_readiness_update_replans(due):
    e = due.planner.add_event({"title": "Midterm", "kind": "exam", "at": "2026-11-05T18:00"})
    assert e["blocks"]
    e2 = due.planner.update_event(e["id"], {"readiness": 5})
    assert e2["blocks"] == [] and not e2["prep"]["needs_prep"]


def test_validation(due):
    with pytest.raises(ValueError):
        due.planner.add_event({"title": "", "kind": "exam", "at": "2026-11-05T18:00"})
    with pytest.raises(ValueError):
        due.planner.add_event({"title": "x", "kind": "party", "at": "2026-11-05T18:00"})
    with pytest.raises(ValueError):
        due.planner.add_habit({"name": "x", "window": "midnight"})


def test_habits_spread_and_leisure_goes_late(due):
    p = due.planner
    p.add_habit({"name": "Gaming", "minutes": 60, "per_week": 3, "window": "evening"})
    p.add_habit({"name": "Run", "minutes": 30, "per_week": 2, "window": "morning", "category": "exercise"})
    res = p.schedule_habits()
    assert res["placed"] == 5 and not res["missed"]
    blocks = p.blocks(NOW.date(), NOW.date() + timedelta(days=7))
    gaming = [b for b in blocks if b["title"] == "Gaming"]
    assert len({b["day"] for b in gaming}) == 3
    assert all(parse_dt(b["start"]).hour >= 21 for b in gaming)  # fun after the work
    runs = [b for b in blocks if b["title"] == "Run"]
    assert all(parse_dt(b["start"]).hour < 12 for b in runs)


def test_habit_already_done_is_not_rescheduled(due):
    p = due.planner
    p.add_habit({"name": "Gym", "minutes": 60, "per_week": 2, "window": "any", "category": "exercise"})
    for d in (1, 2):
        due.tracker.log("gym", minutes=60, end=NOW - timedelta(hours=d))  # Monday is the start of the week
    assert p.habits()[0]["done_this_week"] == 1  # same day counts once
    due.tracker.log("Gym", minutes=60, end=NOW + timedelta(days=1))
    assert p.schedule_habits()["placed"] == 0


def test_plan_ics_is_valid(due):
    due.planner.add_event({"title": "Midterm, part 1", "kind": "exam", "at": "2026-11-05T18:00", "duration": 120})
    cal = icalendar.Calendar.from_ical(due.planner.to_ics())
    titles = [str(c.get("SUMMARY")) for c in cal.walk("VEVENT")]
    assert "Exam: Midterm, part 1" in titles and any(t.startswith("Study:") for t in titles)
