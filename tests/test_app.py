import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote

from support import isolated_app_data

from blhx.datasource import wiki as wiki_module
from blhx.storage import config as config_module
from blhx.storage.config import (
    read_config,
    save_armament_target_limit,
    save_next_ship_types,
    save_target_limit,
    toggle_armament_manual_target,
    toggle_manual_target,
    write_config,
)
from blhx.core.constants import (
    DEFAULT_TECH_PRIORITY,
    RECOMMEND_SHIP_TYPE_DISPLAY_ORDER,
    RECOMMEND_TECH_SHIP_TYPES,
    ship_tech_priority_score,
    tech_priority_order,
    ARMAMENT_GROUP_ORDER,
    ARMAMENT_RECOMMEND_GROUP_ORDER,
    BASE_DIR,
    BONUS_ATTRIBUTE_ORDER,
    BONUS_SHIP_TYPE_ORDER,
    CATEGORY_SHIP_TAGS,
    COLLAB_SHIP_TAG,
    CUSTOM_SHIP_TAGS,
    RARITY_FILTER_ORDER,
    RECOMMEND_SHIP_TYPE_ORDER,
    SHIP_TYPE_ORDER,
    WIKI_BASE_URL,
    ship_tag_condition,
)
from blhx.storage.db import (
    get_connection,
    init_db,
)
from blhx.transfer.csv_io import (
    CsvFormatError,
    IMPORT_HEADERS,
    REPORT_HEADERS,
    csv_read,
    csv_write,
    import_csv,
    import_result_bytes,
    import_status_snapshot,
    import_template_bytes,
    parse_workbook_rows,
    process_import_rows,
    reset_import_state,
    start_import,
)
from blhx.storage.history import (
    list_operations,
)
from blhx.web.page import (
    PAGE_HTML,
)
from blhx.domain.recommendations import (
    available_next_ship_types,
    next_ship,
    next_ships_by_type,
    ship_bonus_ship_types,
)
from blhx.web.server import (
    TrackerHandler,
)
from blhx.core.version import (
    APP_VERSION,
)
from blhx.domain.armaments import (
    armament_group_counts,
    clear_armament_statuses,
    count_pending_armament_statuses,
    delete_armament,
    find_armament_brief,
    list_armaments,
    next_armaments_by_group,
    update_armament_status,
    upsert_armament,
)
from blhx.domain.ships import (
    clear_ship_statuses,
    clear_ships,
    count_pending_ship_statuses,
    delete_ship,
    insert_ship,
    list_ships,
    refresh_ship_tags,
    release_manual_target,
    update_ship_status,
)
from blhx.core.state import (
    IMPORT_STATE,
    SYNC_STATE,
)
from blhx.domain.tech import (
    parse_tech_attribute,
)
from blhx.datasource.wiki import (
    _sync_wiki,
    extract_ship_entries,
    fetch_release_entries,
    fetch_ship_payload,
    fetch_ship_raw,
    fetch_wiki_api,
    fetch_wiki_text,
    parse_armament_entries,
    parse_ship_raw,
    parse_template_fields,
)


def tech_point(ship_type, attribute=""):
    """推荐粒度是加成舰种，配置键就是舰种名。

    保留第二参数只为让既有用例读起来仍然表达“某舰种的某个属性”，
    实际比较与传参一律使用舰种名。
    """
    return ship_type


def _default_armament_limits():
    return {
        "collab": {"is_built": 1, "is_enhanced": 1},
        "normal": {"is_built": 1, "is_enhanced": 1},
    }


class AppTests(unittest.TestCase):
    def test_fetch_wiki_text_retries_rate_limit(self):
        class FakeHeaders:
            def get_content_charset(self):
                return "utf-8"

        class FakeResponse:
            def __init__(self):
                self.headers = FakeHeaders()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return "{{舰娘图鉴}}".encode("utf-8")

        with patch.object(
            wiki_module,
            "urlopen",
            side_effect=[
                HTTPError("https://example.test", 567, "Unknown Status", {}, None),
                FakeResponse(),
            ],
        ), patch.object(
            wiki_module, "acquire_wiki_request_slot", return_value=None
        ), patch.object(wiki_module.time, "sleep") as sleep:
            result = fetch_wiki_text("/test")

        self.assertEqual(result, "{{舰娘图鉴}}")
        self.assertEqual(sleep.call_count, 1)

    def test_fetch_wiki_text_solves_challenge(self):
        class FakeHeaders:
            def get_content_charset(self):
                return "utf-8"

        class FakeResponse:
            def __init__(self, body):
                self.body = body.encode("utf-8")
                self.headers = FakeHeaders()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self):
                return self.body

        challenge_body = """
        <script>
        var e={A:1,B:2,C:3},t=0;
        return t+=e[_0x649a("0x0")],t+=e[_0x649a("0x1")],
        t=e[_0x649a("0x2")](t,e[_0x649a("0x3")]),
        [t,e[_0x649a("0x8")](n)][a]
        document.cookie="__tst_status="+a(0)+"#;";
        document.cookie=a(1)+";";
        case"0":t+="EO_Bot_Ssid=";continue;
        case"3":t=a[_0x649a("0x7")](t,9);continue;
        </script>
        """
        requests = []

        def urlopen_side_effect(request, timeout=25):
            requests.append(request)
            if len(requests) == 1:
                return FakeResponse(challenge_body)
            return FakeResponse("{{舰娘图鉴}}")

        with patch.object(
            wiki_module, "urlopen", side_effect=urlopen_side_effect
        ), patch.object(
            wiki_module, "acquire_wiki_request_slot", return_value=None
        ), patch.object(wiki_module.time, "sleep"):
            result = fetch_wiki_text("/test")

        self.assertEqual(result, "{{舰娘图鉴}}")
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            requests[1].get_header("Cookie"),
            "__tst_status=6#; EO_Bot_Ssid=9",
        )

    def test_extract_ship_entries(self):
        html = """
        <table>
          <tr><td>190</td><td><a href="/blhx/%E5%8F%A4%E9%B9%B0">狼</a></td><td>2017年05月25日</td></tr>
          <tr><td>217</td><td><a href="/blhx/%E6%AD%A6%E8%97%8F">鳄</a></td><td>2023年08月17日</td></tr>
        </table>
        """
        entries = extract_ship_entries(html)
        self.assertEqual(
            entries,
            [
                {"name": "古鹰", "release_date": "2017-05-25"},
                {"name": "武藏", "release_date": "2023-08-17"},
            ],
        )

    def test_fetch_release_entries_uses_backend_api(self):
        responses = [
            {
                "query": {
                    "results": {
                        "古鹰": {
                            "fulltext": "古鹰",
                            "printouts": {"实装日期": ["2017年05月25日"]},
                        }
                    }
                },
                "query-continue-offset": 500,
            },
            {
                "query": {
                    "results": {
                        "武藏": {
                            "fulltext": "武藏",
                            "printouts": {"实装日期": ["2023年08月17日"]},
                        }
                    }
                },
            },
        ]

        def fetch_wiki_api_side_effect(params):
            self.assertEqual(params["action"], "ask")
            self.assertIn(f"offset={(2 - len(responses)) * 500}", params["query"])
            return responses.pop(0)

        with patch("blhx.datasource.wiki.fetch_wiki_api", side_effect=fetch_wiki_api_side_effect):
            entries = fetch_release_entries()

        self.assertEqual(
            entries,
            [
                {"name": "古鹰", "release_date": "2017-05-25"},
                {"name": "武藏", "release_date": "2023-08-17"},
            ],
        )

    def test_fetch_ship_raw_uses_revisions_api(self):
        payload = {
            "query": {
                "pages": [
                    {
                        "revisions": [
                            {
                                "slots": {
                                    "main": {"content": "{{舰娘图鉴}}"}
                                }
                            }
                        ]
                    }
                ]
            }
        }
        expected_params = {
            "action": "query",
            "titles": "武藏",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "redirects": "1",
            "format": "json",
            "formatversion": "2",
        }

        with patch("blhx.datasource.wiki.fetch_wiki_api", return_value=payload) as fetch_wiki_api:
            raw_text = fetch_ship_raw("武藏")

        fetch_wiki_api.assert_called_once_with(expected_params)
        self.assertEqual(raw_text, "{{舰娘图鉴}}")

    def test_parse_ship_raw(self):
        raw = r"""
        {{舰娘图鉴
        |名称=武藏
        |类型=战列
        |稀有度=海上传奇
        |解锁图鉴科技点=26
        |突破至满星科技点=52
        |达到120级科技点=39
        |解锁图鉴属性加成=战巡/战列/航战 耐久+2
        |达到120级属性加成=战巡/战列/航战 炮击+1
        }}
        """
        parsed = parse_ship_raw(raw)
        self.assertEqual(parsed["ship_type"], "战列")
        self.assertEqual(parsed["rarity"], "海上传奇")
        self.assertEqual(len(parsed["tech_points"]), 2)
        self.assertEqual(parsed["tech_points"][0]["stage"], "解锁图鉴")
        self.assertEqual(parsed["tech_points"][0]["attribute"], "战巡/战列/航战 耐久")
        self.assertEqual(parsed["tech_points"][1]["stage"], "达到120级")
        self.assertEqual(parsed["tech_points"][1]["attribute"], "战巡/战列/航战 炮击")

    def test_parse_ship_raw_merges_sail_ship_types(self):
        raw = r"""
        {{舰娘图鉴
        |名称=皇家财富号
        |类型=风帆S
        |稀有度=精锐
        |解锁图鉴属性加成=风帆 耐久+2
        }}
        """
        self.assertEqual(parse_ship_raw(raw)["ship_type"], "风帆")

    def test_parse_template_fields_removes_html_comments(self):
        raw = r"""
        {{舰娘图鉴
        |类型=潜艇
        |稀有度=超稀有\<!--选择【普通/稀有/精锐/超稀有/海上传奇/最高方案/决战方案】-->
        }}
        """
        fields = parse_template_fields(raw)
        self.assertEqual(fields["类型"], "潜艇")
        self.assertEqual(fields["稀有度"], "超稀有")

    def test_fetch_ship_payload_logs_raw_response_on_parse_failure(self):
        entry = {"name": "杓鹬", "release_date": "2024-01-01"}
        with patch(
            "blhx.datasource.wiki.fetch_ship_raw", return_value="{{非目标模板}}"
        ), patch("blhx.datasource.wiki.parse_ship_raw", return_value=None):
            with self.assertRaises(ValueError), self.assertLogs(
                "blhx_training_tracker", level="ERROR"
            ) as logs:
                fetch_ship_payload(entry)

        self.assertTrue(any("{{非目标模板}}" in message for message in logs.output))

    def test_csv_roundtrip(self):
        rows = [
            ["舰船名称", "是否已挂机", "是否已练级完成"],
            ["武藏", "是", "否"],
            ['带,逗号"与引号', "否", "是"],
            ["多行\n名称", "是", "是"],
        ]
        data = csv_write(rows)
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(csv_read(data), rows)

    def test_csv_read_accepts_utf8_with_and_without_bom(self):
        rows = [
            ["舰船名称", "是否已挂机", "是否已练级完成"],
            ["武藏", "是", "否"],
        ]
        body = "舰船名称,是否已挂机,是否已练级完成\r\n武藏,是,否\r\n"
        self.assertEqual(csv_read(body.encode("utf-8")), rows)
        self.assertEqual(csv_read(body.encode("utf-8-sig")), rows)
        # 其他编码不再兜底转换，直接作为格式错误返回。
        with self.assertRaises(CsvFormatError):
            csv_read(body.encode("gb18030"))

    def test_csv_read_rejects_invalid_encoding_and_syntax(self):
        with self.assertRaises(CsvFormatError):
            csv_read(b"\xff\xfe\x00\x01\xff\xff")
        with self.assertRaises(CsvFormatError):
            csv_read(
                '舰船名称,是否已挂机,是否已练级完成\r\n"未闭合,是,否\r\n'.encode(
                    "utf-8"
                )
            )

    def test_parse_workbook_rows_rejects_duplicate_and_unknown_layout(self):
        with self.assertRaises(CsvFormatError):
            parse_workbook_rows(
                [["舰船名称", "舰船名称", "是否已挂机", "是否已练级完成"], ["武藏", "错误", "是", "否"]]
            )
        with self.assertRaises(CsvFormatError):
            parse_workbook_rows([["舰船名称", "是否已挂机"]])
        with self.assertRaises(CsvFormatError):
            parse_workbook_rows([])
        self.assertEqual(parse_workbook_rows([IMPORT_HEADERS]), [])
        rows = parse_workbook_rows(
            [
                ["舰船名称", "是否已挂机", "是否已练级完成", "备注"],
                ["武藏", "是", "否", "额外列忽略"],
                ["", "", "", ""],
                ["古鹰", "否", "是"],
            ]
        )
        self.assertEqual([row["舰船名称"] for row in rows], ["武藏", "古鹰"])

    def test_import_csv_returns_report_and_counts(self):
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {"ship_type": "战列", "rarity": "海上传奇", "tech_points": []},
                )
            payload = csv_write(
                [
                    ["舰船名称", "是否已挂机", "是否已练级完成"],
                    ["武藏", "是", "否"],
                    ["不存在", "是", "否"],
                ]
            )
            report_bytes, updated, skipped = import_csv(payload)
            self.assertEqual((updated, skipped), (1, 1))
            report = csv_read(report_bytes)
            self.assertEqual(report[0], REPORT_HEADERS)
            self.assertEqual(report[1][3], "处理完成")
            self.assertEqual(report[2][3], "未匹配到数据跳过")
            with get_connection() as connection:
                ship = connection.execute(
                    "SELECT is_idling, is_trained FROM ships WHERE name = '武藏'"
                ).fetchone()
                self.assertEqual((ship["is_idling"], ship["is_trained"]), (1, 0))

    def test_start_import_runs_in_background_and_exposes_result(self):
        import time

        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {"ship_type": "战列", "rarity": "海上传奇", "tech_points": []},
                )
            payload = csv_write(
                [
                    ["舰船名称", "是否已挂机", "是否已练级完成"],
                    ["武藏", "是", "否"],
                    ["不存在", "否", "是"],
                ]
            )
            reset_import_state()
            snapshot = start_import(payload, "test.csv")
            self.assertTrue(snapshot["running"])
            self.assertEqual(snapshot["total"], 2)
            for _ in range(200):
                status = import_status_snapshot()
                if not status["running"]:
                    break
                time.sleep(0.02)
            status = import_status_snapshot()
            self.assertFalse(status["running"])
            self.assertEqual(status["status"], "完成")
            self.assertEqual(status["processed"], 1)
            self.assertEqual(status["skipped"], 1)
            report = csv_read(IMPORT_STATE["result"])
            self.assertEqual(report[0], REPORT_HEADERS)
            self.assertEqual(report[1][3], "处理完成")
            self.assertEqual(report[2][3], "未匹配到数据跳过")

    def test_start_import_rejects_invalid_csv_and_concurrent_start(self):
        import threading
        import time

        with isolated_app_data():
            with self.assertRaises(CsvFormatError):
                start_import(b"\xff\xfe\x00\xff")
            self.assertFalse(import_status_snapshot()["running"])

            rows = [["舰船名称", "是否已挂机", "是否已练级完成"]]
            rows.extend([["不存在", "是", "否"]] * 400)
            payload = csv_write(rows)
            reset_import_state()
            start_import(payload, "slow.csv")
            try:
                if import_status_snapshot()["running"]:
                    with self.assertRaises(ValueError):
                        start_import(payload, "again.csv")
            finally:
                for _ in range(400):
                    if not import_status_snapshot()["running"]:
                        break
                    time.sleep(0.02)

    def test_import_template_is_csv(self):
        data = import_template_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(csv_read(data), [IMPORT_HEADERS])

    def test_import_result_is_atomic_and_locked(self):
        import threading
        import time

        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {"ship_type": "战列", "rarity": "海上传奇", "tech_points": []},
                )
            payload = csv_write(
                [
                    ["舰船名称", "是否已挂机", "是否已练级完成"],
                    ["武藏", "是", "是"],
                ]
            )
            reset_import_state()
            with self.assertRaises(LookupError):
                import_result_bytes()
            start_import(payload, "atomic.csv")
            with self.assertRaises(ValueError):
                import_result_bytes()
            for _ in range(200):
                if not import_status_snapshot()["running"]:
                    break
                time.sleep(0.02)
            data, status = import_result_bytes()
            self.assertEqual(status, "完成")
            self.assertEqual(csv_read(data)[1][3], "处理完成")
            self.assertEqual(csv_read(data)[1][0], "武藏")

            errors = []

            def reader():
                try:
                    for _ in range(50):
                        import_status_snapshot()
                except Exception as error:  # pragma: no cover
                    errors.append(error)

            threads = [threading.Thread(target=reader) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])

    def test_page_serves_icon_and_declares_favicon(self):
        import json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer

        with isolated_app_data():
            server = ThreadingHTTPServer(("127.0.0.1", 0), TrackerHandler)
            base = f"http://127.0.0.1:{server.server_address[1]}"
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with urllib.request.urlopen(base + "/") as response:
                    html = response.read().decode("utf-8")
                with urllib.request.urlopen(base + "/icon.png") as response:
                    icon = response.read()
                    content_type = response.headers["Content-Type"]
                with urllib.request.urlopen(base + "/api/version") as response:
                    version_payload = json.loads(response.read().decode("utf-8"))
                self.assertIn('href="/icon.png"', html)
                self.assertIn('rel="icon"', html)
                self.assertEqual(content_type, "image/png")
                self.assertTrue(icon.startswith(b"\x89PNG\r\n\x1a\n"))
                self.assertEqual(version_payload["version"], APP_VERSION)
            finally:
                server.shutdown()
                server.server_close()

    def test_import_process(self):
        with isolated_app_data():
            with get_connection() as connection:
                battle_ship = insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {
                        "ship_type": "战列",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "耐久", "points": 26}
                        ],
                    },
                )
            rows = [
                {"舰船名称": "武藏", "是否已挂机": "是", "是否已练级完成": "否"},
                {"舰船名称": "不存在", "是否已挂机": "是", "是否已练级完成": "否"},
            ]
            report_rows, updated, skipped = process_import_rows(rows)
            self.assertEqual(updated, 1)
            self.assertEqual(skipped, 1)
            self.assertEqual(report_rows[0]["处理状态"], "处理完成")
            self.assertEqual(report_rows[1]["处理状态"], "未匹配到数据跳过")

    def test_ship_filter_sort_and_status_update(self):
        with isolated_app_data():
            with get_connection() as connection:
                ship_1 = insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {
                        "ship_type": "战列",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {
                                "stage": "解锁图鉴",
                                "attribute": "战巡/战列/航战 耐久",
                                "points": 0,
                            }
                        ],
                    },
                )
                ship_2 = insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {
                                "stage": "达到120级",
                                "attribute": "重巡/轻巡 机动",
                                "points": 0,
                            }
                        ],
                    },
                )

            ships = list_ships({"is_trained": "否"})
            self.assertEqual([ship["name"] for ship in ships], ["古鹰", "武藏"])
            ships = list_ships({"bonus_attribute": "机动"})
            self.assertEqual([ship["name"] for ship in ships], ["古鹰"])

            updated = update_ship_status(ship_1["id"], "is_idling", True)
            self.assertTrue(updated["is_idling"])
            self.assertIsNone(updated["is_trained"])

    def test_pending_status_counts_ignore_own_filter(self):
        with isolated_app_data():
            with get_connection() as connection:
                ship_1 = insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {
                        "ship_type": "战列",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "耐久", "points": 0}
                        ],
                    },
                )
                insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "驱逐 机动", "points": 0}
                        ],
                    },
                )
                insert_ship(
                    connection,
                    "夕立",
                    "2017-05-25",
                    {
                        "ship_type": "驱逐",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "炮击", "points": 0}
                        ],
                    },
                )
            update_ship_status(ship_1["id"], "is_idling", True)

            counts = count_pending_ship_statuses({"needs_training": "是"})
            self.assertEqual(
                counts, {"is_idling": 2, "is_trained": 3, "pending": 3}
            )
            # 统计时忽略本字段自身的筛选，但仍受其他筛选影响。
            counts = count_pending_ship_statuses(
                {"needs_training": "是", "is_idling": "是"}
            )
            self.assertEqual(
                counts, {"is_idling": 2, "is_trained": 1, "pending": 1}
            )
            self.assertRegex(APP_VERSION, r"^\d+\.\d+\.\d+$")
            self.assertEqual(
                APP_VERSION,
                (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip(),
            )

    def test_bump_version_script(self):
        import os
        import subprocess
        import tempfile

        script = Path(__file__).resolve().parent.parent / "bump_version.sh"
        self.assertTrue(script.is_file())

        def run(args, version, answers=None):
            with tempfile.TemporaryDirectory() as temp_dir:
                target = Path(temp_dir) / "VERSION"
                target.write_text(version + "\n", encoding="utf-8")
                env = dict(os.environ, VERSION_FILE=str(target))
                result = subprocess.run(
                    [str(script), *args],
                    input=answers,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                return result, target.read_text(encoding="utf-8")

        result, content = run(["patch", "-y"], "1.0.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(content, "1.0.2\n")
        self.assertEqual(run(["minor", "-y"], "1.9.9")[1], "1.10.0\n")
        self.assertEqual(run(["major", "-y"], "1.9.9")[1], "2.0.0\n")
        self.assertEqual(run(["patch", "-y"], "9.9.9")[1], "9.9.10\n")

        result, content = run([], "1.4.2", "2\ny\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(content, "2.0.0\n")

        result, content = run([], "1.4.2", "1\nn\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(content, "1.4.2\n")

        result, content = run(["minor", "-y"], "1.2")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(content, "1.2\n")

        result, content = run(["unknown"], "1.2.3")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(content, "1.2.3\n")

    def test_release_notes_lists_commit_hash_link_and_message(self):
        import os
        import shutil
        import subprocess
        import tempfile

        repo_root = Path(__file__).resolve().parent.parent
        script = repo_root / ".github" / "scripts" / "generate_release_notes.sh"
        self.assertTrue(script.is_file())

        def git(cwd, *args):
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Path(temp_dir) / "repo"
            scripts = repo / ".github" / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy(script, scripts / script.name)
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "test@example.com")
            git(repo, "config", "user.name", "tester")

            (repo / "a.txt").write_text("a", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "feat: 第一个提交")
            git(repo, "tag", "v1.0.0")

            (repo / "b.txt").write_text("b", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "fix: 第二个提交\n\n补充说明行")
            second_sha = git(repo, "rev-parse", "HEAD")
            short_sha = git(repo, "rev-parse", "--short", "HEAD")

            output = Path(temp_dir) / "notes.md"
            subprocess.run(
                [str(scripts / script.name)],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
                env=dict(
                    os.environ,
                    REPOSITORY="owner/repo",
                    VERSION="1.0.1",
                    TAG="v1.0.1",
                    OUTPUT=str(output),
                ),
            )
            notes = output.read_text(encoding="utf-8")

        self.assertIn(f"[`{short_sha}`](https://github.com/owner/repo/commit/{second_sha})", notes)
        self.assertIn("fix: 第二个提交", notes)
        self.assertIn("补充说明行", notes)
        # 只包含上一版本标签之后的提交，不重复列出旧版本内容。
        self.assertNotIn("feat: 第一个提交", notes)

    def test_bonus_filters_use_parsed_ship_types(self):
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "易北",
                    "2024-05-23",
                    {
                        "ship_type": "战巡",
                        "rarity": "决战方案",
                        "tech_points": [
                            {
                                "stage": "解锁图鉴",
                                "attribute": "战巡/战列/航战 耐久",
                                "points": 2,
                            }
                        ],
                    },
                )
                insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {
                        "ship_type": "战列",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {
                                "stage": "解锁图鉴",
                                "attribute": "战列 耐久",
                                "points": 2,
                            }
                        ],
                    },
                )
                insert_ship(
                    connection,
                    "重樱专属",
                    "2024-05-23",
                    {
                        "ship_type": "战列",
                        "rarity": "精锐",
                        "tech_points": [
                            {
                                "stage": "解锁图鉴",
                                "attribute": "重巡 耐久",
                                "points": 2,
                            }
                        ],
                    },
                )

            self.assertEqual(
                parse_tech_attribute("战巡/战列/航战 耐久"),
                (["战巡", "战列", "航战"], "耐久"),
            )
            ships = list_ships(
                {"bonus_ship_type": "战列", "bonus_attribute": "耐久"}
            )
            self.assertEqual([ship["name"] for ship in ships], ["武藏", "易北"])
            ships = list_ships(
                {"bonus_ship_type": "战列", "bonus_attribute": "炮击"}
            )
            self.assertEqual(ships, [])
            ships = list_ships(
                {"ship_types": ["战列"], "bonus_attribute": "耐久"}
            )
            self.assertEqual([ship["name"] for ship in ships], ["武藏", "重樱专属"])
            ships = list_ships({"bonus_ship_type": "重巡", "bonus_attribute": "耐久"})
            self.assertEqual([ship["name"] for ship in ships], ["重樱专属"])

    def test_next_ship_type_config_uses_data_file(self):
        """推荐舰种（加成舰种）落盘到 DATA_DIR/config.json。"""
        with isolated_app_data():
            saved = save_next_ship_types(["战列"])
            self.assertEqual(saved, ["战列"])
            self.assertEqual(
                read_config(),
                {
                    "next_ship_types": ["战列"],
                    "target_limits": {"战列": {"is_idling": 1, "is_trained": 1}},
                    "manual_targets": {},
                    "armament_target_limits": _default_armament_limits(),
                    "armament_manual_targets": {},
                },
            )
            with config_module.CONFIG_PATH.open(encoding="utf-8") as file:
                import json

                self.assertEqual(
                    json.load(file),
                    {
                        "next_ship_types": ["战列"],
                        "target_limits": {"战列": {"is_idling": 1, "is_trained": 1}},
                        "manual_targets": {},
                        "armament_target_limits": _default_armament_limits(),
                        "armament_manual_targets": {},
                    },
                )
            # 多个舰种按推荐展示顺序落盘，重复项去重。
            saved = save_next_ship_types(["驱逐", "战列", "驱逐"])
            self.assertEqual(saved, ["战列", "驱逐"])
            self.assertEqual(read_config()["next_ship_types"], ["战列", "驱逐"])
            # 不在推荐范围内的舰种（如舰船自身舰种「航母」）被拒绝。
            with self.assertRaises(ValueError):
                save_next_ship_types(["航母"])

    def test_recommend_config_survives_until_ship_appears(self):
        """配置先于数据存在：配置可保存，后续出现对应科技点的舰船后自动参与推荐。"""
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {
                                "stage": "达到120级",
                                "attribute": "重巡/轻巡 机动",
                                "points": 5,
                            }
                        ],
                    },
                )
            key = tech_point("重巡", "机动")
            self.assertEqual(save_next_ship_types([key]), [key])
            self.assertEqual(read_config()["next_ship_types"], [key])

            rows = next_ships_by_type(
                "both",
                read_config()["next_ship_types"],
                read_config()["manual_targets"],
                read_config()["target_limits"],
            )
            self.assertEqual([row["ship_type"] for row in rows], [key])
            self.assertEqual(rows[0]["idle"]["name"], "古鹰")
            self.assertTrue(rows[0]["idle_manual_stale"] is False)

            # 舰船全部练级完成后配置仍然保留，等待下一批舰船。
            update_ship_status(
                list_ships({"name": "古鹰"})[0]["id"], "is_trained", True
            )
            self.assertEqual(read_config()["next_ship_types"], [key])

    def test_next_ship_supports_available_ship_types(self):
        with isolated_app_data():
            with get_connection() as connection:
                battle_ship = insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {
                        "ship_type": "战列",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {
                                "stage": "解锁图鉴",
                                "attribute": "战巡/战列/航战 耐久",
                                "points": 26,
                            }
                        ],
                    },
                )
                heavy_cruiser = insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {
                                "stage": "达到120级",
                                "attribute": "重巡/轻巡 机动",
                                "points": 5,
                            }
                        ],
                    },
                )
                destroyer = insert_ship(
                    connection,
                    "夕立",
                    "2017-05-25",
                    {
                        "ship_type": "驱逐",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {
                                "stage": "解锁图鉴",
                                "attribute": "驱逐/轻巡 炮击",
                                "points": 5,
                            }
                        ],
                    },
                )
            update_ship_status(destroyer["id"], "is_trained", True)
            self.assertTrue(
                list_ships({"name": destroyer["name"]})[0]["is_idling"]
            )
            self.assertEqual(
                list_operations()[0]["message"],
                "标记为已练级完成：夕立（同时标记为已挂机完成）",
            )

            options = {item["ship_type"]: item for item in available_next_ship_types()}
            # 选项来自全部加成舰种，计数与待办口径一致。
            self.assertEqual(options[tech_point("战列", "耐久")]["idle_count"], 1)
            self.assertEqual(options[tech_point("战列", "耐久")]["train_count"], 0)
            self.assertEqual(options["重巡"]["idle_count"], 1)
            # 已练级的驱逐不再计入待练级。
            self.assertEqual(options[tech_point("驱逐", "炮击")]["idle_count"], 0)
            self.assertIsNone(
                next_ship(
                    "train",
                    ship_types=[
                        tech_point("战列", "耐久"),
                        tech_point("重巡", "机动"),
                    ],
                )
            )
            self.assertEqual(
                next_ship("idle", ship_types=[tech_point("重巡", "机动")])["name"],
                "古鹰",
            )

            update_ship_status(battle_ship["id"], "is_idling", True)
            update_ship_status(heavy_cruiser["id"], "is_idling", True)
            options = {item["ship_type"]: item for item in available_next_ship_types()}
            self.assertEqual(options["战列"]["train_count"], 1)
            self.assertEqual(options["重巡"]["train_count"], 1)
            ships = list_ships({"ship_types": ["战列", "重巡"]})
            self.assertEqual(
                [ship["name"] for ship in ships], ["古鹰", "武藏"]
            )
            # next_ship 按固定展示顺序取第一个有候选的舰种，战列在重巡之前。
            self.assertEqual(
                next_ship(
                    "train",
                    ship_types=[tech_point("战列", "耐久"), tech_point("重巡", "机动")],
                )["name"],
                "武藏",
            )
            self.assertIsNone(
                next_ship("idle", ship_types=[tech_point("重巡", "机动")])
            )
            grouped = next_ships_by_type(
                "both",
                [tech_point("重巡", "机动"), tech_point("战列", "耐久")],
            )
            # 行顺序由固定展示顺序决定：战列在重巡之前。
            self.assertEqual(
                [row["ship_type"] for row in grouped], ["战列", "重巡"]
            )
            self.assertIsNone(grouped[0]["idle"])
            self.assertEqual(grouped[0]["train"]["name"], "武藏")
            self.assertIsNone(grouped[1]["idle"])
            self.assertEqual(grouped[1]["train"]["name"], "古鹰")

            update_ship_status(heavy_cruiser["id"], "is_trained", True)
            # 没有可推荐舰娘时返回 None，不再抛“舰种不可用”错误。
            self.assertIsNone(
                next_ship("train", ship_types=[tech_point("重巡", "机动")])
            )

    def test_operation_history_records_status_and_delete(self):
        with isolated_app_data():
            with get_connection() as connection:
                ship = insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {
                        "ship_type": "战列",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {
                                "stage": "解锁图鉴",
                                "attribute": "战巡/战列/航战 耐久",
                                "points": 0,
                            }
                        ],
                    },
                )

            self.assertTrue(
                update_ship_status(ship["id"], "is_idling", True)
            )
            self.assertTrue(delete_ship(ship["id"]))
            operations = list_operations()
            self.assertEqual(len(operations), 2)
            self.assertEqual(operations[0]["message"], "删除舰娘「武藏」")
            self.assertEqual(
                operations[1]["message"], "标记为已挂机完成：武藏"
            )
            self.assertTrue(operations[0]["created_at"])

    def test_partial_sync_matches_and_overwrites(self):
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {
                        "ship_type": "旧类型",
                        "rarity": "旧稀有度",
                        "tech_points": [
                            {
                                "stage": "解锁图鉴",
                                "attribute": "旧加成",
                                "points": 0,
                            }
                        ],
                    },
                )
            release_entry = {"name": "武藏", "release_date": "2023-08-17"}
            parsed_ship = {
                "name": "武藏",
                "release_date": "2023-08-17",
                "ship_type": "战列",
                "rarity": "海上传奇",
                "tech_points": [
                    {
                        "stage": "解锁图鉴",
                        "attribute": "战巡/战列/航战 耐久",
                        "points": 0,
                    }
                ],
            }
            with patch(
                "blhx.datasource.wiki.fetch_release_entries", return_value=[release_entry]
            ), patch("blhx.datasource.wiki.fetch_ship_payload", return_value=parsed_ship):
                _sync_wiki(["武藏"])

            self.assertEqual(SYNC_STATE["processed"], 1)
            self.assertEqual(SYNC_STATE["errors"], [])
            ships = list_ships({"name": "武藏"})
            self.assertEqual(ships[0]["ship_type"], "战列")
            self.assertEqual(ships[0]["tech_points"][0]["attribute"], "战巡/战列/航战 耐久")

    def test_partial_sync_requires_wiki_match(self):
        SYNC_STATE.update({"running": False, "status": "", "message": ""})
        with patch(
            "blhx.datasource.wiki.fetch_release_entries",
            return_value=[{"name": "武藏", "release_date": "2023-08-17"}],
        ):
            _sync_wiki(["古鹰"])
        self.assertEqual(SYNC_STATE["status"], "失败")
        self.assertIn("古鹰", SYNC_STATE["message"])

    def test_delete_ship_removes_related_data(self):
        with isolated_app_data():
            with get_connection() as connection:
                ship = insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {
                        "ship_type": "战列",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {
                                "stage": "解锁图鉴",
                                "attribute": "战巡/战列/航战 耐久",
                                "points": 0,
                            }
                        ],
                    },
                )

            self.assertTrue(delete_ship(ship["id"]))
            self.assertEqual(list_ships({}), [])

    def test_clear_ships_removes_all_data(self):
        with isolated_app_data():
            with get_connection() as connection:
                for name, date, stage in [
                    ("武藏", "2023-08-17", "解锁图鉴"),
                    ("古鹰", "2017-05-25", "达到120级"),
                ]:
                    insert_ship(
                        connection,
                        name,
                        date,
                        {
                            "ship_type": "战列",
                            "rarity": "海上传奇",
                            "tech_points": [
                                {
                                    "stage": stage,
                                    "attribute": "战巡/战列/航战 耐久",
                                    "points": 0,
                                }
                            ],
                        },
                    )

            self.assertEqual(clear_ships(), 2)
            self.assertEqual(list_ships({}), [])
            with get_connection() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM tech_points").fetchone()[0],
                    0,
                )


    def test_filter_option_orders(self):
        self.assertEqual(
            SHIP_TYPE_ORDER,
            [
                "驱逐", "轻巡", "重巡", "超巡", "战巡", "战列", "航母", "轻航",
                "重炮", "维修", "航战", "潜艇", "潜母", "运输", "风帆",
            ],
        )
        self.assertEqual(
            RARITY_FILTER_ORDER,
            ["普通", "稀有", "精锐", "超稀有", "海上传奇", "最高方案", "决战方案"],
        )
        self.assertEqual(
            BONUS_SHIP_TYPE_ORDER,
            [
                "驱逐", "轻巡", "重巡", "超巡", "战巡", "战列", "正航", "轻航",
                "重炮", "维修", "航战", "潜艇", "潜母", "运输", "风帆",
            ],
        )
        self.assertEqual(
            BONUS_ATTRIBUTE_ORDER,
            ["耐久", "炮击", "雷击", "防空", "航空", "装填", "机动", "命中", "反潜"],
        )

    def test_sail_ship_types_are_merged(self):
        with isolated_app_data():
            with get_connection() as connection:
                for name, ship_type in [
                    ("皇家财富号", "风帆S"),
                    ("金鹿号", "风帆V"),
                    ("圣马丁号", "风帆M"),
                ]:
                    insert_ship(
                        connection,
                        name,
                        "2024-01-01",
                        {
                            "ship_type": ship_type,
                            "rarity": "精锐",
                            "tech_points": [
                                {"stage": "解锁图鉴", "attribute": "风帆 耐久", "points": 0}
                            ],
                        },
                    )

            ships = list_ships({"ship_types": ["风帆"]})
            self.assertEqual(len(ships), 3)
            self.assertTrue(all(ship["ship_type"] == "风帆" for ship in ships))

    def test_bonus_filters_use_bonus_ship_type_column(self):
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "企业",
                    "2020-01-01",
                    {
                        "ship_type": "航母",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {
                                "stage": "解锁图鉴",
                                "attribute": "轻航/正航 装填",
                                "points": 0,
                            }
                        ],
                    },
                )
                insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {
                        "ship_type": "战列",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {
                                "stage": "解锁图鉴",
                                "attribute": "战巡/战列/航战 耐久",
                                "points": 0,
                            },
                            {
                                "stage": "达到120级",
                                "attribute": "战巡/战列/航战 炮击",
                                "points": 0,
                            },
                        ],
                    },
                )

            ships = list_ships({"bonus_ship_type": "正航"})
            self.assertEqual([ship["name"] for ship in ships], ["企业"])
            ships = list_ships({"bonus_ship_type": "轻航"})
            self.assertEqual([ship["name"] for ship in ships], ["企业"])
            ships = list_ships({"bonus_attribute": "炮击"})
            self.assertEqual([ship["name"] for ship in ships], ["武藏"])
            ships = list_ships({"bonus_ship_type": "战列", "bonus_attribute": "耐久"})
            self.assertEqual([ship["name"] for ship in ships], ["武藏"])
            self.assertEqual(
                list_ships({"bonus_ship_type": "航母"}), []
            )

    def test_shop_tech_point_keys(self):
        """舰船的科技点键按「加成舰种 + 加成属性」展开，多值舰种逐个拆分。"""
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "什罗普郡",
                    "2024-01-01",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡/重炮/超巡 装填", "points": 0},
                            {"stage": "达到120级", "attribute": "重巡/重炮/超巡 机动", "points": 0},
                        ],
                    },
                )
            ship = list_ships({"name": "什罗普郡"})[0]
            # 加成舰种按舰种去重：多个属性归到同一舰种下。
            self.assertEqual(
                ship_bonus_ship_types(ship), ["重巡", "重炮", "超巡"]
            )
            # 没有科技点的舰船返回空列表。
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "企业",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )
            self.assertEqual(
                ship_bonus_ship_types(list_ships({"name": "企业"})[0]), []
            )

    def test_tech_priority_order_covers_every_ship_type(self):
        """有表舰种返回自己的表；无表舰种统一回退到 DEFAULT_TECH_PRIORITY。"""
        for ship_type, expected in [
            ("战列", ["装填", "命中", "炮击", "防空", "耐久"]),
            ("重巡", ["机动", "炮击", "装填", "命中", "雷击", "防空", "耐久"]),
            ("潜艇", ["雷击"]),
        ]:
            self.assertEqual(tech_priority_order(ship_type), expected)
        # 战巡/超巡/潜母等没有独立表，回退默认优先级。
        for ship_type in ("战巡", "超巡", "潜母", "维修", "运输", "风帆", "航战", "重炮"):
            self.assertEqual(tech_priority_order(ship_type), DEFAULT_TECH_PRIORITY)

    def test_ship_tech_priority_score_uses_bonus_attributes(self):
        """舰船在某加成舰种下的得分取该舰命中属性里最靠前的一条。"""
        # 重巡表：机动 0、炮击 1、装填 2、耐久 6
        self.assertEqual(ship_tech_priority_score("重巡", ["装填", "机动"]), 0)
        self.assertEqual(ship_tech_priority_score("重巡", ["装填"]), 2)
        self.assertEqual(ship_tech_priority_score("重巡", ["耐久"]), 6)
        # 表外属性排在表尾。
        self.assertEqual(
            ship_tech_priority_score("重巡", ["不存在属性"]), len(tech_priority_order("重巡"))
        )
        # 一个属性都没有时同样排表尾。
        self.assertEqual(
            ship_tech_priority_score("重巡", []), len(tech_priority_order("重巡"))
        )
        # 无表舰种走默认优先级：装填 0、耐久 4。
        self.assertEqual(ship_tech_priority_score("战巡", ["装填"]), 0)
        self.assertEqual(ship_tech_priority_score("战巡", ["耐久"]), 4)

    def test_tech_priority_order_covers_every_ship_type(self):
        """有表舰种返回自己的表；无表舰种统一回退到 DEFAULT_TECH_PRIORITY。"""
        for ship_type, expected in [
            ("战列", ["装填", "命中", "炮击", "防空", "耐久"]),
            ("重巡", ["机动", "炮击", "装填", "命中", "雷击", "防空", "耐久"]),
            ("潜艇", ["雷击"]),
        ]:
            self.assertEqual(tech_priority_order(ship_type), expected)
        # 战巡/超巡/潜母等没有独立表，回退默认优先级。
        for ship_type in ("战巡", "超巡", "潜母", "维修", "运输", "风帆", "航战", "重炮"):
            self.assertEqual(tech_priority_order(ship_type), DEFAULT_TECH_PRIORITY)

    def test_clear_ships_on_empty_database(self):
        with isolated_app_data():
            self.assertEqual(clear_ships(), 0)
            self.assertEqual(list_ships({}), [])
            self.assertEqual(
                read_config(),
                {
                    "next_ship_types": [],
                    "target_limits": {},
                    "manual_targets": {},
                    "armament_target_limits": _default_armament_limits(),
                    "armament_manual_targets": {},
                },
            )

    def test_clear_ships_rejects_when_sync_running(self):
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {"ship_type": "战列", "rarity": "海上传奇", "tech_points": []},
                )
            SYNC_STATE["running"] = True
            try:
                with self.assertRaises(ValueError):
                    clear_ships()
            finally:
                SYNC_STATE["running"] = False
            self.assertEqual(len(list_ships({})), 1)

    def test_clear_ships_reports_config_remove_failure(self):
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {"ship_type": "战列", "rarity": "海上传奇", "tech_points": []},
                )
            write_config({"next_ship_types": [tech_point("战列", "耐久")]})
            with patch.object(
                config_module.CONFIG_PATH.__class__,
                "unlink",
                side_effect=PermissionError("denied"),
            ):
                with self.assertRaises(ValueError):
                    clear_ships()
            self.assertEqual(len(list_ships({})), 1)
            self.assertEqual(
                read_config(),
                {
                    "next_ship_types": [tech_point("战列", "耐久")],
                    "target_limits": {
                        tech_point("战列", "耐久"): {"is_idling": 1, "is_trained": 1}
                    },
                    "manual_targets": {},
                    "armament_target_limits": _default_armament_limits(),
                    "armament_manual_targets": {},
                },
            )

    def test_clear_ship_statuses_resets_both_fields(self):
        with isolated_app_data():
            with get_connection() as connection:
                ship = insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {"ship_type": "战列", "rarity": "海上传奇", "tech_points": []},
                )
            update_ship_status(ship["id"], "is_trained", True)
            cleared = clear_ship_statuses(ship["id"])
            self.assertEqual(cleared, {"id": ship["id"], "is_idling": None, "is_trained": None})
            record = list_ships({"name": "武藏"})[0]
            self.assertIsNone(record["is_idling"])
            self.assertIsNone(record["is_trained"])
            self.assertEqual(list_operations()[0]["message"], "清除记录：武藏")
            self.assertIsNone(clear_ship_statuses(99999))

    def test_pending_filter_matches_idle_or_trained_unfinished(self):
        with isolated_app_data():
            with get_connection() as connection:
                ships = {}
                for name, ship_type, attribute in (
                    ("武藏", "战列", "耐久"),
                    ("古鹰", "重巡", "机动"),
                    ("夕立", "驱逐", "炮击"),
                ):
                    ships[name] = insert_ship(
                        connection,
                        name,
                        "2017-05-25",
                        {
                            "ship_type": ship_type,
                            "rarity": "精锐",
                            "tech_points": [
                                {"stage": "解锁图鉴", "attribute": attribute, "points": 0}
                            ],
                        },
                    )
            # 武藏只挂机未练级，古鹰挂机+练级都完成，夕立两项都未记录。
            update_ship_status(ships["武藏"]["id"], "is_idling", True)
            update_ship_status(ships["古鹰"]["id"], "is_idling", True)
            update_ship_status(ships["古鹰"]["id"], "is_trained", True)

            pending_names = sorted(
                ship["name"] for ship in list_ships({"pending": "是"})
            )
            self.assertEqual(pending_names, sorted(["武藏", "夕立"]))
            # 两项都未记录的舰船同样命中“未完成”。
            self.assertEqual(
                [
                    ship["name"]
                    for ship in list_ships({"pending": "是", "ship_types": ["驱逐"]})
                ],
                ["夕立"],
            )
            # 只支持“是”；其他取值视为不筛选。
            self.assertEqual(len(list_ships({"pending": "否"})), 3)
            # 关闭筛选时不受影响。            self.assertEqual(len(list_ships({"pending": ""})), 3)

            # 与已有状态筛选按 AND 组合：只挂机未练级的舰船。
            self.assertEqual(
                [
                    ship["name"]
                    for ship in list_ships({"pending": "是", "is_trained": "是"})
                ],
                [],
            )

    def test_manual_targets_persist_and_drive_recommendation(self):
        with isolated_app_data():
            with get_connection() as connection:
                chosen = insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
                insert_ship(
                    connection,
                    "青叶",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
            update_ship_status(chosen["id"], "is_idling", True)
            for ship in list_ships({"ship_types": ["重巡"]}):
                if ship["name"] == "青叶":
                    update_ship_status(ship["id"], "is_idling", True)
            save_next_ship_types([tech_point("重巡", "机动")])
            self.assertEqual(
                next_ships_by_type("both", [tech_point("重巡", "机动")])[0]["train"]["name"],
                "青叶",
            )

            config = toggle_manual_target(tech_point("重巡", "机动"), "is_trained", chosen["id"])
            self.assertEqual(
                config["manual_targets"],
                {tech_point("重巡", "机动"): {"is_trained": [chosen["id"]]}},
            )
            config = read_config()
            row = next_ships_by_type("both", [tech_point("重巡", "机动")],
                config["manual_targets"],
                config["target_limits"],
            )[0]
            self.assertEqual(row["train"]["name"], "古鹰")
            self.assertEqual(row["train_manual_count"], 1)
            self.assertTrue(row["train_items"][0]["manual"])
            self.assertEqual(row["idle_manual_count"], 0)

            config = toggle_manual_target(tech_point("重巡", "机动"), "is_trained", chosen["id"])
            self.assertEqual(config["manual_targets"], {})
            with self.assertRaises(ValueError):
                toggle_manual_target(tech_point("驱逐", "炮击"), "is_trained", chosen["id"])
            with self.assertRaises(ValueError):
                toggle_manual_target(tech_point("重巡", "机动"), "is_trained", 99999)
            with self.assertRaises(ValueError):
                toggle_manual_target(tech_point("重巡", "机动"), "is_trained", True)

    def test_target_limit_controls_manual_capacity_and_auto_fill(self):
        with isolated_app_data():
            with get_connection() as connection:
                ships = []
                for name, is_idling in [
                    ("古鹰", True),
                    ("青叶", True),
                    ("衣笠", True),
                    ("加古", True),
                ]:
                    ship = insert_ship(
                        connection,
                        name,
                        "2017-05-25",
                        {
                            "ship_type": "重巡",
                            "rarity": "精锐",
                            "tech_points": [
                                {
                                    "stage": "解锁图鉴",
                                    "attribute": "重巡 机动",
                                    "points": 0,
                                }
                            ],
                        },
                    )
                    ships.append(ship)
            for ship in ships:
                update_ship_status(ship["id"], "is_idling", True)
            save_next_ship_types([tech_point("重巡", "机动")])

            config = save_target_limit(tech_point("重巡", "机动"), "is_trained", 3)
            self.assertEqual(
                config["target_limits"][tech_point("重巡", "机动")],
                {"is_idling": 1, "is_trained": 3},
            )
            config = toggle_manual_target(tech_point("重巡", "机动"), "is_trained", ships[0]["id"])
            config = toggle_manual_target(tech_point("重巡", "机动"), "is_trained", ships[1]["id"])
            self.assertEqual(
                read_config()["manual_targets"][tech_point("重巡", "机动")]["is_trained"],
                [ships[0]["id"], ships[1]["id"]],
            )
            config = read_config()
            row = next_ships_by_type("both", [tech_point("重巡", "机动")],
                config["manual_targets"],
                config["target_limits"],
            )[0]
            names = [item["ship"]["name"] for item in row["train_items"]]
            self.assertEqual(len(names), 3)
            self.assertEqual(names[:2], ["古鹰", "青叶"])
            self.assertEqual(
                [item["manual"] for item in row["train_items"]],
                [True, True, False],
            )

            config = toggle_manual_target(tech_point("重巡", "机动"), "is_trained", ships[2]["id"])
            self.assertEqual(
                read_config()["manual_targets"][tech_point("重巡", "机动")]["is_trained"],
                [ships[0]["id"], ships[1]["id"], ships[2]["id"]],
            )
            with self.assertRaises(ValueError):
                toggle_manual_target(tech_point("重巡", "机动"), "is_trained", ships[3]["id"])

            config = save_target_limit(tech_point("重巡", "机动"), "is_trained", 1)
            self.assertEqual(
                read_config()["manual_targets"][tech_point("重巡", "机动")]["is_trained"],
                [ships[0]["id"]],
            )
            self.assertEqual(
                config["target_limits"][tech_point("重巡", "机动")]["is_trained"], 1
            )

    def test_save_next_ship_types_keeps_target_config(self):
        with isolated_app_data():
            with get_connection() as connection:
                ship = insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
            save_next_ship_types([tech_point("重巡", "机动")])
            save_target_limit(tech_point("重巡", "机动"), "is_idling", 2)
            toggle_manual_target(tech_point("重巡", "机动"), "is_idling", ship["id"])
            save_next_ship_types([tech_point("重巡", "机动")])
            config = read_config()
            self.assertEqual(
                config["manual_targets"],
                {tech_point("重巡", "机动"): {"is_idling": [ship["id"]]}},
            )
            self.assertEqual(
                config["target_limits"][tech_point("重巡", "机动")]["is_idling"], 2
            )
            save_next_ship_types([])
            config = read_config()
            self.assertEqual(config["manual_targets"], {})
            self.assertEqual(config["target_limits"], {})

    def test_target_limit_validation_and_stale_manual_target(self):
        with isolated_app_data():
            with get_connection() as connection:
                chosen = insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
                insert_ship(
                    connection,
                    "青叶",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
            save_next_ship_types([tech_point("重巡", "机动")])
            for invalid_limit in (0, 11, -1, True, "3", 2.5):
                with self.assertRaises(ValueError):
                    save_target_limit(tech_point("重巡", "机动"), "is_trained", invalid_limit)
            with self.assertRaises(ValueError):
                save_target_limit(tech_point("重巡", "机动"), "bad_field", 2)
            with self.assertRaises(ValueError):
                save_target_limit(tech_point("驱逐", "炮击"), "is_trained", 2)

            config = save_target_limit(tech_point("重巡", "机动"), "is_trained", 10)
            self.assertEqual(config["target_limits"][tech_point("重巡", "机动")]["is_trained"], 10)
            toggle_manual_target(tech_point("重巡", "机动"), "is_trained", chosen["id"])
            delete_ship(chosen["id"])

            # 删除舰船后应同步清理悬空的手动录入，且能立刻替换录入新舰娘。
            config = read_config()
            self.assertEqual(config["manual_targets"], {})
            replacement = list_ships({"ship_types": ["重巡"]})[0]
            config = toggle_manual_target(tech_point("重巡", "机动"), "is_trained", replacement["id"]
            )
            self.assertEqual(
                config["manual_targets"][tech_point("重巡", "机动")]["is_trained"], [replacement["id"]]
            )
            row = next_ships_by_type("both", [tech_point("重巡", "机动")],
                config["manual_targets"],
                config["target_limits"],
            )[0]
            self.assertFalse(row["train_manual_stale"])
            self.assertEqual(row["train_manual_count"], 1)

    def test_completing_manual_target_releases_slot(self):
        with isolated_app_data():
            with get_connection() as connection:
                chosen = insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
                insert_ship(
                    connection,
                    "青叶",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
            save_next_ship_types([tech_point("重巡", "机动")])
            toggle_manual_target(tech_point("重巡", "机动"), "is_idling", chosen["id"])
            self.assertEqual(
                read_config()["manual_targets"][tech_point("重巡", "机动")]["is_idling"],
                [chosen["id"]],
            )

            update_ship_status(chosen["id"], "is_idling", True)
            self.assertEqual(read_config()["manual_targets"], {})
            config = read_config()
            row = next_ships_by_type("both", [tech_point("重巡", "机动")],
                config["manual_targets"],
                config["target_limits"],
            )[0]
            self.assertEqual(row["idle"]["name"], "青叶")
            self.assertTrue(all(not item["manual"] for item in row["idle_items"]))

    def test_concurrent_config_updates_do_not_conflict(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
            save_next_ship_types([tech_point("重巡", "机动")])
            real_write = config_module.write_config

            def slow_write(config):
                import time

                time.sleep(0.02)
                real_write(config)

            with patch("blhx.storage.config.write_config", slow_write):
                with ThreadPoolExecutor(max_workers=8) as executor:
                    jobs = [
                        executor.submit(
                            save_target_limit, tech_point("重巡", "机动"), field, 3
                        )
                        for field in ("is_idling", "is_trained")
                    ]
                    for job in jobs:
                        job.result()

            limits = read_config()["target_limits"][tech_point("重巡", "机动")]
            self.assertEqual(limits["is_idling"], 3)
            self.assertEqual(limits["is_trained"], 3)

    def test_filter_options_expose_custom_ship_tags_last(self):
        handler = TrackerHandler.__new__(TrackerHandler)
        handler.path = "/api/filter-options"
        captured = {}
        handler.send_json = lambda payload: captured.update(payload)
        handler.do_GET()

        ship_types = captured["ship_types"]
        # 自定义标签并入舰种筛选项并排在常规舰种之后，顺序与 README 一致。
        self.assertEqual(ship_types, [*SHIP_TYPE_ORDER, *CUSTOM_SHIP_TAGS])
        self.assertEqual(ship_types[-2:], ["小船", "联动"])
        self.assertEqual(captured["custom_ship_tags"], ["小船", "联动"])

    def test_query_filters_supports_plural_ship_types(self):
        handler = TrackerHandler.__new__(TrackerHandler)
        handler.path = "/api/ships?ship_types=%E9%87%8D%E5%B7%A1&name=%E5%8F%A4"
        filters = handler.query_filters()
        self.assertEqual(filters["ship_types"], ["重巡"])
        self.assertEqual(filters["name"], "古")

        # 单数 ship_type 已不再支持，只有复数参数生效。
        handler.path = "/api/ships?ship_type=%E6%88%98%E5%88%97"
        self.assertNotIn("ship_types", handler.query_filters())

        handler.path = "/api/ships?pending=%E6%98%AF"
        self.assertEqual(handler.query_filters()["pending"], "是")

        handler.path = "/api/ships"
        self.assertNotIn("ship_types", handler.query_filters())
        self.assertEqual(handler.query_filters()["pending"], "")

    def test_training_completion_releases_idle_slot(self):
        with isolated_app_data():
            with get_connection() as connection:
                chosen = insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
                insert_ship(
                    connection,
                    "青叶",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
            save_next_ship_types([tech_point("重巡", "机动")])
            toggle_manual_target(tech_point("重巡", "机动"), "is_idling", chosen["id"])
            update_ship_status(chosen["id"], "is_trained", True)
            # 练级完成会同时把挂机置为完成，挂机名额必须一并释放。
            self.assertEqual(read_config()["manual_targets"], {})

            replacement = [
                ship for ship in list_ships({"ship_types": ["重巡"]})
                if ship["name"] == "青叶"
            ][0]
            config = toggle_manual_target(tech_point("重巡", "机动"), "is_idling", replacement["id"]
            )
            self.assertEqual(
                config["manual_targets"][tech_point("重巡", "机动")]["is_idling"],
                [replacement["id"]],
            )

    def test_completed_ship_rejected_as_manual_target(self):
        with isolated_app_data():
            with get_connection() as connection:
                done = insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
            save_next_ship_types([tech_point("重巡", "机动")])
            update_ship_status(done["id"], "is_trained", True)
            with self.assertRaises(ValueError):
                toggle_manual_target(tech_point("重巡", "机动"), "is_trained", done["id"])

    def test_release_manual_target_uses_config_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        with isolated_app_data():
            with get_connection() as connection:
                ship = insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
            save_next_ship_types([tech_point("重巡", "机动")])
            toggle_manual_target(tech_point("重巡", "机动"), "is_idling", ship["id"])
            real_write = config_module.write_config

            def slow_write(config):
                import time

                time.sleep(0.05)
                real_write(config)

            with patch("blhx.storage.config.write_config", slow_write):
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [
                        executor.submit(release_manual_target, ship["id"]),
                        executor.submit(
                            save_target_limit,
                            tech_point("重巡", "机动"),
                            "is_trained",
                            3,
                        ),
                    ]
                    for future in futures:
                        future.result()

            config = read_config()
            self.assertEqual(config["target_limits"][tech_point("重巡", "机动")]["is_trained"], 3)
            self.assertEqual(config["manual_targets"], {})

    def test_clear_ships_serialized_with_config_writes(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
            save_next_ship_types([tech_point("重巡", "机动")])
            real_write = config_module.write_config

            def slow_write(config):
                import time

                time.sleep(0.05)
                real_write(config)

            with patch("blhx.storage.config.write_config", slow_write):
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [
                        executor.submit(save_target_limit, tech_point("重巡", "机动"), "is_trained", 3),
                        executor.submit(clear_ships),
                    ]
                    for future in futures:
                        future.result()

            # 无论谁先执行，清空后配置都必须回到空；不允许旧快照把配置写回来。
            config = read_config()
            if list_ships({}):
                self.assertEqual(config["target_limits"][tech_point("重巡", "机动")]["is_trained"], 3)
            else:
                self.assertEqual(
            config,
            {
                "next_ship_types": [],
                "target_limits": {},
                "manual_targets": {},
                "armament_target_limits": _default_armament_limits(),
                "armament_manual_targets": {},
            },
        )

    def test_page_script_conflict_and_safety_guards(self):
        html = PAGE_HTML
        # 属性注入防护
        self.assertIn("const escapeAttr = value =>", html)
        self.assertNotIn('data-name="${escapeHtml(', html)
        self.assertNotIn('data-id="${escapeHtml(', html)
        # 自动保存与通知
        self.assertIn("showToast('推荐舰种已更新')", html)
        self.assertNotIn("confirmNextButton", html)
        # 串行保存与配置回读保护
        self.assertIn("function syncNextTypes()", html)
        self.assertIn("const stale = versionAtStart !== selectionVersion;", html)
        self.assertIn("applyRemoteConfig(result, stale || typesDirty || hadPendingSave);", html)
        # 推荐舰种顺序（由后端下发顺序决定）与空行隐藏
        self.assertIn("function recommendSortKey(shipType) {", html)
        self.assertIn("nextTargetOrder", html)
        self.assertIn("function rowHasTargets(row) {", html)
        self.assertIn("所选舰种暂无可推荐舰娘", html)
        # 默认仅展示需要练级的舰船，已练级不预设筛选；重置筛选后保持一致。
        self.assertIn("needs_training: '是',", html)
        self.assertIn("filterState.needs_training = '是';", html)
        # 选中小船/联动这类自定义标签时，“需要练级”自动回到“全部”。
        self.assertIn("filterOptions.customShipTags.includes(chip.dataset.value)", html)
        self.assertIn("filterState.needs_training = '';", html)
        self.assertIn("is_trained: ''", html)
        self.assertIn("filterState.is_trained = '';", html)
        # 已挂机/已练级筛选行展示未处理数量，数据来自 /api/ships 的 status_counts。
        self.assertIn("pendingLabel: '未挂机'", html)
        self.assertIn("pendingLabel: '未练级'", html)
        self.assertIn("result.status_counts", html)
        # “未完成”筛选排在“已练级”之后，与后端 pending 参数对应。
        self.assertIn(
            "{ key: 'pending', label: '完成状态', options: ['', '是'], optionLabels: { '': '全部', '是': '未完成' } }",
            html,
        )
        # 默认选中“未完成”，重置筛选后保持一致。
        self.assertIn("pending: '是'", html)
        self.assertIn("filterState.pending = '是';", html)
        # 兵装筛选也有一套同样的完成状态筛选，这里只校验舰船筛选块内的顺序。
        ship_filter_block = html[html.index("function renderFilters() {"):html.index("async function completeShipStatus")]
        self.assertLess(
            ship_filter_block.index("{ key: 'is_trained', label: '已练级'"),
            ship_filter_block.index("{ key: 'pending', label: '完成状态'"),
        )
        # 未挂机/未练级数量排在筛选项按钮之后，与筛选按钮同一行展示。
        options_loop = ship_filter_block.index("${group.options.map(option => `")
        pending_count = ship_filter_block.index("${group.pendingLabel && statusCounts[group.key] !== null")
        self.assertLess(options_loop, pending_count)
        # 导入复用同步进度弹窗，完成后下载结果并整页刷新
        self.assertIn("function showImportOverlay()", html)
        self.assertIn("async function refreshImportStatus()", html)
        self.assertIn("function scheduleImportReload()", html)
        self.assertIn("setTimeout(() => window.location.reload(), 1200);", html)
        self.assertIn("scheduleImportReload();", html)
        self.assertIn('id="syncDownload"', html)
        self.assertIn("beginProgress('import',", html)
        # 表格按钮顺序
        idle = html.index(">挂机完成</button>")
        trained = html.index(">练级完成</button>")
        clear = html.index(">清除数据</button>")
        delete = html.index(">删除</button>", clear)
        self.assertLess(idle, trained)
        self.assertLess(trained, clear)
        self.assertLess(clear, delete)

    def test_page_mobile_layout_support(self):
        html = PAGE_HTML
        self.assertIn('content="width=device-width, initial-scale=1, viewport-fit=cover"', html)
        self.assertIn("@media (max-width:820px)", html)
        # 断点以上表格在容器内横向滚动，避免 700px 左右宽度撑破整页。
        self.assertIn('<div class="table-scroll">', html)
        self.assertIn(".table-scroll table { min-width:900px; }", html)
        self.assertIn('.ships-table thead { display:none; }', html)
        self.assertIn(".ships-table td::before { content:attr(data-label);", html)
        # 状态为空时仍保留字段名，并显示“未记录”，避免卡片式布局丢行。
        self.assertIn('.ships-table td:empty::after { content:"未记录";', html)
        self.assertIn(".ships-table td[colspan]", html)
        self.assertIn('.next-table thead { display:none; }', html)
        # 每个数据单元格都带移动端标签，卡片布局才有维度名称。
        for label in ("舰船名称", "舰种", "稀有度", "获得", "120级", "实装日期", "是否需要练级", "是否已挂机", "是否已练级", "操作", "挂机目标", "练级目标"):
            self.assertIn(f'data-label="{label}"', html)
        self.assertIn('env(safe-area-inset-bottom)', html)

    def test_page_tabs_split_recommend_training_and_equipment(self):
        html = PAGE_HTML
        # 三个 Tab 与对应面板一一绑定，且只有推荐页面默认展开。
        self.assertIn('role="tablist"', html)
        for tab, panel, label in (
            ("recommend", "panelRecommend", "推荐页面"),
            ("training", "panelTraining", "练级列表"),
            ("equipment", "panelEquipment", "兵装列表"),
        ):
            self.assertIn(f'data-tab="{tab}"', html)
            self.assertIn(f'id="{panel}"', html)
            self.assertIn(f'aria-controls="{panel}"', html)
            self.assertIn(f'aria-labelledby="tab{tab.capitalize()}"', html)
            self.assertIn(label, html)
        self.assertIn('id="panelRecommend" aria-labelledby="tabRecommend"', html)
        self.assertIn('id="panelTraining" aria-labelledby="tabTraining" hidden', html)
        self.assertIn('id="panelEquipment" aria-labelledby="tabEquipment" hidden', html)
        # 推荐配置与推荐目标留在推荐页，舰船筛选表格迁到练级列表，不同时出现在两个面板。
        recommend_panel = html[html.index('id="panelRecommend"'):html.index('id="panelTraining"')]
        training_panel = html[html.index('id="panelTraining"'):html.index('id="panelEquipment"')]
        self.assertIn('id="nextTypeOptions"', recommend_panel)
        self.assertIn('id="nextTargets"', recommend_panel)
        self.assertNotIn('id="shipTable"', recommend_panel)
        self.assertIn('id="shipTable"', training_panel)
        self.assertIn('id="filterGroups"', training_panel)
        self.assertNotIn('id="nextTargets"', training_panel)
        # 兵装列表独立成表，与舰船表格、推荐目标互不混用。
        equipment_panel = html[html.index('id="panelEquipment"'):html.index('<script>')]
        self.assertIn('id="armamentTable"', equipment_panel)
        self.assertIn('id="armamentFilterGroups"', equipment_panel)
        self.assertIn('是否联动', equipment_panel)
        self.assertNotIn('id="shipTable"', equipment_panel)
        self.assertNotIn('id="nextTargets"', equipment_panel)
        # 兵装推荐挂在推荐页，与舰船推荐并列。
        self.assertIn('id="armamentNextTargets"', recommend_panel)
        self.assertNotIn('id="armamentNextTargets"', training_panel)
        # 切换逻辑：选中态同步、hash 记忆、键盘导航。
        self.assertIn("function switchTab(name, focusTab = false) {", html)
        self.assertIn("button.setAttribute('aria-selected', String(selected));", html)
        self.assertIn("document.getElementById(item.panel).hidden = !selected;", html)
        self.assertIn("function tabFromHash() {", html)
        self.assertIn("const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];", html)
        self.assertIn("if (!keys.includes(event.key)) return;", html)
        self.assertIn('switchTab(tabFromHash());', html)
        self.assertIn(".tab-panel[hidden] { display:none; }", html)
        self.assertIn('.tab[aria-selected="true"]', html)

    def test_page_tabs_mobile_friendly(self):
        html = PAGE_HTML
        mobile = html[html.index("@media (max-width:820px)"):html.index("</style>")]
        # 窄屏三个 Tab 等分并可横向滚动，避免中文标签挤在一起或撑破页面。
        self.assertIn(".tabs { gap:6px; overflow-x:auto;", mobile)
        self.assertIn(".tab { flex:1 0 auto; min-width:0;", mobile)
        self.assertIn(".tabs::-webkit-scrollbar { display:none; }", mobile)
        # 刘海屏横屏时左右安全区，避免 Tab 与卡片贴边或压到圆角区域。
        self.assertIn("padding:18px max(12px,env(safe-area-inset-right))", mobile)
        self.assertIn("env(safe-area-inset-left)", mobile)
        # 触屏没有真实悬停，hover 样式只在支持悬停的设备生效，避免选中态粘连。
        self.assertIn("@media (hover:hover) { .tab:hover {", html)
        # <a class="button"> 在工具栏拉伸时文字必须居中，否则小屏会贴左。
        self.assertIn("a.button { display:inline-flex; align-items:center; justify-content:center;", html)
        self.assertIn(".toolbar > button, .toolbar > .button { width:100%;", mobile)
        # 切换 Tab 回到顶部，解决移动端长列表滚到底后切换看到空白的问题。
        self.assertIn("if (changed && window.scrollTo) window.scrollTo(0, 0);", html)
        self.assertIn("const changed = activeTab !== target.tab;", html)

    def test_page_tab_switching(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("未安装 node，跳过 Tab 切换回归")
        fixture = Path(__file__).with_name("fixtures") / "page_tabs.mjs"
        result = subprocess.run(
            [node, str(fixture)], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("断言: true", result.stdout)

    def test_import_triggers_full_page_reload(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("未安装 node，跳过前端导入刷新回归")
        fixture = Path(__file__).with_name("fixtures") / "import_reload.mjs"
        result = subprocess.run(
            [node, str(fixture)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(fixture).resolve().parents[2]),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("断言: true", result.stdout)

    def test_config_race_handles_late_get(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("未安装 node，跳过前端竞态回归")
        fixture = Path(__file__).with_name("fixtures") / "config_race.mjs"
        result = subprocess.run(
            [node, str(fixture)], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("断言A: true", result.stdout)
        self.assertIn("断言B: true", result.stdout)

    def test_page_writes_keep_loading_until_refresh_finishes(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("未安装 node，跳过前端写操作 loading 回归")
        fixture = Path(__file__).with_name("fixtures") / "page_loading.mjs"
        result = subprocess.run(
            [node, str(fixture)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(fixture).resolve().parents[2]),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("断言: true", result.stdout)

    def test_recommend_order_and_empty_types_hidden(self):
        """推荐行按固定展示顺序排列；没有候选的行必须隐藏。"""
        with isolated_app_data():
            with get_connection() as connection:
                for name, ship_type, attribute in [
                    ("运输一", "运输", "耐久"),
                    ("轻巡一", "轻巡", "命中"),
                    ("驱逐一", "驱逐", "机动"),
                    ("轻航一", "轻航", "装填"),
                    ("战列一", "战列", "耐久"),
                    ("重炮一", "重炮", "耐久"),
                ]:
                    insert_ship(
                        connection,
                        name,
                        "2024-01-01",
                        {
                            "ship_type": ship_type,
                            "rarity": "精锐",
                            "tech_points": [
                                {
                                    "stage": "解锁图鉴",
                                    "attribute": f"{ship_type} {attribute}",
                                    "points": 0,
                                }
                            ],
                        },
                    )
            rows = next_ships_by_type(
                "both", ["运输", "轻巡", "驱逐", "轻航", "战列", "重炮"]
            )
            # 固定顺序：战列 > 战巡 > 正航 > 轻航 > 航战 > 重炮 > 超巡 > 重巡 >
            # 轻巡 > 驱逐 > 其他（按名称）> 标签。这里命中的是战列/轻航/重炮/轻巡/驱逐/运输，
            # 运输属于“其他”，排在所有固定舰种之后。
            self.assertEqual(
                [row["ship_type"] for row in rows],
                ["战列", "轻航", "重炮", "轻巡", "驱逐", "运输"],
            )

            # 配置了但没有对应舰船的舰种行不出现。
            rows = next_ships_by_type("both", ["运输", "潜艇"])
            self.assertEqual([row["ship_type"] for row in rows], ["运输"])

    def test_recommend_orders_by_attribute_priority(self):
        """同一加成舰种内按该舰种优先级表挑人。

        配置「重巡」这一行时，两艘船都属于重巡；排序看各自在该舰种下的
        最优属性：机动在重巡表排第 0、装填排第 2，因此动机会被优先推荐，
        与实装日期和名称无关。
        """
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "一装填",
                    "2024-01-01",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡/重炮/超巡 装填", "points": 0}
                        ],
                    },
                )
                insert_ship(
                    connection,
                    "二机动",
                    "2023-01-01",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡/重炮/超巡 机动", "points": 0}
                        ],
                    },
                )
            # 名称刻意让装填舰船字典序更靠前：只有真正比较属性优先级才会选「二机动」。
            self.assertEqual(
                next_ship("idle", ship_types=["重巡"])["name"], "二机动"
            )
            rows = next_ships_by_type(
                "idle",
                ["重巡"],
                target_limits={"重巡": {"is_idling": 2, "is_trained": 1}},
            )
            self.assertEqual(
                [item["ship"]["name"] for item in rows[0]["idle_items"]],
                ["二机动", "一装填"],
            )

    def test_recommend_hides_tech_points_without_targets(self):
        """挂机与练级都没有目标项的科技点行必须隐藏。"""
        with isolated_app_data():
            with get_connection() as connection:
                only_idle = insert_ship(
                    connection,
                    "战列一",
                    "2024-01-01",
                    {
                        "ship_type": "战列",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "战列 耐久", "points": 0}
                        ],
                    },
                )
                done = insert_ship(
                    connection,
                    "重巡一",
                    "2024-01-01",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
                idle_and_train = insert_ship(
                    connection,
                    "重巡二",
                    "2024-01-02",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
                insert_ship(
                    connection,
                    "驱逐一",
                    "2024-01-01",
                    {
                        "ship_type": "驱逐",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "驱逐 机动", "points": 0}
                        ],
                    },
                )
            # 重巡全部已完成，两个模式都无候选，不应出现在结果中。
            update_ship_status(done["id"], "is_trained", True)
            update_ship_status(idle_and_train["id"], "is_trained", True)
            update_ship_status(only_idle["id"], "is_idling", True)

            keys = [
                tech_point("战列", "耐久"),
                tech_point("重巡", "机动"),
                tech_point("驱逐", "机动"),
            ]
            rows = next_ships_by_type("both", keys)
            # 重巡行两个模式都为空被过滤；战列只剩练级目标。
            # 固定顺序里战列在驱逐之前。
            self.assertEqual(
                [row["ship_type"] for row in rows], ["战列", "驱逐"]
            )
            by_type = {row["ship_type"]: row for row in rows}
            self.assertTrue(by_type["战列"]["train_items"])
            self.assertFalse(by_type["战列"]["idle_items"])

    def test_recommend_normalizes_ship_type_aliases(self):
        """风帆M/S/V 在加成舰种里统一归一为「风帆」，科技点行不会重复。"""
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "皇家财富号",
                    "2024-01-01",
                    {
                        "ship_type": "风帆S",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "风帆 耐久", "points": 0}
                        ],
                    },
                )
            # 推荐选项是全部加成舰种，风帆只出现一次（别名已归一）。
            options = [option["ship_type"] for option in available_next_ship_types()]
            self.assertEqual(options.count("风帆"), 1)
            rows = next_ships_by_type("both", ["风帆"])
            self.assertEqual([row["ship_type"] for row in rows], ["风帆"])

    def test_recommend_rows_expose_pending_counts(self):
        with isolated_app_data():
            with get_connection() as connection:
                idle_ship = insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {
                        "ship_type": "战列",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "战列 耐久", "points": 0}
                        ],
                    },
                )
                fresh_ship = insert_ship(
                    connection,
                    "长门",
                    "2023-08-17",
                    {
                        "ship_type": "战列",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "战列 耐久", "points": 0}
                        ],
                    },
                )
                done_ship = insert_ship(
                    connection,
                    "古鹰",
                    "2023-08-17",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
            # 武藏只挂机（待练级），长门未记录（待挂机），古鹰两项都完成不计入。
            update_ship_status(idle_ship["id"], "is_idling", True)
            update_ship_status(done_ship["id"], "is_idling", True)
            update_ship_status(done_ship["id"], "is_trained", True)

            key = tech_point("战列", "耐久")
            rows = {row["ship_type"]: row for row in next_ships_by_type("both", [key])}
            self.assertEqual(rows[key]["idle_count"], 1)
            self.assertEqual(rows[key]["train_count"], 1)

            # 计数口径与推荐徽标一致，避免两处统计漂移。
            option = next(
                item for item in available_next_ship_types() if item["ship_type"] == key
            )
            self.assertEqual(rows[key]["idle_count"], option["idle_count"])
            self.assertEqual(rows[key]["train_count"], option["train_count"])

    def test_tag_recommendation_options_include_tags_last(self):
        """推荐目标 = 15 个加成舰种 + 2 个自定义标签，标签排在全部舰种之后。"""
        with isolated_app_data():
            # 固定展示顺序：战列 > 战巡 > 正航 > 轻航 > 航战 > 重炮 > 超巡 > 重巡 >
            # 轻巡 > 驱逐 > 其他（按名称）> 标签。
            self.assertEqual(
                [item["ship_type"] for item in available_next_ship_types()],
                [
                    *RECOMMEND_SHIP_TYPE_DISPLAY_ORDER,
                    # 未列入固定顺序的加成舰种归入“其他”，按名称拼音排序。
                    "风帆", "潜母", "潜艇", "维修", "运输",
                    *CUSTOM_SHIP_TAGS,
                ],
            )
            options = {item["ship_type"]: item for item in available_next_ship_types()}
            self.assertFalse(options["驱逐"]["is_tag"])
            self.assertTrue(options["小船"]["is_tag"])
            self.assertTrue(options["联动"]["is_tag"])
            # 标签没有属性优先级表。
            self.assertFalse(options["小船"]["has_priority_table"])
            self.assertEqual(options["小船"]["priority"], [])
            # 标签可以与舰种一起配置，落盘同样按固定展示顺序。
            self.assertEqual(
                save_next_ship_types(["联动", "驱逐", "小船"]),
                ["驱逐", "小船", "联动"],
            )

    def test_tag_recommendation_matches_tagged_ships_without_bonus(self):
        """标签船通常没有科技点，推荐不能附加“需要练级 = 是”。"""
        with isolated_app_data():
            with get_connection() as connection:
                small = insert_ship(
                    connection,
                    "小企业",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )
                plain = insert_ship(
                    connection,
                    "企业",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "海上传奇", "tech_points": []},
                )
            with get_connection() as connection:
                refresh_ship_tags(connection)
            # 无科技点 → 不满足 needs_training，但标签行仍应命中。
            self.assertFalse(
                list_ships({"name": "小企业"})[0]["needs_training"]
            )
            rows = next_ships_by_type("both", ["小船"])
            self.assertEqual([row["ship_type"] for row in rows], ["小船"])
            self.assertTrue(rows[0]["is_tag"])
            self.assertEqual(
                [item["ship"]["name"] for item in rows[0]["idle_items"]],
                ["小企业"],
            )
            # 非标签船不会被带进标签行。
            self.assertEqual(
                [ship["name"] for ship in list_ships({"ship_types": ["小船"]})],
                ["小企业"],
            )
            self.assertNotIn(
                plain["id"],
                [item["ship"]["id"] for item in rows[0]["idle_items"]],
            )
            self.assertIn(
                small["id"],
                [item["ship"]["id"] for item in rows[0]["idle_items"]],
            )

    def test_tag_recommendation_sorts_by_rarity_then_date(self):
        """标签行按稀有度 > 实装日期 > 名称排序（标签船没有属性优先级）。"""
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "小乙",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "超稀有", "tech_points": []},
                )
                insert_ship(
                    connection,
                    "小甲",
                    "2023-01-01",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )
            with get_connection() as connection:
                refresh_ship_tags(connection)
            rows = next_ships_by_type(
                "idle",
                ["小船"],
                target_limits={"小船": {"is_idling": 3, "is_trained": 1}},
            )
            # 稀有度优先级：超稀有低于精锐，因此「小甲」在前；同名次再按日期。
            self.assertEqual(
                [item["ship"]["name"] for item in rows[0]["idle_items"]],
                ["小甲", "小乙"],
            )

    def test_tag_recommendation_keeps_rows_separate_from_ship_types(self):
        """同一艘船既属于加成舰种又带标签时，分别出现在各自的行里。"""
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "小天鹅",
                    "2024-01-01",
                    {
                        "ship_type": "驱逐",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "驱逐 机动", "points": 0}
                        ],
                    },
                )
            with get_connection() as connection:
                refresh_ship_tags(connection)
            # 有科技点的船不算“小船”（小船的规则要求无科技点）。
            self.assertEqual(list_ships({"ship_types": ["小船"]}), [])
            rows = next_ships_by_type("both", ["驱逐", "小船"])
            self.assertEqual([row["ship_type"] for row in rows], ["驱逐"])
            self.assertFalse(rows[0]["is_tag"])

    def test_custom_small_ship_tag_filter_and_recommendation(self):
        """标签“小船”= 无科技点且名称以“小”开头，可筛选、可推荐、排在最后。"""
        with isolated_app_data():
            with get_connection() as connection:
                small = insert_ship(
                    connection,
                    "小企业",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )
                # 名称以“小”开头但有科技点的仍属原舰种，不归入小船。
                trainable = insert_ship(
                    connection,
                    "小天鹅",
                    "2024-01-01",
                    {
                        "ship_type": "驱逐",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "驱逐 机动", "points": 0}
                        ],
                    },
                )
                # 无科技点但名称不以“小”开头的不归入小船。
                plain = insert_ship(
                    connection,
                    "企业",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )

            self.assertIn("小船", CUSTOM_SHIP_TAGS)
            self.assertEqual(
                [ship["name"] for ship in list_ships({"ship_types": ["小船"]})],
                ["小企业"],
            )
            # 小天鹅有科技点、企业无“小”前缀，都不在小船内。
            self.assertEqual(
                [
                    ship["name"]
                    for ship in list_ships({"ship_types": ["航母", "小船"]})
                ],
                ["企业", "小企业"],
            )
            # 推荐候选固定带“需要练级 = 是”，选中小船时不能因此被过滤为空。
            self.assertEqual(
                [
                    ship["name"]
                    for ship in list_ships(
                        {"ship_types": ["小船"], "needs_training": "是"}
                    )
                ],
                ["小企业"],
            )
            self.assertEqual(
                sorted(
                    ship["name"]
                    for ship in list_ships(
                        {"ship_types": ["驱逐", "小船"], "needs_training": "是"}
                    )
                ),
                sorted(["小天鹅", "小企业"]),
            )
            # 未选小船时，“需要练级 = 是”仍然排除无科技点的舰船。
            self.assertNotIn(
                "企业",
                [ship["name"] for ship in list_ships({"needs_training": "是"})],
            )

            # 推荐以加成舰种为单位：只有带科技点的舰船能进入推荐候选。
            self.assertEqual(
                [row["ship_type"] for row in next_ships_by_type("both", ["驱逐"])],
                ["驱逐"],
            )
            rows = next_ships_by_type("both", ["驱逐"])
            self.assertEqual([row["ship_type"] for row in rows], ["驱逐"])
            self.assertEqual(
                [item["ship"]["name"] for item in rows[0]["idle_items"]], ["小天鹅"]
            )
            # 无科技点的小船（小企业）不会出现在任何推荐行里。
            self.assertEqual(
                [ship["name"] for ship in list_ships({"ship_types": ["小船"]})],
                ["小企业"],
            )

            # 兵装列表的舰种筛选同样支持小船；小船没有兵装时结果为空。
            self.assertEqual(list_armaments({"ship_type": "小船"}), [])
            with get_connection() as connection:
                upsert_armament(
                    connection,
                    small["id"],
                    {"release_date": "2024-02-01", "is_collab": False},
                )
            self.assertEqual(
                [item["name"] for item in list_armaments({"ship_type": "小船"})],
                ["小企业"],
            )

    def test_ship_tags_written_on_sync_and_orthogonal_to_ship_type(self):
        """标签写入 ships.tags，且与舰种正交：小船既能被标签筛到也能被原舰种筛到。"""
        with isolated_app_data():
            with get_connection() as connection:
                # insert_ship 是 Wikidata 同步的写入口，标签在此时落库。
                insert_ship(
                    connection,
                    "小企业",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )
                insert_ship(
                    connection,
                    "企业",
                    "2024-01-01",
                    {
                        "ship_type": "航母",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "航空", "points": 0}
                        ],
                    },
                )
            with get_connection() as connection:
                rows = {
                    row["name"]: row["tags"]
                    for row in connection.execute("SELECT name, tags FROM ships")
                }
            # 同步时按规则写入；不命中的写成空串，便于重刷后清掉残留。
            self.assertEqual(rows["小企业"], "小船")
            self.assertEqual(rows["企业"], "")

            # 与舰种正交：标签筛选和原舰种筛选都能命中同一艘船。
            self.assertEqual(
                [ship["name"] for ship in list_ships({"ship_types": ["小船"]})],
                ["小企业"],
            )
            self.assertEqual(
                [ship["name"] for ship in list_ships({"ship_types": ["航母"]})],
                ["企业", "小企业"],
            )
            # 多个选项取并集，不会因为选中标签而把原舰种排除掉。
            self.assertEqual(
                sorted(
                    ship["name"]
                    for ship in list_ships({"ship_types": ["航母", "小船"]})
                ),
                sorted(["企业", "小企业"]),
            )

            # 接口返回 tags 数组，供前端展示。
            tagged = list_ships({"ship_types": ["小船"]})[0]
            self.assertEqual(tagged["tags"], ["小船"])
            self.assertEqual(list_ships({"ship_types": ["航母"]})[0]["tags"], [])

    def test_category_tag_without_sync_matches_nothing(self):
        """“联动”是名称集合型标签：未同步（tags 为空）时命中 0，而不是报错。"""
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "小企业",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )
            self.assertIn("联动", CUSTOM_SHIP_TAGS)
            # 联动不由本地字段判定，规则为 None，成员来自 Wiki 分类。
            self.assertIsNone(ship_tag_condition("联动"))
            self.assertIn("联动", CATEGORY_SHIP_TAGS)
            self.assertEqual(list_ships({"ship_types": ["联动"]}), [])
            # 标签不是加成舰种：无科技点的舰船不产生推荐候选。
            self.assertEqual(next_ships_by_type("both", ["联动"]), [])
            self.assertEqual(
                next_ships_by_type("both", ["重巡"]), []
            )

    def test_untagged_ships_are_not_matched_by_tag_filter(self):
        """标签以同步产出的 tags 为准：未重刷的存量数据不会凭空命中标签。

        标签不是从本地字段现算的，因此未同步时筛不到小船，原舰种筛选照常可用。
        """
        with isolated_app_data():
            with get_connection() as connection:
                ship = insert_ship(
                    connection,
                    "小企业",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )
                # 模拟存量行：tags 列刚补上，值为空串。
                connection.execute(
                    "UPDATE ships SET tags = '' WHERE id = ?", (ship["id"],)
                )

            tagged = list_ships({"ship_types": ["小船"]})
            self.assertEqual(tagged, [])
            # 舰种筛选不依赖标签，未同步也能正常命中。
            self.assertEqual(
                [item["name"] for item in list_ships({"ship_types": ["航母"]})],
                ["小企业"],
            )
            self.assertEqual(list_ships({"ship_types": ["航母"]})[0]["tags"], [])
            # 无科技点的舰船不产生推荐候选。
            self.assertEqual(next_ships_by_type("both", ["航母"]), [])

            # 重刷同步后标签生效。
            with get_connection() as connection:
                refresh_ship_tags(connection)
            self.assertEqual(
                [item["name"] for item in list_ships({"ship_types": ["小船"]})],
                ["小企业"],
            )

    def test_refresh_ship_tags_backfills_existing_rows(self):
        """全量同步重刷会补齐 tags，且能清掉不再命中的历史标签。"""
        with isolated_app_data():
            with get_connection() as connection:
                ship = insert_ship(
                    connection,
                    "小企业",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )
                connection.execute(
                    "UPDATE ships SET tags = '联动' WHERE id = ?", (ship["id"],)
                )
                updated = refresh_ship_tags(connection, ship["id"])
                row = connection.execute(
                    "SELECT tags FROM ships WHERE id = ?", (ship["id"],)
                ).fetchone()
            self.assertEqual(updated, 1)
            self.assertEqual(row["tags"], "小船")

    def test_full_sync_backfills_ship_tags(self):
        """全量同步结束时会重算全部标签，存量数据跑一次即可补齐。"""
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "小企业",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )
                # 模拟升级前的存量行：tags 列刚补上，值为空串。
                connection.execute("UPDATE ships SET tags = ''")
            with patch(
                "blhx.datasource.wiki.fetch_release_entries",
                return_value=[{"name": "小企业", "release_date": "2024-01-01"}],
            ), patch("blhx.datasource.wiki.fetch_armament_entries", return_value=[]):
                _reset_sync_state()
                _sync_wiki()
            with get_connection() as connection:
                tags = connection.execute(
                    "SELECT tags FROM ships WHERE name = '小企业'"
                ).fetchone()["tags"]
            self.assertEqual(tags, "小船")
            self.assertEqual(SYNC_STATE["errors"], [])
            _reset_sync_state()

    def test_partial_sync_backfills_only_matched_ships(self):
        """少量同步只刷新本次同步的舰船标签。"""
        with isolated_app_data():
            with get_connection() as connection:
                for name in ("小企业", "小赤城"):
                    insert_ship(
                        connection,
                        name,
                        "2024-01-01",
                        {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                    )
                connection.execute("UPDATE ships SET tags = ''")
            with patch(
                "blhx.datasource.wiki.fetch_release_entries",
                return_value=[{"name": "小企业", "release_date": "2024-01-01"}],
            ), patch("blhx.datasource.wiki.fetch_ship_payload") as payload, patch(
                "blhx.datasource.wiki.fetch_armament_entries", return_value=[]
            ):
                payload.return_value = {
                    "name": "小企业",
                    "release_date": "2024-01-01",
                    "ship_type": "航母",
                    "rarity": "精锐",
                    "tech_points": [],
                }
                _reset_sync_state()
                _sync_wiki(["小企业"])
            with get_connection() as connection:
                tags = {
                    row["name"]: row["tags"]
                    for row in connection.execute("SELECT name, tags FROM ships")
                }
            self.assertEqual(tags["小企业"], "小船")
            # 未参与本次同步的舰船保持原样，留给全量同步处理。
            self.assertEqual(tags["小赤城"], "")
            _reset_sync_state()

    def test_tag_filter_reads_tags_column_not_rule(self):
        """筛选只看 tags 列：手动写入的标签可筛到，规则满足但未写标签的筛不到。"""
        with isolated_app_data():
            with get_connection() as connection:
                # 名字带“小”且无科技点，规则满足——但故意不写 tags。
                no_tag = insert_ship(
                    connection,
                    "小企业",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )
                # 名字不以“小”开头，但手动写入 tags，应被筛到。
                with_tag = insert_ship(
                    connection,
                    "企业",
                    "2024-01-01",
                    {
                        "ship_type": "航母",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "航空", "points": 0}
                        ],
                    },
                )
                connection.execute(
                    "UPDATE ships SET tags = '' WHERE id = ?", (no_tag["id"],)
                )
                connection.execute(
                    "UPDATE ships SET tags = '小船' WHERE id = ?", (with_tag["id"],)
                )

            self.assertEqual(
                [item["name"] for item in list_ships({"ship_types": ["小船"]})],
                ["企业"],
            )
            # 舰种筛选与标签互不影响：企业仍是航母，小企业也仍是航母。
            self.assertEqual(
                sorted(
                    item["name"] for item in list_ships({"ship_types": ["航母"]})
                ),
                sorted(["企业", "小企业"]),
            )

    def test_tag_match_is_not_substring(self):
        """标签按 `/` 分隔成员精确匹配，避免子串误命中。"""
        with isolated_app_data():
            with get_connection() as connection:
                ship = insert_ship(
                    connection,
                    "测试舰",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )
                # 写入一个包含“小船”子串但不等于“小船”的标签。
                connection.execute(
                    "UPDATE ships SET tags = '小小船' WHERE id = ?", (ship["id"],)
                )
            self.assertEqual(list_ships({"ship_types": ["小船"]}), [])
            connection_tags = list_ships({"ship_types": ["航母"]})[0]["tags"]
            self.assertEqual(connection_tags, ["小小船"])

    def test_collab_tag_from_category_members(self):
        """联动标签按 Wiki「分类:联动舰娘」成员写入，并支持筛选与推荐。"""
        with isolated_app_data():
            with get_connection() as connection:
                collab = insert_ship(
                    connection,
                    "环",
                    "2020-11-26",
                    {
                        "ship_type": "重巡",
                        "rarity": "超稀有",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
                normal = insert_ship(
                    connection,
                    "古鹰",
                    "2020-11-26",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
                # 联动舰船即使无科技点也应打标签，与“小船”规则互不影响。
                small_collab = insert_ship(
                    connection,
                    "小赤城",
                    "2020-11-26",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )

            with get_connection() as connection:
                refresh_ship_tags(
                    connection, category_tags={COLLAB_SHIP_TAG: {"环", "小赤城"}}
                )
                tags = {
                    row["name"]: row["tags"]
                    for row in connection.execute("SELECT name, tags FROM ships")
                }
            self.assertEqual(tags["环"], "联动")
            self.assertEqual(tags["古鹰"], "")
            # 一个舰船可以同时命中多个标签。
            self.assertEqual(tags["小赤城"], "小船/联动")

            self.assertEqual(
                sorted(item["name"] for item in list_ships({"ship_types": ["联动"]})),
                sorted(["环", "小赤城"]),
            )
            # 标签与舰种正交：重巡筛选照常包含联动船。
            self.assertEqual(
                sorted(item["name"] for item in list_ships({"ship_types": ["重巡"]})),
                sorted(["环", "古鹰"]),
            )
            self.assertEqual(
                [item["name"] for item in list_ships({"ship_types": ["小船"]})],
                ["小赤城"],
            )
            # 接口返回的 tags 是数组，供前端展示。
            self.assertEqual(
                list_ships({"ship_types": ["小船"]})[0]["tags"], ["小船", "联动"]
            )

            # 推荐以加成舰种为单位：联动舰船提供重巡加成时计入重巡行。
            options = {item["ship_type"]: item for item in available_next_ship_types()}
            self.assertEqual(options["重巡"]["idle_count"], 2)
            rows = next_ships_by_type("both", ["重巡"])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["ship_type"], "重巡")

    def test_collab_tag_requires_category_members(self):
        """联动标签必须由名称集合驱动：未提供集合时按不命中处理。"""
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "环",
                    "2020-11-26",
                    {
                        "ship_type": "重巡",
                        "rarity": "超稀有",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
                refresh_ship_tags(
                    connection, category_tags={COLLAB_SHIP_TAG: {"环"}}
                )
            self.assertEqual(
                list_ships({"ship_types": ["联动"]})[0]["tags"], ["联动"]
            )
            # 未传名称集合时不命中该类标签。
            with get_connection() as connection:
                refresh_ship_tags(connection)
            self.assertEqual(list_ships({"ship_types": ["联动"]}), [])

    def test_full_sync_writes_collab_tag(self):
        """全量同步会抓取联动分类并把成员写入 tags。"""
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "环",
                    "2020-11-26",
                    {
                        "ship_type": "重巡",
                        "rarity": "超稀有",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 机动", "points": 0}
                        ],
                    },
                )
                connection.execute("UPDATE ships SET tags = ''")
            with patch(
                "blhx.datasource.wiki.fetch_collab_ship_names", return_value={"环"}
            ), patch("blhx.datasource.wiki.fetch_release_entries", return_value=[]), patch(
                "blhx.datasource.wiki.fetch_armament_entries", return_value=[]
            ):
                _reset_sync_state()
                _sync_wiki()
            with get_connection() as connection:
                tags = connection.execute(
                    "SELECT tags FROM ships WHERE name = '环'"
                ).fetchone()["tags"]
            self.assertEqual(tags, "联动")
            self.assertEqual(SYNC_STATE["errors"], [])
            _reset_sync_state()

    def test_collab_category_failure_does_not_break_sync(self):
        """联动分类抓取失败只记录错误，不影响舰船与其它标签。"""
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "小企业",
                    "2024-01-01",
                    {"ship_type": "航母", "rarity": "精锐", "tech_points": []},
                )
                connection.execute("UPDATE ships SET tags = ''")
            with patch(
                "blhx.datasource.wiki.fetch_collab_ship_names", side_effect=ValueError("接口异常")
            ), patch("blhx.datasource.wiki.fetch_release_entries", return_value=[]), patch(
                "blhx.datasource.wiki.fetch_armament_entries", return_value=[]
            ):
                _reset_sync_state()
                _sync_wiki()
            self.assertEqual(SYNC_STATE["status"], "完成")
            self.assertTrue(
                any("联动分类" in item for item in SYNC_STATE["errors"])
            )
            # 其它标签照常写入。
            self.assertEqual(
                [item["name"] for item in list_ships({"ship_types": ["小船"]})],
                ["小企业"],
            )
            _reset_sync_state()

    def test_init_db_does_not_migrate_legacy_schema(self):
        """旧表结构不再自动迁移：缺列时 init_db 直接报错，由用户自行重建数据库。"""
        import sqlite3

        with isolated_app_data():
            with get_connection() as connection:
                connection.execute("DROP TABLE tech_points")
                connection.execute(
                    """
                    CREATE TABLE tech_points (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ship_id INTEGER NOT NULL,
                        stage TEXT NOT NULL,
                        attribute TEXT NOT NULL DEFAULT '',
                        points INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
            with self.assertRaises(sqlite3.OperationalError):
                init_db()

    def test_clear_ships_clears_config_logs_and_sequences(self):
        with isolated_app_data():
            with get_connection() as connection:
                ship = insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {
                        "ship_type": "战列",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "战列 耐久", "points": 0}
                        ],
                    },
                )
            update_ship_status(ship["id"], "is_idling", True)
            write_config({"next_ship_types": [tech_point("战列", "耐久")]})
            self.assertTrue(config_module.CONFIG_PATH.exists())

            self.assertEqual(clear_ships(), 1)
            self.assertFalse(config_module.CONFIG_PATH.exists())
            self.assertEqual(
                read_config(),
                {
                    "next_ship_types": [],
                    "target_limits": {},
                    "manual_targets": {},
                    "armament_target_limits": _default_armament_limits(),
                    "armament_manual_targets": {},
                },
            )
            with get_connection() as connection:
                for table in ("ships", "tech_points", "operation_logs"):
                    self.assertEqual(
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        0,
                    )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sqlite_sequence").fetchone()[0],
                    0,
                )
            self.assertEqual(SYNC_STATE["status"], "未开始")

# 兵装页面原型 HTML：与真实页面同构，含常规日期行、假名、联动行与结尾说明。
ARMAMENT_PAGE_HTML = """
<div class="panel-body"><span class="panel-title pull-left">\
<a id="已实装角色一览"></a>已实装角色一览</span>
<ul>
<li><a href="/blhx/2022%E5%B9%B46%E6%9C%889%E6%97%A59:00%E6%B8%AF%E5%8C%BA%E6%94%B9%E5%BB%BA" title="2022年6月9日9:00港区改建">2022年6月9日</a>：\
<span class="xtb-image"><a href="/blhx/%E6%AC%A7%E6%A0%B9%E4%BA%B2%E7%8E%8B" title="欧根亲王">\
<img alt="欧根亲王头像.jpg" src="https://example.test/a.jpg" /></a>\
<a href="/blhx/%E6%AC%A7%E6%A0%B9%E4%BA%B2%E7%8E%8B" title="欧根亲王">\
<span class="nowrap" style="color:#c90" title="欧根亲王"><span class="AF">萨沃伊亲王</span></span></a></span>、\
<span class="xtb-image"><a href="/blhx/%E6%8B%89%E8%8F%B2" title="拉菲">\
<img alt="拉菲头像.jpg" src="https://example.test/b.jpg" /></a>\
<a href="/blhx/%E6%8B%89%E8%8F%B2" title="拉菲">\
<span class="nowrap" style="color:#8000ff" title="拉菲"><span class="AF">拉菲</span></span></a></span></li>
<li><a href="/blhx/2022%E5%B9%B47%E6%9C%8814%E6%97%A59:00%E6%B8%AF%E5%8C%BA%E6%94%B9%E5%BB%BA" title="2022年7月14日9:00港区改建">2022年7月14日</a>：\
<span class="xtb-image"><a href="/blhx/%E8%AE%A9%C2%B7%E5%B7%B4%E5%B0%94" title="让·巴尔">\
<img alt="让·巴尔头像.jpg" src="https://example.test/c.jpg" /></a>\
<a href="/blhx/%E8%AE%A9%C2%B7%E5%B7%B4%E5%B0%94" title="让·巴尔">\
<span class="nowrap" style="color:#c90" title="让·巴尔"><span class="AF">简·布罗伊</span></span></a></span></li>
<li><a href="/blhx/2023%E5%B9%B44%E6%9C%881%E6%97%A513:00%E6%B8%AF%E5%8C%BA%E6%94%B9%E5%BB%BA" title="2023年3月31日13:00港区改建">2023年4月1日</a>：\
<span class="xtb-image"><a href="/blhx/%E7%89%B9%E8%A3%85%E5%9E%8B%E5%B8%83%E9%87%8CMKIII" title="特装型布里MKIII">\
<img alt="布里头像.jpg" src="https://example.test/d.jpg" /></a>\
<a href="/blhx/%E7%89%B9%E8%A3%85%E5%9E%8B%E5%B8%83%E9%87%8CMKIII" title="特装型布里MKIII">\
<span class="nowrap" style="color:#c90" title="特装型布里MKIII"><span class="AF">特装型布里MKIII</span></span></a></span>\
（特别活动赠送，无需强化与属性改装）</li>
<li><a href="/blhx/2023%E5%B9%B44%E6%9C%8827%E6%97%A510:00%E6%B8%AF%E5%8C%BA%E6%94%B9%E5%BB%BA" title="2023年4月27日10:00港区改建">DOA联动复刻</a>：\
<span class="xtb-image"><a href="/blhx/%E7%8E%AF" title="环">\
<img alt="环头像.jpg" src="https://example.test/e.jpg" /></a>\
<a href="/blhx/%E7%8E%AF" title="环">\
<span class="nowrap" style="color:#c90" title="环"><span class="AF">环</span></span></a></span>、\
<span class="xtb-image"><a href="/blhx/BLACK%E2%98%85ROCK_SHOOTER" title="BLACK★ROCK SHOOTER">\
<img alt="BRS头像.jpg" src="https://example.test/f.jpg" /></a>\
<a href="/blhx/BLACK%E2%98%85ROCK_SHOOTER" title="BLACK★ROCK SHOOTER">\
<span class="nowrap" style="color:#c90" title="BLACK★ROCK SHOOTER"><span class="AF">BLACK★ROCK SHOOTER</span></span></a></span></li>
</ul>
<dl><dd>（联动兵装为活动赠送，无需属性改装）</dd></dl>
</div>
"""


def _reset_sync_state() -> None:
    """_sync_wiki 直接调用时不会重置计数，测试前后需要自己清理，避免污染其他用例。"""
    SYNC_STATE.update(
        {
            "running": False,
            "status": "未开始",
            "message": "",
            "total": 0,
            "processed": 0,
            "skipped": 0,
            "armaments": 0,
            "errors": [],
        }
    )


def _insert_ship_with_armament(connection, name, ship_type, rarity, entry):
    ship = insert_ship(
        connection,
        name,
        "2023-08-17",
        {"ship_type": ship_type, "rarity": rarity, "tech_points": []},
    )
    upsert_armament(connection, ship["id"], entry)
    armament = connection.execute(
        "SELECT id FROM armaments WHERE ship_id = ?", (ship["id"],)
    ).fetchone()
    return ship, armament["id"]


class ArmamentTests(unittest.TestCase):
    def test_parse_armament_entries(self):
        entries = parse_armament_entries(ARMAMENT_PAGE_HTML)
        self.assertEqual(
            [entry["name"] for entry in entries],
            ["欧根亲王", "拉菲", "让·巴尔", "特装型布里MKIII", "环", "BLACK★ROCK SHOOTER"],
        )
        # 常规兵装带日期；假名用原始名称，不采用 span.AF 的显示名。
        self.assertEqual(entries[0]["release_date"], "2022-06-09")
        self.assertFalse(entries[0]["is_collab"])
        self.assertEqual(entries[1]["name"], "拉菲")
        self.assertEqual(entries[2]["name"], "让·巴尔")
        # 有日期的活动赠送兵装按常规处理，日期取显示日期而不是链接页的日期。
        self.assertEqual(entries[3]["release_date"], "2023-04-01")
        self.assertFalse(entries[3]["is_collab"])
        # 联动没有日期，按列表先后编号。
        self.assertTrue(entries[4]["is_collab"])
        self.assertEqual(entries[4]["collab_group"], "DOA联动复刻")
        self.assertEqual(entries[4]["collab_order"], 1)
        self.assertEqual(entries[5]["collab_order"], 2)
        self.assertEqual(entries[5]["release_date"], "")

    def test_parse_armament_entries_requires_section(self):
        with self.assertRaises(ValueError):
            parse_armament_entries("<p>没有兵装列表</p>")

    def test_armament_status_enhance_implies_built(self):
        with isolated_app_data():
            with get_connection() as connection:
                _, armament_id = _insert_ship_with_armament(
                    connection,
                    "拉菲",
                    "驱逐",
                    "精锐",
                    {"release_date": "2022-06-09", "is_collab": False},
                )

            result = update_armament_status(armament_id, "is_enhanced", True)
            self.assertTrue(result["is_built"])
            self.assertTrue(result["is_enhanced"])
            # 取消强化不回退打造状态。
            result = update_armament_status(armament_id, "is_enhanced", False)
            self.assertTrue(result["is_built"])
            self.assertFalse(result["is_enhanced"])

            operations = list_operations(10)
            self.assertTrue(
                any("同时标记为打造完成" in item["message"] for item in operations)
            )

    def test_armament_status_validation_and_missing(self):
        with isolated_app_data():
            with get_connection() as connection:
                _, armament_id = _insert_ship_with_armament(
                    connection,
                    "拉菲",
                    "驱逐",
                    "精锐",
                    {"release_date": "2022-06-09", "is_collab": False},
                )
            with self.assertRaises(ValueError):
                update_armament_status(armament_id, "is_idling", True)
            with self.assertRaises(ValueError):
                update_armament_status(armament_id, "is_built", "是")
            self.assertIsNone(update_armament_status(999999, "is_built", True))
            self.assertIsNone(clear_armament_statuses(999999))
            self.assertIsNone(delete_armament(999999))

    def test_armament_filters_and_status_filters(self):
        with isolated_app_data():
            with get_connection() as connection:
                _, normal_id = _insert_ship_with_armament(
                    connection,
                    "拉菲",
                    "驱逐",
                    "精锐",
                    {"release_date": "2022-06-09", "is_collab": False},
                )
                _, collab_id = _insert_ship_with_armament(
                    connection,
                    "环",
                    "重巡",
                    "超稀有",
                    {
                        "release_date": "",
                        "is_collab": True,
                        "collab_group": "DOA联动复刻",
                        "collab_order": 1,
                    },
                )
            update_armament_status(normal_id, "is_enhanced", True)

            self.assertEqual(len(list_armaments({"group": "collab"})), 1)
            self.assertEqual(len(list_armaments({"group": "normal"})), 1)
            # 兵装只有 group 一个类型入口；is_collab 不再是筛选参数，传入被忽略。
            self.assertEqual(
                len(list_armaments({"group": "collab", "is_collab": "否"})), 1
            )
            self.assertEqual(len(list_armaments({"is_built": "是"})), 1)
            self.assertEqual(len(list_armaments({"is_built": "否"})), 1)
            self.assertEqual(len(list_armaments({"is_enhanced": "是"})), 1)
            self.assertEqual(len(list_armaments({"ship_type": "驱逐"})), 1)
            self.assertEqual(
                [item["name"] for item in list_armaments({"name": "环"})], ["环"]
            )
            counts = count_pending_armament_statuses({})
            self.assertEqual(counts["is_built"], 1)
            self.assertEqual(counts["is_enhanced"], 1)
            self.assertEqual(counts["pending"], 1)
            # 常规兵装在前、联动按 Wiki 顺序在后。
            self.assertEqual(
                [item["name"] for item in list_armaments({})], ["拉菲", "环"]
            )
            self.assertEqual(armament_group_counts()["collab"]["built_count"], 1)

    def test_armament_pending_filter_matches_unbuilt_or_unenhanced(self):
        with isolated_app_data():
            with get_connection() as connection:
                armament_ids = {}
                for name in ("拉菲", "标枪", "绫波"):
                    armament_ids[name] = _insert_ship_with_armament(
                        connection,
                        name,
                        "驱逐",
                        "精锐",
                        {"release_date": "2022-06-09", "is_collab": False},
                    )[1]
            # 拉菲两项都未记录，标枪只打造未强化，绫波打造与强化都完成。
            update_armament_status(armament_ids["标枪"], "is_built", True)
            update_armament_status(armament_ids["绫波"], "is_enhanced", True)

            pending_names = sorted(
                item["name"] for item in list_armaments({"pending": "是"})
            )
            self.assertEqual(pending_names, sorted(["拉菲", "标枪"]))
            # 未记录（NULL）与未完成（0）同样命中“未完成”，全部完成的绫波被排除。
            self.assertEqual(
                [
                    item["name"]
                    for item in list_armaments(
                        {"pending": "是", "is_built": "是"}
                    )
                ],
                ["标枪"],
            )
            # 只支持“是”；其他取值视为不筛选。
            self.assertEqual(len(list_armaments({"pending": "否"})), 3)
            self.assertEqual(len(list_armaments({"pending": ""})), 3)

            counts = count_pending_armament_statuses({"is_built": "是"})
            # 未打造计数忽略自身筛选，因此与“已打造 = 否”下结果一致。
            self.assertEqual(
                counts["is_built"],
                count_pending_armament_statuses({"is_built": "否"})["is_built"],
            )
            # 未强化与未完成则叠加“已打造 = 是”，只剩只打造未强化的标枪。
            self.assertEqual(counts["is_enhanced"], 1)
            self.assertEqual(counts["pending"], 1)

    def test_armament_clear_and_delete_and_ship_cascade(self):
        with isolated_app_data():
            with get_connection() as connection:
                ship, armament_id = _insert_ship_with_armament(
                    connection,
                    "拉菲",
                    "驱逐",
                    "精锐",
                    {"release_date": "2022-06-09", "is_collab": False},
                )
            update_armament_status(armament_id, "is_enhanced", True)
            cleared = clear_armament_statuses(armament_id)
            self.assertIsNone(cleared["is_built"])
            self.assertIsNone(cleared["is_enhanced"])

            update_armament_status(armament_id, "is_built", True)
            deleted = delete_armament(armament_id)
            self.assertEqual(deleted["name"], "拉菲")
            self.assertEqual(list_armaments({}), [])
            # 陆装删除不影响舰船本身。
            self.assertEqual(len(list_ships({})), 1)

            with get_connection() as connection:
                upsert_armament(
                    connection,
                    ship["id"],
                    {"release_date": "2022-06-09", "is_collab": False},
                )
            self.assertEqual(len(list_armaments({})), 1)
            self.assertTrue(delete_ship(ship["id"]))
            self.assertEqual(list_armaments({}), [])

    def test_armament_sync_and_partial_sync(self):
        with isolated_app_data():
            with get_connection() as connection:
                for name in ("欧根亲王", "拉菲", "让·巴尔", "特装型布里MKIII", "环",
                             "BLACK★ROCK SHOOTER"):
                    insert_ship(
                        connection,
                        name,
                        "2023-08-17",
                        {"ship_type": "驱逐", "rarity": "精锐", "tech_points": []},
                    )

            with patch(
                "blhx.datasource.wiki.fetch_armament_entries",
                return_value=parse_armament_entries(ARMAMENT_PAGE_HTML),
            ), patch("blhx.datasource.wiki.fetch_release_entries", return_value=[]):
                _sync_wiki()

            self.assertEqual(SYNC_STATE["armaments"], 6)
            self.assertEqual(SYNC_STATE["errors"], [])
            armaments = list_armaments({})
            self.assertEqual(len(armaments), 6)
            self.assertEqual(
                next(item for item in armaments if item["name"] == "让·巴尔")[
                    "release_date"
                ],
                "2022-07-14",
            )
            # 已有状态不被重复同步覆盖。
            collab = next(item for item in armaments if item["name"] == "环")
            update_armament_status(collab["id"], "is_enhanced", True)
            with patch(
                "blhx.datasource.wiki.fetch_armament_entries",
                return_value=parse_armament_entries(ARMAMENT_PAGE_HTML),
            ), patch("blhx.datasource.wiki.fetch_release_entries", return_value=[]):
                _sync_wiki()
            after = find_armament_brief(collab["id"])
            self.assertTrue(after["is_built"])
            self.assertTrue(after["is_enhanced"])

            # 少量同步只更新指定舰娘的兵装。
            with patch(
                "blhx.datasource.wiki.fetch_armament_entries",
                return_value=parse_armament_entries(ARMAMENT_PAGE_HTML),
            ), patch(
                "blhx.datasource.wiki.fetch_release_entries",
                return_value=[
                    {"name": "拉菲", "release_date": "2017-05-25"},
                ],
            ), patch("blhx.datasource.wiki.fetch_ship_payload") as payload:
                payload.return_value = {
                    "name": "拉菲",
                    "release_date": "2017-05-25",
                    "ship_type": "驱逐",
                    "rarity": "精锐",
                    "tech_points": [],
                }
                _sync_wiki(["拉菲"])
            self.assertEqual(SYNC_STATE["armaments"], 1)

    def test_armament_sync_reports_wiki_failure_without_breaking_ship_sync(self):
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "拉菲",
                    "2023-08-17",
                    {"ship_type": "驱逐", "rarity": "精锐", "tech_points": []},
                )
            with patch(
                "blhx.datasource.wiki.fetch_armament_entries", side_effect=ValueError("页面异常")
            ), patch("blhx.datasource.wiki.fetch_release_entries", return_value=[]):
                _sync_wiki()
            self.assertEqual(SYNC_STATE["status"], "完成")
            self.assertTrue(
                any("兵装页面" in error for error in SYNC_STATE["errors"])
            )


    def test_armament_recommend_sort_priority_rarity_then_date(self):
        """海上传奇 > 决战方案 > 最高方案，其余稀有度一律按实装日期。"""
        with isolated_app_data():
            samples = [
                # 名称, 稀有度, 实装日期：精心安排让“日期顺序”与“稀有度顺序”互相冲突
                ("普通兵装早", "普通", "2022-01-01"),
                ("超稀有兵装中", "超稀有", "2023-01-01"),
                ("精锐兵装晚", "精锐", "2024-01-01"),
                ("最高方案兵装晚", "最高方案", "2024-06-01"),
                ("最高方案兵装早", "最高方案", "2023-06-01"),
                ("决战方案兵装", "决战方案", "2024-09-01"),
                ("海上传奇兵装", "海上传奇", "2024-12-01"),
            ]
            with get_connection() as connection:
                ids = {}
                for name, rarity, date in samples:
                    _, armament_id = _insert_ship_with_armament(
                        connection,
                        name,
                        "驱逐",
                        rarity,
                        {"release_date": date, "is_collab": False},
                    )
                    ids[name] = armament_id

            config = read_config()
            row = next(
                item
                for item in next_armaments_by_group(
                    "both",
                    config["armament_manual_targets"],
                    {"normal": {"is_built": 10, "is_enhanced": 1}},
                )
                if item["group"] == "normal"
            )
            order = [item["armament"]["name"] for item in row["built_items"]]
            self.assertEqual(
                order,
                [
                    # 三个特殊稀有度优先，彼此按优先级
                    "海上传奇兵装",
                    "决战方案兵装",
                    "最高方案兵装早",
                    "最高方案兵装晚",
                    # 其余稀有度不区分高低，只按实装日期升序
                    "普通兵装早",
                    "超稀有兵装中",
                    "精锐兵装晚",
                ],
            )
            # 普通/精锐/超稀有 之间不再有稀有度高低之分。
            self.assertLess(order.index("普通兵装早"), order.index("精锐兵装晚"))
            self.assertLess(order.index("超稀有兵装中"), order.index("精锐兵装晚"))

    def test_armament_sort_key_falls_back_to_wiki_order_for_collab(self):
        """联动兵装没有实装日期，按 Wiki 列表顺序。"""
        collab = [
            {"name": "二", "rarity": "超稀有", "release_date": "", "collab_order": 2},
            {"name": "一", "rarity": "普通", "release_date": "", "collab_order": 1},
        ]
        from blhx.domain.armaments import _armament_sort_key

        self.assertEqual(
            [item["name"] for item in sorted(collab, key=_armament_sort_key)],
            ["一", "二"],
        )
        # 常规兵装即使日期更晚，没有特殊稀有度时也排在特殊稀有度之后。
        mixed = [
            {"name": "常规早", "rarity": "普通", "release_date": "2022-01-01", "collab_order": 0},
            {"name": "最高方案", "rarity": "最高方案", "release_date": "2026-01-01", "collab_order": 0},
        ]
        self.assertEqual(
            [item["name"] for item in sorted(mixed, key=_armament_sort_key)],
            ["最高方案", "常规早"],
        )

    def test_armament_target_limits_and_manual_targets(self):
        with isolated_app_data():
            with get_connection() as connection:
                _, first_id = _insert_ship_with_armament(
                    connection,
                    "拉菲",
                    "驱逐",
                    "精锐",
                    {"release_date": "2022-06-09", "is_collab": False},
                )
                _, second_id = _insert_ship_with_armament(
                    connection,
                    "亚利桑那",
                    "战列",
                    "精锐",
                    {"release_date": "2022-06-09", "is_collab": False},
                )
                _insert_ship_with_armament(
                    connection,
                    "环",
                    "重巡",
                    "超稀有",
                    {
                        "release_date": "",
                        "is_collab": True,
                        "collab_group": "DOA联动复刻",
                        "collab_order": 1,
                    },
                )

            config = toggle_armament_manual_target("normal", "is_built", first_id)
            self.assertEqual(
                config["armament_manual_targets"]["normal"]["is_built"], [first_id]
            )
            # 上限为 1 时不能再录入第二个。
            with self.assertRaises(ValueError):
                toggle_armament_manual_target("normal", "is_built", second_id)

            config = save_armament_target_limit("normal", "is_built", 2)
            self.assertEqual(config["armament_target_limits"]["normal"]["is_built"], 2)
            toggle_armament_manual_target("normal", "is_built", second_id)

            # 标记为打造完成后，该兵装从录入里被移除，名额释放。
            update_armament_status(first_id, "is_built", True)
            config = read_config()
            self.assertEqual(
                config["armament_manual_targets"].get("normal", {}).get("is_built"),
                [second_id],
            )

            rows = next_armaments_by_group(
                "both",
                config["armament_manual_targets"],
                config["armament_target_limits"],
            )
            # 推荐里常规兵装优先展示，联动排在后面。
            self.assertEqual([row["group"] for row in rows], ["normal", "collab"])
            self.assertEqual(
                [row["group_label"] for row in rows], ["常规兵装", "联动兵装"]
            )
            normal_row = next(row for row in rows if row["group"] == "normal")
            self.assertEqual(
                [item["armament"]["name"] for item in normal_row["built_items"]],
                ["亚利桑那"],
            )

            with self.assertRaises(ValueError):
                save_armament_target_limit("normal", "is_built", 0)
            with self.assertRaises(ValueError):
                save_armament_target_limit("unknown", "is_built", 1)
            with self.assertRaises(ValueError):
                save_armament_target_limit("normal", "is_idling", 1)
            with self.assertRaises(ValueError):
                toggle_armament_manual_target("normal", "is_built", 999999)

    def test_armament_rows_expose_pending_counts(self):
        with isolated_app_data():
            with get_connection() as connection:
                normal_done, _ = _insert_ship_with_armament(
                    connection,
                    "拉菲",
                    "驱逐",
                    "精锐",
                    {"release_date": "2022-06-09", "is_collab": False},
                )
                _, normal_pending = _insert_ship_with_armament(
                    connection,
                    "标枪",
                    "驱逐",
                    "精锐",
                    {"release_date": "2022-06-10", "is_collab": False},
                )
                _, collab_pending = _insert_ship_with_armament(
                    connection,
                    "环",
                    "重巡",
                    "超稀有",
                    {
                        "release_date": "",
                        "is_collab": True,
                        "collab_group": "DOA联动复刻",
                        "collab_order": 1,
                    },
                )
            # 拉菲两项都完成；标枪只打造（待强化）；环两项都未记录（待打造且待强化）。
            update_armament_status(normal_pending, "is_built", True)
            built = next(
                item["id"]
                for item in list_armaments({"name": "拉菲"})
            )
            update_armament_status(built, "is_enhanced", True)

            rows = {row["group"]: row for row in next_armaments_by_group("both")}
            self.assertEqual(rows["normal"]["built_count"], 0)
            self.assertEqual(rows["normal"]["enhanced_count"], 1)
            self.assertEqual(rows["collab"]["built_count"], 1)
            self.assertEqual(rows["collab"]["enhanced_count"], 1)

            # 与推荐页分组徽标同源。
            self.assertEqual(
                rows["collab"]["built_count"],
                armament_group_counts()["collab"]["built_count"],
            )
            self.assertEqual(
                rows["collab"]["enhanced_count"],
                armament_group_counts()["collab"]["enhanced_count"],
            )

    def test_armament_config_survives_ship_config_writes(self):
        with isolated_app_data():
            with get_connection() as connection:
                insert_ship(
                    connection,
                    "拉菲",
                    "2023-08-17",
                    {
                        "ship_type": "驱逐",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "驱逐 机动", "points": 0},
                            {"stage": "解锁图鉴", "attribute": "驱逐 炮击", "points": 0},
                        ],
                    },
                )
            save_armament_target_limit("collab", "is_built", 3)
            # 保存推荐舰种、写入手工录入、清空舰船状态都不能丢掉兵装配置。
            save_next_ship_types(
                [tech_point("驱逐", "机动"), tech_point("驱逐", "炮击")]
            )
            save_target_limit(tech_point("驱逐", "炮击"), "is_idling", 2)
            config = read_config()
            self.assertEqual(config["armament_target_limits"]["collab"]["is_built"], 3)

    def test_clear_ships_clears_armaments(self):
        with isolated_app_data():
            with get_connection() as connection:
                _, armament_id = _insert_ship_with_armament(
                    connection,
                    "拉菲",
                    "驱逐",
                    "精锐",
                    {"release_date": "2022-06-09", "is_collab": False},
                )
            toggle_armament_manual_target("normal", "is_built", armament_id)
            self.assertEqual(clear_ships(), 1)
            self.assertEqual(list_armaments({}), [])
            config = read_config()
            self.assertEqual(config["armament_manual_targets"], {})
            self.assertEqual(
                config["armament_target_limits"], _default_armament_limits()
            )
            with get_connection() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM armaments").fetchone()[0],
                    0,
                )

    def test_armament_page_and_endpoints_exposed(self):
        html = PAGE_HTML
        self.assertIn("/api/armaments?", html)
        self.assertIn("/api/armament/status", html)
        self.assertIn("/api/armament/clear-status", html)
        self.assertIn("/api/armament/delete", html)
        self.assertIn("/api/armament-next-group?", html)
        self.assertIn("/api/armament-target-limit", html)
        self.assertIn("/api/armament-manual-target", html)
        # 强化完成按钮排在打造完成之后；清除数据在删除之前。
        built = html.index(">打造完成</button>")
        enhanced = html.index(">强化完成</button>")
        clear = html.index(">清除数据</button>", enhanced)
        delete = html.index(">删除</button>", clear)
        self.assertLess(built, enhanced)
        self.assertLess(enhanced, clear)
        self.assertLess(clear, delete)
        for label in ("是否联动", "是否已打造", "是否已强化", "打造目标", "强化目标"):
            self.assertIn(f'data-label="{label}"', html)
        # 推荐表格的类型列展示当前待办数量。
        self.assertIn("待挂机${idle} / 待练级${train}", html)
        self.assertIn("待打造${built} / 待强化${enhanced}", html)        # 推荐顺序（常规在前）与筛选项顺序（是/否）互不影响。
        self.assertEqual(ARMAMENT_RECOMMEND_GROUP_ORDER, ["normal", "collab"])
        self.assertEqual(ARMAMENT_GROUP_ORDER, ["collab", "normal"])

    def test_armament_rows_and_recommendations_expose_wiki_url(self):
        with isolated_app_data():
            with get_connection() as connection:
                _, normal_id = _insert_ship_with_armament(
                    connection,
                    "拉菲",
                    "驱逐",
                    "精锐",
                    {"release_date": "2022-06-09", "is_collab": False},
                )
                _, collab_id = _insert_ship_with_armament(
                    connection,
                    "环",
                    "重巡",
                    "超稀有",
                    {
                        "release_date": "",
                        "is_collab": True,
                        "collab_group": "DOA联动复刻",
                        "collab_order": 1,
                    },
                )

            armaments = {row["id"]: row for row in list_armaments({})}
            # 兵装名称取自舰船，Wiki 地址同样复用舰船表记录。
            self.assertEqual(
                armaments[normal_id]["wiki_url"],
                f"{WIKI_BASE_URL}/blhx/{quote('拉菲', safe='')}",
            )
            self.assertEqual(
                armaments[collab_id]["wiki_url"],
                f"{WIKI_BASE_URL}/blhx/{quote('环', safe='')}",
            )
            # 单条查询与推荐结果使用同一份字段。
            self.assertEqual(
                find_armament_brief(normal_id)["wiki_url"],
                armaments[normal_id]["wiki_url"],
            )
            rows = {row["group"]: row for row in next_armaments_by_group("both")}
            for group, field in (("normal", "built_items"), ("collab", "built_items")):
                item = rows[group][field][0]["armament"]
                self.assertEqual(
                    item["wiki_url"], armaments[item["id"]]["wiki_url"]
                )

    def test_ship_and_armament_names_link_to_wiki(self):
        html = PAGE_HTML
        # 舰船列表、舰船推荐、兵装列表、兵装推荐四处名称都渲染成 Wiki 链接。
        self.assertIn("const wikiLink = (url, name) =>", html)
        self.assertIn(
            '<td data-label="舰船名称">${wikiLink(ship.wiki_url, ship.name)}</td>',
            html,
        )
        self.assertIn(
            '<div class="next-ship-name">${wikiLink(ship.wiki_url, ship.name)}',
            html,
        )
        self.assertIn(
            '<td data-label="舰船名称">${wikiLink(armament.wiki_url, armament.name)}</td>',
            html,
        )
        self.assertIn(
            '<div class="next-ship-name">${wikiLink(armament.wiki_url, armament.name)}',
            html,
        )
        self.assertIn('target="_blank" rel="noopener"', html)
        self.assertIn(".wiki-link", html)


if __name__ == "__main__":
    unittest.main()
