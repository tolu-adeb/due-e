"""Turn Items into Discord posts (or plain terminal text for test mode)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from itertools import groupby
from typing import Literal

from .feeds import Item

Style = Literal["discord", "plain"]
Kind = Literal["morning", "evening"]

DISCORD_LIMIT = 2000
_MD_SPECIAL = re.compile(r"([\\*_~`|>\[\]])")


def _esc(text: str, style: Style) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text) if style == "discord" else text


def clock(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def day_label(d: date, today: date) -> str:
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return f"{d:%a %b} {d.day}"


def relative(due: datetime, now: datetime) -> str:
    secs = int((due - now).total_seconds())
    if secs <= 0:
        return "now"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"in {d}d {h}h" if h else f"in {d}d"
    if h:
        return f"in {h}h {m}m" if m else f"in {h}h"
    return f"in {max(m, 1)}m"


def line(item: Item, now: datetime, style: Style, show_day: bool = False) -> str:
    title = _esc(item.title, style)
    if style == "discord" and item.url:
        title = f"[{title}](<{item.url}>)"  # <...> suppresses link previews
    when = "all day" if item.all_day else clock(item.due)
    if show_day:
        when = f"{day_label(item.due.date(), now.date())} {when}"
    rel = f"<t:{int(item.due.timestamp())}:R>" if style == "discord" else relative(item.due, now)
    course = f"{_esc(item.course, style)} · " if item.course else ""
    if style == "discord":
        return f"• **{title}** · {course}{when} · {rel}"
    return f"  - {title} · {course}{when} · {rel}"


def _bold(text: str, style: Style) -> str:
    return f"**{text}**" if style == "discord" else text.upper()


def _section(name: str, items: list[Item], now: datetime, style: Style, show_day=False) -> list[str]:
    if not items:
        return []
    return ["", _bold(f"{name} ({len(items)})", style), *(line(i, now, style, show_day) for i in items)]


def digest(items: list[Item], now: datetime, kind: Kind, horizon_hours: int = 48,
           style: Style = "discord") -> tuple[str, int]:
    """Morning/evening sweep. Returns (text, number_of_items) so callers can skip empty posts."""
    until = now + timedelta(hours=horizon_hours)
    window = [i for i in items if now < i.due <= until]
    today, tomorrow = now.date(), now.date() + timedelta(days=1)

    due_today = [i for i in window if i.due.date() == today]
    due_tomorrow = [i for i in window if i.due.date() == tomorrow]
    later = [i for i in window if i.due.date() > tomorrow]

    icon, label = ("☀️", "Morning sweep") if kind == "morning" else ("🌙", "Evening sweep")
    header = f"{icon} {_bold(f'DUE-E · {label}', style)} — {now:%a %b} {now.day}"
    if not window:
        return f"{header}\nRadar clear — nothing due in the next {horizon_hours}h. ✅", 0

    out = [header]
    out += _section("🔴 Due tonight" if kind == "evening" else "🔴 Due today", due_today, now, style)
    out += _section("🟠 Due tomorrow", due_tomorrow, now, style)
    out += _section(f"🟡 Later in the next {horizon_hours}h", later, now, style, show_day=True)
    return "\n".join(out), len(window)


def agenda(items: list[Item], now: datetime, days: int = 7, title: str = "Due soon",
           style: Style = "discord", course: str | None = None) -> str:
    """Grouped-by-day list (used for /due and the weekly summary)."""
    until = now + timedelta(days=days)
    window = [i for i in items if now < i.due <= until]
    scope = f" · {course}" if course else ""
    header = f"📡 {_bold(title, style)} — next {days} day{'s' if days != 1 else ''}{scope}"
    if not window:
        return f"{header}\nNothing on the radar. ✅"

    courses = {i.course for i in window if i.course}
    out = [header, f"{len(window)} item{'s' if len(window) != 1 else ''}"
                   + (f" across {len(courses)} course{'s' if len(courses) != 1 else ''}" if courses else "")]
    for d, group in groupby(window, key=lambda i: i.due.date()):
        label = day_label(d, now.date())
        if label in ("Today", "Tomorrow"):
            label += f" · {d:%a %b} {d.day}"
        out += ["", _bold(label, style)]
        out += [line(i, now, style) for i in group]
    return "\n".join(out)


def chunk(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line boundaries so each message fits Discord's 2000-char limit."""
    chunks, cur = [], ""
    for ln in text.split("\n"):
        while len(ln) > limit:  # pathological single line
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(ln[:limit])
            ln = ln[limit:]
        candidate = f"{cur}\n{ln}" if cur else ln
        if len(candidate) > limit:
            chunks.append(cur)
            cur = ln
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks
