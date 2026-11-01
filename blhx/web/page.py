from __future__ import annotations

from pathlib import Path


STATIC_DIR = Path(__file__).with_name("static")
PAGE_HTML = (Path(__file__).with_name("page.html")).read_text(encoding="utf-8")

