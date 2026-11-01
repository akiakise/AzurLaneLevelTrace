from __future__ import annotations

import os
from pathlib import Path


VERSION_FILE = Path(__file__).resolve().parent.parent.parent / "VERSION"
DEFAULT_VERSION = "0.0.0"


def read_version() -> str:
    """读取仓库根目录 VERSION；容器内缺失时回退到 APP_VERSION 或默认值。"""
    try:
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        version = ""
    return version or os.getenv("APP_VERSION", "").strip() or DEFAULT_VERSION


APP_VERSION = read_version()
