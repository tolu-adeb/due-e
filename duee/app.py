"""Wires the pieces together so the bot, the web app and the CLI share one state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable

from . import analysis
from .config import Config
from .db import DB
from .planner import Planner
from .radar import Radar
from .status import Status
from .tracker import Tracker


@dataclass
class DueE:
    cfg: Config
    radar: Radar
    status: Status
    db: DB
    tracker: Tracker
    planner: Planner
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @classmethod
    def create(cls, cfg: Config, mode: str = "live", clock: Callable[[], datetime] | None = None,
               db_path=None) -> "DueE":
        radar = Radar(cfg, clock=clock)
        db = DB(db_path or cfg.db_file)
        tracker = Tracker(db, cfg, radar.now)
        planner = Planner(db, cfg, radar, tracker, radar.now)
        radar.going = {r["ref"] for r in db.query("SELECT ref FROM rsvps")}
        return cls(cfg, radar, Status(cfg.status_file, mode=mode), db, tracker, planner)

    def set_going(self, ref: str, going: bool) -> None:
        """RSVP to an optional (club) event: going → it blocks time and shows up in your day close-out."""
        if not any(b.ref == ref for b in self.radar.optional):
            raise KeyError(ref)
        if going:
            self.db.execute("INSERT OR IGNORE INTO rsvps (ref, created_at) VALUES (?, ?)", (ref, self.now().isoformat()))
        else:
            self.db.execute("DELETE FROM rsvps WHERE ref=?", (ref,))
        self.radar.going = {r["ref"] for r in self.db.query("SELECT ref FROM rsvps")}

    def now(self) -> datetime:
        return self.radar.now()

    async def refresh(self, force: bool = False) -> None:
        await asyncio.to_thread(self.radar.refresh, force)
        self.status.record_refresh(self.now(), self.radar.feed_results, len(self.radar.items))
        if self.cfg.planner.auto_tasks:
            await self.sync_tasks()

    async def sync_tasks(self) -> dict:
        """Canvas deadlines → planner tasks (reader + optional Claude), then plan what changed."""
        from . import reader as rd

        def read_fn(it):
            return rd.merged_read(self.cfg, it.title, it.course, it.description, it.all_day)

        async with self._lock:
            return await asyncio.to_thread(self.planner.sync_tasks, self.radar.items, read_fn)

    def link_task(self, label: str) -> int | None:
        """Best guess at which task a logged activity counts toward."""
        return self.planner.match_task(label)

    def analyze(self, days: int = 7, end: date | None = None) -> dict:
        end = end or self.now().date()
        start = end - timedelta(days=days - 1)
        an = analysis.analyze(self.tracker.activities(start, end), self.tracker.confirmations(start, end),
                              self.cfg, start, end)
        return an

    def learn_best_hours(self) -> None:
        """Feed the planner your real best focus hours (needs a couple of weeks of focus ratings)."""
        an = self.analyze(28)
        rated = [h for h in an["focus_by_hour"] if h["focus"] is not None]
        self.planner.best_hours = an["best_hours"] if len(rated) >= 3 else []

    async def replan_all(self) -> dict:
        async with self._lock:
            await self.refresh()
            self.learn_best_hours()
            return self.planner.replan_all()
