"""接入模块 —— 待办拉取与详情同步（工具 1 / 工具 2 的业务实现，企业化设计 §5.1）。

负责唯一键去重与审批字段标准化，输出内部 DTO，
**不把厂商原始字段暴露给解析与规则模块**。

## 唯一的写权威审查上下文的地方

`our_party_name` / `our_party_contract_label` / `our_party_business_role` /
`contract_type` 是**业务事实**，由申请人填写、经审批流程确认。
解析模块（M4）只能**读**并用解析结果做交叉核验，绝不能写入 ——
让不确定的来源去驱动方向敏感的规则集，会让 18 条规则结论**静默全线反转**。

## 刚拉取完的任务 `context_status` 必然是 `missing`

待办列表接口只给基础字段，**不含**权威上下文（那些只在详情接口里）。
所以拉取后处于"基础信息已知、立场未知"的状态 —— 这是**正确行为**，
它准确描述了"我们还不知道我方是谁"；详情同步后才推进到 `complete`。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.enums import (
    ContextSource,
    ContextStatus,
    DownloadStatus,
    ErrorCode,
    JobType,
    LogType,
    TaskStatus,
)
from app.errors import AppError
from app.models import ApprovalAttachment, ApprovalTask
from app.ports.approval_gateway import (
    ApprovalDetailDTO,
    ApprovalReadGateway,
    AttachmentDTO,
    AuthoritativeContextDTO,
    PendingApprovalDTO,
)
from app.services.dto import (
    ApprovalDetailResult,
    AttachmentSummary,
    PullResult,
    TaskOutcome,
)
from app.services.log_service import LogService


def _context_payload(
    our_party_name: str | None,
    our_party_contract_label: str | None,
    our_party_business_role: str | None,
    contract_type: str | None,
) -> dict[str, str | None]:
    """四项业务事实的**具名**表示（写进 `context_conflict_json`）。

    做成具名对象而不是数组：数组的顺序由解包决定，将来加一项或调一次顺序，
    库里已有的记录就会**静默错位**（"我方名称"显示成合同类型），而它不报错。
    """
    return {
        "our_party_name": our_party_name,
        "our_party_contract_label": our_party_contract_label,
        "our_party_business_role": our_party_business_role,
        "contract_type": contract_type,
    }
from app.workflow.jobs import (
    build_idempotency_key,
    create_job,
    mark_failed,
    mark_running,
    mark_succeeded,
    pull_window_token,
)


class ApprovalInboundService:
    """接入模块的应用服务。

    依赖的是**窄接口** `ApprovalReadGateway`（只有三个读取方法），
    而不是读写兼备的组合端口 —— 这样"只被授予读取权限"的审批系统
    也能正常驱动本模块（企业化设计 §10.1）。
    """

    def __init__(
        self,
        gateway: ApprovalReadGateway,
        session: Session,
        *,
        log: LogService | None = None,
    ) -> None:
        """
        Args:
            gateway: 审批系统读取端口（适配器）。
            session: 数据库会话（提交由调用方负责）。
            log: 日志服务；缺省用同一会话新建一个。

        `provider` / `tenant_id` **取自适配器**，而不是本服务读配置：
        适配器才是"我在跟谁对接"的权威。若两者各读一份，
        同一个审批单会被重复建任务，而两边的去重键永远对不上。
        """
        self._gateway = gateway
        self._session = session
        self._log = log or LogService(session)
        self._provider = gateway.provider
        self._tenant_id = gateway.tenant_id

    # ------------------------------------------------------------------
    # 工具 1：待办拉取
    # ------------------------------------------------------------------
    def list_pending_contract_approvals(self, limit: int = 20) -> PullResult:
        """拉取待处理审批单列表，并按 `(provider, tenant_id, instance_id)` 去重入库。

        Args:
            limit: 拉取条数上限（≥ 1）。

        Returns:
            `PullResult`，含本次新建/刷新的统计。

        Raises:
            ValueError: `limit` 非法（编程错误）。
            AppError: 外部系统调用失败（已记入作业台账并写日志）。

        **去重语义（需求 2.4.4）**：已存在则只更新审批单本身的字段，
        不重建任务、不清空已有解析结果、不重置任务状态 ——
        重复拉取必须是无害的。
        """
        if limit < 1:
            raise ValueError(f"limit 必须 ≥ 1，收到 {limit}")

        job, _ = self._open_pull_job()
        mark_running(self._session, job)

        try:
            pendings = tuple(self._gateway.list_pending(limit))
            # 用 SAVEPOINT 包住入库：中途失败时不会留下"拉了一半"的任务，
            # 而作业台账的失败记录仍然保留（它在 savepoint 之外）
            with self._session.begin_nested():
                outcomes = tuple(self._upsert_task(dto) for dto in pendings)
        except AppError as exc:
            self._abort_job(job, exc.code, exc.message)
            raise
        except Exception as exc:  # noqa: BLE001
            # 非业务异常也必须给作业收尾：否则作业会停在 running，
            # 看起来像"正在执行"，把后续排查引向错误方向
            self._abort_job(
                job,
                ErrorCode.INVALID_GATEWAY_RESPONSE,
                f"待办拉取出现未预期异常：{exc}",
            )
            raise

        mark_succeeded(self._session, job)

        created = sum(1 for outcome in outcomes if outcome.created)
        result = PullResult(
            provider=self._provider,
            tenant_id=self._tenant_id,
            fetched=len(pendings),
            created=created,
            updated=len(outcomes) - created,
            items=outcomes,
            job_idempotency_key=job.idempotency_key,
        )

        self._log.log(
            log_type=LogType.PULL,
            message=(
                f"待办拉取完成：共 {result.fetched} 条，"
                f"新建 {result.created} 条，刷新 {result.updated} 条"
            ),
            payload={
                "provider": self._provider,
                "tenant_id": self._tenant_id,
                "task_ids": list(result.task_ids),
            },
        )
        self._session.flush()
        return result

    # ------------------------------------------------------------------
    # 工具 2：详情同步
    # ------------------------------------------------------------------
    def get_contract_approval(self, instance_id: str) -> ApprovalDetailResult:
        """查询审批单详情，并同步权威上下文、审批表单与附件元数据。

        **本方法会写库**（不只是查询）：详情接口是权威上下文与附件清单的唯一来源，
        不落库的话，外部系统不可用或该实例被删除后控制台就什么都看不到了。
        任务不存在时会**自动创建** —— 详情本身携带了建任务所需的全部基础字段，
        因此"没拉取先查详情"是正常用法。

        **同步执行，但同样写作业台账**：写台账 ≠ 入队，作业创建后立即置为成功，
        M4 的 Worker 不会消费它。缺了台账，**详情同步失败在库里不留任何痕迹** ——
        拉取失败有记录、下载失败有记录，唯独详情失败查不到。

        Raises:
            AppError: 外部系统调用失败。**不写任何任务数据**，
                但作业台账会留下失败记录与稳定错误码。
        """
        job = self._open_detail_job(instance_id)
        mark_running(self._session, job)

        try:
            detail = self._gateway.get_detail(instance_id)
        except AppError as exc:
            self._abort_detail_job(job, exc)
            raise

        seed = self._pending_from_detail(detail)
        with self._session.begin_nested():
            outcome = self._upsert_task(seed)
            task = self._session.get(ApprovalTask, outcome.task_id)
            if task is None:  # pragma: no cover - flush 后必然存在
                raise RuntimeError(f"任务 {outcome.task_id} 在 flush 后不可见")

            context_status = self._apply_context(task, detail.context)
            task.form_data_json = _dump_json(detail.form_data)
            attachments = self._upsert_attachments(task, detail.attachments)

        # 任务可能是**本次同步才建出来的**，只有到这一步才知道它的 id。
        # 不补这次回填，详情作业会永远 `task_id=None`：
        #   - 按任务查作业历史时看不到它（"这个单子同步过几次"查不出来）；
        #   - 删任务时 `ON DELETE CASCADE` 也带不走它，留下孤儿行。
        job.task_id = task.id
        mark_succeeded(self._session, job)
        self._log.log(
            log_type=LogType.DETAIL,
            message=(
                f"详情同步完成：{detail.approval_code}，"
                f"上下文状态 {context_status}，附件 {len(attachments)} 个"
            ),
            task_id=task.id,
            payload={
                "is_new_task": outcome.created,
                # ⚠️ form_data 不进日志：其中可能含人员姓名、证件号、联系方式。
                # 这里只记录"有没有表单数据"，不下发内容。
                "has_form_data": bool(detail.form_data),
            },
        )
        self._session.flush()

        return ApprovalDetailResult(
            task_id=task.id,
            instance_id=task.instance_id,
            approval_code=task.approval_code,
            task_status=task.task_status,
            context_status=context_status,
            our_party_name=task.our_party_name,
            our_party_contract_label=task.our_party_contract_label,
            our_party_business_role=task.our_party_business_role,
            contract_type=task.contract_type,
            form_data=detail.form_data,
            attachments=attachments,
        )

    # ------------------------------------------------------------------
    # 内部：作业
    # ------------------------------------------------------------------
    def _open_pull_job(self):
        """创建/复用**本时间窗口**的拉取作业。

        拉取是**批量**操作，且天然带时间窗口（M4 的 Scheduler 会周期性触发它），
        窗口使"同一分钟内的重复触发"合流为同一条作业记录。
        """
        identity = f"{self._provider}:{self._tenant_id}"
        key = build_idempotency_key(
            JobType.PULL,
            identity,
            pull_window_token(minutes=settings.pull_window_minutes),
        )
        job, _ = create_job(
            self._session,
            job_type=JobType.PULL,
            idempotency_key=key,
            input_payload={"provider": self._provider, "tenant_id": self._tenant_id},
        )
        return job, key

    def _open_detail_job(self, instance_id: str):
        """创建/复用该审批单**当前输入版本**的详情作业。

        幂等键 = `detail:{provider}:{tenant}:{instance_id}:{已存上下文版本}`。
        版本语义见 `_context_version`：**数据变化后的第一次同步一定会拿到新作业**。
        """
        task = self._find_task(self._provider, self._tenant_id, instance_id)
        key = build_idempotency_key(
            JobType.DETAIL,
            f"{self._provider}:{self._tenant_id}:{instance_id}",
            _context_version(task),
        )
        job, _ = create_job(
            self._session,
            job_type=JobType.DETAIL,
            idempotency_key=key,
            input_payload={
                "provider": self._provider,
                "tenant_id": self._tenant_id,
                "instance_id": instance_id,
            },
            task_id=None if task is None else task.id,
        )
        return job

    def _abort_detail_job(self, job, error: AppError) -> None:
        """详情失败的收尾：作业置失败 + 结构化错误码日志。

        **不阻塞任务**：详情是一次同步读取，没有"卡在中间某一步"的状态，
        随时可以再调一次。把一次读取失败升级成需要人工介入的工单，
        代价远大于重试一次。

        作业状态仍由 `mark_failed` 按可重试性决定：瞬时错误且预算未耗尽
        进入 `retry_wait`（下一次调用会复用同一条作业重试），
        确定性错误直接 `failed`，不浪费重试预算。
        """
        status = mark_failed(self._session, job, error=error)
        self._log.log_error(
            log_type=LogType.DETAIL,
            message=f"详情同步失败：{error.message}",
            error_code=error.code,
            payload={
                "job_status": status.value,
                "attempt_no": job.attempt_no,
                "max_attempts": job.max_attempts,
                "next_retry_at": _iso(job.next_retry_at),
            },
        )

    def _abort_job(self, job, code: ErrorCode, message: str) -> None:
        """失败收尾：更新作业状态 + 记录结构化错误码日志。"""
        status = mark_failed(self._session, job, error_code=code, message=message)
        self._log.log_error(
            log_type=LogType.PULL,
            message=f"待办拉取失败：{message}",
            error_code=code,
            payload={
                "job_status": status.value,
                "attempt_no": job.attempt_no,
                "max_attempts": job.max_attempts,
                "next_retry_at": _iso(job.next_retry_at),
            },
        )

    # ------------------------------------------------------------------
    # 内部：任务去重
    # ------------------------------------------------------------------
    def _upsert_task(self, dto: PendingApprovalDTO) -> TaskOutcome:
        """按 `(provider, tenant_id, instance_id)` 去重后写入任务。"""
        existing = self._find_task(dto.provider, dto.tenant_id, dto.instance_id)
        if existing is not None:
            self._refresh_task(existing, dto)
            return TaskOutcome(
                task_id=existing.id,
                instance_id=existing.instance_id,
                approval_code=existing.approval_code,
                created=False,
            )

        task = ApprovalTask(
            provider=dto.provider,
            tenant_id=dto.tenant_id,
            instance_id=dto.instance_id,
            approval_code=dto.approval_code,
            approval_title=dto.approval_title,
            applicant_name=dto.applicant_name,
            apply_time=dto.apply_time,
            task_status=TaskStatus.PENDING.value,
            # 刚建的任务还不知道我方立场：准确状态就是 missing
            context_status=ContextStatus.MISSING.value,
            context_source=ContextSource.APPROVAL_SYSTEM.value,
        )
        self._session.add(task)
        self._session.flush()
        return TaskOutcome(
            task_id=task.id,
            instance_id=task.instance_id,
            approval_code=task.approval_code,
            created=True,
        )

    def _find_task(
        self, provider: str, tenant_id: str, instance_id: str
    ) -> ApprovalTask | None:
        """按对象级去重键查找任务。"""
        statement = select(ApprovalTask).where(
            ApprovalTask.provider == provider,
            ApprovalTask.tenant_id == tenant_id,
            ApprovalTask.instance_id == instance_id,
        )
        return self._session.execute(statement).scalar_one_or_none()

    @staticmethod
    def _refresh_task(task: ApprovalTask, dto: PendingApprovalDTO) -> None:
        """刷新"来自待办列表"的字段。

        **刻意不碰**下面这些：

        | 字段 | 为什么不碰 |
        | --- | --- |
        | `task_status` | 重复拉取不该把已完成或阻塞的任务打回 `pending` |
        | `our_party_*` / `context_status` | 它们来自**详情**接口，不是待办列表 |
        | `form_data_json` | 同上 |
        | 解析结果与附件记录 | 重复拉取必须是无害的 |

        只赋值、不"先清空再写"，是因为这些字段本就由待办列表独占 ——
        清空会让"外部系统某次少返回一个字段"表现为**数据消失**，
        而不是表现为"这次没更新"。
        """
        task.approval_code = dto.approval_code
        task.approval_title = dto.approval_title
        task.applicant_name = dto.applicant_name
        task.apply_time = dto.apply_time

    # ------------------------------------------------------------------
    # 内部：权威上下文与附件
    # ------------------------------------------------------------------
    def _pending_from_detail(self, detail: ApprovalDetailDTO) -> PendingApprovalDTO:
        """由详情构造待办 DTO（详情携带了建任务所需的全部基础字段）。"""
        return PendingApprovalDTO(
            provider=self._provider,
            tenant_id=self._tenant_id,
            instance_id=detail.instance_id,
            approval_code=detail.approval_code,
            approval_title=detail.approval_title,
            applicant_name=detail.applicant_name,
            apply_time=detail.apply_time,
            attachment_count=len(detail.attachments),
        )

    def _apply_context(
        self, task: ApprovalTask, context: AuthoritativeContextDTO
    ) -> str:
        """写入权威审查上下文并判定 `context_status`。

        ## 判定规则（M8 修订）

        | 库里状态 | 本次声明 | 结果 |
        | --- | --- | --- |
        | 未人工确认 | 四项齐全 | `complete`（覆盖；来源 `approval_system`） |
        | 未人工确认 | 缺任一项 | `missing` |
        | **`confirmed`（人工背书）** | 与库里**相同** | 保持 `confirmed`（并清掉冲突记录） |
        | **`confirmed`** | 有值但**不同** | **`conflict`**：保留人工值、记下声明值、**不覆盖** |
        | **`confirmed`** | 本次没给全 | 保持 `confirmed`（"系统没说"≠"说了别的"） |

        ## ⚠️ 为什么"取值变了"不能直接覆盖（2026-09-15 实测修正）

        旧口径是"取值未变 → 保留 `confirmed`；变了 → 重算成 `complete`"。
        它在**人工只能确认、不能改值**时是安全的：`confirmed` 只可能是
        系统取值的镜像，"变了"就意味着确认过期。

        有了"人工修正"之后，它变成一个**没有症状的错误**：

        ```text
        人工把业务角色改成 seller（本意：这份合同我方是卖方）
          → 审批系统同步回来仍是 buyer（它没变过）
          → 旧口径覆盖人工值、把状态重置为 complete
          → 而 complete **也在** `_TRUSTED_CONTEXT` 里 → 回写门禁照常放行
          → 规则方向被判反（seller 该判的规则不适用、buyer 的规则误命中）
          → 报告上看不出任何异常，而结论已经错了
        ```

        因此"人工背书过的立场"与本次声明冲突时，正确结果是**停下来等人工裁定**：
        `conflict` 不在 `_TRUSTED_CONTEXT` 里，回写被拒（`CONTEXT_NOT_VALID`），
        直到有人再看一眼。这同时补上了 `conflict` 的**产生路径** ——
        在此之前没有任何生产代码会写出这个状态（只有测试手工 seed 过）。

        两个来源的值都写进 `context_conflict_json`：只说"冲突了"而说不出
        "哪两个值冲突"时，人的下一步只能是猜。
        """
        declared = (
            context.our_party_name,
            context.our_party_contract_label,
            context.our_party_business_role,
            context.contract_type,
        )
        stored = (
            task.our_party_name,
            task.our_party_contract_label,
            task.our_party_business_role,
            task.contract_type,
        )

        # "人工背书过"的判据只有 `context_status == confirmed` 这一个。
        # ⚠️ **不能**用 `context_source` 判断：旧实现在"保留 confirmed"之前
        # 就把它改成了 `approval_system`，因此历史数据里两者并不一致 ——
        # 按 source 判断会让那些任务在下次同步时被旧逻辑再覆盖一次。
        if task.context_status == ContextStatus.CONFIRMED.value:
            if declared == stored:
                # 声明与人工背书一致（可能本来一致，也可能系统刚追上）
                task.context_conflict_json = None
                return task.context_status

            if all(value is not None for value in declared):
                task.context_conflict_json = json.dumps(
                    {
                        "declared": _context_payload(*declared),
                        "confirmed": _context_payload(*stored),
                    },
                    ensure_ascii=False,
                )
                task.context_status = ContextStatus.CONFLICT.value
                return task.context_status

            # 本次声明不完整：人工裁定仍然成立（"没拿到"不等于"拿到了别的"）
            task.context_conflict_json = None
            return task.context_status

        (
            task.our_party_name,
            task.our_party_contract_label,
            task.our_party_business_role,
            task.contract_type,
        ) = declared
        task.context_source = ContextSource.APPROVAL_SYSTEM.value
        task.context_conflict_json = None

        complete = all(value is not None for value in declared)
        task.context_status = (
            ContextStatus.COMPLETE if complete else ContextStatus.MISSING
        ).value
        return task.context_status

    def _upsert_attachments(
        self, task: ApprovalTask, attachments: tuple[AttachmentDTO, ...]
    ) -> tuple[AttachmentSummary, ...]:
        """按 `(task_id, attachment_id)` 去重写入附件**元数据**。

        ⚠️ 只写元数据，**不下载任何字节** —— 下载是工具 3（`AttachmentService`）的职责。

        已存在的记录**保留**其下载状态、对象键与校验和：
        重复同步详情若把 `download_status` 打回 `pending`，
        "附件已经下载好了"这件事会凭空消失，系统会重新下载一遍。
        """
        summaries: list[AttachmentSummary] = []

        for dto in attachments:
            existing = self._find_attachment(task.id, dto.attachment_id)

            if existing is None:
                self._session.add(
                    ApprovalAttachment(
                        task_id=task.id,
                        attachment_id=dto.attachment_id,
                        file_name=dto.file_name,
                        file_type=dto.file_type,
                        download_status=DownloadStatus.PENDING.value,
                        # 首次同步就知道"外部已标记不可用"，同样要留下提示
                        error_message=_UNAVAILABLE_HINT if not dto.available else None,
                    )
                )
                self._session.flush()
                summaries.append(
                    AttachmentSummary(
                        attachment_id=dto.attachment_id,
                        file_name=dto.file_name,
                        file_type=dto.file_type,
                        available=dto.available,
                        download_status=DownloadStatus.PENDING.value,
                        is_new=True,
                    )
                )
                continue

            existing.file_name = dto.file_name
            existing.file_type = dto.file_type

            if (
                not dto.available
                and existing.download_status == DownloadStatus.PENDING.value
            ):
                # 只在"还没尝试过下载"时写入提示，避免覆盖真正的下载失败原因。
                # 不可用是"外部系统告诉我们的事实"，下载失败是"我们尝试后的结果"，
                # 后者信息量更大，不能被覆盖掉。
                # 是否可用每次同步都从外部系统重新获取，因此不需要单独存一列。
                existing.error_message = _UNAVAILABLE_HINT

            summaries.append(
                AttachmentSummary(
                    attachment_id=existing.attachment_id,
                    file_name=existing.file_name,
                    file_type=existing.file_type or "",
                    available=dto.available,
                    download_status=existing.download_status,
                    is_new=False,
                )
            )

        return tuple(summaries)

    def _find_attachment(
        self, task_id: int, attachment_id: str
    ) -> ApprovalAttachment | None:
        statement = select(ApprovalAttachment).where(
            ApprovalAttachment.task_id == task_id,
            ApprovalAttachment.attachment_id == attachment_id,
        )
        return self._session.execute(statement).scalar_one_or_none()


#: 外部系统把附件标记为不可用时的提示。
#: 它描述的是**外部事实**，与"我们尝试下载后失败"是两回事，
#: 因此写入时不能覆盖真正的下载失败原因。
_UNAVAILABLE_HINT = "审批系统已将该附件标记为不可用"


def _dump_json(payload: Any) -> str | None:
    """把表单数据序列化为紧凑 JSON；空值返回 `None` 而不是 `"{}"`。

    `"{}"` 与 `NULL` 看起来一样，但前者会让人以为"外部系统返回了空表单"，
    后者才准确表达"这次没拿到表单"。
    """
    if not payload:
        return None
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _iso(moment: Any) -> str | None:
    """把时间转成 ISO 字符串（用于日志 payload）。"""
    return None if moment is None else moment.isoformat()


#: 任务尚不存在、或权威上下文还没落库时，详情作业使用的版本占位。
_FIRST_DETAIL_VERSION = "first"

#: 参与"上下文版本"计算的字段 —— 就是那 4 个权威业务事实。
_CONTEXT_FIELDS = (
    "our_party_name",
    "our_party_contract_label",
    "our_party_business_role",
    "contract_type",
)


def _context_version(task: ApprovalTask | None) -> str:
    """用**已存**的权威上下文构造版本串；无任务或上下文为空时返回 `first`。

    ⚠️ 用"已存的"而不是"刚取到的"：幂等键必须在**发起调用之前**算出来，
    而新内容只有调用之后才知道。这带来的性质才是关键 ——
    **数据变化后的第一次同步一定会拿到新作业**，不会被吞掉：

    ```text
    第 1 次：库中无上下文    → 版本 first      → 作业 A，成功，存下 C1
    第 2 次：外部已改为 C2   → 版本 digest(C1) → 作业 B（新），成功，存下 C2
    第 3 次：外部仍为 C2     → 版本 digest(C2) → 作业 C（新），此后复用 C
    第 4 次：外部改为 C3     → 版本 digest(C2) → 复用 C，成功，存下 C3
    ```

    代价只是版本号比内容落后一轮 —— 与下载作业（版本同样取**已存**的
    `file_checksum`）是同一套语义，两处刻意保持一致，
    免得将来有人以为其中一处"算错了"而改坏另一处。
    """
    if task is None:
        return _FIRST_DETAIL_VERSION

    values = [getattr(task, field) for field in _CONTEXT_FIELDS]
    if not any(value for value in values):
        return _FIRST_DETAIL_VERSION

    material = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
