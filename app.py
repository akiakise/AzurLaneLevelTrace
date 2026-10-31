from __future__ import annotations

import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, unquote, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATABASE_PATH = DATA_DIR / "blhx-training.sqlite3"
WIKI_BASE_URL = os.getenv("WIKI_BASE_URL", "https://wiki.biligame.com").rstrip("/")
WIKI_API_PATH = "/blhx/api.php"
WIKI_REQUEST_INTERVAL = float(os.getenv("WIKI_REQUEST_INTERVAL", "0.12"))
WIKI_CONCURRENCY = max(1, int(os.getenv("WIKI_CONCURRENCY", "4")))
WIKI_RETRY_COUNT = max(1, int(os.getenv("WIKI_RETRY_COUNT", "4")))
WIKI_RETRY_DELAY = max(0.0, float(os.getenv("WIKI_RETRY_DELAY", "1.5")))
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

SYNC_STATE: dict[str, Any] = {
    "running": False,
    "status": "未开始",
    "message": "",
    "total": 0,
    "processed": 0,
    "skipped": 0,
    "errors": [],
}
SYNC_LOCK = threading.Lock()
WIKI_REQUEST_LOCK = threading.Lock()
NEXT_WIKI_REQUEST_AT = 0.0


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_template_value(value: str) -> str:
    return re.sub(r"\\?<!--.*?-->", "", value, flags=re.DOTALL).strip()


@contextmanager
def get_connection():
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                ship_type TEXT NOT NULL DEFAULT '',
                rarity TEXT NOT NULL DEFAULT '',
                release_date TEXT NOT NULL DEFAULT '',
                needs_training INTEGER NOT NULL DEFAULT 0,
                is_idling INTEGER,
                is_trained INTEGER,
                wiki_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tech_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ship_id INTEGER NOT NULL REFERENCES ships(id) ON DELETE CASCADE,
                stage TEXT NOT NULL,
                attribute TEXT NOT NULL DEFAULT '',
                points INTEGER NOT NULL DEFAULT 0,
                UNIQUE(ship_id, stage, attribute)
            );

            CREATE INDEX IF NOT EXISTS idx_ships_type_status
                ON ships(ship_type, needs_training, is_idling, is_trained);
            CREATE INDEX IF NOT EXISTS idx_tech_points_ship
                ON tech_points(ship_id);

            DELETE FROM tech_points WHERE stage = '突破至满星';
            """
        )
        for row in connection.execute(
            "SELECT id, ship_type, rarity FROM ships"
        ).fetchall():
            ship_type = clean_template_value(row["ship_type"])
            rarity = clean_template_value(row["rarity"])
            if ship_type != row["ship_type"] or rarity != row["rarity"]:
                connection.execute(
                    "UPDATE ships SET ship_type = ?, rarity = ? WHERE id = ?",
                    (ship_type, rarity, row["id"]),
                )


def ship_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "ship_type": row["ship_type"],
        "rarity": row["rarity"],
        "release_date": row["release_date"],
        "needs_training": bool(row["needs_training"]),
        "is_idling": None if row["is_idling"] is None else bool(row["is_idling"]),
        "is_trained": None if row["is_trained"] is None else bool(row["is_trained"]),
        "wiki_url": row["wiki_url"],
        "tech_points": [],
    }


def status_text(value: bool | None) -> str:
    if value is None:
        return ""
    return "是" if value else "否"


def acquire_wiki_request_slot() -> None:
    global NEXT_WIKI_REQUEST_AT
    current_time = time.monotonic()
    with WIKI_REQUEST_LOCK:
        wait_seconds = max(0.0, NEXT_WIKI_REQUEST_AT - current_time)
        NEXT_WIKI_REQUEST_AT = max(current_time, NEXT_WIKI_REQUEST_AT) + WIKI_REQUEST_INTERVAL
    if wait_seconds:
        time.sleep(wait_seconds)


def insert_ship(
    connection: sqlite3.Connection,
    name: str,
    release_date: str,
    parsed_ship: dict[str, Any],
    overwrite: bool = False,
) -> sqlite3.Row:
    current_time = utc_now_text()
    wiki_url = f"{WIKI_BASE_URL}/blhx/{quote(name, safe='')}"
    needs_training = 1 if parsed_ship["tech_points"] else 0
    conflict_action = (
        """
        UPDATE SET
          ship_type = excluded.ship_type,
          rarity = excluded.rarity,
          release_date = excluded.release_date,
          needs_training = excluded.needs_training,
          wiki_url = excluded.wiki_url,
          updated_at = excluded.updated_at
        """
        if overwrite
        else "NOTHING"
    )
    connection.execute(
        f"""
        INSERT INTO ships (
            name, ship_type, rarity, release_date, needs_training,
            is_idling, is_trained, wiki_url, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
        ON CONFLICT(name) DO {conflict_action}
        """,
        (
            name,
            parsed_ship.get("ship_type", ""),
            parsed_ship.get("rarity", ""),
            release_date,
            needs_training,
            wiki_url,
            current_time,
            current_time,
        ),
    )
    ship = connection.execute(
        "SELECT * FROM ships WHERE name = ?", (name,)
    ).fetchone()
    if overwrite:
        connection.execute("DELETE FROM tech_points WHERE ship_id = ?", (ship["id"],))
    for tech_point in parsed_ship["tech_points"]:
        connection.execute(
            """
            INSERT OR IGNORE INTO tech_points (ship_id, stage, attribute, points)
            VALUES (?, ?, ?, ?)
            """,
            (
                ship["id"],
                tech_point["stage"],
                tech_point["attribute"],
                tech_point["points"],
            ),
        )
    return ship


class WikiTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[dict[str, Any]]]] = []
        self.table_stack: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "table":
            table: list[list[dict[str, Any]]] = []
            self.tables.append(table)
            self.table_stack.append({"rows": table, "row": None, "cell": None})
            return

        if not self.table_stack:
            return
        context = self.table_stack[-1]
        if tag == "tr" and context["row"] is None:
            context["row"] = []
        elif tag in {"td", "th"} and context["row"] is not None and context["cell"] is None:
            context["cell"] = {"text": "", "links": []}
        elif context["cell"] is not None:
            if tag == "a" and attributes.get("href"):
                context["cell"]["links"].append(attributes)

    def handle_data(self, data: str) -> None:
        if self.table_stack:
            cell = self.table_stack[-1]["cell"]
            if cell is not None:
                cell["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if not self.table_stack:
            return
        context = self.table_stack[-1]
        if tag in {"td", "th"} and context["cell"] is not None:
            context["row"].append(context["cell"])
            context["cell"] = None
        elif tag == "tr" and context["row"] is not None:
            if context["row"]:
                context["rows"].append(context["row"])
            context["row"] = None
        elif tag == "table":
            self.table_stack.pop()


def extract_ship_entries(html_text: str) -> list[dict[str, str]]:
    parser = WikiTableParser()
    parser.feed(html_text)
    entries_by_name: dict[str, dict[str, str]] = {}
    for table in parser.tables:
        for row in table:
            if len(row) < 3:
                continue
            date_text = row[2]["text"].strip()
            date_match = re.fullmatch(r"(\d{4})年(\d{2})月(\d{2})日", date_text)
            if not date_match:
                continue
            for link in row[1]["links"]:
                href = link.get("href", "")
                if not href.startswith("/blhx/"):
                    continue
                page_name = href[len("/blhx/") :].split("#", 1)[0].split("?", 1)[0]
                ship_name = unquote(page_name).strip()
                if not ship_name:
                    continue
                iso_date = "-".join(date_match.groups())
                entries_by_name[ship_name] = {"name": ship_name, "release_date": iso_date}
                break
    return list(entries_by_name.values())


def parse_template_fields(raw_text: str) -> dict[str, str]:
    start = raw_text.find("{{舰娘图鉴")
    if start < 0:
        return {}
    fields: dict[str, str] = {}
    for line in raw_text[start:].splitlines():
        line = line.strip()
        if line.startswith("}}"):
            break
        if not line.startswith("|"):
            continue
        key, separator, value = line[1:].partition("=")
        if separator:
            fields[key.strip()] = clean_template_value(value)
    return fields


def attribute_from_text(value: str) -> str:
    match = re.search(r"(装填|命中|炮击|防空|耐久|航空|雷击|机动)", value)
    return match.group(1) if match else ""


def attribute_bonus_from_text(value: str) -> str:
    return re.sub(r"\s*\+\s*\d+\s*$", "", value.strip())


def parse_ship_raw(raw_text: str) -> dict[str, Any] | None:
    fields = parse_template_fields(raw_text)
    if not fields:
        return None
    tech_points: list[dict[str, Any]] = []
    for stage, attribute_key in TECH_STAGE_FIELDS.items():
        attribute = attribute_bonus_from_text(fields.get(attribute_key, ""))
        if attribute:
            tech_points.append({"stage": stage, "attribute": attribute, "points": 0})
    return {
        "ship_type": fields.get("类型", ""),
        "rarity": fields.get("稀有度", ""),
        "tech_points": tech_points,
    }


def parse_wiki_challenge_cookie(body: str) -> str | None:
    if "__tst_status" not in body or "EO_Bot_Ssid" not in body:
        return None
    object_match = re.search(
        r"var\s+e\s*=\s*\{(.*?)\}\s*,\s*t\s*=\s*0\s*;", body, re.DOTALL
    )
    status_values = (
        re.findall(r":\s*(\d+)\s*(?:[,}]|$)", object_match.group(1))
        if object_match
        else []
    )
    ssid_match = re.search(
        r'EO_Bot_Ssid=";.*?\(\s*t,\s*(\d+)\s*\)', body, re.DOTALL
    )
    if len(status_values) < 3 or ssid_match is None:
        return None
    return (
        f"__tst_status={sum(map(int, status_values[:3]))}#; "
        f"EO_Bot_Ssid={ssid_match.group(1)}"
    )


def fetch_wiki_text(path: str) -> str:
    url = f"{WIKI_BASE_URL}{path}"
    last_error: Exception | None = None
    challenge_cookie = ""
    for attempt in range(WIKI_RETRY_COUNT):
        acquire_wiki_request_slot()
        headers = {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": f"{WIKI_BASE_URL}/blhx/%E5%AE%9E%E8%A3%85%E6%97%A5%E6%9C%9F",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
        }
        if challenge_cookie:
            headers["Cookie"] = challenge_cookie
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=25) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                body = response.read().decode(charset, errors="replace")
            if "__tst_status" in body and "EO_Bot_Ssid" in body:
                parsed_cookie = parse_wiki_challenge_cookie(body)
                if parsed_cookie and parsed_cookie != challenge_cookie:
                    challenge_cookie = parsed_cookie
                    LOGGER.warning("检测到 Wiki 防护校验，已生成 Cookie 并重试: %s", url)
                    continue
            return body
        except HTTPError as error:
            last_error = error
            if error.code not in {403, 408, 429} and error.code < 500:
                raise
            error.close()
        except (URLError, TimeoutError, OSError) as error:
            last_error = error
        if attempt + 1 < WIKI_RETRY_COUNT:
            delay = WIKI_RETRY_DELAY * (2**attempt) + random.uniform(0, 0.5)
            LOGGER.warning(
                "Wiki 请求失败，%.1f 秒后重试（第 %s/%s 次）: %s",
                delay,
                attempt + 1,
                WIKI_RETRY_COUNT,
                url,
            )
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def fetch_wiki_api(params: dict[str, Any]) -> dict[str, Any]:
    path = f"{WIKI_API_PATH}?{urlencode(params)}"
    body = fetch_wiki_text(path)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        LOGGER.error(
            "解析 Wiki API 响应失败，原始返回（长度 %s）：%s\n%s",
            len(body),
            body,
        )
        raise ValueError("Wiki API 返回非 JSON 数据") from None
    if not isinstance(payload, dict):
        LOGGER.error("Wiki API 响应结构异常，原始返回：%s", body)
        raise ValueError("Wiki API 响应结构异常")
    if payload.get("error"):
        LOGGER.error("Wiki API 请求失败，原始返回：%s", body)
        raise ValueError(payload["error"].get("info", "Wiki API 请求失败"))
    return payload


def fetch_release_entries() -> list[dict[str, str]]:
    entries_by_name: dict[str, dict[str, str]] = {}
    offset = 0
    while True:
        query = (
            "[[分类:舰娘]] OR [[分类:联动舰娘]] OR [[分类:方案舰娘]] "
            "OR [[分类:META舰娘]]|?实装日期|?编号"
            f"|sort=实装日期|order=asc|limit=500|offset={offset}"
        )
        payload = fetch_wiki_api(
            {
                "action": "ask",
                "query": query,
                "format": "json",
                "formatversion": "2",
            }
        )
        results = payload.get("query", {}).get("results", {})
        if not isinstance(results, dict):
            LOGGER.error("Wiki API 舰娘列表响应结构异常，原始返回：%s", payload)
            raise ValueError("Wiki API 舰娘列表响应结构异常")
        for result in results.values():
            ship_name = str(result.get("fulltext", "")).strip()
            date_values = result.get("printouts", {}).get("实装日期", [])
            date_text = str(date_values[0]).strip() if date_values else ""
            date_match = re.fullmatch(
                r"(\d{4})年(\d{1,2})月(?:(\d{1,2})日)?", date_text
            )
            if not ship_name or date_match is None:
                LOGGER.warning(
                    "Wiki API 舰娘缺少有效实装日期，已跳过: %s（%s）",
                    ship_name or "未知名称",
                    date_text,
                )
                continue
            year, month, day = date_match.groups()
            entries_by_name[ship_name] = {
                "name": ship_name,
                "release_date": f"{year}-{int(month):02d}-{int(day or 1):02d}",
            }
        offset = payload.get("query-continue-offset")
        if not offset:
            break
    return list(entries_by_name.values())


def fetch_ship_raw(ship_name: str) -> str:
    payload = fetch_wiki_api(
        {
            "action": "query",
            "titles": ship_name,
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "redirects": "1",
            "format": "json",
            "formatversion": "2",
        }
    )
    pages = payload.get("query", {}).get("pages", [])
    if not isinstance(pages, list) or not pages:
        LOGGER.error("Wiki API 未返回舰娘页面，原始返回：%s", payload)
        raise ValueError("Wiki API 未返回舰娘页面")
    page = pages[0]
    if page.get("missing"):
        LOGGER.error("Wiki API 未找到舰娘页面，原始返回：%s", payload)
        raise ValueError(f"Wiki API 未找到舰娘页面：{ship_name}")
    raw_text = (
        page.get("revisions", [{}])[0]
        .get("slots", {})
        .get("main", {})
        .get("content", "")
    )
    if not raw_text:
        LOGGER.error("Wiki API 未返回舰娘内容，原始返回：%s", payload)
        raise ValueError(f"Wiki API 未返回舰娘内容：{ship_name}")
    return raw_text


def fetch_ship_payload(entry: dict[str, str]) -> dict[str, Any]:
    raw_text = fetch_ship_raw(entry["name"])
    parsed_ship = parse_ship_raw(raw_text)
    if parsed_ship is None:
        LOGGER.error(
            "解析舰娘 Wiki 数据失败，原始返回（长度 %s）：%s\n%s",
            len(raw_text),
            entry["name"],
            raw_text,
        )
        raise ValueError("未找到舰娘图鉴模板")
    return {
        "name": entry["name"],
        "release_date": entry["release_date"],
        **parsed_ship,
    }


def _begin_sync(target_names: list[str] | None = None) -> bool:
    with SYNC_LOCK:
        if SYNC_STATE["running"]:
            return False
        sync_label = "、".join(target_names or [])
        SYNC_STATE.update(
            {
                "running": True,
                "status": "同步中",
                "message": f"正在匹配舰娘 {sync_label}" if target_names else "正在读取实装日期页面",
                "total": 0,
                "processed": 0,
                "skipped": 0,
                "errors": [],
            }
        )
        return True


def sync_wiki(target_names: list[str] | None = None) -> None:
    if not _begin_sync(target_names):
        return
    _sync_wiki(target_names)


def _sync_wiki(target_names: list[str] | None = None) -> None:
    try:
        entries = fetch_release_entries()
        if target_names is not None:
            entries_by_name = {entry["name"]: entry for entry in entries}
            missing_names = [
                target_name
                for target_name in target_names
                if target_name not in entries_by_name
            ]
            if missing_names:
                raise ValueError(
                    "Wiki 后端接口未找到舰娘：" + "、".join(missing_names)
                )
            entries = [entries_by_name[target_name] for target_name in target_names]
        SYNC_STATE["total"] = len(entries)
        init_db()
        with get_connection() as connection:
            existing_names = {
                row["name"]
                for row in connection.execute("SELECT name FROM ships").fetchall()
            }
            if target_names is not None:
                pending_entries = entries
                SYNC_STATE["skipped"] = 0
                SYNC_STATE["message"] = (
                    f"已匹配 {len(target_names)} 个舰娘，正在同步 Wiki 数据"
                )
            else:
                pending_entries = [
                    entry for entry in entries if entry["name"] not in existing_names
                ]
                SYNC_STATE["skipped"] = len(entries) - len(pending_entries)
                SYNC_STATE["message"] = (
                    f"已读取实装日期页面，并发查询 {len(pending_entries)} 艘新舰船"
                    if pending_entries
                    else "没有需要新增的舰船"
                )

            with ThreadPoolExecutor(max_workers=WIKI_CONCURRENCY) as executor:
                futures = {
                    executor.submit(fetch_ship_payload, entry): entry
                    for entry in pending_entries
                }
                for future in as_completed(futures):
                    entry = futures[future]
                    ship_name = entry["name"]
                    try:
                        parsed_ship = future.result()
                        insert_ship(
                            connection,
                            ship_name,
                            entry["release_date"],
                            parsed_ship,
                            overwrite=target_names is not None,
                        )
                        existing_names.add(ship_name)
                        SYNC_STATE["processed"] += 1
                        SYNC_STATE["message"] = f"已同步 {ship_name}"
                    except Exception as error:
                        LOGGER.exception("处理单舰 Wiki 数据失败: %s", ship_name)
                        SYNC_STATE["errors"].append(f"{ship_name}: {error}")
                        SYNC_STATE["message"] = f"处理 {ship_name} 时出错"
        SYNC_STATE["status"] = "完成"
        SYNC_STATE["message"] = (
            f"{'、'.join(target_names)} 同步完成"
            if target_names is not None
            else "同步完成"
        )
    except Exception as error:
        LOGGER.exception("Wiki 同步失败")
        SYNC_STATE["status"] = "失败"
        SYNC_STATE["message"] = str(error)
    finally:
        SYNC_STATE["running"] = False


def start_sync(ship_names: str | list[str] | None = None) -> bool:
    if ship_names is None:
        target_names = None
    elif isinstance(ship_names, str):
        target_names = [
            name.strip()
            for name in re.split(r"[,，;；\r\n]+", ship_names)
            if name.strip()
        ]
    else:
        target_names = [
            name.strip() for name in ship_names if isinstance(name, str) and name.strip()
        ]
    if ship_names is not None and not target_names:
        raise ValueError("请输入要同步的舰娘名称")
    if not _begin_sync(target_names):
        return False
    threading.Thread(target=_sync_wiki, args=(target_names,), daemon=True).start()
    return True


def delete_ship(ship_id: int) -> bool:
    if ship_id <= 0:
        raise ValueError("舰娘 ID 无效")
    with get_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM ships WHERE id = ?", (ship_id,)
        )
        return cursor.rowcount > 0


def clear_ships() -> int:
    if SYNC_STATE["running"]:
        raise ValueError("同步正在进行中，请稍后再清空数据")
    with get_connection() as connection:
        cursor = connection.execute("DELETE FROM ships")
        return cursor.rowcount


def list_ships(filters: dict[str, str]) -> list[dict[str, Any]]:
    conditions: list[str] = []
    parameters: list[Any] = []

    if filters.get("name"):
        conditions.append("s.name LIKE ?")
        parameters.append(f"%{filters['name']}%")
    if filters.get("ship_type"):
        conditions.append("s.ship_type = ?")
        parameters.append(filters["ship_type"])
    if filters.get("rarity"):
        conditions.append("s.rarity = ?")
        parameters.append(filters["rarity"])
    if filters.get("needs_training") in {"是", "否"}:
        conditions.append("s.needs_training = ?")
        parameters.append(1 if filters["needs_training"] == "是" else 0)
    if filters.get("is_idling") in {"是", "否"}:
        if filters["is_idling"] == "是":
            conditions.append("s.is_idling = 1")
        else:
            conditions.append("COALESCE(s.is_idling, 0) = 0")
    if filters.get("is_trained") in {"是", "否"}:
        if filters["is_trained"] == "是":
            conditions.append("s.is_trained = 1")
        else:
            conditions.append("COALESCE(s.is_trained, 0) = 0")
    if filters.get("tech_attribute"):
        keyword = f"%{filters['tech_attribute']}%"
        conditions.append(
            """
            EXISTS (
                SELECT 1 FROM tech_points tp
                WHERE tp.ship_id = s.id
                  AND tp.stage IN ('解锁图鉴', '达到120级')
                  AND tp.attribute LIKE ?
            )
            """
        )
        parameters.append(keyword)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT s.* FROM ships s {where_clause}
            ORDER BY CASE WHEN s.release_date = '' THEN 1 ELSE 0 END,
                     s.release_date, s.name COLLATE NOCASE
            """,
            parameters,
        ).fetchall()
        ships = [ship_row_to_dict(row) for row in rows]
        if ships:
            placeholders = ",".join("?" for _ in ships)
            tech_rows = connection.execute(
                f"""
                SELECT ship_id, stage, attribute, points
                FROM tech_points
                WHERE ship_id IN ({placeholders})
                ORDER BY stage, attribute, points
                """,
                [ship["id"] for ship in ships],
            ).fetchall()
            tech_by_ship: dict[int, list[dict[str, Any]]] = {}
            for tech_row in tech_rows:
                tech_by_ship.setdefault(tech_row["ship_id"], []).append(dict(tech_row))
            for ship in ships:
                ship["tech_points"] = tech_by_ship.get(ship["id"], [])
    return ships


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


def next_ship(mode: str, ship_type: str = "") -> dict[str, Any] | None:
    if mode not in {"idle", "train"}:
        raise ValueError("mode 必须是 idle 或 train")
    filters = {
        "ship_type": ship_type,
        "needs_training": "是",
        "is_idling": "否" if mode == "idle" else "",
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


def update_ship_status(
    ship_id: int,
    field: str,
    value: bool,
) -> dict[str, Any] | None:
    if field not in {"is_idling", "is_trained"}:
        raise ValueError("field 只支持 is_idling 或 is_trained")
    if not isinstance(value, bool):
        raise ValueError("value 必须是布尔值")
    with get_connection() as connection:
        connection.execute(
            f"UPDATE ships SET {field} = ?, updated_at = ? WHERE id = ?",
            (1 if value else 0, utc_now_text(), ship_id),
        )
        row = connection.execute(
            "SELECT id, is_idling, is_trained FROM ships WHERE id = ?",
            (ship_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "is_idling": None if row["is_idling"] is None else bool(row["is_idling"]),
        "is_trained": None if row["is_trained"] is None else bool(row["is_trained"]),
    }


def column_index_from_ref(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref.upper())
    if not letters:
        return 1
    result = 0
    for character in letters.group():
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def local_name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    shared_strings: list[str] = []
    for item in root:
        shared_strings.append(
            "".join(
                node.text or ""
                for node in item.iter()
                if local_name(node) == "t"
            )
        )
    return shared_strings


def cell_text(
    cell: ElementTree.Element, shared_strings: list[str]
) -> str:
    value_node = next(
        (node for node in cell if local_name(node) == "v"), None
    )
    if cell.attrib.get("t") == "inlineStr":
        return "".join(
            node.text or ""
            for node in cell.iter()
            if local_name(node) == "t"
        )
    if value_node is None:
        return ""
    value = value_node.text or ""
    if cell.attrib.get("t") == "s":
        try:
            return shared_strings[int(value)]
        except (ValueError, IndexError):
            return ""
    return value


def xlsx_read(data: bytes) -> list[list[str]]:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        sheet_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/")
            and name.endswith(".xml")
        )
        if not sheet_names:
            return []
        shared_strings = read_shared_strings(archive)
        root = ElementTree.fromstring(archive.read(sheet_names[0]))
        rows: list[list[str]] = []
        for row_node in root.iter():
            if local_name(row_node) != "row":
                continue
            values: list[str] = []
            for cell_node in row_node:
                if local_name(cell_node) != "c":
                    continue
                cell_ref = cell_node.attrib.get("r", "")
                column_index = (
                    column_index_from_ref(cell_ref) if cell_ref else len(values)
                )
                while len(values) < column_index:
                    values.append("")
                values[column_index - 1] = cell_text(cell_node, shared_strings)
            rows.append(values)
        return rows


def xml_escape(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def cell_reference(row_index: int, column_index: int) -> str:
    letters = ""
    while column_index:
        column_index, remainder = divmod(column_index - 1, 26)
        letters = chr(remainder + ord("A")) + letters
    return f"{letters}{row_index}"


def xlsx_write(
    rows: list[list[Any]],
    sheet_name: str = "Sheet1",
    column_widths: list[float] | None = None,
) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="{xml_escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        )

        row_xml: list[str] = []
        for row_index, row in enumerate(rows, start=1):
            cell_xml: list[str] = []
            for column_index, value in enumerate(row, start=1):
                reference = cell_reference(row_index, column_index)
                if value is None or value == "":
                    cell_xml.append(f'<c r="{reference}"/>')
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    cell_xml.append(
                        f'<c r="{reference}" t="n"><v>{xml_escape(value)}</v></c>'
                    )
                else:
                    cell_xml.append(
                        f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{xml_escape(value)}</t></is></c>'
                    )
            row_xml.append(
                f'<row r="{row_index}">{"".join(cell_xml)}</row>'
            )

        column_xml = ""
        if column_widths:
            column_xml = "<cols>" + "".join(
                f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
                for index, width in enumerate(column_widths, start=1)
            ) + "</cols>"
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
{column_xml}<sheetData>{"".join(row_xml)}</sheetData>
</worksheet>""",
        )
    return output.getvalue()


def import_template_bytes() -> bytes:
    headers = ["舰船名称", "是否已挂机", "是否已练级完成"]
    return xlsx_write(
        [headers],
        "导入模板",
        [24, 16, 20],
    )


def parse_workbook_rows(rows: list[list[str]]) -> list[dict[str, str]]:
    if not rows:
        raise ValueError("Excel 文件为空")
    headers = [str(value).strip() for value in rows[0]]
    required_headers = {"舰船名称", "是否已挂机", "是否已练级完成"}
    if not required_headers.issubset(set(headers)):
        raise ValueError("Excel 第一行必须包含：舰船名称、是否已挂机、是否已练级完成")

    parsed_rows: list[dict[str, str]] = []
    for row in rows[1:]:
        item = {
            header: str(row[index]).strip() if index < len(row) else ""
            for index, header in enumerate(headers)
            if header
        }
        parsed_rows.append(item)
    return parsed_rows


def process_import_rows(
    rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], int, int]:
    init_db()
    with get_connection() as connection:
        ship_rows = connection.execute(
            "SELECT id, name, is_idling, is_trained FROM ships"
        ).fetchall()
        ships_by_name = {row["name"]: row for row in ship_rows}

    report_rows: list[dict[str, str]] = []
    updated_count = 0
    skipped_count = 0
    valid_values = {"是", "否"}

    for row in rows:
        ship_name = row.get("舰船名称", "").strip()
        idling_value = row.get("是否已挂机", "").strip()
        trained_value = row.get("是否已练级完成", "").strip()

        if not ship_name:
            status = "跳过"
            message = "舰船名称为空，已跳过"
        elif ship_name not in ships_by_name:
            status = "未匹配到数据跳过"
            message = "未匹配到数据跳过"
        elif idling_value not in valid_values:
            status = "跳过"
            message = "是否已挂机只支持填写是或否"
        elif trained_value not in valid_values:
            status = "跳过"
            message = "是否已练级完成只支持填写是或否"
        else:
            with get_connection() as connection:
                connection.execute(
                    """
                    UPDATE ships
                    SET is_idling = ?, is_trained = ?, updated_at = ?
                    WHERE name = ?
                    """,
                    (
                        1 if idling_value == "是" else 0,
                        1 if trained_value == "是" else 0,
                        utc_now_text(),
                        ship_name,
                    ),
                )
            updated_count += 1
            status = "处理完成"
            message = "处理完成"

        if status != "处理完成":
            skipped_count += 1
        report_rows.append(
            {
                "舰船名称": ship_name,
                "是否已挂机": idling_value,
                "是否已练级完成": trained_value,
                "处理状态": status,
                "处理说明": message,
            }
        )

    return report_rows, updated_count, skipped_count


def import_excel(data: bytes) -> tuple[bytes, int, int]:
    rows = parse_workbook_rows(xlsx_read(data))
    report_rows, updated_count, skipped_count = process_import_rows(rows)
    report = [
        [
            "舰船名称",
            "是否已挂机",
            "是否已练级完成",
            "处理状态",
            "处理说明",
        ]
    ]
    for row in report_rows:
        report.append(
            [
                row["舰船名称"],
                row["是否已挂机"],
                row["是否已练级完成"],
                row["处理状态"],
                row["处理说明"],
            ]
        )
    report_bytes = xlsx_write(report, "导入结果", [24, 16, 20, 22, 34])
    return report_bytes, updated_count, skipped_count


@dataclass
class UploadedFile:
    filename: str
    data: bytes


def parse_multipart_file(content_type: str, body: bytes) -> UploadedFile | None:
    boundary_match = re.search(
        r'boundary=(?:"([^"]+)"|([^;]+))', content_type, re.IGNORECASE
    )
    if not boundary_match:
        return None
    boundary = (boundary_match.group(1) or boundary_match.group(2)).strip().encode()
    delimiter = b"--" + boundary
    for segment in body.split(delimiter)[1:]:
        if segment.startswith(b"--"):
            break
        if segment.startswith(b"\r\n"):
            segment = segment[2:]
        elif segment.startswith(b"\n"):
            segment = segment[1:]
        if segment.endswith(b"\r\n"):
            segment = segment[:-2]
        elif segment.endswith(b"\n"):
            segment = segment[:-1]
        header_bytes, separator, file_data = segment.partition(b"\r\n\r\n")
        if not separator:
            header_bytes, separator, file_data = segment.partition(b"\n\n")
        if not separator:
            continue
        header_text = header_bytes.decode("latin-1", errors="replace")
        disposition = next(
            (
                line
                for line in header_text.splitlines()
                if line.lower().startswith("content-disposition:")
            ),
            "",
        )
        filename_match = re.search(r'filename\*?=(?:"([^"]*)"|([^;\r\n]+))', disposition, re.IGNORECASE)
        if not filename_match:
            continue
        filename = (filename_match.group(1) or filename_match.group(2)).strip()
        if filename.startswith("UTF-8''"):
            filename = unquote(filename[7:])
        return UploadedFile(filename=filename, data=file_data)
    return None


PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>碧蓝航线练级数据记录</title>
  <style>
    :root { color-scheme: dark; --bg:#0f172a; --panel:#16213c; --panel2:#1e293b; --line:#2f3f5d; --text:#e6edf7; --muted:#9db0c9; --blue:#38bdf8; --green:#4ade80; --red:#f87171; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font-family:"Microsoft YaHei",system-ui,sans-serif; }
    body.loading { overflow:hidden; cursor:progress; user-select:none; }
    body.loading button:not(.sync-close), body.loading input, body.loading select { pointer-events:none; opacity:.6; }
    .container { max-width:1500px; margin:0 auto; padding:28px 24px 60px; }
    header { display:flex; justify-content:space-between; align-items:center; gap:16px; margin-bottom:22px; }
    h1 { font-size:26px; margin:0; }
    .subtitle { color:var(--muted); margin-top:6px; }
    .toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    button,.button { border:1px solid #35507a; background:var(--panel2); color:var(--text); padding:10px 16px; border-radius:8px; cursor:pointer; text-decoration:none; font-size:14px; transition:.2s; }
    button:hover,.button:hover { border-color:var(--blue); color:var(--blue); }
    button.primary { background:#0891b2; border-color:#0891b2; color:white; }
    button.primary:hover { background:#06b6d4; color:white; }
    button.danger { border-color:#7f1d1d; color:#fca5a5; }
    button.danger:hover { background:#7f1d1d; border-color:#b91c1c; color:white; }
    .card { background:linear-gradient(180deg,var(--panel),#131d34); border:1px solid var(--line); border-radius:14px; padding:20px; box-shadow:0 8px 28px rgba(0,0,0,.18); }
    .name-search { max-width:420px; margin:18px 0 16px; }
    .filter-groups { display:grid; gap:12px; margin-bottom:16px; }
    .chip-group { display:flex; align-items:center; flex-wrap:wrap; gap:8px; }
    .chip-label { color:var(--muted); font-size:12px; min-width:92px; }
    .chip { border:1px solid #35507a; background:var(--panel2); color:var(--text); padding:6px 12px; border-radius:999px; cursor:pointer; font-size:13px; transition:.2s; }
    .chip:hover { border-color:var(--blue); color:var(--blue); }
    .chip.selected { background:#0891b2; border-color:#0891b2; color:white; }
    input,select { width:100%; background:#0b1120; border:1px solid var(--line); color:var(--text); border-radius:8px; padding:10px; font-size:14px; }
    input:focus,select:focus { outline:2px solid rgba(56,189,248,.25); border-color:var(--blue); }
    .status-line { min-height:20px; color:var(--muted); font-size:13px; }
    .next-card { margin-bottom:18px; min-height:82px; }
    .next-title { color:var(--muted); font-size:13px; margin-bottom:10px; }
    .ship-name { font-size:21px; font-weight:700; }
    .ship-meta { color:var(--muted); margin-top:6px; }
    table { width:100%; border-collapse:collapse; margin-top:16px; }
    th,td { padding:12px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
    th { color:var(--muted); font-size:12px; white-space:nowrap; background:#172136; position:sticky; top:0; }
    tr:hover td { background:rgba(56,189,248,.05); }
    .tech-list { display:flex; flex-wrap:wrap; gap:6px; max-width:420px; }
    .tech { background:#123049; border:1px solid #244b6b; color:#bae6fd; padding:3px 8px; border-radius:999px; font-size:12px; white-space:nowrap; }
    .yes { color:var(--green); font-weight:700; }
    .no { color:var(--red); font-weight:700; }
    .empty { color:var(--muted); }
    .actions { display:flex; gap:10px; margin-top:12px; }
    .row-actions { display:flex; flex-wrap:wrap; gap:6px; }
    .row-actions button { padding:6px 10px; font-size:12px; white-space:nowrap; }
    .modal-overlay { display:none; position:fixed; inset:0; z-index:90; align-items:center; justify-content:center; padding:20px; background:rgba(8,13,25,.78); backdrop-filter:blur(3px); }
    .modal-overlay.active { display:flex; }
    .modal { width:min(460px,92vw); background:linear-gradient(180deg,var(--panel),#131d34); border:1px solid var(--line); border-radius:14px; padding:22px; box-shadow:0 18px 48px rgba(0,0,0,.35); }
    .modal h2 { font-size:19px; margin:0 0 16px; }
    .modal-label { display:block; color:var(--muted); font-size:12px; margin-bottom:8px; }
    .modal textarea { width:100%; min-height:120px; background:#0b1120; border:1px solid var(--line); color:var(--text); border-radius:8px; padding:10px; font-size:14px; resize:vertical; font-family:inherit; line-height:1.5; }
    .modal-message { color:var(--text); font-size:14px; line-height:1.6; margin:0; }
    .modal-error { color:var(--red); font-size:12px; margin-top:10px; }
    .modal-footer { display:flex; justify-content:flex-end; gap:10px; margin-top:20px; }
    .sync-overlay { display:none; position:fixed; inset:0; z-index:100; align-items:center; justify-content:center; padding:20px; background:rgba(8,13,25,.76); backdrop-filter:blur(3px); }
    .sync-overlay.active { display:flex; }
    .sync-modal { width:min(520px,92vw); background:linear-gradient(180deg,var(--panel),#131d34); border:1px solid var(--line); border-radius:14px; padding:24px; box-shadow:0 18px 48px rgba(0,0,0,.35); }
    .sync-title-row { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }
    .sync-title-row h2 { font-size:19px; margin:0; }
    .spinner { width:18px; height:18px; border:2px solid rgba(56,189,248,.25); border-top-color:var(--blue); border-radius:50%; animation:spin .8s linear infinite; }
    .spinner[hidden] { display:none; }
    @keyframes spin { to { transform:rotate(360deg); } }
    .progress-head { display:flex; justify-content:space-between; color:var(--muted); font-size:12px; margin:18px 0 8px; }
    .progress-track { height:8px; background:#0b1120; border-radius:999px; overflow:hidden; }
    .progress-fill { width:0%; height:100%; background:linear-gradient(90deg,#0891b2,#38bdf8); transition:width .3s ease; }
    .sync-count { margin-top:8px; color:var(--muted); font-size:13px; text-align:right; }
    .sync-stats { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-top:16px; }
    .sync-stat { background:#0b1120; border:1px solid var(--line); border-radius:8px; padding:10px; }
    .sync-stat-label { color:var(--muted); font-size:11px; margin-bottom:4px; }
    .sync-stat-value { font-size:17px; font-weight:700; }
    .sync-errors { max-height:130px; overflow:auto; margin:14px 0 0; padding-left:18px; color:var(--red); font-size:12px; line-height:1.6; }
    .sync-close { display:block; width:100%; margin-top:20px; }
    @media (max-width:680px) { header { display:block; } .toolbar { margin-top:14px; } .chip-label { min-width:100%; } th,td { padding:9px 6px; } .sync-stats { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <div id="syncOverlay" class="sync-overlay">
    <div class="sync-modal" role="dialog" aria-modal="true" aria-labelledby="syncTitle" aria-live="polite">
      <div class="sync-title-row">
        <h2 id="syncTitle">同步进度</h2>
        <div id="syncSpinner" class="spinner" hidden></div>
      </div>
      <div id="syncMessage" class="status-line"></div>
      <div class="progress-head"><span>进度</span><span id="syncPercent">0%</span></div>
      <div class="progress-track"><div id="syncProgressBar" class="progress-fill"></div></div>
      <div id="syncCount" class="sync-count">0 / 0</div>
      <div class="sync-stats">
        <div class="sync-stat"><div class="sync-stat-label">新增</div><div id="syncProcessed" class="sync-stat-value">0</div></div>
        <div class="sync-stat"><div class="sync-stat-label">跳过</div><div id="syncSkipped" class="sync-stat-value">0</div></div>
        <div class="sync-stat"><div class="sync-stat-label">错误</div><div id="syncErrorCount" class="sync-stat-value">0</div></div>
      </div>
      <ul id="syncErrors" class="sync-errors" hidden></ul>
      <button id="syncClose" class="sync-close" disabled onclick="closeSyncOverlay()">关闭</button>
    </div>
  </div>

  <div id="bulkSyncModal" class="modal-overlay">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="bulkSyncTitle">
      <h2 id="bulkSyncTitle">少量同步</h2>
      <label class="modal-label" for="bulkSyncNames">舰娘名称（多个名称用逗号、分号或换行分隔）</label>
      <textarea id="bulkSyncNames" rows="5" placeholder="例如：武藏，古鹰&#10;U-552"></textarea>
      <div id="bulkSyncError" class="modal-error" hidden></div>
      <div class="modal-footer">
        <button type="button" onclick="closeBulkSyncModal()">取消</button>
        <button type="button" class="primary" onclick="submitBulkSync()">发起同步</button>
      </div>
    </div>
  </div>

  <div id="fullSyncConfirmModal" class="modal-overlay">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="fullSyncConfirmTitle">
      <h2 id="fullSyncConfirmTitle">全量同步确认</h2>
      <p class="modal-message">将请求 Wiki 后端接口并同步所有舰娘，确认发起全量同步？</p>
      <div class="modal-footer">
        <button type="button" onclick="cancelFullSync()">取消</button>
        <button type="button" class="primary" onclick="confirmFullSync()">确认同步</button>
      </div>
    </div>
  </div>

  <div id="deleteConfirmModal" class="modal-overlay">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="deleteConfirmTitle">
      <h2 id="deleteConfirmTitle">删除确认</h2>
      <p id="deleteConfirmMessage" class="modal-message"></p>
      <div class="modal-footer">
        <button type="button" onclick="cancelDeleteShip()">取消</button>
        <button type="button" class="danger" onclick="confirmDeleteShip()">删除</button>
      </div>
    </div>
  </div>

  <div id="clearConfirmModal" class="modal-overlay">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="clearConfirmTitle">
      <h2 id="clearConfirmTitle">清空数据确认</h2>
      <p class="modal-message">将删除数据库中所有舰娘及其科技点数据，此操作不可恢复。</p>
      <div class="modal-footer">
        <button type="button" onclick="cancelClearShips()">取消</button>
        <button type="button" class="danger" onclick="confirmClearShips()">确认清空</button>
      </div>
    </div>
  </div>

  <div class="container">
    <header>
      <div>
        <h1>碧蓝航线练级数据记录</h1>
        <div class="subtitle">全量同步 · Excel 导入 · 挂机与练级排序</div>
      </div>
      <div class="toolbar">
        <button id="syncButton" class="primary" onclick="requestFullSync()">全量同步</button>
        <button id="partialSyncButton" onclick="startBulkSync()">少量同步</button>
        <a class="button" href="/api/template">下载导入模板</a>
        <button onclick="document.getElementById('importFile').click()">导入 Excel</button>
        <button id="clearDataButton" class="danger" onclick="requestClearShips()">清空数据</button>
        <input id="importFile" type="file" accept=".xlsx" style="display:none" onchange="importExcel(this)">
      </div>
    </header>

    <div class="card next-card">
      <div class="next-title">推荐目标</div>
      <div id="nextTarget" class="empty">暂未选择</div>
      <div class="actions">
        <button onclick="getNext('idle')">获取下一个挂机舰船</button>
        <button onclick="getNext('train')">获取下一个练级舰船</button>
      </div>
    </div>

    <div class="card">
      <div class="name-search">
        <input id="fName" placeholder="输入名称关键字，自动模糊查询">
      </div>
      <div id="filterGroups" class="filter-groups"></div>
      <div id="message" class="status-line"></div>
      <table>
        <thead><tr><th>舰船名称</th><th>舰种</th><th>稀有度</th><th>获得</th><th>120级</th><th>实装日期</th><th>是否需要练级</th><th>是否已挂机</th><th>是否已练级完成</th><th>操作</th></tr></thead>
        <tbody id="shipTable"><tr><td colspan="10" class="empty">加载中...</td></tr></tbody>
      </table>
    </div>
  </div>

  <script>
    let shipData = [];
    let filterOptions = { shipTypes: [], rarities: [] };
    let nameSearchTimer = null;
    const techAttributes = ['装填', '命中', '炮击', '防空', '耐久', '航空', '雷击', '机动'];
    const filterState = {
      name: '',
      ship_type: '',
      rarity: '',
      tech_attribute: '',
      needs_training: '',
      is_idling: '',
      is_trained: '否'
    };
    const escapeHtml = value => {
      const div = document.createElement('div');
      div.textContent = value;
      return div.innerHTML;
    };
    const statusClass = value => value === true ? 'yes' : value === false ? 'no' : 'empty';
    const statusText = value => value === true ? '是' : value === false ? '否' : '';

    async function requestJson(url, options = {}) {
      const response = await fetch(url, options);
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || response.statusText);
      return result;
    }

    function showMessage(text, isError = false) {
      const element = document.getElementById('message');
      element.textContent = text || '';
      element.style.color = isError ? '#f87171' : '#9db0c9';
    }

    async function loadShips() {
      const params = new URLSearchParams({
        ...filterState
      });
      try {
        const result = await requestJson('/api/ships?' + params.toString());
        shipData = result.ships;
        renderShips();
        showMessage(`共 ${shipData.length} 条记录`);
      } catch (error) {
        showMessage(error.message, true);
      }
    }

    function renderShips() {
      const tbody = document.getElementById('shipTable');
      if (!shipData.length) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty">没有匹配的数据</td></tr>';
        return;
      }
      tbody.innerHTML = shipData.map(ship => {
        const stageTech = stage => ship.tech_points
          .filter(item => item.stage === stage)
          .map(item => item.attribute)
          .join('、') || '无';
        const actionButtons = [];
        actionButtons.push(`<button type="button" class="danger" data-delete data-id="${escapeHtml(ship.id)}" data-name="${escapeHtml(ship.name)}">删除</button>`);
        if (ship.is_idling !== true) {
          actionButtons.push(`<button type="button" data-action="is_idling" data-id="${escapeHtml(ship.id)}">已挂机完成</button>`);
        }
        if (ship.is_trained !== true) {
          actionButtons.push(`<button type="button" data-action="is_trained" data-id="${escapeHtml(ship.id)}">已练级完成</button>`);
        }
        return `<tr>
          <td><a href="${escapeHtml(ship.wiki_url)}" target="_blank" style="color:#e6edf7;">${escapeHtml(ship.name)}</a></td>
          <td>${escapeHtml(ship.ship_type || '')}</td>
          <td>${escapeHtml(ship.rarity || '')}</td>
          <td>${escapeHtml(stageTech('解锁图鉴'))}</td>
          <td>${escapeHtml(stageTech('达到120级'))}</td>
          <td>${escapeHtml(ship.release_date || '')}</td>
          <td class="${ship.needs_training ? 'yes' : 'empty'}">${ship.needs_training ? '是' : '否'}</td>
          <td class="${statusClass(ship.is_idling)}">${statusText(ship.is_idling)}</td>
          <td class="${statusClass(ship.is_trained)}">${statusText(ship.is_trained)}</td>
          <td><div class="row-actions">${actionButtons.join('')}</div></td>
        </tr>`;
      }).join('');
    }

    function renderNext(ship) {
      const target = document.getElementById('nextTarget');
      if (!ship) {
        target.innerHTML = '<span class="empty">没有符合条件的数据</span>';
        return;
      }
      const tech = ship.tech_points
        .filter(item => item.stage === '解锁图鉴' || item.stage === '达到120级')
        .map(item => `${item.stage}${item.attribute ? '｜' + item.attribute : ''}`)
        .join('、');
      target.innerHTML = `
        <div class="ship-name">${escapeHtml(ship.name)}</div>
        <div class="ship-meta">${escapeHtml(ship.ship_type || '')} · ${escapeHtml(ship.rarity || '')} · ${escapeHtml(tech || '无科技点')}</div>`;
    }

    function renderFilters() {
      const groups = [
        { key: 'ship_type', label: '舰种', options: ['', ...filterOptions.shipTypes] },
        { key: 'rarity', label: '稀有度', options: ['', ...filterOptions.rarities] },
        { key: 'tech_attribute', label: '科技属性', options: ['', ...techAttributes] },
        { key: 'needs_training', label: '需要练级', options: ['', '是', '否'] },
        { key: 'is_idling', label: '已挂机', options: ['', '是', '否'] },
        { key: 'is_trained', label: '已练级完成', options: ['', '是', '否'] }
      ];
      document.getElementById('filterGroups').innerHTML = groups.map(group => `
        <div class="chip-group">
          <span class="chip-label">${escapeHtml(group.label)}</span>
          ${group.options.map(option => `
            <button type="button"
              class="chip ${filterState[group.key] === option ? 'selected' : ''}"
              data-group="${escapeHtml(group.key)}"
              data-value="${escapeHtml(option)}"
            >${escapeHtml(option || '全部')}</button>
          `).join('')}
        </div>
      `).join('');
    }

    async function completeShipStatus(shipId, field) {
      try {
        await requestJson('/api/ship/status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: shipId, field, value: true })
        });
        showMessage('状态已更新');
        await loadShips();
      } catch (error) {
        showMessage(error.message, true);
      }
    }

    async function deleteShip(shipId, shipName) {
      try {
        await requestJson('/api/ship/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: shipId })
        });
        showMessage(`已删除 ${shipName}`);
        await loadShips();
      } catch (error) {
        showMessage(error.message, true);
      }
    }

    let pendingDeleteShip = null;

    function requestDeleteShip(shipId, shipName) {
      pendingDeleteShip = { id: shipId, name: shipName };
      document.getElementById('deleteConfirmMessage').textContent =
        `确认删除舰娘「${shipName}」及其相关数据？`;
      document.getElementById('deleteConfirmModal').classList.add('active');
    }

    function cancelDeleteShip() {
      pendingDeleteShip = null;
      document.getElementById('deleteConfirmModal').classList.remove('active');
    }

    async function confirmDeleteShip() {
      if (!pendingDeleteShip) return;
      await deleteShip(pendingDeleteShip.id, pendingDeleteShip.name);
      cancelDeleteShip();
    }

    function requestClearShips() {
      document.getElementById('clearConfirmModal').classList.add('active');
    }

    function cancelClearShips() {
      document.getElementById('clearConfirmModal').classList.remove('active');
    }

    async function confirmClearShips() {
      try {
        const result = await requestJson('/api/ships/clear', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        cancelClearShips();
        showMessage(`已清空 ${result.count} 个舰娘`);
        await loadShips();
      } catch (error) {
        cancelClearShips();
        showMessage(error.message, true);
      }
    }

    function parseSyncNames(text) {
      return [...new Set(
        String(text || '')
          .split(/[,，;；\r\n]+/)
          .map(name => name.trim())
          .filter(Boolean)
      )];
    }

    function startBulkSync() {
      document.getElementById('bulkSyncNames').value = '';
      document.getElementById('bulkSyncError').hidden = true;
      document.getElementById('bulkSyncModal').classList.add('active');
      document.getElementById('bulkSyncNames').focus();
    }

    function closeBulkSyncModal() {
      document.getElementById('bulkSyncModal').classList.remove('active');
    }

    function submitBulkSync() {
      const names = parseSyncNames(document.getElementById('bulkSyncNames').value);
      if (!names.length) {
        document.getElementById('bulkSyncError').textContent = '请至少输入一个舰娘名称';
        document.getElementById('bulkSyncError').hidden = false;
        return;
      }
      closeBulkSyncModal();
      startSync(names);
    }

    function requestFullSync() {
      document.getElementById('fullSyncConfirmModal').classList.add('active');
    }

    function cancelFullSync() {
      document.getElementById('fullSyncConfirmModal').classList.remove('active');
    }

    function confirmFullSync() {
      cancelFullSync();
      startSync();
    }

    async function getNext(mode) {
      try {
        const params = new URLSearchParams({ mode, ship_type: filterState.ship_type });
        const result = await requestJson('/api/next?' + params.toString());
        renderNext(result.ship);
        showMessage(result.ship ? '已获取推荐目标' : '没有符合条件的数据');
      } catch (error) {
        showMessage(error.message, true);
      }
    }

    async function refreshSyncStatus() {
      try {
        const result = await requestJson('/api/sync/status');
        updateSyncModal(result);
        if (result.running) {
          setTimeout(refreshSyncStatus, 1000);
          return;
        }
        if (result.status === '完成') {
          await loadShips();
          if (!result.errors.length) setTimeout(() => closeSyncOverlay(), 2200);
        }
      } catch (error) {
        showMessage(error.message, true);
        if (document.getElementById('syncOverlay').classList.contains('active')) {
          setTimeout(refreshSyncStatus, 2000);
        }
      }
    }

    async function startSync(shipNames = null) {
      try {
        const targetNames = shipNames === null
          ? null
          : Array.isArray(shipNames)
            ? shipNames.map(name => String(name).trim()).filter(Boolean)
            : parseSyncNames(shipNames);
        if (shipNames !== null && !targetNames.length) {
          showMessage('请输入要同步的舰娘名称', true);
          return;
        }
        showSyncOverlay();
        document.body.classList.add('loading');
        document.getElementById('syncButton').disabled = true;
        await requestJson('/api/sync', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(targetNames ? { ship_names: targetNames } : {})
        });
        showMessage(
          targetNames
            ? `已启动 ${targetNames.length} 个舰娘同步`
            : '全量同步已启动'
        );
        refreshSyncStatus();
      } catch (error) {
        document.body.classList.remove('loading');
        document.getElementById('syncButton').disabled = false;
        document.getElementById('partialSyncButton').disabled = false;
        closeSyncOverlay();
        showMessage(error.message, true);
      }
    }

    function showSyncOverlay() {
      document.getElementById('syncOverlay').classList.add('active');
    }

    function closeSyncOverlay() {
      document.getElementById('syncOverlay').classList.remove('active');
    }

    function updateSyncModal(result) {
      const completed = result.processed + result.skipped + result.errors.length;
      const percent = result.total
        ? Math.round(completed / result.total * 100)
        : (result.status === '完成' ? 100 : 0);
      const errors = result.errors.map(error => `<li>${escapeHtml(error)}</li>`).join('');
      document.getElementById('syncMessage').textContent = result.message || result.status;
      document.getElementById('syncPercent').textContent = `${percent}%`;
      document.getElementById('syncProgressBar').style.width = `${percent}%`;
      document.getElementById('syncCount').textContent = `${completed} / ${result.total}`;
      document.getElementById('syncProcessed').textContent = result.processed;
      document.getElementById('syncSkipped').textContent = result.skipped;
      document.getElementById('syncErrorCount').textContent = result.errors.length;
      document.getElementById('syncErrors').innerHTML = errors;
      document.getElementById('syncErrors').hidden = !errors;
      document.getElementById('syncSpinner').hidden = !result.running;
      document.getElementById('syncClose').disabled = result.running;
      document.getElementById('syncButton').disabled = result.running;
      document.getElementById('partialSyncButton').disabled = result.running;
      document.body.classList.toggle('loading', result.running);
      if (result.running) showSyncOverlay();
    }

    async function importExcel(input) {
      const file = input.files[0];
      if (!file) return;
      const form = new FormData();
      form.append('file', file);
      try {
        const response = await fetch('/api/import', { method: 'POST', body: form });
        if (!response.ok) {
          const result = await response.json().catch(() => ({}));
          throw new Error(result.error || response.statusText);
        }
        const blob = await response.blob();
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = '导入处理结果.xlsx';
        link.click();
        URL.revokeObjectURL(link.href);
        showMessage('Excel 处理完成，已下载处理结果');
        await loadShips();
      } catch (error) {
        showMessage(error.message, true);
      } finally {
        input.value = '';
      }
    }

    function resetFilters() {
      filterState.name = '';
      filterState.ship_type = '';
      filterState.rarity = '';
      filterState.tech_attribute = '';
      filterState.needs_training = '';
      filterState.is_idling = '';
      filterState.is_trained = '否';
      document.getElementById('fName').value = '';
      renderFilters();
      loadShips();
    }

    async function initialize() {
      const ships = await requestJson('/api/ships');
      filterOptions.shipTypes = [...new Set(ships.ships.map(ship => ship.ship_type).filter(Boolean))].sort();
      filterOptions.rarities = [...new Set(ships.ships.map(ship => ship.rarity).filter(Boolean))];
      renderFilters();
      await loadShips();
      refreshSyncStatus();
    }

    document.getElementById('fName').addEventListener('input', event => {
      filterState.name = event.target.value.trim();
      clearTimeout(nameSearchTimer);
      nameSearchTimer = setTimeout(loadShips, 300);
    });

    document.getElementById('filterGroups').addEventListener('click', event => {
      const chip = event.target.closest('button.chip');
      if (!chip) return;
      filterState[chip.dataset.group] = chip.dataset.value;
      renderFilters();
      loadShips();
    });

    document.getElementById('shipTable').addEventListener('click', event => {
      const deleteButton = event.target.closest('button[data-delete]');
      if (deleteButton) {
        requestDeleteShip(Number(deleteButton.dataset.id), deleteButton.dataset.name);
        return;
      }
      const actionButton = event.target.closest('button[data-action]');
      if (!actionButton) return;
      completeShipStatus(Number(actionButton.dataset.id), actionButton.dataset.action);
    });

    initialize().catch(error => showMessage(error.message, true));
  </script>
</body>
</html>"""


class TrackerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BLHXTrainingTracker/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_bytes(
        self,
        data: bytes,
        status_code: int = 200,
        content_type: str = "application/octet-stream",
        filename: str | None = None,
    ) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{filename}"',
            )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            status_code,
            "application/json; charset=utf-8",
        )

    def send_error_json(self, message: str, status_code: int = 400) -> None:
        self.send_json({"error": message}, status_code)

    def read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length else b""

    def read_json(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.read_body().decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("请求体必须是有效的 JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def query_filters(self) -> dict[str, str]:
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        keys = [
            "name",
            "ship_type",
            "rarity",
            "tech_attribute",
            "needs_training",
            "is_idling",
            "is_trained",
        ]
        return {key: query.get(key, [""])[0].strip() for key in keys}

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/":
                self.send_bytes(
                    PAGE_HTML.encode("utf-8"),
                    content_type="text/html; charset=utf-8",
                )
            elif path == "/api/ships":
                self.send_json({"ships": list_ships(self.query_filters())})
            elif path == "/api/next":
                query = parse_qs(urlsplit(self.path).query)
                mode = query.get("mode", ["idle"])[0]
                ship_type = query.get("ship_type", [""])[0]
                self.send_json({"ship": next_ship(mode, ship_type)})
            elif path == "/api/sync/status":
                self.send_json(
                    {
                        "running": SYNC_STATE["running"],
                        "status": SYNC_STATE["status"],
                        "message": SYNC_STATE["message"],
                        "total": SYNC_STATE["total"],
                        "processed": SYNC_STATE["processed"],
                        "skipped": SYNC_STATE["skipped"],
                        "errors": list(SYNC_STATE["errors"]),
                    }
                )
            elif path == "/api/template":
                self.send_bytes(
                    import_template_bytes(),
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    filename="blhx-training-import-template.xlsx",
                )
            else:
                self.send_error_json("Not Found", 404)
        except Exception as error:
            LOGGER.exception("处理 GET 请求失败: %s", self.path)
            self.send_error_json(str(error), 500)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/api/sync":
                payload = self.read_json()
                ship_names = payload.get("ship_names")
                if ship_names is None and payload.get("ship_name") is not None:
                    ship_names = [payload.get("ship_name")]
                started = start_sync(ship_names)
                if started:
                    self.send_json({"started": True})
                else:
                    self.send_json({"started": False, "message": "同步正在进行中"})
            elif path == "/api/import":
                body = self.read_body()
                uploaded = parse_multipart_file(
                    self.headers.get("Content-Type", ""), body
                )
                if uploaded is None:
                    self.send_error_json("请上传 Excel 文件")
                    return
                report_bytes, updated_count, skipped_count = import_excel(uploaded.data)
                self.send_bytes(
                    report_bytes,
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    filename="blhx-training-import-result.xlsx",
                )
            elif path == "/api/ship/status":
                payload = self.read_json()
                ship_id = int(payload.get("id", 0))
                field = str(payload.get("field", ""))
                updated_ship = update_ship_status(
                    ship_id, field, payload.get("value", False)
                )
                if updated_ship is None:
                    self.send_error_json("舰船不存在", 404)
                    return
                self.send_json({"ship": updated_ship})
            elif path == "/api/ship/delete":
                payload = self.read_json()
                deleted = delete_ship(int(payload.get("id", 0)))
                if not deleted:
                    self.send_error_json("舰船不存在", 404)
                    return
                self.send_json({"deleted": True})
            elif path == "/api/ships/clear":
                self.read_json()
                cleared_count = clear_ships()
                self.send_json({"cleared": True, "count": cleared_count})
            else:
                self.send_error_json("Not Found", 404)
        except Exception as error:
            LOGGER.exception("处理 POST 请求失败: %s", self.path)
            self.send_error_json(str(error), 400)


def main() -> None:
    init_db()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer((host, port), TrackerHandler)
    print(f"碧蓝航线练级数据记录已启动: http://localhost:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
