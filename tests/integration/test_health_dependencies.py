"""依赖健康明细与优雅停机（M9 Task 7）。"""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from app.main import app
from app.outbox import OutboxDispatcher
from app.worker import Worker

CLIENT = TestClient(app)


def test_health_dependencies_lists_database_and_is_sanitized() -> None:
    response = CLIENT.get("/health/dependencies")

    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["database"]["ok"] is True
    assert isinstance(body["checks"]["database"]["latency_ms"], float)

    # ⚠️ 脱敏纪律：连接串 / 主机 / 密钥绝不出现在响应里
    raw = response.text.lower()
    for forbidden in ("sqlite", "postgres://", "password", "secret", "@"):
        assert forbidden not in raw, f"响应里出现了敏感片段：{forbidden}"


def test_health_dependencies_omits_unconfigured_dependencies() -> None:
    """未配置的依赖不出现在清单里（缺席 ≠ 不健康，而是"这个形态没有它"）。"""
    response = CLIENT.get("/health/dependencies")

    body = response.json()
    # 测试环境默认 local 存储 + 无 Redis
    assert "redis" not in body["checks"]
    assert "minio" not in body["checks"]


def test_worker_stops_within_one_poll_interval(work_dir) -> None:
    """SIGTERM 语义：request_stop 后 Worker 在一个轮询间隔内退出。

    真信号（SIGTERM）在 Windows 测试进程里不可移植；这里直接驱动
    `request_stop()` —— run_worker.py 的信号处理只是调用它，语义相同。
    ⚠️ 给真的会话工厂：空转期间 Worker 也会领取（查库），lambda: None 会让
    后台线程抛 AttributeError（测试虽然过，但线程异常是污染）。
    """
    import sqlite3
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.config import PROJECT_ROOT

    path = work_dir / "worker-stop.db"
    conn = sqlite3.connect(path)
    conn.executescript((PROJECT_ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    factory = sessionmaker(bind=engine, future=True)

    worker = Worker(
        session_factory=factory,
        handler=lambda run: None,
        poll_interval=0.05,
    )
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()

    time.sleep(0.2)  # 让它至少空转几轮
    worker.request_stop()
    thread.join(timeout=1.0)

    assert not thread.is_alive(), "停机请求后必须在一个轮询间隔内退出"
    engine.dispose()


def test_outbox_dispatcher_stops_within_one_poll_interval(work_dir) -> None:
    """派发器同样支持优雅停机（M9 补齐：此前它是无限 while + sleep）。"""
    import sqlite3

    from app.adapters.approval.mock_approval_gateway import MockApprovalGateway
    from app.config import PROJECT_ROOT

    path = work_dir / "outbox-stop.db"
    conn = sqlite3.connect(path)
    conn.executescript((PROJECT_ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    conn.close()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    factory = sessionmaker(bind=engine, future=True)

    dispatcher = OutboxDispatcher(
        factory, MockApprovalGateway(), dispatcher_id="stop-test", poll_interval=0.05
    )
    thread = threading.Thread(target=dispatcher.run_forever, daemon=True)
    thread.start()

    time.sleep(0.2)
    dispatcher.request_stop()
    thread.join(timeout=1.0)

    assert not thread.is_alive(), "停机请求后必须在一个轮询间隔内退出"
    engine.dispose()
