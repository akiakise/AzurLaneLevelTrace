from __future__ import annotations

import sqlite3
from typing import Any

from ..core.constants import (
    ARMAMENT_GROUP_FLAGS,
    ARMAMENT_GROUP_LABELS,
    ARMAMENT_RARITY_PRIORITY,
    ARMAMENT_RECOMMEND_GROUP_ORDER,
    ARMAMENT_STATUS_FIELDS,
)
from ..core.utils import utc_now_text
from ..storage.db import get_connection
from ..storage.history import record_operation


# “未完成”把打造与强化视为一组：任意一项不是已完成（未记录或未完成）即命中。
# 必须用 COALESCE 兜住 NULL，否则 NOT (NULL = 1) 仍为 NULL，未记录的行会被漏掉。
ARMAMENT_PENDING_CONDITION = (
    "NOT (COALESCE(a.is_built, 0) = 1 AND COALESCE(a.is_enhanced, 0) = 1)"
)


# 兵装查询统一联表 ships，名称、舰种、稀有度都以舰船表为准。
ARMAMENT_SELECT = """
    SELECT a.id, a.ship_id, a.release_date, a.is_collab, a.collab_group,
           a.collab_order, a.is_built, a.is_enhanced,
           s.name, s.ship_type, s.rarity
    FROM armaments a
    JOIN ships s ON s.id = a.ship_id
"""


def armament_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    is_collab = bool(row["is_collab"])
    return {
        "id": row["id"],
        "ship_id": row["ship_id"],
        "name": row["name"],
        "ship_type": row["ship_type"],
        "rarity": row["rarity"],
        "release_date": row["release_date"],
        "is_collab": is_collab,
        "group": "collab" if is_collab else "normal",
        "group_label": ARMAMENT_GROUP_LABELS["collab" if is_collab else "normal"],
        "collab_group": row["collab_group"],
        "collab_order": row["collab_order"],
        "is_built": None if row["is_built"] is None else bool(row["is_built"]),
        "is_enhanced": (
            None if row["is_enhanced"] is None else bool(row["is_enhanced"])
        ),
    }


def upsert_armament(
    connection: sqlite3.Connection,
    ship_id: int,
    entry: dict[str, Any],
) -> None:
    """写入或更新一条兵装。

    同步只覆盖 Wiki 数据，保留用户已标记的打造/强化状态，避免重复同步丢状态。
    """
    current_time = utc_now_text()
    connection.execute(
        """
        INSERT INTO armaments (
            ship_id, release_date, is_collab, collab_group, collab_order,
            is_built, is_enhanced, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)
        ON CONFLICT(ship_id) DO UPDATE SET
          release_date = excluded.release_date,
          is_collab = excluded.is_collab,
          collab_group = excluded.collab_group,
          collab_order = excluded.collab_order,
          updated_at = excluded.updated_at
        """,
        (
            ship_id,
            entry.get("release_date", ""),
            1 if entry.get("is_collab") else 0,
            entry.get("collab_group", ""),
            int(entry.get("collab_order") or 0),
            current_time,
            current_time,
        ),
    )


def delete_armament_for_ship(
    connection: sqlite3.Connection, ship_id: int
) -> int:
    """删除某舰船的兵装，返回删除条数；删除舰船时随外键级联之外显式调用。"""
    cursor = connection.execute(
        "DELETE FROM armaments WHERE ship_id = ?", (ship_id,)
    )
    return cursor.rowcount


def _build_armament_conditions(
    filters: dict[str, Any],
) -> tuple[list[str], list[Any]]:
    conditions: list[str] = []
    parameters: list[Any] = []
    if filters.get("name"):
        conditions.append("s.name LIKE ?")
        parameters.append(f"%{filters['name']}%")
    armament_ids = [
        int(value)
        for value in (filters.get("armament_ids") or [])
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    if armament_ids:
        conditions.append(f"a.id IN ({','.join('?' for _ in armament_ids)})")
        parameters.extend(armament_ids)
    if filters.get("ship_type"):
        conditions.append("s.ship_type = ?")
        parameters.append(filters["ship_type"])
    if filters.get("rarity"):
        conditions.append("s.rarity = ?")
        parameters.append(filters["rarity"])
    group = str(filters.get("group") or "").strip()
    if group in ARMAMENT_GROUP_FLAGS:
        conditions.append("a.is_collab = ?")
        parameters.append(ARMAMENT_GROUP_FLAGS[group])
    # 兵装只有“是否联动”一个维度，is_collab=是/否 与 group 等价，两条入口都支持。
    is_collab = str(filters.get("is_collab") or "").strip()
    if is_collab in {"是", "否"}:
        conditions.append("a.is_collab = ?")
        parameters.append(1 if is_collab == "是" else 0)
    for field in ARMAMENT_STATUS_FIELDS:
        value = str(filters.get(field) or "").strip()
        if value == "是":
            conditions.append(f"a.{field} = 1")
        elif value == "否":
            conditions.append(f"COALESCE(a.{field}, 0) = 0")
    # 未完成 = 未打造或未强化，仅“是”需要筛选，“否”等同不筛选。
    if str(filters.get("pending") or "").strip() == "是":
        conditions.append(ARMAMENT_PENDING_CONDITION)
    return conditions, parameters


def count_pending_armament_statuses(filters: dict[str, Any]) -> dict[str, int]:
    """统计当前其他筛选条件下未打造、未强化与未完成的兵装数量。"""
    counts: dict[str, int] = {}
    count_fields = [
        ("is_built", "COALESCE(a.is_built, 0) = 0"),
        ("is_enhanced", "COALESCE(a.is_enhanced, 0) = 0"),
        (
            "pending",
            "NOT (COALESCE(a.is_built, 0) = 1 AND COALESCE(a.is_enhanced, 0) = 1)",
        ),
    ]
    for field, condition in count_fields:
        conditions, parameters = _build_armament_conditions(
            {key: value for key, value in filters.items() if key != field}
        )
        conditions.append(condition)
        with get_connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) FROM armaments a
                JOIN ships s ON s.id = a.ship_id
                WHERE {' AND '.join(conditions)}
                """,
                parameters,
            ).fetchone()
        counts[field] = int(row[0]) if row else 0
    return counts


def list_armaments(filters: dict[str, Any]) -> list[dict[str, Any]]:
    conditions, parameters = _build_armament_conditions(filters)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    # 常规兵装按实装日期正序，联动兵装没有日期，按 Wiki 列表顺序排在最后。
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            {ARMAMENT_SELECT}
            {where_clause}
            ORDER BY a.is_collab,
                     CASE WHEN a.is_collab = 0 THEN a.release_date END,
                     a.collab_order,
                     s.name COLLATE NOCASE
            """,
            parameters,
        ).fetchall()
    return [armament_row_to_dict(row) for row in rows]


def find_armament_brief(armament_id: int) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            f"{ARMAMENT_SELECT} WHERE a.id = ?", (armament_id,)
        ).fetchone()
    return armament_row_to_dict(row) if row is not None else None


def clear_armament_statuses(armament_id: int) -> dict[str, Any] | None:
    """清除单件兵装的打造与强化状态，恢复为未记录。"""
    if armament_id <= 0:
        raise ValueError("兵装 ID 无效")
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT a.id, a.ship_id, s.name, s.ship_type
            FROM armaments a JOIN ships s ON s.id = a.ship_id
            WHERE a.id = ?
            """,
            (armament_id,),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            """
            UPDATE armaments
            SET is_built = NULL, is_enhanced = NULL, updated_at = ?
            WHERE id = ?
            """,
            (utc_now_text(), armament_id),
        )
        record_operation(
            connection,
            "clear_armament_status",
            f"清除兵装记录：{row['name']}",
            ship_id=row["ship_id"],
            ship_name=row["name"],
            ship_type=row["ship_type"],
        )
    _release_completed_armament_targets(armament_id)
    return {"id": armament_id, "is_built": None, "is_enhanced": None}


def update_armament_status(
    armament_id: int,
    field: str,
    value: bool,
) -> dict[str, Any] | None:
    """更新兵装状态；标记强化完成时自动联动标记打造完成。"""
    if field not in ARMAMENT_STATUS_FIELDS:
        raise ValueError("field 只支持 is_built 或 is_enhanced")
    if not isinstance(value, bool):
        raise ValueError("value 必须是布尔值")
    label = ARMAMENT_STATUS_FIELDS[field]
    action_text = "标记为" if value else "取消标记为"
    with get_connection() as connection:
        current = connection.execute(
            """
            SELECT a.is_built, s.id AS ship_id, s.name, s.ship_type
            FROM armaments a JOIN ships s ON s.id = a.ship_id
            WHERE a.id = ?
            """,
            (armament_id,),
        ).fetchone()
        if current is None:
            return None
        was_built = current["is_built"]
        if field == "is_enhanced" and value:
            # 强化必然先打造，标记强化完成时同步补齐打造完成。
            connection.execute(
                """
                UPDATE armaments
                SET is_enhanced = 1, is_built = 1, updated_at = ?
                WHERE id = ?
                """,
                (utc_now_text(), armament_id),
            )
        else:
            connection.execute(
                f"UPDATE armaments SET {field} = ?, updated_at = ? WHERE id = ?",
                (1 if value else 0, utc_now_text(), armament_id),
            )
        row = connection.execute(
            "SELECT is_built, is_enhanced FROM armaments WHERE id = ?",
            (armament_id,),
        ).fetchone()
        message = f"{action_text}{label}完成：{current['name']}（兵装）"
        if field == "is_enhanced" and value and not was_built:
            message += "（同时标记为打造完成）"
        record_operation(
            connection,
            f"armament_{field}",
            message,
            ship_id=current["ship_id"],
            ship_name=current["name"],
            ship_type=current["ship_type"],
        )
    if value:
        _release_completed_armament_targets(armament_id)
    return {
        "id": armament_id,
        "ship_id": current["ship_id"],
        "is_built": None if row["is_built"] is None else bool(row["is_built"]),
        "is_enhanced": (
            None if row["is_enhanced"] is None else bool(row["is_enhanced"])
        ),
    }


def delete_armament(armament_id: int) -> dict[str, Any] | None:
    """删除一件兵装，返回被删除兵装的名称；不存在时返回 None。"""
    if armament_id <= 0:
        raise ValueError("兵装 ID 无效")
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT a.id, a.ship_id, s.name, s.ship_type
            FROM armaments a JOIN ships s ON s.id = a.ship_id
            WHERE a.id = ?
            """,
            (armament_id,),
        ).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM armaments WHERE id = ?", (armament_id,))
        record_operation(
            connection,
            "delete_armament",
            f"删除兵装「{row['name']}」",
            ship_id=row["ship_id"],
            ship_name=row["name"],
            ship_type=row["ship_type"],
        )
    _release_completed_armament_targets(armament_id)
    return {
        "id": armament_id,
        "ship_id": row["ship_id"],
        "name": row["name"],
    }


def armament_group_counts() -> dict[str, int]:
    """返回联动与常规兵装数量，用于推荐页分组展示。"""
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT is_collab,
                   SUM(CASE WHEN COALESCE(is_built, 0) = 0 THEN 1 ELSE 0 END)
                       AS unbuilt,
                   SUM(CASE WHEN COALESCE(is_enhanced, 0) = 0 THEN 1 ELSE 0 END)
                       AS unenhanced
            FROM armaments
            GROUP BY is_collab
            """
        ).fetchall()
    counts = {
        "collab": {"built_count": 0, "enhanced_count": 0},
        "normal": {"built_count": 0, "enhanced_count": 0},
    }
    for row in rows:
        group = "collab" if row["is_collab"] else "normal"
        counts[group] = {
            "built_count": int(row["unbuilt"] or 0),
            "enhanced_count": int(row["unenhanced"] or 0),
        }
    return counts


def _release_completed_armament_targets(armament_id: int) -> None:
    """兵装完成或删除后，把它从兵装手动录入配置里移除，释放名额。"""
    from ..storage.config import config_transaction, read_config, write_config

    with config_transaction():
        config = read_config()
        manual = {
            key: {name: list(ids) for name, ids in value.items()}
            for key, value in config["armament_manual_targets"].items()
        }
        changed = False
        for group, targets in list(manual.items()):
            for field, ids in list(targets.items()):
                if armament_id in ids:
                    targets[field] = [value for value in ids if value != armament_id]
                    changed = True
                    if not targets[field]:
                        targets.pop(field, None)
            if not targets:
                manual.pop(group, None)
        if not changed:
            return
        write_config(
            {
                "next_ship_types": config["next_ship_types"],
                "target_limits": config["target_limits"],
                "manual_targets": config["manual_targets"],
                "armament_target_limits": config["armament_target_limits"],
                "armament_manual_targets": manual,
            }
        )


def _armament_sort_key(armament: dict[str, Any]) -> tuple[Any, ...]:
    """排序：海上传奇、决战方案、最高方案优先，其余按实装日期。

    三个特殊稀有度之后再按实装日期升序；联动兵装没有日期，退回 Wiki 列表顺序。
    名称只用于兜底，避免同日期时顺序不稳定。
    """
    return (
        ARMAMENT_RARITY_PRIORITY.get(
            armament["rarity"], len(ARMAMENT_RARITY_PRIORITY)
        ),
        armament["release_date"] or "9999-99-99",
        armament["collab_order"],
        armament["name"],
    )


def _manual_armaments(
    group: str,
    field: str,
    armament_ids: list[int],
) -> list[dict[str, Any]]:
    if not armament_ids:
        return []
    rows = list_armaments({"group": group, "armament_ids": armament_ids})
    by_id = {row["id"]: row for row in rows}
    return [
        by_id[armament_id]
        for armament_id in armament_ids
        if armament_id in by_id and by_id[armament_id][field] is not True
    ]


def _auto_armaments(
    group: str,
    field: str,
    limit: int,
    exclude_ids: set[int],
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    filters = {"group": group, field: "否"}
    candidates = [
        armament
        for armament in list_armaments(filters)
        if armament["id"] not in exclude_ids
    ]
    return sorted(candidates, key=_armament_sort_key)[:limit]


def next_armaments_by_group(
    mode: str,
    manual_targets: dict[str, dict[str, list[int]]] | None = None,
    target_limits: dict[str, dict[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """按联动/常规两组给出兵装推荐，结构对齐 next_ships_by_type。"""
    if mode not in {"both", "built", "enhanced"}:
        raise ValueError("mode 必须是 both、built 或 enhanced")
    manual_targets = manual_targets or {}
    target_limits = target_limits or {}
    rows: list[dict[str, Any]] = []
    for group in ARMAMENT_RECOMMEND_GROUP_ORDER:
        group_targets = manual_targets.get(group, {})
        group_limits = target_limits.get(group, {})
        row: dict[str, Any] = {
            "group": group,
            "group_label": ARMAMENT_GROUP_LABELS[group],
        }
        for field, mode_name in (
            ("is_built", "built"),
            ("is_enhanced", "enhanced"),
        ):
            manual_ids = list(group_targets.get(field, []))
            limit = group_limits.get(field, 1)
            manual_rows = _manual_armaments(group, field, manual_ids)
            found_manual_ids = {armament["id"] for armament in manual_rows}
            stale_count = len(manual_ids) - len(manual_rows)
            items: list[dict[str, Any]] = []
            if mode in {"both", mode_name}:
                items.extend(
                    {"armament": armament, "manual": True}
                    for armament in manual_rows
                )
                items.extend(
                    {"armament": armament, "manual": False}
                    for armament in _auto_armaments(
                        group,
                        field,
                        max(0, limit - len(items)),
                        found_manual_ids,
                    )
                )
            else:
                items.extend(
                    {"armament": armament, "manual": True}
                    for armament in manual_rows
                )
            row[f"{mode_name}_items"] = items
            row[f"{mode_name}_limit"] = limit
            row[f"{mode_name}_manual_count"] = len(manual_rows)
            row[f"{mode_name}_manual_stale"] = stale_count > 0
            row[mode_name] = items[0]["armament"] if items else None
        if row["built_items"] or row["enhanced_items"]:
            rows.append(row)
    return rows
