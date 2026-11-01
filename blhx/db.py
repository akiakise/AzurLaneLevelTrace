"""兼容入口：实现已迁移到 blhx.storage.db。"""

from __future__ import annotations

import sys

from .storage import db as _module

sys.modules[__name__] = _module
