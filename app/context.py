"""关联 ID（`correlation_id`）的**进程内携带**（设计文档 §4.7 / 决策④）。

## 为什么用 contextvar 而不是给每个函数加参数

加参数意味着**每一处新增的日志调用都可能忘记传**，而漏传的表现是
"日志少了一段" —— 不报错、不告警，只在排障时发现追不下去。
contextvar 让"带上关联 ID"成为默认行为，漏不掉。

## 为什么必须清理（这是本模块最容易出事的地方）

contextvar 的值**会随线程复用而残留**：线程池把线程交还给下一个请求时，
变量仍是上一个请求留下的值。不清理的表现是**日志串号** ——
A 请求的日志被记到 B 请求的关联 ID 下，而两边各自看起来都很正常。
**排障时最怕的就是"证据本身是错的"**，因此 `bind()` / `reset()` 必须成对使用。

## 校验失败为什么不静默替换

静默替换（例如换成一个新 UUID）会让调用方手里的 ID 与我们库里的对不上 ——
他以为能查到，实际永远查不到，且**没有任何提示**。因此不合法就直接拒绝（400）。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

#: 调用方可传入的关联 ID 的字符集与长度上限（§4.7）。
#: 限定字符集不只是"防注入"：这个值会进日志、进响应头、进 URL 查询串，
#: 放任意字符进来，它在其中某一处就会变形。
CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

#: 请求头名
CORRELATION_ID_HEADER = "X-Correlation-ID"

_context: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_correlation_id() -> str:
    """生成服务端关联 ID（调用方未提供时使用）。"""
    return uuid.uuid4().hex


def is_valid_correlation_id(value: str | None) -> bool:
    """是否符合可接受的字符集与长度。"""
    return bool(value) and CORRELATION_ID_PATTERN.fullmatch(value) is not None


def get_correlation_id() -> str | None:
    """当前上下文里的关联 ID。

    未绑定时返回 `None`（CLI、测试、以及**没有 HTTP 请求入口**的路径）——
    而不是凭空生成一个：那样每次调用都会得到不同的值，
    "同一次请求的全链路"反而被拆成了互不相干的碎片。
    """
    return _context.get()


def bind_correlation_id(value: str) -> Token[str | None]:
    """绑定并返回 token；调用方**必须**用它调 `reset_correlation_id`。

    Raises:
        ValueError: 取值不符合 `CORRELATION_ID_PATTERN`（调用方须映射为 400）。
    """
    if not is_valid_correlation_id(value):
        raise ValueError(
            f"关联 ID 非法：需匹配 ^[A-Za-z0-9._:-]{{1,128}}$，收到 {value!r}"
        )
    return _context.set(value)


def reset_correlation_id(token: Token[str | None]) -> None:
    """恢复到绑定前的值。**必须**在请求结束（含异常路径）时调用。"""
    _context.reset(token)


@contextmanager
def correlation_scope(value: str | None = None) -> Iterator[str]:
    """在 `with` 块内绑定关联 ID，**退出时一定重置**。

    优先用它而不是手写 `bind` / `reset`：手写时漏掉 `finally`
    就是上面说的"日志串号"，而且只在并发下才显形。
    Worker 从作业记录读回关联 ID 后，也是用它把上下文补上的。
    """
    identifier = value if value is not None else new_correlation_id()
    token = bind_correlation_id(identifier)
    try:
        yield identifier
    finally:
        reset_correlation_id(token)
