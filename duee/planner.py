"""Planner: upcoming events → prep blocks (only when needed), plus weekly habit scheduling.

How prep is decided
  needed = base hours for the kind × difficulty factor × (how far you are from ready)
  minus prep you've already logged toward it. Under 30 minutes left → no prep scheduled.
  You can always override with your own estimate (prep_hours).

How blocks are placed
  Free time = your waking window − plan-calendar blocks − other events − existing DUE-E blocks (+ buffers).
  Prep is spread across the days before the event (spaced practice beats cramming), prefers your
  focus windows / best hours, respects a daily cap, and for exams the last block is a "Final review".
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable

from .config import Config
from .db import DB
from .radar import Radar
from .tracker import Tracker, iso, parse_dt

KINDS = ("exam", "project", "assignment", "presentation", "interview", "event")
BASE_HOURS = {"exam": 8, "project": 12, "assignment": 3, "presentation": 3, "interview": 5, "event": 0}
END_BUFFER = {"exam": 60, "interview": 60, "presentation": 60, "project": 120, "assignment": 120, "event": 0}
MAX_PREP_PER_DAY = {"exam": 180, "project": 240, "assignment": 180, "presentation": 120, "interview": 120, "event": 0}
NO_PREP_BELOW = 30
WINDOWS = ("morning", "afternoon", "evening", "any")

Interval = tuple[datetime, datetime]


# ── pure helpers (easy to test) ─────────────────────────────────────────────
def prep_estimate(kind: str, difficulty: int, readiness: int, prep_hours: float | None,
                  logged_minutes: int) -> dict:
    if prep_hours is not None:
        needed = float(prep_hours) * 60
        basis = "your estimate"
    else:
        diff = 0.4 + 0.2 * max(1, min(5, difficulty))          # 1 → 0.6×, 3 → 1.0×, 5 → 1.4×
        ready_gap = (5 - max(1, min(5, readiness))) / 4         # readiness 5 → nothing left
        needed = BASE_HOURS.get(kind, 0) * 60 * diff * ready_gap
        basis = f"{kind}, difficulty {difficulty}/5, readiness {readiness}/5"
    remaining = max(0, round(needed - logged_minutes))
    if BASE_HOURS.get(kind, 0) == 0 and prep_hours is None:
        reason = "events don't get prep unless you give an estimate"
    elif remaining < NO_PREP_BELOW:
        reason = "you're ready — no prep needed" if logged_minutes or readiness >= 4 else "not enough to need a block"
    else:
        reason = basis
    return {"needed_minutes": round(needed), "logged_minutes": logged_minutes,
            "remaining_minutes": remaining if remaining >= NO_PREP_BELOW else 0,
            "needs_prep": remaining >= NO_PREP_BELOW, "reason": reason}


def merge_intervals(iv: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for s, e in sorted(iv):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def free_slots(start: datetime, end: datetime, busy: list[Interval]) -> list[Interval]:
    slots, cur = [], start
    for s, e in merge_intervals([(max(s, start), min(e, end)) for s, e in busy if e > start and s < end]):
        if s > cur:
            slots.append((cur, s))
        cur = max(cur, e)
    if cur < end:
        slots.append((cur, end))
    return slots


def _at(d: date, t: time, tz) -> datetime:
    return datetime.combine(d, t, tzinfo=tz)


def _ceil15(dt: datetime) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    return dt + timedelta(minutes=(-dt.minute) % 15)


def day_window(d: date, cfg: Config) -> Interval:
    tz, t = cfg.timezone, cfg.tracker
    start = _at(d, t.day_start, tz)
    end = _at(d, t.day_end, tz)
    if end <= start:  # day_end after midnight
        end = _at(d, time(23, 59), tz)
    return start, end


def _pick(day_slots: list[Interval], minutes: int, windows: list[Interval],
          latest: bool = False) -> Interval | None:
    """Best slot of `minutes`: anywhere inside a preferred window first, then anywhere free.
    latest=True packs toward the end (used for leisure, so fun comes after the work)."""
    inside: list[Interval] = []
    rest: list[Interval] = []
    for slot in day_slots:
        parts = [(max(slot[0], ws), min(slot[1], we)) for ws, we in windows if we > slot[0] and ws < slot[1]]
        parts = [(a, b) for a, b in parts if b > a]
        inside += parts
        rest += free_slots(slot[0], slot[1], parts)
    need = timedelta(minutes=minutes)
    for group in (sorted(inside), sorted(rest)):
        for s, e in (reversed(group) if latest else group):
            if e - s >= need:
                return (e - need, e) if latest else (s, s + need)
    return None


# ── the planner ─────────────────────────────────────────────────────────────
class Planner:
    def __init__(self, db: DB, cfg: Config, radar: Radar, tracker: Tracker,
                 clock: Callable[[], datetime] | None = None):
        self.db, self.cfg, self.radar, self.tracker = db, cfg, radar, tracker
        self.clock = clock or radar.now
        self.best_hours: list[int] = []  # filled from analysis so prep lands in your best hours

    def now(self) -> datetime:
        return self.clock().astimezone(self.cfg.timezone)

    # ── events ──────────────────────────────────────────────────────────────
    def _clean_event(self, data: dict) -> dict:
        title = str(data.get("title", "")).strip()
        if not title:
            raise ValueError("title is required")
        kind = data.get("kind", "event")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        at = data.get("at")
        at_dt = at if isinstance(at, datetime) else datetime.fromisoformat(str(at))
        if at_dt.tzinfo is None:
            at_dt = at_dt.replace(tzinfo=self.cfg.timezone)
        prep = data.get("prep_hours")
        prep = None if prep in (None, "") else float(prep)
        if prep is not None and not 0 <= prep <= 100:
            raise ValueError("prep_hours must be 0–100")
        return {
            "title": title, "kind": kind, "course": (data.get("course") or None),
            "at": iso(at_dt.astimezone(self.cfg.timezone)),
            "duration": int(data.get("duration") or 0),
            "difficulty": max(1, min(5, int(data.get("difficulty") or 3))),
            "readiness": max(1, min(5, int(data.get("readiness") or 1))),
            "prep_hours": prep, "notes": str(data.get("notes") or ""),
        }

    def add_event(self, data: dict, plan: bool = True) -> dict:
        row = self._clean_event(data)
        row["created_at"] = iso(self.now())
        eid = self.db.insert("events", row)
        if plan:
            self.replan_event(eid)
        return self.event(eid)

    def update_event(self, eid: int, data: dict, plan: bool = True) -> dict:
        cur = self.db.one("SELECT * FROM events WHERE id=?", (eid,))
        if not cur:
            raise KeyError(eid)
        row = self._clean_event({**cur, **data})
        sets = ", ".join(f"{k}=:{k}" for k in row)
        self.db.execute(f"UPDATE events SET {sets} WHERE id=:id", {**row, "id": eid})
        if "done" in data:
            self.db.execute("UPDATE events SET done=? WHERE id=?", (1 if data["done"] else 0, eid))
        if plan:
            self.replan_event(eid)
        return self.event(eid)

    def delete_event(self, eid: int) -> None:
        self.db.execute("DELETE FROM blocks WHERE kind='prep' AND ref_id=? AND status='planned'", (eid,))
        self.db.execute("DELETE FROM events WHERE id=?", (eid,))

    def logged_prep(self, eid: int) -> int:
        row = self.db.one("SELECT COALESCE(SUM(minutes),0) AS m FROM activities WHERE event_id=? AND end IS NOT NULL",
                          (eid,))
        return int(row["m"])

    def event(self, eid: int) -> dict:
        e = self.db.one("SELECT * FROM events WHERE id=?", (eid,))
        if not e:
            raise KeyError(eid)
        return self._decorate(e)

    def events(self, include_past: bool = False) -> list[dict]:
        rows = self.db.query("SELECT * FROM events ORDER BY at")
        now = self.now()
        if not include_past:
            rows = [e for e in rows if parse_dt(e["at"]) >= now - timedelta(hours=12) and not e["done"]]
        return [self._decorate(e) for e in rows]

    def _decorate(self, e: dict) -> dict:
        est = prep_estimate(e["kind"], e["difficulty"], e["readiness"], e["prep_hours"], self.logged_prep(e["id"]))
        blocks = self.db.query("SELECT * FROM blocks WHERE kind='prep' AND ref_id=? ORDER BY start", (e["id"],))
        now = self.now()
        upcoming = [b for b in blocks if b["status"] == "planned" and parse_dt(b["end"]) > now]
        sched = sum(_mins(b) for b in upcoming)
        short = max(0, est["remaining_minutes"] - sched)
        if short < self.cfg.planner.min_block_minutes:  # rounding leftovers aren't worth a warning
            short = 0
        e = dict(e)
        e["prep"] = {**est, "scheduled_minutes": sched, "unscheduled_minutes": short,
                     "needed_hours": round(est["needed_minutes"] / 60, 1),
                     "scheduled_hours": round(sched / 60, 1)}
        e["blocks"] = blocks
        return e

    # ── busy time ─────────────────────────────────────────────────────────
    def busy(self, start: datetime, end: datetime, exclude_block_ids: set[int] = frozenset()) -> list[Interval]:
        buf = timedelta(minutes=self.cfg.planner.buffer_minutes)
        iv: list[Interval] = []
        for b in self.radar.blocks_between(start, end):
            iv.append((b.start - buf, b.end + buf))
        for e in self.db.query("SELECT * FROM events WHERE duration > 0 AND done=0"):
            s = parse_dt(e["at"])
            iv.append((s - buf, s + timedelta(minutes=e["duration"]) + buf))
        for b in self.db.query("SELECT * FROM blocks WHERE status IN ('planned','done','partial') "
                               "AND end > ? AND start < ?", (iso(start), iso(end))):
            if b["id"] not in exclude_block_ids:
                iv.append((parse_dt(b["start"]) - buf, parse_dt(b["end"]) + buf))
        return merge_intervals(iv)

    def _planned_on(self, d: date) -> int:
        return sum(_mins(b) for b in self.db.query(
            "SELECT * FROM blocks WHERE day=? AND kind='prep' AND status='planned'", (d.isoformat(),)))

    def _focus_windows(self, d: date) -> list[Interval]:
        tz = self.cfg.timezone
        if self.best_hours:
            return merge_intervals([(_at(d, time(h), tz), _at(d, time(h), tz) + timedelta(hours=1))
                                    for h in self.best_hours])
        return [(_at(d, a, tz), _at(d, b, tz)) for a, b in self.cfg.planner.focus_windows]

    # ── prep scheduling ───────────────────────────────────────────────────
    def _clear_future(self, kind: str, ref_id: int | None = None) -> None:
        now = iso(self.now())
        if ref_id is None:
            self.db.execute("DELETE FROM blocks WHERE kind=? AND status='planned' AND start > ?", (kind, now))
        else:
            self.db.execute("DELETE FROM blocks WHERE kind=? AND ref_id=? AND status='planned' AND start > ?",
                            (kind, ref_id, now))

    def replan_event(self, eid: int) -> dict:
        self._clear_future("prep", eid)
        e = self.db.one("SELECT * FROM events WHERE id=?", (eid,))
        if not e or e["done"]:
            return {"placed": 0, "unplaced": 0}
        est = prep_estimate(e["kind"], e["difficulty"], e["readiness"], e["prep_hours"], self.logged_prep(eid))
        if not est["needs_prep"]:
            return {"placed": 0, "unplaced": 0, "reason": est["reason"]}
        return self._place_prep(e, est["remaining_minutes"])

    def _place_prep(self, e: dict, remaining: int) -> dict:
        p = self.cfg.planner
        now = self.now()
        start = _ceil15(now + timedelta(minutes=15))
        deadline = parse_dt(e["at"]) - timedelta(minutes=END_BUFFER.get(e["kind"], 60))
        if deadline <= start:
            return {"placed": 0, "unplaced": remaining, "reason": "no time left before the event"}

        days = [start.date() + timedelta(days=i) for i in range((deadline.date() - start.date()).days + 1)]
        per_day_event = {d: 0 for d in days}
        # spread: no single day takes more than its fair share (rounded up to a block), capped per kind
        fair = math.ceil(remaining / len(days) / p.block_minutes) * p.block_minutes
        cap_event = min(MAX_PREP_PER_DAY.get(e["kind"], 180), max(p.block_minutes, fair))
        busy = self.busy(start, deadline + timedelta(days=1))
        placed: list[tuple[datetime, datetime]] = []

        progress = True
        while remaining > 0 and progress:
            progress = False
            for d in days:
                if remaining <= 0:
                    break
                chunk = min(p.block_minutes, remaining)
                chunk = max(chunk, p.min_block_minutes)
                if per_day_event[d] + chunk > cap_event:
                    continue
                if self._planned_on(d) + sum(_span(s, t) for s, t in placed if s.date() == d) + chunk \
                        > p.max_planned_minutes_per_day:
                    continue
                ws, we = day_window(d, self.cfg)
                ws, we = max(ws, start), min(we, deadline)
                if we <= ws:
                    continue
                slot = _pick(free_slots(ws, we, busy), chunk, self._focus_windows(d))
                if not slot:
                    continue
                placed.append(slot)
                per_day_event[d] += chunk
                remaining -= chunk
                buf = timedelta(minutes=p.buffer_minutes)
                busy = merge_intervals(busy + [(slot[0] - buf, slot[1] + buf)])
                progress = True

        placed.sort()
        verb = {"exam": "Study", "interview": "Prep", "presentation": "Rehearse"}.get(e["kind"], "Work on")
        cat = "project" if e["kind"] == "project" else "study"
        for i, (s, t) in enumerate(placed):
            title = f"{verb}: {e['title']}"
            if e["kind"] == "exam" and i == len(placed) - 1 and len(placed) > 1:
                title = f"Final review: {e['title']}"
            self.db.insert("blocks", {"kind": "prep", "ref_id": e["id"], "title": title, "category": cat,
                                      "start": iso(s), "end": iso(t), "day": s.date().isoformat(),
                                      "status": "planned", "created_at": iso(now)})
        return {"placed": len(placed), "placed_minutes": sum(_span(s, t) for s, t in placed),
                "unplaced": max(0, remaining)}

    # ── habits ────────────────────────────────────────────────────────────
    def add_habit(self, data: dict) -> dict:
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("name is required")
        window = data.get("window", "any")
        if window not in self.cfg.planner.habit_windows:
            raise ValueError(f"window must be one of {', '.join(self.cfg.planner.habit_windows)}")
        minutes, per_week = int(data.get("minutes") or 30), int(data.get("per_week") or 3)
        if not (5 <= minutes <= 480 and 1 <= per_week <= 7):
            raise ValueError("minutes 5–480, per_week 1–7")
        row = {"name": name, "category": self.tracker._check_category(data.get("category"), name),
               "minutes": minutes, "per_week": per_week, "window": window, "active": 1,
               "created_at": iso(self.now())}
        row["id"] = self.db.insert("habits", row)
        return row

    def update_habit(self, hid: int, data: dict) -> None:
        allowed = {k: data[k] for k in ("name", "category", "minutes", "per_week", "window", "active") if k in data}
        if allowed:
            sets = ", ".join(f"{k}=:{k}" for k in allowed)
            self.db.execute(f"UPDATE habits SET {sets} WHERE id=:id", {**allowed, "id": hid})

    def delete_habit(self, hid: int) -> None:
        self.db.execute("DELETE FROM blocks WHERE kind='habit' AND ref_id=? AND status='planned'", (hid,))
        self.db.execute("DELETE FROM habits WHERE id=?", (hid,))

    def habits(self) -> list[dict]:
        now = self.now()
        week_start = now.date() - timedelta(days=now.weekday())
        out = []
        for h in self.db.query("SELECT * FROM habits ORDER BY active DESC, name"):
            h["done_this_week"] = self._habit_done(h, week_start)
            streak, ws = 0, week_start - timedelta(days=7)
            while streak < 52 and self._habit_done(h, ws) >= h["per_week"]:
                streak += 1
                ws -= timedelta(days=7)
            h["streak_weeks"] = streak
            h["upcoming"] = self.db.query(
                "SELECT * FROM blocks WHERE kind='habit' AND ref_id=? AND status='planned' AND start > ? ORDER BY start",
                (h["id"], iso(now)))
            out.append(h)
        return out

    def _habit_done(self, h: dict, week_start: date) -> int:
        we = week_start + timedelta(days=6)
        via_blocks = self.db.one(
            "SELECT COUNT(*) AS n FROM blocks WHERE kind='habit' AND ref_id=? AND status IN ('done','partial') "
            "AND day BETWEEN ? AND ?", (h["id"], week_start.isoformat(), we.isoformat()))["n"]
        manual_days = self.db.one(
            "SELECT COUNT(DISTINCT day) AS n FROM activities WHERE ref IS NULL AND LOWER(label)=LOWER(?) "
            "AND day BETWEEN ? AND ?", (h["name"], week_start.isoformat(), we.isoformat()))["n"]
        return via_blocks + manual_days

    def schedule_habits(self, days: int = 7) -> dict:
        self._clear_future("habit")
        now = self.now()
        start = _ceil15(now + timedelta(minutes=15))
        first = start.date()
        horizon = [first + timedelta(days=i) for i in range(days)]
        end = _at(horizon[-1], time(23, 59), self.cfg.timezone)
        busy = self.busy(start, end)
        buf = timedelta(minutes=self.cfg.planner.buffer_minutes)
        placed, missed = 0, []
        week_start = now.date() - timedelta(days=now.weekday())
        for h in sorted(self.db.query("SELECT * FROM habits WHERE active=1"), key=lambda h: -h["per_week"]):
            # only schedule what's still owed this week (plus next week's share if the horizon crosses Monday)
            want = min(days, max(0, h["per_week"] - self._habit_done(h, week_start)) if days <= 7 else h["per_week"])
            if want == 0:
                continue
            targets = sorted({round(i * days / want) for i in range(want)})
            used: set[date] = set()
            for idx in targets:
                ok = False
                for d in horizon[idx:] + horizon[:idx]:
                    if d in used:
                        continue
                    ws, we = self.cfg.planner.habit_windows[h["window"]]
                    lo, hi = day_window(d, self.cfg)
                    win = (max(_at(d, ws, self.cfg.timezone), start, lo), min(_at(d, we, self.cfg.timezone), hi))
                    if win[1] <= win[0]:
                        continue
                    leisure = self.cfg.tracker.kind_of(h["category"]) == "leisure"
                    slot = _pick(free_slots(win[0], win[1], busy), h["minutes"], [], latest=leisure)
                    if slot:
                        self.db.insert("blocks", {"kind": "habit", "ref_id": h["id"], "title": h["name"],
                                                  "category": h["category"], "start": iso(slot[0]),
                                                  "end": iso(slot[1]), "day": d.isoformat(), "status": "planned",
                                                  "created_at": iso(now)})
                        busy = merge_intervals(busy + [(slot[0] - buf, slot[1] + buf)])
                        used.add(d)
                        placed += 1
                        ok = True
                        break
                if not ok:
                    missed.append(h["name"])
        return {"placed": placed, "missed": missed}

    def replan_all(self) -> dict:
        """Wipe future planned blocks and rebuild: prep for every event (soonest first), then habits."""
        self._clear_future("prep")
        self._clear_future("habit")
        results = {}
        for e in self.db.query("SELECT * FROM events WHERE done=0 ORDER BY at"):
            if parse_dt(e["at"]) > self.now():
                results[e["id"]] = self.replan_event(e["id"])
        return {"events": results, "habits": self.schedule_habits()}

    # ── views / export ────────────────────────────────────────────────────
    def blocks(self, start: date, end: date) -> list[dict]:
        return self.db.query("SELECT * FROM blocks WHERE day BETWEEN ? AND ? ORDER BY start",
                             (start.isoformat(), end.isoformat()))

    def to_ics(self) -> str:
        now = self.now()
        lo, hi = (now - timedelta(days=14)).date(), (now + timedelta(days=60)).date()
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//DUE-E//Planner//EN", "CALSCALE:GREGORIAN",
                 "X-WR-CALNAME:DUE-E plan", "REFRESH-INTERVAL;VALUE=DURATION:PT30M"]
        stamp = now.astimezone(_UTC).strftime("%Y%m%dT%H%M%SZ")

        def ev(uid, title, s, e, desc=""):
            lines.extend(["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
                          f"DTSTART:{s.astimezone(_UTC).strftime('%Y%m%dT%H%M%SZ')}",
                          f"DTEND:{e.astimezone(_UTC).strftime('%Y%m%dT%H%M%SZ')}",
                          f"SUMMARY:{_ics_escape(title)}"] + ([f"DESCRIPTION:{_ics_escape(desc)}"] if desc else [])
                         + ["END:VEVENT"])

        for b in self.blocks(lo, hi):
            if b["status"] == "skipped":
                continue
            ev(f"block-{b['id']}@due-e", ("✓ " if b["status"] == "done" else "") + b["title"],
               parse_dt(b["start"]), parse_dt(b["end"]), f"DUE-E {b['kind']} block")
        for e in self.db.query("SELECT * FROM events"):
            s = parse_dt(e["at"])
            ev(f"event-{e['id']}@due-e", f"{e['kind'].title()}: {e['title']}", s,
               s + timedelta(minutes=e["duration"] or 15), e["notes"] or "")
        lines.append("END:VCALENDAR")
        return "\r\n".join(lines) + "\r\n"


_UTC = timezone.utc


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")


def _span(s: datetime, e: datetime) -> int:
    return int((e - s).total_seconds() // 60)


def _mins(b: dict) -> int:
    return _span(parse_dt(b["start"]), parse_dt(b["end"]))
