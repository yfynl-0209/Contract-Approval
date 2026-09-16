"""MinIO 适配器跑 `ObjectStorageContract`（M9 Task 4）。

与 `LocalFileStorage` 跑**同一套合约** —— "能不能互相替换"由这里回答。
MinIO 不可达时整组跳过（本地开发默认 backend=local，不受影响）；
`verify_m9.py` 的验收则要求 MinIO 必须可达。
"""

from __future__ import annotations

import os
import uuid

import pytest
from minio import Minio

from app.adapters.storage.minio_storage import MinioStorage
from app.ports.object_storage import ObjectStorage
from tests.contract.storage_contract import ObjectStorageContract

MINIO_ENDPOINT = os.environ.get("M9_MINIO_ENDPOINT", "127.0.0.1:59000")
MINIO_ACCESS_KEY = os.environ.get("M9_MINIO_ACCESS_KEY", "m9admin")
MINIO_SECRET_KEY = os.environ.get("M9_MINIO_SECRET_KEY", "m9admin-secret")


def _minio_available() -> bool:
    try:
        client = Minio(
            MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            secure=False,
        )
        client.bucket_exists("probe-nonexistent")
        return True
    except Exception:  # noqa: BLE001 - 探测失败 = 容器没起，跳过
        return False


class TestMinioStorageContract(ObjectStorageContract):
    """MinIO 实现的合约验证（每条测试独享一个桶）。"""

    @pytest.fixture()
    def contract_storage(self) -> ObjectStorage:
        return MinioStorage(
            endpoint=MINIO_ENDPOINT,
            access_key=MINIO_ACCESS_KEY,
            secret_key=MINIO_SECRET_KEY,
            bucket=f"m9-test-{uuid.uuid4().hex[:8]}",
            secure=False,
        )

    def seed_raw_object(self, storage: ObjectStorage, key: str, data: bytes) -> None:
        """绕过 `put` 的摘要校验直接写 —— 用底层客户端直写。"""
        import io

        assert isinstance(storage, MinioStorage)
        storage._client.put_object(  # noqa: SLF001 - 测试探查钩子
            storage._bucket,  # noqa: SLF001
            key,
            io.BytesIO(data),
            length=len(data),
            content_type="application/pdf",
        )

    def stored_object_count(self, storage: ObjectStorage) -> int:
        assert isinstance(storage, MinioStorage)
        return sum(
            1 for _ in storage._client.list_objects(storage._bucket, recursive=True)  # noqa: SLF001
        )

    # MinIO 桶由夹具创建、名字唯一 —— 不清理（本地容器，随测试销毁）


@pytest.fixture(scope="module", autouse=True)
def _require_minio() -> None:
    if not _minio_available():
        pytest.skip(
            "MinIO 不可达（M9 开发容器未启动？端口 59000）——本地 local 后端不受影响"
        )
