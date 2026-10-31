import tempfile
import unittest
from pathlib import Path

import app


class AppTests(unittest.TestCase):
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

    def test_parse_ship_raw(self):
        raw = """
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
        self.assertEqual(len(parsed["tech_points"]), 3)
        self.assertEqual(parsed["tech_points"][0]["attribute"], "耐久")
        self.assertEqual(parsed["tech_points"][2]["attribute"], "炮击")

    def test_xlsx_roundtrip(self):
        rows = [
            ["舰船名称", "是否已挂机", "是否已练级完成"],
            ["武藏", "是", "否"],
        ]
        data = app.xlsx_write(rows, "测试")
        self.assertEqual(app.xlsx_read(data), rows)

    def test_import_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app.DATA_DIR = Path(temp_dir)
            app.DATABASE_PATH = Path(temp_dir) / "test.sqlite3"
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


if __name__ == "__main__":
    unittest.main()
