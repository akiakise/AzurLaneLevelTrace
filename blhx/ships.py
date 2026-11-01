from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import quote

from .constants import (
    WIKI_BASE_URL,
    bonus_attribute_sort_key,
    bonus_ship_type_sort_key,
    normalize_ship_type,
    tech_stage_sort_key,
)
from .config import remove_config
from .db import get_connection
from .history import record_operation
from .state import SYNC_STATE
from .tech import parse_tech_attribute
from .utils import utc_now_text


def ship_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "ship_type": row["ship_type"],
        "rarity": row["rarity"],
        "bonus_ship_types": [],
        "release_date": row["release_date"],
        "needs_training": bool(row["needs_training"]),
        "is_idling": None if row["is_idling"] is None else bool(row["is_idling"]),
        "is_trained": None if row["is_trained"] is None else bool(row["is_trained"]),
        "wiki_url": row["wiki_url"],
        "tech_points": [],
    }


def find_ship_brief(ship_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT id, name, ship_type, is_idling, is_trained
            FROM ships WHERE id = ?
            """,
            (ship_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "ship_type": row["ship_type"],
        "is_idling": None if row["is_idling"] is None else bool(row["is_idling"]),
        "is_trained": None if row["is_trained"] is None else bool(row["is_trained"]),
    }


def find_existing_ship_ids(ship_ids: set[int]) -> set[int]:
    if not ship_ids:
        return set()
    placeholders = ",".join("?" for _ in ship_ids)
    with get_connection() as connection:
        rows = connection.execute(
            f"SELECT id FROM ships WHERE id IN ({placeholders})",
            list(ship_ids),
        ).fetchall()
    return {row["id"] for row in rows}


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
    ship_type = normalize_ship_type(parsed_ship.get("ship_type", ""))
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
            ship_type,
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
        bonus_ship_types, bonus_attribute = parse_tech_attribute(
            tech_point["attribute"]
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO tech_points (
                ship_id, stage, attribute, bonus_ship_type, bonus_attribute, points
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ship["id"],
                tech_point["stage"],
                tech_point["attribute"],
                "/".join(bonus_ship_types),
                bonus_attribute,
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
        deleted = cursor.rowcount > 0
    if deleted:
        release_manual_target(ship_id)
    return deleted


def clear_ships() -> int:
    from .config import config_transaction
    from .state import SYNC_LOCK

    with config_transaction(), SYNC_LOCK:
        if SYNC_STATE["running"]:
            raise ValueError("同步正在进行中，请稍后再清空数据")
        remove_config()
        with get_connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM ships").fetchone()[0]
            connection.execute("DELETE FROM ships")
            connection.execute("DELETE FROM tech_points")
            connection.execute("DELETE FROM operation_logs")
            has_sequence_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
            ).fetchone()
            if has_sequence_table:
                connection.execute("DELETE FROM sqlite_sequence")
        SYNC_STATE.update(
            {
                "running": False,
                "status": "未开始",
                "message": "",
                "total": 0,
                "processed": 0,
                "skipped": 0,
                "errors": [],
            }
        )
        return count


def list_ships(filters: dict[str, Any]) -> list[dict[str, Any]]:
    conditions: list[str] = []
    parameters: list[Any] = []
    ship_type_filters = [
        normalize_ship_type(value.strip())
        for value in (
            filters.get("ship_types")
            or ([filters["ship_type"]] if filters.get("ship_type") else [])
        )
        if isinstance(value, str) and value.strip()
    ]
    bonus_ship_type_filter = normalize_ship_type(
        str(filters.get("bonus_ship_type") or "")
    )
    bonus_attribute_filter = str(filters.get("bonus_attribute") or "").strip()

    ship_id_filters = [
        int(value)
        for value in (filters.get("ship_ids") or [])
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if filters.get("name"):
        conditions.append("s.name LIKE ?")
        parameters.append(f"%{filters['name']}%")
    if ship_id_filters:
        conditions.append(
            f"s.id IN ({','.join('?' for _ in ship_id_filters)})"
        )
        parameters.extend(ship_id_filters)
    if ship_type_filters:
        conditions.append(
            f"s.ship_type IN ({','.join('?' for _ in ship_type_filters)})"
        )
        parameters.extend(ship_type_filters)
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
    if bonus_ship_type_filter or bonus_attribute_filter:
        tech_conditions = [
            "tp.ship_id = s.id",
            "tp.stage IN ('解锁图鉴', '达到120级')",
        ]
        tech_parameters: list[Any] = []
        if bonus_ship_type_filter:
            tech_conditions.append(
                "instr('/' || tp.bonus_ship_type || '/', ?) > 0"
            )
            tech_parameters.append(f"/{bonus_ship_type_filter}/")
        if bonus_attribute_filter:
            tech_conditions.append("tp.bonus_attribute = ?")
            tech_parameters.append(bonus_attribute_filter)
        conditions.append(
            "EXISTS (SELECT 1 FROM tech_points tp WHERE "
            + " AND ".join(tech_conditions)
            + ")"
        )
        parameters.extend(tech_parameters)

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
                SELECT ship_id, stage, attribute, bonus_ship_type,
                       bonus_attribute, points
                FROM tech_points
                WHERE ship_id IN ({placeholders})
                """,
                [ship["id"] for ship in ships],
            ).fetchall()
            tech_by_ship: dict[int, list[dict[str, Any]]] = {}
            for tech_row in tech_rows:
                tech_point = dict(tech_row)
                ship_id = tech_point.pop("ship_id")
                tech_by_ship.setdefault(ship_id, []).append(tech_point)
            for ship in ships:
                ship_tech_points = tech_by_ship.get(ship["id"], [])
                ship_tech_points.sort(
                    key=lambda item: (
                        tech_stage_sort_key(item["stage"]),
                        bonus_ship_type_sort_key(
                            item["bonus_ship_type"].split("/")[0]
                        ),
                        bonus_attribute_sort_key(item["bonus_attribute"]),
                    )
                )
                bonus_types: set[str] = set()
                for item in ship_tech_points:
                    bonus_types.update(
                        part
                        for part in item["bonus_ship_type"].split("/")
                        if part
                    )
                ship["bonus_ship_types"] = sorted(
                    bonus_types, key=bonus_ship_type_sort_key
                )
                ship["tech_points"] = ship_tech_points
    return ships


def clear_ship_statuses(ship_id: int) -> dict[str, Any] | None:
    """清除单舰的已挂机、已练级状态，恢复为未记录。"""
    if ship_id <= 0:
        raise ValueError("舰娘 ID 无效")
    with get_connection() as connection:
        ship = connection.execute(
            "SELECT id, name, ship_type FROM ships WHERE id = ?", (ship_id,)
        ).fetchone()
        if ship is None:
            return None
        connection.execute(
            """
            UPDATE ships
            SET is_idling = NULL, is_trained = NULL, updated_at = ?
            WHERE id = ?
            """,
            (utc_now_text(), ship_id),
        )
        record_operation(
            connection,
            "clear_status",
            f"清除记录：{ship['name']}",
            ship_id=ship["id"],
            ship_name=ship["name"],
            ship_type=ship["ship_type"],
        )
    release_manual_target(ship_id)
    return {"id": ship_id, "is_idling": None, "is_trained": None}


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
    if value:
        # 完成后释放该舰所有已完成状态对应的录入，避免继续占用名额。
        from .config import config_transaction, read_config, write_config

        with config_transaction():
            _release_completed_manual_targets(
                ship_id, read_config, write_config
            )
    return {
        "id": row["id"],
        "is_idling": None if row["is_idling"] is None else bool(row["is_idling"]),
        "is_trained": None if row["is_trained"] is None else bool(row["is_trained"]),
    }


def _release_completed_manual_targets(ship_id: int, read_config, write_config) -> None:
    config = read_config()
    with get_connection() as connection:
        row = connection.execute(
            "SELECT is_idling, is_trained FROM ships WHERE id = ?", (ship_id,)
        ).fetchone()
    if row is None:
        return
    completed_fields = [
        field
        for field in ("is_idling", "is_trained")
        if row[field] == 1
    ]
    manual_targets = {
        key: {name: list(ids) for name, ids in value.items()}
        for key, value in config["manual_targets"].items()
    }
    changed = False
    for ship_type, targets in list(manual_targets.items()):
        for target_field in completed_fields:
            ids = targets.get(target_field)
            if ids and ship_id in ids:
                targets[target_field] = [value for value in ids if value != ship_id]
                changed = True
                if not targets[target_field]:
                    targets.pop(target_field, None)
        if not targets:
            manual_targets.pop(ship_type, None)
    if changed:
        write_config(
            {
                "next_ship_types": config["next_ship_types"],
                "target_limits": config["target_limits"],
                "manual_targets": manual_targets,
            }
        )


def release_manual_target(ship_id: int, field: str | None = None) -> None:
    """把舰船从手动录入配置中移除；field 为空时移除该舰的全部录入。"""
    from .config import config_transaction, read_config, write_config

    with config_transaction():
        _release_manual_target_locked(ship_id, field, read_config, write_config)


def _release_manual_target_locked(
    ship_id: int,
    field: str | None,
    read_config,
    write_config,
) -> None:
    config = read_config()
    manual_targets = {
        key: {name: list(ids) for name, ids in value.items()}
        for key, value in config["manual_targets"].items()
    }
    fields = [field] if field else ["is_idling", "is_trained"]
    changed = False
    for ship_type, targets in list(manual_targets.items()):
        for target_field in fields:
            ids = targets.get(target_field)
            if ids and ship_id in ids:
                targets[target_field] = [value for value in ids if value != ship_id]
                changed = True
                if not targets[target_field]:
                    targets.pop(target_field, None)
        if not targets:
            manual_targets.pop(ship_type, None)
    if changed:
        write_config(
            {
                "next_ship_types": config["next_ship_types"],
                "target_limits": config["target_limits"],
                "manual_targets": manual_targets,
            }
        )
