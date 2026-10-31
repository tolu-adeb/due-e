import asyncio
import json
from datetime import datetime, time, timedelta

from duee.feeds import FeedResult
from duee.scheduler import Job, Scheduler
from duee.status import DEGRADED, DOWN, OK, Status, evaluate, read_status

from .conftest import NOW, TZ


def test_next_run_daily_and_weekly():
    assert Job("morning", time(8)).next_after(NOW, TZ) == NOW + timedelta(days=1)  # exactly 8:00 -> tomorrow
    assert Job("evening", time(20)).next_after(NOW, TZ) == NOW.replace(hour=20)
    sunday = Job("weekly", time(19), weekday=6).next_after(NOW, TZ)
    assert sunday.weekday() == 6 and sunday.date().isoformat() == "2026-11-08"


def test_next_run_across_dst():
    before = datetime(2026, 10, 31, 9, 0, tzinfo=TZ)  # EDT
    nxt = Job("morning", time(8)).next_after(before, TZ)  # Nov 1 is DST-end day
    assert nxt.hour == 8 and nxt.utcoffset() == timedelta(hours=-5)


class Collect:
    def __init__(self):
        self.posts = []

    async def post(self, text):
        self.posts.append(text)


def test_scheduler_fires_due_jobs_and_writes_status(cfg, radar):
    clock = {"now": NOW.replace(hour=7, minute=59, second=30)}
    radar.clock = lambda: clock["now"]
    poster = Collect()
    status = Status(cfg.status_file)
    sleeps = []

    async def fake_sleep(secs):
        sleeps.append(secs)
        clock["now"] += timedelta(seconds=secs)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    sched = Scheduler(cfg, radar, poster, status, style="plain", sleep=fake_sleep)
    try:
        asyncio.run(sched.run_forever())
    except asyncio.CancelledError:
        pass

    assert len(poster.posts) == 1 and "Morning sweep".upper() in poster.posts[0]
    data = read_status(cfg.status_file)
    assert data["last_posts"]["morning"]["ok"] is True
    assert data["feeds"]["Canvas"]["ok"] is True
    assert data["next_runs"]["morning"].startswith("2026-11-03T08:00")


def test_failed_post_is_recorded(cfg, radar):
    class Broken:
        async def post(self, text):
            raise RuntimeError("discord down")

    status = Status(cfg.status_file)
    asyncio.run(Scheduler(cfg, radar, Broken(), status).fire("evening"))
    post = read_status(cfg.status_file)["last_posts"]["evening"]
    assert post["ok"] is False and post["error"] == "RuntimeError"


def test_health_levels():
    fresh = {"heartbeat_at": NOW.isoformat(), "last_posts": {}}
    assert evaluate(fresh, NOW, [FeedResult("Canvas", ok=True, items=3)])[0] == OK
    assert evaluate(fresh, NOW, [FeedResult("Canvas", ok=False, used_cache=True, error="HTTP 500")])[0] == DEGRADED
    stale = {"heartbeat_at": (NOW - timedelta(hours=1)).isoformat()}
    assert evaluate(stale, NOW, [])[0] == DOWN
    assert evaluate(None, NOW, [])[0] == DOWN
    assert evaluate(None, NOW, [FeedResult("Canvas", ok=True, items=1)], check_bot=False)[0] == OK


def test_status_write_is_valid_json(cfg):
    s = Status(cfg.status_file)
    s.started(NOW)
    assert json.loads(cfg.status_file.read_text())["app"] == "due-e"
