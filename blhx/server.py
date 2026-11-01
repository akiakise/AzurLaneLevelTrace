"""兼容入口：实现已迁移到 blhx.web.server。"""

from __future__ import annotations

import sys

from .web import server as _module

sys.modules[__name__] = _module
