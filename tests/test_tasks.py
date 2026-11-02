"""v0.4: Canvas deadlines → planned work time."""
import sqlite3
from dataclasses import replace
from datetime import timedelta

from duee.db import DB
from duee.tracker import parse_dt

from .conftest import NOW


def _tasks(due):
    return {e["title"]: e for e in due.planner.events()}


def test_sync_creates_tasks_and_skips_non_tasks(due):
    r = due.planner.sync_tasks(due.radar.items)
    t = _tasks(due)
    assert r["created"] >= 4 and r["skipped"] >= 0
    assert "Project 2" in t and "Homework 5" in t and "Quiz 8" in t
    assert not any("Office Hours" in k for k in t)
    assert not any("Homework 4" in k for k in t)  # already past
    assert t["Project 2"]["source"] == "canvas" and t["Project 2"]["read"]["summary"]
    # an all-day exam gets an 8 AM placeholder so studying ends the day before
    mid = t["Midterm Exam"]
    assert parse_dt(mid["at"]).hour == 8 and mid["kind"] == "exam"
    # work actually got scheduled, and before the deadline
    p2 = t["Project 2"]
    assert p2["prep"]["scheduled_minutes"] > 0
    assert all(parse_dt(b["end"]) <= parse_dt(p2["at"]) for b in p2["blocks"])


def test_sync_is_idempotent_and_respects_ignore(due):
    due.planner.sync_tasks(due.radar.items)
    n = len(due.planner.events())
    again = due.planner.sync_tasks(due.radar.items)
    assert again["created"] == 0 and len(due.planner.events()) == n
    quiz = _tasks(due)["Quiz 8"]
    due.planner.update_event(quiz["id"], {"ignored": True})
    assert "Quiz 8" not in _tasks(due)
    due.planner.sync_tasks(due.radar.items)
    assert "Quiz 8" not in _tasks(due)  # stays hidden
    # deleting a Canvas task hides it rather than letting sync bring it back
    hw = _tasks(due)["Homework 5"]
    due.planner.delete_event(hw["id"])
    due.planner.sync_tasks(due.radar.items)
    assert "Homework 5" not in _tasks(due)


def test_links_a_manual_twin_instead_of_duplicating(due):
    due.planner.add_event({"title": "370 midterm", "kind": "exam", "course": "EECS 370",
                           "at": (NOW.replace(hour=19) + timedelta(days=3)).isoformat(), "difficulty": 4, "readiness": 1})
    r = due.planner.sync_tasks(due.radar.items)
    exams = [e for e in due.planner.events() if e["kind"] == "exam"]
    assert r["linked"] == 1 and len(exams) == 1 and exams[0]["title"] == "370 midterm"
    assert exams[0]["uid"]
    due.planner.sync_tasks(due.radar.items)  # a second sync must not rename your entry
    assert [e["title"] for e in due.planner.events() if e["kind"] == "exam"] == ["370 midterm"]


def test_moved_due_date_follows_canvas_unless_you_edited(due):
    due.planner.sync_tasks(due.radar.items)
    p2 = _tasks(due)["Project 2"]
    item = next(i for i in due.radar.items if i.title == "Project 2")
    later = item.due + timedelta(days=2)
    moved = replace(item, due=later, uid=item.uid.split("@")[0] + "@" + later.isoformat())
    r = due.planner.sync_tasks([moved])
    assert r["updated"] == 1 and parse_dt(_tasks(due)["Project 2"]["at"]) == later

    # once you change the estimate yourself, Canvas changes don't overwrite your edits
    due.planner.update_event(p2["id"], {"prep_hours": 3})
    assert _tasks(due)["Project 2"]["user_edited"]
    even_later = later + timedelta(days=1)
    moved2 = replace(item, due=even_later, uid=item.uid.split("@")[0] + "@" + even_later.isoformat())
    due.planner.sync_tasks([moved2])
    assert parse_dt(_tasks(due)["Project 2"]["at"]) == later


def test_deep_work_gets_long_blocks_light_work_short(due):
    due.planner.sync_tasks(due.radar.items)
    t = _tasks(due)
    mins = lambda b: (parse_dt(b["end"]) - parse_dt(b["start"])).total_seconds() / 60
    p2 = t["Project 2"]
    assert p2["read"]["deep"]
    assert max(mins(b) for b in p2["blocks"]) > due.cfg.planner.light_block_minutes
    lab = t["Lab 6 Prelab"]
    if lab["blocks"]:
        assert max(mins(b) for b in lab["blocks"]) <= due.cfg.planner.light_block_minutes


def test_done_clears_future_blocks_and_match_task(due):
    due.planner.sync_tasks(due.radar.items)
    t = _tasks(due)
    assert due.planner.match_task("281 project 2") == t["Project 2"]["id"]
    assert due.planner.match_task("370 hw") == t["Homework 5"]["id"]
    assert due.planner.match_task("gym") is None
    due.planner.update_event(t["Project 2"]["id"], {"done": True})
    left = due.db.query("SELECT * FROM blocks WHERE kind='prep' AND ref_id=? AND status='planned'", (t["Project 2"]["id"],))
    assert not left


def test_logged_work_auto_links_and_shrinks_plan(due):
    due.planner.sync_tasks(due.radar.items)
    before = _tasks(due)["Project 2"]["prep"]["remaining_minutes"]
    eid = due.link_task("281 project 2")
    due.tracker.log("281 project 2", minutes=120, event_id=eid)
    assert _tasks(due)["Project 2"]["prep"]["remaining_minutes"] <= before - 100


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, title TEXT, kind TEXT, course TEXT, at TEXT, "
                "duration INTEGER, difficulty INTEGER, readiness INTEGER, prep_hours REAL, notes TEXT, "
                "done INTEGER DEFAULT 0, created_at TEXT)")
    con.execute("INSERT INTO events (title, kind, at) VALUES ('old exam', 'exam', '2026-11-05T09:00:00-05:00')")
    con.commit(); con.close()
    db = DB(path)
    row = db.one("SELECT * FROM events")
    assert row["title"] == "old exam" and row["source"] == "manual" and row["ignored"] == 0 and row["uid"] is None


def test_your_own_study_blocks_count_toward_the_task(due):
    """'Study 281 Project 2' on your calendar is credited, not double-booked, and isn't treated as a class."""
    due.planner.sync_tasks(due.radar.items)
    p2 = _tasks(due)["Project 2"]
    own = due.planner.own_blocks(p2)
    assert [b.title for b in own] == ["Study 281 Project 2"]
    assert p2["prep"]["your_calendar_minutes"] == 120
    assert any("already blocked" in w for w in p2["read"]["why"])
    # DUE-E's own blocks never overlap the one you already made
    mine = own[0]
    for b in p2["blocks"]:
        if b["status"] == "planned":
            assert parse_dt(b["end"]) <= mine.start or parse_dt(b["start"]) >= mine.end
