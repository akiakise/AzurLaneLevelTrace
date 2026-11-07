from __future__ import annotations

import logging
import os
import threading
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent.parent
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

SHIP_TYPE_ALIASES = {
    "风帆M": "风帆",
    "风帆S": "风帆",
    "风帆V": "风帆",
}

SHIP_TYPE_ORDER = [
    "驱逐",
    "轻巡",
    "重巡",
    "超巡",
    "战巡",
    "战列",
    "航母",
    "轻航",
    "重炮",
    "维修",
    "航战",
    "潜艇",
    "潜母",
    "运输",
    "风帆",
]

RARITY_FILTER_ORDER = [
    "普通",
    "稀有",
    "精锐",
    "超稀有",
    "海上传奇",
    "最高方案",
    "决战方案",
]

RARITY_PRIORITY_ORDER = {
    "海上传奇": 0,
    "决战方案": 1,
    "普通": 2,
    "稀有": 3,
    "精锐": 4,
    "超稀有": 5,
    "最高方案": 6,
}

RECOMMEND_SHIP_TYPE_ORDER = [
    "战列",
    "战巡",
    "航母",
    "轻航",
    "重巡",
    "轻巡",
    "驱逐",
]

BONUS_SHIP_TYPE_ORDER = [
    "驱逐",
    "轻巡",
    "重巡",
    "超巡",
    "战巡",
    "战列",
    "正航",
    "轻航",
    "重炮",
    "维修",
    "航战",
    "潜艇",
    "潜母",
    "运输",
    "风帆",
]

BONUS_ATTRIBUTE_ORDER = [
    "耐久",
    "炮击",
    "雷击",
    "防空",
    "航空",
    "装填",
    "机动",
    "命中",
    "反潜",
]

TECH_PRIORITY_BY_TYPE = {
    "战列": ["装填", "命中", "炮击", "防空", "耐久"],
    "轻航": ["装填", "命中", "航空", "防空"],
    "正航": ["装填", "命中", "航空", "防空"],
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
    "反潜",
]

TECH_STAGE_FIELDS = {
    "解锁图鉴": "解锁图鉴属性加成",
    "达到120级": "达到120级属性加成",
}

# 兵装分组：有具体实装日期的为常规兵装，无日期的为活动赠送的联动兵装。
ARMAMENT_GROUP_ORDER = ["collab", "normal"]
# 推荐结果里常规兵装优先展示，与筛选项“是（联动）/ 否（常规）”的顺序刻意分开。
ARMAMENT_RECOMMEND_GROUP_ORDER = ["normal", "collab"]
ARMAMENT_GROUP_LABELS = {"collab": "联动兵装", "normal": "常规兵装"}
# 分组对应的 armaments.is_collab 取值，筛选用。
ARMAMENT_GROUP_FLAGS = {"collab": 1, "normal": 0}
# 兵装状态字段与展示名；强化完成会自动联动打造完成，顺序即按钮顺序。
ARMAMENT_STATUS_FIELDS = {"is_built": "打造", "is_enhanced": "强化"}
ARMAMENT_MANUAL_FIELDS = ("is_built", "is_enhanced")

SHIP_TYPE_RANK = {value: index for index, value in enumerate(SHIP_TYPE_ORDER)}
RARITY_FILTER_RANK = {value: index for index, value in enumerate(RARITY_FILTER_ORDER)}
BONUS_SHIP_TYPE_RANK = {value: index for index, value in enumerate(BONUS_SHIP_TYPE_ORDER)}
BONUS_ATTRIBUTE_RANK = {value: index for index, value in enumerate(BONUS_ATTRIBUTE_ORDER)}
TECH_STAGE_RANK = {value: index for index, value in enumerate(TECH_STAGE_FIELDS)}
# 舰种名称拼音，用于“其他按名称排序”，避免中文按码位排序失真。
SHIP_TYPE_PINYIN = {
    "驱逐": "quzhu",
    "轻巡": "qingxun",
    "重巡": "zhongxun",
    "超巡": "chaoxun",
    "战巡": "zhanxun",
    "战列": "zhanlie",
    "航母": "hangmu",
    "正航": "zhenghang",
    "轻航": "qinghang",
    "重炮": "zhongpao",
    "维修": "weixiu",
    "航战": "hangzhan",
    "潜艇": "qianting",
    "潜母": "qianmu",
    "运输": "yunshu",
    "风帆": "fengfan",
}

RECOMMEND_SHIP_TYPE_RANK = {
    value: index for index, value in enumerate(RECOMMEND_SHIP_TYPE_ORDER)
}



def normalize_ship_type(value: str) -> str:
    text = (value or "").strip()
    return SHIP_TYPE_ALIASES.get(text, text)


def ship_type_sort_key(value: str) -> tuple[int, str]:
    return (SHIP_TYPE_RANK.get(value, len(SHIP_TYPE_RANK)), value)


def rarity_filter_sort_key(value: str) -> tuple[int, str]:
    return (RARITY_FILTER_RANK.get(value, len(RARITY_FILTER_RANK)), value)


def recommend_ship_type_sort_key(value: str) -> tuple[int, str, str]:
    """推荐舰种展示顺序：固定优先项在前，其余按名称拼音排序。"""
    return (
        RECOMMEND_SHIP_TYPE_RANK.get(value, len(RECOMMEND_SHIP_TYPE_RANK)),
        SHIP_TYPE_PINYIN.get(value, value),
        value,
    )


def bonus_ship_type_sort_key(value: str) -> tuple[int, str]:
    return (BONUS_SHIP_TYPE_RANK.get(value, len(BONUS_SHIP_TYPE_RANK)), value)


def bonus_attribute_sort_key(value: str) -> tuple[int, str]:
    return (BONUS_ATTRIBUTE_RANK.get(value, len(BONUS_ATTRIBUTE_RANK)), value)


def tech_stage_sort_key(value: str) -> tuple[int, str]:
    return (TECH_STAGE_RANK.get(value, len(TECH_STAGE_RANK)), value)
