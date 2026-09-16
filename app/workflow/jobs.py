"""作业台账 —— 幂等键构造、创建与状态流转。

## 两级闸门（最容易搞混的一点）

| 闸门 | 落点 | 回答的问题 |
| --- | --- | --- |
| **对象级** | `approval_tasks.UNIQUE(provider, tenant_id, instance_id)` | 同一审批单**只能有一条任务记录** |
| **操作级** | `workflow_jobs.UNIQUE(idempotency_key)` | 同一**输入版本**的同一操作**不得重复入队** |

⚠️ **不得把操作级闸门当成对象级闸门用**。审批表单与附件都会变化，
若幂等键只认审批单号，同一审批单的**第二次同步会被唯一约束永久拒绝**。
因此 `build_idempotency_key()` 强制要求"输入版本"参与构造，
拿不到版本时**退化为一次性键**：宁可多跑一次，也不能把对象卡死。

## M3 只写不消费

本模块只负责把作业**记进台账**（工具 1~3 同步执行完成）。
Worker 在 M4 引入后按 `job_status='queued'` / `retry_wait` 领取作业，
届时本模块的写入路径不需要任何改动。

**时间基准**：与状态机一致 —— naive UTC，对齐 SQLite 的 `CURRENT_TIMESTAMP`。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.context import get_correlation_id
from app.enums import ErrorCode, JobStatus, JobType, is_retryable
from app.errors import AppError, IdempotencyConflict
from app.models import WorkflowJob
from app.workflow.job_inputs import JobInputError, validate_job_input
from app.workflow.state_machine import utcnow

__all__ = [
    "JobInputError",
    "backoff_seconds",
    "build_idempotency_key",
    "create_job",
    "freeze_job_input",
    "mark_failed",
    "mark_running",
    "mark_succeeded",
    "pull_window_token",
    "request_fingerprint",
    "reset_for_retry",
    "utcnow",
]

#: 幂等键的字段分隔符。冒号不会出现在 provider / instance_id 中，
#: 因此 `job_type:identity:version` 三段可以无歧义地拆开。
_SEPARATOR = ":"

#: 指数退避的基数与上限（秒）
_BACKOFF_BASE = 2.0
_BACKOFF_CAP_SECONDS = 300.0


# ============================================================
# 幂等键
# ============================================================


def build_idempotency_key(
    job_type: JobType | str,
    identity: str,
    version: str | None = None,
) -> str:
    """构造作业幂等键：`{job_type}:{identity}:{version}`。

    `identity` 建议：`pull` → `{provider}:{tenant_id}`；`detail` → `{instance_id}`；
    `download` → `{instance_id}:{attachment_id}`。

    `version` 是**输入版本**，可取外部系统的 `updated_at`、表单版本号、附件 ETag，
    或 `request_fingerprint(payload)`。

    Raises:
        ValueError: `job_type` 不在受控枚举内，或 `identity` 为空白。

    ## `version=None` 的取舍 —— 本函数最重要的语义

    拿不到外部版本时，我们**无法**判断"这次调用与上次是否同一件事"。只有两个选择：

    1. 拒绝创建作业 → 功能整体不可用；
    2. 生成一次性键 → 允许后续同步，只是失去"重复入队"的合并能力。

    选 2：**宁可多跑一次，也不能把对象永久卡死** ——
    前者是可观测、可恢复的浪费，后者是静默的功能失效。
    """
    job_type_value = _as_job_type(job_type)

    identity_text = (identity or "").strip()
    if not identity_text:
        raise ValueError("作业的业务标识不能为空")

    version_text = (version or "").strip()
    if not version_text:
        # 一次性键：不同调用永不碰撞，因此"重复入队"不会被合并，
        # 但也绝不会因为复用了别人的键而互相阻塞
        version_text = f"oneshot-{uuid.uuid4().hex}"

    return _SEPARATOR.join((job_type_value.value, identity_text, version_text))


def pull_window_token(now: datetime | None = None, *, minutes: int = 1) -> str:
    """拉取作业的"输入版本"：**分钟级时间窗口**。

    拉取没有天然的外部版本号（它本身就是去拉别人的数据），因此用时间窗口当版本：
    同一窗口内的重复点击合流为一个作业（防双击、防重复触发）；
    跨窗口则是新的一次拉取（本来就该重新拉一遍）。

    Args:
        now: 基准时间（测试可注入）。缺省为当前 UTC。
        minutes: 窗口长度（分钟），必须为正。

    Raises:
        ValueError: `minutes` 不是正数。
    """
    if minutes <= 0:
        raise ValueError(f"窗口长度必须为正数，收到 {minutes}")

    moment = now or utcnow()
    window_start = moment.replace(
        minute=(moment.minute // minutes) * minutes, second=0, microsecond=0
    )
    return f"w{window_start.strftime('%Y%m%d%H%M')}"


def request_fingerprint(payload: Any) -> str:
    """对请求内容取稳定指纹（SHA-256 前 16 位十六进制）。

    用途：外部系统不提供版本号，但**响应内容本身**可以充当版本 ——
    内容相同即视为同一次操作，内容变了就允许产生新作业。

    `sort_keys=True` 保证字典顺序不影响指纹；取前 16 位是为了让幂等键保持可读，
    碰撞概率在此用途下可以忽略。
    """
    serialized = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def freeze_job_input(payload: Mapping[str, Any]) -> tuple[str, str]:
    """把作业输入冻结成 `(input_json, input_digest)`。

    ## 为什么两个值必须由同一个函数一起产生

    `input_digest` 是 `input_json` 的摘要。若两处各自序列化一次，
    就可能出现"落库的 JSON 与摘要对应的内容不是同一份" ——
    那时"这份结果基于什么输入"会有两个互不相同的答案，**且两个看起来都合法**。

    因此规定：**摘要只能由落库的那串文本算出**，不允许分别计算。

    Args:
        payload: 作业的不可变输入（结构按 `job_type` 各自定义，见设计文档 §4.5）。

    Returns:
        `(规范 JSON 文本, SHA-256 十六进制)`。

    Raises:
        ValueError: `payload` 为空。空输入等同于"没有输入"，
            会让这次作业永远无法回答"基于什么输入"。
    """
    if not payload:
        raise ValueError("作业输入不能为空")

    serialized = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ============================================================
# 创建与流转
# ============================================================


def _reuse_existing(
    existing: WorkflowJob,
    *,
    job_type: str,
    task_id: int | None,
    input_digest: str,
) -> WorkflowJob:
    """复用既有作业前**逐项比对**；不一致就报错，而不是静默复用。

    ⚠️ "命中幂等键"**不等于**"该复用"。键由调用方构造，构造得不对
    （漏掉租户、版本取错、直接传了一个常量）时，两份**不同**的输入会共用同一个键。

    此时静默返回既有作业的后果很具体：调用方以为自己的参数生效了，
    实际拿到的是**另一份输入的旧作业与旧数据**（例如另一个租户的附件），
    而库里没有任何一处看得出这件事 —— 作业状态正常、输入字段完整、
    唯一约束也没被违反。

    比对的正是"作业身份"的全部构成：`job_type` / `task_id` / `input_digest`。
    正常路径下三者必然相同，因此这条检查的代价是零。

    Raises:
        IdempotencyConflict: 键相同但作业身份不同（`app/api/errors.py` 映射为 409）。
    """
    if (
        existing.job_type == job_type
        and existing.task_id == task_id
        and existing.input_digest == input_digest
    ):
        return existing

    raise IdempotencyConflict(
        f"幂等键 {existing.idempotency_key!r} 已绑定到另一份输入："
        f"既有 (job_type={existing.job_type}, task_id={existing.task_id}, "
        f"input_digest={existing.input_digest[:12]}…)，"
        f"本次 (job_type={job_type}, task_id={task_id}, "
        f"input_digest={input_digest[:12]}…)。"
        f"同一个键只能对应同一个操作 —— 请检查键的构造是否漏掉了区分输入的字段",
        code=ErrorCode.IDEMPOTENCY_CONFLICT,
    )


def create_job(
    session: Session,
    *,
    job_type: JobType | str,
    idempotency_key: str,
    input_payload: Mapping[str, Any],
    task_id: int | None = None,
    max_attempts: int = 3,
) -> tuple[WorkflowJob, bool]:
    """创建作业；幂等键已存在时返回**既有作业**。

    Args:
        session: 数据库会话（调用方负责提交）。
        input_payload: **不可变输入**（必填）。重试沿用同一输入，
            它是"这份结果基于什么输入"的唯一答案 —— 因此不允许缺省。
        task_id: 所属任务；拉取作业不属于任何单个任务，可为 `None`。
        max_attempts: 最大尝试次数，必须 ≥ 1（数据库有 CHECK）。

    Returns:
        `(作业, 是否新建)`。`created=False` 表示命中幂等键、复用了既有作业。

    Raises:
        ValueError: 参数非法（属编程错误）。
        JobInputError: 输入不符合该作业类型的模型。
        IdempotencyConflict: 幂等键已绑定到**另一份输入**（不静默复用）。
    """
    job_type_value = _as_job_type(job_type)

    key = (idempotency_key or "").strip()
    if not key:
        raise ValueError("幂等键不能为空")
    if max_attempts < 1:
        # 数据库也有 CHECK。在这里先拦一道是为了给出**能看懂**的错误：
        # `max_attempts=0` 会让任何瞬时错误都无法重试，任务却静默地直接 blocked。
        raise ValueError(f"max_attempts 必须 ≥ 1，收到 {max_attempts}")

    # 输入先**校验再冻结**：即便下面命中幂等键、复用了既有作业，
    # "输入不合法"也该立刻暴露，而不是因为走了快路径就被跳过。
    # 冻结的是**校验后**的完整输入（含默认值）—— 让"这份结果基于什么输入"没有留白：
    # 不写默认值的话，读回时无法区分"当时用的是默认 DPI"与"当时没记录 DPI"。
    input_json, input_digest = freeze_job_input(
        validate_job_input(job_type_value, input_payload)
    )

    # 快路径：绝大多数重复调用在这里就返回了
    existing = _find_by_key(session, key)
    if existing is not None:
        return (
            _reuse_existing(
                existing,
                job_type=job_type_value.value,
                task_id=task_id,
                input_digest=input_digest,
            ),
            False,
        )

    job = WorkflowJob(
        task_id=task_id,
        job_type=job_type_value.value,
        idempotency_key=key,
        input_json=input_json,
        input_digest=input_digest,
        # 【关联 ID 落库】Worker 在**另一个进程**，contextvar 传不过去 ——
        # 请求侧的关联 ID 只能靠作业记录带过去，否则 Worker 的日志断了链。
        correlation_id=get_correlation_id(),
        job_status=JobStatus.QUEUED.value,
        attempt_no=0,
        max_attempts=max_attempts,
    )

    # 用 SAVEPOINT 隔开这次插入：并发下唯一约束冲突时只回滚这一步，
    # 不会把调用方在同一个事务里的其他改动一起丢掉。
    try:
        with session.begin_nested():
            session.add(job)
            session.flush()
    except IntegrityError:
        # 另一个进程抢先建了同一个幂等键 —— 唯一约束是最后防线，
        # 此处回退为"使用既有作业"，与快路径结果一致。
        session.expire_all()
        existing = _find_by_key(session, key)
        if existing is None:
            raise
        # 并发路径同样要比对 —— 唯一约束只保证"键不重复"，
        # 不保证"键正确"。两条路径漏掉任何一条，缺陷就会从另一条漏过去。
        return (
            _reuse_existing(
                existing,
                job_type=job_type_value.value,
                task_id=task_id,
                input_digest=input_digest,
            ),
            False,
        )

    return job, True


def mark_running(session: Session, job: WorkflowJob, *, now: datetime | None = None) -> None:
    """置为运行中，并**在开始执行时**递增尝试次数。

    `attempt_no` 的准确定义是**"自上次成功以来消耗掉的尝试次数"**，
    不是"执行过的总次数"。

    为什么必须在开始时递增：这样"正在跑"的作业也持有准确的尝试次数。
    若改成失败时递增，一个执行到一半进程崩溃的作业重启后会看起来像"从未尝试过"，
    于是它能无限重试 —— 而 `max_attempts` 正是为兜住这种情况。
    """
    job.job_status = JobStatus.RUNNING.value
    job.attempt_no = (job.attempt_no or 0) + 1
    job.started_at = now or utcnow()
    job.next_retry_at = None


def mark_succeeded(
    session: Session,
    job: WorkflowJob,
    *,
    checkpoint: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """置为成功，并**重置尝试计数**、留存检查点。

    ⚠️ 必须重置 `attempt_no`，否则会出真实错误：同一时间窗口内的拉取被触发 3 次
    且都成功，`attempt_no` 变成 3；随后**第一次**瞬时失败就撞上 `max_attempts=3`，
    被判定为"重试耗尽"直接失败 —— 重试预算被成功的执行吃光了。

    检查点用于"从失败位置恢复"（§7.4）：例如解析到第 7 页失败，重试时从第 7 页继续。
    """
    job.job_status = JobStatus.SUCCEEDED.value
    job.attempt_no = 0
    job.next_retry_at = None
    job.last_error_code = None
    job.last_error_text = None
    job.finished_at = now or utcnow()
    if checkpoint is not None:
        job.checkpoint_json = json.dumps(checkpoint, ensure_ascii=False, default=str)


def reset_for_retry(session: Session, job: WorkflowJob) -> None:
    """显式重新开始一轮：清空尝试计数与错误信息。

    **只在服务入口被显式调用时用**（人工点重试、API 重新触发）。
    一次显式调用就是"重新开始一轮"，理应拿回完整的重试预算 ——
    否则一个曾经重试耗尽的作业会永远卡在 `failed`，人工怎么点都没用，只能手工改库。

    **不用于 Worker 内部的自动重试**：那是**同一轮**，必须继续累积 `attempt_no`，
    否则 `max_attempts` 永远不会耗尽，"自动重试耗尽后 blocked"这条规则失效。

    判别标准因此很清晰：**跨服务入口 = 新一轮；进程内重试 = 同一轮。**
    """
    job.job_status = JobStatus.QUEUED.value
    job.attempt_no = 0
    job.next_retry_at = None
    job.last_error_code = None
    job.last_error_text = None
    job.started_at = None
    job.finished_at = None


def mark_failed(
    session: Session,
    job: WorkflowJob,
    *,
    error: AppError | None = None,
    error_code: ErrorCode | None = None,
    message: str = "",
    now: datetime | None = None,
) -> JobStatus:
    """按错误的**可重试性**决定进入 `retry_wait` 还是 `failed`。

    判据唯一（`is_retryable`），不允许调用方自行判断：

    | 情况 | 结果 |
    | --- | --- |
    | 瞬时错误 且 `attempt_no < max_attempts` | `retry_wait` + 指数退避 |
    | 瞬时错误 但尝试次数耗尽 | `failed` |
    | 确定性错误 | `failed`（**不浪费重试**） |

    Returns:
        流转后的作业状态，便于调用方据此决定是否要让任务 `blocked`。

    Raises:
        ValueError: 既没有 `error` 也没有 `error_code`（无从分类）。
    """
    resolved_code = error.code if error is not None else error_code
    if resolved_code is None:
        raise ValueError("mark_failed 需要 error 或 error_code，否则无法判断是否可重试")

    resolved_message = message or (getattr(error, "message", "") or "")

    moment = now or utcnow()
    attempts_left = (job.attempt_no or 0) < (job.max_attempts or 1)

    if is_retryable(resolved_code) and attempts_left:
        job.job_status = JobStatus.RETRY_WAIT.value
        job.next_retry_at = moment + timedelta(
            seconds=backoff_seconds(job.attempt_no or 1)
        )
    else:
        job.job_status = JobStatus.FAILED.value
        job.next_retry_at = None
        job.finished_at = moment

    job.last_error_code = str(resolved_code)
    job.last_error_text = resolved_message

    return JobStatus(job.job_status)


def backoff_seconds(
    attempt_no: int, *, base: float = _BACKOFF_BASE, cap: float = _BACKOFF_CAP_SECONDS
) -> float:
    """指数退避：`base ** attempt_no`，上限 `cap` 秒。

    上限不可省：不封顶的话第 10 次重试要等约 17 分钟，而那时人早就该介入看看到底出了什么事 ——
    无限增长的等待会让"重试耗尽 → blocked → 人工处理"这条路径迟迟走不到。

    **本实现是确定性的（无抖动）**，便于测试与复现。M4 引入多个 Worker 并发轮询后
    需在此加随机抖动以避免惊群 —— 那是 M4 的任务。
    """
    if attempt_no < 1:
        attempt_no = 1
    return min(cap, base**attempt_no)


def _find_by_key(session: Session, idempotency_key: str) -> WorkflowJob | None:
    """按幂等键查作业。"""
    statement = select(WorkflowJob).where(
        WorkflowJob.idempotency_key == idempotency_key
    )
    return session.execute(statement).scalar_one_or_none()


def _as_job_type(value: JobType | str) -> JobType:
    """把作业类型规范化为 `JobType`（取值域与数据库 CHECK 一致）。"""
    if isinstance(value, JobType):
        return value
    try:
        return JobType(value)
    except ValueError as exc:
        raise ValueError(f"未知的作业类型：{value!r}") from exc
