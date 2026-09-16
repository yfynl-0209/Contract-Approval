"""接入服务测试（M3 / T6）。

覆盖 M3 的第一条完成标志：**连续拉取 3 次，`approval_tasks` 仍只有 6 行**。

另外一个容易被忽略、但后果很实际的约束：

> 重复拉取 / 重复同步必须**无害** —— 不能把已完成的任务打回 `pending`，
> 不能把已下载的附件打回 `pending`，不能把人工确认过的立场抹掉。

这类缺陷不会立刻报错，而是表现为"过一阵子任务状态莫名回退"，
所以每一条都单独写成用例。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.enums import (
    ContextSource,
    ContextStatus,
    DownloadStatus,
    ErrorCode,
    JobStatus,
    JobType,
    TaskStatus,
)
from app.errors import PermanentGatewayError, TransientGatewayError
from app.models import ApprovalAttachment, ApprovalTask, TaskLog, WorkflowJob
from app.ports.approval_gateway import (
    ApprovalDetailDTO,
    AttachmentDTO,
    AuthoritativeContextDTO,
    PendingApprovalDTO,
)
from app.services.pull_service import ApprovalInboundService

# ============================================================
# 夹具
# ============================================================


@pytest.fixture()
def session(work_dir: Path):
    engine = create_engine(
        f"sqlite:///{(work_dir / 'pull.db').as_posix()}", future=True
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db_session:
            yield db_session
    finally:
        engine.dispose()


class _FakeReadGateway:
    """只实现读取端口的假适配器。

    刻意带 `provider` / `tenant_id`：它们是端口的契约成员，
    服务据此构造去重键（而不是自己去读配置）。
    """

    provider = "mock"
    tenant_id = "default"

    def __init__(
        self,
        *,
        pendings: list[PendingApprovalDTO] | None = None,
        detail: ApprovalDetailDTO | None = None,
        error: Exception | None = None,
    ) -> None:
        self.pendings = pendings or []
        self.detail = detail
        self.error = error
        self.calls: list[tuple[str, object]] = []

    def list_pending(self, limit: int) -> list[PendingApprovalDTO]:
        self.calls.append(("list_pending", limit))
        if self.error is not None:
            raise self.error
        return list(self.pendings)[:limit]

    def get_detail(self, instance_id: str) -> ApprovalDetailDTO:
        self.calls.append(("get_detail", instance_id))
        if self.error is not None:
            raise self.error
        assert self.detail is not None, "测试未提供 detail"
        return self.detail

    def download_attachment(self, instance_id: str, attachment_id: str):
        raise NotImplementedError


def _pending(
    code: str,
    *,
    title: str = "测试合同",
    applicant: str = "张三",
    attachments: int = 1,
) -> PendingApprovalDTO:
    return PendingApprovalDTO(
        provider="mock",
        tenant_id="default",
        instance_id=code,
        approval_code=code,
        approval_title=title,
        applicant_name=applicant,
        apply_time="2026-09-01 09:00:00",
        attachment_count=attachments,
    )


def _detail(
    code: str = "HT-2026-0001",
    *,
    context: AuthoritativeContextDTO | None = None,
    form_data: dict | None = None,
    attachments: tuple[AttachmentDTO, ...] | None = None,
) -> ApprovalDetailDTO:
    return ApprovalDetailDTO(
        instance_id=code,
        approval_code=code,
        approval_title="测试合同",
        applicant_name="张三",
        apply_time="2026-09-01 09:00:00",
        context=context
        or AuthoritativeContextDTO(
            our_party_name="示例科技有限公司",
            our_party_contract_label="party_a",
            our_party_business_role="buyer",
            contract_type="procurement",
        ),
        form_data=form_data if form_data is not None else {"amount": "1200000"},
        attachments=attachments
        if attachments is not None
        else (AttachmentDTO(attachment_id="A-1", file_name="c.pdf", file_type="pdf"),),
    )


def _real_fixture_pendings(limit: int = 50) -> list[PendingApprovalDTO]:
    """用 **mock 审批系统的真实 fixture** 构造待办 DTO（无需启动服务）。

    这样"6 条待办"这条验收标准跑在真实数据上，而不是自造的假数据。
    """
    from mock_approval.store import store

    return [
        PendingApprovalDTO(
            provider="mock",
            tenant_id="default",
            instance_id=item.approval_code,
            approval_code=item.approval_code,
            approval_title=item.approval_title,
            applicant_name=item.applicant_name,
            apply_time=item.apply_time,
            attachment_count=len(item.attachments),
        )
        for item in store.list_pending(limit)
    ]


def _tasks(session: Session) -> list[ApprovalTask]:
    session.expire_all()
    return list(session.execute(select(ApprovalTask)).scalars().all())


def _jobs(session: Session) -> list[WorkflowJob]:
    session.expire_all()
    return list(session.execute(select(WorkflowJob)).scalars().all())


def _logs(session: Session) -> list[TaskLog]:
    session.expire_all()
    return list(session.execute(select(TaskLog)).scalars().all())


# ============================================================
# 1. 完成标志：连续拉取 3 次仍 6 行
# ============================================================


def test_three_pulls_keep_only_six_tasks(session: Session) -> None:
    """**M3 第一完成标志**：连续拉取 3 次，`approval_tasks` 仍只有 6 行。

    这条同时验证了"去重"与"幂等"两件事：
    行数不涨，且 task id 完全不变（说明是刷新而不是删除重建 ——
    重建会丢掉附件、解析结果与审查结果的关联）。
    """
    gateway = _FakeReadGateway(pendings=_real_fixture_pendings())
    service = ApprovalInboundService(gateway, session)

    first = service.list_pending_contract_approvals()
    session.commit()
    assert first.fetched == 6
    assert first.created == 6
    assert first.updated == 0

    ids_after_first = sorted(task.id for task in _tasks(session))

    for _ in range(2):
        service.list_pending_contract_approvals()
        session.commit()

    assert len(_tasks(session)) == 6, "重复拉取产生了新任务，去重失效"
    assert sorted(task.id for task in _tasks(session)) == ids_after_first, (
        "任务被删除重建了：这会切断附件、解析结果与审查结果的关联"
    )


def test_later_pulls_report_updates_not_creates(session: Session) -> None:
    """第 2、3 次拉取应当报告"刷新"而不是"新建"。"""
    gateway = _FakeReadGateway(pendings=_real_fixture_pendings())
    service = ApprovalInboundService(gateway, session)

    service.list_pending_contract_approvals()
    session.commit()

    second = service.list_pending_contract_approvals()
    session.commit()

    assert second.created == 0
    assert second.updated == 6


def test_repeated_pull_resets_attempt_counter(session: Session) -> None:
    """同一时间窗口内多次成功拉取后，作业的尝试计数必须归零。

    不归零会让后续**第一次**真实失败就撞上 `max_attempts`，
    被误判为"重试耗尽" —— 重试预算被成功的执行吃光了。
    """
    gateway = _FakeReadGateway(pendings=_real_fixture_pendings())
    service = ApprovalInboundService(gateway, session)

    for _ in range(3):
        service.list_pending_contract_approvals()
        session.commit()

    jobs = _jobs(session)
    assert len(jobs) == 1, "同一时间窗口应当复用同一条作业记录"
    assert jobs[0].job_status == JobStatus.SUCCEEDED.value
    assert jobs[0].attempt_no == 0


def test_pull_result_carries_provider_identity(session: Session) -> None:
    """身份来自**适配器**，不是服务读配置。"""
    gateway = _FakeReadGateway(pendings=[_pending("HT-1")])
    service = ApprovalInboundService(gateway, session)

    result = service.list_pending_contract_approvals()

    assert result.provider == "mock"
    assert result.tenant_id == "default"


def test_blank_approval_code_leaves_no_partial_tasks(session: Session) -> None:
    """入库中途失败不得留下"拉了一半"的任务。

    入库循环用 SAVEPOINT 包住：第 3 条因数据库 CHECK 失败时，
    前两条一起回滚（不会出现 2 条任务的半截状态），
    而作业台账与日志仍然保留（它们在 savepoint 之外）—— 失败必须可查。
    """
    gateway = _FakeReadGateway(
        pendings=[
            _pending("HT-OK-1"),
            _pending("HT-OK-2"),
            # approval_code 为空白 → 触发数据库 CHECK 约束
            _pending("HT-BAD", title=""),
        ]
    )
    # 构造一个空白 approval_code（绕过 PendingApprovalDTO 的正常用法）
    gateway.pendings[2] = PendingApprovalDTO(
        provider="mock",
        tenant_id="default",
        instance_id="HT-BAD",
        approval_code="",
        approval_title="空白编号",
        applicant_name="张三",
        apply_time="2026-09-01",
        attachment_count=0,
    )
    service = ApprovalInboundService(gateway, session)

    with pytest.raises(IntegrityError):
        service.list_pending_contract_approvals()

    assert _tasks(session) == [], "中途失败留下了部分任务"
    assert len(_jobs(session)) == 1, "作业台账应当保留失败记录"
    assert _jobs(session)[0].job_status == JobStatus.FAILED.value


# ============================================================
# 2. 拉取形成的初始状态
# ============================================================


def test_pulled_task_starts_with_missing_context(session: Session) -> None:
    """刚拉取完的任务 `context_status` 必然是 `missing`。

    待办列表接口只给基础字段，**不含**权威上下文 ——
    那些只在详情接口里。因此"基础信息已知、立场未知"是准确状态，
    不是缺陷。
    """
    gateway = _FakeReadGateway(pendings=[_pending("HT-1")])
    service = ApprovalInboundService(gateway, session)

    service.list_pending_contract_approvals()
    session.commit()

    task = _tasks(session)[0]
    assert task.task_status == TaskStatus.PENDING.value
    assert task.context_status == ContextStatus.MISSING.value
    assert task.our_party_name is None
    assert task.form_data_json is None


def test_refresh_does_not_reset_task_status(session: Session) -> None:
    """**重复拉取不得把已完成的任务打回 `pending`。**

    这是"重复拉取只更新不新建"最容易被破坏的一半：
    行数没涨，但状态被重置了，任务会重新走一遍全流程。
    """
    gateway = _FakeReadGateway(pendings=[_pending("HT-1")])
    service = ApprovalInboundService(gateway, session)
    service.list_pending_contract_approvals()
    session.commit()

    task = _tasks(session)[0]
    task.task_status = TaskStatus.DONE.value
    session.commit()

    service.list_pending_contract_approvals()
    session.commit()

    assert _tasks(session)[0].task_status == TaskStatus.DONE.value


def test_refresh_updates_pull_sourced_fields(session: Session) -> None:
    """待办列表来源的字段确实被刷新（否则"只更新不新建"就是空话）。"""
    gateway = _FakeReadGateway(pendings=[_pending("HT-1", title="旧标题")])
    service = ApprovalInboundService(gateway, session)
    service.list_pending_contract_approvals()
    session.commit()

    gateway.pendings = [_pending("HT-1", title="新标题", applicant="李四")]
    service.list_pending_contract_approvals()
    session.commit()

    task = _tasks(session)[0]
    assert task.approval_title == "新标题"
    assert task.applicant_name == "李四"


def test_refresh_does_not_touch_context_fields(session: Session) -> None:
    """拉取**不碰**权威上下文 —— 它只来自详情接口。

    若拉取把上下文清空，"已完成详情同步的任务被一次例行拉取打回 missing"，
    而 missing 会让方向敏感规则全部转为待人工判断。
    """
    gateway = _FakeReadGateway(
        pendings=[_pending("HT-1")], detail=_detail("HT-1")
    )
    service = ApprovalInboundService(gateway, session)
    service.list_pending_contract_approvals()
    service.get_contract_approval("HT-1")
    session.commit()
    assert _tasks(session)[0].context_status == ContextStatus.COMPLETE.value

    service.list_pending_contract_approvals()
    session.commit()

    task = _tasks(session)[0]
    assert task.context_status == ContextStatus.COMPLETE.value
    assert task.our_party_name == "示例科技有限公司"
    assert task.form_data_json is not None


def test_limit_must_be_positive(session: Session) -> None:
    service = ApprovalInboundService(_FakeReadGateway(), session)

    with pytest.raises(ValueError, match="limit 必须"):
        service.list_pending_contract_approvals(limit=0)


# ============================================================
# 3. 详情同步
# ============================================================


def test_detail_sync_advances_context_to_complete(session: Session) -> None:
    """详情同步后上下文推进到 `complete`，四个业务事实落库。"""
    gateway = _FakeReadGateway(pendings=[_pending("HT-1")], detail=_detail("HT-1"))
    service = ApprovalInboundService(gateway, session)
    service.list_pending_contract_approvals()
    session.commit()

    result = service.get_contract_approval("HT-1")
    session.commit()

    assert result.context_status == ContextStatus.COMPLETE.value
    assert result.our_party_name == "示例科技有限公司"
    assert result.our_party_contract_label == "party_a"
    assert result.our_party_business_role == "buyer"
    assert result.contract_type == "procurement"
    assert result.task_status == TaskStatus.PENDING.value


def test_detail_sync_persists_form_data(session: Session) -> None:
    """审批表单必须落库：外部系统不可用时历史详情仍可查看。"""
    gateway = _FakeReadGateway(
        detail=_detail("HT-9", form_data={"amount": "1200000", "dept": "采购部"})
    )
    service = ApprovalInboundService(gateway, session)

    service_created = service.get_contract_approval("HT-9")
    session.commit()

    task = _tasks(session)[0]
    assert task.form_data_json is not None
    assert "1200000" in task.form_data_json
    assert service_created.form_data == {"amount": "1200000", "dept": "采购部"}


def test_detail_sync_creates_task_when_not_pulled(session: Session) -> None:
    """未拉取就先查详情，应当**自动建任务**而不是报"任务不存在"。

    详情接口携带了建任务所需的全部基础字段，因此报错只会给调用方添麻烦。
    """
    gateway = _FakeReadGateway(detail=_detail("HT-DIRECT"))
    service = ApprovalInboundService(gateway, session)

    result = service.get_contract_approval("HT-DIRECT")
    session.commit()

    assert len(_tasks(session)) == 1
    assert result.instance_id == "HT-DIRECT"
    assert result.context_status == ContextStatus.COMPLETE.value


def test_detail_sync_can_run_again_after_values_change(session: Session) -> None:
    """**详情变化后必须能再次同步**（幂等键语义在服务层的体现）。

    这是最早那个死锁风险的服务级反例：若"同一审批单只能同步一次"，
    第二次同步会被静默吞掉，任务永远停在第一次的立场上 ——
    而立场错误会让方向敏感的规则结论全线反转。
    """
    gateway = _FakeReadGateway(detail=_detail("HT-1"))
    service = ApprovalInboundService(gateway, session)
    service.get_contract_approval("HT-1")
    session.commit()

    # 外部系统的数据变了
    gateway.detail = _detail(
        "HT-1",
        context=AuthoritativeContextDTO(
            our_party_name="示例科技有限公司",
            our_party_contract_label="party_b",
            our_party_business_role="seller",
            contract_type="sales",
        ),
    )

    result = service.get_contract_approval("HT-1")
    session.commit()

    assert result.our_party_contract_label == "party_b"
    assert result.our_party_business_role == "seller"
    assert len(_tasks(session)) == 1, "应当是更新既有任务，而不是新建"


def test_missing_context_field_keeps_status_missing(session: Session) -> None:
    """四项业务事实缺任一 → `missing`，而不是 `complete`。

    若判成 complete，方向敏感的规则会拿着空立场去判断，
    结论看似正常、实际全线错位。
    """
    gateway = _FakeReadGateway(
        detail=_detail(
            "HT-1",
            context=AuthoritativeContextDTO(
                our_party_name="示例科技有限公司",
                our_party_contract_label="party_a",
                our_party_business_role=None,  # 缺一项
                contract_type="procurement",
            ),
        )
    )
    service = ApprovalInboundService(gateway, session)

    result = service.get_contract_approval("HT-1")

    assert result.context_status == ContextStatus.MISSING.value


def test_confirmed_context_survives_unchanged_resync(session: Session) -> None:
    """人工确认过的立场，在外部取值未变时必须保留 `confirmed`。

    否则详情每同步一次就把确认抹掉一次，
    回写门禁（M6 要求 `context_status=valid`）会莫名其妙地失败 ——
    而使用者"明明刚点过确认"，这类故障极难排查。
    """
    gateway = _FakeReadGateway(detail=_detail("HT-1"))
    service = ApprovalInboundService(gateway, session)
    service.get_contract_approval("HT-1")
    session.commit()

    task = _tasks(session)[0]
    task.context_status = ContextStatus.CONFIRMED.value
    session.commit()

    service.get_contract_approval("HT-1")
    session.commit()

    assert _tasks(session)[0].context_status == ContextStatus.CONFIRMED.value


def test_changed_declaration_becomes_conflict_not_a_silent_override(
    session: Session,
) -> None:
    """⚠️ 人工背书过的立场与新声明不一致 → **`conflict`**，绝不静默覆盖。

    这条守的是一个**没有症状**的错误（2026-09-15 实测修正）：

    ```text
    人工把业务角色改成 seller（"这份合同我方是卖方"）
      → 审批系统同步回来仍是 buyer（它没变过）
      → 旧口径覆盖人工值、把状态重置为 complete
      → 而 complete 也在 _TRUSTED_CONTEXT 里 → 回写门禁照常放行
      → 规则方向被判反（seller 该判的不判、buyer 的误命中）
      → 报告上看不出任何异常
    ```

    旧口径在"人工只能确认、不能改值"时是安全的（`confirmed` 只可能是系统取值的
    镜像）。有了"人工修正"之后就不成立了 —— 因此这一条不是换个状态码，
    而是把"人说过的话被机器悄悄改回去"这件事**变成不可发生**。
    """
    gateway = _FakeReadGateway(detail=_detail("HT-1"))
    service = ApprovalInboundService(gateway, session)
    service.get_contract_approval("HT-1")
    session.commit()

    task = _tasks(session)[0]
    # 人工背书：把业务角色改成 seller
    task.our_party_business_role = "seller"
    task.context_status = ContextStatus.CONFIRMED.value
    task.context_source = ContextSource.MANUAL.value
    session.commit()

    gateway.detail = _detail(
        "HT-1",
        context=AuthoritativeContextDTO(
            our_party_name="示例科技有限公司",
            our_party_contract_label="party_a",
            our_party_business_role="buyer",  # ← 系统仍声明 buyer
            contract_type="procurement",
        ),
    )

    result = service.get_contract_approval("HT-1")
    session.commit()

    assert result.context_status == ContextStatus.CONFLICT.value
    # 人工那一组值**必须保留**：裁定结果以人的判断为准
    assert result.our_party_business_role == "seller"
    # 门禁行为（`conflict` → 拒绝回写）由 `test_writeback_service.py` 的参数化
    # 用例守着（那里直接跑门禁函数）；这里刻意**不**去 import 私有的
    # `_TRUSTED_CONTEXT` —— 跨模块引用私有名正是本项目踩过的坑
    # （改名方与引用方各自都"看起来没问题"）


def test_conflict_record_carries_both_sides(session: Session) -> None:
    """冲突记录必须能说出"**哪两个值**冲突"。

    只说"冲突了"时，人的下一步只能是猜（或去翻审计）——
    而这一屏的全部意义就是让他快速裁定。
    """
    gateway = _FakeReadGateway(detail=_detail("HT-1"))
    service = ApprovalInboundService(gateway, session)
    service.get_contract_approval("HT-1")
    session.commit()

    task = _tasks(session)[0]
    task.our_party_name = "人工写的公司名"
    task.context_status = ContextStatus.CONFIRMED.value
    task.context_source = ContextSource.MANUAL.value
    session.commit()

    service.get_contract_approval("HT-1")
    session.commit()

    conflict = json.loads(_tasks(session)[0].context_conflict_json)
    assert conflict["declared"]["our_party_name"] == "示例科技有限公司"
    assert conflict["confirmed"]["our_party_name"] == "人工写的公司名"


def test_conflict_clears_when_the_declaration_agrees_again(session: Session) -> None:
    """声明与人工背书重新一致 → 回到 `confirmed`，并清掉冲突记录。

    ⚠️ 清掉很重要：留着它，下一个人会以为冲突还在 ——
    而"状态说没事、旁边挂着一份冲突对照"是最难解释的一种组合。
    """
    gateway = _FakeReadGateway(detail=_detail("HT-1"))
    service = ApprovalInboundService(gateway, session)
    service.get_contract_approval("HT-1")
    session.commit()

    task = _tasks(session)[0]
    task.our_party_business_role = "seller"
    task.context_status = ContextStatus.CONFIRMED.value
    task.context_source = ContextSource.MANUAL.value
    task.context_conflict_json = '{"declared": {}, "confirmed": {}}'
    session.commit()

    # 系统这次声明的正是人工背书的那一组
    gateway.detail = _detail(
        "HT-1",
        context=AuthoritativeContextDTO(
            our_party_name="示例科技有限公司",
            our_party_contract_label="party_a",
            our_party_business_role="seller",
            contract_type="procurement",
        ),
    )

    result = service.get_contract_approval("HT-1")
    session.commit()

    assert result.context_status == ContextStatus.CONFIRMED.value
    # `context_conflict_json` 是**内部裁定辅助**，不在这条服务层返回 DTO 里
    # （它经由 `GET /api/tasks/{id}` 的 `context_conflict` 下发给控制台）
    assert _tasks(session)[0].context_conflict_json is None


def test_incomplete_declaration_does_not_break_a_confirmed_context(
    session: Session,
) -> None:
    """本次声明不完整 → 人工裁定仍然成立（"**没拿到**"≠"**拿到了别的**"）。

    把它也算冲突，会让审批系统某次少给一个字段就把所有已确认的立场打回待裁定 ——
    那是一次上游降级变成全量人工重做。
    """
    gateway = _FakeReadGateway(detail=_detail("HT-1"))
    service = ApprovalInboundService(gateway, session)
    service.get_contract_approval("HT-1")
    session.commit()

    task = _tasks(session)[0]
    task.our_party_business_role = "seller"
    task.context_status = ContextStatus.CONFIRMED.value
    task.context_source = ContextSource.MANUAL.value
    session.commit()

    gateway.detail = _detail(
        "HT-1",
        # 四个字段全缺（真实审批系统未必提供全部四项）
        context=AuthoritativeContextDTO(),
    )

    result = service.get_contract_approval("HT-1")
    session.commit()

    assert result.context_status == ContextStatus.CONFIRMED.value
    assert result.our_party_business_role == "seller"


# ============================================================
# 4. 附件元数据
# ============================================================


def test_detail_sync_creates_attachment_metadata(session: Session) -> None:
    """附件**元数据**落库，状态为 pending —— 字节下载是工具 3 的职责。"""
    gateway = _FakeReadGateway(detail=_detail("HT-1"))
    service = ApprovalInboundService(gateway, session)

    result = service.get_contract_approval("HT-1")
    session.commit()

    assert result.attachments[0].is_new is True
    assert result.attachments[0].download_status == DownloadStatus.PENDING.value

    attachment = session.execute(select(ApprovalAttachment)).scalar_one()
    assert attachment.attachment_id == "A-1"
    assert attachment.download_status == DownloadStatus.PENDING.value
    assert attachment.object_key is None, "详情同步不该触发下载"


def test_resync_does_not_reset_download_status(session: Session) -> None:
    """重复同步详情**不得**把已下载的附件打回 `pending`。

    打回 pending 会让"附件已经下载好了"凭空消失，
    系统重新下载一遍 —— 而重复同步详情本来就是常态。
    """
    gateway = _FakeReadGateway(detail=_detail("HT-1"))
    service = ApprovalInboundService(gateway, session)
    service.get_contract_approval("HT-1")
    session.commit()

    # 模拟工具 3 已经下载完成
    attachment = session.execute(select(ApprovalAttachment)).scalar_one()
    attachment.download_status = DownloadStatus.SUCCESS.value
    attachment.object_key = "sha256/aa/bb/x.pdf"
    attachment.file_checksum = "a" * 64
    session.commit()

    result = service.get_contract_approval("HT-1")
    session.commit()

    attachment = session.execute(select(ApprovalAttachment)).scalar_one()
    assert attachment.download_status == DownloadStatus.SUCCESS.value
    assert attachment.object_key == "sha256/aa/bb/x.pdf"
    assert attachment.file_checksum == "a" * 64
    assert result.attachments[0].is_new is False


def test_unavailable_attachment_gets_a_hint_without_overwriting_errors(
    session: Session,
) -> None:
    """外部标记不可用时写入提示，但**不覆盖**真正的下载失败原因。

    不可用是"外部系统告诉我们的事实"，下载失败是"我们尝试后的结果"。
    后者信息量更大，不能被覆盖掉。
    """
    gateway = _FakeReadGateway(
        detail=_detail(
            "HT-1",
            attachments=(
                AttachmentDTO(
                    attachment_id="A-5002",
                    file_name="gone.pdf",
                    file_type="pdf",
                    available=False,
                ),
            ),
        )
    )
    service = ApprovalInboundService(gateway, session)
    service.get_contract_approval("HT-1")
    session.commit()

    attachment = session.execute(select(ApprovalAttachment)).scalar_one()
    assert attachment.error_message == "审批系统已将该附件标记为不可用"

    # 工具 3 尝试下载并失败 → 写入真实失败原因
    attachment.download_status = DownloadStatus.FAILED.value
    attachment.error_message = "附件在审批系统中已被删除"
    session.commit()

    service.get_contract_approval("HT-1")
    session.commit()

    attachment = session.execute(select(ApprovalAttachment)).scalar_one()
    assert attachment.error_message == "附件在审批系统中已被删除", (
        "重复同步覆盖了真正的下载失败原因"
    )


# ============================================================
# 5. 失败处理
# ============================================================


def test_transient_gateway_error_marks_job_retry_wait(session: Session) -> None:
    """瞬时错误 → 作业进入 `retry_wait` 并带上下次重试时间。"""
    gateway = _FakeReadGateway(
        error=TransientGatewayError("超时", code=ErrorCode.APPROVAL_API_TIMEOUT)
    )
    service = ApprovalInboundService(gateway, session)

    with pytest.raises(TransientGatewayError):
        service.list_pending_contract_approvals()

    job = _jobs(session)[0]
    assert job.job_status == JobStatus.RETRY_WAIT.value
    assert job.last_error_code == ErrorCode.APPROVAL_API_TIMEOUT.value
    assert job.next_retry_at is not None
    assert _tasks(session) == []


def test_permanent_gateway_error_marks_job_failed(session: Session) -> None:
    """确定性错误 → 直接 `failed`，**不浪费重试**。"""
    gateway = _FakeReadGateway(
        error=PermanentGatewayError("鉴权失败", code=ErrorCode.AUTH_FAILED)
    )
    service = ApprovalInboundService(gateway, session)

    with pytest.raises(PermanentGatewayError):
        service.list_pending_contract_approvals()

    job = _jobs(session)[0]
    assert job.job_status == JobStatus.FAILED.value
    assert job.next_retry_at is None


def test_failure_writes_structured_error_log(session: Session) -> None:
    """失败必须留下**带错误码**的日志，而不是只有一句中文。"""
    gateway = _FakeReadGateway(
        error=TransientGatewayError("连不上", code=ErrorCode.APPROVAL_UNREACHABLE)
    )
    service = ApprovalInboundService(gateway, session)

    with pytest.raises(TransientGatewayError):
        service.list_pending_contract_approvals()

    entry = next(entry for entry in _logs(session) if entry.log_level == "error")
    assert entry.error_code == ErrorCode.APPROVAL_UNREACHABLE.value
    assert entry.log_type == "pull"


# ============================================================
# 6. 日志
# ============================================================


def test_successful_pull_writes_summary_log(session: Session) -> None:
    gateway = _FakeReadGateway(pendings=_real_fixture_pendings())
    service = ApprovalInboundService(gateway, session)

    service.list_pending_contract_approvals()
    session.commit()

    entry = next(entry for entry in _logs(session) if entry.log_type == "pull")
    assert "共 6 条" in entry.log_content
    assert entry.error_code is None


def test_detail_log_does_not_contain_form_data(session: Session) -> None:
    """详情日志只记"有没有表单数据"，**不下发内容**。

    这里的保障**来自"不传"而不是"脱敏"**：`form_data` 里可能含
    人员姓名、证件号、联系方式，而姓名之类的普通文本不会被任何脱敏规则命中。
    脱敏是第二层防线，第一层是"根本不传"。
    """
    gateway = _FakeReadGateway(
        detail=_detail(
            "HT-1",
            form_data={"applicant": "张三", "phone": "13800138000"},
        )
    )
    service = ApprovalInboundService(gateway, session)

    service.get_contract_approval("HT-1")
    session.commit()

    entry = next(entry for entry in _logs(session) if entry.log_type == "detail")
    assert "张三" not in entry.log_content
    assert "13800138000" not in entry.log_content
    assert "has_form_data" in entry.log_content


# ============================================================
# 7. 详情作业台账（验收标准 6 / 13）
# ============================================================


def _detail_jobs(session: Session) -> list[WorkflowJob]:
    session.expire_all()
    return [
        job
        for job in session.execute(select(WorkflowJob)).scalars().all()
        if job.job_type == JobType.DETAIL.value
    ]


def test_detail_sync_writes_a_job(session: Session) -> None:
    """**验收 13**：详情同步同样写一条作业台账。

    写台账 ≠ 入队：详情仍然是同步执行、当场完成，
    M4 的 Worker 不会消费它（它从不进入 `queued` 等待态）。

    台账的意义是"这次同步发生过"，以及失败时留下可查的记录。
    """
    gateway = _FakeReadGateway(detail=_detail("HT-1"))
    service = ApprovalInboundService(gateway, session)

    service.get_contract_approval("HT-1")
    session.commit()

    jobs = _detail_jobs(session)
    assert len(jobs) == 1
    assert jobs[0].job_status == JobStatus.SUCCEEDED.value
    assert jobs[0].attempt_no == 0, "成功作业的尝试次数归零，下一轮是全新预算"
    assert jobs[0].task_id == _tasks(session)[0].id, "作业应挂到同步出来的任务上"


def test_repeated_detail_sync_reuses_the_job(session: Session) -> None:
    """同一输入版本重复同步**不新建作业**（验收 13 的后半句）。"""
    gateway = _FakeReadGateway(detail=_detail("HT-1"))
    service = ApprovalInboundService(gateway, session)

    for _ in range(3):
        service.get_contract_approval("HT-1")
        session.commit()

    # 第 1 次用 "first" 版本建作业，第 2 次起版本稳定在已存上下文的摘要上
    assert len(_detail_jobs(session)) == 2, "版本稳定后必须复用同一条作业"


def test_changed_context_produces_a_new_job(session: Session) -> None:
    """**验收 6**：详情变化后必须能产生**新作业**并成功。

    这是"作业幂等键不得只含 `instance_id`"的服务级反例：若键只认审批单号，
    第二次同步会被唯一约束**永久拒绝**，任务永远停在第一次的立场上 ——
    而立场错误会让方向敏感的规则全线反转，且**静默反转、无人察觉**。

    任务侧的变化已由 `test_detail_sync_can_run_again_after_values_change` 覆盖，
    这里只补**作业**这一层。

    版本语义刻意与下载作业一致（都取**已存**值）：数据变化后的第一次同步
    必然拿到新键，不会被吞掉。
    """
    gateway = _FakeReadGateway(detail=_detail("HT-1"))
    service = ApprovalInboundService(gateway, session)

    service.get_contract_approval("HT-1")
    session.commit()
    first = _detail_jobs(session)[0]

    # 外部系统的权威上下文变了：我方从"甲方采购方"变成"乙方供货方"
    gateway.detail = _detail(
        "HT-1",
        context=AuthoritativeContextDTO(
            our_party_name="示例科技有限公司",
            our_party_contract_label="party_b",
            our_party_business_role="seller",
            contract_type="sales",
        ),
    )
    service.get_contract_approval("HT-1")
    session.commit()

    jobs = _detail_jobs(session)
    assert len(jobs) == 2, "上下文变化后必须产生新作业，否则这次同步被静默吞掉"
    assert jobs[1].id != first.id

    task = _tasks(session)[0]
    assert task.our_party_contract_label == "party_b"
    assert task.our_party_business_role == "seller"


def test_detail_failure_leaves_a_job_record(session: Session) -> None:
    """详情失败**必须留下痕迹**。

    缺了这条：拉取失败有作业记录、下载失败有作业记录，
    唯独详情失败在库里查不到任何东西 —— 而"这个单子为什么一直没同步上"
    恰恰是最需要线索的问题。
    """
    gateway = _FakeReadGateway(
        detail=_detail("HT-1"),
        error=TransientGatewayError("连不上", code=ErrorCode.APPROVAL_UNREACHABLE),
    )
    service = ApprovalInboundService(gateway, session)

    with pytest.raises(TransientGatewayError):
        service.get_contract_approval("HT-1")
    session.commit()

    job = _detail_jobs(session)[0]
    assert job.job_status == JobStatus.RETRY_WAIT.value
    assert job.last_error_code == ErrorCode.APPROVAL_UNREACHABLE.value

    # 详情取不到，就不该留下一条空任务
    assert _tasks(session) == []

    entry = next(entry for entry in _logs(session) if entry.log_level == "error")
    assert entry.error_code == ErrorCode.APPROVAL_UNREACHABLE.value


def test_detail_failure_does_not_block_any_task(session: Session) -> None:
    """详情是同步读取，失败**不阻塞任务**。

    它没有"卡在中间某一步"的状态，随时可以再调一次；
    把一次读取失败升级成需要人工介入的工单，代价远大于重试一次。
    这条与拉取/下载失败的处置刻意不同，所以单独写一条。
    """
    gateway = _FakeReadGateway(pendings=[_pending("HT-1")])
    service = ApprovalInboundService(gateway, session)
    service.list_pending_contract_approvals()
    session.commit()

    gateway.error = PermanentGatewayError(
        "审批单不存在", code=ErrorCode.INSTANCE_NOT_FOUND
    )
    with pytest.raises(PermanentGatewayError):
        service.get_contract_approval("HT-1")
    session.commit()

    task = _tasks(session)[0]
    assert task.task_status == TaskStatus.PENDING.value, "详情失败不该阻塞任务"
    assert task.blocked_stage is None
