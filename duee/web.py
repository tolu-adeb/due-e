"""Local web app: log what you did, close out your day, plan events, schedule habits, see insights.

Runs on aiohttp (already installed with discord.py), so it shares the bot's event loop.
Security defaults: binds to 127.0.0.1; refuses a non-local bind without a token; writes require
JSON bodies (blocks cross-site form posts); Host header is checked to stop DNS-rebinding.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from aiohttp import web

from . import ai, render
from .config import KINDS
from .app import DueE
from .tracker import iso

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _is_loopback(host: str) -> bool:
    if host in LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


# ── middleware ───────────────────────────────────────────────────────────────
def make_middlewares(due: DueE):
    token = due.cfg.web.token

    @web.middleware
    async def security(request: web.Request, handler):
        if not token:
            host = (request.host or "").rsplit(":", 1)[0]
            if not _is_loopback(host):
                return web.json_response({"error": "forbidden host"}, status=403)
        else:
            given = (request.headers.get("X-Duee-Token") or request.cookies.get("duee_token")
                     or request.query.get("token") or "")
            if not hmac.compare_digest(given, token):
                return web.json_response({"error": "unauthorized"}, status=401)
        # Only POST can be sent cross-site by a plain HTML form; PATCH/PUT/DELETE always need a CORS preflight.
        if request.method == "POST" and request.content_type != "application/json":
            return web.json_response({"error": "send JSON"}, status=415)
        resp = await handler(request)
        if token and request.query.get("token"):
            resp.set_cookie("duee_token", token, httponly=True, samesite="Strict", max_age=60 * 60 * 24 * 90)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    @web.middleware
    async def errors(request: web.Request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except KeyError:
            return web.json_response({"error": "not found"}, status=404)
        except ai.AiUnavailable as e:
            return web.json_response({"error": str(e)}, status=503)
        except Exception:  # noqa: BLE001
            log.exception("web handler failed")
            return web.json_response({"error": "internal error — check the DUE-E logs"}, status=500)

    return [security, errors]


def _day(request: web.Request, due: DueE) -> date:
    raw = request.query.get("day")
    return date.fromisoformat(raw) if raw else due.now().date()


def _dt(value, due: DueE) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=due.cfg.timezone)


async def _json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise ValueError("invalid JSON body")
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    return body


def _item_json(i) -> dict:
    return {"uid": i.uid, "title": i.title, "course": i.course, "due": iso(i.due), "all_day": i.all_day,
            "url": i.url, "source": i.source}


# ── routes ───────────────────────────────────────────────────────────────────
def build_app(due: DueE) -> web.Application:
    app = web.Application(middlewares=make_middlewares(due))
    r = web.RouteTableDef()
    t, p = due.tracker, due.planner

    @r.get("/")
    async def index(_):
        return web.FileResponse(STATIC / "index.html")

    @r.get("/api/state")
    async def state(request):
        await due.refresh()
        day = _day(request, due)
        now = due.now()
        day_an = due.analyze(1, end=day)
        tasks_by_uid = {e["uid"]: {"id": e["id"], "kind": e["kind"], "scheduled": e["prep"]["scheduled_minutes"],
                                   "needed": e["prep"]["remaining_minutes"], "short": e["prep"]["unscheduled_minutes"]}
                        for e in p.events() if e.get("uid")}
        return web.json_response({
            "now": iso(now), "day": day.isoformat(), "today": now.date().isoformat(),
            "timer": t.running(),
            "activities": t.activities(day, day),
            "review": t.review_items(day, due.radar.committed_blocks),
            "club_events": [{"ref": c.ref, "title": c.title, "source": c.source, "start": iso(c.start),
                             "end": iso(c.end), "going": c.ref in due.radar.going}
                            for c in due.radar.optional_between(now, now + timedelta(days=7))],
            "deadlines": [{**_item_json(i), "task": tasks_by_uid.get(i.uid)} for i in due.radar.upcoming(days=7)],
            "categories": [{"name": c.name, "kind": c.kind} for c in due.cfg.tracker.categories]
                          + [{"name": "other", "kind": "maintenance"}],
            "kinds": list(KINDS),
            "day_stats": {"score": day_an["score"], "totals": day_an["totals"],
                          "by_category": day_an["by_category"]},
            "events": [{"id": e["id"], "title": e["title"]} for e in p.events()],
            "ai": ai.available(due.cfg),
            "feeds": [f.__dict__ for f in due.radar.feed_results],
        })

    # tracker
    def _maybe_new_category(b: dict) -> None:
        """`{category: "music", kind: "leisure"}` creates the type on the fly."""
        if b.get("category") and b.get("kind") and b["category"] not in t.category_names():
            b["category"] = t.add_category(b["category"], b["kind"])["name"]

    @r.post("/api/categories")
    async def add_category(request):
        b = await _json(request)
        return web.json_response(t.add_category(b.get("name", ""), b.get("kind", "")), status=201)

    @r.patch("/api/activities/{id}")
    async def patch_activity(request):
        b = await _json(request)
        _maybe_new_category(b)
        return web.json_response(t.update_activity(int(request.match_info["id"]), category=b.get("category"),
                                                   label=b.get("label"), focus=b.get("focus")))

    @r.post("/api/log")
    async def log_activity(request):
        b = await _json(request)
        _maybe_new_category(b)
        event_id = b.get("event_id")
        if event_id is None and b.get("label"):
            event_id = due.link_task(b["label"])  # "281 project 2" counts toward Project 2 automatically
        row = t.log(b.get("label", ""), minutes=b.get("minutes"), start=_dt(b.get("start"), due),
                    end=_dt(b.get("end"), due), category=b.get("category") or None, focus=b.get("focus"),
                    source="manual", event_id=event_id, notes=b.get("notes", ""))
        if event_id:
            row["task"] = p.event(event_id)["title"]
            p.replan_event(event_id)
        return web.json_response(row, status=201)

    @r.post("/api/timer/start")
    async def timer_start(request):
        b = await _json(request)
        _maybe_new_category(b)
        event_id = b.get("event_id") or due.link_task(b.get("label", ""))
        return web.json_response(t.start_timer(b.get("label", ""), b.get("category") or None, event_id))

    @r.post("/api/timer/stop")
    async def timer_stop(request):
        b = await _json(request)
        return web.json_response(t.stop_timer(b.get("focus")) or {})

    @r.delete("/api/activities/{id}")
    async def delete_activity(request):
        if not t.delete(int(request.match_info["id"])):
            raise KeyError("activity")
        return web.json_response({"ok": True})

    @r.post("/api/confirm")
    async def confirm(request):
        b = await _json(request)
        _maybe_new_category(b)
        day = date.fromisoformat(b["day"]) if b.get("day") else due.now().date()
        item = next((i for i in t.review_items(day, due.radar.committed_blocks) if i["ref"] == b.get("ref")), None)
        if not item:
            raise KeyError("item")
        if item["kind"] == "calendar" and not item.get("event_id") and not item.get("is_class"):
            item["event_id"] = due.link_task(item["title"])  # your "Study 281 P2" block counts toward Project 2
        result = t.confirm(item, b.get("status", ""), b.get("actual_minutes"), b.get("category"))
        if item["kind"] == "prep" and item.get("event_id") and result["status"] in ("partial", "skipped", "cancelled"):
            # missed prep doesn't vanish — it's rescheduled into the remaining days
            result["replan"] = p.replan_event(item["event_id"])
        return web.json_response(result)

    @r.post("/api/rsvp")
    async def rsvp(request):
        b = await _json(request)
        due.set_going(str(b.get("ref", "")), bool(b.get("going")))
        return web.json_response({"ok": True, "going": bool(b.get("going"))})

    # planner
    @r.get("/api/events")
    async def events(_):
        return web.json_response(p.events())

    @r.post("/api/events")
    async def add_event(request):
        b = await _json(request)
        await due.refresh()
        due.learn_best_hours()
        return web.json_response(p.add_event(b), status=201)

    @r.patch("/api/events/{id}")
    async def patch_event(request):
        b = await _json(request)
        await due.refresh()
        return web.json_response(p.update_event(int(request.match_info["id"]), b))

    @r.delete("/api/events/{id}")
    async def delete_event(request):
        p.delete_event(int(request.match_info["id"]))
        return web.json_response({"ok": True})

    @r.post("/api/events/{id}/replan")
    async def replan_event(request):
        await due.refresh()
        due.learn_best_hours()
        eid = int(request.match_info["id"])
        result = p.replan_event(eid)
        return web.json_response({"result": result, "event": p.event(eid)})

    @r.post("/api/tasks/sync")
    async def tasks_sync(_):
        await asyncio.to_thread(due.radar.refresh, True)
        return web.json_response(await due.sync_tasks())

    @r.post("/api/replan")
    async def replan(_):
        return web.json_response(await due.replan_all())

    @r.get("/api/agenda")
    async def agenda(request):
        await due.refresh()
        days = max(1, min(21, int(request.query.get("days", 7))))
        start = _day(request, due)
        end = start + timedelta(days=days - 1)
        lo = datetime.combine(start, datetime.min.time(), tzinfo=due.cfg.timezone)
        hi = lo + timedelta(days=days)
        cal = [{"ref": b.ref, "title": b.title, "start": iso(b.start), "end": iso(b.end), "kind": "calendar",
                "source": b.source, "is_class": t.cfg.tracker.kind_of(t.categorize(b.title)) == "class"}
               for b in due.radar.blocks_between(lo, hi)]
        cal += [{"ref": b.ref, "title": f"{b.title} ({b.source})", "start": iso(b.start), "end": iso(b.end),
                 "kind": "club", "source": b.source} for b in due.radar.optional_between(lo, hi)
                if b.ref not in due.radar.going]
        blocks = [{**b, "ref": f"block:{b['id']}"} for b in p.blocks(start, end)]
        evs = [{"id": e["id"], "title": e["title"], "kind": "event", "event_kind": e["kind"], "start": e["at"],
                "duration": e["duration"]} for e in p.events() if start.isoformat() <= e["at"][:10] <= end.isoformat()
               and e.get("source") != "canvas"]  # Canvas tasks already show as "due:" deadlines
        dl = [_item_json(i) for i in due.radar.items if start <= i.due.date() <= end]
        return web.json_response({"start": start.isoformat(), "days": days, "calendar": cal, "blocks": blocks,
                                  "events": evs, "deadlines": dl})

    # habits
    @r.get("/api/habits")
    async def habits(_):
        return web.json_response(p.habits())

    @r.post("/api/habits")
    async def add_habit(request):
        row = p.add_habit(await _json(request))
        await due.refresh()
        p.schedule_habits()
        return web.json_response(row, status=201)

    @r.patch("/api/habits/{id}")
    async def patch_habit(request):
        p.update_habit(int(request.match_info["id"]), await _json(request))
        return web.json_response({"ok": True})

    @r.delete("/api/habits/{id}")
    async def delete_habit(request):
        p.delete_habit(int(request.match_info["id"]))
        return web.json_response({"ok": True})

    @r.post("/api/habits/schedule")
    async def schedule_habits(_):
        await due.refresh()
        return web.json_response(p.schedule_habits())

    # insights
    @r.get("/api/insights")
    async def insights(request):
        days = max(1, min(90, int(request.query.get("days", 7))))
        an = due.analyze(days)
        an["ai_available"] = ai.available(due.cfg)
        return web.json_response(an)

    @r.post("/api/ai-review")
    async def ai_review(request):
        b = await _json(request)
        an = due.analyze(int(b.get("days", 7)))
        text = await asyncio.to_thread(ai.weekly_review, due.cfg, an, p.events(), p.habits())
        return web.json_response({"text": text})

    @r.get("/plan.ics")
    async def plan_ics(_):
        return web.Response(text=p.to_ics(), content_type="text/calendar", charset="utf-8",
                            headers={"Content-Disposition": 'inline; filename="due-e-plan.ics"'})

    @r.get("/api/digest")
    async def digest_preview(request):
        await due.refresh()
        kind = request.query.get("kind", "morning")
        text, _ = render.digest(due.radar.items, due.now(), kind if kind in ("morning", "evening") else "morning",
                                due.cfg.horizon_hours, style="plain")
        return web.json_response({"text": text})

    app.add_routes(r)
    app.router.add_static("/static", STATIC)
    return app


async def start_web(due: DueE) -> web.AppRunner | None:
    w = due.cfg.web
    if not w.enabled:
        return None
    if not _is_loopback(w.host) and not w.token:
        raise SystemExit(f"Refusing to serve the web app on {w.host} without web.token set (it can edit your data).")
    runner = web.AppRunner(build_app(due), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, w.host, w.port).start()
    except OSError as e:
        await runner.cleanup()
        raise SystemExit(f"Can't open the web app on {w.host}:{w.port} ({e.strerror or e}). "
                         "Is another DUE-E already running? Change web.port in config.yaml.") from None
    log.info("web app on http://%s:%d%s", w.host, w.port, " (token required)" if w.token else "")
    return runner
