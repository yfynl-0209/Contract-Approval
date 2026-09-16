"""等待持久依赖就绪（compose `migrate` / 首次部署前置，M9 Task 7）。

计划纪律：**有界的启动重试，不用固定 sleep** —— 逐个探测、失败退避，
超时仍不就绪则退出码非 0，让编排系统明确失败而不是带着坏依赖继续跑。

检查项（按部署形态）：
- PostgreSQL：`SELECT 1`（**必需** —— 数据库是不可替代的持久依赖）；
- Redis / MinIO：仅当对应配置存在时检查（缺失配置 = 该形态不用它，不算失败）。

用法：
    python scripts/wait_for_dependencies.py --timeout 60 --interval 2
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence


def _probes() -> list[tuple[str, bool, "Sequence[None]"]]:
    """返回 (名称, 必需?, 探测函数)。函数不可达时抛异常。"""
    from app.config import settings
    from app.db import engine
    from sqlalchemy import text

    items: list[tuple[str, bool, object]] = []

    def _db() -> None:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    items.append(("postgresql", True, _db))

    if settings.redis_url:
        def _redis() -> None:
            from app.adapters.queue.redis_notifier import RedisJobNotifier

            notifier = RedisJobNotifier(
                url=settings.redis_url,
                namespace=f"{settings.env}:{settings.tenant_id}",
            )
            notifier.cache_set("wait-probe", "1", ttl_seconds=5)
            notifier.cache_get("wait-probe")

        items.append(("redis", False, _redis))

    if settings.storage_backend == "minio":
        def _minio() -> None:
            from app.adapters.storage import build_storage

            build_storage().exists("sha256/wait-probe-nonexistent")

        items.append(("minio", False, _minio))

    return items  # type: ignore[return-value]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="等待持久依赖就绪（有界重试）")
    parser.add_argument("--timeout", type=float, default=60.0, help="总超时（秒）")
    parser.add_argument("--interval", type=float, default=2.0, help="重试间隔（秒）")
    args = parser.parse_args(argv)

    checks = _probes()
    required = [(name, fn) for name, required, fn in checks if required]
    optional = [(name, fn) for name, required, fn in checks if not required]
    print(
        f"[wait] 检查 {len(required)} 个必需依赖"
        f"{f' + {len(optional)} 个可选' if optional else ''}",
        flush=True,
    )

    deadline = time.monotonic() + args.timeout
    pending = list(required)
    while True:
        still: list[tuple[str, object]] = []
        for name, fn in pending:
            try:
                fn()
                print(f"[wait] {name}: 就绪", flush=True)
            except Exception as exc:  # noqa: BLE001 - 探测失败重试，不暴露细节
                still.append((name, fn))
                print(f"[wait] {name}: 未就绪（{type(exc).__name__}），重试…", flush=True)
        pending = still
        if not pending:
            break
        if time.monotonic() > deadline:
            names = ", ".join(name for name, _ in pending)
            print(f"[wait] 超时：必需依赖 {names} 在 {args.timeout}s 内未就绪", flush=True)
            return 1
        time.sleep(args.interval)

    for name, fn in optional:
        try:
            fn()
            print(f"[wait] {name}: 可用", flush=True)
        except Exception:  # noqa: BLE001 - 可选依赖缺席只提醒，不失败
            print(f"[wait] {name}: 不可用（可选，继续）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
