from __future__ import annotations

import re


def attribute_from_text(value: str) -> str:
    match = re.search(r"(装填|命中|炮击|防空|耐久|航空|雷击|机动)", value)
    return match.group(1) if match else ""


def attribute_bonus_from_text(value: str) -> str:
    return re.sub(r"\s*\+\s*\d+\s*$", "", value.strip())


def parse_tech_attribute(value: str) -> tuple[list[str], str]:
    text = value.strip()
    type_part, separator, attribute_part = text.rpartition(" ")
    if not separator:
        return [], attribute_from_text(text)
    ship_types = [item.strip() for item in type_part.split("/") if item.strip()]
    return ship_types, attribute_from_text(attribute_part)


def tech_attribute_matches(
    attribute: str,
    ship_type: str,
    tech_attribute: str,
    fallback_ship_type: str = "",
) -> bool:
    attribute_ship_types, attribute_name = parse_tech_attribute(attribute)
    if not attribute_ship_types and fallback_ship_type:
        attribute_ship_types = [fallback_ship_type]
    return ship_type in attribute_ship_types and attribute_name == tech_attribute

