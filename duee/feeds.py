"""Fetch .ics feeds and turn their events/to-dos into deadline Items."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
import requests

from . import __version__
from .config import Feed

log = logging.getLogger(__name__)

USER_AGENT = f"DUE-E/{__version__} (deadline radar)"

# Canvas puts the course in brackets at the end: "Project 2 [EECS 281 001 FA 2026]"
_BRACKET_COURSE = re.compile(r"^(?P<title>.*?)\s*\[(?P<course>[^\[\]]+)\]\s*$")
# Shorten "EECS 281 001 FA 2026" -> "EECS 281"
_COURSE_CODE = re.compile(r"\b([A-Z]{2,10})\s*-?\s*(\d{3}[A-Z]?)\b")


@dataclass(frozen=True)
class Item:
    uid: str
    title: str
    course: str | None
    due: datetime  # timezone-aware, in the configured timezone
    all_day: bool
    url: str | None
    source: str

    @property
    def dedup_key(self) -> tuple[str, datetime]:
        return (re.sub(r"\s+", " ", self.title.casefold()).strip(), self.due)


@dataclass(frozen=True)
class CalBlock:
    """A time block from a 'plan' calendar (class, study block, gym…)."""
    uid: str
    title: str
    start: datetime
    end: datetime
    source: str

    @property
    def ref(self) -> str:
        return f"cal:{self.uid}"

    @property
    def minutes(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() // 60))


@dataclass
class FeedResult:
    name: str
    ok: bool
    items: int = 0
    used_cache: bool = False
    error: str | None = None


def _safe_error(exc: Exception) -> str:
    """Describe an error WITHOUT leaking the feed URL (Canvas feed URLs are secret tokens)."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.ConnectionError):
        return "connection error"
    if isinstance(exc, FileNotFoundError):
        return "file not found"
    if isinstance(exc, ValueError):
        return f"invalid feed: {exc}"
    return type(exc).__name__


def _cache_path(feed: Feed, cache_dir: Path) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", feed.name.lower()).strip("-") or "feed"
    digest = hashlib.sha256(feed.url.encode()).hexdigest()[:10]
    return cache_dir / f"{slug}-{digest}.ics"


def _read_source(url: str, timeout: float) -> str:
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if url.startswith(("http://", "https://")):
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        text = resp.content.decode("utf-8", errors="replace")
    else:
        text = Path(url.removeprefix("file://")).expanduser().read_text(encoding="utf-8")
    if "BEGIN:VCALENDAR" not in text:
        raise ValueError("response is not an iCalendar file")
    return text


def load_feed_text(feed: Feed, cache_dir: Path, timeout: float = 15.0) -> tuple[str | None, FeedResult]:
    """Return (ics_text, result). Falls back to the last good copy if the live fetch fails."""
    cache = _cache_path(feed, cache_dir)
    try:
        text = _read_source(feed.url, timeout)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(text, encoding="utf-8")
        except OSError as e:  # cache is best-effort
            log.warning("could not write cache for %s: %s", feed.name, e)
        return text, FeedResult(feed.name, ok=True)
    except Exception as exc:  # noqa: BLE001 — any failure should degrade, not crash
        err = _safe_error(exc)
        log.warning("feed %s failed: %s", feed.name, err)
        if cache.exists():
            return cache.read_text(encoding="utf-8"), FeedResult(feed.name, ok=False, used_cache=True, error=err)
        return None, FeedResult(feed.name, ok=False, error=err)


def split_course(summary: str) -> tuple[str, str | None]:
    """'Project 2 [EECS 281 001 FA 2026]' -> ('Project 2', 'EECS 281')."""
    m = _BRACKET_COURSE.match(summary)
    if not m:
        return summary.strip(), None
    title, course = m.group("title").strip(), m.group("course").strip()
    code = _COURSE_CODE.search(course.upper())
    return (title or summary.strip()), (f"{code.group(1)} {code.group(2)}" if code else course)


def _due_of(comp, tz: ZoneInfo) -> tuple[datetime, bool] | None:
    prop = comp.get("DUE") if comp.name == "VTODO" else None
    if prop is None:
        prop = comp.get("DTSTART")
    if prop is None:
        return None
    value = prop.dt
    if isinstance(value, datetime):
        if value.tzinfo is None:  # "floating" time -> treat as local
            value = value.replace(tzinfo=tz)
        return value.astimezone(tz), False
    if isinstance(value, date):  # all-day: due by end of that day
        return datetime.combine(value, time(23, 59), tzinfo=tz), True
    return None


def parse_blocks(text: str, feed: Feed, tz: ZoneInfo, start: datetime, end: datetime) -> list[CalBlock]:
    """Parse timed events from a plan calendar into blocks overlapping [start, end]. All-day events are skipped."""
    cal = icalendar.Calendar.from_ical(text)
    out: list[CalBlock] = []
    for comp in recurring_ical_events.of(cal).between(start, end):
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        s, e = comp.get("DTSTART"), comp.get("DTEND")
        if s is None or not isinstance(s.dt, datetime):
            continue
        s_dt = s.dt if s.dt.tzinfo else s.dt.replace(tzinfo=tz)
        if e is not None and isinstance(e.dt, datetime):
            e_dt = e.dt if e.dt.tzinfo else e.dt.replace(tzinfo=tz)
        elif comp.get("DURATION") is not None:
            e_dt = s_dt + comp.get("DURATION").dt
        else:
            continue  # zero-length events aren't time blocks
        s_dt, e_dt = s_dt.astimezone(tz), e_dt.astimezone(tz)
        if e_dt <= s_dt:
            continue
        uid = str(comp.get("UID", f"{feed.name}:{comp.get('SUMMARY')}"))
        out.append(CalBlock(uid=f"{uid}@{s_dt.isoformat()}", title=str(comp.get("SUMMARY", "(untitled)")),
                            start=s_dt, end=e_dt, source=feed.name))
    return sorted(out, key=lambda b: b.start)


def parse_items(text: str, feed: Feed, tz: ZoneInfo, start: datetime, end: datetime) -> list[Item]:
    """Parse an ICS string into Items whose due time falls in [start, end]."""
    cal = icalendar.Calendar.from_ical(text)
    comps = recurring_ical_events.of(cal, components=["VEVENT", "VTODO"]).between(start, end)

    items: list[Item] = []
    for comp in comps:
        if str(comp.get("STATUS", "")).upper() in {"CANCELLED", "COMPLETED"}:
            continue
        due = _due_of(comp, tz)
        if due is None:
            continue
        due_at, all_day = due
        if not (start <= due_at <= end):
            continue
        title, course = split_course(str(comp.get("SUMMARY", "(untitled)")))
        url = comp.get("URL")
        uid = str(comp.get("UID", f"{feed.name}:{title}"))
        items.append(
            Item(
                uid=f"{uid}@{due_at.isoformat()}",
                title=title,
                course=feed.course or course,
                due=due_at,
                all_day=all_day,
                url=str(url) if url else None,
                source=feed.name,
            )
        )
    return items
