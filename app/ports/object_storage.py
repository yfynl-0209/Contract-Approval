"""对象存储端口：与具体实现（本地文件 / MinIO）解耦。

管理的是**长期保存位置**（对象键），与 `approval_attachments.file_path`
（受控临时物化路径）是两回事 —— 调用端拿到永久真实路径等于绕过鉴权，两者不可合并。

按内容寻址：天然去重、与 M4 的解析缓存键对齐、且**内容不可变**
（文件改了必然换 key，绝不覆盖历史证据）。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: SHA-256 十六进制串的合法字符集
_HEX_DIGITS = frozenset("0123456789abcdef")

#: `open_stream` 的默认分片大小（64 KiB：响应转发与 S3 分段读取的常用折中）
DEFAULT_CHUNK_SIZE = 64 * 1024


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

    ## M9 扩展：`stat` / `open_stream` / `read_range`

    端口原本只有整读（`get`），附件下发因此必须把整份字节读进内存再按
    `Range` 切片（attachments.py 的"已登记技术债"）。三个新方法把
    "知道多大"与"按区间取"从全量下载中拆出来 —— 20MB 的上界是配置不是
    架构，调大它不应让进程内存跟着涨。
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

    def stat(self, key: str) -> ObjectRef:
        """只取元数据（大小 / 类型），**不读内容**。

        ⚠️ `sha256`：内容寻址键（`sha256/…`）时为**键内嵌的摘要**——
        写入时 `put` 已拒绝过"键与内容不符"，不可变存储下该承诺持续成立；
        非内容寻址键时为 **空串**（MinIO 的 ETag 是 MD5，冒充 SHA-256 是撒谎）。
        调用方需要真摘要时用 `get` / `open_stream` 自己算。
        """
        ...

    def open_stream(
        self, key: str, *, chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> Iterator[bytes]:
        """按分片**流式**读取，调用方负责耗尽或关闭迭代器。

        不存在时：**首次 next() 才抛** `PermanentStorageError(OBJECT_NOT_FOUND)`——
        与 `get` 的语义一致，但允许调用方先拿到迭代器再失败。
        """
        ...

    def read_range(self, key: str, start: int, end: int) -> bytes:
        """读取**闭区间** `[start, end]`（含两端）的字节。

        越界（`start > end` 或 `end >= size`）抛 `ValueError`——
        这是调用方的编程错误（API 层的 `_parse_range` 已按 RFC 收敛过边界），
        不属于存储故障，不占用端口错误码。
        """
        ...

    def presign_get(self, key: str, *, expires_in: int) -> str:
        """生成短期下载地址。

        本地实现没有真实签名能力，返回值**不得下发给普通调用端**；M9 起由 MinIO 实现。
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


#: 内容寻址键的固定前缀（与各实现共享）
CONTENT_ADDRESSED_PREFIX = "sha256/"

#: 扩展名 → Content-Type 的最小映射（`stat` 不读内容，类型按扩展名推断；
#: 写入时声明的类型不回读 —— S3 的对象元数据查询会把 stat 变成两次往返）
_EXTENSION_CONTENT_TYPES: dict[str, str] = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "json": "application/json",
    "csv": "text/csv",
    "txt": "text/plain",
}


def embedded_digest_of(key: str) -> str:
    """内容寻址键**内嵌**的摘要；非内容寻址键返回空串。

    键形如 `sha256/ab/cd/<64位hex>.<ext>` 或
    `sha256/ab/cd/<64位hex>.standard_document.json`（工件键）——
    摘要取文件名的**第一个点分段**（与 local 实现的 put 校验同一规则）。
    """
    if not key.startswith(CONTENT_ADDRESSED_PREFIX):
        return ""
    return key.rsplit("/", 1)[-1].split(".", 1)[0].lower()


def guess_content_type(key: str) -> str:
    """按扩展名推断 Content-Type（stat 用；未登记的扩展名走八位流）。"""
    extension = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    return _EXTENSION_CONTENT_TYPES.get(extension, "application/octet-stream")
