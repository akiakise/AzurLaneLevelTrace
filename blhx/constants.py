from __future__ import annotations

import logging
import os
import threading
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATABASE_PATH = DATA_DIR / "blhx-training.sqlite3"
CONFIG_PATH = DATA_DIR / "config.json"
WIKI_BASE_URL = os.getenv("WIKI_BASE_URL", "https://wiki.biligame.com").rstrip("/")
WIKI_API_PATH = "/blhx/api.php"
WIKI_REQUEST_INTERVAL = float(os.getenv("WIKI_REQUEST_INTERVAL", "0.12"))
WIKI_CONCURRENCY = max(1, int(os.getenv("WIKI_CONCURRENCY", "4")))
WIKI_RETRY_COUNT = max(1, int(os.getenv("WIKI_RETRY_COUNT", "4")))
WIKI_RETRY_DELAY = max(0.0, float(os.getenv("WIKI_RETRY_DELAY", "1.5")))
WIKI_REQUEST_LOCK = threading.Lock()
WIKI_NEXT_REQUEST_AT = 0.0
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("blhx_training_tracker")

RARITY_ORDER = {
    "海上传奇": 0,
    "决战方案": 1,
    "普通": 2,
    "稀有": 3,
    "精锐": 4,
    "超稀有": 5,
    "最高方案": 6,
}

TECH_PRIORITY_BY_TYPE = {
    "战列": ["装填", "命中", "炮击", "防空", "耐久"],
    "航母": ["装填", "命中", "航空", "防空"],
    "重巡": ["机动", "炮击", "装填", "命中", "雷击", "防空", "耐久"],
    "轻巡": ["机动", "炮击", "命中", "装填", "雷击", "防空", "耐久"],
    "驱逐": ["机动", "炮击", "装填", "雷击", "命中"],
    "潜艇": ["雷击"],
}

DEFAULT_TECH_PRIORITY = [
    "装填",
    "命中",
    "炮击",
    "防空",
    "耐久",
    "航空",
    "雷击",
    "机动",
]

TECH_STAGE_FIELDS = {
    "解锁图鉴": "解锁图鉴属性加成",
    "达到120级": "达到120级属性加成",
}

