"""规则批次服务（M5 / T8）—— 批次幂等、落库、版本快照。

## 批次是"一次输入"的封装

同一份任务会被反复审查：重新解析、修正我方立场、改一条规则、换模型。
每次结论必须能回答"**这是用哪份解析、哪组业务事实、哪个规则版本、哪个模型得出的**"。
因此批次（`review_runs`）绑定的不是"时间"，而是**六项输入**。

## 六项全同才复用（§4.5）

| # | 输入项 | 换成别的意味着 |
| --- | --- | --- |
| 1 | `parse_id` | 解析版本变了 |
| 2 | `context_snapshot_json` | 权威上下文（立场 / 合同类型）变了 |
| 3 | `ruleset_version` | 规则集变了 |
| 4 | `model_version` | 模型变了（Mock ↔ 真实 LLM） |
| 5 | `prompt_version` | 提示词变了 |
| 6 | `config_version` | 引擎配置变了 |

⚠️ **六项必须全比。** 判据窄、记录宽，就会在模型变化时**静默复用旧结论** ——
而库里没有任何一处看得出这件事。`SIX_INPUT_FIELDS` 把这件事写成**代码常量**：
新增一项输入却忘了纳入比较，`_find_reusable` 会漏判，而那种缺陷不报错。

## 版本与快照**同源**

`ruleset_version` 就是 `ruleset_snapshot_json` 的摘要（见 `ruleset_version_of`），
且两者都在 `start_run` 内部派生、**不由调用方传入**。
两个独立的序列化定义会各自漂移 —— 而"版本没变、内容变了"是审计里最坏的一种不一致；
让调用方各传一个，一致性就寄托在"每一处调用都没写错"上。

## 快照记的是**库里的原始配置**

`ruleset_snapshot_of` 取的是 `review_rules` 的**原始列**（`match_text` 等文本），
不是解析后的对象再序列化一遍。理由有三：

1. 它是"当时的配置"的**字面**还原 —— 审计要的正是这个；
2. 解析对象里是枚举与嵌套模型，再序列化一遍就多出一层"我们怎么表示它"的定义，
   而那层定义会随代码演进 —— 旧快照的含义会**悄悄改变**；
3. 新增列时只需在 `_SNAPSHOT_COLUMNS` 里加一个名字，不需要改序列化逻辑。

⚠️ `_SNAPSHOT_COLUMNS` **不含 `updated_at`**：那是**易变**列，
碰一下就换版本号 —— 于是"重新导入一次种子"会凭空产生一批新批次，
而规则内容其实一个字都没变。

## 复用是默认，重跑要**显式**

`force=True` 时即使六项全同也新建批次。它不是可有可无的：
批次可能因**单次 LLM 失败**而含 `needs_review`，而没有强制开关时，
"修好之后再跑一次"会被幂等挡在门外 —— 用户唯一能做的就是**改一个不相关的参数去骗过缓存**，
而那会污染版本绑定（与 M4 验收 30 同源：失败记录不构成缓存命中）。

## 本模块**不写** `review_results`（决策 ⑦）

聚合结论是**现算**的。这里只在 `complete_run` 里把它写进**日志**，不落任何结果表 ——
`tests/test_rule_service.py` 有一条断言守着这件事
（"边界只写在文档里，会随后续开发自然腐蚀"）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.context import get_correlation_id
from app.enums import (
    ErrorCode,
    EvaluationStatus,
    JobType,
    LogLevel,
    LogType,
    ParseStatus,
    ReasonCode,
    RiskLevel,
    RunStatus,
)
from app.errors import PermanentError
from app.models import ApprovalTask, ContractParse, ParseArtifact, ReviewRun
from app.models import RuleEvaluation as RuleHitRow
from app.ports.field_contract import BasicInfoFieldSet, ClauseFieldSet, ExtractedField
from app.ports.object_storage import ObjectStorage
from app.ports.parse_document import StandardDocument
from app.rules.fact_resolver import resolve_derived_fields
from app.rules.applicability import ReviewContext
from app.rules.aggregator import Aggregate, aggregate
from app.rules.evaluator import LlmJudge, RuleSpec, build_rule_spec, evaluate_rule
from app.rules.evaluator import RuleEvaluation as Evaluation
from app.rules.evidence import attach_evidence
from app.schemas import RuleConfig
from app.services.log_service import LogService
from app.workflow.jobs import build_idempotency_key, create_job

#: 批次幂等的**六项输入**。列名与 `review_runs` 一致。
#:
#: ⚠️ 这张表是判据本身，不是文档：`_find_reusable` 按它逐个比较。
#: 新增一项输入时**必须**同时加进这里，否则那一项变了也不会新建批次 ——
#: 结论会是"用旧输入算出来的"，而记录里写着新输入。
SIX_INPUT_FIELDS: Final[tuple[str, ...]] = (
    "parse_id",
    "context_snapshot_json",
    "ruleset_version",
    "model_version",
    "prompt_version",
    "config_version",
)

#: 进快照的 `review_rules` 列。**语义列**，不含易变列（见模块 docstring）。
_SNAPSHOT_COLUMNS: Final[tuple[str, ...]] = (
    "id",
    "rule_code",
    "rule_name",
    "rule_category",
    "risk_level",
    "rule_status",
    "priority",
    "rule_version",
    "match_mode",
    "match_text",
    "applies_when_json",
    "fallback_match_json",
    "exclude_text",
    "suggestion_text",
)


@dataclass(frozen=True)
class ActiveRule:
    """一条启用规则：**库里那一行** + 解析后的配置。

    `raw` 是**原始列映射**，两个地方要用：
    - 快照（记"当时的配置"，要字面值）；
    - `build_rule_spec`（它按**字符串**解析 `match_text` 这类文本列，
      而 `RuleConfig` 里存的是解析后的对象）。
    """

    rule_id: int
    config: RuleConfig
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class RunInputs:
    """一批次的六项输入。**判据与记录用的是同一组字段。**"""

    parse_id: int
    context_snapshot_json: str
    ruleset_version: str
    model_version: str
    prompt_version: str
    config_version: str

    def as_columns(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in SIX_INPUT_FIELDS}


@dataclass(frozen=True)
class RunStart:
    """批次创建的结论。`reused=True` 表示六项全同、复用了既有批次。

    ⚠️ **必须带 `job_id`**：调用方要轮询的是**作业**（`/api/jobs/{job_id}`），
    而一个批次可能有多个作业（重跑、回收）。只给 `run_id` 的话，
    调用方只能去轮询批次 —— 而批次没有"排队中"这个状态，它一建出来就是 `running`，
    于是"入队了没"这件事在接口上根本无法回答（§4.6 的 `TaskRef` 要答的正是它）。
    """

    run_id: int
    version_no: int
    reused: bool
    ruleset_version: str
    #: 入队产生的作业；`None` 只可能出现在直接调用 `start_run` 的场景（测试）
    job_id: int | None = None
    #: 作业状态（**真实值**，不是从 `reused` 推断的 —— 见 `request_rule_run`）
    job_status: str | None = None


# ============================================================
# 规范化与摘要
# ============================================================


def normalize_json(payload: Any) -> str:
    """**稳定序列化**：排序键、紧凑分隔符。

    ⚠️ 不规范化就直接比字符串，dict 键序不同就会判成"输入变了" ——
    一次无意义的重复触发会**凭空多出一个批次**（`version_no` 白涨，
    历史里多一份内容完全相同的评价）。与 M4 的 `input_digest` 同一条要求：
    **先校验、再按稳定序列化算摘要**。

    `ensure_ascii=False` 是有意的：中文在库里保持可读，
    排查时不必先过一遍转义（且更省空间）。
    """
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def context_snapshot_of(context: ReviewContext) -> str:
    """上下文快照。

    ⚠️ **不含 `contract_text`**：正文属于**解析**（第 1 项输入），
    把它塞进上下文快照会让"解析结果变了"与"立场变了"无法区分 ——
    而这两件事的处置完全不同（重新解析 vs 修正立场）。
    它还会让这一列从几百字节涨到几兆。
    """
    return normalize_json(
        {
            "contract_type": context.contract_type,
            "our_contract_label": context.our_contract_label,
            "our_business_role": context.our_business_role,
            "party_context_status": context.party_context_status,
            "contract_type_status": context.contract_type_status,
        }
    )


def ruleset_snapshot_of(rules: Sequence[ActiveRule]) -> str:
    """规则集快照（**规范化**，按 `rule_code` 排序）。

    快照让历史批次**自证**：规则日后被原地修改，这一批结论的依据仍在库里
    （这正是评审提的审计缺口 —— 只存哈希与"当前规则表"还原不出当时的配置）。

    ⚠️ 排序按 `rule_code` 而不是库里的 `priority`：
    调整优先级不该让"规则集变了"，那是**执行顺序**，不是**内容**。
    （`priority` 仍进快照 —— 它变了要换批次，因为判定顺序可能影响结论。）
    """
    return normalize_json(
        [
            {name: item.raw.get(name) for name in _SNAPSHOT_COLUMNS}
            for item in sorted(rules, key=lambda item: item.config.rule_code)
        ]
    )


def ruleset_version_of(rules: Sequence[ActiveRule]) -> str:
    """规则集版本 = **快照的摘要**。

    ⚠️ 由快照派生，而不是另算一遍：两套序列化定义会各自漂移，
    而"版本没变、内容变了"是审计里最坏的一种不一致。
    同源之后，任何能改变快照的东西都会改变版本 —— 包括 `rule_version`
    （改一条规则的版本号却不换批次号，等于声称"规则集没变"）。
    """
    return hashlib.sha256(ruleset_snapshot_of(rules).encode("utf-8")).hexdigest()


# ============================================================
# 读规则
# ============================================================


def load_active_rules(session: Session) -> tuple[ActiveRule, ...]:
    """读全部**启用**规则，按 `priority` 排序（执行顺序）。

    Raises:
        RuleConfigError: 某条规则的配置非法。**必须让它在加载阶段炸**，
            而不是跑到第 37 条时才发现 —— 那时前 36 条已经写进库了。
    """
    from app.models import ReviewRule  # 局部导入：避免模块级循环

    rows = (
        session.execute(
            select(ReviewRule)
            .where(ReviewRule.rule_status == "active")
            .order_by(ReviewRule.priority, ReviewRule.id)
        )
        .scalars()
        .all()
    )
    return tuple(
        ActiveRule(
            rule_id=row.id,
            config=RuleConfig.from_row(_row_mapping(row)),
            raw=_row_mapping(row),
        )
        for row in rows
    )


def _row_mapping(row: Any) -> dict[str, Any]:
    """ORM 行 → 按**列名**取值的映射（`RuleConfig.from_row` 与快照都按列名用）。"""
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def specs_by_code(rules: Sequence[ActiveRule]) -> dict[str, RuleSpec]:
    """`rule_code` → `RuleSpec`（**解析一次，评价 N 次**）。

    ⚠️ 用 `raw` 而不是 `config`：`build_rule_spec` 要的是 `match_text` 这类
    **文本列**，而 `RuleConfig` 里存的是解析后的对象（`match_condition`）。
    拿错的表现是 `AttributeError`（响的），但更常见的是**取到同名不同义的字段**
    —— 那种要等结论错了才发现。
    """
    return {
        item.config.rule_code: build_rule_spec(
            rule_code=item.config.rule_code,
            rule_name=item.config.rule_name,
            risk_level=str(item.config.risk_level),
            rule_version=item.config.rule_version,
            match_mode=str(item.config.match_mode),
            match_text=str(item.raw.get("match_text") or ""),
            applies_when_json=item.raw.get("applies_when_json"),
            fallback_match_json=item.raw.get("fallback_match_json"),
            exclude_text=item.raw.get("exclude_text"),
        )
        for item in rules
    }


# ============================================================
# 批次
# ============================================================


def start_run(
    session: Session,
    *,
    task_id: int,
    parse_id: int,
    context: ReviewContext,
    rules: Sequence[ActiveRule],
    model_version: str,
    prompt_version: str,
    config_version: str,
    force: bool = False,
) -> RunStart:
    """获取或新建批次。**不提交**（由调用方在同一事务里接着写评价）。

    ⚠️ **版本与快照都在这里派生**，不由调用方传入：两者同源之后，
    "传进一个与规则集不匹配的版本号"这件事在类型上就不可能发生。

    ⚠️ 并发：两个执行体可能同时算出同一个 `version_no`，
    那时 `UNIQUE(task_id, version_no)` 会让其中一个失败 —— **这是有意的**。
    让约束挡住，比让两条批次悄悄并存好；后者会让"同一版本号两份结论"成为事实。

    Returns:
        `RunStart`。`reused=True` 时**既不新建、也不重跑** ——
        调用方应直接返回既有批次的结论。
    """
    snapshot = ruleset_snapshot_of(rules)
    inputs = RunInputs(
        parse_id=parse_id,
        context_snapshot_json=context_snapshot_of(context),
        ruleset_version=hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
        model_version=model_version,
        prompt_version=prompt_version,
        config_version=config_version,
    )

    if not force:
        existing = _find_reusable(session, task_id=task_id, inputs=inputs)
        if existing is not None:
            return RunStart(
                run_id=existing.id,
                version_no=existing.version_no,
                reused=True,
                ruleset_version=inputs.ruleset_version,
            )

    version_no = _next_version_no(session, task_id=task_id)
    run = ReviewRun(
        task_id=task_id,
        version_no=version_no,
        run_status=RunStatus.RUNNING.value,
        ruleset_snapshot_json=snapshot,
        **inputs.as_columns(),
    )
    session.add(run)
    session.flush()

    LogService(session).log(
        log_type=LogType.RULE,
        task_id=task_id,
        level=LogLevel.INFO,
        message=(
            f"创建审查批次 v{version_no}（规则集 {inputs.ruleset_version[:12]}…，"
            f"模型 {inputs.model_version}，提示词 {inputs.prompt_version}）"
            + ("【强制重跑】" if force else "")
        ),
        payload={
            "run_id": run.id,
            "version_no": version_no,
            "correlation_id": get_correlation_id(),
            **inputs.as_columns(),
        },
    )
    return RunStart(
        run_id=run.id,
        version_no=version_no,
        reused=False,
        ruleset_version=inputs.ruleset_version,
    )


def _find_reusable(session: Session, *, task_id: int, inputs: RunInputs) -> ReviewRun | None:
    """按**六项**找既有批次（全同才算同一次输入）。

    ⚠️ 用 `==` 逐列比较，判据就是 `SIX_INPUT_FIELDS` —— 不写"比前三项"这种简化：
    判据窄、记录宽时，模型或提示词变化会**静默复用旧结论**。
    """
    statement = select(ReviewRun).where(ReviewRun.task_id == task_id)
    for name in SIX_INPUT_FIELDS:
        statement = statement.where(getattr(ReviewRun, name) == getattr(inputs, name))
    return (
        session.execute(statement.order_by(ReviewRun.version_no.desc()).limit(1))
        .scalars()
        .first()
    )


def _next_version_no(session: Session, *, task_id: int) -> int:
    latest = (
        session.execute(
            select(ReviewRun.version_no)
            .where(ReviewRun.task_id == task_id)
            .order_by(ReviewRun.version_no.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    return (latest or 0) + 1


# ============================================================
# 落库
# ============================================================


def record_evaluations(
    session: Session,
    *,
    run: RunStart,
    task_id: int,
    rules: Sequence[ActiveRule],
    evaluations: Sequence[Evaluation],
) -> int:
    """把一批评价写进 `rule_hits`。**不提交**。

    ⚠️ **四态都要落**，包括 `not_hit` 与 `not_applicable`：
    少了它们就答不出"这条规则为什么没报警"，而那正是这张表
    连不适用规则都保留的全部理由。

    Raises:
        ValueError: 出现未启用规则的评价，或有规则被评了两次。
            两者都会让"每批次每条规则恰好一条"（`UNIQUE(run_id, rule_id)`）
            以**约束冲突**的形式暴露 —— 而那时已经离真正的原因（调用方拼错了）很远了。
    """
    rule_ids = {item.config.rule_code: item.rule_id for item in rules}
    seen: set[str] = set()
    written = 0

    for evaluation in evaluations:
        code = evaluation.rule_code
        if code not in rule_ids:
            raise ValueError(
                f"评价里的规则 {code} 不在本批次的启用规则集内 —— "
                "要么规则被停用了，要么评价来自另一批规则"
            )
        if code in seen:
            raise ValueError(f"规则 {code} 在同一批次里被评价了两次")
        seen.add(code)

        session.add(
            RuleHitRow(
                run_id=run.run_id,
                task_id=task_id,
                rule_id=rule_ids[code],
                rule_version=evaluation.rule_version,
                risk_level=evaluation.risk_level.value,
                # 物理列名是 `hit_status`（需求 2.4.9 规定），语义是四态评价
                evaluation_status=evaluation.status.value,
                reason_code=(
                    evaluation.reason_code.value if evaluation.reason_code else None
                ),
                reason_text=evaluation.reason_text,
                evidence_text=evaluation.evidence_text,
                evidence_position=evaluation.evidence_position,
                evidence_json=evaluation.evidence_json,
                hit_detail_json=normalize_json(evaluation.hit_detail),
            )
        )
        written += 1

    session.flush()
    return written


def complete_run(session: Session, *, run: RunStart, aggregate: Aggregate) -> None:
    """收尾：批次状态 + 摘要日志。**不提交**。

    ⚠️ **聚合结论只进日志，不落库**（决策 ⑦）：它是**现算**的，
    落进 `review_results` 是 M6 的事。边界只写在文档里会随开发自然腐蚀，
    因此 `tests/test_rule_service.py` 有一条断言守着"`review_results` 仍为 0"。
    """
    row = session.get(ReviewRun, run.run_id)
    if row is None:  # pragma: no cover - 只在调用方传错 run_id 时发生
        raise ValueError(f"批次 {run.run_id} 不存在")

    row.run_status = RunStatus.COMPLETED.value
    # SQLite 的 `CURRENT_TIMESTAMP` 是 UTC，这里与之一致（不留本地时区，
    # 否则同一列里会混进两种时间基准，而它们只差几个小时、看不出来）
    row.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)

    LogService(session).log(
        log_type=LogType.RESULT,
        task_id=row.task_id,
        level=LogLevel.INFO,
        message=aggregate.summary,
        payload={
            "run_id": run.run_id,
            "version_no": run.version_no,
            "overall_risk_level": aggregate.overall_risk_level.value,
            "review_status": aggregate.review_status.value,
            "counts": aggregate.counts,
        },
    )


def rule_ids_by_code(rules: Sequence[ActiveRule]) -> dict[str, int]:
    """`rule_code` → `review_rules.id`（落库时要用它填外键）。"""
    return {item.config.rule_code: item.rule_id for item in rules}


#: 无模型时的 `model_version`。它是**配置快照**（接入了哪个模型），
#: 不是运行结果 —— 单次调用失败记在该规则的 `reason_code` 上，不得改它。
#: M5 阶段没有接入真实模型：9 条 `llm` 规则走规则自带的 fallback（验收 3/19）。
DEFAULT_MODEL_VERSION: Final[str] = "none:fallback"

#: 引擎配置版本（阈值 / 开关）。
#: ⚠️ 改了影响判定的配置就必须改它，否则同一份"输入"会复用旧批次的结论 ——
#: 而库里 `config_version` 写着旧值，两处都不报错。
ENGINE_CONFIG_VERSION: Final[str] = "engine-v1"


def request_rule_run(
    session: Session,
    *,
    parse_id: int,
    context: ReviewContext,
    model_version: str = DEFAULT_MODEL_VERSION,
    prompt_version: str,
    config_version: str = ENGINE_CONFIG_VERSION,
    force: bool = False,
) -> RunStart:
    """工具 5：**只入队**，返回可查询的批次引用。**不提交**。

    ⚠️ 批次在**入队时**就建好（与工具 4 预留解析占位同理）：
    调用方立刻拿到 `run_id`，`result_url` 不必等作业完成才知道。
    执行时 `run_batch` 会认出这个**空批次**并就地评价（见其 docstring）——
    这正是"存在但为空 ≠ 复用"那条判据在链路里的用处。

    ⚠️ 校验 `parse_status == 'succeeded'`：**没通过质量门禁的解析不得进入规则评价**。
    等 worker 去发现的表现是"作业一直失败"，而真正的原因（解析没过门禁）看不出来。

    Raises:
        PermanentError: 解析记录不存在或未过门禁。
    """
    parse = session.get(ContractParse, parse_id)
    if parse is None:
        raise PermanentError(
            f"解析记录 {parse_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )
    if parse.parse_status != ParseStatus.SUCCEEDED.value:
        raise PermanentError(
            f"解析记录 {parse_id} 未通过质量门禁（{parse.parse_status}）："
            f"{parse.parse_error or '无说明'}",
            code=_recorded_error_code(parse.parse_error_code),
        )

    rules = load_active_rules(session)
    start = start_run(
        session,
        task_id=parse.task_id,
        parse_id=parse_id,
        context=context,
        rules=rules,
        model_version=model_version,
        prompt_version=prompt_version,
        config_version=config_version,
        force=force,
    )

    job, _job_created = create_job(
        session,
        job_type=JobType.RULE,
        task_id=parse.task_id,
        # 幂等键带上规则集版本：规则集变了就是**另一次输入**，该建新作业
        idempotency_key=build_idempotency_key(
            JobType.RULE, str(start.run_id), start.ruleset_version[:16]
        ),
        input_payload={"run_id": start.run_id, "parse_id": parse_id},
    )

    # ⚠️ 作业状态**照实读回**，不从 `reused` 推断：六项全同但上一次仍在跑时，
    # `reused=True` 而作业是 `running` —— 把那种情况答成"已完成"，
    # 调用方就会停止轮询，然后去读一个**还没有结论**的批次（P1-1）。
    return RunStart(
        run_id=start.run_id,
        version_no=start.version_no,
        reused=start.reused,
        ruleset_version=start.ruleset_version,
        job_id=job.id,
        job_status=job.job_status,
    )


def context_for_parse(session: Session, parse_id: int) -> ReviewContext:
    """从解析记录（与它所属的审批任务）构造权威上下文。

    ⚠️ **两个核验状态分开取**，分别对应"我方立场"与"合同类型"两个维度
    （M4 §4.8 的两个保留键）。用一个总的 `context_status` 顶替两者，
    会让一次**立场**争议把合同类型类规则也判成"判不了" ——
    而那会让整份审查结论无谓地全部失效（见 `app/rules/applicability.py`）。
    """
    parse = session.get(ContractParse, parse_id)
    if parse is None:
        raise PermanentError(
            f"解析记录 {parse_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )
    task = session.get(ApprovalTask, parse.task_id)
    if task is None:
        raise PermanentError(
            f"解析记录 {parse_id} 所属的审批任务 {parse.task_id} 不存在",
            code=ErrorCode.RESOURCE_NOT_FOUND,
        )

    party_status, type_status = _consistency_statuses(parse)
    return ReviewContext(
        contract_type=task.contract_type,
        our_contract_label=task.our_party_contract_label,
        our_business_role=task.our_party_business_role,
        party_context_status=party_status,
        contract_type_status=type_status,
    )


def _consistency_statuses(parse: ContractParse) -> tuple[str | None, str | None]:
    """从解析产出的保留键里取 `_party_consistency` / `_contract_type_consistency`。

    读不到时返回 `(None, None)` 而不是抛错：那表示"没有核验结论"，
    适用性判断会按"上下文缺失"处理（`needs_review`）—— 而不是当成"没有冲突"。
    """
    try:
        payload = json.loads(parse.basic_info_json or "{}")
    except ValueError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return payload.get("_party_consistency"), payload.get("_contract_type_consistency")


def execute_existing_run(
    session: Session,
    *,
    run_id: int,
    storage: ObjectStorage,
    llm_judge: LlmJudge | None = None,
) -> BatchResult:
    """执行**指定**批次。**禁止新建批次，也不读当前规则集/当前配置。**

    ⚠️ 这是 P0② 的修法。`run_batch` 会重新加载**当前**启用规则并再调一次
    `start_run` —— 入队到执行之间规则或配置变了，它就会建出**另一个**批次：

    ```text
    作业输入指向 run_id=10
    worker 重新加载当前规则（规则集已变）
    → start_run 判定"六项不同" → 建 run_id=11
    → 作业成功，result_ref 仍指向 run_id=10
    → run_id=10 仍是空批次        ← 作业成功、批次为空、结论全无
    ```

    因此 worker **必须**走这条路径：批次就是入队时冻结的那一个，
    上下文、规则集、六项版本**全部从批次读回**。

    ⚠️ **只支持两种入口**（评审 P2 的取舍）：

    | 批次现状 | 行为 |
    | --- | --- |
    | **空**（没有任何评价） | 执行 |
    | **完整**（已有评价） | 复用，不重跑 |

    **不承诺"部分评价就地补完"**：实现是"重评全部规则再插入"，
    那会直接撞上 `UNIQUE(run_id, rule_id)`；而"持久化了一半评价"在正常路径上
    **不该存在** —— worker 的写入与完成在**同一个事务**里，失败即整体回滚。
    **承诺一个不该存在的状态、还实现不对，比不承诺更糟。**

    Raises:
        PermanentError: 批次不存在、快照无法解析、或快照里的规则已被删除。
    """
    run = session.get(ReviewRun, run_id)
    if run is None:
        raise PermanentError(
            f"审查批次 {run_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )

    start = RunStart(
        run_id=run.id,
        version_no=run.version_no,
        reused=False,
        ruleset_version=run.ruleset_version or "",
    )

    # 已有评价 ⇒ 完整批次（见上）→ 复用；没收尾的顺手收尾（聚合是现算的，不必重跑）
    evaluations = evaluations_of_run(session, run_id)
    if evaluations:
        if run.run_status != RunStatus.COMPLETED.value:
            complete_run(session, run=start, aggregate=aggregate(evaluations))
        return BatchResult(
            run_id=run.id,
            version_no=run.version_no,
            reused=True,
            ruleset_version=start.ruleset_version,
            evaluations=evaluations,
            aggregate=aggregate(evaluations),
        )

    context = ReviewContext(**json.loads(run.context_snapshot_json or "{}"))
    rules = _rules_from_snapshot(run.ruleset_snapshot_json)
    _ensure_rules_still_exist(session, rules)
    inputs = load_batch_inputs(session, parse_id=run.parse_id, storage=storage)

    specs = specs_by_code(rules)
    evaluations = [
        _evaluate_one(
            specs[item.config.rule_code],
            context=context,
            fields=inputs.fields,
            text=inputs.text,
            document=inputs.document,
            currency=inputs.currency,
            llm_judge=llm_judge,
        )
        for item in rules
    ]

    record_evaluations(
        session, run=start, task_id=run.task_id, rules=rules, evaluations=evaluations
    )
    summary = aggregate(evaluations)
    complete_run(session, run=start, aggregate=summary)

    return BatchResult(
        run_id=run.id,
        version_no=run.version_no,
        reused=False,
        ruleset_version=start.ruleset_version,
        evaluations=tuple(evaluations),
        aggregate=summary,
    )


def run_rule_job(
    session: Session,
    *,
    run_id: int,
    storage: ObjectStorage,
    llm_judge: LlmJudge | None = None,
) -> BatchResult:
    """`JobType.RULE` 作业的处理器：执行**它自己那个**批次。**不提交**。

    ⚠️ **不再接受调用方传版本号**：批次的模型/提示词/配置版本是入队时冻结的，
    由调用方再传一份，要么与批次不符（让"执行"与"批次的声明"不一致），
    要么促成 P0② 那种"另建一个批次"。全部从批次读回即可。
    """
    return execute_existing_run(
        session, run_id=run_id, storage=storage, llm_judge=llm_judge
    )


def _rules_from_snapshot(raw: str | None) -> tuple[ActiveRule, ...]:
    """从批次**冻结的**规则集快照重建 `ActiveRule`（P0②）。

    快照里存的是 `review_rules` 的**原始列**（`match_mode` / `match_text` /
    `applies_when_json` / …），因此足以重建 `RuleSpec` ——
    "按冻结的规则集执行"不需要另建结构。

    ⚠️ 执行顺序取自快照的 `priority`，不是当前库里的顺序：
    优先级可以在入队之后被改，而**本次批次的判定顺序属于当时那套配置**。
    """
    try:
        entries = json.loads(raw or "[]")
    except ValueError as exc:
        raise PermanentError(
            "批次的规则集快照无法解析，拒绝改用当前规则集执行",
            code=ErrorCode.UNEXPECTED_ERROR,
        ) from exc

    rules = [
        ActiveRule(rule_id=entry["id"], config=RuleConfig.from_row(entry), raw=entry)
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), int)
    ]
    return tuple(
        sorted(rules, key=lambda item: (item.raw.get("priority") or 0, item.rule_id))
    )


def _ensure_rules_still_exist(
    session: Session, rules: Sequence[ActiveRule]
) -> None:
    """⚠️ **停用**不影响执行；**删除**必须明确拒绝。

    静默改用"现在还在的那几条"会让本批次的结论与它声称的规则集不一致 ——
    而 `ruleset_version` 仍指向那份旧快照，**两处都不报错**。
    """
    from app.models import ReviewRule  # 局部导入：避免模块级循环

    ids = [item.rule_id for item in rules]
    if not ids:
        return

    existing = {
        row[0]
        for row in session.execute(select(ReviewRule.id).where(ReviewRule.id.in_(ids)))
    }
    missing = sorted(set(ids) - existing)
    if missing:
        raise PermanentError(
            f"批次的规则集里有 {len(missing)} 条规则已被删除（id={missing}）—— "
            "无法按冻结的规则集执行，也不能改用当前规则集",
            code=ErrorCode.RESOURCE_NOT_FOUND,
        )


# ============================================================
# 装配：解析产物 → 批次输入
# ============================================================

#: 标准文档工件的 `kind`（与 `app/services/parse_service.py` 写入时一致）
ARTIFACT_KIND_STANDARD_DOCUMENT: Final[str] = "standard_document"

#: 规则没给币种时用于可比性判断的默认币种。
#: ⚠️ 它是**兜底**，不是业务事实 —— 解析出的 `currency` 字段优先。
DEFAULT_CURRENCY: Final[str] = "CNY"


@dataclass(frozen=True)
class BatchInputs:
    """批次评价需要的全部输入。"""

    parse_id: int
    document: StandardDocument
    fields: dict[str, ExtractedField]
    text: str
    currency: str


def load_batch_inputs(
    session: Session, *, parse_id: int, storage: ObjectStorage
) -> BatchInputs:
    """把 M4 的解析产物装配成批次输入。**只读**（外加一次对象存储读取）。

    ⚠️ **必须还原标准文档**，不能只读字段 JSON：`keyword` / `regex` / `llm`
    三类规则要的是**合同正文**，而正文只存在于文档工件里
    （`DocumentPage.text` 是权威明文）。只读字段的装配会让这三类规则**全部判不了**，
    而报告上看起来像"合同里没有这些内容"。

    ⚠️ 门禁：`parse_status` 必须是 `succeeded`（**已过质量门禁**，§4.9）。
    在残缺的输入上跑 40 条规则，得到的是一份"看起来正常"的报告，
    而它的依据本来就是不可用的。

    Raises:
        PermanentError: 解析记录不存在、未过门禁、或缺工件。
    """
    parse = session.get(ContractParse, parse_id)
    if parse is None:
        raise PermanentError(
            f"解析记录 {parse_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )

    if parse.parse_status != ParseStatus.SUCCEEDED.value:
        # 错误码用**库里记着的那个**（M4 写的 `parse_error_code`），不另发明一个：
        # 另发明会让"解析失败"与"解析没过门禁"看起来是两种不同的故障，
        # 而排查的人会去找一个根本不存在的第二处失败点。
        raise PermanentError(
            f"解析记录 {parse_id} 未通过质量门禁（{parse.parse_status}）："
            f"{parse.parse_error or '无说明'}",
            code=_recorded_error_code(parse.parse_error_code),
        )

    document = _standard_document(session, parse_id=parse_id, storage=storage)

    fields: dict[str, ExtractedField] = {}
    for raw, model in (
        (parse.basic_info_json, BasicInfoFieldSet),
        (parse.clause_info_json, ClauseFieldSet),
    ):
        if raw:
            loaded = model.model_validate_json(raw)
            fields.update({item.field_code: item for item in loaded.fields})

    # ⚠️ 派生字段**必须在这里补上**（T3a）：6 条 expr 规则读的是它们，
    # 而 M4 只抽直接字段 —— 漏掉这一步的表现是"未约定预付款"这类**业务结论**，
    # 报告上看不出这是实现缺口。
    fields.update(resolve_derived_fields(document))

    return BatchInputs(
        parse_id=parse_id,
        document=document,
        fields=fields,
        text=document_text(document),
        currency=_currency_of(fields),
    )


def document_text(document: StandardDocument) -> str:
    """正文拼接。**与证据定位用同一个拼接方式**（页间换行）。

    ⚠️ 跨页的片段匹配不到 —— 与 T6 的已知边界一致（见 `app/rules/evidence.py`）。
    换一种拼接方式（比如去掉换行）会让"能不能匹配到"取决于调用方是谁，
    而那种差异是静默的。
    """
    return "\n".join(page.text for page in document.pages)


def _standard_document(
    session: Session, *, parse_id: int, storage: ObjectStorage
) -> StandardDocument:
    artifact = (
        session.execute(
            select(ParseArtifact)
            .where(
                ParseArtifact.parse_id == parse_id,
                ParseArtifact.kind == ARTIFACT_KIND_STANDARD_DOCUMENT,
            )
            .order_by(ParseArtifact.artifact_version.desc())
        )
        .scalars()
        .first()
    )
    if artifact is None:
        raise PermanentError(
            f"解析记录 {parse_id} 没有标准文档工件，无法做规则评价",
            code=ErrorCode.OBJECT_NOT_FOUND,
        )
    return StandardDocument.model_validate_json(storage.get(artifact.object_key))


def _recorded_error_code(raw: str | None) -> ErrorCode:
    """把库里记着的错误码字符串还原成枚举；认不出时退回 `RESOURCE_NOT_FOUND`。"""
    try:
        return ErrorCode(raw) if raw else ErrorCode.RESOURCE_NOT_FOUND
    except ValueError:
        return ErrorCode.RESOURCE_NOT_FOUND


def _currency_of(fields: Mapping[str, ExtractedField]) -> str:
    item = fields.get("currency")
    value = (item.value_text or "").strip() if item is not None else ""
    return value or DEFAULT_CURRENCY


# ============================================================
# 读回落库的评价（决策 ⑦ 的关键一环）
# ============================================================


def evaluations_of_run(session: Session, run_id: int) -> tuple[Evaluation, ...]:
    """把某批次的评价**从 `rule_hits` 读回来**。

    ⚠️ 这不是"多余的往返"，而是决策 ⑦ 成立的前提：聚合结论**不落库**，
    因此它必须能**从依据重新算出**。若读不回来，就只能把聚合另行存一份 ——
    那正是决策 ⑦ 要避免的"结论与依据各自漂移"。

    ⚠️ `rule_hits` 上只有 `rule_id`，**没有 `rule_code`** —— 必须连表取。
    少了这个 join，返回值里就没有规则码，而关注点、日志、界面全都要它。
    """
    from app.models import ReviewRule  # 局部导入：避免模块级循环

    rows = session.execute(
        select(RuleHitRow, ReviewRule.rule_code)
        .join(ReviewRule, ReviewRule.id == RuleHitRow.rule_id)
        .where(RuleHitRow.run_id == run_id)
        .order_by(RuleHitRow.id)
    ).all()
    return tuple(_evaluation_of(row, code) for row, code in rows)


def rule_names_by_code(session: Session, codes: Sequence[str]) -> dict[str, str]:
    """规则编码 → 中文名（`ReviewRule.rule_name`）。

    评价行上只有 `rule_code`，而界面要给人看的是名字 ——
    这张表让 API 层在**不惊动领域对象**（`Evaluation` 保持纯判断结果）的
    前提下把名字补上。查不到的码不在字典里，前端回退显示编码
    （规则被删时 `rule_name` 为 `null`，而不是编一个名字）。
    """
    from app.models import ReviewRule  # 局部导入：避免模块级循环

    unique = sorted(set(codes))
    if not unique:
        return {}
    rows = session.execute(
        select(ReviewRule.rule_code, ReviewRule.rule_name).where(
            ReviewRule.rule_code.in_(unique)
        )
    ).all()
    return {code: name for code, name in rows if name is not None}


def _evaluation_of(row: Any, rule_code: str) -> Evaluation:
    """`rule_hits` 行 → 领域评价（`record_evaluations` 的**逆映射**）。

    ⚠️ 这是第二处"列 ↔ 字段"的映射，与 `record_evaluations` 成对。
    两处一旦不一致，**写入是对的、读回来是错的**（或反之），而两边都不报错 ——
    因此 `tests/test_rule_service.py` 有一条**往返**断言：写进去什么，读回来必须一样。
    """
    return Evaluation(
        rule_code=rule_code,
        rule_version=row.rule_version,
        status=EvaluationStatus(row.evaluation_status),
        risk_level=RiskLevel(row.risk_level),
        reason_code=(
            ReasonCode(row.reason_code) if row.reason_code is not None else None
        ),
        reason_text=row.reason_text,
        hit_detail=json.loads(row.hit_detail_json) if row.hit_detail_json else {},
        evidence_text=row.evidence_text,
        evidence_position=row.evidence_position,
        evidence_json=row.evidence_json,
    )


def aggregate_of_run(session: Session, run_id: int) -> Aggregate:
    """**现算**某批次的汇总结论（决策 ⑦）。"""
    return aggregate(evaluations_of_run(session, run_id))


# ============================================================
# 批次执行（M5 端到端的那一步）
# ============================================================


@dataclass(frozen=True)
class BatchResult:
    """一次批次执行的结果。

    `reused=True` 时 `evaluations` 是**从库里读回来的**，不是重跑的 ——
    这正是"六项全同则复用"的可观测含义。
    """

    run_id: int
    version_no: int
    reused: bool
    ruleset_version: str
    evaluations: tuple[Evaluation, ...]
    aggregate: Aggregate


def run_batch(
    session: Session,
    *,
    task_id: int,
    parse_id: int,
    context: ReviewContext,
    fields: Mapping[str, ExtractedField],
    text: str | None,
    document: StandardDocument,
    currency: str = "CNY",
    model_version: str,
    prompt_version: str,
    config_version: str,
    force: bool = False,
    llm_judge: LlmJudge | None = None,
) -> BatchResult:
    """执行一次完整批次：取批次 → 评价全部启用规则 → 补证据 → 落库 → 聚合。**不提交**。

    Args:
        fields: 字段码 → 字段结论（**含 T3a 的派生字段**）。
        text: 合同正文。`None` = 不可用（keyword/regex 类规则会判不了，
            而不是"未命中" —— 两者的业务含义相反）。
        document: 标准文档 —— **必填**。证据定位（T6）要在它里面找到原文片段；
            没有它就无法核验任何命中，而"看不见依据的命中"在使用上与幻觉没有区别。
            ⚠️ 因此不设默认值：允许不传，就等于允许一条"命中但没有依据"的结论
            悄悄落库，而报告上它看起来完全正常。
        llm_judge: `llm` 模式的判定钩子。`None` = 没有模型 → 走规则自带的 fallback。

    ⚠️ **复用时不重跑，一次都不跑**：`reused=True` 直接读回既有评价。
    哪怕只是"再跑一遍确定性的 31 条"也是错的 —— 那会让 `needs_review`
    的批次在用户没要求的情况下改变结论，而且"复用"这个词就不再成立。
    """
    rules = load_active_rules(session)
    start = start_run(
        session,
        task_id=task_id,
        parse_id=parse_id,
        context=context,
        rules=rules,
        model_version=model_version,
        prompt_version=prompt_version,
        config_version=config_version,
        force=force,
    )

    if start.reused:
        evaluations = evaluations_of_run(session, start.run_id)
        if evaluations:
            # 已有评价 → **真复用**。若上一次没收尾（worker 崩在落库之后、
            # `complete_run` 之前），这里补上收尾 —— 聚合是现算的，不必重跑。
            row = session.get(ReviewRun, start.run_id)
            if row is not None and row.run_status != RunStatus.COMPLETED.value:
                complete_run(session, run=start, aggregate=aggregate(evaluations))
            return BatchResult(
                run_id=start.run_id,
                version_no=start.version_no,
                reused=True,
                ruleset_version=start.ruleset_version,
                evaluations=evaluations,
                aggregate=aggregate(evaluations),
            )

        # ⚠️ 批次**存在但没有任何评价** → 这不是"复用"，是"**已入队、还没算**"。
        #
        # 工具 5 在入队时就把批次建好（与工具 4 预留解析占位同理），
        # worker 拿到的正是这种批次。按"存在即复用"处理的话：
        # 任务成功、批次为空、结论全无 —— 而作业状态显示 `succeeded`，
        # 调用方拿到一份**没有结论的空结果**，却没有任何一处报错。
        #
        # 就地评价（不新建批次）也顺带覆盖了"worker 崩在落库之前"的重试。

    specs = specs_by_code(rules)
    evaluations = [
        _evaluate_one(
            specs[item.config.rule_code],
            context=context,
            fields=fields,
            text=text,
            document=document,
            currency=currency,
            llm_judge=llm_judge,
        )
        for item in rules
    ]

    record_evaluations(
        session, run=start, task_id=task_id, rules=rules, evaluations=evaluations
    )
    summary = aggregate(evaluations)
    complete_run(session, run=start, aggregate=summary)

    return BatchResult(
        run_id=start.run_id,
        version_no=start.version_no,
        reused=False,
        ruleset_version=start.ruleset_version,
        evaluations=tuple(evaluations),
        aggregate=summary,
    )


def _evaluate_one(
    spec: RuleSpec,
    *,
    context: ReviewContext,
    fields: Mapping[str, ExtractedField],
    text: str | None,
    document: StandardDocument,
    currency: str,
    llm_judge: LlmJudge | None,
) -> Evaluation:
    """§4.1 的固定顺序：适用性 → 条件 → 证据核验。"""
    evaluation = evaluate_rule(
        spec,
        context=context,
        text=text,
        fields=fields,
        default_currency=currency,
        llm_judge=llm_judge,
    )
    # 第 ⑤ 步：证据定位与反向核验。定位不到的命中会被**降级**为 `needs_review`
    # （见 `app/rules/evidence.py`）—— 那是"判不了"，不是"没问题"。
    return attach_evidence(evaluation, spec=spec, document=document, fields=fields)
