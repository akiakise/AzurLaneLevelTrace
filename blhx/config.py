"""兼容入口：实现已迁移到 blhx.storage.config。"""

from __future__ import annotations

import sys

from .storage import config as _module

sys.modules[__name__] = _module
