"""任务状态机 —— `task_status` 的唯一修改入口。

    pending → parsing → reviewing → done
        ↘         ↓          ↓
         blocked ←┴──────────┘        人工重试 → parsing / reviewing

两条红线：

1. **`done` 是终态**（`done → blocked` 也非法）：已成功回写的任务不该因为后续动作
   失败而被判阻塞，否则调用端会看到"已完成的合同又变成阻塞了"。
2. **`blocked` 只能由 `mark_blocked()` 进入**：它同时写三个字段。允许直接
   `transition(BLOCKED)` 就会出现"阻塞了但没有失败位置"，人工重试无从判断恢复点（§7.4）。

时间戳统一用 **naive UTC**，与 SQLite `CURRENT_TIMESTAMP` 一致；混用本地时间会让
"重试是否到期"这类判断在跨时区部署时**静默出错**。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.enums import ErrorCode, JobType, TaskStatus
from app.errors import InvalidStateTransition
from app.models import ApprovalTask

#: 合法状态转换表。不在此表内的转换一律拒绝。
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.PARSING, TaskStatus.BLOCKED}),
    TaskStatus.PARSING: frozenset({TaskStatus.REVIEWING, TaskStatus.BLOCKED}),
    TaskStatus.REVIEWING: frozenset({TaskStatus.DONE, TaskStatus.BLOCKED}),
    # 人工重试从检查点恢复：解析类问题回 parsing，规则/结果/回写类回 reviewing
    TaskStatus.BLOCKED: frozenset({TaskStatus.PARSING, TaskStatus.REVIEWING}),
    TaskStatus.DONE: frozenset(),  # 终态
}

#: 失败位置 → 人工重试的恢复起点（§7.4 检查点恢复）
_STAGE_RESUME: dict[JobType, TaskStatus] = {
    JobType.PULL: TaskStatus.PARSING,
    JobType.DETAIL: TaskStatus.PARSING,
    JobType.DOWNLOAD: TaskStatus.PARSING,
    JobType.PARSE: TaskStatus.PARSING,
    JobType.RULE: TaskStatus.REVIEWING,
    JobType.RESULT: TaskStatus.REVIEWING,
    # 回写失败只重试回写：任务回 reviewing，由回写模块按 blocked_stage 判断，
    # 不重新解析、也不重新审查（重新审查会产生新批次，把一次写入失败放大）
    JobType.WRITEBACK: TaskStatus.REVIEWING,
}


def utcnow() -> datetime:
    """naive UTC 当前时间，与 SQLite `CURRENT_TIMESTAMP` 的口径一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ============================================================
# 人工重试：从检查点恢复（M7 / §7.4）
# ============================================================
# ⚠️ 「恢复到哪个业务状态」（`_STAGE_RESUME`）与「重跑哪个作业」（本表）是
# **两个维度**，缺一不可：
#
#   只有状态、没有作业 → 任务回到 `reviewing`，却没有任何作业可领。
#   界面显示"已重试"，而系统里什么都没发生 —— 这是最难排查的一类
#   "看起来做完了"。
#
# 因此两张表放在同一个模块：它们是同一份"检查点知识"的两半，
# 分开放时新增一个 `JobType` 只会更新其中一处。

#: 失败位置 → 人工重试要**重新执行的那一步**。
#:
#: `result` → `RESULT`（而不是重跑整批规则）：批次已经是好的，
#: 失败的只是"把结论落成结果"这一步；重跑规则会**新建批次**，
#: 把一次保存失败放大成一次结论变更。
#:
#: ⚠️ `pull` / `detail` / `download` **刻意不在表内**：这三步在工具 1–3 的
#: 调用路径上是**同步**完成的（Worker 不领取它们），它们的恢复入口是
#: **重新执行对应的工具** —— 重跑工具 3 成功后 `attachment_service`
#: 会自己 `start_retry(task)`。把它们映射成一个 Worker 永远不会领取的作业，
#: 才是真正的坑：任务回到了 `parsing`，然后永远停在那里。
_STAGE_RETRY_JOB: dict[JobType, JobType] = {
    JobType.PARSE: JobType.PARSE,
    JobType.RULE: JobType.RULE,
    JobType.RESULT: JobType.RESULT,
    JobType.WRITEBACK: JobType.WRITEBACK,
}


def retry_job_type(blocked_stage: JobType | str | None) -> JobType | None:
    """给定失败位置，返回人工重试要重跑的作业类型。

    返回 `None` 是一个**明确的业务结论**：这一步的恢复入口不在重试接口
    （见 `_STAGE_RETRY_JOB` 的说明）。调用方据此给出稳定错误码与
    "该去执行哪个工具"的指引 —— 而不是猜一个作业类型出来。
    """
    if blocked_stage is None:
        return None
    try:
        stage = _as_job_type(blocked_stage)
    except ValueError:
        return None
    return _STAGE_RETRY_JOB.get(stage)


def as_task_status(value: TaskStatus | str) -> TaskStatus:
    """把取值规范化为 `TaskStatus`；非法取值抛 `ValueError`（编程错误，非业务错误）。"""
    if isinstance(value, TaskStatus):
        return value
    try:
        return TaskStatus(value)
    except ValueError as exc:
        raise ValueError(f"未知的任务状态：{value!r}") from exc


def can_transition(current: TaskStatus | str, target: TaskStatus | str) -> bool:
    """判断转换是否合法（供调用端决定按钮是否可点）。"""
    return as_task_status(target) in ALLOWED_TRANSITIONS[as_task_status(current)]


def transition(
    task: ApprovalTask,
    to_status: TaskStatus | str,
    *,
    reason: str | None = None,
) -> bool:
    """切换任务状态；返回是否**真的发生了变化**（同状态为幂等空操作，返回 False）。

    同状态不报错是有意的：重复设置同一状态是常态（如下载成功后再确认仍在 `parsing`），
    为此报错会让调用方到处写 `if` 判断，反而更容易漏掉真正的非法转换。
    `reason` 只用于异常消息，不落库（落库的可读原因走 `mark_blocked` 的 `message`）。

    Raises:
        InvalidStateTransition: 目标状态不是当前状态的合法后继。
    """
    current = as_task_status(task.task_status)
    target = as_task_status(to_status)

    if current == target:
        return False

    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransition(
            f"任务 {task.id} 不允许从 {current} 转换到 {target}"
            + (f"（{reason}）" if reason else ""),
            code=ErrorCode.INVALID_STATE_TRANSITION,
        )

    task.task_status = target.value
    # 赋值即标记为脏：SQLAlchemy 会在 UPDATE 时带上 onupdate 的 updated_at
    return True


def mark_blocked(
    task: ApprovalTask,
    *,
    stage: JobType | str,
    error_code: ErrorCode,
    message: str,
) -> bool:
    """把任务置为阻塞，并**同时**记录失败位置、稳定错误码与可读原因。

    三者缺一不可，否则会出现"阻塞了但说不清为什么、也不知道从哪恢复"。

    ⚠️ **先验证能否转换，再一次性写入**。反过来（先写字段再转换）会在非法转换时
    留下**部分写入**：调用方看到异常、以为什么都没发生，而业务失败路径**同样会提交**
    （见 `app/db.py`），库里于是留下 `task_status=done` 却 `blocked_stage=download`
    的矛盾记录 —— "报错但数据已改"是最难排查的一类写入。

    Raises:
        ValueError: `stage` 不是受控的失败位置取值。
        InvalidStateTransition: 任务已处于终态（此时不写入任何字段）。
    """
    stage_value = _as_job_type(stage)

    # 先转换（可能抛错），确认合法后再写另外三个字段
    changed = transition(task, TaskStatus.BLOCKED, reason=message)

    task.blocked_stage = stage_value.value
    task.last_error_code = str(error_code)
    task.block_reason = message

    return changed


def resume_target(blocked_stage: str | None) -> TaskStatus:
    """给定失败位置，返回人工重试应该回到的状态（§7.4）。

    失败位置缺失或无法识别时回到 `parsing` —— 链路最上游的**安全选择**：
    从头再走最多是重复劳动，而"跳过解析直接审查"会让后续步骤拿着过期结果继续跑。
    """
    if blocked_stage is None:
        return TaskStatus.PARSING
    try:
        return _STAGE_RESUME[JobType(blocked_stage)]
    except ValueError:
        return TaskStatus.PARSING


def start_retry(task: ApprovalTask) -> TaskStatus:
    """人工重试：`blocked` → 检查点恢复阶段，并累加 `retry_count`。

    清理三个阻塞字段：它们描述的是**当前**阻塞状态，重试后已不再处于该状态
    （历史仍可查，见 M6 的审计事件）。

    `retry_count` 是**累积计数器**，成功时不清零 —— 它回答"这个任务被人工干预过几次"，
    清零会让反复重试仍失败的任务看起来像从未重试过。

    Raises:
        InvalidStateTransition: 任务不处于 `blocked`（无从重试）。
    """
    if as_task_status(task.task_status) is not TaskStatus.BLOCKED:
        raise InvalidStateTransition(
            f"任务 {task.id} 当前状态为 {task.task_status}，只有 blocked 状态可以人工重试",
            code=ErrorCode.INVALID_STATE_TRANSITION,
        )

    target = resume_target(task.blocked_stage)

    task.blocked_stage = None
    task.last_error_code = None
    task.block_reason = None
    task.retry_count = (task.retry_count or 0) + 1

    transition(task, target, reason="人工重试")
    return target


def _as_job_type(value: JobType | str) -> JobType:
    """把失败位置规范化为 `JobType`（取值域与 `blocked_stage` 的 CHECK 一致）。"""
    if isinstance(value, JobType):
        return value
    try:
        return JobType(value)
    except ValueError as exc:
        raise ValueError(f"未知的失败位置：{value!r}") from exc
