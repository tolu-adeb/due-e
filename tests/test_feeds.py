from datetime import datetime, timedelta

import pytest
import requests

from duee.config import Feed
from duee.feeds import _safe_error, load_feed_text, parse_items, split_course

from .conftest import FIXTURES, NOW, TZ


@pytest.mark.parametrize(
    "summary, title, course",
    [
        ("Project 2 [EECS 281 001 FA 2026]", "Project 2", "EECS 281"),
        ("Quiz 8 [PHYSICS 240 F26]", "Quiz 8", "PHYSICS 240"),
        ("Essay [Intro to Writing]", "Essay", "Intro to Writing"),
        ("Reading Response", "Reading Response", None),
        ("Lab [3] [EECS 370]", "Lab [3]", "EECS 370"),
    ],
)
def test_split_course(summary, title, course):
    assert split_course(summary) == (title, course)


def _parse(name):
    text = (FIXTURES / name).read_text()
    return parse_items(text, Feed("t", "x"), TZ, NOW - timedelta(days=1), NOW + timedelta(days=15))


def test_canvas_parsing():
    items = {i.title: i for i in _parse("canvas.ics")}
    # UTC "Z" times are converted to Detroit time (EST after Nov 1)
    assert items["Project 2"].due == datetime(2026, 11, 2, 23, 59, tzinfo=TZ)
    assert items["Project 2"].url.endswith("/assignments/1001")
    # all-day events are due at end of day
    assert items["Midterm Exam"].all_day and items["Midterm Exam"].due.hour == 23
    # past items are outside the window
    assert "Homework 4" not in items


def test_recurring_events_expand():
    weekly = [i for i in _parse("canvas.ics") if i.title == "Weekly Problem Set"]
    assert [i.due.date().isoformat() for i in weekly] == ["2026-11-03", "2026-11-10"]
    assert all(i.due.hour == 18 for i in weekly)  # 6 PM local on both sides of the DST change
    assert len({i.uid for i in weekly}) == 2


def test_vtodo_floating_and_cancelled():
    items = {i.title: i for i in _parse("course_site.ics")}
    assert items["Problem Set 3"].due == datetime(2026, 11, 5, 22, 59, tzinfo=TZ)  # VTODO uses DUE
    assert items["Reading Response"].due == datetime(2026, 11, 3, 17, 0, tzinfo=TZ)  # floating -> local
    assert "Cancelled Quiz" not in items


def test_failed_fetch_falls_back_to_cache(tmp_path):
    src = tmp_path / "feed.ics"
    src.write_text((FIXTURES / "canvas.ics").read_text())
    feed = Feed("Canvas", str(src))
    cache = tmp_path / "cache"

    text, res = load_feed_text(feed, cache)
    assert res.ok and text

    src.unlink()
    text2, res2 = load_feed_text(feed, cache)
    assert not res2.ok and res2.used_cache and text2 == text


def test_non_ics_response_is_rejected(tmp_path):
    bad = tmp_path / "login.html"
    bad.write_text("<html>Please log in</html>")
    text, res = load_feed_text(Feed("Bad", str(bad)), tmp_path / "cache")
    assert text is None and not res.ok and "not an iCalendar" in res.error


def test_errors_never_leak_feed_url(monkeypatch, tmp_path):
    secret = "https://umich.instructure.com/feeds/calendars/user_SECRETTOKEN.ics"

    def boom(*a, **k):
        raise requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {secret}")

    monkeypatch.setattr(requests, "get", boom)
    _, res = load_feed_text(Feed("Canvas", secret), tmp_path / "cache")
    assert "SECRETTOKEN" not in res.error
    assert _safe_error(requests.Timeout(secret)) == "timeout"
