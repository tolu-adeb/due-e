"""Config loading: config.yaml + environment (.env) for secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# kind → how the analyzer treats time in that category
KINDS = ("productive", "health", "maintenance", "leisure", "waste")


@dataclass
class Feed:
    name: str
    url: str
    course: str | None = None  # force a course label for every item in this feed
    enabled: bool = True
    # "deadlines": point-in-time due dates (Canvas). "plan": your time-blocking calendar
    # (classes, study blocks, gym…) — used as busy time and confirmed at the end of the day.
    # "events": optional events (club calendars) — shown to you, but only count as busy /
    # get confirmed once you mark yourself as going.
    role: str = "deadlines"


@dataclass
class Category:
    name: str
    kind: str
    match: list[str] = field(default_factory=list)


DEFAULT_CATEGORIES = [
    Category("class", "productive", ["lecture", "class", "discussion", "lab section", "recitation"]),
    Category("study", "productive", ["study", "homework", "hw", "problem set", "pset", "review", "prep",
                                     "practice exam", "reading", "eecs", "phys", "math", "lab report"]),
    Category("project", "productive", ["project", "build", "code", "coding", "cervo", "vera", "due-e", "abg", "arduino"]),
    Category("work", "productive", ["work", "handshake", "masa", "meeting", "shift", "youphoria"]),
    Category("exercise", "health", ["gym", "run", "lift", "workout", "walk", "basketball", "swim"]),
    Category("sleep", "health", ["sleep", "nap"]),
    Category("meals", "maintenance", ["breakfast", "lunch", "dinner", "eat", "cook"]),
    Category("admin", "maintenance", ["email", "errand", "laundry", "commute", "clean", "shower", "groceries"]),
    Category("social", "leisure", ["friends", "hang", "party", "call", "date"]),
    Category("leisure", "leisure", ["game", "gaming", "movie", "show", "netflix", "music", "read for fun"]),
    Category("scrolling", "waste", ["tiktok", "instagram", "insta", "youtube", "reddit", "twitter", "scroll", "x.com"]),
]


@dataclass
class TrackerConfig:
    categories: list[Category] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    daily_target_hours: float = 6.0       # productive hours you're aiming for on a weekday
    weekend_target_hours: float = 3.0
    waste_budget_minutes: int = 60        # wasted time per day you're OK with
    day_start: time = time(8, 0)          # your normal waking window (used for gaps + scheduling)
    day_end: time = time(23, 30)

    def category(self, name: str) -> Category | None:
        return next((c for c in self.categories if c.name == name), None)

    def kind_of(self, name: str) -> str:
        c = self.category(name)
        return c.kind if c else "maintenance"


@dataclass
class PlannerConfig:
    focus_windows: list[tuple[time, time]] = field(
        default_factory=lambda: [(time(10), time(12)), (time(14), time(17)), (time(19), time(22))])
    block_minutes: int = 60
    min_block_minutes: int = 30
    buffer_minutes: int = 15
    max_planned_minutes_per_day: int = 240   # cap on DUE-E-scheduled prep per day
    habit_windows: dict[str, tuple[time, time]] = field(default_factory=lambda: {
        "morning": (time(7), time(12)), "afternoon": (time(12), time(17)),
        "evening": (time(17), time(23)), "any": (time(8), time(23)),
    })


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8787
    token: str | None = None   # required if you bind to anything other than localhost
    public_url: str | None = None  # how links in Discord should point at the web app


@dataclass
class AiConfig:
    enabled: bool = False
    model: str = "claude-haiku-5-5"
    max_tokens: int = 900


@dataclass
class Config:
    timezone: ZoneInfo
    feeds: list[Feed]
    channel_id: int | None = None
    guild_id: int | None = None
    morning: time = time(8, 0)
    evening: time = time(20, 0)
    weekly_day: int = 6  # Sunday
    weekly_time: time = time(19, 0)
    horizon_hours: int = 48
    post_when_empty: bool = True
    ignore_patterns: list[str] = field(default_factory=list)
    data_dir: Path = Path(".duee")
    fetch_timeout: float = 15.0
    cache_ttl_minutes: int = 10
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    planner: PlannerConfig = field(default_factory=PlannerConfig)
    web: WebConfig = field(default_factory=WebConfig)
    ai: AiConfig = field(default_factory=AiConfig)

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def status_file(self) -> Path:
        return self.data_dir / "status.json"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "duee.sqlite3"

    @property
    def web_url(self) -> str:
        return self.web.public_url or f"http://{self.web.host}:{self.web.port}"


def load_dotenv(path: str | Path = ".env") -> None:
    """Tiny .env loader (KEY=VALUE lines). Real environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _parse_time(value: str | None, default: time) -> time:
    if value is None:
        return default
    hh, mm = str(value).split(":")
    return time(int(hh) % 24, int(mm))


def _parse_window(value: str) -> tuple[time, time]:
    a, b = str(value).split("-")
    return _parse_time(a.strip(), time(0)), _parse_time(b.strip(), time(0))


def _expand(value: str) -> str:
    out = os.path.expandvars(value)
    if "${" in out or out.startswith("$"):
        raise ValueError(f"Unresolved environment variable in config value: {value!r}")
    return out


def _optional(value) -> str | None:
    """Expand ${VARS}; an unset variable means 'not configured'."""
    if value in (None, ""):
        return None
    out = os.path.expandvars(str(value)).strip()
    return None if (not out or "$" in out) else out


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path)
    raw = yaml.safe_load(path.read_text()) or {}
    base = path.parent

    tz = ZoneInfo(raw.get("timezone", "America/Detroit"))
    discord = raw.get("discord", {}) or {}
    sched = raw.get("schedule", {}) or {}
    weekly = sched.get("weekly", {}) or {}
    filters = raw.get("filters", {}) or {}

    feeds = []
    for f in raw.get("feeds", []) or []:
        role = f.get("role", "deadlines")
        if role not in ("deadlines", "plan", "events"):
            raise ValueError(f"feed {f.get('name')!r}: role must be 'deadlines', 'plan' or 'events'")
        feeds.append(Feed(name=f["name"], url=_expand(str(f["url"])), course=f.get("course"),
                          enabled=f.get("enabled", True), role=role))
    if not feeds:
        raise ValueError("config has no feeds — add at least one .ics link under `feeds:`")

    def _int_or_none(v):
        s = _optional(v)
        return int(s) if s else None

    data_dir = Path(raw.get("data_dir", ".duee"))
    if not data_dir.is_absolute():
        data_dir = base / data_dir

    # ── tracker ──
    t = raw.get("tracker", {}) or {}
    tracker = TrackerConfig()
    if t.get("categories"):
        cats = []
        for name, spec in t["categories"].items():
            spec = spec or {}
            kind = spec.get("kind", "maintenance")
            if kind not in KINDS:
                raise ValueError(f"category {name!r}: kind must be one of {', '.join(KINDS)}")
            cats.append(Category(name, kind, [str(m).lower() for m in spec.get("match", [])]))
        tracker.categories = cats
    tracker.daily_target_hours = float(t.get("daily_target_hours", tracker.daily_target_hours))
    tracker.weekend_target_hours = float(t.get("weekend_target_hours", tracker.weekend_target_hours))
    tracker.waste_budget_minutes = int(t.get("waste_budget_minutes", tracker.waste_budget_minutes))
    tracker.day_start = _parse_time(t.get("day_start"), tracker.day_start)
    tracker.day_end = _parse_time(t.get("day_end"), tracker.day_end)

    # ── planner ──
    p = raw.get("planner", {}) or {}
    planner = PlannerConfig()
    if p.get("focus_windows"):
        planner.focus_windows = [_parse_window(w) for w in p["focus_windows"]]
    for key in ("block_minutes", "min_block_minutes", "buffer_minutes", "max_planned_minutes_per_day"):
        if key in p:
            setattr(planner, key, int(p[key]))
    for name, w in (p.get("habit_windows") or {}).items():
        planner.habit_windows[name] = _parse_window(w)

    # ── web ──
    w = raw.get("web", {}) or {}
    web = WebConfig(
        enabled=bool(w.get("enabled", True)),
        host=str(w.get("host", "127.0.0.1")),
        port=int(w.get("port", 8787)),
        token=_optional(w.get("token")),
        public_url=_optional(w.get("public_url")),
    )

    # ── ai ──
    a = raw.get("ai", {}) or {}
    ai = AiConfig(enabled=bool(a.get("enabled", False)), model=str(a.get("model", AiConfig.model)),
                  max_tokens=int(a.get("max_tokens", AiConfig.max_tokens)))

    return Config(
        timezone=tz,
        feeds=feeds,
        channel_id=_int_or_none(discord.get("channel_id")),
        guild_id=_int_or_none(discord.get("guild_id")),
        morning=_parse_time(sched.get("morning"), time(8, 0)),
        evening=_parse_time(sched.get("evening"), time(20, 0)),
        weekly_day=DAYS[str(weekly.get("day", "sun")).lower()[:3]],
        weekly_time=_parse_time(weekly.get("time"), time(19, 0)),
        horizon_hours=int(raw.get("horizon_hours", 48)),
        post_when_empty=bool(raw.get("post_when_empty", True)),
        ignore_patterns=list(filters.get("ignore", []) or []),
        data_dir=data_dir,
        fetch_timeout=float(raw.get("fetch_timeout", 15)),
        cache_ttl_minutes=int(raw.get("cache_ttl_minutes", 10)),
        tracker=tracker,
        planner=planner,
        web=web,
        ai=ai,
    )
