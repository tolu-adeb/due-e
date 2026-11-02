from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from duee.config import Config, Feed
from duee.radar import Radar

FIXTURES = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("America/Detroit")
NOW = datetime(2026, 11, 2, 8, 0, tzinfo=TZ)  # Monday morning, day after DST ends


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(
        timezone=TZ,
        feeds=[
            Feed("Canvas", str(FIXTURES / "canvas.ics")),
            Feed("281 site", str(FIXTURES / "course_site.ics"), course="EECS 281"),
        ],
        ignore_patterns=["office hours"],
        data_dir=tmp_path / ".duee",
        channel_id=1,
    )
    c.planner.auto_tasks = False  # tests that want Canvas → tasks turn it on explicitly
    return c


@pytest.fixture
def radar(cfg) -> Radar:
    r = Radar(cfg, clock=lambda: NOW)
    r.refresh(force=True)
    return r


class Clock:
    """A clock tests can move."""

    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def due(cfg, clock):
    from duee.app import DueE

    cfg.feeds.append(Feed("Schedule", str(FIXTURES / "plan.ics"), role="plan"))
    d = DueE.create(cfg, clock=clock, db_path=":memory:")
    d.radar.refresh(force=True)
    return d
