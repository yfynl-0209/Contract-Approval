"""本地对象存储适配器测试（M3 / T3 + T9）。

本文件分两部分，**边界要分清**：

| 部分 | 内容 | 换实现时要做什么 |
| --- | --- | --- |
| `TestLocalFileStorageContract` | 继承 `tests/contract/storage_contract.py` 的语义合约 | **不用重写**，换个子类即可 |
| 其余测试 | 依赖本地文件系统内部结构的断言 | 各实现**各自**写 |

M9 换成 MinIO 时，新增 `TestMinioStorageContract(ObjectStorageContract)`
就自动获得整套语义验证；而 `.tmp` 残留、`root` 目录探查、
`OSError` 分类这些只有本地文件系统才有的行为，留在本文件里。

## 本地实现特有的风险

| 风险 | 失败后果 |
| --- | --- |
| 非原子写入 | 进程被杀后留下**半份合同**，且看起来是完整的 |
| 临时文件残留 | 合同正文留在不受数据库追踪的文件里，"删库即清空"不再成立 |
| 路径边界 | **A 文件被写进 B 的键下且永不暴露**，比写入失败危险得多 |

错误分类决定"重试还是失败"：权限不足是确定性的（重试不会让权限出现）；
其他 IO 问题按瞬时处理（一次抖动不该让任务永久失败）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.adapters.storage.local_file_storage import LocalFileStorage
from app.enums import ErrorCode
from app.errors import AppError, PermanentStorageError, TransientStorageError
from app.ports.object_storage import ObjectStorage
from contract.storage_contract import ObjectStorageContract, key_for

# ============================================================
# 0. 把语义合约套在本地实现上
# ============================================================


class TestLocalFileStorageContract(ObjectStorageContract):
    """本地实现必须通过整套存储合约。

    M9 的 MinIO 实现会继承同一个基类 —— 这就是"可替换"的证明方式：
    不是读代码确认方法名对得上，而是**跑同一套测试**。
    """

    @pytest.fixture()
    def contract_storage(self, work_dir: Path) -> ObjectStorage:
        # 与下面的 storage 夹具分开目录：合约测试会做"写入被篡改的内容"，
        # 与实现测试共用一个目录会让两边的残留互相污染
        return LocalFileStorage(work_dir / "contract-objects")

    def seed_raw_object(self, storage: ObjectStorage, key: str, data: bytes) -> None:
        assert isinstance(storage, LocalFileStorage)
        _write_raw(storage, key, data)

    def stored_object_count(self, storage: ObjectStorage) -> int:
        assert isinstance(storage, LocalFileStorage)
        return len([path for path in storage.root.rglob("*") if path.is_file()])


# ============================================================
# 以下为本地实现特有行为
# ============================================================


def test_put_accepts_artifact_keys_with_kind_suffix(work_dir: Path) -> None:
    """工件键形如 `sha256/{ab}/{cd}/{sha}.{kind}.json` —— 摘要是**第一个点分段**。

    曾经用 `Path(key).stem` 提取，只剥掉 `.json`，把 `…standard_document`
    当成摘要与内容比对：**同一份内容**也被判 `CHECKSUM_MISMATCH`，
    真实存储路径上所有工件写入全部失败（单元测试用假存储或无后缀键，
    因此没抓到；M8 演示现场第一次用真存储写工件时暴露）。
    """
    import hashlib

    storage = LocalFileStorage(work_dir / "objects")
    data = b'{"schema_version": 1, "pages": []}'
    digest = hashlib.sha256(data).hexdigest()
    key = f"sha256/{digest[0:2]}/{digest[2:4]}/{digest}.standard_document.json"

    ref = storage.put(key, data, content_type="application/json")

    assert ref.sha256 == digest
    assert storage.get(key) == data



@pytest.fixture()
def storage(work_dir: Path) -> LocalFileStorage:
    """每个测试用独立的存储根目录。"""
    return LocalFileStorage(work_dir / "objects")


def _write_raw(storage: LocalFileStorage, key: str, data: bytes) -> None:
    """绕过 `put` 直接在目标位置落一份内容（用于构造"存储被损坏"的场景）。"""
    target = storage.root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


# ---- 1. 原子写入 ----


def test_atomic_write_leaves_no_temporary_files(storage: LocalFileStorage) -> None:
    """写入走"临时文件 + 原子替换"，因此不得留下 `.tmp` 残留。"""
    data = b"atomic"
    storage.put(key_for(data), data, content_type="application/pdf")

    assert list(storage.root.rglob("*.tmp")) == []


def test_temporary_file_is_removed_when_replace_fails(
    storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原子替换失败时必须清掉临时文件。

    否则 **合同正文会残留在不受数据库追踪的临时文件里**：
    既占空间，也让"删库即清空数据"这个预期不再成立。
    """
    data = b"contract body"
    key = key_for(data)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError(5, "模拟替换失败")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(TransientStorageError):
        storage.put(key, data, content_type="application/pdf")

    leftovers = [path for path in storage.root.rglob("*") if path.is_file()]
    assert leftovers == [], f"替换失败后残留了文件：{leftovers}"


# ---- 2. 路径边界 ----


def test_traversal_attempt_writes_nothing_outside_root(
    storage: LocalFileStorage, work_dir: Path
) -> None:
    """只报错不够，还要确认根目录之外**没有留下任何文件**。

    合约里已经断言了"非法键被拒绝"，这里补的是**副作用**：
    拒绝之前可能已经把文件写出去了一部分。
    """
    with pytest.raises(PermanentStorageError):
        storage.put("../escaped.txt", b"x", content_type="text/plain")

    assert not (work_dir / "escaped.txt").exists()


# ---- 3. 短期访问 ----


def test_presign_get_returns_relative_path(storage: LocalFileStorage) -> None:
    """本地实现的"签名"只是受控相对路径，不得是绝对路径。

    绝对路径等于把服务器目录结构暴露给调用端，
    并给出一个**绕过鉴权**的永久地址。
    """
    data = b"signed"
    key = key_for(data)
    storage.put(key, data, content_type="application/pdf")

    signed = storage.presign_get(key, expires_in=60)

    assert not signed.startswith("/"), "不得返回绝对路径"
    assert signed.endswith(".pdf")
    assert signed.startswith("objects/")


# ---- 4. 错误分类：决定"重试还是失败" ----


def test_permission_error_is_permanent(
    storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """权限不足是**确定性**错误：重试一万次，权限也不会自己出现。"""
    data = b"denied"
    key = key_for(data)

    def deny(*args: object, **kwargs: object) -> int:
        raise PermissionError("模拟无写权限")

    monkeypatch.setattr(Path, "write_bytes", deny)

    with pytest.raises(PermanentStorageError) as excinfo:
        storage.put(key, data, content_type="application/pdf")

    assert excinfo.value.code == ErrorCode.STORAGE_WRITE_DENIED


def test_stat_permission_error_maps_to_permanent(
    storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读取**已存在对象**时的权限异常也必须走统一错误体系。

    裸的 `PermissionError` 没有 `.code`，T4 的调度器无从分类；
    更糟的是它会被"其他 OSError 按瞬时处理"的分支吞掉，
    与适配器其他地方"权限不足属确定性错误"的语义自相矛盾 ——
    同一类问题在不同代码路径上被分成两类，是最难排查的那种不一致。
    """
    data = b"existing object"
    key = key_for(data)

    def raise_permission(*args: object, **kwargs: object) -> None:
        raise PermissionError("模拟无权限")

    # 让 is_file() 返回 True 进入"幂等校验"分支，再让 stat() 失败
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(Path, "stat", raise_permission)

    with pytest.raises(PermanentStorageError) as excinfo:
        storage.put(key, data, content_type="application/pdf")

    assert excinfo.value.code == ErrorCode.STORAGE_WRITE_DENIED


def test_stat_os_error_maps_to_transient(
    storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """其他读取失败按瞬时处理（重试有界，代价可控）。"""
    data = b"existing object"
    key = key_for(data)

    def raise_os_error(*args: object, **kwargs: object) -> None:
        raise OSError(5, "模拟 IO 错误")

    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(Path, "stat", raise_os_error)

    with pytest.raises(TransientStorageError) as excinfo:
        storage.put(key, data, content_type="application/pdf")

    assert excinfo.value.code == ErrorCode.STORAGE_UNAVAILABLE


def test_unlink_failure_does_not_mask_original_error(
    storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清理失败不得顶掉真正的错误。

    在 `finally` 里抛出的异常会**顶替**正在传播的那个异常，
    于是排障时看到的变成一句莫名其妙的清理失败，
    而真正有价值的失败原因（这里是"替换失败"）彻底消失。
    """
    data = b"masking"
    key = key_for(data)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError(5, "模拟失败")

    monkeypatch.setattr(os, "replace", boom)
    monkeypatch.setattr(Path, "unlink", boom)

    with pytest.raises(TransientStorageError) as excinfo:
        storage.put(key, data, content_type="application/pdf")

    # 拿到的是**替换失败**的分类，而不是清理失败造成的混乱
    assert excinfo.value.code == ErrorCode.STORAGE_UNAVAILABLE


def test_other_os_error_is_transient(
    storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """其他 IO 问题按**瞬时**处理。

    反过来做的代价更大：一次磁盘 IO 抖动就会把任务打成永久失败。
    而若真是配额或磁盘满，重试会再次失败并最终退化为 blocked，不会静默成功。
    """
    data = b"io error"
    key = key_for(data)

    def fail(*args: object, **kwargs: object) -> int:
        raise OSError(5, "模拟 IO 错误")

    monkeypatch.setattr(Path, "write_bytes", fail)

    with pytest.raises(TransientStorageError) as excinfo:
        storage.put(key, data, content_type="application/pdf")

    assert excinfo.value.code == ErrorCode.STORAGE_UNAVAILABLE
    assert excinfo.value.retryable is True


def test_storage_satisfies_port(storage: LocalFileStorage) -> None:
    """实现必须满足端口（`runtime_checkable` 只能验方法名，语义由合约验）。"""
    assert isinstance(storage, ObjectStorage)


# ---- 5. 不变量：不得漏出裸的文件系统异常 ----


@pytest.mark.parametrize(
    "fs_error",
    [PermissionError(13, "模拟权限不足"), OSError(5, "模拟 IO 错误")],
    ids=["权限不足", "其他 IO 错误"],
)
def test_no_raw_filesystem_exception_escapes_the_adapter(
    storage: LocalFileStorage,
    fs_error: OSError,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**不变量**：任何公开方法都不得漏出裸的文件系统异常。

    ## 这条测试的来历

    `Path.is_file()` **只吞"不存在"类错误**（`ENOENT` / `ENOTDIR` /
    `EBADF` / `ELOOP`），**权限错误会原样抛出**。这与直觉相反，
    而当时源码里恰好写着一段相反的注释（"内部会吞掉 `OSError`"）——
    错误注释让人停止怀疑，于是三处存在性检查都漏了裸的 `PermissionError`：

    ```text
    exists(key)            → PermissionError（code=None）
    put(key, data)         → PermissionError（code=None）
    presign_get(key, 60)   → PermissionError（code=None）
    ```

    裸异常没有 `.code`，调用方（M4 的 Worker）只能退化成 `except Exception`，
    等于把"重试还是立即阻塞"的分类责任丢回给调用方。

    ## 为什么穷举每个方法而不是挑代表

    这类漏洞的特点是"有分支、漏一个"：
    权限错误只在**读**路径上出现，只看 `put` 的权限分支永远发现不了
    `exists` 与 `presign_get` 也漏。抽查发现不了这种形状的问题。
    """
    data = b"invariant probe"
    key = key_for(data)
    storage.put(key, data, content_type="application/pdf")

    def patched_stat(*args: object, **kwargs: object) -> object:
        raise fs_error

    monkeypatch.setattr(Path, "stat", patched_stat)

    calls = {
        "exists": lambda: storage.exists(key),
        "get": lambda: storage.get(key),
        "put": lambda: storage.put(key, data, content_type="application/pdf"),
        "presign_get": lambda: storage.presign_get(key, expires_in=60),
    }

    for name, call in calls.items():
        try:
            call()
        except AppError as exc:
            assert isinstance(exc.code, ErrorCode), (
                f"{name} 抛出的异常没有稳定 error_code"
            )
        except Exception as exc:  # noqa: BLE001 - 这里正是在检查异常类型
            raise AssertionError(
                f"{name} 漏出了裸异常 {type(exc).__name__}：{exc}"
            ) from exc


def test_existence_check_raises_instead_of_answering_false(
    storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**存在性无法确定时必须抛错，而不是返回 False。**

    "读不出来"若被报成"不存在"，调用方会据此以为对象没写过而重新下载，
    把权限问题掩盖成一次正常的重复下载 —— 状态与事实不符，
    而且掩盖的恰好是最需要人介入的那类环境问题。
    """
    data = b"cannot tell"
    key = key_for(data)
    storage.put(key, data, content_type="application/pdf")

    def deny(*args: object, **kwargs: object) -> object:
        raise PermissionError(13, "模拟权限不足")

    monkeypatch.setattr(Path, "stat", deny)

    with pytest.raises(PermanentStorageError) as excinfo:
        storage.exists(key)

    assert excinfo.value.code == ErrorCode.STORAGE_WRITE_DENIED
    assert excinfo.value.retryable is False
