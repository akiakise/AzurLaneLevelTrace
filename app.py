from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import zipfile
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATABASE_PATH = DATA_DIR / "blhx-training.sqlite3"
WIKI_BASE_URL = os.getenv("WIKI_BASE_URL", "https://wiki.biligame.com").rstrip("/")
WIKI_REQUEST_INTERVAL = float(os.getenv("WIKI_REQUEST_INTERVAL", "0.12"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "BLHX-Training-Tracker/1.0 (+https://github.com/)",
)

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
    "解锁图鉴": ("解锁图鉴科技点", "解锁图鉴属性加成"),
    "突破至满星": ("突破至满星科技点", ""),
    "达到120级": ("达到120级科技点", "达到120级属性加成"),
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


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
            """
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


def insert_ship(
    connection: sqlite3.Connection,
    name: str,
    release_date: str,
    parsed_ship: dict[str, Any],
) -> sqlite3.Row:
    current_time = utc_now_text()
    wiki_url = f"{WIKI_BASE_URL}/blhx/{quote(name, safe='')}"
    needs_training = 1 if parsed_ship["tech_points"] else 0
    connection.execute(
        """
        INSERT INTO ships (
            name, ship_type, rarity, release_date, needs_training,
            is_idling, is_trained, wiki_url, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
        ON CONFLICT(name) DO NOTHING
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
            fields[key.strip()] = value.strip()
    return fields


def integer_from_text(value: str) -> int | None:
    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


def attribute_from_text(value: str) -> str:
    match = re.search(r"(装填|命中|炮击|防空|耐久|航空|雷击|机动)", value)
    return match.group(1) if match else ""


def parse_ship_raw(raw_text: str) -> dict[str, Any] | None:
    fields = parse_template_fields(raw_text)
    if not fields:
        return None
    tech_points: list[dict[str, Any]] = []
    for stage, (points_key, attribute_key) in TECH_STAGE_FIELDS.items():
        points = integer_from_text(fields.get(points_key, ""))
        if points is None or points <= 0:
            continue
        attribute = attribute_from_text(fields.get(attribute_key, "")) if attribute_key else ""
        tech_points.append({"stage": stage, "attribute": attribute, "points": points})
    return {
        "ship_type": fields.get("类型", ""),
        "rarity": fields.get("稀有度", ""),
        "tech_points": tech_points,
    }


def fetch_wiki_text(path: str) -> str:
    url = f"{WIKI_BASE_URL}{path}"
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urlopen(request, timeout=25) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_release_entries() -> list[dict[str, str]]:
    html_text = fetch_wiki_text("/blhx/%E5%AE%9E%E8%A3%85%E6%97%A5%E6%9C%9F")
    return extract_ship_entries(html_text)


def fetch_ship_raw(ship_name: str) -> str:
    encoded_name = quote(ship_name, safe="")
    return fetch_wiki_text(f"/blhx/index.php?title={encoded_name}&action=raw")


def sync_wiki() -> None:
    with SYNC_LOCK:
        if SYNC_STATE["running"]:
            return
        SYNC_STATE.update(
            {
                "running": True,
                "status": "同步中",
                "message": "正在读取实装日期页面",
                "total": 0,
                "processed": 0,
                "skipped": 0,
                "errors": [],
            }
        )

    try:
        entries = fetch_release_entries()
        SYNC_STATE["total"] = len(entries)
        SYNC_STATE["message"] = "已读取实装日期页面"
        init_db()
        with get_connection() as connection:
            existing_names = {
                row["name"]
                for row in connection.execute("SELECT name FROM ships").fetchall()
            }
            for entry in entries:
                ship_name = entry["name"]
                if ship_name in existing_names:
                    SYNC_STATE["skipped"] += 1
                    continue
                try:
                    raw_text = fetch_ship_raw(ship_name)
                    parsed_ship = parse_ship_raw(raw_text)
                    if parsed_ship is None:
                        SYNC_STATE["errors"].append(f"{ship_name}: 未找到舰娘图鉴模板")
                        continue
                    insert_ship(connection, ship_name, entry["release_date"], parsed_ship)
                    existing_names.add(ship_name)
                    SYNC_STATE["processed"] += 1
                except Exception as error:
                    SYNC_STATE["errors"].append(f"{ship_name}: {error}")
                finally:
                    time.sleep(WIKI_REQUEST_INTERVAL)
        SYNC_STATE["status"] = "完成"
        SYNC_STATE["message"] = "同步完成"
    except Exception as error:
        SYNC_STATE["status"] = "失败"
        SYNC_STATE["message"] = str(error)
    finally:
        SYNC_STATE["running"] = False


def start_sync() -> bool:
    with SYNC_LOCK:
        if SYNC_STATE["running"]:
            return False
    threading.Thread(target=sync_wiki, daemon=True).start()
    return True


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
    if filters.get("tech_point"):
        keyword = f"%{filters['tech_point']}%"
        conditions.append(
            """
            EXISTS (
                SELECT 1 FROM tech_points tp
                WHERE tp.ship_id = s.id
                  AND (tp.stage LIKE ? OR tp.attribute LIKE ?)
            )
            """
        )
        parameters.extend([keyword, keyword])

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with get_connection() as connection:
        rows = connection.execute(
            f"SELECT s.* FROM ships s {where_clause} ORDER BY s.name COLLATE NOCASE",
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
        attribute = tech_point["attribute"] or "其他"
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
    .container { max-width:1500px; margin:0 auto; padding:28px 24px 60px; }
    header { display:flex; justify-content:space-between; align-items:center; gap:16px; margin-bottom:22px; }
    h1 { font-size:26px; margin:0; }
    .subtitle { color:var(--muted); margin-top:6px; }
    .toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
    button,.button { border:1px solid #35507a; background:var(--panel2); color:var(--text); padding:10px 16px; border-radius:8px; cursor:pointer; text-decoration:none; font-size:14px; transition:.2s; }
    button:hover,.button:hover { border-color:var(--blue); color:var(--blue); }
    button.primary { background:#0891b2; border-color:#0891b2; color:white; }
    button.primary:hover { background:#06b6d4; color:white; }
    .card { background:linear-gradient(180deg,var(--panel),#131d34); border:1px solid var(--line); border-radius:14px; padding:20px; box-shadow:0 8px 28px rgba(0,0,0,.18); }
    .filters { display:grid; grid-template-columns:repeat(6,minmax(140px,1fr)); gap:12px; margin:18px 0; }
    label { display:block; color:var(--muted); font-size:12px; margin-bottom:6px; }
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
    @media (max-width:1100px) { .filters { grid-template-columns:repeat(3,minmax(130px,1fr)); } }
    @media (max-width:680px) { .filters { grid-template-columns:1fr; } header { display:block; } .toolbar { margin-top:14px; } th,td { padding:9px 6px; } }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>碧蓝航线练级数据记录</h1>
        <div class="subtitle">Wiki 同步 · Excel 导入 · 挂机与练级排序</div>
      </div>
      <div class="toolbar">
        <button class="primary" onclick="startSync()">Wiki 同步</button>
        <a class="button" href="/api/template">下载导入模板</a>
        <button onclick="document.getElementById('importFile').click()">导入 Excel</button>
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
      <div class="filters">
        <div><label>舰船名称</label><input id="fName" placeholder="名称关键字"></div>
        <div><label>舰种</label><select id="fType"><option value="">全部</option></select></div>
        <div><label>稀有度</label><select id="fRarity"><option value="">全部</option></select></div>
        <div><label>科技点 / 属性</label><input id="fTech" placeholder="如：耐久、解锁图鉴"></div>
        <div><label>是否需要练级</label><select id="fNeed"><option value="">全部</option><option>是</option><option>否</option></select></div>
        <div><label>是否已挂机</label><select id="fIdle"><option value="">全部</option><option>是</option><option>否</option></select></div>
        <div><label>是否已练级完成</label><select id="fTrained"><option value="">全部</option><option>是</option><option>否</option></select></div>
        <div style="align-items:end;display:flex;gap:8px;"><button class="primary" onclick="loadShips()">查询</button><button onclick="resetFilters()">重置</button></div>
      </div>
      <div id="message" class="status-line"></div>
      <table>
        <thead><tr><th>舰船名称</th><th>舰种</th><th>稀有度</th><th>科技点</th><th>实装日期</th><th>是否需要练级</th><th>是否已挂机</th><th>是否已练级完成</th></tr></thead>
        <tbody id="shipTable"><tr><td colspan="8" class="empty">加载中...</td></tr></tbody>
      </table>
    </div>
  </div>

  <script>
    let shipData = [];
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
        name: document.getElementById('fName').value.trim(),
        ship_type: document.getElementById('fType').value,
        rarity: document.getElementById('fRarity').value,
        tech_point: document.getElementById('fTech').value.trim(),
        needs_training: document.getElementById('fNeed').value,
        is_idling: document.getElementById('fIdle').value,
        is_trained: document.getElementById('fTrained').value
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
        tbody.innerHTML = '<tr><td colspan="8" class="empty">没有匹配的数据</td></tr>';
        return;
      }
      tbody.innerHTML = shipData.map(ship => {
        const tech = ship.tech_points.map(item =>
          `<span class="tech">${escapeHtml(item.stage)}${item.attribute ? '｜' + escapeHtml(item.attribute) : ''}｜+${escapeHtml(item.points)}</span>`
        ).join('') || '<span class="empty">无</span>';
        return `<tr>
          <td><a href="${escapeHtml(ship.wiki_url)}" target="_blank" style="color:#e6edf7;">${escapeHtml(ship.name)}</a></td>
          <td>${escapeHtml(ship.ship_type || '')}</td>
          <td>${escapeHtml(ship.rarity || '')}</td>
          <td><div class="tech-list">${tech}</div></td>
          <td>${escapeHtml(ship.release_date || '')}</td>
          <td class="${ship.needs_training ? 'yes' : 'empty'}">${ship.needs_training ? '是' : '否'}</td>
          <td class="${statusClass(ship.is_idling)}">${statusText(ship.is_idling)}</td>
          <td class="${statusClass(ship.is_trained)}">${statusText(ship.is_trained)}</td>
        </tr>`;
      }).join('');
    }

    function renderNext(ship) {
      const target = document.getElementById('nextTarget');
      if (!ship) {
        target.innerHTML = '<span class="empty">没有符合条件的数据</span>';
        return;
      }
      const tech = ship.tech_points.map(item => `${item.stage}${item.attribute ? '｜' + item.attribute : ''}｜+${item.points}`).join('、');
      target.innerHTML = `
        <div class="ship-name">${escapeHtml(ship.name)}</div>
        <div class="ship-meta">${escapeHtml(ship.ship_type || '')} · ${escapeHtml(ship.rarity || '')} · ${escapeHtml(tech || '无科技点')}</div>`;
    }

    async function getNext(mode) {
      try {
        const params = new URLSearchParams({ mode, ship_type: document.getElementById('fType').value });
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
        const errors = result.errors.length ? `；错误 ${result.errors.length} 条` : '';
        showMessage(`${result.status}｜总数 ${result.total}｜新增 ${result.processed}｜跳过 ${result.skipped}${errors}`);
        if (result.running) setTimeout(refreshSyncStatus, 1200);
        if (!result.running && result.status === '完成') await loadShips();
      } catch (error) {
        showMessage(error.message, true);
      }
    }

    async function startSync() {
      try {
        await requestJson('/api/sync', { method: 'POST' });
        showMessage('同步已启动');
        refreshSyncStatus();
      } catch (error) {
        showMessage(error.message, true);
      }
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
      ['fName','fTech'].forEach(id => document.getElementById(id).value = '');
      ['fType','fRarity','fNeed','fIdle','fTrained'].forEach(id => document.getElementById(id).value = '');
      loadShips();
    }

    async function initialize() {
      const ships = await requestJson('/api/ships');
      const types = [...new Set(ships.ships.map(ship => ship.ship_type).filter(Boolean))].sort();
      const rarities = [...new Set(ships.ships.map(ship => ship.rarity).filter(Boolean))];
      document.getElementById('fType').innerHTML = '<option value="">全部</option>' + types.map(type => `<option>${escapeHtml(type)}</option>`).join('');
      document.getElementById('fRarity').innerHTML = '<option value="">全部</option>' + rarities.map(rarity => `<option>${escapeHtml(rarity)}</option>`).join('');
      await loadShips();
      refreshSyncStatus();
    }

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

    def query_filters(self) -> dict[str, str]:
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        keys = [
            "name",
            "ship_type",
            "rarity",
            "tech_point",
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
            self.send_error_json(str(error), 500)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/api/sync":
                self.read_body()
                started = start_sync()
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
            else:
                self.send_error_json("Not Found", 404)
        except Exception as error:
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
