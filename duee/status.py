"""status.json — DUE-E's contract with VERA.

TAL-E (Steward) reads this file to answer "is DUE-E alive, are its feeds healthy, did it post?".
FIX-E (Mechanic) runs `python -m duee check` and uses the exit code:
    0 = ok, 1 = degraded (a feed is failing / serving cache, or a post failed), 2 = down (no heartbeat).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from .feeds import FeedResult

OK, DEGRADED, DOWN = 0, 1, 2
LEVELS = {OK: "ok", DEGRADED: "degraded", DOWN: "down"}
HEARTBEAT_STALE = timedelta(minutes=10)


class Status:
    def __init__(self, path: Path, mode: str = "live"):
        self.path = path
        self.data: dict = {
            "app": "due-e",
            "version": __version__,
            "mode": mode,
            "pid": os.getpid(),
            "started_at": None,
            "heartbeat_at": None,
            "last_refresh_at": None,
            "item_count": 0,
            "feeds": {},
            "last_posts": {},
            "next_runs": {},
        }

    @staticmethod
    def _ts(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt else None

    def started(self, at: datetime) -> None:
        self.data["started_at"] = self._ts(at)
        self.heartbeat(at)

    def heartbeat(self, at: datetime) -> None:
        self.data["heartbeat_at"] = self._ts(at)
        self.write()

    def record_refresh(self, at: datetime, results: list[FeedResult], item_count: int) -> None:
        self.data["last_refresh_at"] = self._ts(at)
        self.data["item_count"] = item_count
        feeds = self.data["feeds"]
        for r in results:
            prev = feeds.get(r.name, {})
            feeds[r.name] = {
                "ok": r.ok,
                "items": r.items,
                "used_cache": r.used_cache,
                "error": r.error,
                "last_ok_at": self._ts(at) if r.ok else prev.get("last_ok_at"),
                "consecutive_failures": 0 if r.ok else prev.get("consecutive_failures", 0) + 1,
            }
        self.write()

    def record_post(self, kind: str, at: datetime, ok: bool, items: int = 0, error: str | None = None) -> None:
        self.data["last_posts"][kind] = {"at": self._ts(at), "ok": ok, "items": items, "error": error}
        self.write()

    def set_next_runs(self, runs: dict[str, datetime]) -> None:
        self.data["next_runs"] = {k: self._ts(v) for k, v in runs.items()}

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".status-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)  # atomic, so VERA never reads a half-written file


def read_status(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def evaluate(status: dict | None, now: datetime, feed_results: list[FeedResult] | None = None,
             check_bot: bool = True) -> tuple[int, list[str]]:
    """Return (level, reasons). Combines a live feed check with the running bot's status file."""
    level, reasons = OK, []

    def bump(lvl: int, why: str):
        nonlocal level
        level = max(level, lvl)
        reasons.append(why)

    for r in feed_results or []:
        if not r.ok:
            bump(DEGRADED, f"feed '{r.name}' failing ({r.error}){' — serving cached copy' if r.used_cache else ''}")
        elif r.items == 0:
            reasons.append(f"feed '{r.name}' ok but has 0 upcoming items (check the link?)")

    if not check_bot:
        return level, reasons
    if status is None:
        bump(DOWN, "no status.json — the bot has never run here (or data_dir is wrong)")
        return level, reasons

    hb = status.get("heartbeat_at")
    if not hb:
        bump(DOWN, "status.json has no heartbeat")
    else:
        age = now - datetime.fromisoformat(hb)
        if age > HEARTBEAT_STALE:
            bump(DOWN, f"last heartbeat {int(age.total_seconds() // 60)} min ago — bot is probably not running")

    for kind, post in (status.get("last_posts") or {}).items():
        if not post.get("ok"):
            bump(DEGRADED, f"last {kind} post failed: {post.get('error')}")

    return level, reasons
