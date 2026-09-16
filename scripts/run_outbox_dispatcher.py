#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Outbox 派发器启动入口（**组合根**）。

与 `scripts/run_worker.py` 同一约定：派发器接收**已注入的网关**，
接线（数据库在哪、用哪个审批系统实现）只在这一处发生。
服务层（`app/outbox.py`）只依赖 `ApprovalCommentGateway` 端口，
不导入具体适配器 —— 依赖方向不能反过来。

    .venv/Scripts/python.exe scripts/run_outbox_dispatcher.py \
        --poll-interval 1 --max-iterations 10
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

# 直接以 `python scripts/run_outbox_dispatcher.py` 运行时，sys.path[0] 是
# scripts/，因此显式加入项目根目录（与 run_worker.py 同一约定）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.approval.mock_approval_gateway import MockApprovalGateway  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.outbox import DEFAULT_POLL_INTERVAL, OutboxDispatcher  # noqa: E402


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Outbox 派发器（组合根）：把回写意图送达审批系统。"
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="空闲时的轮询间隔（秒）",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="跑多少轮就退出（不填则一直跑）。用于冒烟与演练",
    )
    parser.add_argument(
        "--dispatcher-id",
        default=None,
        help="租约持有者标识；不填则自动生成（带随机后缀）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    dispatcher = OutboxDispatcher(
        SessionLocal,
        MockApprovalGateway(),
        dispatcher_id=args.dispatcher_id,
        poll_interval=args.poll_interval,
    )

    print(
        f"[outbox] 派发器启动（id={dispatcher.dispatcher_id}，"
        f"poll_interval={args.poll_interval}s）",
        flush=True,
    )

    # M9 Task 7：SIGTERM 优雅停机 —— 停止领取新事件，当前送达跑完后退出
    import signal

    def _graceful(signum, frame):  # noqa: ARG001 - 信号处理签名
        print(f"\n[outbox] 收到信号 {signum}，停止领取新事件…", flush=True)
        dispatcher.request_stop()

    signal.signal(signal.SIGTERM, _graceful)

    try:
        dispatcher.run_forever(max_iterations=args.max_iterations)
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        print("\n[outbox] 收到中断，退出", flush=True)
    print("[outbox] 已退出", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
