from __future__ import annotations

from typing import Any

from ..core.constants import (
    CUSTOM_SHIP_TAGS,
    RECOMMEND_TARGET_ORDER,
    RECOMMEND_TECH_SHIP_TYPES,
    TECH_PRIORITY_BY_TYPE,
    RARITY_PRIORITY_ORDER,
    normalize_ship_type,
    recommend_target_sort_key,
    ship_tag_match_condition,
    ship_tech_priority_score,
    tech_priority_order,
)
from ..storage.db import get_connection
from .ships import list_ships


def rarity_rank(ship: dict[str, Any]) -> int:
    return RARITY_PRIORITY_ORDER.get(ship["rarity"], len(RARITY_PRIORITY_ORDER))


def ship_bonus_attributes(ship: dict[str, Any]) -> dict[str, list[str]]:
    """舰船按加成舰种归组的加成属性。

    `bonus_ship_type` 可以是 `重巡/重炮/超巡` 这种多值，逐个归入对应舰种；
    一艘船可以同时出现在多个加成舰种里（这正是加成舰种的语义）。
    """
    grouped: dict[str, list[str]] = {}
    for tech_point in ship.get("tech_points", []):
        attribute = (tech_point.get("bonus_attribute") or "").strip()
        if not attribute:
            continue
        for bonus_ship_type in (tech_point.get("bonus_ship_type") or "").split("/"):
            bonus_ship_type = normalize_ship_type(bonus_ship_type)
            if not bonus_ship_type:
                continue
            attributes = grouped.setdefault(bonus_ship_type, [])
            if attribute not in attributes:
                attributes.append(attribute)
    return grouped


def ship_bonus_ship_types(ship: dict[str, Any]) -> list[str]:
    """舰船参与的全部加成舰种（保持首次出现顺序）。"""
    return list(ship_bonus_attributes(ship))


def tech_priority_rank(ship: dict[str, Any], ship_type: str = "") -> int:
    """舰船在某加成舰种下的匹配优先级，越小越优先。

    `ship_type` 为空时取该舰所有加成舰种里最靠前的一个（用于全局比较）。
    """
    grouped = ship_bonus_attributes(ship)
    if not grouped:
        return len(BONUS_SHIP_TYPE_ORDER) + 1
    if ship_type:
        return ship_tech_priority_score(ship_type, grouped.get(ship_type, []))
    return min(
        ship_tech_priority_score(candidate, attributes)
        for candidate, attributes in grouped.items()
    )


def list_tech_ship_type_options() -> list[dict[str, Any]]:
    """可配置的推荐目标：全部加成舰种 + 自定义标签（`RECOMMEND_TARGET_ORDER`）。

    顺序固定为 `recommend_target_sort_key()`：战列 > 战巡 > 正航 > 轻航 > 航战 >
    重炮 > 超巡 > 重巡 > 轻巡 > 驱逐 > 其他（按名称）> 标签。
    每项附带当前待挂机 / 待练级数量，口径与推荐候选一致。
    """
    counts = _pending_counts()
    tag_counts = _pending_tag_counts()
    options: list[dict[str, Any]] = []
    for target in sorted(RECOMMEND_TARGET_ORDER, key=recommend_target_sort_key):
        is_tag = target in CUSTOM_SHIP_TAGS
        bucket = (tag_counts if is_tag else counts).get(target, {})
        options.append(
            {
                "ship_type": target,
                "is_tag": is_tag,
                "has_priority_table": (not is_tag)
                and target in tech_priority_tables(),
                "priority": [] if is_tag else tech_priority_order(target),
                "idle_count": bucket.get("idle_count", 0),
                "train_count": bucket.get("train_count", 0),
            }
        )
    return options


def _pending_tag_counts() -> dict[str, dict[str, int]]:
    """按自定义标签统计待挂机 / 待练级数量（标签船通常没有科技点）。"""
    counts: dict[str, dict[str, int]] = {}
    with get_connection() as connection:
        for tag in CUSTOM_SHIP_TAGS:
            condition = ship_tag_match_condition(tag, "s")
            row = connection.execute(
                f"""
                SELECT SUM(CASE WHEN COALESCE(s.is_idling, 0) = 0 THEN 1 ELSE 0 END)
                           AS idle_count,
                       SUM(CASE WHEN s.is_idling = 1 THEN 1 ELSE 0 END) AS train_count
                FROM ships s
                WHERE {condition}
                  AND COALESCE(s.is_trained, 0) = 0
                """
            ).fetchone()
            counts[tag] = {
                "idle_count": int(row["idle_count"] or 0) if row else 0,
                "train_count": int(row["train_count"] or 0) if row else 0,
            }
    return counts


def tech_priority_tables() -> frozenset[str]:
    return frozenset(TECH_PRIORITY_BY_TYPE)


def _pending_counts() -> dict[str, dict[str, int]]:
    """按加成舰种统计待挂机与待练级数量。

    待挂机 = 需要练级、未练级且未挂机；待练级 = 需要练级、未练级且已挂机。
    一艘船可以在多个加成舰种下各计一次，但**同一加成舰种内只计一次**：
    一艘船常常有多条科技点（如「奇尔沙治」同时有耐久与炮击两条，
    bonus_ship_type 都是「战巡/战列/航战」），按 tech_points 行聚合会把
    同一艘船重复累加，数量与推荐列表对不上。
    """
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT s.id,
                   tp.bonus_ship_type,
                   COALESCE(s.is_idling, 0) AS is_idling
            FROM tech_points tp
            JOIN ships s ON s.id = tp.ship_id
            WHERE tp.bonus_ship_type <> ''
              AND s.needs_training = 1
              AND COALESCE(s.is_trained, 0) = 0
            """
        ).fetchall()
    # (加成舰种, 舰船 ID) 去重后再计数，避免同舰多条科技点被重复累加。
    seen: set[tuple[str, int]] = set()
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        for bonus_ship_type in row["bonus_ship_type"].split("/"):
            bonus_ship_type = normalize_ship_type(bonus_ship_type)
            if not bonus_ship_type:
                continue
            key = (bonus_ship_type, row["id"])
            if key in seen:
                continue
            seen.add(key)
            bucket = counts.setdefault(
                bonus_ship_type, {"idle_count": 0, "train_count": 0}
            )
            if row["is_idling"]:
                bucket["train_count"] += 1
            else:
                bucket["idle_count"] += 1
    return counts


def available_next_ship_types() -> list[dict[str, Any]]:
    """推荐配置的可选项（兼容旧名称，实现走加成舰种选项）。"""
    return list_tech_ship_type_options()


def known_ship_types() -> set[str]:
    return set(RECOMMEND_TARGET_ORDER)


def validate_ship_types(ship_types: list[str]) -> list[str]:
    """配置校验 + 规范化：加成舰种或自定义标签均可，去重并按展示顺序排列。"""
    normalized = _normalize_ship_types(ship_types)
    if not normalized:
        return normalized
    unknown = [value for value in normalized if value not in RECOMMEND_TARGET_ORDER]
    if unknown:
        raise ValueError("推荐目标不在可选范围内：" + "、".join(unknown))
    return sorted(normalized, key=recommend_target_sort_key)


def _normalize_ship_types(ship_types: list[str] | None) -> list[str]:
    if ship_types is None:
        return []
    if not isinstance(ship_types, list) or not all(
        isinstance(value, str) for value in ship_types
    ):
        raise ValueError("next_ship_types 必须是加成舰种或自定义标签列表")
    normalized: list[str] = []
    for value in ship_types:
        ship_type = normalize_ship_type(value)
        if ship_type and ship_type not in normalized:
            normalized.append(ship_type)
    return normalized


def is_ship_tag(target: str) -> bool:
    return target in CUSTOM_SHIP_TAGS


def _candidate_filters(mode: str, target: str) -> dict[str, Any]:
    """推荐候选的筛选条件。

    加成舰种候选要求“需要练级 = 是”；自定义标签（小船/联动）与是否需要练级
    正交——标签船普遍没有科技点（`needs_training = 0`），一旦带上该条件
    结果恒为空，因此标签候选不附加它。
    """
    filters: dict[str, Any] = {
        "is_idling": "否" if mode == "idle" else "是",
        "is_trained": "否" if mode == "train" else "",
    }
    if is_ship_tag(target):
        filters["ship_types"] = [target]
    else:
        filters["needs_training"] = "是"
    return filters


def _match_ship_type(ship: dict[str, Any], ship_type: str) -> bool:
    """舰船是否属于某个推荐目标（加成舰种或自定义标签）。"""
    if is_ship_tag(ship_type):
        return ship_type in (ship.get("tags") or [])
    return ship_type in ship_bonus_attributes(ship)


def _tag_sort_key(ship: dict[str, Any]) -> tuple[Any, ...]:
    """标签行内排序：稀有度 > 实装日期（升序，空日期排最后）> 名称。"""
    return (
        rarity_rank(ship),
        ship.get("release_date") == "",
        ship.get("release_date") or "",
        ship["name"],
    )


def _sort_candidates(
    candidates: list[dict[str, Any]],
    ship_type: str,
) -> list[dict[str, Any]]:
    """行内候选排序。

    加成舰种行：先按该舰种属性优先级，再按稀有度，最后按名称。
    标签行：标签船没有科技点，没有属性优先级可用，按稀有度 > 实装日期 > 名称。
    """
    if is_ship_tag(ship_type):
        return sorted(candidates, key=_tag_sort_key)
    grouped = [(ship, ship_bonus_attributes(ship).get(ship_type, [])) for ship in candidates]
    return [
        ship
        for ship, _ in sorted(
            grouped,
            key=lambda item: (
                ship_tech_priority_score(ship_type, item[1]),
                rarity_rank(item[0]),
                item[0]["name"],
            ),
        )
    ]


def next_ship(
    mode: str,
    ship_types: list[str] | None = None,
) -> dict[str, Any] | None:
    if mode not in {"idle", "train"}:
        raise ValueError("mode 必须是 idle 或 train")
    types = _normalize_ship_types(ship_types)
    if not types:
        return None
    for ship_type in sorted(types, key=recommend_target_sort_key):
        candidates = [
            ship
            for ship in list_ships(_candidate_filters(mode, ship_type))
            if _match_ship_type(ship, ship_type)
        ]
        if candidates:
            return _sort_candidates(candidates, ship_type)[0]
    return None


MANUAL_FIELD_LABELS = {"is_idling": "挂机", "is_trained": "练级"}
MODE_BY_FIELD = {"is_idling": "idle", "is_trained": "train"}


def _is_completed(ship: dict[str, Any], field: str) -> bool:
    return ship[field] is True


def _manual_target_ships(
    ship_type: str,
    field: str,
    ship_ids: list[int],
) -> list[dict[str, Any]]:
    if not ship_ids:
        return []
    ships = list_ships({"ship_ids": ship_ids})
    by_id = {ship["id"]: ship for ship in ships}
    return [
        by_id[ship_id]
        for ship_id in ship_ids
        if ship_id in by_id
        and _match_ship_type(by_id[ship_id], ship_type)
        and not _is_completed(by_id[ship_id], field)
    ]


def _auto_target_ships(
    ship_type: str,
    mode: str,
    limit: int,
    exclude_ids: set[int],
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    candidates = [
        ship
        for ship in list_ships(_candidate_filters(mode, ship_type))
        if ship["id"] not in exclude_ids and _match_ship_type(ship, ship_type)
    ]
    return _sort_candidates(candidates, ship_type)[:limit]


def _target_items(
    ships: list[dict[str, Any]],
    manual_ids: set[int],
) -> list[dict[str, Any]]:
    return [
        {"ship": ship, "manual": ship["id"] in manual_ids}
        for ship in ships
    ]


def next_ships_by_type(
    mode: str,
    ship_types: list[str],
    manual_targets: dict[str, dict[str, list[int]]] | None = None,
    target_limits: dict[str, dict[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """按推荐目标（加成舰种 / 自定义标签）逐行给出挂机 / 练级推荐。"""
    if mode not in {"both", "idle", "train"}:
        raise ValueError("mode 必须是 both、idle 或 train")
    types = _normalize_ship_types(ship_types)
    manual_targets = manual_targets or {}
    target_limits = target_limits or {}
    counts = _pending_counts()
    tag_counts = _pending_tag_counts()
    rows: list[dict[str, Any]] = []
    for ship_type in sorted(types, key=recommend_target_sort_key):
        is_tag = is_ship_tag(ship_type)
        type_targets = manual_targets.get(ship_type, {})
        type_limits = target_limits.get(ship_type, {})
        type_counts = (tag_counts if is_tag else counts).get(ship_type, {})
        row: dict[str, Any] = {
            "ship_type": ship_type,
            "is_tag": is_tag,
            "has_priority_table": (not is_tag)
            and ship_type in tech_priority_tables(),
            "idle_count": type_counts.get("idle_count", 0),
            "train_count": type_counts.get("train_count", 0),
        }
        for field, mode_name in (
            ("is_idling", "idle"),
            ("is_trained", "train"),
        ):
            manual_ids = list(type_targets.get(field, []))
            limit = type_limits.get(field, 1)
            manual_ships = _manual_target_ships(ship_type, field, manual_ids)
            found_manual_ids = {ship["id"] for ship in manual_ships}
            stale_count = len(manual_ids) - len(manual_ships)
            items: list[dict[str, Any]] = []
            if mode in {"both", mode_name}:
                items.extend(_target_items(manual_ships, found_manual_ids))
                items.extend(
                    _target_items(
                        _auto_target_ships(
                            ship_type,
                            mode_name,
                            max(0, limit - len(items)),
                            found_manual_ids,
                        ),
                        set(),
                    )
                )
            else:
                items.extend(_target_items(manual_ships, found_manual_ids))
            row[f"{mode_name}_items"] = items
            row[f"{mode_name}_limit"] = limit
            row[f"{mode_name}_manual_count"] = len(manual_ships)
            row[f"{mode_name}_manual_stale"] = stale_count > 0
            row[mode_name] = items[0]["ship"] if items else None
        modes = ["idle", "train"] if mode == "both" else [mode]
        if any(row[f"{mode_name}_items"] for mode_name in modes):
            rows.append(row)
    return rows
