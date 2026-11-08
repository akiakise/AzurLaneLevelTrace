from __future__ import annotations

import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode
from urllib.request import Request, urlopen

from ..core.constants import (
    LOGGER,
    normalize_ship_type,
    TECH_STAGE_FIELDS,
    USER_AGENT,
    WIKI_API_PATH,
    WIKI_BASE_URL,
    WIKI_CONCURRENCY,
    WIKI_NEXT_REQUEST_AT,
    WIKI_REQUEST_INTERVAL,
    WIKI_REQUEST_LOCK,
    WIKI_RETRY_COUNT,
    WIKI_RETRY_DELAY,
)
from ..storage.db import get_connection, init_db
from ..domain.ships import insert_ship
from ..core.state import SYNC_LOCK, SYNC_STATE
from ..core.utils import clean_template_value


def acquire_wiki_request_slot() -> None:
    global WIKI_NEXT_REQUEST_AT
    current_time = time.monotonic()
    with WIKI_REQUEST_LOCK:
        wait_seconds = max(0.0, WIKI_NEXT_REQUEST_AT - current_time)
        WIKI_NEXT_REQUEST_AT = max(current_time, WIKI_NEXT_REQUEST_AT) + WIKI_REQUEST_INTERVAL
    if wait_seconds:
        time.sleep(wait_seconds)


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


def parse_ship_raw(raw_text: str) -> dict[str, Any] | None:
    from ..domain.tech import attribute_bonus_from_text

    fields = parse_template_fields(raw_text)
    if not fields:
        return None
    tech_points: list[dict[str, Any]] = []
    for stage, attribute_key in TECH_STAGE_FIELDS.items():
        attribute = attribute_bonus_from_text(fields.get(attribute_key, ""))
        if attribute:
            tech_points.append({"stage": stage, "attribute": attribute, "points": 0})
    return {
        "ship_type": normalize_ship_type(fields.get("类型", "")),
        "rarity": fields.get("稀有度", ""),
        "tech_points": tech_points,
    }


# 兵装页面：从「已实装角色一览」到「联动兵装为活动赠送」之间的列表，
# 每个 <li> 为一批，行首显示具体日期的是常规兵装，显示「XX联动」的是活动赠送的联动兵装。
ARMAMENT_WIKI_TITLE = "特殊兵装"
ARMAMENT_SECTION_START = "已实装角色一览"
ARMAMENT_SECTION_END = "联动兵装为活动赠送"
ARMAMENT_ROW_PATTERN = re.compile(
    r'<li>\s*<a href="/blhx/(?P<href>[^"?#]+)"[^>]*>(?P<label>.*?)</a>：(?P<body>.*?)</li>',
    re.DOTALL,
)
# 每艘舰船由同一页面的两个 <a> 组成：第一个带头像图，第二个的 span.AF 是兵装页显示名。
# title 属性始终是舰船原始名称（例如显示「简·布罗伊」时 title 为「让·巴尔」），
# 因此不需要再逐舰请求舰娘页面，直接取 title 即可。
ARMAMENT_SHIP_PATTERN = re.compile(
    r'<span class="xtb-image">\s*'
    r'<a href="/blhx/(?P<href>[^"?#]+)" title="(?P<title>[^"]*)">.*?</a>\s*'
    r'<a href="/blhx/[^"?#]+" title="[^"]*">\s*'
    r'<span class="nowrap"[^>]*>\s*<span class="AF">(?P<display>[^<]*)</span>',
    re.DOTALL,
)
ARMAMENT_DATE_PATTERN = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def slice_armament_section(html_text: str) -> str:
    start = html_text.find(f">{ARMAMENT_SECTION_START}<")
    if start < 0:
        raise ValueError(f"兵装页面未找到「{ARMAMENT_SECTION_START}」")
    end = html_text.find(ARMAMENT_SECTION_END, start)
    if end < 0:
        raise ValueError(f"兵装页面未找到「{ARMAMENT_SECTION_END}」")
    return html_text[start:end]


def parse_armament_entries(html_text: str) -> list[dict[str, Any]]:
    """解析兵装页面的「已实装角色一览」，按 Wiki 列表顺序返回兵装条目。

    常规兵装带具体实装日期；联动兵装没有日期，用 collab_group 记录所属联动、
    collab_order 记录在联动段内的先后顺序（1 起）。
    """
    entries: list[dict[str, Any]] = []
    collab_order = 0
    for row in ARMAMENT_ROW_PATTERN.finditer(slice_armament_section(html_text)):
        label = _strip_tags(row.group("label"))
        body = row.group("body")
        date_match = ARMAMENT_DATE_PATTERN.fullmatch(label)
        release_date = (
            f"{date_match.group(1)}-{int(date_match.group(2)):02d}-"
            f"{int(date_match.group(3)):02d}"
            if date_match
            else ""
        )
        collab_group = "" if date_match else label
        for ship in ARMAMENT_SHIP_PATTERN.finditer(body):
            ship_name = ship.group("title").strip()
            if not ship_name:
                continue
            if collab_group:
                collab_order += 1
            entries.append(
                {
                    "name": ship_name,
                    "release_date": release_date,
                    "is_collab": not date_match,
                    "collab_group": collab_group,
                    "collab_order": collab_order if collab_group else 0,
                }
            )
    return entries


def fetch_armament_entries() -> list[dict[str, Any]]:
    html_text = fetch_wiki_text(f"/blhx/{quote(ARMAMENT_WIKI_TITLE)}")
    return parse_armament_entries(html_text)


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


COLLAB_CATEGORY_TITLE = "Category:联动舰娘"


def fetch_collab_ship_names() -> set[str]:
    """读取「分类:联动舰娘」的全部成员名称，用于打联动标签。

    用 MediaWiki 的 categorymembers 接口按标题取成员，比解析分类页面 HTML 稳定：
    分类页是导航壳，成员列表由皮肤渲染，且部分舰船标题带重定向。
    """
    names: set[str] = set()
    continuation: dict[str, Any] = {}
    while True:
        payload = fetch_wiki_api(
            {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": COLLAB_CATEGORY_TITLE,
                "cmlimit": "500",
                "format": "json",
                "formatversion": "2",
                **continuation,
            }
        )
        members = payload.get("query", {}).get("categorymembers", [])
        if not isinstance(members, list):
            LOGGER.error("Wiki API 联动分类响应结构异常，原始返回：%s", payload)
            raise ValueError("Wiki API 联动分类响应结构异常")
        for member in members:
            title = str(member.get("title", "")).strip()
            if title:
                names.add(unquote(title.replace("_", " ")))
        # 分类成员超过 500 时分页续取。
        next_token = payload.get("continue")
        if not isinstance(next_token, dict) or not next_token:
            break
        continuation = next_token
    return names


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
                "armaments": 0,
                "errors": [],
            }
        )
        return True


def _refresh_ship_tags(connection, target_names: list[str] | None) -> None:
    """重算 ships.tags；少量同步只刷新本次同步的舰船。

    联动标签的成员来自 Wiki「分类:联动舰娘」，无法用本地字段推导，
    因此在这里抓取名称集合并传给 refresh_ship_tags()。抓取失败时该标签
    按不命中处理，不让整次同步失败。
    """
    from ..core.constants import COLLAB_SHIP_TAG
    from ..domain.ships import refresh_ship_tags

    category_tags: dict[str, set[str]] = {}
    SYNC_STATE["message"] = "正在读取联动舰娘分类"
    try:
        category_tags[COLLAB_SHIP_TAG] = fetch_collab_ship_names()
    except Exception as error:
        LOGGER.exception("读取联动舰娘分类失败")
        SYNC_STATE["errors"].append(f"联动分类: {error}")

    try:
        if target_names is None:
            refresh_ship_tags(connection, category_tags=category_tags)
            return
        ship_ids = [
            row["id"]
            for row in connection.execute(
                f"SELECT id FROM ships WHERE name IN ({','.join('?' for _ in target_names)})",
                target_names,
            ).fetchall()
        ]
        for ship_id in ship_ids:
            refresh_ship_tags(connection, ship_id, category_tags=category_tags)
    except Exception as error:
        # 标签刷新失败不应让整次同步报错，数据字段本身已写入。
        LOGGER.exception("刷新舰船标签失败")
        SYNC_STATE["errors"].append(f"标签刷新: {error}")


def _sync_armaments(
    connection,
    target_names: list[str] | None = None,
) -> None:
    """同步兵装数据；少量同步只更新这几个舰娘的兵装。

    兵装同步失败不应让整次舰船同步报错，因此单独 try 并把错误写进同步错误列表。
    """
    from ..domain.armaments import upsert_armament

    SYNC_STATE["message"] = "正在读取兵装页面"
    try:
        entries = fetch_armament_entries()
    except Exception as error:
        LOGGER.exception("读取兵装页面失败")
        SYNC_STATE["errors"].append(f"兵装页面: {error}")
        return
    if target_names is not None:
        target_set = set(target_names)
        entries = [entry for entry in entries if entry["name"] in target_set]
    ship_rows = connection.execute("SELECT id, name FROM ships").fetchall()
    ship_ids = {row["name"]: row["id"] for row in ship_rows}
    matched = 0
    missing: set[str] = set()
    for entry in entries:
        ship_id = ship_ids.get(entry["name"])
        if ship_id is None:
            missing.add(entry["name"])
            continue
        upsert_armament(connection, ship_id, entry)
        matched += 1
    if missing:
        # 兵装页面比舰船库多出舰船（例如舰船缺少实装日期而未入库）只提示，不当作同步错误，
        # 否则每次全量同步都会因为这一条让弹窗停在错误状态。
        LOGGER.warning(
            "兵装页面中的 %s 艘舰船未匹配到舰船库：%s",
            len(missing),
            "、".join(sorted(missing)),
        )
    SYNC_STATE["armaments"] = matched
    SYNC_STATE["message"] = f"已同步 {matched} 条兵装数据"
    if missing:
        SYNC_STATE["message"] += f"，{len(missing)} 条未匹配到舰船"


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
            # 全量同步顺带重算全部标签：存量数据升级后 tags 为空，
            # 跑一次全量同步即可补齐；少量同步只刷本次涉及的舰船。
            _refresh_ship_tags(connection, target_names)
            _sync_armaments(connection, target_names)
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
