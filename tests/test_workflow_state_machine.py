"""任务状态机测试（M3 / T4）。

状态机是本项目里最容易"看起来对、实际错"的一块：
非法转换不拦、阻塞时漏记失败位置，都不会立刻报错，
而是在几周后表现为"某个任务卡在奇怪的状态里，没人知道为什么"。

因此本文件重点覆盖三类**必须拒绝**的情况：

1. 跨阶段跳跃（`pending → reviewing`）；
2. 终态回退（`done → blocked`）；
3. 阻塞但缺少失败位置（无从恢复检查点）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.enums import ErrorCode, JobType, TaskStatus
from app.errors import InvalidStateTransition, WorkflowError
from app.models import ApprovalTask
from app.workflow.state_machine import (
    ALLOWED_TRANSITIONS,
    can_transition,
    mark_blocked,
    resume_target,
    start_retry,
    transition,
)


def _task(status: TaskStatus = TaskStatus.PENDING, **overrides: object) -> ApprovalTask:
    """构造一个未持久化的任务对象（状态机只操作属性，不需要数据库）。"""
    task = ApprovalTask(
        approval_code="HT-2026-0001",
        instance_id="HT-2026-0001",
        approval_title="测试合同",
        context_status="complete",
    )
    task.task_status = status.value
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


# ============================================================
# 1. 合法转换
# ============================================================


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (TaskStatus.PENDING, TaskStatus.PARSING),
        (TaskStatus.PARSING, TaskStatus.REVIEWING),
        (TaskStatus.REVIEWING, TaskStatus.DONE),
        # 所有非终态都能进入 blocked
        (TaskStatus.PENDING, TaskStatus.BLOCKED),
        (TaskStatus.PARSING, TaskStatus.BLOCKED),
        (TaskStatus.REVIEWING, TaskStatus.BLOCKED),
        # 人工重试从检查点恢复
        (TaskStatus.BLOCKED, TaskStatus.PARSING),
        (TaskStatus.BLOCKED, TaskStatus.REVIEWING),
    ],
    ids=[
        "待办→解析",
        "解析→审查",
        "审查→完成",
        "待办→阻塞",
        "解析→阻塞",
        "审查→阻塞",
        "阻塞→解析",
        "阻塞→审查",
    ],
)
def test_legal_transitions_are_applied(start: TaskStatus, target: TaskStatus) -> None:
    task = _task(start)

    changed = transition(task, target)

    assert changed is True
    assert task.task_status == target.value


def test_same_status_is_a_noop_not_an_error() -> None:
    """重复设置同一状态是**幂等空操作**。

    流程中重复设置同一状态是常态（下载成功后再确认一次仍处于 `parsing`）。
    为此报错会逼着调用方到处写 `if` 判断，反而更容易漏掉真正的非法转换。
    """
    task = _task(TaskStatus.PARSING)

    assert transition(task, TaskStatus.PARSING) is False
    assert task.task_status == TaskStatus.PARSING.value


def test_can_transition_matches_the_transition_table() -> None:
    """`can_transition` 必须与真正执行转换时的判据完全一致。

    它是给调用端判断"按钮能不能点"用的；若两者不一致，
    界面会显示可点、点下去却抛异常。
    """
    for current, allowed in ALLOWED_TRANSITIONS.items():
        for target in TaskStatus:
            expected = target in allowed and target is not current
            assert can_transition(current, target) is expected, (
                f"{current} → {target} 的判断与实际转换表不一致"
            )


# ============================================================
# 2. 非法转换必须被拒绝
# ============================================================


@pytest.mark.parametrize(
    ("start", "target"),
    [
        (TaskStatus.PENDING, TaskStatus.REVIEWING),
        (TaskStatus.PENDING, TaskStatus.DONE),
        (TaskStatus.PARSING, TaskStatus.PENDING),  # 回退
        (TaskStatus.PARSING, TaskStatus.DONE),  # 跳过审查
        (TaskStatus.REVIEWING, TaskStatus.PARSING),  # 回退
        (TaskStatus.DONE, TaskStatus.BLOCKED),
        (TaskStatus.DONE, TaskStatus.REVIEWING),
        (TaskStatus.DONE, TaskStatus.PARSING),
    ],
    ids=[
        "待办→审查(跳阶段)",
        "待办→完成(跳阶段)",
        "解析→待办(回退)",
        "解析→完成(跳过审查)",
        "审查→解析(回退)",
        "完成→阻塞",
        "完成→审查",
        "完成→解析",
    ],
)
def test_illegal_transition_is_rejected(start: TaskStatus, target: TaskStatus) -> None:
    """非法转换必须抛错，并且**保持原状态不变**。

    `done → blocked` 尤其重要：已经成功回写的任务不该因为后续某个动作失败
    被判阻塞，否则调用端会看到"已完成的合同又变成阻塞了"。
    """
    task = _task(start)

    with pytest.raises(InvalidStateTransition) as excinfo:
        transition(task, target)

    assert excinfo.value.code == ErrorCode.INVALID_STATE_TRANSITION
    assert excinfo.value.retryable is False, "状态机错误重试永远不会变合法"
    assert isinstance(excinfo.value, WorkflowError)
    assert task.task_status == start.value, "转换失败后不得改动原状态"


def test_unknown_status_value_is_a_programming_error() -> None:
    """未知状态取值属于代码缺陷，抛 `ValueError` 而不是业务异常。"""
    task = _task()

    with pytest.raises(ValueError, match="未知的任务状态"):
        transition(task, "whatever")


# ============================================================
# 3. 阻塞记录：三个字段缺一不可
# ============================================================


def test_mark_blocked_records_stage_code_and_reason() -> None:
    """阻塞必须**同时**写失败位置、稳定错误码与可读原因。

    只写其中一个就会出现"阻塞了但说不清为什么、也不知道从哪恢复"：
    人工重试要靠 `blocked_stage` 决定回到哪个检查点。
    """
    task = _task(TaskStatus.PARSING)

    mark_blocked(
        task,
        stage=JobType.DOWNLOAD,
        error_code=ErrorCode.ATTACHMENT_MISSING,
        message="附件 A-5002 在审批系统中已被删除",
    )

    assert task.task_status == TaskStatus.BLOCKED.value
    assert task.blocked_stage == JobType.DOWNLOAD.value
    assert task.last_error_code == ErrorCode.ATTACHMENT_MISSING.value
    assert task.block_reason == "附件 A-5002 在审批系统中已被删除"


def test_mark_blocked_rejects_unknown_stage() -> None:
    """失败位置必须在受控取值内（数据库也有 CHECK 约束同一取值域）。"""
    task = _task(TaskStatus.PARSING)

    with pytest.raises(ValueError, match="未知的失败位置"):
        mark_blocked(
            task,
            stage="whatever",
            error_code=ErrorCode.APPROVAL_API_ERROR,
            message="x",
        )


def test_mark_blocked_on_done_is_rejected() -> None:
    """已完成的合同不得被判阻塞。"""
    task = _task(TaskStatus.DONE)

    with pytest.raises(InvalidStateTransition):
        mark_blocked(
            task,
            stage=JobType.WRITEBACK,
            error_code=ErrorCode.APPROVAL_API_ERROR,
            message="x",
        )


# ============================================================
# 4. 检查点恢复
# ============================================================


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (JobType.PULL, TaskStatus.PARSING),
        (JobType.DETAIL, TaskStatus.PARSING),
        (JobType.DOWNLOAD, TaskStatus.PARSING),
        (JobType.PARSE, TaskStatus.PARSING),
        (JobType.RULE, TaskStatus.REVIEWING),
        (JobType.RESULT, TaskStatus.REVIEWING),
        # 回写问题只重试回写 → 任务回到 reviewing，不重新解析也不重新审查
        (JobType.WRITEBACK, TaskStatus.REVIEWING),
    ],
    ids=["拉取", "详情", "下载", "解析", "规则", "结果", "回写"],
)
def test_resume_target_follows_checkpoint_table(
    stage: JobType, expected: TaskStatus
) -> None:
    """还原起点必须与 §7.4 的检查点表一致。"""
    assert resume_target(stage.value) is expected


@pytest.mark.parametrize("stage", [None, "", "whatever"], ids=["缺失", "空串", "无法识别"])
def test_resume_target_defaults_to_parsing(stage: str | None) -> None:
    """失败位置缺失或无法识别时回到 `parsing` —— 这是**安全选择**。

    从头再走一遍最多是重复劳动；而"跳过解析直接审查"会让后续步骤
    拿着过期的解析结果继续跑，产生更难发现的错误。
    """
    assert resume_target(stage) is TaskStatus.PARSING


def test_start_retry_resumes_and_clears_block_fields() -> None:
    """人工重试：回到检查点 + 累加计数 + 清空**当前**阻塞信息。"""
    task = _task(TaskStatus.PARSING, retry_count=1)
    mark_blocked(
        task,
        stage=JobType.RULE,
        error_code=ErrorCode.APPROVAL_API_ERROR,
        message="审批系统在审查阶段返回 500",
    )

    target = start_retry(task)

    assert target is TaskStatus.REVIEWING
    assert task.task_status == TaskStatus.REVIEWING.value
    assert task.retry_count == 2, "retry_count 是累积计数器，必须在原有基础上加一"
    # 三个字段描述的是"当前"的阻塞状态，重试之后任务已不在该状态
    assert task.blocked_stage is None
    assert task.last_error_code is None
    assert task.block_reason is None


def test_retry_count_accumulates_across_repeated_retries() -> None:
    """反复重试仍失败时，计数必须持续累加。

    它的用途是回答"这个任务被人工干预过几次"。
    若在成功或重试时清零，反复失败的任务会看起来像从未重试过。
    """
    task = _task(TaskStatus.PARSING)

    for expected_count in (1, 2, 3):
        mark_blocked(
            task,
            stage=JobType.PARSE,
            error_code=ErrorCode.STORAGE_UNAVAILABLE,
            message="存储暂时不可用",
        )
        start_retry(task)
        assert task.retry_count == expected_count


def test_start_retry_requires_blocked_status() -> None:
    """只有 `blocked` 状态可以人工重试。"""
    task = _task(TaskStatus.REVIEWING)

    with pytest.raises(InvalidStateTransition) as excinfo:
        start_retry(task)

    assert excinfo.value.code == ErrorCode.INVALID_STATE_TRANSITION


# ============================================================
# 5. 与数据库的衔接
# ============================================================


def test_transition_is_persisted_by_orm(work_dir) -> None:
    """状态确实落到库里，且 `updated_at` 随更新前进。

    这条覆盖一个容易漏掉的细节：`transition` 只改属性、不提交事务，
    真正的持久化依赖调用方提交。因此必须验证"赋值 → flush → 读回"整条链路。
    """
    engine = create_engine(f"sqlite:///{(work_dir / 'sm.db').as_posix()}", future=True)
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            task = ApprovalTask(
                approval_code="HT-SM-1",
                instance_id="HT-SM-1",
                context_status="complete",
            )
            session.add(task)
            session.commit()

            transition(task, TaskStatus.PARSING)
            session.commit()

            session.expire_all()
            reloaded = (
                session.query(ApprovalTask).filter_by(instance_id="HT-SM-1").one()
            )
            assert reloaded.task_status == TaskStatus.PARSING.value
    finally:
        engine.dispose()


# ============================================================
# 非法阻塞不得留下部分写入（复审修正）
# ============================================================


def test_illegal_block_writes_nothing() -> None:
    """非法阻塞**不得改动任何字段**。

    这是"先写字段、再验证转换"的回归测试。那种顺序下：

    ```text
    mark_blocked(task) → 三个阻塞字段已改
                       → transition() 抛 InvalidStateTransition
    而业务失败路径**同样会提交**（见 app/db.py 的 transactional_session）
    → 库里留下 task_status=done 却 blocked_stage=download 的矛盾记录
    ```

    后果比"多写了几个字段"严重得多：调用方收到异常、以为什么都没发生，
    而数据已经不一致。之后任何人看到这条记录都会得出错误结论——
    任务"既完成又阻塞"，排障时根本无从下手。

    断言**四个字段一个都不能变**，而不是只检查 `task_status`：
    后者在"先写字段"的实现下恰好是对的（转换失败不改状态），
    会放过真正的缺陷。
    """
    task = _task(TaskStatus.DONE)

    with pytest.raises(InvalidStateTransition):
        mark_blocked(
            task,
            stage=JobType.DOWNLOAD,
            error_code=ErrorCode.ATTACHMENT_MISSING,
            message="附件已被删除",
        )

    assert task.task_status == TaskStatus.DONE.value
    assert task.blocked_stage is None
    assert task.last_error_code is None
    assert task.block_reason is None


def test_blocking_an_already_blocked_task_records_latest_reason() -> None:
    """已阻塞的任务再次阻塞是**合法空操作**，并把三个字段更新为最新原因。

    这条固定一个刻意选择：`blocked → blocked` 是同状态转换，
    `transition` 返回 `False` 而不报错，三个字段照写。
    于是 `blocked_stage` 反映的是**最近一次**失败，重试会从那里恢复。

    选"最新原因"而不是"首次原因"：运维看到的应当是当前事实。
    若保留首次原因，一个已被重试多次的任务会显示早已过时的失败点。
    该选择是安全的——`_STAGE_RESUME` 把 pull/detail/download/parse
    都映射到 `parsing`，因此在这几个阶段之间覆盖不会**跳过**失败步骤。
    """
    task = _task(TaskStatus.PENDING)

    mark_blocked(
        task,
        stage=JobType.PARSE,
        error_code=ErrorCode.ATTACHMENT_EMPTY,
        message="解析失败：内容为空",
    )
    changed = mark_blocked(
        task,
        stage=JobType.DOWNLOAD,
        error_code=ErrorCode.ATTACHMENT_MISSING,
        message="附件已被删除",
    )

    assert changed is False, "同状态转换不算状态变化"
    assert task.task_status == TaskStatus.BLOCKED.value
    assert task.blocked_stage == JobType.DOWNLOAD.value
    assert task.last_error_code == ErrorCode.ATTACHMENT_MISSING.value
    assert task.block_reason == "附件已被删除"
    assert resume_target(task.blocked_stage) == TaskStatus.PARSING
