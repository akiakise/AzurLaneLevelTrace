from __future__ import annotations

from typing import Any

from .constants import (
    DEFAULT_TECH_PRIORITY,
    RARITY_ORDER,
    TECH_PRIORITY_BY_TYPE,
)
from .db import get_connection
from .ships import list_ships
from .tech import attribute_from_text


def rarity_rank(ship: dict[str, Any]) -> int:
    return RARITY_ORDER.get(ship["rarity"], len(RARITY_ORDER))


def tech_priority_rank(ship: dict[str, Any]) -> int:
    priority = TECH_PRIORITY_BY_TYPE.get(
        ship["ship_type"], DEFAULT_TECH_PRIORITY
    )
    ranks = []
    for tech_point in ship["tech_points"]:
        attribute = attribute_from_text(tech_point["attribute"]) or "其他"
        ranks.append(priority.index(attribute) if attribute in priority else len(priority))
    return min(ranks) if ranks else len(priority) + 1


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
            ORDER BY ship_type
            """
        ).fetchall()
    return [
        {
            "ship_type": row["ship_type"],
            "idle_count": row["idle_count"],
            "train_count": row["train_count"],
        }
        for row in rows
    ]


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
    return list(dict.fromkeys(value.strip() for value in ship_types if value.strip()))


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
    candidates.sort(
        key=lambda ship: (
            tech_priority_rank(ship),
            rarity_rank(ship),
            ship["name"],
        )
    )
    return candidates[0]


def next_ships_by_type(
    mode: str,
    ship_types: list[str],
) -> list[dict[str, Any]]:
    if mode not in {"both", "idle", "train"}:
        raise ValueError("mode 必须是 both、idle 或 train")
    ship_types = _normalize_ship_types("", ship_types)
    _validate_ship_types(ship_types)
    rows: list[dict[str, Any]] = []
    for ship_type in ship_types:
        idle = (
            _best_ship_for_type("idle", ship_type)
            if mode in {"both", "idle"}
            else None
        )
        train = (
            _best_ship_for_type("train", ship_type)
            if mode in {"both", "train"}
            else None
        )
        rows.append(
            {
                "ship_type": ship_type,
                "idle": idle,
                "train": train,
            }
        )
    return rows


def _best_ship_for_type(mode: str, ship_type: str) -> dict[str, Any] | None:
    filters = {
        "ship_types": [ship_type],
        "needs_training": "是",
        "is_idling": "否" if mode == "idle" else "是",
        "is_trained": "否" if mode == "train" else "",
    }
    candidates = list_ships(filters)
    if not candidates:
        return None
    candidates.sort(
        key=lambda ship: (
            tech_priority_rank(ship),
            rarity_rank(ship),
            ship["name"],
        )
    )
    return candidates[0]
