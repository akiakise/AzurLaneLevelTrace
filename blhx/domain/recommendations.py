from __future__ import annotations

from typing import Any

from ..core.constants import (
    CUSTOM_SHIP_TAGS,
    DEFAULT_TECH_PRIORITY,
    normalize_ship_type,
    RARITY_PRIORITY_ORDER,
    SHIP_TYPE_ORDER,
    TECH_PRIORITY_BY_TYPE,
    recommend_ship_type_sort_key,
    ship_tag_match_condition,
)
from ..storage.db import get_connection
from .ships import list_ships


def rarity_rank(ship: dict[str, Any]) -> int:
    return RARITY_PRIORITY_ORDER.get(ship["rarity"], len(RARITY_PRIORITY_ORDER))


def tech_priority_rank(ship: dict[str, Any]) -> int:
    ranks: list[int] = []
    for tech_point in ship["tech_points"]:
        attribute = tech_point.get("bonus_attribute") or ""
        bonus_ship_types = [
            value
            for value in tech_point.get("bonus_ship_type", "").split("/")
            if value
        ] or [ship["ship_type"]]
        for bonus_ship_type in bonus_ship_types:
            priority = TECH_PRIORITY_BY_TYPE.get(
                bonus_ship_type, DEFAULT_TECH_PRIORITY
            )
            ranks.append(
                priority.index(attribute) if attribute in priority else len(priority)
            )
    return min(ranks) if ranks else len(DEFAULT_TECH_PRIORITY) + 1


def _pending_ship_type_counts() -> dict[str, dict[str, int]]:
    """按舰种或标签统计待挂机与待练级数量。

    舰种口径与推荐候选一致：只看需要练级且尚未练级的舰船。
    待挂机 = 其中还没挂机的，待练级 = 其中已经挂机、等待练级的。
    标签口径按标签规则判定（标签与“是否需要练级”正交），同样排除已练级。
    类型徽标与推荐表格每行的数量都走这一份统计，避免两处口径漂移。
    """
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT ship_type,
                   SUM(CASE WHEN COALESCE(is_idling, 0) = 0 THEN 1 ELSE 0 END)
                       AS idle_count,
                   SUM(CASE WHEN is_idling = 1 THEN 1 ELSE 0 END) AS train_count
            FROM ships
            WHERE ship_type <> '' AND needs_training = 1
              AND COALESCE(is_trained, 0) = 0
            GROUP BY ship_type
            """
        ).fetchall()
        counts = {
            normalize_ship_type(row["ship_type"]): {
                "idle_count": int(row["idle_count"] or 0),
                "train_count": int(row["train_count"] or 0),
            }
            for row in rows
        }
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


def _known_ship_type_names() -> list[str]:
    """舰种常量全集（含自定义标签），别名归一后去重。"""
    return list(
        dict.fromkeys(
            normalized
            for normalized in (
                normalize_ship_type(value)
                for value in [*SHIP_TYPE_ORDER, *CUSTOM_SHIP_TAGS]
            )
            if normalized
        )
    )


def available_next_ship_types() -> list[dict[str, Any]]:
    """可配置的推荐舰种。

    可选范围只取决于舰种常量全集，与数据库当前是否有可练级舰娘无关：
    舰种可以先行配置，等后续同步或导入出新的可练级舰船后自动参与推荐。
    当前没有候选时计数为 0，页面照常展示 0 / 0。
    自定义标签（小船、联动）也在可选范围内，排序固定排在最后。
    """
    counts = _pending_ship_type_counts()
    # 以常量全集为准；别名归一后去重，避免风帆M/S/V 生成重复选项。
    ship_types = _known_ship_type_names()
    # 数据库中出现但常量未覆盖的舰种也一并保留，避免既有配置无法再选中。
    ship_types.extend(
        ship_type for ship_type in counts if ship_type not in ship_types
    )
    return sorted(
        (
            {
                "ship_type": ship_type,
                "idle_count": counts.get(ship_type, {}).get("idle_count", 0),
                "train_count": counts.get(ship_type, {}).get("train_count", 0),
            }
            for ship_type in ship_types
        ),
        key=lambda option: recommend_ship_type_sort_key(option["ship_type"]),
    )


def _normalize_ship_types(
    ship_type: str,
    ship_types: list[str] | None,
) -> list[str]:
    if ship_types is None:
        ship_types = [ship_type] if ship_type else []
    if not isinstance(ship_types, list) or not all(
        isinstance(value, str) for value in ship_types
    ):
        raise ValueError("ship_types 必须是舰种列表")
    # 先归一别名（风帆M/S/V → 风帆）再去重，避免同一舰种被分成多行。
    return list(
        dict.fromkeys(
            normalize_ship_type(value) for value in ship_types if value.strip()
        )
    )


def known_ship_types() -> list[str]:
    """全部合法舰种（常量全集含自定义舰种 + 库中已存在舰种），用于配置校验。"""
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT DISTINCT ship_type FROM ships WHERE ship_type <> ''"
        ).fetchall()
    ship_types = _known_ship_type_names()
    ship_types.extend(
        normalize_ship_type(row["ship_type"])
        for row in rows
        if normalize_ship_type(row["ship_type"]) not in ship_types
    )
    return ship_types


def _validate_known_ship_types(ship_types: list[str]) -> None:
    """配置校验只要求舰种合法，不要求当前存在可练级舰娘。"""
    if not ship_types:
        return
    known = set(known_ship_types())
    unknown_types = [value for value in ship_types if value not in known]
    if unknown_types:
        raise ValueError("舰种不存在：" + "、".join(unknown_types))


def _validate_ship_types(ship_types: list[str]) -> None:
    if not ship_types:
        return
    available_types = {
        option["ship_type"]
        for option in available_next_ship_types()
        if option["idle_count"] or option["train_count"]
    }
    unavailable_types = [
        value for value in ship_types if value not in available_types
    ]
    if unavailable_types:
        raise ValueError(
            "舰种不存在或已无可练级舰娘：" + "、".join(unavailable_types)
        )


def _sort_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        candidates,
        key=lambda ship: (
            tech_priority_rank(ship),
            rarity_rank(ship),
            ship["name"],
        ),
    )


def next_ship(
    mode: str,
    ship_type: str = "",
    ship_types: list[str] | None = None,
) -> dict[str, Any] | None:
    if mode not in {"idle", "train"}:
        raise ValueError("mode 必须是 idle 或 train")
    ship_types = _normalize_ship_types(ship_type, ship_types)
    _validate_ship_types(ship_types)
    filters: dict[str, Any] = {
        "ship_types": ship_types,
        "is_idling": "否" if mode == "idle" else "是",
        "is_trained": "否" if mode == "train" else "",
    }
    # 只要选中项里没有标签，就仍然要求“需要练级 = 是”；标签候选不限制。
    if not any(value in CUSTOM_SHIP_TAGS for value in ship_types):
        filters["needs_training"] = "是"
    candidates = list_ships(filters)
    if not candidates:
        return None
    return _sort_candidates(candidates)[0]


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
    ships = list_ships({"ship_ids": ship_ids, "ship_types": [ship_type]})
    by_id = {ship["id"]: ship for ship in ships}
    return [
        by_id[ship_id]
        for ship_id in ship_ids
        if ship_id in by_id and not _is_completed(by_id[ship_id], field)
    ]


def _mode_filters(ship_type: str, mode: str) -> dict[str, Any]:
    """推荐候选的筛选条件。

    舰种候选要求“需要练级 = 是”；标签与是否需要练级正交（小船本身就是无科技点），
    因此标签候选不能带该条件，否则结果恒为空。
    """
    filters: dict[str, Any] = {
        "ship_types": [ship_type],
        "is_idling": "否" if mode == "idle" else "是",
        "is_trained": "否" if mode == "train" else "",
    }
    if ship_type not in CUSTOM_SHIP_TAGS:
        filters["needs_training"] = "是"
    return filters


def _auto_target_ships(
    ship_type: str,
    mode: str,
    limit: int,
    exclude_ids: set[int],
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    filters = _mode_filters(ship_type, mode)
    candidates = [
        ship for ship in list_ships(filters) if ship["id"] not in exclude_ids
    ]
    return _sort_candidates(candidates)[:limit]


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
    if mode not in {"both", "idle", "train"}:
        raise ValueError("mode 必须是 both、idle 或 train")
    ship_types = _normalize_ship_types("", ship_types)
    # 分组推荐允许传入暂无可练级舰娘的舰种：这类行会在下面被过滤掉。
    manual_targets = manual_targets or {}
    target_limits = target_limits or {}
    pending_counts = _pending_ship_type_counts()
    rows: list[dict[str, Any]] = []
    for ship_type in ship_types:
        type_targets = manual_targets.get(ship_type, {})
        type_limits = target_limits.get(ship_type, {})
        type_counts = pending_counts.get(ship_type, {})
        row: dict[str, Any] = {
            "ship_type": ship_type,
            # 该舰种当前待挂机、待练级的舰船数量，与舰种徽标口径一致。
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
    rows.sort(key=lambda row: recommend_ship_type_sort_key(row["ship_type"]))
    return rows
