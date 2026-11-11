from __future__ import annotations

import csv
import io
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import unquote

from ..storage.db import get_connection, init_db
from ..storage.history import record_operation
from ..core.state import IMPORT_LOCK, IMPORT_STATE
from ..core.utils import utc_now_text

CSV_ENCODING = "utf-8-sig"
CSV_CONTENT_TYPE = "text/csv; charset=utf-8"

IMPORT_HEADERS = ["舰船名称", "是否已挂机", "是否已练级完成"]
REPORT_HEADERS = ["舰船名称", "是否已挂机", "是否已练级完成", "处理状态", "处理说明"]


class CsvFormatError(ValueError):
    """CSV 编码或语法不合法，调用方应作为用户输入错误返回。"""


def decode_csv_text(data: bytes) -> str:
    try:
        return data.decode(CSV_ENCODING)
    except UnicodeDecodeError as error:
        raise CsvFormatError("CSV 文件编码无法识别，请使用 UTF-8 编码") from error


def csv_read(data: bytes) -> list[list[str]]:
    text = decode_csv_text(data).lstrip("\ufeff")
    if not text.strip():
        return []
    try:
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        return [
            [str(value).strip() for value in row]
            for row in rows
        ]
    except csv.Error as error:
        raise CsvFormatError(f"CSV 格式错误：{error}") from error


def csv_line(values: list[Any]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow(
        ["" if value is None else value for value in values]
    )
    return buffer.getvalue()


def csv_write(rows: list[list[Any]]) -> bytes:
    text = "".join(csv_line(row) for row in rows)
    return text.encode(CSV_ENCODING)


def import_template_bytes() -> bytes:
    return csv_write([list(IMPORT_HEADERS)])


def parse_workbook_rows(rows: list[list[str]]) -> list[dict[str, str]]:
    if not rows:
        raise CsvFormatError("CSV 文件为空")
    headers = [str(value).strip() for value in rows[0]]
    required_headers = set(IMPORT_HEADERS)
    seen: set[str] = set()
    duplicates = []
    for header in headers:
        if not header:
            continue
        if header in seen and header not in duplicates:
            duplicates.append(header)
        seen.add(header)
    if duplicates:
        raise CsvFormatError(
            "CSV 第一行存在重复列名：" + "、".join(duplicates)
        )
    if not required_headers.issubset(seen):
        raise CsvFormatError("CSV 第一行必须包含：舰船名称、是否已挂机、是否已练级完成")

    parsed_rows: list[dict[str, str]] = []
    for row in rows[1:]:
        if not any(str(value).strip() for value in row):
            continue
        item = {
            header: str(row[index]).strip() if index < len(row) else ""
            for index, header in enumerate(headers)
            if header
        }
        parsed_rows.append(item)
    return parsed_rows


def process_import_rows(
    rows: list[dict[str, str]],
    on_progress: Callable[[int, int, dict[str, str], str], None] | None = None,
) -> tuple[list[dict[str, str]], int, int]:
    init_db()
    with get_connection() as connection:
        ship_rows = connection.execute(
            "SELECT id, name, ship_type, is_idling, is_trained FROM ships"
        ).fetchall()
        ships_by_name = {row["name"]: row for row in ship_rows}

    report_rows: list[dict[str, str]] = []
    updated_count = 0
    skipped_count = 0
    valid_values = {"是", "否"}

    total = len(rows)
    for index, row in enumerate(rows, start=1):
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
                ship = ships_by_name[ship_name]
                record_operation(
                    connection,
                    "import_status",
                    f"CSV 导入状态：已挂机={idling_value}，已练级完成={trained_value}",
                    ship_id=ship["id"],
                    ship_name=ship["name"],
                    ship_type=ship["ship_type"],
                )
            updated_count += 1
            status = "处理完成"
            message = "处理完成"

        if status != "处理完成":
            skipped_count += 1
        report_row = {
            "舰船名称": ship_name,
            "是否已挂机": idling_value,
            "是否已练级完成": trained_value,
            "处理状态": status,
            "处理说明": message,
        }
        report_rows.append(report_row)
        if on_progress is not None:
            on_progress(index, total, report_row, message if status != "处理完成" else f"已处理 {ship_name}")

    return report_rows, updated_count, skipped_count


def build_import_report(report_rows: list[dict[str, str]]) -> bytes:
    report: list[list[str]] = [list(REPORT_HEADERS)]
    for row in report_rows:
        report.append([row[header] for header in REPORT_HEADERS])
    return csv_write(report)


def import_csv(data: bytes) -> tuple[bytes, int, int]:
    rows = parse_workbook_rows(csv_read(data))
    report_rows, updated_count, skipped_count = process_import_rows(rows)
    return build_import_report(report_rows), updated_count, skipped_count


def reset_import_state() -> None:
    """在锁内重置导入状态；调用方若已持有 IMPORT_LOCK 会死锁，改用 _reset_locked。"""
    with IMPORT_LOCK:
        _reset_locked()


def _reset_locked() -> None:
    IMPORT_STATE.update(
        {
            "running": False,
            "status": "未开始",
            "message": "",
            "total": 0,
            "processed": 0,
            "skipped": 0,
            "errors": [],
            "result": b"",
        }
    )


def _snapshot_locked() -> dict[str, Any]:
    return {
        "running": IMPORT_STATE["running"],
        "status": IMPORT_STATE["status"],
        "message": IMPORT_STATE["message"],
        "total": IMPORT_STATE["total"],
        "processed": IMPORT_STATE["processed"],
        "skipped": IMPORT_STATE["skipped"],
        "errors": list(IMPORT_STATE["errors"]),
        "updated": IMPORT_STATE["processed"],
    }


def import_status_snapshot() -> dict[str, Any]:
    with IMPORT_LOCK:
        return _snapshot_locked()


def import_result_bytes() -> tuple[bytes, str]:
    """返回 (结果, 状态)；running/未完成时抛 ValueError，由路由映射为 409。"""
    with IMPORT_LOCK:
        if IMPORT_STATE["running"]:
            raise ValueError("导入尚未完成，请稍后再下载")
        if IMPORT_STATE["status"] != "完成":
            raise LookupError("暂无可下载的导入结果")
        return bytes(IMPORT_STATE["result"]), str(IMPORT_STATE["status"])


def start_import(data: bytes, filename: str = "") -> dict[str, Any]:
    """校验文件后启动后台导入任务；校验失败直接抛 CsvFormatError。"""
    rows = parse_workbook_rows(csv_read(data))
    with IMPORT_LOCK:
        if IMPORT_STATE["running"]:
            raise ValueError("导入正在进行中，请稍后再试")
        _reset_locked()
        IMPORT_STATE.update(
            {
                "running": True,
                "status": "导入中",
                "message": f"正在导入 {filename}" if filename else "正在导入 CSV",
                "total": len(rows),
            }
        )
    thread = threading.Thread(
        target=_run_import,
        args=(rows,),
        name="csv-import",
        daemon=True,
    )
    thread.start()
    return import_status_snapshot()


def _run_import(rows: list[dict[str, str]]) -> None:
    report_rows_seen: list[dict[str, str]] = []

    def track(index: int, total: int, report_row: dict[str, str], message: str) -> None:
        report_rows_seen.append(report_row)
        updated = sum(
            1 for row in report_rows_seen if row["处理状态"] == "处理完成"
        )
        with IMPORT_LOCK:
            IMPORT_STATE["processed"] = updated
            IMPORT_STATE["skipped"] = index - updated
            IMPORT_STATE["message"] = message
            IMPORT_STATE["total"] = total

    try:
        report_rows, updated_count, skipped_count = process_import_rows(rows, track)
        result_bytes = build_import_report(report_rows)
        with IMPORT_LOCK:
            IMPORT_STATE["result"] = result_bytes
            IMPORT_STATE["processed"] = updated_count
            IMPORT_STATE["skipped"] = skipped_count
            IMPORT_STATE["status"] = "完成"
            IMPORT_STATE["message"] = (
                f"导入完成：更新 {updated_count} 条，跳过 {skipped_count} 条"
            )
            IMPORT_STATE["running"] = False
    except Exception as error:
        with IMPORT_LOCK:
            IMPORT_STATE["status"] = "失败"
            IMPORT_STATE["message"] = str(error)
            IMPORT_STATE["errors"].append(str(error))
            IMPORT_STATE["running"] = False


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
        filename_match = re.search(
            r'filename\*?=(?:"([^"]*)"|([^;\r\n]+))', disposition, re.IGNORECASE
        )
        if not filename_match:
            continue
        filename = (filename_match.group(1) or filename_match.group(2)).strip()
        if filename.startswith("UTF-8''"):
            filename = unquote(filename[7:])
        return UploadedFile(filename=filename, data=file_data)
    return None
