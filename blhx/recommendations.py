from __future__ import annotations

from typing import Any

from .constants import (
    DEFAULT_TECH_PRIORITY,
    normalize_ship_type,
    RARITY_PRIORITY_ORDER,
    TECH_PRIORITY_BY_TYPE,
    recommend_ship_type_sort_key,
)
from .db import get_connection
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


def available_next_ship_types() -> list[dict[str, Any]]:
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
    return sorted(
        (
            {
                "ship_type": row["ship_type"],
                "idle_count": row["idle_count"],
                "train_count": row["train_count"],
            }
            for row in rows
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


def _validate_ship_types(ship_types: list[str]) -> None:
    if not ship_types:
        return
    available_types = {
        option["ship_type"] for option in available_next_ship_types()
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
    filters = {
        "ship_types": ship_types,
        "needs_training": "是",
        "is_idling": "否" if mode == "idle" else "是",
        "is_trained": "否" if mode == "train" else "",
    }
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


def _auto_target_ships(
    ship_type: str,
    mode: str,
    limit: int,
    exclude_ids: set[int],
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    filters = {
        "ship_types": [ship_type],
        "needs_training": "是",
        "is_idling": "否" if mode == "idle" else "是",
        "is_trained": "否" if mode == "train" else "",
    }
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
    rows: list[dict[str, Any]] = []
    for ship_type in ship_types:
        type_targets = manual_targets.get(ship_type, {})
        type_limits = target_limits.get(ship_type, {})
        row: dict[str, Any] = {"ship_type": ship_type}
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
