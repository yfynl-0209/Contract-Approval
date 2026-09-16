"""对象存储端口：与具体实现（本地文件 / MinIO）解耦。

管理的是**长期保存位置**（对象键），与 `approval_attachments.file_path`
（受控临时物化路径）是两回事 —— 调用端拿到永久真实路径等于绕过鉴权，两者不可合并。

按内容寻址：天然去重、与 M4 的解析缓存键对齐、且**内容不可变**
（文件改了必然换 key，绝不覆盖历史证据）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: SHA-256 十六进制串的合法字符集
_HEX_DIGITS = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class ObjectRef:
    """对象存储中的一份对象。"""

    key: str
    size: int
    sha256: str
    content_type: str


@runtime_checkable
class ObjectStorage(Protocol):
    """对象存储端口。

    ⚠️ `runtime_checkable` 的 `isinstance` 只检查方法名，签名与语义由合约测试守。

    实现必须满足：**不可变**（同一 key 不得覆盖成不同内容）、**可重复读**。
    """

    def put(self, key: str, data: bytes, *, content_type: str) -> ObjectRef:
        """写入对象；key 已存在且内容一致时幂等返回，不报错。"""
        ...

    def get(self, key: str) -> bytes:
        """读取对象；不存在时抛 `PermanentStorageError`。"""
        ...

    def exists(self, key: str) -> bool:
        """对象是否存在。"""
        ...

    def presign_get(self, key: str, *, expires_in: int) -> str:
        """生成短期下载地址。

        本地实现没有真实签名能力，返回值**不得下发给普通调用端**；M8 起由 MinIO 实现。
        """
        ...


def content_addressed_key(sha256: str, *, suffix: str = "bin") -> str:
    """由内容摘要构造对象键：`sha256/ab/cd/<sha256>.<ext>`。

    两级目录前缀避免单目录堆积过多文件（本地文件系统在几千个文件后明显变慢）。

    **校验不是可选的**：对象键最终会成为文件路径的一部分，未校验的输入可以构造
    `../` 逃逸。这里用"只允许十六进制 + 只允许字母数字扩展名"从源头封死。

    Raises:
        ValueError: 摘要非法，或扩展名含非字母数字字符。
    """
    normalized = sha256.strip().lower()
    if len(normalized) != 64 or not set(normalized) <= _HEX_DIGITS:
        raise ValueError(f"不是合法的 SHA-256 十六进制串：{sha256!r}")

    extension = suffix.strip().lstrip(".").lower()
    if not extension:
        extension = "bin"
    if not extension.isalnum():
        raise ValueError(f"扩展名只允许字母与数字：{suffix!r}")

    return f"sha256/{normalized[:2]}/{normalized[2:4]}/{normalized}.{extension}"
