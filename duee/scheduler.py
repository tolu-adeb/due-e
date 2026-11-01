"""A tiny timezone-aware scheduler that knows nothing about Discord.

It runs the same in live mode (posts to Discord) and dry-run mode (prints to the terminal),
which is what makes the test mode honest.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

from typing import TYPE_CHECKING

from . import ai, render
from .config import Config
from .radar import Radar
from .status import Status

if TYPE_CHECKING:
    from .app import DueE

log = logging.getLogger(__name__)


class Poster(Protocol):
    async def post(self, text: str) -> None: ...


class StdoutPoster:
    """Test mode: print instead of posting."""

    async def post(self, text: str) -> None:
        bar = "─" * 60
        print(f"\n{bar}\n{text}\n{bar}", flush=True)


@dataclass
class Job:
    name: str  # "morning" | "evening" | "weekly"
    at: time
    weekday: int | None = None  # 0=Mon … 6=Sun; None = every day

    def next_after(self, after: datetime, tz: ZoneInfo) -> datetime:
        after = after.astimezone(tz)
        for offset in range(0, 8):
            day = after.date() + timedelta(days=offset)
            if self.weekday is not None and day.weekday() != self.weekday:
                continue
            candidate = datetime.combine(day, self.at, tzinfo=tz)  # zoneinfo handles DST
            if candidate > after:
                return candidate
        raise RuntimeError("unreachable: no run time found within a week")


def jobs_from_config(cfg: Config) -> list[Job]:
    return [
        Job("morning", cfg.morning),
        Job("evening", cfg.evening),
        Job("weekly", cfg.weekly_time, cfg.weekly_day),
    ]


def build_post(kind: str, radar: Radar, cfg: Config, style: render.Style = "discord",
               due: "DueE | None" = None) -> tuple[str, int]:
    now = radar.now()
    if kind == "weekly":
        items = radar.upcoming(days=7, now=now)
        text, count = render.agenda(radar.items, now, 7, "DUE-E · Week ahead", style), len(items)
    else:
        text, count = render.digest(radar.items, now, kind, cfg.horizon_hours, style)  # type: ignore[arg-type]
    if due is not None:
        text += _tracker_section(kind, due, style)
    return text, count


def _tracker_section(kind: str, due: "DueE", style: render.Style) -> str:
    """Planner/tracker extras: today's plan (morning), day close-out (evening), week review (weekly)."""
    from .analysis import _fmt_h, summary_text

    b = (lambda s: f"**{s}**") if style == "discord" else (lambda s: s.upper())
    now = due.now()
    out: list[str] = []
    if kind == "morning":
        blocks = [x for x in due.planner.blocks(now.date(), now.date()) if x["status"] == "planned"]
        if blocks:
            out += ["", b(f"🗓 Today's plan ({len(blocks)})")]
            for x in blocks:
                out.append(f"• {render.clock(datetime.fromisoformat(x['start']))} · {x['title']}"
                           + (" · habit" if x["kind"] == "habit" else ""))
        day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=now.tzinfo)
        clubs = [c for c in due.radar.optional_between(now, day_start + timedelta(days=1))]
        if clubs:
            out += ["", b(f"🎟 Club events today ({len(clubs)})")]
            for c in clubs:
                going = " · ✅ going" if c.ref in due.radar.going else ""
                out.append(f"• {render.clock(c.start)} · {c.title} ({c.source}){going}")
            if any(c.ref not in due.radar.going for c in clubs):
                out.append(f"Going? Tap it in the web app → {due.cfg.web_url}/#today")
        soon = [e for e in due.planner.events() if e["prep"]["unscheduled_minutes"]]
        for e in soon[:3]:
            out.append(f"⚠ {e['title']}: {_fmt_h(e['prep']['unscheduled_minutes'])} of prep has no slot — open the Planner")
    elif kind == "evening":
        an = due.analyze(1)
        t = an["totals"]
        out += ["", b("📊 Today so far")]
        if an["score"] is None:
            out.append("Nothing tracked yet.")
        else:
            out.append(f"Score {an['score']}/100 · productive {_fmt_h(t['productive'])} / {_fmt_h(t['target'])}"
                       f" · waste {_fmt_h(t['waste'])}")
        if t["classes_planned"]:
            out.append(f"Classes {t['classes_attended']:g}/{t['classes_planned']} attended")
        pending = [i for i in due.tracker.review_items(now.date(), due.radar.committed_blocks) if not i["status"]]
        if pending:
            out.append(f"✅ {len(pending)} planned item(s) to confirm → {due.cfg.web_url}/#today")
    elif kind == "weekly":
        out += ["", b("📈 Your week"), summary_text(due.analyze(7), style),
                f"Full breakdown → {due.cfg.web_url}/#insights"]
    return ("\n" + "\n".join(out)) if out else ""


class Scheduler:
    def __init__(self, cfg: Config, radar: Radar, poster: Poster, status: Status,
                 style: render.Style = "discord",
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 due: "DueE | None" = None):
        self.cfg, self.radar, self.poster, self.status, self.style = cfg, radar, poster, status, style
        self.due = due
        self.jobs = jobs_from_config(cfg)
        self._sleep = sleep
        self._next: dict[str, datetime] = {}

    def plan(self, now: datetime) -> dict[str, datetime]:
        self._next = {j.name: j.next_after(now, self.cfg.timezone) for j in self.jobs}
        self.status.set_next_runs(self._next)
        return self._next

    async def refresh(self, force: bool = True) -> None:
        await asyncio.to_thread(self.radar.refresh, force)
        self.status.record_refresh(self.radar.now(), self.radar.feed_results, len(self.radar.items))

    async def fire(self, kind: str) -> None:
        now = self.radar.now()
        try:
            await self.refresh(force=True)
            if kind == "weekly" and self.due is not None:
                # fresh week: re-place habits (and any prep that slid) before posting the plan
                await self.due.replan_all()
            text, count = build_post(kind, self.radar, self.cfg, self.style, self.due)
            if kind == "weekly" and self.due is not None and ai.available(self.cfg):
                try:
                    review = await asyncio.to_thread(ai.weekly_review, self.cfg, self.due.analyze(7),
                                                     self.due.planner.events(), self.due.planner.habits())
                    text += "\n\n" + ("**🤖 Claude's take**" if self.style == "discord" else "CLAUDE'S TAKE") + "\n" + review
                except Exception as exc:  # noqa: BLE001 — the review is a bonus, never block the post
                    log.warning("AI review failed: %s", exc)
            if count == 0 and not self.cfg.post_when_empty:
                log.info("%s: nothing due, skipping post", kind)
                self.status.record_post(kind, now, ok=True, items=0)
                return
            for part in render.chunk(text):
                await self.poster.post(part)
            self.status.record_post(kind, now, ok=True, items=count)
            log.info("%s: posted %d item(s)", kind, count)
        except Exception as exc:  # noqa: BLE001 — keep the loop alive; VERA sees the failure
            log.exception("%s post failed", kind)
            self.status.record_post(kind, now, ok=False, error=type(exc).__name__)

    async def run_forever(self) -> None:
        now = self.radar.now()
        self.status.started(now)
        self.plan(now)
        await self.refresh(force=True)
        for name, at in self._next.items():
            log.info("next %-7s → %s", name, at.strftime("%a %b %d %H:%M %Z"))

        while True:
            now = self.radar.now()
            due = [name for name, at in self._next.items() if now >= at]
            for name in due:
                job = next(j for j in self.jobs if j.name == name)
                await self.fire(name)
                # Schedule from the slot time, not "now", so a slow post never skips a slot.
                self._next[name] = job.next_after(max(now, self._next[name]), self.cfg.timezone)
            self.status.set_next_runs(self._next)
            self.status.heartbeat(now)
            wait = min((at - self.radar.now()).total_seconds() for at in self._next.values())
            await self._sleep(max(1.0, min(wait, 60.0)))  # wake at least every minute for the heartbeat
