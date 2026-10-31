from __future__ import annotations

import json
from typing import Any

from .constants import CONFIG_PATH, LOGGER


def read_config() -> dict[str, Any]:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"next_ship_types": []}
    except (OSError, json.JSONDecodeError) as error:
        LOGGER.warning("读取配置文件失败，使用默认配置: %s", error)
        return {"next_ship_types": []}
    if not isinstance(payload, dict):
        LOGGER.warning("配置文件格式无效，使用默认配置")
        return {"next_ship_types": []}
    ship_types = payload.get("next_ship_types", [])
    if not isinstance(ship_types, list) or not all(
        isinstance(value, str) for value in ship_types
    ):
        LOGGER.warning("推荐舰种配置格式无效，使用默认配置")
        return {"next_ship_types": []}
    return {
        "next_ship_types": list(
            dict.fromkeys(value.strip() for value in ship_types if value.strip())
        )
    }


def write_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(CONFIG_PATH)


def save_next_ship_types(ship_types: list[str]) -> list[str]:
    from .recommendations import available_next_ship_types

    if not isinstance(ship_types, list) or not all(
        isinstance(value, str) for value in ship_types
    ):
        raise ValueError("next_ship_types 必须是舰种列表")
    ship_types = list(
        dict.fromkeys(value.strip() for value in ship_types if value.strip())
    )
    if ship_types:
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
    write_config({"next_ship_types": ship_types})
    return ship_types
