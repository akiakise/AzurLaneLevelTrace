from __future__ import annotations

import re

from ..core.constants import normalize_ship_type

BONUS_ATTRIBUTE_PATTERN = (
    r"(反潜|装填|命中|炮击|防空|耐久|航空|雷击|机动)"
)
BONUS_VALUE_PATTERN = r"\s*\+\s*\d+\s*$"


def attribute_from_text(value: str) -> str:
    match = re.search(BONUS_ATTRIBUTE_PATTERN, value)
    return match.group(1) if match else ""


def attribute_bonus_from_text(value: str) -> str:
    return re.sub(BONUS_VALUE_PATTERN, "", value.strip())


def split_bonus_ship_types(value: str) -> list[str]:
    ship_types: list[str] = []
    for item in value.split("/"):
        ship_type = normalize_ship_type(item)
        if ship_type and ship_type not in ship_types:
            ship_types.append(ship_type)
    return ship_types


def parse_tech_attribute(value: str) -> tuple[list[str], str]:
    text = value.strip()
    type_part, separator, attribute_part = text.rpartition(" ")
    if not separator:
        return [], attribute_from_text(text)
    return split_bonus_ship_types(type_part), attribute_from_text(attribute_part)
