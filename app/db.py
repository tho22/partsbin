"""SQLite storage layer."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("PARTSBIN_DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "partsbin.db"
IMAGE_DIR = DATA_DIR / "images"

CATEGORIES = [
    "resistor",
    "capacitor",
    "inductor",
    "diode",
    "led",
    "transistor",
    "ic",
    "microcontroller",
    "dev-board",
    "single-board-computer",
    "sensor",
    "module",
    "display",
    "connector",
    "switch",
    "relay",
    "power",
    "crystal",
    "cable",
    "mechanical",
    "tool",
    "other",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS parts (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    category      TEXT NOT NULL DEFAULT 'other',
    value         TEXT NOT NULL DEFAULT '',
    package       TEXT NOT NULL DEFAULT '',
    manufacturer  TEXT NOT NULL DEFAULT '',
    mpn           TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    quantity      INTEGER NOT NULL DEFAULT 0,
    min_quantity  INTEGER NOT NULL DEFAULT 0,
    location_id   INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    datasheet_url TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT '',
    image         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_parts_category ON parts(category);
CREATE INDEX IF NOT EXISTS idx_parts_location ON parts(location_id);

CREATE TABLE IF NOT EXISTS part_links (
    id      INTEGER PRIMARY KEY,
    part_id INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
    label   TEXT NOT NULL DEFAULT '',
    url     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'planned',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Parts reserved for (or already consumed by) a project.
CREATE TABLE IF NOT EXISTS project_parts (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    part_id    INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
    quantity   INTEGER NOT NULL DEFAULT 1,
    consumed   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (project_id, part_id)
);

-- Stock movements for traceability.
CREATE TABLE IF NOT EXISTS stock_log (
    id         INTEGER PRIMARY KEY,
    part_id    INTEGER NOT NULL REFERENCES parts(id) ON DELETE CASCADE,
    delta      INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]


def row(cursor) -> dict | None:
    r = cursor.fetchone()
    return dict(r) if r else None
