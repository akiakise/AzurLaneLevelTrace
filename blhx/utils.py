"""兼容入口：实现已迁移到 blhx.core.utils。"""

from __future__ import annotations

import sys

from .core import utils as _module

sys.modules[__name__] = _module
