"""作业台账测试（M3 / T4）。

本文件的**核心**是锁定一个具体的死锁风险：

> 若作业幂等键只认审批单号，同一审批单的**第二次同步会被唯一约束永久拒绝**。

审批表单与附件都会变化，所以"同一审批单只能同步一次"是错的。
`build_idempotency_key()` 因此要求"输入版本"参与构造，
拿不到版本时退化为一次性键 —— 不给"忘记传版本"留后门。

另外覆盖两类错误分类，它们决定"重试还是立即放弃"：

| 情况 | 结果 |
| --- | --- |
| 瞬时错误 且还有剩余尝试 | `retry_wait` + 指数退避 |
| 瞬时错误 但尝试次数耗尽 | `failed` |
| 确定性错误 | `failed`（**不浪费重试**） |
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.enums import ErrorCode, JobStatus, JobType
from app.errors import IdempotencyConflict, PermanentStorageError, TransientGatewayError
from app.models import WorkflowJob
from app.workflow.jobs import (
    backoff_seconds,
    build_idempotency_key,
    create_job,
    freeze_job_input,
    mark_failed,
    mark_running,
    mark_succeeded,
    pull_window_token,
    request_fingerprint,
)


@pytest.fixture()
def session(work_dir: Path):
    """每个测试一个独立的库（按 ORM 元数据建表，因此 CHECK 约束一并生效）。"""
    engine = create_engine(
        f"sqlite:///{(work_dir / 'jobs.db').as_posix()}", future=True
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db_session:
            yield db_session
    finally:
        engine.dispose()


# ============================================================
# 0. 幂等键命中 ≠ 该复用
# ============================================================


def test_same_key_with_different_input_is_rejected(session: Session) -> None:
    """同一个幂等键配上**不同输入**必须报错，不得静默复用。

    ⚠️ 这条守的是一次真实存在过的静默错配：命中幂等键后**直接返回**既有作业、
    不比对任何输入。而键是调用方构造的 —— 构造得不对（漏掉租户、版本取错、
    直接传一个常量）时，两份不同的输入会共用同一个键：

    ```text
    第一次：key=same-key + tenant-A + 附件A  → 建库成功
    第二次：key=same-key + tenant-B + 附件B  → 静默返回**第一次**的作业
    ```

    调用方以为自己的参数生效了，实际拿到的是另一个租户的旧作业与旧数据 ——
    而作业状态正常、输入字段完整、唯一约束也没被违反，
    **库里没有任何一处看得出这件事**。
    """
    first, created = create_job(
        session,
        job_type=JobType.DETAIL,
        idempotency_key="same-key",
        input_payload={
            "provider": "mock",
            "tenant_id": "tenant-A",
            "instance_id": "HT-1",
        },
    )
    assert created is True

    with pytest.raises(IdempotencyConflict) as excinfo:
        create_job(
            session,
            job_type=JobType.DETAIL,
            idempotency_key="same-key",
            input_payload={
                "provider": "mock",
                "tenant_id": "tenant-B",
                "instance_id": "HT-1",
            },
        )

    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert excinfo.value.retryable is False, "重试不会让冲突消失"
    assert "same-key" in str(excinfo.value)

    # 既有作业不得被改动
    existing = session.get(WorkflowJob, first.id)
    assert "tenant-A" in existing.input_json


def test_same_key_same_input_is_still_reused(session: Session) -> None:
    """正常路径**必须**继续复用 —— 否则幂等就没有意义了。

    这条与上一条是一对：只加"拒绝"而忘了"放过"，会让所有重试都变成 409。
    """
    payload = {"provider": "mock", "tenant_id": "default", "instance_id": "HT-1"}
    first, created = create_job(
        session, job_type=JobType.DETAIL, idempotency_key="k", input_payload=payload
    )
    second, created_again = create_job(
        session, job_type=JobType.DETAIL, idempotency_key="k", input_payload=payload
    )

    assert created is True
    assert created_again is False
    assert first.id == second.id


def test_same_key_different_job_type_is_rejected(session: Session) -> None:
    """`job_type` 不同即不同的操作，即使输入形状碰巧合法。"""
    create_job(
        session,
        job_type=JobType.PULL,
        idempotency_key="k2",
        input_payload={"provider": "mock", "tenant_id": "default"},
    )

    with pytest.raises(IdempotencyConflict):
        create_job(
            session,
            job_type=JobType.DETAIL,
            idempotency_key="k2",
            input_payload={
                "provider": "mock",
                "tenant_id": "default",
                "instance_id": "HT-1",
            },
        )


def test_same_key_different_task_is_rejected(session: Session) -> None:
    """`task_id` 不同即不同的操作（同一租户下的另一条审批单）。"""
    payload = {"provider": "mock", "tenant_id": "default", "instance_id": "HT-1"}
    create_job(
        session,
        job_type=JobType.DETAIL,
        idempotency_key="k3",
        input_payload=payload,
        task_id=1,
    )

    with pytest.raises(IdempotencyConflict):
        create_job(
            session,
            job_type=JobType.DETAIL,
            idempotency_key="k3",
            input_payload=payload,
            task_id=2,
        )


# ============================================================
# 1. 幂等键构造
# ============================================================


def test_key_contains_type_identity_and_version() -> None:
    """键形如 `{job_type}:{identity}:{version}`，可读、可排障。"""
    key = build_idempotency_key(
        JobType.DETAIL, "HT-2026-0001", "2026-09-14T10:00:00"
    )

    assert key == "detail:HT-2026-0001:2026-09-14T10:00:00"


def test_key_is_stable_for_same_inputs() -> None:
    """同样的输入必须得到同样的键，否则幂等就是空话。"""
    first = build_idempotency_key("pull", "mock:default", "w202609141000")
    second = build_idempotency_key("pull", "mock:default", "w202609141000")

    assert first == second


def test_missing_version_falls_back_to_oneshot_key() -> None:
    """拿不到输入版本时退化为**一次性键**。

    这是刻意的取舍：此时无法判断"这次调用与上次是否同一件事"，
    只能在"拒绝创建作业（功能不可用）"与"允许后续同步（失去合并能力）"之间选。
    **宁可多跑一次，也不能把对象永久卡死** ——
    前者是可观测、可恢复的浪费，后者是静默的功能失效。
    """
    first = build_idempotency_key(JobType.DETAIL, "HT-2026-0001")
    second = build_idempotency_key(JobType.DETAIL, "HT-2026-0001")

    assert first != second, "缺版本时必须是一次性键，否则第二次同步会被永久拒绝"
    assert first.startswith("detail:HT-2026-0001:")


def test_blank_version_is_treated_as_missing() -> None:
    """空串与纯空白等同于"没给版本"，同样走一次性键。"""
    first = build_idempotency_key(JobType.DETAIL, "HT-1", "   ")
    second = build_idempotency_key(JobType.DETAIL, "HT-1", "")

    assert first != second


@pytest.mark.parametrize(
    "identity", ["", "   ", None], ids=["空串", "纯空白", "None"]
)
def test_blank_identity_is_rejected(identity: str | None) -> None:
    """业务标识为空属于编程错误，必须立刻报错。

    否则会生成 `detail::v1` 这种键 —— 所有没有标识的作业挤在同一个键上，
    互相阻塞，而且从键本身看不出问题。
    """
    with pytest.raises(ValueError, match="业务标识不能为空"):
        build_idempotency_key(JobType.DETAIL, identity)  # type: ignore[arg-type]


def test_unknown_job_type_is_rejected() -> None:
    """作业类型必须在受控取值内（数据库也有 CHECK 约束同一取值域）。"""
    with pytest.raises(ValueError, match="未知的作业类型"):
        build_idempotency_key("whatever", "HT-1", "v1")


def test_version_changes_produce_different_keys() -> None:
    """**同一条审批单、不同输入版本 → 不同的键。**

    这正是修掉那个死锁的关键：详情变了必须能再同步一次。
    """
    v1 = build_idempotency_key(JobType.DETAIL, "HT-1", "2026-09-13T09:00:00")
    v2 = build_idempotency_key(JobType.DETAIL, "HT-1", "2026-09-14T10:00:00")

    assert v1 != v2


# ============================================================
# 2. 拉取时间窗口与请求指纹
# ============================================================


def test_pull_window_collapses_clicks_within_the_same_minute() -> None:
    """同一分钟内的重复点击必须合流为一个作业（防双击、防重复触发）。"""
    base = datetime(2026, 9, 14, 10, 0, 10)

    assert pull_window_token(base) == pull_window_token(base + timedelta(seconds=40))


def test_pull_window_changes_across_minutes() -> None:
    """跨窗口视为新的一次拉取 —— 本来就该重新拉一遍。"""
    first = pull_window_token(datetime(2026, 9, 14, 10, 0, 59))
    second = pull_window_token(datetime(2026, 9, 14, 10, 1, 0))

    assert first != second


def test_pull_window_supports_multi_minute_buckets() -> None:
    """窗口长度可配置：5 分钟窗口内前 5 分钟共用一个 token。"""
    first = pull_window_token(datetime(2026, 9, 14, 10, 3, 0), minutes=5)
    second = pull_window_token(datetime(2026, 9, 14, 10, 4, 59), minutes=5)

    assert first == second


def test_pull_window_rejects_non_positive_length() -> None:
    """窗口长度为 0 会让每次调用都换一个 token，等于没有窗口。"""
    with pytest.raises(ValueError, match="窗口长度必须为正数"):
        pull_window_token(datetime(2026, 9, 14, 10, 0, 0), minutes=0)


def test_request_fingerprint_ignores_key_order() -> None:
    """指纹必须与字典顺序无关，否则同一份内容会得到两个指纹。"""
    first = request_fingerprint({"a": 1, "b": 2})
    second = request_fingerprint({"b": 2, "a": 1})

    assert first == second
    assert len(first) == 16


def test_request_fingerprint_changes_with_content() -> None:
    assert request_fingerprint({"amount": "100"}) != request_fingerprint(
        {"amount": "200"}
    )


# ============================================================
# 3. 创建作业（幂等）
# ============================================================

# M4 起 `create_job` **强制要求不可变输入**（input_json / input_digest 是 NOT NULL），
# 并按作业类型做**严格校验**（`app/workflow/job_inputs.py` 的输入模型）。
# 因此这份样本必须满足对应模型 —— 少一个字段就会被 `JobInputError` 拒绝。
_DETAIL_INPUT = {"provider": "mock", "tenant_id": "default", "instance_id": "HT-1"}
_PULL_INPUT = {"provider": "mock", "tenant_id": "default"}
_PARSE_INPUT = {
    "parse_id": 1,
    "attachment_record_id": 1,
    "source_checksum": "a" * 64,
    "object_key": f"sha256/aa/aa/{'a' * 64}.pdf",
    "content_type": "application/pdf",
}
_DOWNLOAD_INPUT = {
    "provider": "mock",
    "tenant_id": "default",
    "instance_id": "HT-1",
    "attachment_id": "A-1",
    "attachment_record_id": 1,
}


def test_create_job_returns_new_job_first_time(session: Session) -> None:
    job, created = create_job(
        session,
        job_type=JobType.DETAIL,
        idempotency_key="detail:HT-1:v1",
        input_payload=_DETAIL_INPUT,
    )

    assert created is True
    assert job.job_status == JobStatus.QUEUED.value
    assert job.attempt_no == 0
    assert job.max_attempts == 3
    assert job.next_retry_at is None


def test_create_job_is_idempotent_on_repeated_key(session: Session) -> None:
    """同键重复创建 → 返回**既有作业**，不新建、不报错。"""
    first, created_first = create_job(
        session,
        job_type=JobType.DETAIL,
        idempotency_key="detail:HT-1:v1",
        input_payload=_DETAIL_INPUT,
    )
    second, created_second = create_job(
        session,
        job_type=JobType.DETAIL,
        idempotency_key="detail:HT-1:v1",
        input_payload=_DETAIL_INPUT,
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id


def test_same_instance_with_new_version_creates_a_second_job(
    session: Session,
) -> None:
    """**核心反例**：同一审批单、新输入版本 → 必须允许产生新作业。

    若幂等键只认审批单号，第二次同步会命中唯一约束，
    详情永远停在第一次同步时的版本，而且**没有任何报错**。
    """
    first, created_first = create_job(
        session,
        job_type=JobType.DETAIL,
        idempotency_key=build_idempotency_key(JobType.DETAIL, "HT-1", "v1"),
        input_payload=_DETAIL_INPUT,
    )
    second, created_second = create_job(
        session,
        job_type=JobType.DETAIL,
        idempotency_key=build_idempotency_key(JobType.DETAIL, "HT-1", "v2"),
        input_payload=_DETAIL_INPUT,
    )

    assert created_first is True
    assert created_second is True
    assert first.id != second.id

    total = session.execute(
        select(WorkflowJob).where(WorkflowJob.job_type == JobType.DETAIL.value)
    ).scalars().all()
    assert len(total) == 2


def test_hand_made_key_without_version_would_block_the_second_sync(
    session: Session,
) -> None:
    """**反例记录**：手写"只含审批单号"的键会让第二次同步拿不到新作业。

    这条测试演示错误用法的后果，也因此解释了
    `build_idempotency_key()` 为什么要么要求 version、要么强制生成一次性键 ——
    它**不给"忘记传版本"留后门**。
    """
    hand_made = "detail:HT-2026-0001"  # 缺版本段

    _, created_first = create_job(
        session,
        job_type=JobType.DETAIL,
        idempotency_key=hand_made,
        input_payload=_DETAIL_INPUT,
    )
    _, created_second = create_job(
        session,
        job_type=JobType.DETAIL,
        idempotency_key=hand_made,
        input_payload=_DETAIL_INPUT,
    )

    assert created_first is True
    assert created_second is False, (
        "这正是我们要避免的：第二次同步被静默吞掉，详情永远停在旧版本"
    )


def test_create_job_rejects_non_positive_max_attempts(session: Session) -> None:
    """`max_attempts=0` 会让任何瞬时错误都无法重试，任务却静默地直接 blocked。"""
    with pytest.raises(ValueError, match="max_attempts 必须"):
        create_job(
            session,
            job_type=JobType.PULL,
            idempotency_key="pull:mock:default:w1",
            input_payload=_PULL_INPUT,
            max_attempts=0,
        )


def test_create_job_rejects_blank_key(session: Session) -> None:
    with pytest.raises(ValueError, match="幂等键不能为空"):
        create_job(
            session,
            job_type=JobType.PULL,
            idempotency_key="  ",
            input_payload=_PULL_INPUT,
        )


def test_create_job_rejects_empty_input(session: Session) -> None:
    """**空输入必须被拒**：它会让这次作业永远无法回答"基于什么输入"。

    允许空输入的表现很隐蔽：作业照常创建、照常执行、照常成功，
    只是事后回看时 `input_json` 是 `{}` —— 而那时已经无从补救了。
    """
    with pytest.raises(ValueError, match="作业输入不能为空"):
        create_job(
            session,
            job_type=JobType.PULL,
            idempotency_key="pull:mock:default:w1",
            input_payload={},
        )


def test_freeze_job_input_digest_matches_stored_text() -> None:
    """摘要必须由**落库的那串文本**算出，不能各自序列化一次。

    分别计算时可能得到"落库的 JSON 与摘要对应的内容不是同一份"，
    于是"这份结果基于什么输入"有两个互不相同、**而且都看起来合法**的答案。
    """
    payload = {"b": 2, "a": 1}
    text, digest = freeze_job_input(payload)

    assert text == '{"a":1,"b":2}', "键必须排序，否则同一份输入会产出不同摘要"
    assert digest == hashlib.sha256(text.encode("utf-8")).hexdigest()
    # 键序不同、内容相同 → 必须得到同一个摘要
    assert freeze_job_input({"a": 1, "b": 2}) == (text, digest)


def test_job_survives_commit_and_reload(session: Session) -> None:
    """作业必须真正落库（M4 的 Worker 要跨进程读它）。"""
    job, _ = create_job(
        session,
        job_type=JobType.PULL,
        idempotency_key="pull:mock:default:w1",
        input_payload=_PULL_INPUT,
    )
    job_id = job.id
    session.commit()

    session.expire_all()
    reloaded = session.get(WorkflowJob, job_id)

    assert reloaded is not None
    assert reloaded.job_status == JobStatus.QUEUED.value


# ============================================================
# 4. 作业状态流转
# ============================================================


def test_mark_running_increments_attempt_on_start(session: Session) -> None:
    """尝试次数在**开始执行时**递增。

    若改成失败时递增，一个执行到一半进程崩溃的作业重启后会看起来像
    "从未尝试过"，于是它能无限重试 —— 而 `max_attempts` 正是为兜住这种情况而存在。
    """
    job, _ = create_job(
        session,
        job_type=JobType.PARSE,
        idempotency_key="parse:HT-1:1",
        input_payload=_PARSE_INPUT,
    )

    mark_running(session, job)

    assert job.job_status == JobStatus.RUNNING.value
    assert job.attempt_no == 1
    assert job.started_at is not None


def test_mark_succeeded_clears_error_and_stores_checkpoint(session: Session) -> None:
    """成功时要清掉上一次的错误信息，并留存检查点。

    残留的 `last_error_code` 会让人误以为最后一次执行仍失败过。
    """
    job, _ = create_job(
        session,
        job_type=JobType.PARSE,
        idempotency_key="parse:HT-1:1",
        input_payload=_PARSE_INPUT,
    )
    mark_running(session, job)
    mark_failed(
        session, job, error_code=ErrorCode.STORAGE_UNAVAILABLE, message="存储抖动"
    )

    mark_running(session, job)
    mark_succeeded(session, job, checkpoint={"last_page": 7})

    assert job.job_status == JobStatus.SUCCEEDED.value
    assert job.finished_at is not None
    assert job.last_error_code is None
    assert job.last_error_text is None
    assert job.next_retry_at is None
    assert '"last_page": 7' in job.checkpoint_json


def test_transient_error_with_attempts_left_goes_to_retry_wait(
    session: Session,
) -> None:
    """瞬时错误且还有剩余尝试 → `retry_wait` + 指数退避。"""
    job, _ = create_job(
        session,
        job_type=JobType.PARSE,
        idempotency_key="parse:HT-1:1",
        input_payload=_PARSE_INPUT,
    )
    mark_running(session, job)
    moment = datetime(2026, 9, 14, 10, 0, 0)

    status = mark_failed(
        session,
        job,
        error=TransientGatewayError("超时", code=ErrorCode.APPROVAL_API_TIMEOUT),
        now=moment,
    )

    assert status is JobStatus.RETRY_WAIT
    assert job.job_status == JobStatus.RETRY_WAIT.value
    assert job.last_error_code == ErrorCode.APPROVAL_API_TIMEOUT.value
    assert job.next_retry_at == moment + timedelta(seconds=backoff_seconds(1))
    assert job.finished_at is None, "还会重试，不能标记结束时间"


def test_transient_error_with_exhausted_attempts_goes_to_failed(
    session: Session,
) -> None:
    """瞬时错误但尝试次数已耗尽 → `failed`（此时调用方才会让任务 blocked）。"""
    job, _ = create_job(
        session,
        job_type=JobType.PARSE,
        idempotency_key="parse:HT-1:1",
        input_payload=_PARSE_INPUT,
        max_attempts=2,
    )

    for expected_attempt in (1, 2):
        mark_running(session, job)
        assert job.attempt_no == expected_attempt
        status = mark_failed(
            session, job, error_code=ErrorCode.APPROVAL_API_ERROR, message="5xx"
        )
        if expected_attempt < 2:
            assert status is JobStatus.RETRY_WAIT
        else:
            assert status is JobStatus.FAILED
            assert job.next_retry_at is None
            assert job.finished_at is not None


def test_permanent_error_goes_to_failed_immediately(session: Session) -> None:
    """确定性错误**不浪费重试**：重试一万次结果也一样。"""
    job, _ = create_job(
        session,
        job_type=JobType.DOWNLOAD,
        idempotency_key="download:HT-1:A-1",
        input_payload=_DOWNLOAD_INPUT,
        max_attempts=5,
    )
    mark_running(session, job)

    status = mark_failed(
        session,
        job,
        error=PermanentStorageError("路径越界", code=ErrorCode.STORAGE_PATH_INVALID),
    )

    assert status is JobStatus.FAILED
    assert job.next_retry_at is None
    assert job.attempt_no == 1, "确定性错误不应继续消耗尝试次数"


def test_mark_failed_requires_a_code(session: Session) -> None:
    """没有错误码就无法判断"重试还是放弃"，属于调用方缺陷。"""
    job, _ = create_job(
        session,
        job_type=JobType.PARSE,
        idempotency_key="parse:HT-1:1",
        input_payload=_PARSE_INPUT,
    )

    with pytest.raises(ValueError, match="无法判断是否可重试"):
        mark_failed(session, job)


def test_mark_failed_persists_error_text(session: Session) -> None:
    """可读原因要落库，供界面与排障使用。"""
    job, _ = create_job(
        session,
        job_type=JobType.DOWNLOAD,
        idempotency_key="download:HT-1:A-5002",
        input_payload=_DOWNLOAD_INPUT,
    )
    mark_running(session, job)
    mark_failed(
        session,
        job,
        error_code=ErrorCode.ATTACHMENT_MISSING,
        message="附件在审批系统中已被删除",
    )

    assert job.last_error_code == ErrorCode.ATTACHMENT_MISSING.value
    assert job.last_error_text == "附件在审批系统中已被删除"


# ============================================================
# 5. 退避策略
# ============================================================


def test_backoff_grows_exponentially() -> None:
    assert backoff_seconds(1) == 2.0
    assert backoff_seconds(2) == 4.0
    assert backoff_seconds(3) == 8.0


def test_backoff_is_capped() -> None:
    """必须封顶：不封顶的话第 10 次重试要等约 17 分钟。

    到那时人早就该介入看看到底出了什么事 ——
    无限增长的等待会让"重试耗尽 → blocked → 人工处理"这条路径迟迟走不到。
    """
    assert backoff_seconds(10) == 300.0
    assert backoff_seconds(50) == 300.0


def test_backoff_floor_for_invalid_attempt_number() -> None:
    """`attempt_no` 为 0 或负数时按第一次处理，而不是算出比 1 更短的等待。"""
    assert backoff_seconds(0) == backoff_seconds(1)
    assert backoff_seconds(-3) == backoff_seconds(1)
