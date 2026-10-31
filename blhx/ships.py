from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import quote

from .constants import WIKI_BASE_URL
from .db import get_connection
from .history import record_operation
from .state import SYNC_STATE
from .tech import tech_attribute_matches
from .utils import utc_now_text


def ship_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "ship_type": row["ship_type"],
        "rarity": row["rarity"],
        "release_date": row["release_date"],
        "needs_training": bool(row["needs_training"]),
        "is_idling": None if row["is_idling"] is None else bool(row["is_idling"]),
        "is_trained": None if row["is_trained"] is None else bool(row["is_trained"]),
        "wiki_url": row["wiki_url"],
        "tech_points": [],
    }


def status_text(value: bool | None) -> str:
    if value is None:
        return ""
    return "是" if value else "否"


def insert_ship(
    connection: sqlite3.Connection,
    name: str,
    release_date: str,
    parsed_ship: dict[str, Any],
    overwrite: bool = False,
) -> sqlite3.Row:
    current_time = utc_now_text()
    wiki_url = f"{WIKI_BASE_URL}/blhx/{quote(name, safe='')}"
    needs_training = 1 if parsed_ship["tech_points"] else 0
    conflict_action = (
        """
        UPDATE SET
          ship_type = excluded.ship_type,
          rarity = excluded.rarity,
          release_date = excluded.release_date,
          needs_training = excluded.needs_training,
          wiki_url = excluded.wiki_url,
          updated_at = excluded.updated_at
        """
        if overwrite
        else "NOTHING"
    )
    connection.execute(
        f"""
        INSERT INTO ships (
            name, ship_type, rarity, release_date, needs_training,
            is_idling, is_trained, wiki_url, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
        ON CONFLICT(name) DO {conflict_action}
        """,
        (
            name,
            parsed_ship.get("ship_type", ""),
            parsed_ship.get("rarity", ""),
            release_date,
            needs_training,
            wiki_url,
            current_time,
            current_time,
        ),
    )
    ship = connection.execute(
        "SELECT * FROM ships WHERE name = ?", (name,)
    ).fetchone()
    if overwrite:
        connection.execute("DELETE FROM tech_points WHERE ship_id = ?", (ship["id"],))
    for tech_point in parsed_ship["tech_points"]:
        connection.execute(
            """
            INSERT OR IGNORE INTO tech_points (ship_id, stage, attribute, points)
            VALUES (?, ?, ?, ?)
            """,
            (
                ship["id"],
                tech_point["stage"],
                tech_point["attribute"],
                tech_point["points"],
            ),
        )
    return ship


def delete_ship(ship_id: int) -> bool:
    if ship_id <= 0:
        raise ValueError("舰娘 ID 无效")
    with get_connection() as connection:
        ship = connection.execute(
            "SELECT id, name, ship_type FROM ships WHERE id = ?", (ship_id,)
        ).fetchone()
        if ship is None:
            return False
        cursor = connection.execute(
            "DELETE FROM ships WHERE id = ?", (ship_id,)
        )
        record_operation(
            connection,
            "delete_ship",
            f"删除舰娘「{ship['name']}」",
            ship_id=ship["id"],
            ship_name=ship["name"],
            ship_type=ship["ship_type"],
        )
        return cursor.rowcount > 0


def clear_ships() -> int:
    if SYNC_STATE["running"]:
        raise ValueError("同步正在进行中，请稍后再清空数据")
    with get_connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM ships").fetchone()[0]
        cursor = connection.execute("DELETE FROM ships")
        record_operation(connection, "clear_ships", f"清空全部舰娘（共 {count} 个）")
        return cursor.rowcount


def list_ships(filters: dict[str, Any]) -> list[dict[str, Any]]:
    conditions: list[str] = []
    parameters: list[Any] = []
    ship_type_filters = (
        filters.get("ship_types")
        or ([filters["ship_type"]] if filters.get("ship_type") else [])
    )
    tech_attribute_filter = filters.get("tech_attribute", "")
    combined_tech_type_filter = bool(ship_type_filters and tech_attribute_filter)

    if filters.get("name"):
        conditions.append("s.name LIKE ?")
        parameters.append(f"%{filters['name']}%")
    if not combined_tech_type_filter:
        if filters.get("ship_types"):
            ship_types = filters["ship_types"]
            conditions.append(
                f"s.ship_type IN ({','.join('?' for _ in ship_types)})"
            )
            parameters.extend(ship_types)
        elif filters.get("ship_type"):
            conditions.append("s.ship_type = ?")
            parameters.append(filters["ship_type"])
    if filters.get("rarity"):
        conditions.append("s.rarity = ?")
        parameters.append(filters["rarity"])
    if filters.get("needs_training") in {"是", "否"}:
        conditions.append("s.needs_training = ?")
        parameters.append(1 if filters["needs_training"] == "是" else 0)
    if filters.get("is_idling") in {"是", "否"}:
        if filters["is_idling"] == "是":
            conditions.append("s.is_idling = 1")
        else:
            conditions.append("COALESCE(s.is_idling, 0) = 0")
    if filters.get("is_trained") in {"是", "否"}:
        if filters["is_trained"] == "是":
            conditions.append("s.is_trained = 1")
        else:
            conditions.append("COALESCE(s.is_trained, 0) = 0")
    if tech_attribute_filter:
        keyword = f"%{tech_attribute_filter}%"
        conditions.append(
            """
            EXISTS (
                SELECT 1 FROM tech_points tp
                WHERE tp.ship_id = s.id
                  AND tp.stage IN ('解锁图鉴', '达到120级')
                  AND tp.attribute LIKE ?
            )
            """
        )
        parameters.append(keyword)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT s.* FROM ships s {where_clause}
            ORDER BY CASE WHEN s.release_date = '' THEN 1 ELSE 0 END,
                     s.release_date, s.name COLLATE NOCASE
            """,
            parameters,
        ).fetchall()
        ships = [ship_row_to_dict(row) for row in rows]
        if ships:
            placeholders = ",".join("?" for _ in ships)
            tech_rows = connection.execute(
                f"""
                SELECT ship_id, stage, attribute, points
                FROM tech_points
                WHERE ship_id IN ({placeholders})
                ORDER BY stage, attribute, points
                """,
                [ship["id"] for ship in ships],
            ).fetchall()
            tech_by_ship: dict[int, list[dict[str, Any]]] = {}
            for tech_row in tech_rows:
                tech_by_ship.setdefault(tech_row["ship_id"], []).append(dict(tech_row))
            for ship in ships:
                ship["tech_points"] = tech_by_ship.get(ship["id"], [])
    if combined_tech_type_filter:
        ships = [
            ship
            for ship in ships
            if any(
                tech_attribute_matches(
                    tech_point["attribute"],
                    ship_type,
                    tech_attribute_filter,
                    ship["ship_type"],
                )
                for ship_type in ship_type_filters
                for tech_point in ship["tech_points"]
            )
        ]
    return ships


def update_ship_status(
    ship_id: int,
    field: str,
    value: bool,
) -> dict[str, Any] | None:
    if field not in {"is_idling", "is_trained"}:
        raise ValueError("field 只支持 is_idling 或 is_trained")
    if not isinstance(value, bool):
        raise ValueError("value 必须是布尔值")
    action_name = "已挂机完成" if field == "is_idling" else "已练级完成"
    action_text = "标记为" if value else "取消标记为"
    with get_connection() as connection:
        current = connection.execute(
            "SELECT is_idling FROM ships WHERE id = ?",
            (ship_id,),
        ).fetchone()
        if current is None:
            return None
        was_idling = current["is_idling"]
        if field == "is_trained" and value:
            connection.execute(
                """
                UPDATE ships
                SET is_trained = 1, is_idling = 1, updated_at = ?
                WHERE id = ?
                """,
                (utc_now_text(), ship_id),
            )
        else:
            connection.execute(
                f"UPDATE ships SET {field} = ?, updated_at = ? WHERE id = ?",
                (1 if value else 0, utc_now_text(), ship_id),
            )
        row = connection.execute(
            "SELECT id, name, ship_type, is_idling, is_trained FROM ships WHERE id = ?",
            (ship_id,),
        ).fetchone()
        if row is not None:
            message = f"{action_text}{action_name}：{row['name']}"
            if field == "is_trained" and value and not was_idling:
                message += "（同时标记为已挂机完成）"
            record_operation(
                connection,
                field,
                message,
                ship_id=row["id"],
                ship_name=row["name"],
                ship_type=row["ship_type"],
            )
    if row is None:
        return None
    return {
        "id": row["id"],
        "is_idling": None if row["is_idling"] is None else bool(row["is_idling"]),
        "is_trained": None if row["is_trained"] is None else bool(row["is_trained"]),
    }
