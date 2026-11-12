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

# 自定义标签：与舰种正交，一件舰船可以同时有自己的舰种和多个标签。
# 标签在数据同步时写入 ships.tags（`/` 分隔），不改写 ships.ship_type，
# 因此标签筛选不会影响原有舰种筛选，两者可以同时命中同一艘舰船。
SMALL_SHIP_TAG = "小船"
COLLAB_SHIP_TAG = "联动"
# 标签全集，顺序即筛选与推荐里的展示顺序。
CUSTOM_SHIP_TAGS = [SMALL_SHIP_TAG, COLLAB_SHIP_TAG]
# “小船”的判定规则：没有科技点（无需练级）且名称以“小”开头。
SMALL_SHIP_NAME_PREFIX = "小"
SMALL_SHIP_RULE = (
    "COALESCE({p}needs_training, 0) = 0"
    f" AND {{p}}name LIKE '{SMALL_SHIP_NAME_PREFIX}%'"
)
# 标签判定规则表（本地可推导型）：按 SQL 从舰船自身字段派生。
# 新增标签时在这里登记规则，或留空表示不由本地字段判定。
CUSTOM_SHIP_TAG_RULES: dict[str, str] = {
    SMALL_SHIP_TAG: SMALL_SHIP_RULE,
}
# 需要由同步流程提供名称集合的标签：成员来自 Wiki 分类，规则无法用本地字段表达。
# 同步时把抓到的名称集合交给 refresh_ship_tags() 一起写入 tags。
CATEGORY_SHIP_TAGS = [COLLAB_SHIP_TAG]


def ship_tag_condition(tag: str, alias: str = "s") -> str | None:
    """返回某标签的**计算规则** SQL 片段，仅用于同步时写入 ships.tags。

    alias 传空串可生成不带表前缀的列名，供 UPDATE 语句复用。
    没有规则的标签（如占位的“联动”）返回 None，写入时按不命中处理。

    注意：筛选不使用这里的规则，而是查 ships.tags（见 ship_tag_match_condition）。
    规则描述“这艘船该不该有该标签”，tags 列是同步产出的结果，两者分离，
    未重刷同步的存量数据不会因为本地字段碰巧满足规则而凭空出现标签。
    """
    rule = CUSTOM_SHIP_TAG_RULES.get(tag)
    if not rule:
        return None
    return rule.format(p=f"{alias}." if alias else "")


def ship_tag_match_condition(tag: str, alias: str = "s") -> str:
    """生成按 ships.tags 精确匹配某标签的 SQL 片段（`/` 分隔字段的成员判断）。

    用前后加 `/` 再 instr 匹配，避免“小船”误命中“小小船”这类子串情况。
    """
    return f"instr('/' || COALESCE({{p}}tags, '') || '/', '/{tag}/') > 0".format(
        p=f"{alias}." if alias else ""
    )


def ship_tag_select_conditions(alias: str = "s") -> list[tuple[str, str]]:
    """返回 [(标签, SQL 条件)]，按 CUSTOM_SHIP_TAGS 顺序，跳过尚无规则的占位标签。"""
    pairs: list[tuple[str, str]] = []
    for tag in CUSTOM_SHIP_TAGS:
        condition = ship_tag_condition(tag, alias)
        if condition:
            pairs.append((tag, condition))
    return pairs


def split_ship_tags(value: str) -> list[str]:
    """把 `小船/联动` 形式的文本拆成标签列表，保持 CUSTOM_SHIP_TAGS 的顺序。"""
    raw = {part.strip() for part in str(value or "").split("/") if part.strip()}
    ordered = [tag for tag in CUSTOM_SHIP_TAGS if tag in raw]
    ordered.extend(sorted(raw - set(CUSTOM_SHIP_TAGS)))
    return ordered


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

# 推荐目标以**加成舰种**为单位（`tech_points.bonus_ship_type`，如 正航/轻航/驱逐），
# 不是舰船自身的 `ships.ship_type`（航母）。
# 自定义标签（小船/联动）同样可以作为推荐目标，排在全部加成舰种之后。
RECOMMEND_TECH_SHIP_TYPES = list(BONUS_SHIP_TYPE_ORDER)
# 可配置的推荐目标全集：加成舰种 + 自定义标签。
RECOMMEND_TARGET_ORDER = [*RECOMMEND_TECH_SHIP_TYPES, *CUSTOM_SHIP_TAGS]


def recommend_target_sort_key(value: str) -> tuple[int, str]:
    """推荐目标（加成舰种 / 自定义标签）的展示顺序。

    加成舰种按 `BONUS_SHIP_TYPE_ORDER`；标签固定排在全部舰种之后，
    按 `CUSTOM_SHIP_TAGS` 的声明顺序。
    """
    if value in CUSTOM_SHIP_TAGS:
        return (
            len(RECOMMEND_TECH_SHIP_TYPES) + CUSTOM_SHIP_TAGS.index(value),
            value,
        )
    if value in BONUS_SHIP_TYPE_RANK:
        return (BONUS_SHIP_TYPE_RANK[value], value)
    return (len(RECOMMEND_TARGET_ORDER), value)


def tech_priority_order(ship_type: str) -> list[str]:
    """某加成舰种的属性优先级；没有独立表的舰种走 DEFAULT_TECH_PRIORITY。"""
    table = TECH_PRIORITY_BY_TYPE.get(ship_type)
    return table if table is not None else DEFAULT_TECH_PRIORITY


def ship_tech_priority_score(ship_type: str, attributes) -> int:
    """舰船在某加成舰种下的优先级得分，越小越优先。

    取该舰在该加成舰种下命中属性里最靠前的一条；一个都没命中返回表长（排在表尾）。
    `attributes` 是 iterable，元素为该舰在该加成舰种下的加成属性。
    """
    order = tech_priority_order(ship_type)
    ranks = [
        order.index(attribute) if attribute in order else len(order)
        for attribute in attributes
    ]
    return min(ranks) if ranks else len(order)


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
# 兵装推荐只看三个特殊稀有度，其余稀有度一视同仁按实装日期排序。
# 注意与舰船推荐的 RARITY_PRIORITY_ORDER 不同，不要复用。
ARMAMENT_RARITY_PRIORITY = {
    "海上传奇": 0,
    "决战方案": 1,
    "最高方案": 2,
}

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
    "小船": "xiaochuan",
    "联动": "liandong",
}

RECOMMEND_SHIP_TYPE_RANK = {
    value: index for index, value in enumerate(RECOMMEND_SHIP_TYPE_ORDER)
}
# 自定义标签不参与固定优先项，统一排在所有推荐舰种（含未列出的舰种）之后，
# 并按 CUSTOM_SHIP_TAGS 的声明顺序排列。
CUSTOM_RECOMMEND_SHIP_TYPE_RANK = {
    tag: len(RECOMMEND_SHIP_TYPE_ORDER) + len(SHIP_TYPE_ORDER) + index
    for index, tag in enumerate(CUSTOM_SHIP_TAGS)
}



def normalize_ship_type(value: str) -> str:
    text = (value or "").strip()
    return SHIP_TYPE_ALIASES.get(text, text)


def ship_type_sort_key(value: str) -> tuple[int, str]:
    return (SHIP_TYPE_RANK.get(value, len(SHIP_TYPE_RANK)), value)


def rarity_filter_sort_key(value: str) -> tuple[int, str]:
    return (RARITY_FILTER_RANK.get(value, len(RARITY_FILTER_RANK)), value)


def recommend_ship_type_sort_key(value: str) -> tuple[int, str, str]:
    """推荐舰种展示顺序：固定优先项在前，自定义标签最后，其余按名称拼音排序。"""
    if value in CUSTOM_RECOMMEND_SHIP_TYPE_RANK:
        return (
            CUSTOM_RECOMMEND_SHIP_TYPE_RANK[value],
            SHIP_TYPE_PINYIN.get(value, value),
            value,
        )
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
