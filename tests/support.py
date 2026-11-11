from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from blhx.core.constants import BASE_DIR
from blhx.storage import config, db


PROJECT_DATA_DIR = (BASE_DIR / "data").resolve()


@contextmanager
def isolated_app_data() -> Iterator[Path]:
    """把数据库与配置文件切到临时目录，测试结束后恢复原路径。"""
    with tempfile.TemporaryDirectory(prefix="blhx-tests-") as temp_dir:
        data_dir = Path(temp_dir).resolve()
        if data_dir == PROJECT_DATA_DIR or PROJECT_DATA_DIR in data_dir.parents:
            raise RuntimeError(f"测试数据目录不得位于正式 data 目录内：{data_dir}")
        previous = (
            db.DATA_DIR,
            db.DATABASE_PATH,
            config.CONFIG_PATH,
        )
        db.DATA_DIR = data_dir
        db.DATABASE_PATH = data_dir / "blhx-training.sqlite3"
        config.CONFIG_PATH = data_dir / "config.json"
        db.init_db()
        try:
            yield data_dir
        finally:
            db.DATA_DIR, db.DATABASE_PATH, config.CONFIG_PATH = previous
