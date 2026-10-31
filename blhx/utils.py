from __future__ import annotations

import re
from datetime import datetime, timezone


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def clean_template_value(value: str) -> str:
    return re.sub(r"\\?<!--.*?-->", "", value, flags=re.DOTALL).strip()

