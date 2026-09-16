#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Worker 启动入口（**组合根**）。

## 为什么必须有一个显式的组装根

`Worker` 接收的是一个**已注入的处理器**（`Callable[[JobRun], None]`），
它自己不知道数据库在哪、对象存储是哪个实现、每种作业该调谁。
这些"谁知道谁"的接线只有一处应该知道：**组合根**。

放在这里而不是 `app/services/` 里，是因为服务层导入具体适配器会
把依赖方向反过来（`services → adapters`），而那正是本项目
"服务层不依赖实现"这条约定要防的。

## 已接线的作业类型

`.venv/Scripts/python.exe scripts/run_worker.py`

| 类型 | 处理器 | 何时用得上 |
| --- | --- | --- |
| `parse` | `execute_parse_job` | 工具 4 入队后 |
| `rule` | `run_rule_job` | 工具 5 入队后 |
| `result` | `save_review_result` | 工具 6 的**异步 / 重试**路径 |
| `writeback` | `request_writeback` | 工具 7 的**异步 / 重试**路径 |

⚠️ `result` / `writeback` 在演示路径上是**同步**完成的（工具 6/7 直接调服务，
见企业化设计 §6 的同步/异步分界）。这里仍然给它们接上处理器，理由是
**失败恢复**：一次保存或回写如果被登记成了作业（或需要重试），
它必须能被 Worker 执行 —— 否则作业会以"未注册处理器"定性失败，
而那个错误码指向的是**接线**，不是真正的故障。排障方向就此被带偏。

## ⚠️ 未注册的作业类型 **必须失败**，不得静默完成

处理器遇到没有分支的作业类型时抛错。若改成"什么都不做、直接返回"，
`complete_job` 会把作业标成 `succeeded` ——

```text
作业成功、result_ref 指向一个空批次、结论全无
```

这正是本轮在 P0② 修掉的那个缺陷的另一种形态：**让"没做"看起来像"做完了"。**
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from functools import partial
from pathlib import Path

# 直接以 `python scripts/run_worker.py` 运行时，sys.path[0] 是 scripts/，
# 因此显式加入项目根目录（与 check_rules.py / init_db.py 同一约定）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.storage import build_storage  # noqa: E402
from app.composition.llm_pipeline import build_llm_gateway, build_llm_judge  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.enums import ErrorCode, JobType  # noqa: E402
from app.errors import PermanentError  # noqa: E402
from app.models import ReviewRun, WorkflowJob  # noqa: E402
from app.ports.llm_gateway import NONE_MODEL_ID, model_version_of  # noqa: E402
from app.ports.object_storage import ObjectStorage  # noqa: E402
from app.rules.evaluator import LlmJudge  # noqa: E402
from app.services.rule_service import run_rule_job  # noqa: E402
from app.worker import Handler, JobRun, Worker  # noqa: E402

#: 默认领取**全部**四种已接线的作业类型。
#: ⚠️ 每一种都必须有处理器（见模块 docstring）；缺一个就会以
#: "未注册处理器"失败，而那个错误码指向的是接线、不是真正的故障。
DEFAULT_JOB_TYPES: tuple[str, ...] = (
    JobType.PARSE.value,
    JobType.RULE.value,
    JobType.RESULT.value,
    JobType.WRITEBACK.value,
)


def _job_input(run: JobRun) -> dict:
    """取作业输入。

    ⚠️ 从**库里的行**读，而不是从 `ClaimedJob` 上找：作业输入是
    `workflow_jobs.input_json` 这一列的内容，而"领取上下文里带不带它"
    是实现细节。读行只依赖一个稳定的东西 —— 表结构。
    """
    row = run.session.get(WorkflowJob, run.job.job_id)
    if row is None:  # pragma: no cover - 领取刚发生，行不可能不在
        raise PermanentError(
            f"作业 {run.job.job_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )
    payload = json.loads(row.input_json or "{}")
    return payload if isinstance(payload, dict) else {}


def _ensure_model_matches_run(
    session: Session, *, run_id: int, current_model_version: str
) -> None:
    """批次的 `model_version` 与实际可用的模型**必须一致**（M11 Task 4）。

    批次的模型版本在**入队时冻结**（M5 决策 ①）。执行时不一致意味着两种
    不可能同时为真的说法之一：批次声称用了 `qwen-plus` 而实际走了确定性
    fallback，或者反过来。两者都会让 `review_runs.model_version` 失去意义
    —— 而那一列正是统计"模型答得怎么样"的分组键。

    ⚠️ **不得**改成"按当前配置继续跑"：那会让同一份输入在两次执行之间
    得出不同的结论，而报告上两批都写着同一个 `run_id`。
    """
    row = session.get(ReviewRun, run_id)
    if row is None:
        raise PermanentError(
            f"批次 {run_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )
    if row.model_version != current_model_version:
        raise PermanentError(
            f"批次 {run_id} 声明的模型是 {row.model_version!r}，"
            f"而当前执行环境提供的模型是 {current_model_version!r} —— "
            "无法按批次声明的模型执行，也不会改用另一个模型重算"
            "（请以同一配置重新入队，而不是复用旧批次）",
            code=ErrorCode.UNEXPECTED_ERROR,
        )


def make_handler(
    storage: ObjectStorage,
    allowed_types: tuple[str, ...] = (),
    *,
    llm_judge: LlmJudge | None = None,
    model_version: str = NONE_MODEL_ID,
) -> Handler:
    """按 `job_type` 分派的处理器。**未注册的类型抛错**（见模块 docstring）。

    M11：
    - `llm_judge`：`llm` 规则的判定钩子（`None` = 没接模型 = 纯规则模式）；
    - `model_version`：当前执行环境的模型标识（`model_version_of(gateway)`）——
      RULE 分支用它做"批次声明 vs 实际执行"的一致性校验（见下）。
    """

    def handler(run: JobRun) -> None:
        job_type = run.job.job_type

        if job_type == JobType.RULE.value:
            run_id = _job_input(run).get("run_id")
            if not isinstance(run_id, int):
                raise PermanentError(
                    f"RULE 作业 {run.job.job_id} 的输入缺少 run_id —— "
                    "作业必须指向入队时建好的那个批次，不能在做作业时才决定",
                    code=ErrorCode.UNEXPECTED_ERROR,
                )
            # 批次声明的模型与实际可用的模型必须一致（M11 Task 4）——
            # 不一致就显式失败，绝不静默换 fallback 重算（那会让
            # `review_runs.model_version` 变成一句谎话，而库里两处都不报错）。
            _ensure_model_matches_run(
                run.session, run_id=run_id, current_model_version=model_version
            )
            # 批次与规则集都在入队时冻结，这里只负责执行它（见 run_rule_job）
            run_rule_job(
                run.session,
                run_id=run_id,
                storage=storage,
                llm_judge=llm_judge,
            )
            return

        if job_type == JobType.PARSE.value:
            from app.composition.parse_pipeline import build_standard_document
            from app.services.parse_service import execute_parse_job
            from app.workflow.job_inputs import ParseJobInput

            payload = _job_input(run)
            # ⚠️ 未知键在模型层被拒（extra=forbid 的 StrictModel）——
            # "作业声明了什么"与"执行用什么"必须是同一份冻结输入。
            job_input = ParseJobInput.model_validate(payload)
            document_factory = partial(
                build_standard_document,
                options=job_input.parse_options,
                content_type=job_input.content_type,
            )
            execute_parse_job(
                run.session,
                job_input=job_input,
                storage=storage,
                document_factory=document_factory,
                allowed_types=allowed_types,
            )
            return

        if job_type == JobType.RESULT.value:
            from app.services.result_service import save_review_result
            from app.workflow.job_inputs import ResultJobInput

            # ⚠️ 未知键在模型层被拒（`extra=forbid`）—— 与 PARSE 同一约定：
            # "作业声明了什么"与"执行用什么"必须是同一份冻结输入。
            job_input = ResultJobInput.model_validate(_job_input(run))
            # `actor` 取自**冻结的作业输入**（入队时由 API 层的身份依赖写入），
            # 不是此刻 worker 进程的身份 —— 作业是过去那一刻的意图，
            # 审计要记的是**当时**是谁发起的。
            save_review_result(
                run.session,
                run_id=job_input.run_id,
                overall_risk_level=job_input.overall_risk_level,
                summary_text=job_input.summary_text,
                focus_points_json=list(job_input.focus_points_json),
                comment_text=job_input.comment_text,
                actor=job_input.actor.to_actor(),
            )
            return

        if job_type == JobType.WRITEBACK.value:
            from app.services.writeback_service import request_writeback
            from app.workflow.job_inputs import WritebackJobInput

            job_input = WritebackJobInput.model_validate(_job_input(run))
            # `request_writeback` 自己会写 `comment_logs` + `outbox_events` + 审计；
            # 也就是说**回写作业不直接调外部系统** —— 它只是把意图落到事务里，
            # 真正的送达由 Outbox 派发器负责。在这里顺手调一次外部系统，
            # 等于绕开 Outbox 那条"意图与业务状态同生共死"的保证。
            request_writeback(
                run.session,
                instance_id=job_input.instance_id,
                result_id=job_input.result_id,
                actor=job_input.actor.to_actor(),
            )
            return

        raise PermanentError(
            f"作业类型 {job_type} 没有注册处理器 —— 拒绝以'什么都不做'的方式完成它"
            f"（那会把作业标成 succeeded，而结果为空）",
            code=ErrorCode.UNEXPECTED_ERROR,
        )

    return handler


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "后台作业 Worker（组合根）。"
            f"默认领取全部已接线的类型：{', '.join(DEFAULT_JOB_TYPES)}。"
        )
    )
    parser.add_argument(
        "--job-types",
        nargs="+",
        default=list(DEFAULT_JOB_TYPES),
        choices=[item.value for item in JobType],
        help=(
            f"领取哪些类型的作业（默认 {' '.join(DEFAULT_JOB_TYPES)}）。"
            "⚠️ 领取**没有处理器**的类型会以'未注册处理器'失败 —— "
            "那是刻意的（见模块 docstring），不是 bug"
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="空闲时的轮询间隔（秒）",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="跑多少轮就退出（不填则一直跑）。用于冒烟与演练",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help="租约持有者标识；不填则自动生成（带随机后缀）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    # M9：装配作业唤醒（REDIS_URL 未配置 → 纯轮询兜底）
    from app.composition.job_queue import (
        build_job_notifier_from_settings,
        set_job_notifier,
    )

    set_job_notifier(build_job_notifier_from_settings())

    # ⚠️ `allowed_types` 必须显式传：`make_handler` 的默认空元组在
    # `execute_parse_job` 里表示"不复核类型"，但曾经聚合侧把空集当成
    # "没有任何允许的类型"，解析成功后任务被误判 blocked。
    # 聚合侧已修（与门禁同一语义），这里仍然显式传配置 —— 让生产入口
    # 的行为只取决于 `.env`，不取决于两层函数对"空"的默契。
    allowed_types = tuple(
        item.strip()
        for item in settings.attachment_allowed_types.split(",")
        if item.strip()
    )
    # M11：判定钩子与模型标识出自同一个组合根 —— 两边分叉时，批次会
    # 声明一个它并没有使用的模型。打印的是 model_version（配置快照），不是 Key。
    gateway = build_llm_gateway(settings)
    llm_judge = build_llm_judge(settings)
    print(f"[worker] RULE 作业使用的模型：{model_version_of(gateway)}", flush=True)

    worker = Worker(
        SessionLocal,
        make_handler(
            build_storage(),
            allowed_types=allowed_types,
            llm_judge=llm_judge,
            model_version=model_version_of(gateway),
        ),
        worker_id=args.worker_id,
        job_types=[JobType(value) for value in args.job_types],
        poll_interval=args.poll_interval,
    )

    print(
        f"[worker] 启动（id={worker.worker_id}，领取类型={args.job_types}）",
        flush=True,
    )

    # M9 Task 7：SIGTERM 优雅停机 —— 收到信号**停止领取新作业**，
    # 当前作业跑完（含续租与提交）后在一个轮询间隔内退出。
    import signal

    def _graceful(signum, frame):  # noqa: ARG001 - 信号处理签名
        print(f"\n[worker] 收到信号 {signum}，停止领取新作业…", flush=True)
        worker.request_stop()

    signal.signal(signal.SIGTERM, _graceful)

    try:
        worker.run_forever(max_iterations=args.max_iterations)
    except KeyboardInterrupt:  # pragma: no cover - 交互式中断
        print("\n[worker] 收到中断，退出", flush=True)
    print("[worker] 已退出", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
