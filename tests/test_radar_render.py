from datetime import timedelta

from duee import render
from duee.feeds import Item
from duee.radar import merge

from .conftest import NOW


def titles(items):
    return [i.title for i in items]


def test_merge_dedupes_across_feeds_and_keeps_link(radar):
    p2 = [i for i in radar.items if i.title == "Project 2"]
    assert len(p2) == 1
    assert p2[0].url and p2[0].source == "Canvas"


def test_merge_prefers_linked_copy_and_keeps_course():
    due = NOW + timedelta(hours=5)
    a = Item("a", "HW", "EECS 370", due, False, None, "Canvas")
    b = Item("b", "hw ", None, due, False, "https://x", "Site")
    (only,) = merge([a, b])
    assert only.url == "https://x" and only.course == "EECS 370"


def test_ignore_patterns(radar):
    assert "Office Hours" not in titles(radar.items)


def test_upcoming_course_filter(radar):
    assert set(titles(radar.upcoming(days=7, course="eecs370"))) == {"Homework 5", "Midterm Exam"}


def test_morning_digest_buckets(radar):
    text, count = render.digest(radar.items, NOW, "morning", 48, style="plain")
    today, tomorrow, later = text.split("\n\n")[1:]
    assert "DUE TODAY (1)" in today and "Project 2" in today
    assert "DUE TOMORROW (3)" in tomorrow and "Homework 5" in tomorrow
    assert "Lab 6 Prelab" in later and "Quiz 8" not in text  # Quiz 8 is 50h away
    assert count == 5


def test_evening_says_tonight(radar):
    evening = NOW.replace(hour=20)
    text, _ = render.digest(radar.items, evening, "evening", 48, style="plain")
    assert "DUE TONIGHT (1)" in text and "Project 2" in text


def test_empty_digest(radar):
    text, count = render.digest([], NOW, "morning", 48)
    assert count == 0 and "Radar clear" in text


def test_discord_style_uses_live_timestamps_and_escapes(radar):
    item = Item("x", "a_b*c", "EECS 281", NOW + timedelta(hours=3), False, "https://u", "Canvas")
    ln = render.line(item, NOW, "discord")
    assert f"<t:{int(item.due.timestamp())}:R>" in ln
    assert r"a\_b\*c" in ln and "(<https://u>)" in ln


def test_weekly_agenda_groups_by_day(radar):
    text = render.agenda(radar.items, NOW, 7, "Week ahead", style="plain")
    assert "8 items across 3 courses" in text
    assert text.index("TODAY · MON NOV 2") < text.index("TOMORROW · TUE NOV 3") < text.index("THU NOV 5")


def test_relative():
    assert render.relative(NOW + timedelta(minutes=90), NOW) == "in 1h 30m"
    assert render.relative(NOW + timedelta(days=2, hours=3), NOW) == "in 2d 3h"
    assert render.relative(NOW + timedelta(seconds=20), NOW) == "in 1m"


def test_chunk_respects_discord_limit():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = render.chunk(text)
    assert len(parts) > 1 and all(len(p) <= 2000 for p in parts)
    assert "\n".join(parts) == text
