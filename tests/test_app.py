import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError

import app
from support import isolated_app_data


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
            app.wiki,
            "urlopen",
            side_effect=[
                HTTPError("https://example.test", 567, "Unknown Status", {}, None),
                FakeResponse(),
            ],
        ), patch.object(
            app.wiki, "acquire_wiki_request_slot", return_value=None
        ), patch.object(app.wiki.time, "sleep") as sleep:
            result = app.fetch_wiki_text("/test")

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
            app.wiki, "urlopen", side_effect=urlopen_side_effect
        ), patch.object(
            app, "acquire_wiki_request_slot", return_value=None
        ), patch.object(app.wiki.time, "sleep"):
            result = app.fetch_wiki_text("/test")

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
        entries = app.extract_ship_entries(html)
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

        with patch("app.wiki.fetch_wiki_api", side_effect=fetch_wiki_api_side_effect):
            entries = app.fetch_release_entries()

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

        with patch("app.wiki.fetch_wiki_api", return_value=payload) as fetch_wiki_api:
            raw_text = app.fetch_ship_raw("武藏")

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
        parsed = app.parse_ship_raw(raw)
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
        self.assertEqual(app.parse_ship_raw(raw)["ship_type"], "风帆")

    def test_parse_template_fields_removes_html_comments(self):
        raw = r"""
        {{舰娘图鉴
        |类型=潜艇
        |稀有度=超稀有\<!--选择【普通/稀有/精锐/超稀有/海上传奇/最高方案/决战方案】-->
        }}
        """
        fields = app.parse_template_fields(raw)
        self.assertEqual(fields["类型"], "潜艇")
        self.assertEqual(fields["稀有度"], "超稀有")

    def test_fetch_ship_payload_logs_raw_response_on_parse_failure(self):
        entry = {"name": "杓鹬", "release_date": "2024-01-01"}
        with patch(
            "app.wiki.fetch_ship_raw", return_value="{{非目标模板}}"
        ), patch("app.wiki.parse_ship_raw", return_value=None):
            with self.assertRaises(ValueError), self.assertLogs(
                "blhx_training_tracker", level="ERROR"
            ) as logs:
                app.fetch_ship_payload(entry)

        self.assertTrue(any("{{非目标模板}}" in message for message in logs.output))

    def test_init_db_cleans_existing_template_comments(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                ship = app.insert_ship(
                    connection,
                    "易北·META",
                    "2023-08-17",
                    {
                        "ship_type": "潜艇",
                        "rarity": "超稀有",
                        "tech_points": [],
                    },
                )
                connection.execute(
                    "UPDATE ships SET rarity = ? WHERE id = ?",
                    (
                        "超稀有<!--选择【普通/稀有/精锐/超稀有/海上传奇/最高方案/决战方案】-->",
                        ship["id"],
                    ),
                )

            app.init_db()
            ships = app.list_ships({"name": "易北·META"})
            self.assertEqual(ships[0]["rarity"], "超稀有")

    def test_csv_roundtrip(self):
        rows = [
            ["舰船名称", "是否已挂机", "是否已练级完成"],
            ["武藏", "是", "否"],
            ['带,逗号"与引号', "否", "是"],
            ["多行\n名称", "是", "是"],
        ]
        data = app.csv_write(rows)
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(app.csv_read(data), rows)

    def test_csv_read_accepts_bom_utf8_and_gb18030(self):
        rows = [
            ["舰船名称", "是否已挂机", "是否已练级完成"],
            ["武藏", "是", "否"],
        ]
        body = "舰船名称,是否已挂机,是否已练级完成\r\n武藏,是,否\r\n"
        self.assertEqual(app.csv_read(body.encode("utf-8")), rows)
        self.assertEqual(app.csv_read(body.encode("gb18030")), rows)

    def test_csv_read_rejects_invalid_encoding_and_syntax(self):
        with self.assertRaises(app.CsvFormatError):
            app.csv_read(b"\xff\xfe\x00\x01\xff\xff")
        with self.assertRaises(app.CsvFormatError):
            app.csv_read(
                '舰船名称,是否已挂机,是否已练级完成\r\n"未闭合,是,否\r\n'.encode(
                    "utf-8"
                )
            )

    def test_parse_workbook_rows_rejects_duplicate_and_unknown_layout(self):
        with self.assertRaises(app.CsvFormatError):
            app.parse_workbook_rows(
                [["舰船名称", "舰船名称", "是否已挂机", "是否已练级完成"], ["武藏", "错误", "是", "否"]]
            )
        with self.assertRaises(app.CsvFormatError):
            app.parse_workbook_rows([["舰船名称", "是否已挂机"]])
        with self.assertRaises(app.CsvFormatError):
            app.parse_workbook_rows([])
        self.assertEqual(app.parse_workbook_rows([app.IMPORT_HEADERS]), [])
        rows = app.parse_workbook_rows(
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
            with app.get_connection() as connection:
                app.insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {"ship_type": "战列", "rarity": "海上传奇", "tech_points": []},
                )
            payload = app.csv_write(
                [
                    ["舰船名称", "是否已挂机", "是否已练级完成"],
                    ["武藏", "是", "否"],
                    ["不存在", "是", "否"],
                ]
            )
            report_bytes, updated, skipped = app.import_csv(payload)
            self.assertEqual((updated, skipped), (1, 1))
            report = app.csv_read(report_bytes)
            self.assertEqual(report[0], app.REPORT_HEADERS)
            self.assertEqual(report[1][3], "处理完成")
            self.assertEqual(report[2][3], "未匹配到数据跳过")
            with app.get_connection() as connection:
                ship = connection.execute(
                    "SELECT is_idling, is_trained FROM ships WHERE name = '武藏'"
                ).fetchone()
                self.assertEqual((ship["is_idling"], ship["is_trained"]), (1, 0))

    def test_start_import_runs_in_background_and_exposes_result(self):
        import time

        with isolated_app_data():
            with app.get_connection() as connection:
                app.insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {"ship_type": "战列", "rarity": "海上传奇", "tech_points": []},
                )
            payload = app.csv_write(
                [
                    ["舰船名称", "是否已挂机", "是否已练级完成"],
                    ["武藏", "是", "否"],
                    ["不存在", "否", "是"],
                ]
            )
            app.reset_import_state()
            snapshot = app.start_import(payload, "test.csv")
            self.assertTrue(snapshot["running"])
            self.assertEqual(snapshot["total"], 2)
            for _ in range(200):
                status = app.import_status_snapshot()
                if not status["running"]:
                    break
                time.sleep(0.02)
            status = app.import_status_snapshot()
            self.assertFalse(status["running"])
            self.assertEqual(status["status"], "完成")
            self.assertEqual(status["processed"], 1)
            self.assertEqual(status["skipped"], 1)
            report = app.csv_read(app.IMPORT_STATE["result"])
            self.assertEqual(report[0], app.REPORT_HEADERS)
            self.assertEqual(report[1][3], "处理完成")
            self.assertEqual(report[2][3], "未匹配到数据跳过")

    def test_start_import_rejects_invalid_csv_and_concurrent_start(self):
        import threading
        import time

        with isolated_app_data():
            with self.assertRaises(app.CsvFormatError):
                app.start_import(b"\xff\xfe\x00\xff")
            self.assertFalse(app.import_status_snapshot()["running"])

            rows = [["舰船名称", "是否已挂机", "是否已练级完成"]]
            rows.extend([["不存在", "是", "否"]] * 400)
            payload = app.csv_write(rows)
            app.reset_import_state()
            app.start_import(payload, "slow.csv")
            try:
                if app.import_status_snapshot()["running"]:
                    with self.assertRaises(ValueError):
                        app.start_import(payload, "again.csv")
            finally:
                for _ in range(400):
                    if not app.import_status_snapshot()["running"]:
                        break
                    time.sleep(0.02)

    def test_import_template_is_csv(self):
        data = app.import_template_bytes()
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(app.csv_read(data), [app.IMPORT_HEADERS])

    def test_import_result_is_atomic_and_locked(self):
        import threading
        import time

        with isolated_app_data():
            with app.get_connection() as connection:
                app.insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {"ship_type": "战列", "rarity": "海上传奇", "tech_points": []},
                )
            payload = app.csv_write(
                [
                    ["舰船名称", "是否已挂机", "是否已练级完成"],
                    ["武藏", "是", "是"],
                ]
            )
            app.reset_import_state()
            with self.assertRaises(LookupError):
                app.import_result_bytes()
            app.start_import(payload, "atomic.csv")
            with self.assertRaises(ValueError):
                app.import_result_bytes()
            for _ in range(200):
                if not app.import_status_snapshot()["running"]:
                    break
                time.sleep(0.02)
            data, status = app.import_result_bytes()
            self.assertEqual(status, "完成")
            self.assertEqual(app.csv_read(data)[1][3], "处理完成")
            self.assertEqual(app.csv_read(data)[1][0], "武藏")

            errors = []

            def reader():
                try:
                    for _ in range(50):
                        app.import_status_snapshot()
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
            server = ThreadingHTTPServer(("127.0.0.1", 0), app.TrackerHandler)
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
                self.assertEqual(version_payload["version"], app.APP_VERSION)
            finally:
                server.shutdown()
                server.server_close()

    def test_import_process(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                battle_ship = app.insert_ship(
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
            report_rows, updated, skipped = app.process_import_rows(rows)
            self.assertEqual(updated, 1)
            self.assertEqual(skipped, 1)
            self.assertEqual(report_rows[0]["处理状态"], "处理完成")
            self.assertEqual(report_rows[1]["处理状态"], "未匹配到数据跳过")

    def test_ship_filter_sort_and_status_update(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                ship_1 = app.insert_ship(
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
                ship_2 = app.insert_ship(
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

            ships = app.list_ships({"is_trained": "否"})
            self.assertEqual([ship["name"] for ship in ships], ["古鹰", "武藏"])
            ships = app.list_ships({"bonus_attribute": "机动"})
            self.assertEqual([ship["name"] for ship in ships], ["古鹰"])

            updated = app.update_ship_status(ship_1["id"], "is_idling", True)
            self.assertTrue(updated["is_idling"])
            self.assertIsNone(updated["is_trained"])

    def test_pending_status_counts_ignore_own_filter(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                ship_1 = app.insert_ship(
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
                app.insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "机动", "points": 0}
                        ],
                    },
                )
                app.insert_ship(
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
            app.update_ship_status(ship_1["id"], "is_idling", True)

            counts = app.count_pending_ship_statuses({"needs_training": "是"})
            self.assertEqual(
                counts, {"is_idling": 2, "is_trained": 3, "pending": 3}
            )
            # 统计时忽略本字段自身的筛选，但仍受其他筛选影响。
            counts = app.count_pending_ship_statuses(
                {"needs_training": "是", "is_idling": "是"}
            )
            self.assertEqual(
                counts, {"is_idling": 2, "is_trained": 1, "pending": 1}
            )
            self.assertRegex(app.APP_VERSION, r"^\d+\.\d+\.\d+$")
            self.assertEqual(
                app.APP_VERSION,
                (app.BASE_DIR / "VERSION").read_text(encoding="utf-8").strip(),
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

    def test_tech_attribute_matches_each_ship_type(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                app.insert_ship(
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
                app.insert_ship(
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
                app.insert_ship(
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
                app.parse_tech_attribute("战巡/战列/航战 耐久"),
                (["战巡", "战列", "航战"], "耐久"),
            )
            ships = app.list_ships(
                {"bonus_ship_type": "战列", "bonus_attribute": "耐久"}
            )
            self.assertEqual([ship["name"] for ship in ships], ["武藏", "易北"])
            ships = app.list_ships(
                {"bonus_ship_type": "战列", "bonus_attribute": "炮击"}
            )
            self.assertEqual(ships, [])
            ships = app.list_ships(
                {"ship_type": "战列", "bonus_attribute": "耐久"}
            )
            self.assertEqual([ship["name"] for ship in ships], ["武藏", "重樱专属"])
            ships = app.list_ships({"bonus_ship_type": "重巡", "bonus_attribute": "耐久"})
            self.assertEqual([ship["name"] for ship in ships], ["重樱专属"])

    def test_next_ship_type_config_uses_data_file(self):
        with isolated_app_data() as temp_dir:
            with app.get_connection() as connection:
                app.insert_ship(
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

            saved = app.save_next_ship_types(["战列"])
            self.assertEqual(saved, ["战列"])
            self.assertEqual(
                app.read_config(),
                {
                    "next_ship_types": ["战列"],
                    "target_limits": {
                        "战列": {"is_idling": 1, "is_trained": 1}
                    },
                    "manual_targets": {},
                },
            )
            with app.config.CONFIG_PATH.open(encoding="utf-8") as file:
                import json

                self.assertEqual(
                    json.load(file),
                    {
                        "next_ship_types": ["战列"],
                        "target_limits": {
                            "战列": {"is_idling": 1, "is_trained": 1}
                        },
                        "manual_targets": {},
                    },
                )
            with self.assertRaises(ValueError):
                app.save_next_ship_types(["战列", "驱逐"])

    def test_next_ship_supports_available_ship_types(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                battle_ship = app.insert_ship(
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
                heavy_cruiser = app.insert_ship(
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
                destroyer = app.insert_ship(
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
            app.update_ship_status(destroyer["id"], "is_trained", True)
            self.assertTrue(
                app.list_ships({"name": destroyer["name"]})[0]["is_idling"]
            )
            self.assertEqual(
                app.list_operations()[0]["message"],
                "标记为已练级完成：夕立（同时标记为已挂机完成）",
            )

            options = app.available_next_ship_types()
            self.assertEqual(
                options,
                [
                    {"ship_type": "战列", "idle_count": 1, "train_count": 0},
                    {"ship_type": "重巡", "idle_count": 1, "train_count": 0},
                ],
            )
            self.assertIsNone(
                app.next_ship("train", ship_types=["战列", "重巡"])
            )
            self.assertEqual(
                app.next_ship("idle", ship_types=["重巡"])["name"], "古鹰"
            )

            app.update_ship_status(battle_ship["id"], "is_idling", True)
            app.update_ship_status(heavy_cruiser["id"], "is_idling", True)
            options = app.available_next_ship_types()
            self.assertEqual(
                options,
                [
                    {"ship_type": "战列", "idle_count": 0, "train_count": 1},
                    {"ship_type": "重巡", "idle_count": 0, "train_count": 1},
                ],
            )
            ships = app.list_ships({"ship_types": ["战列", "重巡"]})
            self.assertEqual(
                [ship["name"] for ship in ships], ["古鹰", "武藏"]
            )
            self.assertEqual(
                app.next_ship("train", ship_types=["战列", "重巡"])["name"],
                "古鹰",
            )
            self.assertIsNone(app.next_ship("idle", ship_types=["重巡"]))
            grouped = app.next_ships_by_type("both", ["战列", "重巡"])
            self.assertEqual(
                [row["ship_type"] for row in grouped], ["战列", "重巡"]
            )
            self.assertIsNone(grouped[0]["idle"])
            self.assertEqual(grouped[0]["train"]["name"], "武藏")
            self.assertIsNone(grouped[1]["idle"])
            self.assertEqual(grouped[1]["train"]["name"], "古鹰")
            with self.assertRaises(ValueError):
                app.next_ship("train", ship_types=["战列", "驱逐"])

            app.update_ship_status(heavy_cruiser["id"], "is_trained", True)
            with self.assertRaises(ValueError):
                app.next_ship("train", ship_types=["重巡"])

    def test_operation_history_records_status_and_delete(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                ship = app.insert_ship(
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
                app.update_ship_status(ship["id"], "is_idling", True)
            )
            self.assertTrue(app.delete_ship(ship["id"]))
            operations = app.list_operations()
            self.assertEqual(len(operations), 2)
            self.assertEqual(operations[0]["message"], "删除舰娘「武藏」")
            self.assertEqual(
                operations[1]["message"], "标记为已挂机完成：武藏"
            )
            self.assertTrue(operations[0]["created_at"])

    def test_partial_sync_matches_and_overwrites(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                app.insert_ship(
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
                "app.wiki.fetch_release_entries", return_value=[release_entry]
            ), patch("app.wiki.fetch_ship_payload", return_value=parsed_ship):
                app._sync_wiki(["武藏"])

            self.assertEqual(app.SYNC_STATE["processed"], 1)
            self.assertEqual(app.SYNC_STATE["errors"], [])
            ships = app.list_ships({"name": "武藏"})
            self.assertEqual(ships[0]["ship_type"], "战列")
            self.assertEqual(ships[0]["tech_points"][0]["attribute"], "战巡/战列/航战 耐久")

    def test_partial_sync_requires_wiki_match(self):
        app.SYNC_STATE.update({"running": False, "status": "", "message": ""})
        with patch(
            "app.wiki.fetch_release_entries",
            return_value=[{"name": "武藏", "release_date": "2023-08-17"}],
        ):
            app._sync_wiki(["古鹰"])
        self.assertEqual(app.SYNC_STATE["status"], "失败")
        self.assertIn("古鹰", app.SYNC_STATE["message"])

    def test_delete_ship_removes_related_data(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                ship = app.insert_ship(
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

            self.assertTrue(app.delete_ship(ship["id"]))
            self.assertEqual(app.list_ships({}), [])

    def test_clear_ships_removes_all_data(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                for name, date, stage in [
                    ("武藏", "2023-08-17", "解锁图鉴"),
                    ("古鹰", "2017-05-25", "达到120级"),
                ]:
                    app.insert_ship(
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

            self.assertEqual(app.clear_ships(), 2)
            self.assertEqual(app.list_ships({}), [])
            with app.get_connection() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM tech_points").fetchone()[0],
                    0,
                )


    def test_filter_option_orders(self):
        self.assertEqual(
            app.SHIP_TYPE_ORDER,
            [
                "驱逐", "轻巡", "重巡", "超巡", "战巡", "战列", "航母", "轻航",
                "重炮", "维修", "航战", "潜艇", "潜母", "运输", "风帆",
            ],
        )
        self.assertEqual(
            app.RARITY_FILTER_ORDER,
            ["普通", "稀有", "精锐", "超稀有", "海上传奇", "最高方案", "决战方案"],
        )
        self.assertEqual(
            app.BONUS_SHIP_TYPE_ORDER,
            [
                "驱逐", "轻巡", "重巡", "超巡", "战巡", "战列", "正航", "轻航",
                "重炮", "维修", "航战", "潜艇", "潜母", "运输", "风帆",
            ],
        )
        self.assertEqual(
            app.BONUS_ATTRIBUTE_ORDER,
            ["耐久", "炮击", "雷击", "防空", "航空", "装填", "机动", "命中", "反潜"],
        )

    def test_sail_ship_types_are_merged(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                for name, ship_type in [
                    ("皇家财富号", "风帆S"),
                    ("金鹿号", "风帆V"),
                    ("圣马丁号", "风帆M"),
                ]:
                    app.insert_ship(
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

            ships = app.list_ships({"ship_type": "风帆"})
            self.assertEqual(len(ships), 3)
            self.assertTrue(all(ship["ship_type"] == "风帆" for ship in ships))

    def test_bonus_filters_use_bonus_ship_type_column(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                app.insert_ship(
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
                app.insert_ship(
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

            ships = app.list_ships({"bonus_ship_type": "正航"})
            self.assertEqual([ship["name"] for ship in ships], ["企业"])
            ships = app.list_ships({"bonus_ship_type": "轻航"})
            self.assertEqual([ship["name"] for ship in ships], ["企业"])
            ships = app.list_ships({"bonus_attribute": "炮击"})
            self.assertEqual([ship["name"] for ship in ships], ["武藏"])
            ships = app.list_ships({"bonus_ship_type": "战列", "bonus_attribute": "耐久"})
            self.assertEqual([ship["name"] for ship in ships], ["武藏"])
            self.assertEqual(
                app.list_ships({"bonus_ship_type": "航母"}), []
            )

    def test_tech_priority_uses_bonus_ship_type(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                app.insert_ship(
                    connection,
                    "企业",
                    "2020-01-01",
                    {
                        "ship_type": "航母",
                        "rarity": "海上传奇",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "轻航/正航 装填", "points": 0}
                        ],
                    },
                )
                light_carrier = app.insert_ship(
                    connection,
                    "兰利",
                    "2021-01-01",
                    {
                        "ship_type": "轻航",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "轻航/正航 装填", "points": 0}
                        ],
                    },
                )
                other = app.insert_ship(
                    connection,
                    "重樱",
                    "2022-01-01",
                    {
                        "ship_type": "战列",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "战巡/战列/航战 耐久", "points": 0}
                        ],
                    },
                )
            ships = {ship["name"]: ship for ship in app.list_ships({})}
            self.assertEqual(app.tech_priority_rank(ships["企业"]), 0)
            self.assertEqual(app.tech_priority_rank(ships["兰利"]), 0)
            self.assertGreater(app.tech_priority_rank(ships["重樱"]), 0)
            self.assertEqual(light_carrier["ship_type"], "轻航")

    def test_clear_ships_on_empty_database(self):
        with isolated_app_data():
            self.assertEqual(app.clear_ships(), 0)
            self.assertEqual(app.list_ships({}), [])
            self.assertEqual(
                app.read_config(),
                {"next_ship_types": [], "target_limits": {}, "manual_targets": {}},
            )

    def test_clear_ships_rejects_when_sync_running(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                app.insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {"ship_type": "战列", "rarity": "海上传奇", "tech_points": []},
                )
            app.SYNC_STATE["running"] = True
            try:
                with self.assertRaises(ValueError):
                    app.clear_ships()
            finally:
                app.SYNC_STATE["running"] = False
            self.assertEqual(len(app.list_ships({})), 1)

    def test_clear_ships_reports_config_remove_failure(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                app.insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {"ship_type": "战列", "rarity": "海上传奇", "tech_points": []},
                )
            app.write_config({"next_ship_types": ["战列"]})
            with patch.object(
                app.config.CONFIG_PATH.__class__,
                "unlink",
                side_effect=PermissionError("denied"),
            ):
                with self.assertRaises(ValueError):
                    app.clear_ships()
            self.assertEqual(len(app.list_ships({})), 1)
            self.assertEqual(
                app.read_config(),
                {
                    "next_ship_types": ["战列"],
                    "target_limits": {
                        "战列": {"is_idling": 1, "is_trained": 1}
                    },
                    "manual_targets": {},
                },
            )

    def test_clear_ship_statuses_resets_both_fields(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                ship = app.insert_ship(
                    connection,
                    "武藏",
                    "2023-08-17",
                    {"ship_type": "战列", "rarity": "海上传奇", "tech_points": []},
                )
            app.update_ship_status(ship["id"], "is_trained", True)
            cleared = app.clear_ship_statuses(ship["id"])
            self.assertEqual(cleared, {"id": ship["id"], "is_idling": None, "is_trained": None})
            record = app.list_ships({"name": "武藏"})[0]
            self.assertIsNone(record["is_idling"])
            self.assertIsNone(record["is_trained"])
            self.assertEqual(app.list_operations()[0]["message"], "清除记录：武藏")
            self.assertIsNone(app.clear_ship_statuses(99999))

    def test_pending_filter_matches_idle_or_trained_unfinished(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                ships = {}
                for name, ship_type, attribute in (
                    ("武藏", "战列", "耐久"),
                    ("古鹰", "重巡", "机动"),
                    ("夕立", "驱逐", "炮击"),
                ):
                    ships[name] = app.insert_ship(
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
            app.update_ship_status(ships["武藏"]["id"], "is_idling", True)
            app.update_ship_status(ships["古鹰"]["id"], "is_idling", True)
            app.update_ship_status(ships["古鹰"]["id"], "is_trained", True)

            pending_names = sorted(
                ship["name"] for ship in app.list_ships({"pending": "是"})
            )
            self.assertEqual(pending_names, sorted(["武藏", "夕立"]))
            # 两项都未记录的舰船同样命中“未完成”。
            self.assertEqual(
                [
                    ship["name"]
                    for ship in app.list_ships({"pending": "是", "ship_type": "驱逐"})
                ],
                ["夕立"],
            )
            # 只支持“是”；其他取值视为不筛选。
            self.assertEqual(len(app.list_ships({"pending": "否"})), 3)
            # 关闭筛选时不受影响。            self.assertEqual(len(app.list_ships({"pending": ""})), 3)

            # 与已有状态筛选按 AND 组合：只挂机未练级的舰船。
            self.assertEqual(
                [
                    ship["name"]
                    for ship in app.list_ships({"pending": "是", "is_trained": "是"})
                ],
                [],
            )

    def test_manual_targets_persist_and_drive_recommendation(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                chosen = app.insert_ship(
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
                app.insert_ship(
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
            app.update_ship_status(chosen["id"], "is_idling", True)
            for ship in app.list_ships({"ship_type": "重巡"}):
                if ship["name"] == "青叶":
                    app.update_ship_status(ship["id"], "is_idling", True)
            app.save_next_ship_types(["重巡"])
            self.assertEqual(
                app.next_ships_by_type("both", ["重巡"])[0]["train"]["name"],
                "青叶",
            )

            config = app.toggle_manual_target("重巡", "is_trained", chosen["id"])
            self.assertEqual(
                config["manual_targets"], {"重巡": {"is_trained": [chosen["id"]]}}
            )
            config = app.read_config()
            row = app.next_ships_by_type(
                "both",
                ["重巡"],
                config["manual_targets"],
                config["target_limits"],
            )[0]
            self.assertEqual(row["train"]["name"], "古鹰")
            self.assertEqual(row["train_manual_count"], 1)
            self.assertTrue(row["train_items"][0]["manual"])
            self.assertEqual(row["idle_manual_count"], 0)

            config = app.toggle_manual_target("重巡", "is_trained", chosen["id"])
            self.assertEqual(config["manual_targets"], {})
            with self.assertRaises(ValueError):
                app.toggle_manual_target("驱逐", "is_trained", chosen["id"])
            with self.assertRaises(ValueError):
                app.toggle_manual_target("重巡", "is_trained", 99999)
            with self.assertRaises(ValueError):
                app.toggle_manual_target("重巡", "is_trained", True)

    def test_target_limit_controls_manual_capacity_and_auto_fill(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                ships = []
                for name, is_idling in [
                    ("古鹰", True),
                    ("青叶", True),
                    ("衣笠", True),
                    ("加古", True),
                ]:
                    ship = app.insert_ship(
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
                app.update_ship_status(ship["id"], "is_idling", True)
            app.save_next_ship_types(["重巡"])

            config = app.save_target_limit("重巡", "is_trained", 3)
            self.assertEqual(
                config["target_limits"]["重巡"],
                {"is_idling": 1, "is_trained": 3},
            )
            config = app.toggle_manual_target("重巡", "is_trained", ships[0]["id"])
            config = app.toggle_manual_target("重巡", "is_trained", ships[1]["id"])
            self.assertEqual(
                app.read_config()["manual_targets"]["重巡"]["is_trained"],
                [ships[0]["id"], ships[1]["id"]],
            )
            config = app.read_config()
            row = app.next_ships_by_type(
                "both",
                ["重巡"],
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

            config = app.toggle_manual_target("重巡", "is_trained", ships[2]["id"])
            self.assertEqual(
                app.read_config()["manual_targets"]["重巡"]["is_trained"],
                [ships[0]["id"], ships[1]["id"], ships[2]["id"]],
            )
            with self.assertRaises(ValueError):
                app.toggle_manual_target("重巡", "is_trained", ships[3]["id"])

            config = app.save_target_limit("重巡", "is_trained", 1)
            self.assertEqual(
                app.read_config()["manual_targets"]["重巡"]["is_trained"],
                [ships[0]["id"]],
            )
            self.assertEqual(
                config["target_limits"]["重巡"]["is_trained"], 1
            )

    def test_save_next_ship_types_keeps_target_config(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                ship = app.insert_ship(
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
            app.save_next_ship_types(["重巡"])
            app.save_target_limit("重巡", "is_idling", 2)
            app.toggle_manual_target("重巡", "is_idling", ship["id"])
            app.save_next_ship_types(["重巡"])
            config = app.read_config()
            self.assertEqual(
                config["manual_targets"], {"重巡": {"is_idling": [ship["id"]]}}
            )
            self.assertEqual(
                config["target_limits"]["重巡"]["is_idling"], 2
            )
            app.save_next_ship_types([])
            config = app.read_config()
            self.assertEqual(config["manual_targets"], {})
            self.assertEqual(config["target_limits"], {})

    def test_target_limit_validation_and_stale_manual_target(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                chosen = app.insert_ship(
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
                app.insert_ship(
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
            app.save_next_ship_types(["重巡"])
            for invalid_limit in (0, 11, -1, True, "3", 2.5):
                with self.assertRaises(ValueError):
                    app.save_target_limit("重巡", "is_trained", invalid_limit)
            with self.assertRaises(ValueError):
                app.save_target_limit("重巡", "bad_field", 2)
            with self.assertRaises(ValueError):
                app.save_target_limit("驱逐", "is_trained", 2)

            config = app.save_target_limit("重巡", "is_trained", 10)
            self.assertEqual(config["target_limits"]["重巡"]["is_trained"], 10)
            app.toggle_manual_target("重巡", "is_trained", chosen["id"])
            app.delete_ship(chosen["id"])

            # 删除舰船后应同步清理悬空的手动录入，且能立刻替换录入新舰娘。
            config = app.read_config()
            self.assertEqual(config["manual_targets"], {})
            replacement = app.list_ships({"ship_type": "重巡"})[0]
            config = app.toggle_manual_target(
                "重巡", "is_trained", replacement["id"]
            )
            self.assertEqual(
                config["manual_targets"]["重巡"]["is_trained"], [replacement["id"]]
            )
            row = app.next_ships_by_type(
                "both",
                ["重巡"],
                config["manual_targets"],
                config["target_limits"],
            )[0]
            self.assertFalse(row["train_manual_stale"])
            self.assertEqual(row["train_manual_count"], 1)

    def test_completing_manual_target_releases_slot(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                chosen = app.insert_ship(
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
                app.insert_ship(
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
            app.save_next_ship_types(["重巡"])
            app.toggle_manual_target("重巡", "is_idling", chosen["id"])
            self.assertEqual(
                app.read_config()["manual_targets"]["重巡"]["is_idling"],
                [chosen["id"]],
            )

            app.update_ship_status(chosen["id"], "is_idling", True)
            self.assertEqual(app.read_config()["manual_targets"], {})
            config = app.read_config()
            row = app.next_ships_by_type(
                "both",
                ["重巡"],
                config["manual_targets"],
                config["target_limits"],
            )[0]
            self.assertEqual(row["idle"]["name"], "青叶")
            self.assertTrue(all(not item["manual"] for item in row["idle_items"]))

    def test_concurrent_config_updates_do_not_conflict(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        with isolated_app_data():
            with app.get_connection() as connection:
                app.insert_ship(
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
            app.save_next_ship_types(["重巡"])
            real_write = app.config.write_config

            def slow_write(config):
                import time

                time.sleep(0.02)
                real_write(config)

            with patch("blhx.config.write_config", slow_write):
                with ThreadPoolExecutor(max_workers=8) as executor:
                    jobs = [
                        executor.submit(
                            app.save_target_limit, "重巡", field, 3
                        )
                        for field in ("is_idling", "is_trained")
                    ]
                    for job in jobs:
                        job.result()

            limits = app.read_config()["target_limits"]["重巡"]
            self.assertEqual(limits["is_idling"], 3)
            self.assertEqual(limits["is_trained"], 3)

    def test_query_filters_supports_plural_ship_types(self):
        from unittest.mock import MagicMock

        handler = app.TrackerHandler.__new__(app.TrackerHandler)
        handler.path = "/api/ships?ship_types=%E9%87%8D%E5%B7%A1&name=%E5%8F%A4"
        filters = handler.query_filters()
        self.assertEqual(filters["ship_types"], ["重巡"])
        self.assertEqual(filters["name"], "古")

        handler.path = "/api/ships?ship_type=%E6%88%98%E5%88%97"
        self.assertEqual(handler.query_filters()["ship_types"], ["战列"])

        handler.path = "/api/ships?pending=%E6%98%AF"
        self.assertEqual(handler.query_filters()["pending"], "是")

        handler.path = "/api/ships"
        self.assertNotIn("ship_types", handler.query_filters())
        self.assertEqual(handler.query_filters()["pending"], "")

    def test_training_completion_releases_idle_slot(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                chosen = app.insert_ship(
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
                app.insert_ship(
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
            app.save_next_ship_types(["重巡"])
            app.toggle_manual_target("重巡", "is_idling", chosen["id"])
            app.update_ship_status(chosen["id"], "is_trained", True)
            # 练级完成会同时把挂机置为完成，挂机名额必须一并释放。
            self.assertEqual(app.read_config()["manual_targets"], {})

            replacement = [
                ship for ship in app.list_ships({"ship_type": "重巡"})
                if ship["name"] == "青叶"
            ][0]
            config = app.toggle_manual_target(
                "重巡", "is_idling", replacement["id"]
            )
            self.assertEqual(
                config["manual_targets"]["重巡"]["is_idling"],
                [replacement["id"]],
            )

    def test_completed_ship_rejected_as_manual_target(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                done = app.insert_ship(
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
            app.save_next_ship_types(["重巡"])
            app.update_ship_status(done["id"], "is_trained", True)
            with self.assertRaises(ValueError):
                app.toggle_manual_target("重巡", "is_trained", done["id"])

    def test_release_manual_target_uses_config_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        with isolated_app_data():
            with app.get_connection() as connection:
                ship = app.insert_ship(
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
            app.save_next_ship_types(["重巡"])
            app.toggle_manual_target("重巡", "is_idling", ship["id"])
            real_write = app.config.write_config

            def slow_write(config):
                import time

                time.sleep(0.05)
                real_write(config)

            with patch("blhx.config.write_config", slow_write):
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [
                        executor.submit(app.release_manual_target, ship["id"]),
                        executor.submit(
                            app.save_target_limit, "重巡", "is_trained", 3
                        ),
                    ]
                    for future in futures:
                        future.result()

            config = app.read_config()
            self.assertEqual(config["target_limits"]["重巡"]["is_trained"], 3)
            self.assertEqual(config["manual_targets"], {})

    def test_clear_ships_serialized_with_config_writes(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        with isolated_app_data():
            with app.get_connection() as connection:
                app.insert_ship(
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
            app.save_next_ship_types(["重巡"])
            real_write = app.config.write_config

            def slow_write(config):
                import time

                time.sleep(0.05)
                real_write(config)

            with patch("blhx.config.write_config", slow_write):
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [
                        executor.submit(app.save_target_limit, "重巡", "is_trained", 3),
                        executor.submit(app.clear_ships),
                    ]
                    for future in futures:
                        future.result()

            # 无论谁先执行，清空后配置都必须回到空；不允许旧快照把配置写回来。
            config = app.read_config()
            if app.list_ships({}):
                self.assertEqual(config["target_limits"]["重巡"]["is_trained"], 3)
            else:
                self.assertEqual(config, {"next_ship_types": [], "target_limits": {}, "manual_targets": {}})

    def test_page_script_conflict_and_safety_guards(self):
        html = app.PAGE_HTML
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
        # 推荐舰种顺序与空行隐藏
        self.assertIn("function recommendSortKey(shipType) {", html)
        self.assertIn("recommend_ship_types", html)
        self.assertIn("function rowHasTargets(row) {", html)
        self.assertIn("所选舰种暂无可推荐舰娘", html)
        # 默认仅展示需要练级的舰船，已练级不预设筛选；重置筛选后保持一致。
        self.assertIn("needs_training: '是',", html)
        self.assertIn("filterState.needs_training = '是';", html)
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
        self.assertLess(
            html.index("{ key: 'is_trained', label: '已练级'"),
            html.index("{ key: 'pending', label: '完成状态'"),
        )
        # 未挂机/未练级数量排在筛选项按钮之后，与筛选按钮同一行展示。
        options_loop = html.index("${group.options.map(option => `")
        pending_count = html.index("${group.pendingLabel && statusCounts[group.key] !== null")
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
        html = app.PAGE_HTML
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
        for label in ("舰船名称", "舰种", "稀有度", "获得", "120级", "实装日期", "是否需要练级", "是否已挂机", "已练级", "操作", "挂机目标", "练级目标"):
            self.assertIn(f'data-label="{label}"', html)
        self.assertIn('env(safe-area-inset-bottom)', html)

    def test_page_tabs_split_recommend_training_and_equipment(self):
        html = app.PAGE_HTML
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
        # 兵装列表仅占位，不承载任何数据请求或表格。
        equipment_panel = html[html.index('id="panelEquipment"'):html.index('<script>')]
        self.assertIn('功能占位，暂未实现', equipment_panel)
        self.assertNotIn('id="shipTable"', equipment_panel)
        self.assertNotIn('id="nextTargets"', equipment_panel)
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
        html = app.PAGE_HTML
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
        with isolated_app_data():
            with app.get_connection() as connection:
                for name, ship_type in [
                    ("运输一", "运输"),
                    ("轻巡一", "轻巡"),
                    ("驱逐一", "驱逐"),
                    ("轻航一", "轻航"),
                    ("战列一", "战列"),
                    ("重炮一", "重炮"),
                ]:
                    app.insert_ship(
                        connection,
                        name,
                        "2024-01-01",
                        {
                            "ship_type": ship_type,
                            "rarity": "精锐",
                            "tech_points": [
                                {
                                    "stage": "解锁图鉴",
                                    "attribute": f"{ship_type} 耐久",
                                    "points": 0,
                                }
                            ],
                        },
                    )
            rows = app.next_ships_by_type(
                "both",
                ["运输", "轻巡", "驱逐", "轻航", "战列", "重炮"],
            )
            self.assertEqual(
                [row["ship_type"] for row in rows],
                ["战列", "轻航", "轻巡", "驱逐", "运输", "重炮"],
            )

    def test_recommend_normalizes_ship_type_aliases(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                app.insert_ship(
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
            rows = app.next_ships_by_type(
                "both", ["风帆M", "风帆S", "风帆"]
            )
            self.assertEqual([row["ship_type"] for row in rows], ["风帆"])

    def test_recommend_hides_ship_types_without_targets(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                only_idle = app.insert_ship(
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
                idle_and_train = app.insert_ship(
                    connection,
                    "重巡一",
                    "2024-01-01",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 耐久", "points": 0}
                        ],
                    },
                )
                idle_and_train_2 = app.insert_ship(
                    connection,
                    "重巡二",
                    "2024-01-02",
                    {
                        "ship_type": "重巡",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "重巡 耐久", "points": 0}
                        ],
                    },
                )
                app.insert_ship(
                    connection,
                    "驱逐一",
                    "2024-01-01",
                    {
                        "ship_type": "驱逐",
                        "rarity": "精锐",
                        "tech_points": [
                            {"stage": "解锁图鉴", "attribute": "驱逐 耐久", "points": 0}
                        ],
                    },
                )
            # 重巡全部已完成，两个模式都无候选，不应出现在结果中。
            app.update_ship_status(idle_and_train["id"], "is_trained", True)
            app.update_ship_status(idle_and_train_2["id"], "is_trained", True)
            app.update_ship_status(only_idle["id"], "is_idling", True)
            # both 模式下只要任一模式有目标就保留；重巡两个模式都为空必须被过滤。
            rows = app.next_ships_by_type("both", ["战列", "重巡", "驱逐"])
            self.assertEqual([row["ship_type"] for row in rows], ["战列", "驱逐"])
            self.assertTrue(rows[0]["train_items"])
            self.assertFalse(rows[0]["idle_items"])

            # 战列已挂机，idle 模式没有目标项，应被隐藏；重巡仍为空被过滤。
            rows = app.next_ships_by_type("idle", ["战列", "重巡", "驱逐"])
            self.assertEqual([row["ship_type"] for row in rows], ["驱逐"])
            self.assertTrue(rows[0]["idle_items"])

            # train 模式：驱逐仍在练级候选内，战列已完成练级? 用实际状态校验空行过滤。
            rows = app.next_ships_by_type("train", ["战列", "重巡", "驱逐"])
            self.assertNotIn("重巡", [row["ship_type"] for row in rows])
            for row in rows:
                self.assertTrue(row["train_items"])

    def test_init_db_migration_is_atomic(self):
        from unittest.mock import patch

        with isolated_app_data():
            with app.get_connection() as connection:
                connection.execute("DROP TABLE tech_points")
                connection.execute(
                    """
                    CREATE TABLE tech_points (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ship_id INTEGER NOT NULL REFERENCES ships(id) ON DELETE CASCADE,
                        stage TEXT NOT NULL,
                        attribute TEXT NOT NULL DEFAULT '',
                        points INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(ship_id, stage, attribute)
                    )
                    """
                )
                ship = app.insert_ship(
                    connection,
                    "古鹰",
                    "2017-05-25",
                    {"ship_type": "重巡", "rarity": "精锐", "tech_points": []},
                )
                connection.execute(
                    """
                    INSERT INTO tech_points (ship_id, stage, attribute, points)
                    VALUES (?, '解锁图鉴', '重巡 机动', 0)
                    """,
                    (ship["id"],),
                )

            with patch(
                "blhx.tech.parse_tech_attribute",
                side_effect=RuntimeError("boom"),
            ):
                with self.assertRaises(RuntimeError):
                    app.init_db()

            with app.get_connection() as connection:
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(tech_points)"
                    ).fetchall()
                }
                count = connection.execute(
                    "SELECT COUNT(*) FROM tech_points"
                ).fetchone()[0]
            # 迁移失败时必须回滚到旧表，记录不能丢。
            self.assertNotIn("bonus_ship_type", columns)
            self.assertEqual(count, 1)

            app.init_db()
            ships = app.list_ships({"name": "古鹰"})
            self.assertEqual(ships[0]["tech_points"][0]["bonus_ship_type"], "重巡")

    def test_init_db_rebuilds_legacy_tech_points_table(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                connection.execute("DROP TABLE tech_points")
                connection.execute(
                    """
                    CREATE TABLE tech_points (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ship_id INTEGER NOT NULL REFERENCES ships(id) ON DELETE CASCADE,
                        stage TEXT NOT NULL,
                        attribute TEXT NOT NULL DEFAULT '',
                        points INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(ship_id, stage, attribute)
                    )
                    """
                )
                ship = app.insert_ship(
                    connection,
                    "金鹿号",
                    "2024-01-01",
                    {
                        "ship_type": "风帆V",
                        "rarity": "精锐",
                        "tech_points": [],
                    },
                )
                connection.execute(
                    """
                    INSERT INTO tech_points (ship_id, stage, attribute, points)
                    VALUES (?, '解锁图鉴', '风帆 命中', 0)
                    """,
                    (ship["id"],),
                )

            app.init_db()
            with app.get_connection() as connection:
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(tech_points)"
                    ).fetchall()
                }
                self.assertIn("bonus_ship_type", columns)
                self.assertIn("bonus_attribute", columns)
            ships = app.list_ships({"ship_type": "风帆"})
            self.assertEqual([ship["name"] for ship in ships], ["金鹿号"])
            self.assertEqual(ships[0]["tech_points"][0]["bonus_ship_type"], "风帆")
            self.assertEqual(ships[0]["tech_points"][0]["bonus_attribute"], "命中")

    def test_clear_ships_clears_config_logs_and_sequences(self):
        with isolated_app_data():
            with app.get_connection() as connection:
                ship = app.insert_ship(
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
            app.update_ship_status(ship["id"], "is_idling", True)
            app.write_config({"next_ship_types": ["战列"]})
            self.assertTrue(app.config.CONFIG_PATH.exists())

            self.assertEqual(app.clear_ships(), 1)
            self.assertFalse(app.config.CONFIG_PATH.exists())
            self.assertEqual(
                app.read_config(),
                {"next_ship_types": [], "target_limits": {}, "manual_targets": {}},
            )
            with app.get_connection() as connection:
                for table in ("ships", "tech_points", "operation_logs"):
                    self.assertEqual(
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        0,
                    )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sqlite_sequence").fetchone()[0],
                    0,
                )
            self.assertEqual(app.SYNC_STATE["status"], "未开始")


if __name__ == "__main__":
    unittest.main()
