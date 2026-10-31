from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

from .db import get_connection, init_db
from .history import record_operation
from .utils import utc_now_text


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
        .replace(chr(38), chr(38) + "amp;")
        .replace("<", chr(38) + "lt;")
        .replace(">", chr(38) + "gt;")
        .replace('"', chr(38) + "quot;")
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
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships" Target="officeDocument"/>
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
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="sheet1.xml"/>
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
                    f"Excel 导入状态：已挂机={idling_value}，已练级完成={trained_value}",
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
