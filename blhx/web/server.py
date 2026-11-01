from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..storage.config import (
    read_config,
    save_next_ship_types,
    save_target_limit,
    toggle_manual_target,
)
from ..core.constants import (
    BONUS_ATTRIBUTE_ORDER,
    BONUS_SHIP_TYPE_ORDER,
    LOGGER,
    RARITY_FILTER_ORDER,
    RECOMMEND_SHIP_TYPE_ORDER,
    SHIP_TYPE_ORDER,
)
from ..storage.db import init_db
from ..transfer.csv_io import (
    CSV_CONTENT_TYPE,
    CsvFormatError,
    import_result_bytes,
    import_status_snapshot,
    import_template_bytes,
    parse_multipart_file,
    start_import,
)
from ..storage.history import list_operations
from .page import PAGE_HTML, STATIC_DIR
from ..domain.recommendations import available_next_ship_types, next_ship, next_ships_by_type
from ..domain.ships import (
    clear_ship_statuses,
    clear_ships,
    count_pending_ship_statuses,
    delete_ship,
    list_ships,
    update_ship_status,
)
from ..core.state import SYNC_STATE
from ..datasource.wiki import start_sync


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

    def query_filters(self) -> dict[str, Any]:
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        keys = [
            "name",
            "ship_type",
            "rarity",
            "bonus_ship_type",
            "bonus_attribute",
            "needs_training",
            "is_idling",
            "is_trained",
        ]
        filters: dict[str, Any] = {
            key: query.get(key, [""])[0].strip() for key in keys
        }
        # 与 /api/next-group 统一：ship_types 复数参数优先，支持列出多个舰种。
        ship_types = [
            value.strip()
            for value in query.get("ship_types", [])
            if value.strip()
        ]
        if not ship_types and filters["ship_type"]:
            ship_types = [filters["ship_type"]]
        if ship_types:
            filters["ship_types"] = ship_types
        return filters

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
            elif path == "/icon.png":
                self.send_bytes(
                    (STATIC_DIR / "icon.png").read_bytes(),
                    content_type="image/png",
                )
            elif path == "/api/ships":
                filters = self.query_filters()
                self.send_json(
                    {
                        "ships": list_ships(filters),
                        "status_counts": count_pending_ship_statuses(filters),
                    }
                )
            elif path == "/api/filter-options":
                self.send_json(
                    {
                        "ship_types": SHIP_TYPE_ORDER,
                        "rarities": RARITY_FILTER_ORDER,
                        "bonus_ship_types": BONUS_SHIP_TYPE_ORDER,
                        "bonus_attributes": BONUS_ATTRIBUTE_ORDER,
                        "recommend_ship_types": RECOMMEND_SHIP_TYPE_ORDER,
                    }
                )
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
                config = read_config()
                ship_types = (
                    self.next_query_ship_types() or config["next_ship_types"]
                )
                self.send_json(
                    {
                        "rows": next_ships_by_type(
                            mode,
                            ship_types,
                            config["manual_targets"],
                            config["target_limits"],
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
            elif path == "/api/import/status":
                self.send_json(import_status_snapshot())
            elif path == "/api/import/result":
                try:
                    result_data, _ = import_result_bytes()
                except ValueError as error:
                    self.send_error_json(str(error), 409)
                    return
                except LookupError as error:
                    self.send_error_json(str(error), 404)
                    return
                self.send_bytes(
                    result_data,
                    content_type=CSV_CONTENT_TYPE,
                    filename="blhx-training-import-result.csv",
                )
            elif path == "/api/template":
                self.send_bytes(
                    import_template_bytes(),
                    content_type=CSV_CONTENT_TYPE,
                    filename="blhx-training-import-template.csv",
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
                config = read_config()
                self.send_json(
                    {
                        "next_ship_types": ship_types,
                        "target_limits": config["target_limits"],
                        "manual_targets": config["manual_targets"],
                    }
                )
            elif path == "/api/target-limit":
                payload = self.read_json()
                config = save_target_limit(
                    str(payload.get("ship_type", "")),
                    str(payload.get("field", "")),
                    payload.get("limit"),
                )
                self.send_json(config)
            elif path == "/api/manual-target":
                payload = self.read_json()
                config = toggle_manual_target(
                    str(payload.get("ship_type", "")),
                    str(payload.get("field", "")),
                    payload.get("ship_id"),
                )
                self.send_json(config)
            elif path == "/api/ship/clear-status":
                payload = self.read_json()
                cleared = clear_ship_statuses(int(payload.get("id", 0)))
                if cleared is None:
                    self.send_error_json("舰船不存在", 404)
                    return
                self.send_json({"ship": cleared})
            elif path == "/api/import":
                body = self.read_body()
                uploaded = parse_multipart_file(
                    self.headers.get("Content-Type", ""), body
                )
                if uploaded is None:
                    self.send_error_json("请上传 CSV 文件")
                    return
                try:
                    snapshot = start_import(uploaded.data, uploaded.filename)
                except CsvFormatError as error:
                    self.send_error_json(str(error), 400)
                    return
                except ValueError as error:
                    self.send_error_json(str(error), 409)
                    return
                self.send_json({"started": True, "status": snapshot})
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
