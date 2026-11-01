"""兼容入口：实现已迁移到 blhx.core.state。"""

from __future__ import annotations

import sys

from .core import state as _module

sys.modules[__name__] = _module
