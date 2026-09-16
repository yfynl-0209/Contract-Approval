"""mock 审批系统 —— 模拟本项目的外部对接方。

独立进程、独立端口（8001），刻意**不 import `app.*`**：

    app/             我们开发的工具服务
    mock_approval/   我们模拟的外部企业审批系统

这样"接入能力"才是真的通过 HTTP 对接，而不是直接读本地表。

模块构成：`fixtures.json` / `contract_texts.py` / `fixtures/` / `store.py` / `fault_inject.py` / `sample_pdf.py` / `main.py`
"""

from __future__ import annotations

from enum import Enum

__all__ = ["StrEnum"]


class StrEnum(str, Enum):
    """本地字符串枚举基类。不复用 `app.enums.StrEnum`，以保持对 app 包的零依赖。"""

    def __str__(self) -> str:
        return self.value
