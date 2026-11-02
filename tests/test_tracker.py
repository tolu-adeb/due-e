from datetime import date, datetime, timedelta

import pytest

from .conftest import NOW

DAY = NOW.date()


@pytest.mark.parametrize("label, cat", [
    ("EECS 281 Lecture", "class"),
    ("Study 281 Project 2", "study"),
    ("tiktok", "scrolling"),
    ("watching a show", "leisure"),     # 'show' must not trigger 'hw'
    ("brunch", "other"),                # 'run' only matches as a word
    ("morning run", "exercise"),
    ("Cervo backtests", "project"),
])
def test_categorize(due, label, cat):
    assert due.tracker.categorize(label) == cat


def test_log_with_minutes_ends_now(due):
    row = due.tracker.log("gym", minutes=60)
    assert row["category"] == "exercise" and row["minutes"] == 60
    assert row["end"] == NOW.isoformat(timespec="seconds")


@pytest.mark.parametrize("kwargs", [{"minutes": 0}, {}, {"minutes": 30, "focus": 9}, {"minutes": 30, "category": "nope"}])
def test_log_validation(due, kwargs):
    with pytest.raises(ValueError):
        due.tracker.log("x", **kwargs)


def test_timer_start_stop_and_switch(due, clock):
    t = due.tracker
    t.start_timer("leetcode")
    clock.now = NOW + timedelta(minutes=40)
    second = t.start_timer("EECS 370 homework")
    assert second["stopped_previous"]["label"] == "leetcode" and second["stopped_previous"]["minutes"] == 40
    clock.now = NOW + timedelta(minutes=100)
    stopped = t.stop_timer(focus=4)
    assert stopped["minutes"] == 60 and stopped["focus"] == 4
    assert t.running() is None and t.stop_timer() is None
    assert [a["label"] for a in t.activities(DAY, DAY)] == ["leetcode", "EECS 370 homework"]


def test_review_includes_calendar_and_duee_blocks(due):
    due.planner.add_event({"title": "Midterm", "kind": "exam", "at": "2026-11-05T18:00", "difficulty": 4})
    items = due.tracker.review_items(DAY, due.radar.blocks)
    kinds = {i["kind"] for i in items}
    assert {"calendar", "prep"} <= kinds
    lecture = next(i for i in items if i["title"] == "EECS 281 Lecture")
    assert lecture["planned_minutes"] == 90 and lecture["status"] is None


def _item(due, title):
    return next(i for i in due.tracker.review_items(DAY, due.radar.blocks) if i["title"] == title)


def test_confirm_done_partial_skipped_round_trip(due):
    t = due.tracker
    lec = _item(due, "EECS 281 Lecture")
    t.confirm(lec, "done")
    assert [a["minutes"] for a in t.activities(DAY, DAY)] == [90]
    t.confirm(lec, "partial", actual_minutes=30)          # re-confirming replaces, never duplicates
    assert [a["minutes"] for a in t.activities(DAY, DAY)] == [30]
    t.confirm(lec, "skipped")
    assert t.activities(DAY, DAY) == []
    assert _item(due, "EECS 281 Lecture")["status"] == "skipped"


def test_confirm_does_not_double_count_a_timer(due, clock):
    t = due.tracker
    blk = _item(due, "Study 281 Project 2")  # 2–4 PM
    t.log("project 2 grind", start=datetime.fromisoformat(blk["start"]), minutes=110, category="study")
    t.confirm(blk, "done", category="study")
    assert len(t.activities(DAY, DAY)) == 1


def test_confirming_a_duee_block_updates_it(due):
    e = due.planner.add_event({"title": "Midterm", "kind": "exam", "at": "2026-11-05T18:00", "difficulty": 4})
    blk = e["blocks"][0]
    day = date.fromisoformat(blk["day"])
    item = next(i for i in due.tracker.review_items(day, due.radar.blocks) if i["ref"] == f"block:{blk['id']}")
    due.tracker.confirm(item, "done")
    assert due.db.one("SELECT status FROM blocks WHERE id=?", (blk["id"],))["status"] == "done"
    mins = (datetime.fromisoformat(blk["end"]) - datetime.fromisoformat(blk["start"])).seconds // 60
    assert due.planner.logged_prep(e["id"]) == mins  # counts toward the exam's prep


def test_custom_types_and_learning(due):
    t = due.tracker
    assert t.categorize("piano practice") == "other"
    with pytest.raises(ValueError):
        t.add_category("music", "fun")                      # bad kind
    with pytest.raises(ValueError):
        t.add_category("x" * 30, "leisure")                 # too long
    assert t.add_category("Music", "leisure") == {"name": "music", "kind": "leisure", "created": True}
    assert due.cfg.tracker.kind_of("music") == "leisure"     # analysis sees it immediately
    t.log("piano practice", minutes=30, category="music")
    assert t.categorize("piano practice") == "music"         # learned — no more "other"
    assert t.categorize("Piano  Practice session") == "music"


def test_learned_label_overrides_default(due):
    t = due.tracker
    assert t.categorize("youtube lecture") in ("scrolling", "class")
    row = t.log("youtube lecture", minutes=60)
    t.update_activity(row["id"], category="study")
    assert t.categorize("youtube lecture") == "study"
    assert t.activities(DAY, DAY)[0]["category"] == "study"
    assert t.categorize("youtube") == "scrolling"           # only the exact phrase was learned


def test_custom_types_persist_across_restarts(due):
    from duee.tracker import Tracker
    due.tracker.add_category("clubs", "productive")
    due.tracker.log("MASA meeting", minutes=60, category="clubs")
    fresh = Tracker(due.db, due.cfg, due.now)
    assert "clubs" in fresh.category_names() and fresh.categorize("MASA meeting") == "clubs"


def test_recategorizing_a_calendar_item_is_remembered(due):
    lec = _item(due, "Study 281 Project 2")
    due.tracker.confirm(lec, "done", category="project")
    assert due.tracker.categorize("Study 281 Project 2") == "project"
