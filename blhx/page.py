"""兼容入口：实现已迁移到 blhx.web.page。"""

from __future__ import annotations

import sys

from .web import page as _module

sys.modules[__name__] = _module
