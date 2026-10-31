"""SQLite storage for the tracker, planner and habits. One file: .duee/duee.sqlite3."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id          INTEGER PRIMARY KEY,
    label       TEXT NOT NULL,
    category    TEXT NOT NULL,
    start       TEXT NOT NULL,          -- ISO datetime with offset
    end         TEXT,                   -- NULL while a timer is running
    minutes     INTEGER,
    day         TEXT NOT NULL,          -- local YYYY-MM-DD of start
    focus       INTEGER,                -- 1..5, optional self-rating
    source      TEXT NOT NULL,          -- manual | timer | calendar | plan | discord
    ref         TEXT,                   -- confirmation ref when it came from the calendar
    event_id    INTEGER,                -- prep counted toward a planner event
    notes       TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activities_day ON activities(day);

CREATE TABLE IF NOT EXISTS confirmations (
    ref             TEXT PRIMARY KEY,   -- cal:<uid@start> or block:<id>
    day             TEXT NOT NULL,
    title           TEXT NOT NULL,
    start           TEXT NOT NULL,
    end             TEXT NOT NULL,
    planned_minutes INTEGER NOT NULL,
    status          TEXT NOT NULL,      -- done | partial | skipped | cancelled
    actual_minutes  INTEGER,
    category        TEXT,
    activity_id     INTEGER,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conf_day ON confirmations(day);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- exam | project | assignment | presentation | interview | event
    course      TEXT,
    at          TEXT NOT NULL,          -- when it happens / is due
    duration    INTEGER DEFAULT 0,      -- minutes the event itself takes (blocks the calendar)
    difficulty  INTEGER DEFAULT 3,      -- 1..5
    readiness   INTEGER DEFAULT 1,      -- 1 (haven't started) .. 5 (ready)
    prep_hours  REAL,                   -- your own estimate; overrides DUE-E's
    notes       TEXT DEFAULT '',
    done        INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,          -- prep | habit
    ref_id      INTEGER NOT NULL,       -- events.id or habits.id
    title       TEXT NOT NULL,
    category    TEXT NOT NULL,
    start       TEXT NOT NULL,
    end         TEXT NOT NULL,
    day         TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'planned',  -- planned | done | partial | skipped
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_day ON blocks(day);

CREATE TABLE IF NOT EXISTS rsvps (
    ref         TEXT PRIMARY KEY,       -- cal:<uid@start> of an optional (club) event you're going to
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS habits (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    minutes     INTEGER NOT NULL,
    per_week    INTEGER NOT NULL,       -- 7 = daily
    window      TEXT NOT NULL DEFAULT 'any',      -- morning | afternoon | evening | any
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL") if str(path) != ":memory:" else None
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        """Run a write; returns lastrowid (inserts) or rowcount."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount

    def insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        return self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", row)

    def close(self) -> None:
        self._conn.close()
