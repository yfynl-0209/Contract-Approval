"""给**不可中断的调用**加一道执行时限。

PyMuPDF 的 `get_pixmap` 与 ONNX Runtime 的推理都是**同步 C 调用，无法从外部中断**。
没有时限时，一次卡死会让 Worker **永远阻塞** —— 而症状只是"这个作业不动了"，
既没有错误码，也没有日志，排障时连"卡在哪一步"都看不出来。

## 为什么放在 `app/` 顶层而不是某个适配器目录里

它是**跨层工具**：解析适配器用它，将来 Worker 侧的超时控制也会用它。
放进任何一个适配器目录都会产生"适配器 import 适配器" ——
而 `tests/test_source_invariants.py::test_composition_root_is_the_only_adapter_importer`
明确要求**适配器只在组合根被接线**。那条不变量是对的，
因此该改的是这个文件的**位置**，不是放宽约束。
（与 `app/context.py` 同理：横切关注点放在顶层。）

## 能力边界（必须说清，否则会被当成"真正的超时"）

超时后我们做的只是**不再等待**，那个调用仍在后台线程里跑：

- 它解决的是"**Worker 永远阻塞**"，**不**解决"那个调用还在吃 CPU"；
- 反复超时意味着后台线程在累积 —— 那是"这个输入有问题，或超时值太小"的信号，
  必须能从日志里看出来（因此 `DeadlineExceeded` 带上标签与超时值）；
- 能做到真正取消的方案要用**子进程隔离**（可杀），那是另一个量级的改动，M4 不做。

线程用 `daemon=True`：被放弃的调用不该拖住进程退出。
"""

from __future__ import annotations

import threading
from typing import Any, TypeVar

T = TypeVar("T")


class DeadlineExceeded(Exception):
    """调用在时限内没有返回。**不是**业务错误 —— 由调用方翻译成对应的端口异常。"""

    def __init__(self, label: str, timeout: float) -> None:
        super().__init__(f"{label} 超过执行时限 {timeout:g} 秒")
        self.label = label
        self.timeout = timeout


def run_with_deadline(operation: Any, *, timeout: float, label: str) -> Any:
    """在独立线程里执行 `operation()`，超过 `timeout` 秒仍未返回则抛 `DeadlineExceeded`。

    Raises:
        DeadlineExceeded: 超时（原调用被放弃，但仍可能在后台运行）。
        其余异常：**原样透传** —— 调用方靠它区分"超时"与"调用失败"。
    """
    outcome: dict[str, Any] = {}
    finished = threading.Event()

    def _target() -> None:
        try:
            outcome["value"] = operation()
        # 捕获 BaseException：被放弃的线程里再抛异常会打到
        # threading 的默认钩子上，而那条路径与本次调用已经无关了
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc
        finally:
            finished.set()

    threading.Thread(target=_target, name=f"deadline:{label}", daemon=True).start()

    if not finished.wait(timeout):
        raise DeadlineExceeded(label, timeout)
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")
