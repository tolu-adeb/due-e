"""The radar: refresh every feed, merge, dedupe, filter, and answer 'what's coming up?'."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Callable

from .config import Config
from .feeds import CalBlock, FeedResult, Item, load_feed_text, parse_blocks, parse_items

log = logging.getLogger(__name__)

LOOKBACK = timedelta(days=1)
PLAN_LOOKBACK = timedelta(days=8)
LOOKAHEAD = timedelta(days=15)  # covers the weekly view and /due up to 14 days


def merge(items: list[Item]) -> list[Item]:
    """Drop duplicates (same title + due time, or same UID) across feeds. Prefer items with a link."""
    by_key: dict[tuple, Item] = {}
    seen_uids: set[str] = set()
    for it in items:
        if it.uid in seen_uids:
            continue
        seen_uids.add(it.uid)
        existing = by_key.get(it.dedup_key)
        if existing is None or (existing.url is None and it.url is not None):
            if existing is not None and existing.course and not it.course:
                it = replace(it, course=existing.course)
            by_key[it.dedup_key] = it
    return sorted(by_key.values(), key=lambda i: (i.due, i.course or "", i.title))


class Radar:
    def __init__(self, cfg: Config, clock: Callable[[], datetime] | None = None):
        self.cfg = cfg
        self.clock = clock or (lambda: datetime.now(cfg.timezone))
        self._ignore = [re.compile(p, re.IGNORECASE) for p in cfg.ignore_patterns]
        self.items: list[Item] = []
        self.blocks: list[CalBlock] = []  # time blocks from 'plan' calendars
        self.optional: list[CalBlock] = []  # events from 'events' calendars (clubs) — opt-in
        self.going: set[str] = set()  # refs of optional events you said you're going to
        self.feed_results: list[FeedResult] = []
        self.refreshed_at: datetime | None = None
        self._lock = threading.Lock()

    def now(self) -> datetime:
        return self.clock().astimezone(self.cfg.timezone)

    def _ignored(self, item: Item) -> bool:
        return any(p.search(item.title) for p in self._ignore)

    def refresh(self, force: bool = False) -> list[Item]:
        # The scheduler and slash commands can both trigger refreshes from worker threads.
        with self._lock:
            return self._refresh(force)

    def _refresh(self, force: bool) -> list[Item]:
        now = self.now()
        ttl = timedelta(minutes=self.cfg.cache_ttl_minutes)
        if not force and self.refreshed_at and now - self.refreshed_at < ttl:
            return self.items

        start, end = now - LOOKBACK, now + LOOKAHEAD
        collected: list[Item] = []
        blocks: list[CalBlock] = []
        optional: list[CalBlock] = []
        results: list[FeedResult] = []
        for feed in self.cfg.feeds:
            if not feed.enabled:
                continue
            text, result = load_feed_text(feed, self.cfg.cache_dir, self.cfg.fetch_timeout)
            if text is not None:
                try:
                    if feed.role in ("plan", "events"):
                        # a week back so you can still confirm days you forgot to close out
                        parsed_blocks = parse_blocks(text, feed, self.cfg.timezone, now - PLAN_LOOKBACK, end)
                        result.items = len(parsed_blocks)
                        (blocks if feed.role == "plan" else optional).extend(parsed_blocks)
                    else:
                        parsed = [i for i in parse_items(text, feed, self.cfg.timezone, start, end)
                                  if not self._ignored(i)]
                        result.items = len(parsed)
                        collected.extend(parsed)
                except Exception as exc:  # noqa: BLE001 — a malformed feed shouldn't kill the others
                    log.exception("could not parse feed %s", feed.name)
                    result.ok, result.error = False, f"parse error: {type(exc).__name__}"
            results.append(result)

        self.items = merge(collected)
        self.blocks = sorted({b.uid: b for b in blocks}.values(), key=lambda b: b.start)
        self.optional = sorted({b.uid: b for b in optional}.values(), key=lambda b: b.start)
        self.feed_results = results
        self.refreshed_at = now
        return self.items

    def upcoming(self, hours: float | None = None, days: float | None = None,
                 course: str | None = None, now: datetime | None = None) -> list[Item]:
        now = now or self.now()
        span = timedelta(hours=hours) if hours is not None else timedelta(days=days or 7)
        until = now + span
        out = [i for i in self.items if now < i.due <= until]
        if course:
            needle = course.casefold().replace(" ", "")
            out = [i for i in out if i.course and needle in i.course.casefold().replace(" ", "")]
        return out

    @property
    def committed_blocks(self) -> list[CalBlock]:
        """Your plan calendar + the optional (club) events you said you're going to."""
        return sorted(self.blocks + [b for b in self.optional if b.ref in self.going], key=lambda b: b.start)

    def blocks_between(self, start: datetime, end: datetime) -> list[CalBlock]:
        return [b for b in self.committed_blocks if b.end > start and b.start < end]

    def optional_between(self, start: datetime, end: datetime) -> list[CalBlock]:
        return [b for b in self.optional if b.end > start and b.start < end]
