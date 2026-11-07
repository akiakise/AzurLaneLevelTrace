from __future__ import annotations

import threading
from typing import Any


SYNC_STATE: dict[str, Any] = {
    "running": False,
    "status": "未开始",
    "message": "",
    "total": 0,
    "processed": 0,
    "skipped": 0,
    "armaments": 0,
    "errors": [],
}
SYNC_LOCK = threading.Lock()

IMPORT_STATE: dict[str, Any] = {
    "running": False,
    "status": "未开始",
    "message": "",
    "total": 0,
    "processed": 0,
    "skipped": 0,
    "errors": [],
    "result": b"",
}
IMPORT_LOCK = threading.Lock()
