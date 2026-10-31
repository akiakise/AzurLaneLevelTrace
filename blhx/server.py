from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import read_config, save_next_ship_types
from .constants import LOGGER
from .db import init_db
from .excel import import_excel, import_template_bytes, parse_multipart_file
from .history import list_operations
from .page import PAGE_HTML
from .recommendations import available_next_ship_types, next_ship, next_ships_by_type
from .ships import clear_ships, delete_ship, list_ships, update_ship_status
from .state import SYNC_STATE
from .wiki import start_sync


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

    def next_query_ship_types(self) -> list[str]:
        query = parse_qs(urlsplit(self.path).query)
        ship_type = query.get("ship_type", [""])[0]
        ship_types = [
            value.strip()
            for value in query.get("ship_types", [])
            if value.strip()
        ]
        if not ship_types and ship_type:
            ship_types = [ship_type.strip()]
        return ship_types

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
            elif path == "/api/config":
                self.send_json(read_config())
            elif path == "/api/next-options":
                self.send_json({"ship_types": available_next_ship_types()})
            elif path == "/api/next":
                query = parse_qs(urlsplit(self.path).query)
                mode = query.get("mode", ["idle"])[0]
                self.send_json(
                    {
                        "ship": next_ship(
                            mode,
                            ship_types=self.next_query_ship_types(),
                        )
                    }
                )
            elif path == "/api/next-group":
                query = parse_qs(urlsplit(self.path).query)
                mode = query.get("mode", ["both"])[0]
                self.send_json(
                    {
                        "rows": next_ships_by_type(
                            mode,
                            self.next_query_ship_types(),
                        )
                    }
                )
            elif path == "/api/operations":
                query = parse_qs(urlsplit(self.path).query)
                limit = int(query.get("limit", ["200"])[0])
                self.send_json({"operations": list_operations(limit)})
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
            elif path == "/api/config":
                payload = self.read_json()
                ship_types = save_next_ship_types(
                    payload.get("next_ship_types", [])
                )
                self.send_json({"next_ship_types": ship_types})
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
