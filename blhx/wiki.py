"""兼容入口：实现已迁移到 blhx.datasource.wiki。"""

from __future__ import annotations

import sys

from .datasource import wiki as _module

sys.modules[__name__] = _module
