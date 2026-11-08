from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from ..core.constants import DATA_DIR, DATABASE_PATH, normalize_ship_type
from ..core.utils import clean_template_value


SCHEMA_VERSION = 4


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


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _tech_points_columns(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute("PRAGMA table_info(tech_points)").fetchall()
    }


def _create_tech_points_table(connection: sqlite3.Connection, name: str) -> None:
    connection.execute(
        f"""
        CREATE TABLE {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ship_id INTEGER NOT NULL REFERENCES ships(id) ON DELETE CASCADE,
            stage TEXT NOT NULL,
            attribute TEXT NOT NULL DEFAULT '',
            bonus_ship_type TEXT NOT NULL DEFAULT '',
            bonus_attribute TEXT NOT NULL DEFAULT '',
            points INTEGER NOT NULL DEFAULT 0,
            UNIQUE(ship_id, stage, attribute, bonus_ship_type, bonus_attribute)
        )
        """
    )


def _migrate_tech_points(connection: sqlite3.Connection) -> None:
    """重建 tech_points：新增加成舰种和加成属性两列。

    旧记录会按 ``attribute`` 文本解析后重新写入，避免升级后加成筛选为空。
    """
    from ..domain.tech import parse_tech_attribute

    if not _table_exists(connection, "tech_points"):
        _create_tech_points_table(connection, "tech_points")
        return
    if {"bonus_ship_type", "bonus_attribute"}.issubset(
        _tech_points_columns(connection)
    ):
        return
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        legacy_rows = connection.execute(
            "SELECT ship_id, stage, attribute, points FROM tech_points"
        ).fetchall()
        connection.execute("DROP TABLE tech_points")
        _create_tech_points_table(connection, "tech_points")
        for row in legacy_rows:
            ship_types, attribute = parse_tech_attribute(row["attribute"])
            connection.execute(
                """
                INSERT OR IGNORE INTO tech_points (
                    ship_id, stage, attribute, bonus_ship_type, bonus_attribute, points
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["ship_id"],
                    row["stage"],
                    row["attribute"],
                    "/".join(ship_types),
                    attribute,
                    row["points"],
                ),
            )
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _create_armaments_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
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
        )
        """
    )


def _ships_columns(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute("PRAGMA table_info(ships)").fetchall()
    }


def _migrate_ships_tags(connection: sqlite3.Connection) -> None:
    """为存量库补 ships.tags 列（自定义标签，`/` 分隔）。

    只加列不填值：标签在同步时写入，存量数据由用户执行一次全量同步重刷。
    筛选侧对“小船”另有 SQL 兜底判定，因此重刷前也能正常筛到。
    """
    if "tags" in _ships_columns(connection):
        return
    connection.execute(
        "ALTER TABLE ships ADD COLUMN tags TEXT NOT NULL DEFAULT ''"
    )


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_connection() as connection:
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
            CREATE INDEX IF NOT EXISTS idx_operation_logs_time
                ON operation_logs(created_at DESC);
            """
        )
        _migrate_tech_points(connection)
        # tags 列需在建表之后补，兼容已存在的旧库。
        _migrate_ships_tags(connection)
        _create_armaments_table(connection)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tech_points_ship ON tech_points(ship_id)"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tech_points_bonus
                ON tech_points(bonus_ship_type, bonus_attribute)
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_armaments_group "
            "ON armaments(is_collab, collab_order, release_date)"
        )
        for row in connection.execute(
            "SELECT id, ship_type, rarity FROM ships"
        ).fetchall():
            ship_type = normalize_ship_type(clean_template_value(row["ship_type"]))
            rarity = clean_template_value(row["rarity"])
            if ship_type != row["ship_type"] or rarity != row["rarity"]:
                connection.execute(
                    "UPDATE ships SET ship_type = ?, rarity = ? WHERE id = ?",
                    (ship_type, rarity, row["id"]),
                )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
