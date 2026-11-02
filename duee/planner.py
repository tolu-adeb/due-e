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

from . import reader as rd
from .config import Config
from .db import DB
from .radar import Radar
from .tracker import Tracker, iso, parse_dt

KINDS = ("exam", "quiz", "project", "assignment", "essay", "lab", "reading", "presentation", "interview", "event")
BASE_HOURS = {"exam": 8, "quiz": 1.5, "project": 10, "assignment": 3, "essay": 4, "lab": 2, "reading": 1,
              "presentation": 3, "interview": 5, "event": 0}
# stop working this long before it's due / happens (submit buffer, or rest before an exam)
END_BUFFER = {"exam": 60, "quiz": 30, "interview": 60, "presentation": 60, "project": 120, "assignment": 120,
              "essay": 120, "lab": 60, "reading": 30, "event": 0}
MAX_PREP_PER_DAY = {"exam": 180, "quiz": 90, "project": 240, "assignment": 180, "essay": 180, "lab": 120,
                    "reading": 90, "presentation": 120, "interview": 120, "event": 0}
DEEP_KINDS = {"exam", "project", "essay", "presentation", "interview"}
VERB = {"exam": "Study", "quiz": "Review", "interview": "Prep", "presentation": "Rehearse", "reading": "Read"}
PROGRESS_KINDS = {"project", "assignment", "essay", "lab", "reading"}  # "how far along?" instead of "how ready?"
NO_PREP_BELOW = 30
WINDOWS = ("morning", "afternoon", "evening", "any")

Interval = tuple[datetime, datetime]


# ── pure helpers (easy to test) ─────────────────────────────────────────────
def prep_estimate(kind: str, difficulty: int, readiness: int, prep_hours: float | None,
                  logged_minutes: int, est_minutes: int | None = None, pace: float = 1.0) -> dict:
    ready_gap = (5 - max(1, min(5, readiness))) / 4             # readiness/progress 5 → nothing left
    if prep_hours is not None:
        needed = float(prep_hours) * 60
        basis = "your estimate"
    elif est_minutes:
        needed = est_minutes * pace * ready_gap                  # the reader's estimate × your pace
        basis = "task reader" + (f" × your pace {pace:.2f}" if pace != 1 else "")
    else:
        diff = 0.4 + 0.2 * max(1, min(5, difficulty))          # 1 → 0.6×, 3 → 1.0×, 5 → 1.4×
        needed = BASE_HOURS.get(kind, 0) * 60 * diff * ready_gap * pace
        basis = f"{kind}, difficulty {difficulty}/5, readiness {readiness}/5"
    remaining = max(0, round(needed - logged_minutes))
    if BASE_HOURS.get(kind, 0) == 0 and prep_hours is None and not est_minutes:
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


def _pick_flex(day_slots: list[Interval], minutes: int, windows: list[Interval],
               min_minutes: int) -> Interval | None:
    """Like _pick, but prefers a slightly shorter block inside a preferred window over a full block outside it.
    Order: full block in a window → biggest shortened block (≥ min) in a window → full block anywhere →
    shortened block anywhere."""
    inside: list[Interval] = []
    rest: list[Interval] = []
    for slot in day_slots:
        parts = [(max(slot[0], ws), min(slot[1], we)) for ws, we in windows if we > slot[0] and ws < slot[1]]
        parts = [(a, b) for a, b in parts if b > a]
        inside += parts
        rest += free_slots(slot[0], slot[1], parts)
    need, low = timedelta(minutes=minutes), timedelta(minutes=min_minutes)
    for group in (sorted(inside), sorted(inside + rest)):
        for s, e in group:
            if e - s >= need:
                return s, s + need
        fits = [(e - s, s) for s, e in group if e - s >= low]
        if fits:
            length, s = max(fits, key=lambda x: (x[0], -x[1].timestamp()))
            length = timedelta(minutes=(int(length.total_seconds() // 60) // 15) * 15)
            if length >= low:
                return s, s + length
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

    def update_event(self, eid: int, data: dict, plan: bool = True, by_user: bool = True) -> dict:
        cur = self.db.one("SELECT * FROM events WHERE id=?", (eid,))
        if not cur:
            raise KeyError(eid)
        row = self._clean_event({**cur, **data})
        sets = ", ".join(f"{k}=:{k}" for k in row)
        self.db.execute(f"UPDATE events SET {sets} WHERE id=:id", {**row, "id": eid})
        for flag in ("done", "ignored"):
            if flag in data:
                self.db.execute(f"UPDATE events SET {flag}=? WHERE id=?", (1 if data[flag] else 0, eid))
        if by_user and cur.get("source") == "canvas" and ({"prep_hours", "kind", "at", "title"} & set(data)):
            self.db.execute("UPDATE events SET user_edited=1 WHERE id=?", (eid,))  # sync keeps your edits
        if data.get("done") or data.get("ignored"):
            self._clear_future("prep", eid)
        elif plan:
            self.replan_event(eid)
        return self.event(eid)

    def delete_event(self, eid: int) -> None:
        self.db.execute("DELETE FROM blocks WHERE kind='prep' AND ref_id=? AND status='planned'", (eid,))
        row = self.db.one("SELECT source FROM events WHERE id=?", (eid,))
        if row and row["source"] == "canvas":
            # a deleted Canvas task would come straight back on the next sync, so hide it instead
            self.db.execute("UPDATE events SET ignored=1 WHERE id=?", (eid,))
        else:
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
            rows = [e for e in rows if parse_dt(e["at"]) >= now - timedelta(hours=12)
                    and not e["done"] and not e.get("ignored")]
        return [self._decorate(e) for e in rows]

    # ── what each task needs ──────────────────────────────────────────────
    def history(self) -> list[dict]:
        """Finished tasks with the reader's estimate and the time you actually logged — fuels your pace."""
        now = iso(self.now())
        rows = self.db.query("SELECT id, course, kind, est_minutes FROM events WHERE est_minutes > 0 "
                             "AND (done=1 OR at < ?) AND ignored=0", (now,))
        return [{**r, "actual_minutes": self.logged_prep(r["id"])} for r in rows]

    def own_blocks(self, e: dict, open_tasks: list[dict] | None = None) -> list:
        """Future blocks on YOUR calendar that are clearly about this task ("Study 281 Project 2")."""
        now = self.now()
        due = parse_dt(e["at"])
        rows = self._open_rows() if open_tasks is None else open_tasks
        return [b for b in self.radar.blocks_between(now, due)
                if b.start >= now and self.tracker.categorize(b.title) != "class"
                and self.match_task(b.title, rows) == e["id"]]

    def _estimate(self, e: dict, history: list[dict] | None = None) -> tuple[dict, str | None]:
        pace, why = (1.0, None)
        if e.get("prep_hours") is None:
            pace, why = rd.calibrate(self.history() if history is None else history, e.get("course"), e["kind"])
        est = prep_estimate(e["kind"], e["difficulty"], e["readiness"], e["prep_hours"],
                            self.logged_prep(e["id"]), e.get("est_minutes"), pace)
        return est, why

    def _is_deep(self, e: dict, needed: int) -> bool:
        r = rd.TaskRead.from_json(e.get("reader"))
        if r is not None:
            return r.deep
        return e["kind"] in DEEP_KINDS or (e["kind"] == "assignment" and needed >= 150)

    def _decorate(self, e: dict) -> dict:
        est, pace_why = self._estimate(e)
        blocks = self.db.query("SELECT * FROM blocks WHERE kind='prep' AND ref_id=? ORDER BY start", (e["id"],))
        now = self.now()
        upcoming = [b for b in blocks if b["status"] == "planned" and parse_dt(b["end"]) > now]
        sched = sum(_mins(b) for b in upcoming)
        short = max(0, est["remaining_minutes"] - sched)
        if short < self.cfg.planner.min_block_minutes:  # rounding leftovers aren't worth a warning
            short = 0
        e = dict(e)
        own = sum(b.minutes for b in self.own_blocks(e))
        sched += own
        short = max(0, short - own)
        e["prep"] = {**est, "scheduled_minutes": sched, "unscheduled_minutes": short, "your_calendar_minutes": own,
                     "needed_hours": round(est["needed_minutes"] / 60, 1),
                     "scheduled_hours": round(sched / 60, 1)}
        e["blocks"] = blocks
        r = rd.TaskRead.from_json(e.get("reader"))
        deep = self._is_deep(e, est["needed_minutes"])
        why = list(r.signals) if r else []
        if pace_why:
            why.append(pace_why)
        if e["prep"]["your_calendar_minutes"]:
            why.append(f"you already blocked {e['prep']['your_calendar_minutes']}m for it on your calendar")
        if est["needs_prep"]:
            why.append("deep work → long blocks in your best hours" if deep
                       else "light work → short blocks, off-peak, so prime hours stay free")
        e["read"] = {"kind": r.kind if r else e["kind"], "summary": r.summary if r else "", "why": why,
                     "confidence": r.confidence if r else None, "source": r.source if r else "you",
                     "deep": deep, "progress_label": "progress" if e["kind"] in PROGRESS_KINDS else "readiness"}
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
        if e.get("ignored"):
            return {"placed": 0, "unplaced": 0}
        est, _ = self._estimate(e)
        if not est["needs_prep"]:
            return {"placed": 0, "unplaced": 0, "reason": est["reason"]}
        own = sum(b.minutes for b in self.own_blocks(e))   # time you already blocked for it yourself
        remaining = est["remaining_minutes"] - own
        if remaining < NO_PREP_BELOW:
            return {"placed": 0, "unplaced": 0, "reason": "covered by blocks on your own calendar"}
        return self._place_prep(e, remaining, self._is_deep(e, est["needed_minutes"]))

    def _offpeak_windows(self, d: date) -> list[Interval]:
        ws, we = day_window(d, self.cfg)
        return free_slots(ws, we, self._focus_windows(d))

    def _place_prep(self, e: dict, remaining: int, deep: bool = True) -> dict:
        p = self.cfg.planner
        block = p.deep_block_minutes if deep else p.light_block_minutes
        now = self.now()
        start = _ceil15(now + timedelta(minutes=15))
        deadline = parse_dt(e["at"]) - timedelta(minutes=END_BUFFER.get(e["kind"], 60))
        if deadline <= start:
            return {"placed": 0, "unplaced": remaining, "reason": "no time left before the event"}

        days = [start.date() + timedelta(days=i) for i in range((deadline.date() - start.date()).days + 1)]
        per_day_event = {d: 0 for d in days}
        # spread: no single day takes more than its fair share (rounded up to a block), capped per kind
        fair = math.ceil(remaining / len(days) / block) * block
        cap_event = min(max(MAX_PREP_PER_DAY.get(e["kind"], 180), block), max(block, fair))
        busy = self.busy(start, deadline + timedelta(days=1))
        placed: list[tuple[datetime, datetime]] = []

        progress = True
        while remaining > 0 and progress:
            progress = False
            for d in days:
                if remaining <= 0:
                    break
                chunk = min(block, remaining)
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
                windows = self._focus_windows(d) if deep else self._offpeak_windows(d)
                slot = _pick_flex(free_slots(ws, we, busy), chunk, windows, p.min_block_minutes)
                if not slot:
                    continue
                got = _span(*slot)
                placed.append(slot)
                per_day_event[d] += got
                remaining -= got
                buf = timedelta(minutes=p.buffer_minutes)
                busy = merge_intervals(busy + [(slot[0] - buf, slot[1] + buf)])
                progress = True

        placed.sort()
        verb = VERB.get(e["kind"], "Work on")
        cat = "project" if e["kind"] == "project" else "study"
        label = f"{e['title']} ({e['course']})" if e.get("course") and e["course"] not in e["title"] else e["title"]
        for i, (s, t) in enumerate(placed):
            title = f"{verb}: {label}"
            if e["kind"] in ("exam", "quiz") and i == len(placed) - 1 and len(placed) > 1:
                title = f"Final review: {label}"
            self.db.insert("blocks", {"kind": "prep", "ref_id": e["id"], "title": title, "category": cat,
                                      "start": iso(s), "end": iso(t), "day": s.date().isoformat(),
                                      "status": "planned", "created_at": iso(now)})
        return {"placed": len(placed), "placed_minutes": sum(_span(s, t) for s, t in placed),
                "unplaced": max(0, remaining)}

    # ── Canvas → tasks ────────────────────────────────────────────────────
    def sync_tasks(self, items, read_fn=None) -> dict:
        """Turn upcoming deadline items into planner tasks, then plan any that are new or changed.

        - Office hours / lectures etc. are skipped (the reader says "not a task").
        - A Canvas exam that matches an event you entered yourself is linked, not duplicated.
        - Moved due dates follow Canvas; your own edits (estimate, kind, time) are never overwritten.
        - All-day exams/quizzes get a placeholder time of 8 AM so study ends the day before.
        """
        read_fn = read_fn or (lambda it: rd.read(it.title, it.course, it.description, it.all_day))
        now = self.now()
        horizon = now + timedelta(days=self.cfg.planner.task_lookahead_days)
        created, updated, linked, skipped = [], [], [], 0
        existing = {e["uid"]: e for e in self.db.query("SELECT * FROM events WHERE uid IS NOT NULL")}
        by_base = {}
        for e in existing.values():
            by_base.setdefault(e["uid"].split("@")[0], []).append(e)

        for it in items:
            if not (now < it.due <= horizon):
                continue
            if it.uid in existing:
                e = existing[it.uid]
                if e["source"] == "canvas" and not e["user_edited"] and not e["done"] and e["title"] != it.title:
                    self.db.execute("UPDATE events SET title=? WHERE id=?", (it.title, e["id"]))
                continue
            r = read_fn(it)
            if r.kind is None:
                skipped += 1
                continue
            at = it.due
            if it.all_day and r.kind in ("exam", "quiz"):
                at = it.due.replace(hour=8, minute=0)
            row = {"title": it.title, "kind": r.kind if r.kind in KINDS else "assignment", "course": it.course,
                   "at": iso(at), "duration": 0, "difficulty": r.difficulty, "readiness": 1, "prep_hours": None,
                   "notes": r.summary, "source": "canvas", "uid": it.uid, "url": it.url,
                   "est_minutes": r.est_minutes, "reader": r.to_json(), "user_edited": 0, "ignored": 0,
                   "done": 0, "created_at": iso(now)}

            # same assignment, new due date (Canvas uid stays, the occurrence stamp changes)
            moved = next((e for e in by_base.get(it.uid.split("@")[0], [])
                          if e["title"] == it.title and not e["done"]), None)
            if moved:
                if not moved["user_edited"]:
                    self.db.execute("UPDATE events SET at=?, uid=? WHERE id=?", (row["at"], it.uid, moved["id"]))
                    updated.append(moved["id"])
                continue
            twin = self._manual_twin(row)
            if twin:
                self.db.execute("UPDATE events SET uid=?, url=COALESCE(url, ?), reader=COALESCE(reader, ?), "
                                "est_minutes=COALESCE(est_minutes, ?) WHERE id=?",
                                (it.uid, it.url, row["reader"], row["est_minutes"], twin["id"]))
                linked.append(twin["id"])
                continue
            created.append(self.db.insert("events", row))

        # something changed → re-plan all open work together, tightest deadline first
        placed = self.rebalance() if (created or updated) else {}
        return {"created": len(created), "updated": len(updated), "linked": len(linked), "skipped": skipped,
                "placed": placed}

    def _manual_twin(self, row: dict) -> dict | None:
        """An event you added by hand that is clearly the same thing (same course, kind, ±1 day)."""
        at = parse_dt(row["at"])
        for e in self.db.query("SELECT * FROM events WHERE uid IS NULL AND done=0 AND kind=?", (row["kind"],)):
            if abs((parse_dt(e["at"]) - at).total_seconds()) > 36 * 3600:
                continue
            same_course = rd._course_key(e["course"]) and rd._course_key(e["course"]) == rd._course_key(row["course"])
            if same_course or rd.match_score(e["title"], row["title"], row["course"]) >= 0.6:
                return e
        return None

    def _open_rows(self) -> list[dict]:
        cutoff = iso(self.now() - timedelta(hours=12))
        return self.db.query("SELECT id, title, course, kind, at FROM events WHERE done=0 AND ignored=0 AND at >= ?",
                             (cutoff,))

    def match_task(self, label: str, open_tasks: list[dict] | None = None) -> int | None:
        """Which open task a logged label is about ("281 project 2" → Project 2). None if unsure."""
        open_tasks = self._open_rows() if open_tasks is None else open_tasks
        scored = sorted(((rd.match_score(label, e["title"], e["course"]), e) for e in open_tasks),
                        key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.6 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.15):
            return scored[0][1]["id"]
        # "370 hw" → the only open 370 assignment
        kind_words = {"assignment": r"\b(hw|homework|pset|problem set)\b", "project": r"\bproject|proj\b",
                      "exam": r"\b(exam|midterm|final|study)\b", "quiz": r"\bquiz\b", "lab": r"\b(lab|prelab)\b",
                      "essay": r"\b(essay|paper)\b", "reading": r"\b(reading|response)\b"}
        import re as _re
        for kind, pat in kind_words.items():
            if _re.search(pat, label, _re.I):
                same = [e for s, e in scored if e["kind"] == kind and s >= 0.3]
                if len(same) == 1:
                    return same[0]["id"]
        return None

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
        for eid in self.by_slack([e["id"] for e in self.db.query(
                "SELECT id, at FROM events WHERE done=0 AND ignored=0") if parse_dt(e["at"]) > self.now()]):
            results[eid] = self.replan_event(eid)
        return {"events": results, "habits": self.schedule_habits()}

    def rebalance(self) -> dict:
        """Re-place all future (not yet done) work blocks, least-slack first. Habits and past blocks stay."""
        self._clear_future("prep")
        ids = [e["id"] for e in self.db.query("SELECT id, at FROM events WHERE done=0 AND ignored=0")
               if parse_dt(e["at"]) > self.now()]
        return {eid: self.replan_event(eid) for eid in self.by_slack(ids)}

    def by_slack(self, ids: list[int]) -> list[int]:
        """Least slack first: (free time before the deadline) − (work left). Tight tasks get first pick of slots."""
        history = self.history()
        now = self.now()
        start = _ceil15(now + timedelta(minutes=15))
        scored = []
        for eid in ids:
            e = self.db.one("SELECT * FROM events WHERE id=?", (eid,))
            est, _ = self._estimate(e, history)
            deadline = parse_dt(e["at"]) - timedelta(minutes=END_BUFFER.get(e["kind"], 60))
            free = 0
            if deadline > start:
                busy = self.busy(start, deadline)
                d = start.date()
                while d <= deadline.date():
                    ws, we = day_window(d, self.cfg)
                    ws, we = max(ws, start), min(we, deadline)
                    if we > ws:
                        free += sum(_span(a, b) for a, b in free_slots(ws, we, busy))
                    d += timedelta(days=1)
            scored.append((free - est["remaining_minutes"], e["at"], eid))
        return [eid for *_, eid in sorted(scored)]

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
