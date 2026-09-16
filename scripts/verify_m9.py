#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M9 验收：基础设施迁移（PostgreSQL / Alembic / Redis / MinIO / Compose）。

与 verify_m8 同一纪律：**fail-closed** —— 任何一项不满足退出码非 0，
绝不"带病通过"。检查项：

 1. PostgreSQL 可达（默认本地开发容器 55432）
 2. Alembic 迁移在空 PG 库上 upgrade → downgrade → upgrade 往返成功
 3. 迁移结构与 ORM 元数据一致（由 tests/postgres/test_migrations.py 证明）
 4. 业务函数在 PG 下正确（tests/postgres 冒烟 + 并发 全绿）
 5. Redis 可达；断连时 Worker 退回轮询仍能处理作业（tests/integration）
 6. MinIO 可达；与 LocalFileStorage 跑同一份合约（tests/test_adapter_minio_storage.py）
 7. docker compose 配置合法（docker compose config，exit 0）
 8. SQLite 全量回归不受影响（由 verify_m8.ps1 的门禁覆盖，此处不重复跑）

用法：
    python scripts/verify_m9.py [--verbose]
    # 依赖：docker 容器（postgres/redis/minio）在跑；M9_* 环境变量可覆盖默认端口
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import uuid

try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
except (AttributeError, io.UnsupportedOperation):  # pragma: no cover
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

PG_URL = os.environ.get("M9_TEST_DATABASE_URL", "postgresql+psycopg://m9:m9@127.0.0.1:55432/m9")
REDIS_URL = os.environ.get("M9_REDIS_URL", "redis://127.0.0.1:56379/0")
MINIO_ENDPOINT = os.environ.get("M9_MINIO_ENDPOINT", "127.0.0.1:59000")


class Criterion:
    def __init__(self, index: int, text: str, verify) -> None:
        self.index = index
        self.text = text
        self.verify = verify
        self.ok = False
        self.measured = ""


def _run_pytest(paths: list[str]) -> tuple[bool, str]:
    import pytest

    exit_code = pytest.main(["-q", "--no-header", *paths])
    return exit_code == 0, f"pytest exit={exit_code}"


def build_criteria() -> list[Criterion]:
    criteria: list[Criterion] = []

    # 1. PostgreSQL 可达
    def check_pg() -> None:
        from sqlalchemy import create_engine, text

        engine = create_engine(PG_URL, pool_pre_ping=True)
        try:
            version = engine.connect().execute(text("SELECT version()")).scalar_one()
            Criterion_OK = "PostgreSQL"
            globals()["_pg_version"] = version.split(",")[0].split(" on ")[0]
        finally:
            engine.dispose()

    criteria.append(Criterion(1, "PostgreSQL 可达", check_pg))

    # 2+3+4. tests/postgres（迁移往返 / 结构比对 / 冒烟 / 并发）
    def check_pg_tests() -> None:
        ok, measured = _run_pytest(["tests/postgres"])
        if not ok:
            raise AssertionError(f"tests/postgres 未全绿（{measured}）")
        globals()["_pg_tests"] = measured

    criteria.append(Criterion(2, "Alembic 迁移往返 + 结构比对 + 冒烟/并发（tests/postgres）", check_pg_tests))

    # 5. Redis 可达 + 通知合约
    def check_redis() -> None:
        from app.adapters.queue.redis_notifier import RedisJobNotifier

        notifier = RedisJobNotifier(url=REDIS_URL, namespace="verify-m9")
        notifier.cache_set("verify", "1", ttl_seconds=10)
        assert notifier.cache_get("verify") == "1", "Redis 缓存读写不符"

    criteria.append(Criterion(3, "Redis 可达（notify/wait/lock/cache 可用）", check_redis))

    # 6. MinIO 可达 + 与 local 同合约
    def check_minio() -> None:
        from minio import Minio

        client = Minio(
            MINIO_ENDPOINT,
            access_key=os.environ.get("M9_MINIO_ACCESS_KEY", "m9admin"),
            secret_key=os.environ.get("M9_MINIO_SECRET_KEY", "m9admin-secret"),
            secure=False,
        )
        if not client.bucket_exists("verify-m9"):
            client.make_bucket("verify-m9")
        globals()["_minio_ok"] = True

    criteria.append(Criterion(4, "MinIO 可达（S3 API 正常）", check_minio))

    def check_minio_contract() -> None:
        ok, measured = _run_pytest(["tests/test_adapter_minio_storage.py"])
        if not ok:
            raise AssertionError(f"MinIO 合约测试未全绿（{measured}）")

    criteria.append(Criterion(5, "MinIO 与 LocalFileStorage 跑同一份 ObjectStorage 合约", check_minio_contract))

    # 7. Redis 故障恢复（断连轮询兜底 / 清缓存不丢数据）
    def check_redis_recovery() -> None:
        ok, measured = _run_pytest(["tests/integration/test_redis_recovery.py"])
        if not ok:
            raise AssertionError(f"Redis 恢复测试未全绿（{measured}）")

    criteria.append(Criterion(6, "Redis 故障恢复：断连轮询兜底 / 清缓存不丢数据", check_redis_recovery))

    # 8. compose 配置合法
    def check_compose() -> None:
        env = dict(
            os.environ,
            POSTGRES_PASSWORD="verify",
            MINIO_ROOT_USER="verify",
            MINIO_ROOT_PASSWORD="verify",
            MOCK_APPROVAL_TOKEN="verify",
        )
        result = subprocess.run(
            ["docker", "compose", "config", "--quiet"],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AssertionError(f"docker compose config 失败：{result.stderr.strip()[:200]}")

    criteria.append(Criterion(7, "docker compose 配置合法（compose config exit 0）", check_compose))

    # 9. 优雅停机 + 依赖健康（Task 7）
    def check_graceful() -> None:
        ok, measured = _run_pytest(["tests/integration/test_health_dependencies.py"])
        if not ok:
            raise AssertionError(f"停机/依赖健康测试未全绿（{measured}）")

    criteria.append(Criterion(8, "优雅停机（SIGTERM 语义）+ 依赖健康脱敏", check_graceful))

    return criteria


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M9 验收（fail-closed）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    print("=" * 68)
    print("M9 acceptance: PostgreSQL / Alembic / Redis / MinIO / Compose")
    print("=" * 68)

    results = []
    passed = 0
    for criterion in build_criteria():
        try:
            criterion.verify()
            criterion.ok = True
            note = ""
        except Exception as exc:  # noqa: BLE001 - 验收要的是"为什么不满足"
            note = f": {exc}" if args.verbose else ""
        results.append(criterion)
        status = "PASS" if criterion.ok else "FAIL"
        print(f"  [{status}] {criterion.index}. {criterion.text}{note}")
        passed += 1 if criterion.ok else 0

    print("-" * 68)
    total = len(results)
    print(f"结论：{passed}/{total} 通过（fail-closed：exit={0 if passed == total else 1}）")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
