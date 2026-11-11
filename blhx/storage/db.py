from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from ..core.constants import DATA_DIR, DATABASE_PATH


@contextmanager
def get_connection():
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_connection() as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                ship_type TEXT NOT NULL DEFAULT '',
                rarity TEXT NOT NULL DEFAULT '',
                release_date TEXT NOT NULL DEFAULT '',
                needs_training INTEGER NOT NULL DEFAULT 0,
                is_idling INTEGER,
                is_trained INTEGER,
                wiki_url TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tech_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ship_id INTEGER NOT NULL REFERENCES ships(id) ON DELETE CASCADE,
                stage TEXT NOT NULL,
                attribute TEXT NOT NULL DEFAULT '',
                bonus_ship_type TEXT NOT NULL DEFAULT '',
                bonus_attribute TEXT NOT NULL DEFAULT '',
                points INTEGER NOT NULL DEFAULT 0,
                UNIQUE(ship_id, stage, attribute, bonus_ship_type, bonus_attribute)
            );

            CREATE TABLE IF NOT EXISTS armaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ship_id INTEGER NOT NULL UNIQUE REFERENCES ships(id) ON DELETE CASCADE,
                release_date TEXT NOT NULL DEFAULT '',
                is_collab INTEGER NOT NULL DEFAULT 0,
                collab_group TEXT NOT NULL DEFAULT '',
                collab_order INTEGER NOT NULL DEFAULT 0,
                is_built INTEGER,
                is_enhanced INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS operation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_type TEXT NOT NULL,
                ship_id INTEGER,
                ship_name TEXT NOT NULL DEFAULT '',
                ship_type TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ships_type_status
                ON ships(ship_type, needs_training, is_idling, is_trained);
            CREATE INDEX IF NOT EXISTS idx_tech_points_ship ON tech_points(ship_id);
            CREATE INDEX IF NOT EXISTS idx_tech_points_bonus
                ON tech_points(bonus_ship_type, bonus_attribute);
            CREATE INDEX IF NOT EXISTS idx_armaments_group
                ON armaments(is_collab, collab_order, release_date);
            CREATE INDEX IF NOT EXISTS idx_operation_logs_time
                ON operation_logs(created_at DESC);
            """
        )
