"""兼容入口：实现已迁移到 blhx.transfer.csv_io。"""

from __future__ import annotations

import sys

from .transfer import csv_io as _module

sys.modules[__name__] = _module
