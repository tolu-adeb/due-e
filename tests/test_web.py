import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from duee.web import build_app


def call(due, fn):
    async def main():
        client = TestClient(TestServer(build_app(due)))
        await client.start_server()
        try:
            return await fn(client)
        finally:
            await client.close()
    return asyncio.run(main())


def test_state_log_confirm_flow(due):
    async def flow(c):
        r = await c.get("/api/state")
        st = await r.json()
        assert r.status == 200 and st["day"] == "2026-11-02" and st["deadlines"]
        lecture = next(i for i in st["review"] if i["title"] == "EECS 281 Lecture")

        r = await c.post("/api/log", json={"label": "tiktok", "minutes": 40})
        assert r.status == 201 and (await r.json())["category"] == "scrolling"

        r = await c.post("/api/confirm", json={"ref": lecture["ref"], "status": "done", "day": "2026-11-02"})
        assert r.status == 200

        st = await (await c.get("/api/state")).json()
        assert {a["label"] for a in st["activities"]} == {"tiktok", "EECS 281 Lecture"}
        assert st["day_stats"]["totals"]["waste"] == 40
    call(due, flow)


def test_timer_endpoints(due, clock):
    async def flow(c):
        await c.post("/api/timer/start", json={"label": "leetcode"})
        clock.now = clock.now.replace(minute=45)
        r = await (await c.post("/api/timer/stop", json={"focus": 4})).json()
        assert r["minutes"] == 45 and r["focus"] == 4
    call(due, flow)


def test_planner_and_habits_endpoints(due):
    async def flow(c):
        r = await c.post("/api/events", json={"title": "Midterm", "kind": "exam", "at": "2026-11-05T18:00",
                                              "difficulty": 4, "readiness": 2})
        ev = await r.json()
        assert r.status == 201 and ev["prep"]["scheduled_minutes"] > 0
        r = await c.patch(f"/api/events/{ev['id']}", json={"readiness": 5})
        assert (await r.json())["prep"]["scheduled_minutes"] == 0
        r = await c.post("/api/habits", json={"name": "Gaming", "minutes": 60, "per_week": 2, "window": "evening"})
        assert r.status == 201
        ag = await (await c.get("/api/agenda?days=7")).json()
        assert any(b["kind"] == "habit" for b in ag["blocks"]) and ag["calendar"]
        ics = await c.get("/plan.ics")
        assert ics.status == 200 and "BEGIN:VCALENDAR" in await ics.text()
        ins = await (await c.get("/api/insights?days=7")).json()
        assert "score" in ins and ins["ai_available"] is False
    call(due, flow)


def test_bad_input_is_a_400_not_a_crash(due):
    async def flow(c):
        r = await c.post("/api/log", json={"label": "x"})
        assert r.status == 400 and "minutes" in (await r.json())["error"]
        r = await c.post("/api/events", json={"title": "x", "kind": "nope", "at": "2026-11-05T18:00"})
        assert r.status == 400
        r = await c.delete("/api/activities/9999")
        assert r.status == 404
        r = await c.post("/api/ai-review", json={})
        assert r.status == 503
    call(due, flow)


def test_rejects_cross_site_form_posts_and_foreign_hosts(due):
    async def flow(c):
        r = await c.post("/api/log", data={"label": "x", "minutes": "5"})
        assert r.status == 415
        r = await c.get("/api/state", headers={"Host": "evil.example"})
        assert r.status == 403
    call(due, flow)


def test_token_required_when_configured(due):
    due.cfg.web.token = "s3cret"

    async def flow(c):
        assert (await c.get("/api/state")).status == 401
        assert (await c.get("/api/state", headers={"X-Duee-Token": "s3cret"})).status == 200
        r = await c.get("/plan.ics?token=s3cret")  # calendar apps can subscribe with the token in the URL
        assert r.status == 200 and "duee_token" in r.cookies
    call(due, flow)


def test_refuses_public_bind_without_token(due):
    from duee.web import start_web
    due.cfg.web.host = "0.0.0.0"
    with pytest.raises(SystemExit):
        asyncio.run(start_web(due))


def test_skipped_prep_gets_rescheduled(due):
    async def flow(c):
        ev = await (await c.post("/api/events", json={"title": "Midterm", "kind": "exam", "at": "2026-11-05T18:00",
                                                       "difficulty": 4, "readiness": 2})).json()
        first = ev["blocks"][0]
        r = await (await c.post("/api/confirm", json={"ref": f"block:{first['id']}", "status": "skipped",
                                                       "day": first["day"]})).json()
        assert r["replan"]["placed"] > 0
        after = await (await c.get("/api/events")).json()
        assert after[0]["prep"]["unscheduled_minutes"] == 0
        assert after[0]["prep"]["scheduled_minutes"] >= after[0]["prep"]["remaining_minutes"]
    call(due, flow)


def test_club_events_are_opt_in(cfg, clock):
    from duee.app import DueE
    from duee.config import Feed

    from .conftest import FIXTURES
    cfg.feeds.append(Feed("MASA", str(FIXTURES / "plan.ics"), role="events"))  # reuse: lectures as 'club' events
    d = DueE.create(cfg, clock=clock, db_path=":memory:")
    d.radar.refresh(force=True)
    assert d.radar.optional and not d.radar.blocks
    day = clock().date()
    assert d.tracker.review_items(day, d.radar.committed_blocks) == []        # not going → nothing to confirm
    lecture = next(b for b in d.radar.optional if b.start.date() == day and "Lecture" in b.title)

    async def flow(c):
        r = await c.post("/api/rsvp", json={"ref": lecture.ref, "going": True})
        assert r.status == 200
        st = await (await c.get("/api/state")).json()
        assert any(x["going"] for x in st["club_events"])
        assert [i["title"] for i in st["review"]] == ["EECS 281 Lecture"]     # going → shows in close-out
        assert (await c.post("/api/rsvp", json={"ref": "cal:nope", "going": True})).status == 404
    call(d, flow)
    # going also blocks the time for planning
    busy = d.planner.busy(lecture.start, lecture.end)
    assert busy and busy[0][0] <= lecture.start


def test_custom_types_over_the_api(due):
    async def flow(c):
        st = await (await c.get("/api/state")).json()
        assert "class" in st["kinds"] and any(x["name"] == "class" and x["kind"] == "class" for x in st["categories"])
        r = await c.post("/api/log", json={"label": "guitar", "minutes": 30, "category": "music", "kind": "leisure"})
        row = await r.json()
        assert r.status == 201 and row["category"] == "music"
        r = await c.post("/api/log", json={"label": "chess", "minutes": 20, "category": "boardgames"})
        assert r.status == 400 and "create it first" in (await r.json())["error"]
        r = await c.patch(f"/api/activities/{row['id']}", json={"category": "hobby", "kind": "leisure"})
        assert (await r.json())["category"] == "hobby"
        assert (await c.post("/api/categories", json={"name": "clubs", "kind": "productive"})).status == 201
        st = await (await c.get("/api/state")).json()
        assert {"music", "hobby", "clubs"} <= {x["name"] for x in st["categories"]}
    call(due, flow)


def test_v04_tasks_classes_and_auto_link(due):
    async def flow(c):
        r = await c.post("/api/tasks/sync", json={})
        res = await r.json()
        assert r.status == 200 and res["created"] >= 4

        events = await (await c.get("/api/events")).json()
        p2 = next(e for e in events if e["title"] == "Project 2")
        assert p2["read"]["why"] and p2["read"]["progress_label"] == "progress" and p2["source"] == "canvas"

        st = await (await c.get("/api/state")).json()
        dl = next(d for d in st["deadlines"] if d["title"] == "Project 2")
        assert dl["task"]["id"] == p2["id"]
        # classes are split out for attendance
        lecture = next(i for i in st["review"] if i["title"] == "EECS 281 Lecture")
        assert lecture["is_class"]
        assert not all(i["is_class"] for i in st["review"])

        ag = await (await c.get("/api/agenda?days=7")).json()
        assert any(x["is_class"] for x in ag["calendar"]) and not all(x["is_class"] for x in ag["calendar"])

        # logging "281 project 2" counts toward the task without picking it
        r = await c.post("/api/log", json={"label": "281 project 2", "minutes": 60})
        assert (await r.json())["event_id"] == p2["id"]

        # "not a task" hides it; editing the estimate replans
        hw = next(e for e in events if e["title"] == "Homework 5")
        r = await c.patch(f"/api/events/{hw['id']}", json={"prep_hours": 1})
        assert r.status == 200
        assert (await (await c.post(f"/api/events/{hw['id']}/replan", json={})).json())["event"]["prep"]["needed_minutes"] == 60
        await c.patch(f"/api/events/{hw['id']}", json={"ignored": True})
        events = await (await c.get("/api/events")).json()
        assert not any(e["title"] == "Homework 5" for e in events)
    call(due, flow)
