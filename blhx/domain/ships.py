from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import quote

from ..core.constants import (
    CUSTOM_SHIP_TAGS,
    WIKI_BASE_URL,
    bonus_attribute_sort_key,
    bonus_ship_type_sort_key,
    normalize_ship_type,
    ship_tag_condition,
    ship_tag_match_condition,
    split_ship_tags,
    tech_stage_sort_key,
)
from ..storage.config import remove_config
from ..storage.db import get_connection
from ..storage.history import record_operation
from ..core.state import SYNC_STATE
from .tech import parse_tech_attribute
from ..core.utils import utc_now_text

# “未完成”把挂机与练级视为一组：任意一项不是已完成（未记录或未完成）即命中。
# 必须用 COALESCE 兜住 NULL，否则 NOT (NULL = 1) 仍为 NULL，未记录的行会被漏掉。
PENDING_CONDITION = (
    "NOT (COALESCE(s.is_idling, 0) = 1 AND COALESCE(s.is_trained, 0) = 1)"
)


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
        "tags": split_ship_tags(row["tags"]),
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
    # 标签在同步时写入；规则内含科技点判定，需在 needs_training 落库后再算。
    refresh_ship_tags(connection, ship["id"])
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


def refresh_ship_tags(
    connection: sqlite3.Connection,
    ship_id: int | None = None,
    category_tags: dict[str, set[str]] | None = None,
) -> int:
    """重算并写入 ships.tags，返回更新条数。

    标签分两类来源，合并后写入同一列：

    - 本地规则型（`CUSTOM_SHIP_TAG_RULES`）：用 SQL 从舰船自身字段判定，如“小船”。
    - 名称集合型（`CATEGORY_SHIP_TAGS`）：成员由同步流程从 Wiki 分类抓取后传入，
      如“联动”，无法用本地字段表达。

    同步时会顺带写入初值，存量数据需要一次全量同步重刷才能补上 tags 列。
    未传入名称集合时，对应标签按不命中处理，避免重刷清空已有标签。
    """
    category_tags = category_tags or {}
    select_parts: list[str] = []
    parameters: list[Any] = []
    # 每个标签产出一个“命中则为标签名、否则空串”的片段。
    for tag in CUSTOM_SHIP_TAGS:
        rule = ship_tag_condition(tag, "")
        if rule:
            select_parts.append(f"CASE WHEN {rule} THEN '{tag}' ELSE '' END")
        elif tag in category_tags:
            names = sorted(category_tags[tag])
            if names:
                placeholders = ",".join("?" for _ in names)
                select_parts.append(
                    f"CASE WHEN name IN ({placeholders}) THEN '{tag}' ELSE '' END"
                )
                parameters.extend(names)
            else:
                select_parts.append("''")
        else:
            # 既无本地规则也未提供名称集合：本标签不命中，但保留占位以免影响拼接。
            select_parts.append("''")
    pieces = [
        part if index == 0 else f"'/' || {part}"
        for index, part in enumerate(select_parts)
    ]
    tag_expression = "TRIM(REPLACE(" + " || ".join(pieces) + ", '//', '/'), '/')"
    where_clause = "WHERE id = ?" if ship_id is not None else ""
    if ship_id is not None:
        parameters.append(ship_id)
    cursor = connection.execute(
        f"UPDATE ships SET tags = {tag_expression} {where_clause}",
        parameters,
    )
    return cursor.rowcount


def delete_ship(ship_id: int) -> bool:
    if ship_id <= 0:
        raise ValueError("舰娘 ID 无效")
    with get_connection() as connection:
        ship = connection.execute(
            "SELECT id, name, ship_type FROM ships WHERE id = ?", (ship_id,)
        ).fetchone()
        if ship is None:
            return False
        # 兵装随舰船一起删除；外键级联之外再显式删除一层。
        connection.execute("DELETE FROM armaments WHERE ship_id = ?", (ship_id,))
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
    from ..storage.config import config_transaction
    from ..core.state import SYNC_LOCK

    with config_transaction(), SYNC_LOCK:
        if SYNC_STATE["running"]:
            raise ValueError("同步正在进行中，请稍后再清空数据")
        remove_config()
        with get_connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM ships").fetchone()[0]
            connection.execute("DELETE FROM ships")
            connection.execute("DELETE FROM tech_points")
            connection.execute("DELETE FROM armaments")
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
                "armaments": 0,
                "errors": [],
            }
        )
        return count


def _build_ship_conditions(
    filters: dict[str, Any],
    skip_fields: frozenset[str] = frozenset(),
) -> tuple[list[str], list[Any]]:
    """按筛选条件生成 WHERE 片段；skip_fields 用于忽略指定状态筛选。"""
    conditions: list[str] = []
    parameters: list[Any] = []
    ship_type_filters = [
        normalize_ship_type(value.strip())
        for value in (filters.get("ship_types") or [])
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
    trainable_folded = False
    if ship_type_filters:
        # 自定义标签与舰种正交：标签走独立判定，舰种照常等值匹配，
        # 多个选项之间取并集，因此一艘小船既能被“小船”筛到，也能被原舰种筛到。
        tag_types = [value for value in ship_type_filters if value in CUSTOM_SHIP_TAGS]
        plain_types = [
            value for value in ship_type_filters if value not in CUSTOM_SHIP_TAGS
        ]
        # 标签本身就允许无科技点（小船就是），不能把“需要练级 = 是”整条 AND 上去，
        # 否则选中标签时结果恒为空；此时把它并进舰种分支内。
        trainable_folded = bool(tag_types) and filters.get("needs_training") == "是"
        type_conditions: list[str] = []
        if plain_types:
            plain_condition = (
                f"s.ship_type IN ({','.join('?' for _ in plain_types)})"
            )
            if trainable_folded:
                plain_condition = f"({plain_condition} AND s.needs_training = 1)"
            type_conditions.append(plain_condition)
            parameters.extend(plain_types)
        # 标签一律按同步产出的 ships.tags 匹配，不按规则现算：
        # 未重刷同步的存量数据 tags 为空，筛不到标签（跑一次全量同步即可）。
        for tag in tag_types:
            type_conditions.append(ship_tag_match_condition(tag, "s"))
        conditions.append("(" + " OR ".join(type_conditions) + ")")
    if filters.get("rarity"):
        conditions.append("s.rarity = ?")
        parameters.append(filters["rarity"])
    if filters.get("needs_training") in {"是", "否"} and not trainable_folded:
        conditions.append("s.needs_training = ?")
        parameters.append(1 if filters["needs_training"] == "是" else 0)
    if "is_idling" not in skip_fields and filters.get("is_idling") in {"是", "否"}:
        if filters["is_idling"] == "是":
            conditions.append("s.is_idling = 1")
        else:
            conditions.append("COALESCE(s.is_idling, 0) = 0")
    if "is_trained" not in skip_fields and filters.get("is_trained") in {"是", "否"}:
        if filters["is_trained"] == "是":
            conditions.append("s.is_trained = 1")
        else:
            conditions.append("COALESCE(s.is_trained, 0) = 0")
    if "pending" not in skip_fields and filters.get("pending") == "是":
        # 未完成 = 未挂机或未练级，两者只要有一项未记录就命中。
        conditions.append(PENDING_CONDITION)
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
    return conditions, parameters


def count_pending_ship_statuses(filters: dict[str, Any]) -> dict[str, int]:
    """统计当前其他筛选条件下未挂机、未练级与未完成的舰船数量。"""
    counts: dict[str, int] = {}
    count_fields = [
        ("is_idling", "COALESCE(s.is_idling, 0) = 0"),
        ("is_trained", "COALESCE(s.is_trained, 0) = 0"),
        ("pending", PENDING_CONDITION),
    ]
    for field, condition in count_fields:
        conditions, parameters = _build_ship_conditions(
            filters, frozenset({field})
        )
        conditions.append(condition)
        with get_connection() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) FROM ships s WHERE {' AND '.join(conditions)}",
                parameters,
            ).fetchone()
        counts[field] = int(row[0]) if row else 0
    return counts


def list_ships(filters: dict[str, Any]) -> list[dict[str, Any]]:
    conditions, parameters = _build_ship_conditions(filters)

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
        from ..storage.config import config_transaction, read_config, write_config

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
                "armament_target_limits": config["armament_target_limits"],
                "armament_manual_targets": config["armament_manual_targets"],
            }
        )


def release_manual_target(ship_id: int, field: str | None = None) -> None:
    """把舰船从手动录入配置中移除；field 为空时移除该舰的全部录入。"""
    from ..storage.config import config_transaction, read_config, write_config

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
                "armament_target_limits": config["armament_target_limits"],
                "armament_manual_targets": config["armament_manual_targets"],
            }
        )
