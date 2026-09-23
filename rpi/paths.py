"""Canonical filesystem layout, resolved relative to the project root.

Kept in one place so no module has to guess where things live, and so the
layout can be changed without touching call sites.

    inbox/   JSONL dropped by the fetcher (synced in from the Linux box)
    state/   fetcher bookkeeping - host-local, never synced
    db/      SQLite system of record
    data/    exported artefacts for the UI
    ui/      static front end
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

INBOX_DIR: Path = PROJECT_ROOT / "inbox"
STATE_DIR: Path = PROJECT_ROOT / "state"
DB_DIR: Path = PROJECT_ROOT / "db"
DB_PATH: Path = DB_DIR / "rpi.sqlite3"
DATA_DIR: Path = PROJECT_ROOT / "data"
UI_DIR: Path = PROJECT_ROOT / "ui"

EXPORT_PATH: Path = DATA_DIR / "rpi.json"
