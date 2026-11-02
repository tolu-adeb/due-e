"""Tracker: log what you did (manual, timers, Discord) and confirm your calendar at the end of the day."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Callable, Iterable

from .config import KINDS, Category, Config
from .db import DB
from .feeds import CalBlock

STATUSES = ("done", "partial", "skipped", "cancelled")
_CAT_NAME = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,23}$")


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Tracker:
    def __init__(self, db: DB, cfg: Config, clock: Callable[[], datetime]):
        self.db, self.cfg, self.clock = db, cfg, clock
        self._load_custom()

    # Your own types live in the DB and are merged into the config's list at startup, so every part
    # of DUE-E (analysis, planner, web) sees them the same way.
    def _load_custom(self) -> None:
        known = {c.name for c in self.cfg.tracker.categories}
        for row in self.db.query("SELECT * FROM categories ORDER BY created_at"):
            if row["name"] not in known:
                self.cfg.tracker.categories.append(Category(row["name"], row["kind"], []))
                known.add(row["name"])
        self._learned = [(r["keyword"], r["category"]) for r in
                         self.db.query("SELECT keyword, category FROM category_keywords")]
        self._learned.sort(key=lambda kc: -len(kc[0]))  # most specific phrase wins
        self._patterns = [
            (c.name, re.compile(r"\b(" + "|".join(re.escape(m) for m in c.match) + r")\b", re.IGNORECASE))
            for c in self.cfg.tracker.categories if c.match
        ]

    def now(self) -> datetime:
        return self.clock().astimezone(self.cfg.timezone)

    # ── categories ────────────────────────────────────────────────────────────
    def categorize(self, label: str) -> str:
        low = " ".join(label.lower().split())
        for kw, cat in self._learned:  # what you taught DUE-E beats the defaults
            if re.search(r"\b" + re.escape(kw) + r"\b", low):
                return cat
        for name, pat in self._patterns:
            if pat.search(label):
                return name
        return "other"

    def category_names(self) -> list[str]:
        return [c.name for c in self.cfg.tracker.categories] + ["other"]

    def _check_category(self, category: str | None, label: str) -> str:
        if not category:
            return self.categorize(label)
        if category not in self.category_names():
            raise ValueError(f"unknown category {category!r} — create it first (name + kind)")
        return category

    def add_category(self, name: str, kind: str) -> dict:
        name = " ".join(str(name).strip().lower().split())
        if not _CAT_NAME.match(name):
            raise ValueError("type name: 1–24 chars, letters/numbers/spaces/-/_")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if name in self.category_names():
            existing = self.cfg.tracker.category(name)
            return {"name": name, "kind": existing.kind if existing else "maintenance", "created": False}
        self.db.execute("INSERT INTO categories (name, kind, created_at) VALUES (?,?,?)",
                        (name, kind, iso(self.now())))
        self._load_custom()
        return {"name": name, "kind": kind, "created": True}

    def learn(self, label: str, category: str) -> bool:
        """Remember that this label means this category — only when it differs from what DUE-E would guess."""
        phrase = " ".join(label.lower().split())
        if not phrase or len(phrase) > 40 or category == "other" or self.categorize(label) == category:
            return False
        self.db.execute("INSERT INTO category_keywords (keyword, category, created_at) VALUES (?,?,?) "
                        "ON CONFLICT(keyword) DO UPDATE SET category=excluded.category",
                        (phrase, category, iso(self.now())))
        self._load_custom()
        return True

    def update_activity(self, activity_id: int, category: str | None = None, label: str | None = None,
                        focus: int | None = None) -> dict:
        row = self.db.one("SELECT * FROM activities WHERE id=?", (activity_id,))
        if not row:
            raise KeyError(activity_id)
        if category is not None:
            category = self._check_category(category, row["label"])
            self.learn(label or row["label"], category)
            row["category"] = category
        if label:
            row["label"] = label.strip()
        if focus is not None:
            if not 1 <= int(focus) <= 5:
                raise ValueError("focus must be 1–5")
            row["focus"] = int(focus)
        self.db.execute("UPDATE activities SET category=?, label=?, focus=? WHERE id=?",
                        (row["category"], row["label"], row["focus"], activity_id))
        return row

    # ── logging ───────────────────────────────────────────────────────────────
    def log(self, label: str, minutes: int | None = None, start: datetime | None = None,
            end: datetime | None = None, category: str | None = None, focus: int | None = None,
            source: str = "manual", ref: str | None = None, event_id: int | None = None,
            notes: str = "") -> dict:
        label = label.strip()
        if not label:
            raise ValueError("label is required")
        if focus is not None and not 1 <= int(focus) <= 5:
            raise ValueError("focus must be 1–5")
        tz = self.cfg.timezone
        if start and end:
            start, end = start.astimezone(tz), end.astimezone(tz)
        elif start and minutes:
            start = start.astimezone(tz)
            end = start + timedelta(minutes=int(minutes))
        elif minutes:
            end = (end or self.now()).astimezone(tz)
            start = end - timedelta(minutes=int(minutes))
        else:
            raise ValueError("give minutes, or a start and end time")
        mins = int((end - start).total_seconds() // 60)
        if mins <= 0 or mins > 24 * 60:
            raise ValueError("duration must be between 1 minute and 24 hours")
        if category:
            category = self._check_category(category, label)
            if source in ("manual", "discord", "timer"):
                self.learn(label, category)
        row = {
            "label": label, "category": self._check_category(category, label),
            "start": iso(start), "end": iso(end), "minutes": mins, "day": start.date().isoformat(),
            "focus": focus, "source": source, "ref": ref, "event_id": event_id, "notes": notes,
            "created_at": iso(self.now()),
        }
        row["id"] = self.db.insert("activities", row)
        return row

    def delete(self, activity_id: int) -> bool:
        self.db.execute("UPDATE confirmations SET activity_id=NULL WHERE activity_id=?", (activity_id,))
        return self.db.execute("DELETE FROM activities WHERE id=?", (activity_id,)) > 0

    def activities(self, start_day: date, end_day: date) -> list[dict]:
        return self.db.query(
            "SELECT * FROM activities WHERE end IS NOT NULL AND day BETWEEN ? AND ? ORDER BY start",
            (start_day.isoformat(), end_day.isoformat()))

    # ── timers ────────────────────────────────────────────────────────────────
    def running(self) -> dict | None:
        return self.db.one("SELECT * FROM activities WHERE end IS NULL ORDER BY start DESC LIMIT 1")

    def start_timer(self, label: str, category: str | None = None, event_id: int | None = None,
                    source: str = "timer") -> dict:
        if not label.strip():
            raise ValueError("label is required")
        if category:
            self.learn(label, self._check_category(category, label))
        stopped = self.stop_timer()
        now = self.now()
        row = {"label": label.strip(), "category": self._check_category(category, label), "start": iso(now),
               "end": None, "minutes": None, "day": now.date().isoformat(), "focus": None, "source": source,
               "ref": None, "event_id": event_id, "notes": "", "created_at": iso(now)}
        row["id"] = self.db.insert("activities", row)
        row["stopped_previous"] = stopped
        return row

    def stop_timer(self, focus: int | None = None) -> dict | None:
        cur = self.running()
        if not cur:
            return None
        now = self.now()
        mins = max(1, int((now - parse_dt(cur["start"])).total_seconds() // 60))
        if focus is not None and not 1 <= int(focus) <= 5:
            raise ValueError("focus must be 1–5")
        self.db.execute("UPDATE activities SET end=?, minutes=?, focus=? WHERE id=?",
                        (iso(now), mins, focus, cur["id"]))
        return {**cur, "end": iso(now), "minutes": mins, "focus": focus}

    # ── end-of-day confirmation ───────────────────────────────────────────────
    def review_items(self, day: date, cal_blocks: Iterable[CalBlock]) -> list[dict]:
        """Everything that was *planned* for a day: your calendar blocks + DUE-E prep/habit blocks."""
        confs = {c["ref"]: c for c in self.db.query("SELECT * FROM confirmations WHERE day=?", (day.isoformat(),))}
        items = []
        for b in cal_blocks:
            if b.start.date() != day:
                continue
            items.append({"ref": b.ref, "title": b.title, "start": iso(b.start), "end": iso(b.end),
                          "planned_minutes": b.minutes, "kind": "calendar", "source": b.source,
                          "category": self.categorize(b.title), "event_id": None})
        for blk in self.db.query("SELECT * FROM blocks WHERE day=? ORDER BY start", (day.isoformat(),)):
            mins = int((parse_dt(blk["end"]) - parse_dt(blk["start"])).total_seconds() // 60)
            items.append({"ref": f"block:{blk['id']}", "title": blk["title"], "start": blk["start"],
                          "end": blk["end"], "planned_minutes": mins, "kind": blk["kind"], "source": "DUE-E",
                          "category": blk["category"], "event_id": blk["ref_id"] if blk["kind"] == "prep" else None})
        for it in items:
            it["is_class"] = self.cfg.tracker.kind_of(it["category"]) == "class"
            c = confs.get(it["ref"])
            it["status"] = c["status"] if c else None
            it["actual_minutes"] = c["actual_minutes"] if c else None
            if c and c.get("category"):
                it["category"] = c["category"]
                it["is_class"] = self.cfg.tracker.kind_of(it["category"]) == "class"
        return sorted(items, key=lambda i: i["start"])

    def confirm(self, item: dict, status: str, actual_minutes: int | None = None,
                category: str | None = None) -> dict:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        ref, start = item["ref"], parse_dt(item["start"])
        planned = int(item["planned_minutes"])
        if category and item["kind"] == "calendar":  # you re-typed a calendar item → remember for next time
            self.learn(item["title"], self._check_category(category, item["title"]))
        category = self._check_category(category or item.get("category"), item["title"])

        prev = self.db.one("SELECT * FROM confirmations WHERE ref=?", (ref,))
        if prev and prev.get("activity_id"):
            self.db.execute("DELETE FROM activities WHERE id=?", (prev["activity_id"],))

        activity_id = None
        if status in ("done", "partial"):
            minutes = int(actual_minutes) if actual_minutes else (planned if status == "done" else max(1, planned // 2))
            if not self._already_logged(start, minutes, category):
                act = self.log(item["title"], minutes=minutes, start=start, category=category,
                               source="plan" if ref.startswith("block:") else "calendar", ref=ref,
                               event_id=item.get("event_id"))
                activity_id = act["id"]
            actual_minutes = minutes
        else:
            actual_minutes = 0

        self.db.execute(
            """INSERT INTO confirmations (ref, day, title, start, end, planned_minutes, status, actual_minutes,
                                          category, activity_id, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ref) DO UPDATE SET status=excluded.status, actual_minutes=excluded.actual_minutes,
                 category=excluded.category, activity_id=excluded.activity_id, updated_at=excluded.updated_at""",
            (ref, start.date().isoformat(), item["title"], item["start"], item["end"], planned, status,
             actual_minutes, category, activity_id, iso(self.now())))
        if ref.startswith("block:"):
            self.db.execute("UPDATE blocks SET status=? WHERE id=?",
                            ("skipped" if status == "cancelled" else status, int(ref.split(":")[1])))
        return {"ref": ref, "status": status, "actual_minutes": actual_minutes, "activity_id": activity_id}

    def _already_logged(self, start: datetime, minutes: int, category: str) -> bool:
        """Don't double-count when you both ran a timer and then confirmed the calendar block."""
        end = start + timedelta(minutes=minutes)
        for a in self.db.query("SELECT * FROM activities WHERE end IS NOT NULL AND day=? AND category=? "
                               "AND ref IS NULL", (start.date().isoformat(), category)):
            a_s, a_e = parse_dt(a["start"]), parse_dt(a["end"])
            overlap = (min(end, a_e) - max(start, a_s)).total_seconds() / 60
            if overlap >= 0.5 * minutes:
                return True
        return False

    def confirmations(self, start_day: date, end_day: date) -> list[dict]:
        return self.db.query("SELECT * FROM confirmations WHERE day BETWEEN ? AND ? ORDER BY start",
                             (start_day.isoformat(), end_day.isoformat()))
