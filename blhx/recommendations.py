"""兼容入口：实现已迁移到 blhx.domain.recommendations。"""

from __future__ import annotations

import sys

from .domain import recommendations as _module

sys.modules[__name__] = _module
