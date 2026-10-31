import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError

import app


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
        with tempfile.TemporaryDirectory() as temp_dir:
            app.db.DATA_DIR = Path(temp_dir)
            app.db.DATABASE_PATH = Path(temp_dir) / "test.sqlite3"
            app.init_db()
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

    def test_xlsx_roundtrip(self):
        rows = [
            ["舰船名称", "是否已挂机", "是否已练级完成"],
            ["武藏", "是", "否"],
        ]
        data = app.xlsx_write(rows, "测试")
        self.assertEqual(app.xlsx_read(data), rows)

    def test_import_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app.db.DATA_DIR = Path(temp_dir)
            app.db.DATABASE_PATH = Path(temp_dir) / "test.sqlite3"
            app.init_db()
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
        with tempfile.TemporaryDirectory() as temp_dir:
            app.db.DATA_DIR = Path(temp_dir)
            app.db.DATABASE_PATH = Path(temp_dir) / "test.sqlite3"
            app.init_db()
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
            ships = app.list_ships({"tech_attribute": "机动"})
            self.assertEqual([ship["name"] for ship in ships], ["古鹰"])

            updated = app.update_ship_status(ship_1["id"], "is_idling", True)
            self.assertTrue(updated["is_idling"])
            self.assertIsNone(updated["is_trained"])

    def test_tech_attribute_matches_each_ship_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app.db.DATA_DIR = Path(temp_dir)
            app.db.DATABASE_PATH = Path(temp_dir) / "test.sqlite3"
            app.init_db()
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
                {"ship_type": "战列", "tech_attribute": "耐久"}
            )
            self.assertEqual([ship["name"] for ship in ships], ["武藏", "易北"])
            ships = app.list_ships(
                {"ship_type": "战列", "tech_attribute": "炮击"}
            )
            self.assertEqual(ships, [])

    def test_next_ship_type_config_uses_data_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app.db.DATA_DIR = Path(temp_dir)
            app.db.DATABASE_PATH = Path(temp_dir) / "test.sqlite3"
            app.config.CONFIG_PATH = Path(temp_dir) / "config.json"
            app.init_db()
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
            self.assertEqual(app.read_config(), {"next_ship_types": ["战列"]})
            with app.config.CONFIG_PATH.open(encoding="utf-8") as file:
                import json

                self.assertEqual(
                    json.load(file), {"next_ship_types": ["战列"]}
                )
            with self.assertRaises(ValueError):
                app.save_next_ship_types(["战列", "驱逐"])

    def test_next_ship_supports_available_ship_types(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app.db.DATA_DIR = Path(temp_dir)
            app.db.DATABASE_PATH = Path(temp_dir) / "test.sqlite3"
            app.init_db()
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

            options = app.available_next_ship_types()
            self.assertEqual(
                options,
                [
                    {"ship_type": "战列", "idle_count": 1, "train_count": 1},
                    {"ship_type": "重巡", "idle_count": 1, "train_count": 1},
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
            self.assertEqual(
                app.next_ship("idle", ship_types=["重巡"])["name"], "古鹰"
            )
            grouped = app.next_ships_by_type("both", ["战列", "重巡"])
            self.assertEqual(
                [row["ship_type"] for row in grouped], ["战列", "重巡"]
            )
            self.assertEqual(grouped[0]["idle"]["name"], "武藏")
            self.assertEqual(grouped[0]["train"]["name"], "武藏")
            self.assertEqual(grouped[1]["idle"]["name"], "古鹰")
            self.assertEqual(grouped[1]["train"]["name"], "古鹰")
            with self.assertRaises(ValueError):
                app.next_ship("train", ship_types=["战列", "驱逐"])

            app.update_ship_status(heavy_cruiser["id"], "is_trained", True)
            with self.assertRaises(ValueError):
                app.next_ship("train", ship_types=["重巡"])

    def test_operation_history_records_status_and_delete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app.db.DATA_DIR = Path(temp_dir)
            app.db.DATABASE_PATH = Path(temp_dir) / "test.sqlite3"
            app.init_db()
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
        with tempfile.TemporaryDirectory() as temp_dir:
            app.db.DATA_DIR = Path(temp_dir)
            app.db.DATABASE_PATH = Path(temp_dir) / "test.sqlite3"
            app.init_db()
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
        with tempfile.TemporaryDirectory() as temp_dir:
            app.db.DATA_DIR = Path(temp_dir)
            app.db.DATABASE_PATH = Path(temp_dir) / "test.sqlite3"
            app.init_db()
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
        with tempfile.TemporaryDirectory() as temp_dir:
            app.db.DATA_DIR = Path(temp_dir)
            app.db.DATABASE_PATH = Path(temp_dir) / "test.sqlite3"
            app.init_db()
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


if __name__ == "__main__":
    unittest.main()
