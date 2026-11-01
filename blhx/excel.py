from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from urllib.parse import unquote

from .db import get_connection, init_db
from .history import record_operation
from .utils import utc_now_text

CSV_ENCODING = "utf-8-sig"
CSV_CONTENT_TYPE = "text/csv; charset=utf-8"

IMPORT_HEADERS = ["舰船名称", "是否已挂机", "是否已练级完成"]
REPORT_HEADERS = ["舰船名称", "是否已挂机", "是否已练级完成", "处理状态", "处理说明"]


class CsvFormatError(ValueError):
    """CSV 编码或语法不合法，调用方应作为用户输入错误返回。"""


def decode_csv_text(data: bytes) -> str:
    for encoding in (CSV_ENCODING, "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvFormatError("CSV 文件编码无法识别，请使用 UTF-8 或 GB18030 编码")


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


def import_csv(data: bytes) -> tuple[bytes, int, int]:
    rows = parse_workbook_rows(csv_read(data))
    report_rows, updated_count, skipped_count = process_import_rows(rows)
    report: list[list[str]] = [list(REPORT_HEADERS)]
    for row in report_rows:
        report.append([row[header] for header in REPORT_HEADERS])
    return csv_write(report), updated_count, skipped_count


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
