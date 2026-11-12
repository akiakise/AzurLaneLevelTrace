from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from ..core.constants import (
    ARMAMENT_GROUP_ORDER,
    ARMAMENT_MANUAL_FIELDS,
    CONFIG_PATH,
    LOGGER,
    bonus_ship_type_sort_key,
    normalize_ship_type,
)
from .db import get_connection


MANUAL_TARGET_FIELDS = ("is_idling", "is_trained")
DEFAULT_TARGET_LIMIT = 1
MAX_TARGET_LIMIT = 10
CONFIG_LOCK = threading.RLock()


@contextmanager
def config_transaction() -> Iterator[None]:
    """串行化 config.json 的读取-修改-写入，避免并发互相覆盖。"""
    with CONFIG_LOCK:
        yield


def normalize_target_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_TARGET_LIMIT
    return max(1, min(value, MAX_TARGET_LIMIT))


def _normalize_ship_type_list(value: Any) -> list[str]:
    """规范化推荐舰种（加成舰种）列表：展开别名、去重，并按展示顺序排列。"""
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        return []
    ship_types: list[str] = []
    for item in value:
        ship_type = normalize_ship_type(item)
        if ship_type and ship_type not in ship_types:
            ship_types.append(ship_type)
    return sorted(ship_types, key=bonus_ship_type_sort_key)


def _normalize_ship_ids(value: Any) -> list[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list):
        return []
    ship_ids: list[int] = []
    for item in value:
        if isinstance(item, int) and not isinstance(item, bool) and item > 0:
            if item not in ship_ids:
                ship_ids.append(item)
    return ship_ids


def _normalize_target_limits(
    value: Any,
    ship_types: list[str],
) -> dict[str, dict[str, int]]:
    source = value if isinstance(value, dict) else {}
    limits: dict[str, dict[str, int]] = {}
    for key in ship_types:
        raw = source.get(key)
        raw = raw if isinstance(raw, dict) else {}
        limits[key] = {
            field: normalize_target_limit(raw.get(field))
            for field in MANUAL_TARGET_FIELDS
        }
    return limits


def _normalize_manual_targets(
    value: Any,
    target_limits: dict[str, dict[str, int]],
) -> dict[str, dict[str, list[int]]]:
    if not isinstance(value, dict):
        return {}
    manual_targets: dict[str, dict[str, list[int]]] = {}
    for raw_key, targets in value.items():
        if not isinstance(raw_key, str) or not isinstance(targets, dict):
            continue
        key = normalize_ship_type(raw_key)
        if not key or key not in target_limits:
            continue
        normalized: dict[str, list[int]] = {}
        for field in MANUAL_TARGET_FIELDS:
            ship_ids = _normalize_ship_ids(targets.get(field))
            ship_ids = ship_ids[: target_limits[key][field]]
            if ship_ids:
                normalized[field] = ship_ids
        if normalized:
            manual_targets[key] = normalized
    return manual_targets


def empty_config() -> dict[str, Any]:
    return {
        "next_ship_types": [],
        "target_limits": {},
        "manual_targets": {},
        "armament_target_limits": _normalize_armament_target_limits({}),
        "armament_manual_targets": {},
    }


def _normalize_armament_target_limits(value: Any) -> dict[str, dict[str, int]]:
    source = value if isinstance(value, dict) else {}
    limits: dict[str, dict[str, int]] = {}
    for group in ARMAMENT_GROUP_ORDER:
        raw = source.get(group)
        raw = raw if isinstance(raw, dict) else {}
        limits[group] = {
            field: normalize_target_limit(raw.get(field))
            for field in ARMAMENT_MANUAL_FIELDS
        }
    return limits


def _normalize_armament_manual_targets(
    value: Any,
    target_limits: dict[str, dict[str, int]],
) -> dict[str, dict[str, list[int]]]:
    if not isinstance(value, dict):
        return {}
    manual_targets: dict[str, dict[str, list[int]]] = {}
    for group, targets in value.items():
        if group not in target_limits or not isinstance(targets, dict):
            continue
        normalized: dict[str, list[int]] = {}
        for field in ARMAMENT_MANUAL_FIELDS:
            armament_ids = _normalize_ship_ids(targets.get(field))
            armament_ids = armament_ids[: target_limits[group][field]]
            if armament_ids:
                normalized[field] = armament_ids
        if normalized:
            manual_targets[group] = normalized
    return manual_targets


def read_config() -> dict[str, Any]:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return empty_config()
    except (OSError, json.JSONDecodeError) as error:
        LOGGER.warning("读取配置文件失败，使用默认配置: %s", error)
        return empty_config()
    if not isinstance(payload, dict):
        LOGGER.warning("配置文件格式无效，使用默认配置")
        return empty_config()
    ship_types = _normalize_ship_type_list(payload.get("next_ship_types", []))
    target_limits = _normalize_target_limits(
        payload.get("target_limits", {}), ship_types
    )
    armament_target_limits = _normalize_armament_target_limits(
        payload.get("armament_target_limits", {})
    )
    return {
        "next_ship_types": ship_types,
        "target_limits": target_limits,
        "manual_targets": _normalize_manual_targets(
            payload.get("manual_targets", {}), target_limits
        ),
        "armament_target_limits": armament_target_limits,
        "armament_manual_targets": _normalize_armament_manual_targets(
            payload.get("armament_manual_targets", {}), armament_target_limits
        ),
    }


def _drop_missing_ship_ids(
    manual_targets: dict[str, dict[str, list[int]]],
) -> dict[str, dict[str, list[int]]]:
    ship_ids = {
        ship_id
        for targets in manual_targets.values()
        for ids in targets.values()
        for ship_id in ids
    }
    if not ship_ids:
        return manual_targets
    from ..domain.ships import find_existing_ship_ids

    existing = find_existing_ship_ids(ship_ids)
    cleaned: dict[str, dict[str, list[int]]] = {}
    for key, targets in manual_targets.items():
        kept: dict[str, list[int]] = {}
        for field, ids in targets.items():
            kept_ids = [ship_id for ship_id in ids if ship_id in existing]
            if kept_ids:
                kept[field] = kept_ids
        if kept:
            cleaned[key] = kept
    return cleaned


def _drop_missing_armament_ids(
    manual_targets: dict[str, dict[str, list[int]]],
) -> dict[str, dict[str, list[int]]]:
    armament_ids = {
        armament_id
        for targets in manual_targets.values()
        for ids in targets.values()
        for armament_id in ids
    }
    if not armament_ids:
        return manual_targets
    placeholders = ",".join("?" for _ in armament_ids)
    with get_connection() as connection:
        rows = connection.execute(
            f"SELECT id FROM armaments WHERE id IN ({placeholders})",
            list(armament_ids),
        ).fetchall()
    existing = {row["id"] for row in rows}
    cleaned: dict[str, dict[str, list[int]]] = {}
    for group, targets in manual_targets.items():
        kept: dict[str, list[int]] = {}
        for field, ids in targets.items():
            kept_ids = [value for value in ids if value in existing]
            if kept_ids:
                kept[field] = kept_ids
        if kept:
            cleaned[group] = kept
    return cleaned


def write_config(config: dict[str, Any]) -> None:
    ship_types = _normalize_ship_type_list(config.get("next_ship_types", []))
    target_limits = _normalize_target_limits(
        config.get("target_limits", {}), ship_types
    )
    armament_target_limits = _normalize_armament_target_limits(
        config.get("armament_target_limits", {})
    )
    payload = {
        "next_ship_types": ship_types,
        "target_limits": target_limits,
        "manual_targets": _drop_missing_ship_ids(
            _normalize_manual_targets(
                config.get("manual_targets", {}), target_limits
            )
        ),
        "armament_target_limits": armament_target_limits,
        "armament_manual_targets": _drop_missing_armament_ids(
            _normalize_armament_manual_targets(
                config.get("armament_manual_targets", {}), armament_target_limits
            )
        ),
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = CONFIG_PATH.with_suffix(
        f"{CONFIG_PATH.suffix}.{threading.get_ident()}.tmp"
    )
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(CONFIG_PATH)


def remove_config() -> None:
    try:
        CONFIG_PATH.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        LOGGER.error("删除配置文件失败: %s", error)
        raise ValueError(f"删除推荐配置失败：{error}") from error


def save_next_ship_types(ship_types: list[str]) -> list[str]:
    with config_transaction():
        return _save_next_ship_types(ship_types)


def _save_next_ship_types(ship_types: list[str]) -> list[str]:
    from ..domain.recommendations import validate_ship_types

    if not isinstance(ship_types, list) or not all(
        isinstance(value, str) for value in ship_types
    ):
        raise ValueError("next_ship_types 必须是加成舰种或自定义标签列表")
    keys = validate_ship_types(ship_types)
    current = read_config()
    write_config(
        {
            "next_ship_types": keys,
            "target_limits": current["target_limits"],
            "manual_targets": current["manual_targets"],
            "armament_target_limits": current["armament_target_limits"],
            "armament_manual_targets": current["armament_manual_targets"],
        }
    )
    return keys


def _require_selected_ship_type(config: dict[str, Any], ship_type: str) -> str:
    """校验加成舰种已加入推荐配置，并返回规范化后的舰种。"""
    key = normalize_ship_type(str(ship_type or "").strip())
    if not key:
        raise ValueError("ship_type 不能为空")
    if key not in config["next_ship_types"]:
        raise ValueError(f"舰种「{key}」不在推荐配置中")
    return key


def _require_field(field: str) -> str:
    if field not in MANUAL_TARGET_FIELDS:
        raise ValueError("field 只支持 is_idling 或 is_trained")
    return field


def save_target_limit(key: str, field: str, limit: Any) -> dict[str, Any]:
    with config_transaction():
        return _save_target_limit(key, field, limit)


def _save_target_limit(key: str, field: str, limit: Any) -> dict[str, Any]:
    config = read_config()
    key = _require_selected_ship_type(config, key)
    field = _require_field(field)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit 必须是 1 到 10 之间的整数")
    if not 1 <= limit <= MAX_TARGET_LIMIT:
        raise ValueError(f"limit 必须是 1 到 {MAX_TARGET_LIMIT} 之间的整数")
    target_limits = {
        item: dict(value) for item, value in config["target_limits"].items()
    }
    target_limits[key][field] = limit
    manual_targets = {
        item: {name: list(ids) for name, ids in value.items()}
        for item, value in config["manual_targets"].items()
    }
    targets = manual_targets.get(key, {})
    if len(targets.get(field, [])) > limit:
        targets[field] = targets[field][:limit]
        if not targets[field]:
            targets.pop(field, None)
        if targets:
            manual_targets[key] = targets
        else:
            manual_targets.pop(key, None)
    write_config(
        {
            "next_ship_types": config["next_ship_types"],
            "target_limits": target_limits,
            "manual_targets": manual_targets,
            "armament_target_limits": config["armament_target_limits"],
            "armament_manual_targets": config["armament_manual_targets"],
        }
    )
    return read_config()


def _active_manual_ids(key: str, field: str, ship_ids: list[int]) -> list[int]:
    if not ship_ids:
        return []
    from ..domain.recommendations import ship_bonus_ship_types
    from ..domain.ships import list_ships

    rows = list_ships({"ship_ids": ship_ids})
    by_id = {row["id"]: row for row in rows}
    return [
        ship_id
        for ship_id in ship_ids
        if ship_id in by_id
        and key in ship_bonus_ship_types(by_id[ship_id])
        and by_id[ship_id][field] is not True
    ]


def toggle_manual_target(
    key: str,
    field: str,
    ship_id: Any,
) -> dict[str, Any]:
    """人工录入/取消某科技点行某模式下的舰娘；已存在则移除。"""
    with config_transaction():
        return _toggle_manual_target(key, field, ship_id)


def _toggle_manual_target(
    key: str,
    field: str,
    ship_id: Any,
) -> dict[str, Any]:
    config = read_config()
    key = _require_selected_ship_type(config, key)
    field = _require_field(field)
    limit = config["target_limits"][key][field]
    manual_targets = {
        item: {name: list(ids) for name, ids in value.items()}
        for item, value in config["manual_targets"].items()
    }
    targets = manual_targets.setdefault(key, {})
    ship_ids = targets.setdefault(field, [])
    if ship_id is None:
        ship_ids.clear()
    else:
        if isinstance(ship_id, bool) or not isinstance(ship_id, int) or ship_id <= 0:
            raise ValueError("ship_id 必须是正整数")
        from ..domain.recommendations import ship_bonus_ship_types
        from ..domain.ships import find_ship_brief

        ship = find_ship_brief(ship_id)
        if ship is None:
            raise ValueError("舰船不存在")
        full_ship = _ship_with_tech_points(ship_id)
        if full_ship is None or key not in ship_bonus_ship_types(full_ship):
            raise ValueError(f"舰船「{ship['name']}」没有「{key}」加成")
        if ship_id in ship_ids:
            ship_ids.remove(ship_id)
        else:
            if ship[field] is True:
                raise ValueError(f"舰船「{ship['name']}」已完成，不能录入")
            # 只统计仍存在且未完成的有效录入，避免悬空/已完成项占满名额。
            active_ids = _active_manual_ids(key, field, ship_ids)
            if len(active_ids) >= limit:
                raise ValueError(f"「{key}」最多录入 {limit} 个，请先取消已有录入")
            ship_ids[:] = active_ids
            ship_ids.append(ship_id)
    if not ship_ids:
        targets.pop(field, None)
    if not targets:
        manual_targets.pop(key, None)
    write_config(
        {
            "next_ship_types": config["next_ship_types"],
            "target_limits": config["target_limits"],
            "manual_targets": manual_targets,
            "armament_target_limits": config["armament_target_limits"],
            "armament_manual_targets": config["armament_manual_targets"],
        }
    )
    return read_config()


def _ship_with_tech_points(ship_id: int) -> dict[str, Any] | None:
    from ..domain.ships import list_ships

    rows = list_ships({"ship_ids": [ship_id]})
    return rows[0] if rows else None


def _require_armament_group(config: dict[str, Any], group: str) -> str:
    group = str(group or "").strip()
    if group not in ARMAMENT_GROUP_ORDER:
        raise ValueError("兵装分组只支持 collab 或 normal")
    return group


def _require_armament_field(field: str) -> str:
    if field not in ARMAMENT_MANUAL_FIELDS:
        raise ValueError("field 只支持 is_built 或 is_enhanced")
    return field


def save_armament_target_limit(
    group: str, field: str, limit: Any
) -> dict[str, Any]:
    with config_transaction():
        return _save_armament_target_limit(group, field, limit)


def _save_armament_target_limit(
    group: str, field: str, limit: Any
) -> dict[str, Any]:
    config = read_config()
    group = _require_armament_group(config, group)
    field = _require_armament_field(field)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit 必须是 1 到 10 之间的整数")
    if not 1 <= limit <= MAX_TARGET_LIMIT:
        raise ValueError(f"limit 必须是 1 到 {MAX_TARGET_LIMIT} 之间的整数")
    target_limits = {
        key: dict(value) for key, value in config["armament_target_limits"].items()
    }
    target_limits[group][field] = limit
    manual_targets = {
        key: {name: list(ids) for name, ids in value.items()}
        for key, value in config["armament_manual_targets"].items()
    }
    targets = manual_targets.get(group, {})
    if len(targets.get(field, [])) > limit:
        targets[field] = targets[field][:limit]
        if not targets[field]:
            targets.pop(field, None)
        if targets:
            manual_targets[group] = targets
        else:
            manual_targets.pop(group, None)
    write_config(
        {
            "next_ship_types": config["next_ship_types"],
            "target_limits": config["target_limits"],
            "manual_targets": config["manual_targets"],
            "armament_target_limits": target_limits,
            "armament_manual_targets": manual_targets,
        }
    )
    return read_config()


def _active_armament_ids(
    group: str, field: str, armament_ids: list[int]
) -> list[int]:
    if not armament_ids:
        return []
    from ..domain.armaments import list_armaments

    rows = list_armaments({"group": group, "armament_ids": armament_ids})
    by_id = {row["id"]: row for row in rows}
    return [
        armament_id
        for armament_id in armament_ids
        if armament_id in by_id and by_id[armament_id][field] is not True
    ]


def toggle_armament_manual_target(
    group: str,
    field: str,
    armament_id: Any,
) -> dict[str, Any]:
    """人工录入/取消某兵装分组某状态下的兵装；已存在则移除。"""
    with config_transaction():
        return _toggle_armament_manual_target(group, field, armament_id)


def _toggle_armament_manual_target(
    group: str,
    field: str,
    armament_id: Any,
) -> dict[str, Any]:
    config = read_config()
    group = _require_armament_group(config, group)
    field = _require_armament_field(field)
    limit = config["armament_target_limits"][group][field]
    manual_targets = {
        key: {name: list(ids) for name, ids in value.items()}
        for key, value in config["armament_manual_targets"].items()
    }
    targets = manual_targets.setdefault(group, {})
    armament_ids = targets.setdefault(field, [])
    if armament_id is None:
        armament_ids.clear()
    else:
        if (
            isinstance(armament_id, bool)
            or not isinstance(armament_id, int)
            or armament_id <= 0
        ):
            raise ValueError("armament_id 必须是正整数")
        from ..domain.armaments import find_armament_brief

        armament = find_armament_brief(armament_id)
        if armament is None:
            raise ValueError("兵装不存在")
        if armament["group"] != group:
            raise ValueError(
                f"兵装「{armament['name']}」不属于分组「{group}」"
            )
        if armament_id in armament_ids:
            armament_ids.remove(armament_id)
        else:
            if armament[field] is True:
                raise ValueError(f"兵装「{armament['name']}」已完成，不能录入")
            # 只统计仍存在且未完成的有效录入，避免悬空/已完成项占满名额。
            active_ids = _active_armament_ids(group, field, armament_ids)
            if len(active_ids) >= limit:
                raise ValueError(
                    f"该兵装分组最多录入 {limit} 个，请先取消已有录入"
                )
            armament_ids[:] = active_ids
            armament_ids.append(armament_id)
    if not armament_ids:
        targets.pop(field, None)
    if not targets:
        manual_targets.pop(group, None)
    write_config(
        {
            "next_ship_types": config["next_ship_types"],
            "target_limits": config["target_limits"],
            "manual_targets": config["manual_targets"],
            "armament_target_limits": config["armament_target_limits"],
            "armament_manual_targets": manual_targets,
        }
    )
    return read_config()
