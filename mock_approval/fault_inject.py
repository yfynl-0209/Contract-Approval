"""故障注入：让"外部系统不稳定"这件事可以按需复现。

需求 2.4.4 规定，**接口调用失败**要进入 `blocked` 状态并允许人工重试。
但如果外部系统永远正常，这条分支就永远演示不出来，也无法测试。

因此在 mock 侧提供故障注入：可以指定目标接口、目标审批单与故障类型，
让审查系统真实地经历 500 / 超时 / 404，从而走到 `blocked` → 人工重试 → 恢复的完整路径。

用法（服务启动后）：

    # 让 HT-2026-0001 的附件下载返回 500
    curl -X POST http://127.0.0.1:8001/api/faults -H "Authorization: Bearer demo-token" \\
         -H "Content-Type: application/json" \\
         -d '{"target": "download", "instance_id": "HT-2026-0001", "mode": "http_500"}'

    # 查看当前故障 / 清除全部故障
    curl http://127.0.0.1:8001/api/faults
    curl -X DELETE http://127.0.0.1:8001/api/faults
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Final

from mock_approval import StrEnum

#: 支持注入故障的目标接口；`*` 表示所有接口
TARGETS: Final[frozenset[str]] = frozenset(
    {"*", "list_pending", "get_detail", "download", "write_comment"}
)


class FaultMode(StrEnum):
    """故障类型。"""

    HTTP_500 = "http_500"  # 服务端错误
    TIMEOUT = "timeout"  # 先阻塞一段时间再返回 504，模拟调用超时
    NOT_FOUND = "not_found"  # 资源不存在（模拟附件被删除）


@dataclass(frozen=True)
class Fault:
    target: str
    mode: str
    instance_id: str | None = None
    delay_seconds: float = 5.0
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class FaultRegistry:
    """故障注册表。

    查找顺序：**精确匹配（目标接口 + 指定审批单）优先于通配**，
    这样"只让某一条审批单失败"的演示不会被全局故障干扰。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._faults: list[Fault] = []

    def set(self, fault: Fault) -> Fault:
        if fault.target not in TARGETS:
            raise ValueError(f"未知的故障目标 {fault.target!r}，可选：{sorted(TARGETS)}")
        if fault.mode not in {m.value for m in FaultMode}:
            raise ValueError(f"未知的故障类型 {fault.mode!r}")
        with self._lock:
            self._faults.append(fault)
        return fault

    def clear(self, target: str | None = None) -> int:
        """清除故障；不传参则清空全部。返回清除条数。"""
        with self._lock:
            if target is None:
                count = len(self._faults)
                self._faults = []
                return count
            kept = [f for f in self._faults if f.target != target]
            count = len(self._faults) - len(kept)
            self._faults = kept
            return count

    def find(self, target: str, instance_id: str | None = None) -> Fault | None:
        """查找作用于给定请求的故障。"""
        with self._lock:
            # 第一优先：目标接口 + 指定审批单完全匹配
            for fault in reversed(self._faults):
                if fault.target == target and fault.instance_id == instance_id:
                    return fault
            # 第二优先：目标接口匹配且不限审批单
            for fault in reversed(self._faults):
                if fault.target == target and fault.instance_id is None:
                    return fault
            # 第三优先：全局通配
            for fault in reversed(self._faults):
                if fault.target == "*":
                    return fault
        return None

    def list_all(self) -> list[Fault]:
        with self._lock:
            return list(self._faults)


#: 模块级单例，与 store 一样由所有请求共享
faults = FaultRegistry()
