"""`ObjectStorage` 的**实现无关**合约测试。

## 为什么必须有它

`ObjectStorage` 是 `Protocol`，而 `runtime_checkable` 的 `isinstance`
**只检查方法名** —— 不校验签名，更不校验语义。
所以"这个实现能不能替换那个实现"这个问题，只能靠**跑同一套测试**来回答。

M9 会把 `LocalFileStorage` 换成 MinIO。如果那时才发现 MinIO 的 `put`
会覆盖已有对象、或者 `get` 返回的是一次性流，返工成本远高于现在把合约钉死。

## 用法

实现方继承 `ObjectStorageContract`，提供 `contract_storage` 夹具与两个探查钩子：

```python
class TestMinioStorageContract(ObjectStorageContract):
    @pytest.fixture()
    def contract_storage(self) -> ObjectStorage: ...

    def seed_raw_object(self, storage, key, data) -> None: ...

    def stored_object_count(self, storage) -> int: ...
```

本模块里**没有**以 `Test` 开头的类名，因此 pytest 不会直接收集它。
必须由实现方显式继承 —— 否则会出现"跑了一遍却不知道在测谁"。

## 哪些测试**不该**放进来

凡是依赖实现内部结构的断言（本地实现的 `.tmp` 残留、`root` 目录探查、
`OSError` → 错误分类），都留在各自的实现测试里。合约只约束
**任何实现都必须满足的外部语义**。
"""

from __future__ import annotations

import hashlib

import pytest

from app.enums import ErrorCode
from app.errors import PermanentStorageError
from app.ports.object_storage import ObjectStorage, content_addressed_key


def digest_of(data: bytes) -> str:
    """内容的 SHA-256 十六进制串。"""
    return hashlib.sha256(data).hexdigest()


def key_for(data: bytes, suffix: str = "pdf") -> str:
    """按内容寻址规则构造该内容的对象键。"""
    return content_addressed_key(digest_of(data), suffix=suffix)


class ObjectStorageContract:
    """`ObjectStorage` 的语义合约 —— 每个实现都必须通过。"""

    # ------------------------------------------------------------------
    # 由实现方提供的夹具与钩子
    # ------------------------------------------------------------------

    @pytest.fixture()
    def contract_storage(self) -> ObjectStorage:
        """被测实现。子类必须覆盖。"""
        raise NotImplementedError("子类必须提供 contract_storage 夹具")

    def seed_raw_object(self, storage: ObjectStorage, key: str, data: bytes) -> None:
        """绕过 `put`，直接在 `key` 处放一份内容。

        用于构造"存储里的内容与键名对不上"的场景 ——
        这是**唯一**能验证实现是否真的校验了摘要的办法。
        若无法绕过写入（例如真实 MinIO 不接受直连写），
        也可以改用"写入后再由外部篡改"的方式实现。
        """
        raise NotImplementedError("子类必须实现 seed_raw_object")

    def stored_object_count(self, storage: ObjectStorage) -> int:
        """当前存储中对象的个数（用于验证内容寻址去重）。"""
        raise NotImplementedError("子类必须实现 stored_object_count")

    # ------------------------------------------------------------------
    # 1. 基本读写
    # ------------------------------------------------------------------

    def test_put_then_get_roundtrip(self, contract_storage: ObjectStorage) -> None:
        """写入后取回的字节必须与写入时完全一致 —— 这是"证据可核验"的地基。"""
        data = "PDF-1.4 合同正文".encode()
        key = key_for(data)

        ref = contract_storage.put(key, data, content_type="application/pdf")

        assert ref.key == key
        assert ref.size == len(data)
        assert ref.sha256 == digest_of(data)
        assert contract_storage.get(key) == data
        assert contract_storage.exists(key) is True

    def test_put_is_idempotent_for_same_content(
        self, contract_storage: ObjectStorage
    ) -> None:
        """重复上传同一内容**不是错误**。

        内容寻址下"同一份模板合同被多个审批单上传"是正常业务场景；
        若此处报错，拉取流程会出现大量假失败。
        """
        data = b"identical content"
        key = key_for(data)

        first = contract_storage.put(key, data, content_type="application/pdf")
        second = contract_storage.put(key, data, content_type="application/pdf")

        assert first == second

    def test_same_content_is_stored_only_once(
        self, contract_storage: ObjectStorage
    ) -> None:
        """同一份内容只落一个对象。"""
        data = b"same bytes uploaded twice"
        key = key_for(data)

        contract_storage.put(key, data, content_type="application/pdf")
        contract_storage.put(key, data, content_type="application/pdf")

        assert self.stored_object_count(contract_storage) == 1, "内容寻址去重失效"

    def test_repeated_get_is_stable(self, contract_storage: ObjectStorage) -> None:
        """连续两次 `get` 必须返回完全相同的字节。

        这条专门为新实现准备：对象存储的读接口常见"返回一次性流"，
        一旦实现把流直接暴露出去，第二次读就会拿到空内容 ——
        而"证据可核验"要求随时能再读一次原始文件。
        """
        data = b"read me twice"
        key = key_for(data)
        contract_storage.put(key, data, content_type="application/pdf")

        assert contract_storage.get(key) == contract_storage.get(key) == data

    # ------------------------------------------------------------------
    # 2. 内容寻址：键与内容必须一致
    # ------------------------------------------------------------------

    def test_key_digest_must_match_content(
        self, contract_storage: ObjectStorage
    ) -> None:
        """传错 key 必须报错。

        若不校验，A 文件会被写进 B 的键下，而且**永远不会被发现**：
        之后 `get(B_key)` 返回的是 A 的内容，但所有人都以为拿到了 B。
        """
        wrong_key = key_for("B 的内容".encode())

        with pytest.raises(PermanentStorageError) as excinfo:
            contract_storage.put(
                wrong_key, "A 的内容".encode(), content_type="application/pdf"
            )

        assert excinfo.value.code == ErrorCode.CHECKSUM_MISMATCH

    def test_key_with_different_size_existing_object_is_rejected(
        self, contract_storage: ObjectStorage
    ) -> None:
        """同一键下已有**大小不同**的对象，说明存储内容与键名已对不上，必须报错。"""
        data = b"correct content"
        key = key_for(data)
        self.seed_raw_object(contract_storage, key, b"short")

        with pytest.raises(PermanentStorageError) as excinfo:
            contract_storage.put(key, data, content_type="application/pdf")

        assert excinfo.value.code == ErrorCode.CHECKSUM_MISMATCH

    def test_key_with_same_size_but_different_content_is_rejected(
        self, contract_storage: ObjectStorage
    ) -> None:
        """**同长度但内容不同**必须被拒绝 —— 这是"只比大小"的致命盲区。

        构造"正确内容为 AAAA、存储里已有同长度错误内容 BBBB"的场景：

        ```text
        只比大小的实现：put 放行 → 声称"摘要 AAAA 保存成功"
                                 → get 实际返回 BBBB
        ```

        系统带着一份**看起来成功、实际内容不符**的对象继续往下走，
        "历史证据可核验"这条核心承诺被破坏，**而且没有任何报错**。

        这里刻意选择"报错"而不是"自动覆盖修正"：存储里出现与键不符的内容，
        说明存储本身已不可信（磁盘损坏或被外部进程改写），
        这属于必须让人知道的信号，不该被一次"顺手修复"掩盖过去。
        """
        correct = b"AAAA"
        corrupted = b"BBBB"
        key = key_for(correct)
        assert len(correct) == len(corrupted), "前置条件：两者长度必须相同"

        self.seed_raw_object(contract_storage, key, corrupted)

        with pytest.raises(PermanentStorageError) as excinfo:
            contract_storage.put(key, correct, content_type="application/pdf")

        assert excinfo.value.code == ErrorCode.CHECKSUM_MISMATCH
        # 拒绝之后不得改动已有内容：既不能"幂等跳过"，也不能悄悄覆盖
        assert contract_storage.get(key) == corrupted

    def test_non_content_addressed_key_skips_digest_check(
        self, contract_storage: ObjectStorage
    ) -> None:
        """非 `sha256/` 前缀的键不参与摘要校验。

        否则导出的报表、审计工件等"非内容寻址"的对象根本写不进去。
        """
        ref = contract_storage.put(
            "exports/report.csv", b"a,b\n1,2", content_type="text/csv"
        )

        assert ref.key == "exports/report.csv"
        assert contract_storage.get("exports/report.csv") == b"a,b\n1,2"

    # ------------------------------------------------------------------
    # 3. 键的合法性
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "bad_key",
        ["../escaped.txt", "a/../../escaped.txt", "", "  padded.txt", "/absolute.txt"],
        ids=["上跳一级", "中途上跳", "空串", "含首尾空白", "绝对路径"],
    )
    def test_invalid_key_is_rejected(
        self, contract_storage: ObjectStorage, bad_key: str
    ) -> None:
        """非法键一律被拒，错误码为"路径非法"（确定性错误，重试无意义）。"""
        with pytest.raises(PermanentStorageError) as excinfo:
            contract_storage.put(bad_key, b"x", content_type="text/plain")

        assert excinfo.value.code == ErrorCode.STORAGE_PATH_INVALID

    # ------------------------------------------------------------------
    # 4. 缺失对象与短期访问
    # ------------------------------------------------------------------

    def test_exists_is_false_for_unwritten_key(
        self, contract_storage: ObjectStorage
    ) -> None:
        """`exists` 必须能区分"没写过"与"写过"。

        调用方据此跳过重复下载；若恒返回 True，每次都会白下载一遍。
        """
        assert contract_storage.exists(key_for(b"never written")) is False

    def test_get_missing_object_reports_object_not_found(
        self, contract_storage: ObjectStorage
    ) -> None:
        """缺失用独立的 `OBJECT_NOT_FOUND`，不要复用"路径非法"。

        两者排障动作完全不同：一个是"键写错了"，一个是"东西没写进去"。
        """
        with pytest.raises(PermanentStorageError) as excinfo:
            contract_storage.get(key_for(b"never written"))

        assert excinfo.value.code == ErrorCode.OBJECT_NOT_FOUND

    def test_presign_get_requires_positive_expiry(
        self, contract_storage: ObjectStorage
    ) -> None:
        """`expires_in <= 0` 是调用方错误，属于编程缺陷，直接抛 `ValueError`。"""
        with pytest.raises(ValueError):
            contract_storage.presign_get(key_for(b"x"), expires_in=0)

    # ------------------------------------------------------------------
    # 5. M9 扩展：stat / open_stream / read_range
    # ------------------------------------------------------------------

    def test_stat_reports_size_without_reading_content(
        self, contract_storage: ObjectStorage
    ) -> None:
        """`stat` 只取元数据：大小正确、摘要来自**键内嵌**（内容寻址键）。"""
        data = b"stat me without reading"
        key = key_for(data)
        contract_storage.put(key, data, content_type="application/pdf")

        ref = contract_storage.stat(key)

        assert ref.key == key
        assert ref.size == len(data)
        assert ref.sha256 == digest_of(data), "内容寻址键的 stat 摘要 = 键内嵌摘要"
        assert ref.content_type == "application/pdf"

    def test_stat_of_non_content_addressed_key_has_empty_sha256(
        self, contract_storage: ObjectStorage
    ) -> None:
        """非内容寻址键：实现**拿不到**真摘要（MinIO ETag 是 MD5）——
        返回空串是诚实的；编一个值出来才是事故。"""
        contract_storage.put("exports/report.csv", b"a,b", content_type="text/csv")

        ref = contract_storage.stat("exports/report.csv")

        assert ref.sha256 == ""
        assert ref.size == 3

    def test_read_range_returns_inclusive_slice(
        self, contract_storage: ObjectStorage
    ) -> None:
        """`read_range(start, end)` 闭区间两端含 —— 与 HTTP Range 语义对齐。"""
        data = b"0123456789"
        key = key_for(data)
        contract_storage.put(key, data, content_type="application/pdf")

        assert contract_storage.read_range(key, 0, 3) == b"0123"
        assert contract_storage.read_range(key, 8, 9) == b"89"
        assert contract_storage.read_range(key, 4, 4) == b"4"  # 单字节区间

    def test_read_range_out_of_bounds_is_value_error(
        self, contract_storage: ObjectStorage
    ) -> None:
        """越界是**调用方编程错误**（API 层已按 RFC 收敛边界），ValueError 而非端口错误。"""
        data = b"0123456789"
        key = key_for(data)
        contract_storage.put(key, data, content_type="application/pdf")

        with pytest.raises(ValueError):
            contract_storage.read_range(key, 0, 10)  # end >= size
        with pytest.raises(ValueError):
            contract_storage.read_range(key, 5, 2)  # start > end
        with pytest.raises(ValueError):
            contract_storage.read_range(key, -1, 3)

    def test_open_stream_yields_the_whole_object(
        self, contract_storage: ObjectStorage
    ) -> None:
        """流式分片拼回应与整读逐字节一致 —— 分片是实现细节，不是语义。"""
        data = bytes(range(256)) * 700  # ~176KB，跨多个 64KiB 分片
        key = key_for(data)
        contract_storage.put(key, data, content_type="application/pdf")

        chunks = list(contract_storage.open_stream(key))

        assert b"".join(chunks) == data
        assert len(chunks) > 1, "应真正分片，而不是一次读完"

    def test_open_stream_missing_object_raises_on_first_next(
        self, contract_storage: ObjectStorage
    ) -> None:
        """缺失对象：拿到迭代器**不炸**，首次 next() 才抛 —— 与端口文档一致。"""
        iterator = contract_storage.open_stream(key_for(b"never written"))

        with pytest.raises(PermanentStorageError) as excinfo:
            next(iterator)

        assert excinfo.value.code == ErrorCode.OBJECT_NOT_FOUND

    def test_read_range_missing_object_reports_object_not_found(
        self, contract_storage: ObjectStorage
    ) -> None:
        """`read_range` 的缺失语义与 `get` 相同：独立的 OBJECT_NOT_FOUND。"""
        with pytest.raises(PermanentStorageError) as excinfo:
            contract_storage.read_range(key_for(b"never written"), 0, 1)

        assert excinfo.value.code == ErrorCode.OBJECT_NOT_FOUND
