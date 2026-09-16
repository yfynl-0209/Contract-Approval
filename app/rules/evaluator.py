"""四状态规则评价引擎 —— 一条规则在一个批次里**恰好**产生一条评价。

## 固定顺序（计划 §11.2），不可调换

```text
① 适用性        适用条件所需上下文缺失/冲突 → needs_review；明确不满足 → not_applicable
② 证据质量      字段四态 → 判不了还是能判（见 app/rules/matching.py）
③ 确定性条件    keyword / regex / expr → hit / not_hit
④ 必要时 LLM    仅 llm 模式；模型不可用时走**规则自带的 fallback**
```

**① 之后就直接返回、不读字段**：`not_applicable` 的规则**不该**因为"字段读不出来"
而升级成 `needs_review` —— 那会把不适用规则重新拉回人工队列，
使 `not_applicable` 失去"减少噪声"的作用（`HT-2026-0004` 标准品采购的 IP 规则正是这个场景）。

## 为什么每一条都要有 `reason_code`

`rule_hits.reason_code` 是**稳定机器码**：`not_hit` 也要记
（`CONDITION_NOT_MATCHED`），否则"这条规则为什么没报警"就只剩人去读中文。
四态各自都能落到一个码上，因此这里**不产出空码**。

## 本模块是纯函数

不碰数据库、不读配置、不记日志。输入是规则规格 + 上下文 + 文本 + 字段，输出是结论。
批次、落库与幂等是 `app/services/rule_service.py`（T8）的事。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from app.enums import EvaluationStatus, MatchMode, ReasonCode, RiskLevel
from app.ports.field_contract import ExtractedField
from app.rules.applicability import (
    Applicability,
    ApplicabilityResult,
    ReviewContext,
    evaluate_applicability,
)
from app.rules.matching import (
    MatchResult,
    MatchVerdict,
    match_expr,
    match_keyword,
    match_regex,
    parse_exclude_text,
)
from app.schemas import (
    AppliesWhenConfig,
    ExprMatchConfig,
    FallbackCondition,
    KeywordMatchConfig,
    LlmMatchConfig,
    MatchCondition,
    RegexMatchConfig,
    parse_applies_when,
    parse_fallback_config,
    parse_match_config,
)

#: `llm` 规则的判定钩子 —— 由 T5 提供（构造提示词、约束 schema、解释响应、证据反向核验）。
#:
#: ⚠️ 做成**钩子**而不是在这里直接调 `LLMGateway`：提示词与响应解释是 LLM 专属知识，
#: 混进评价引擎会让"顺序与分派"这件事被一堆模型细节淹没。
#: `None` 是**合法配置**（本项目的主要形态：没有模型），此时 llm 规则走规则自带的 fallback。
LlmJudge = Callable[["RuleSpec", str], MatchResult]


@dataclass(frozen=True)
class RuleSpec:
    """一条规则的**已解析**规格 —— 引擎只认它，不认数据库行。"""

    rule_code: str
    rule_name: str
    risk_level: RiskLevel
    rule_version: int
    match_mode: MatchMode
    config: MatchCondition
    applies_when: AppliesWhenConfig | None = None
    fallback: FallbackCondition | None = None
    exclude_phrases: tuple[str, ...] = ()
    suggestion: str | None = None

    @property
    def is_llm(self) -> bool:
        return self.match_mode is MatchMode.LLM


def build_rule_spec(
    *,
    rule_code: str,
    rule_name: str,
    risk_level: str,
    rule_version: int,
    match_mode: str,
    match_text: str,
    applies_when_json: str | None = None,
    fallback_match_json: str | None = None,
    exclude_text: str | None = None,
    suggestion_text: str | None = None,
) -> RuleSpec:
    """把一行 `review_rules` 变成 `RuleSpec`（**解析一次，评价 N 次**）。

    解析放在这里而不是每条规则评价时：40 条规则 × 每批次一次 JSON 解析与校验
    是白白重复的工作，而校验失败应该在**加载阶段**炸，不是跑到一半才炸。

    Raises:
        RuleConfigError: 配置非法（未知键、非法枚举、正则无法编译…）。
    """
    return RuleSpec(
        rule_code=rule_code,
        rule_name=rule_name,
        risk_level=RiskLevel(risk_level),
        rule_version=rule_version,
        match_mode=MatchMode(match_mode),
        config=parse_match_config(match_mode, match_text, rule_code=rule_code),
        applies_when=parse_applies_when(applies_when_json, rule_code=rule_code),
        fallback=parse_fallback_config(fallback_match_json, rule_code=rule_code),
        exclude_phrases=parse_exclude_text(exclude_text),
        suggestion=suggestion_text,
    )


@dataclass(frozen=True)
class RuleEvaluation:
    """一条规则在一个批次里的评价。字段名与 `rule_hits` 的列一一对应。"""

    rule_code: str
    rule_version: int
    #: 四态（落 `rule_hits.hit_status`）
    status: EvaluationStatus
    #: 规则自身的风险等级（无论命中与否都要落库 —— 列是 NOT NULL）
    risk_level: RiskLevel
    reason_code: ReasonCode
    reason_text: str
    #: 命中的原文片段（供 T6 定位证据）。`not_hit` / `not_applicable` 一律为空。
    located_text: str | None = None
    #: 进 `rule_hits.hit_detail_json`（计算过程）
    hit_detail: dict[str, Any] = dataclass_field(default_factory=dict)

    # ---- 证据（由 T6 的 `app/rules/evidence.py` 填充）----
    #: 主要证据的原文片段（落 `rule_hits.evidence_text`）
    evidence_text: str | None = None
    #: 主要证据的位置 JSON（落 `rule_hits.evidence_position`）；
    #: 含 **text 与 bbox 两个精度** —— M8 画框用的是 bbox 那个
    evidence_position: str | None = None
    #: 全部证据（落 `rule_hits.evidence_json`）：`[{text, position}, ...]`
    evidence_json: str | None = None

    @property
    def is_hit(self) -> bool:
        return self.status is EvaluationStatus.HIT

    @property
    def has_evidence(self) -> bool:
        """是否带可核验的证据。

        ⚠️ `False` 的 `HIT` 是**可疑**的 —— 除了缺失类（`absent`）规则：
        它的结论是"全文都没有"，那条**没有**可指的片段，
        依据记在 `hit_detail`（检索过哪些词），见 `app/rules/evidence.py`。
        """
        return bool(self.evidence_json)


def evaluate_rule(
    spec: RuleSpec,
    *,
    context: ReviewContext,
    text: str | None,
    fields: Mapping[str, ExtractedField],
    default_currency: str,
    llm_judge: LlmJudge | None = None,
) -> RuleEvaluation:
    """评价**一条**规则。**不提交、不落库。**

    Args:
        context: 权威审查上下文（由 `review_runs.context_snapshot_json` 还原）。
        text: 合同正文。`None` = 不可用（此时 keyword/regex 判不了）。
            ⚠️ 粒度由调用方决定（整篇 / 某一块），见 `app/rules/matching.py`。
        fields: 字段码 → 字段结论。**缺键**表示"解析结果里没有这个字段"。
        llm_judge: `llm` 模式的实际判定钩子；`None` = 没有模型（走 fallback）。
    """
    # ---- ① 适用性 ----
    applicability = evaluate_applicability(spec.applies_when, context)
    if applicability.applicability is Applicability.NOT_APPLICABLE:
        return _not_applicable(spec, applicability)
    if applicability.applicability is Applicability.UNKNOWN:
        return _needs_review(
            spec,
            applicability.reason_code or ReasonCode.CONTEXT_MISSING,
            applicability.reason_text or "权威上下文不足，无法判断该规则是否适用",
        )

    # ---- ② ③ ④ 按模式分派 ----
    outcome = _match_by_mode(
        spec,
        text=text,
        fields=fields,
        default_currency=default_currency,
        llm_judge=llm_judge,
    )
    return _from_match(spec, outcome)


# ============================================================
# 分派
# ============================================================


def _match_by_mode(
    spec: RuleSpec,
    *,
    text: str | None,
    fields: Mapping[str, ExtractedField],
    default_currency: str,
    llm_judge: LlmJudge | None,
) -> MatchResult:
    config = spec.config

    if isinstance(config, KeywordMatchConfig):
        return match_keyword(
            config, text=text, exclude_phrases=spec.exclude_phrases
        )

    if isinstance(config, RegexMatchConfig):
        return match_regex(config, text=text)

    if isinstance(config, ExprMatchConfig):
        return match_expr(
            config,
            field=fields.get(config.field),
            default_currency=default_currency,
        )

    if isinstance(config, LlmMatchConfig):
        return _match_llm(spec, text=text, llm_judge=llm_judge)

    # 四条分支由 `MatchCondition` 的取值域穷尽；到这里说明 DTO 与引擎脱节了。
    raise ValueError(  # pragma: no cover - 由 DTO 取值域保证
        f"规则 {spec.rule_code} 的匹配模式没有对应的评价分支：{type(config).__name__}"
    )


def _match_llm(
    spec: RuleSpec, *, text: str | None, llm_judge: LlmJudge | None
) -> MatchResult:
    """`llm` 模式：**有模型就用模型，没有就用规则自带的 fallback**。

    ## 两条分支的区别必须说清楚

    | 情形 | 处置 | 为什么 |
    | --- | --- | --- |
    | **没有接入模型**（`llm_judge is None`，本项目的主要形态） | 走 `fallback_match_json` | 计划 §2.2 要求"**无 GPU 也能完成全部业务开发**"。实测 9 条 llm 规则**全部**配了 fallback，因此这条路是**常态路径**，不是兜底 |
    | **接了模型但这次调用失败** | `needs_review(MODEL_UNAVAILABLE)` | 单次抖动**不得**静默换成确定性答案 —— 那会让"本该用模型判的"看起来判过了。要改善只能显式重跑（`force`） |

    ⚠️ 把两者合并（"失败也走 fallback"）的后果很隐蔽：模型挂了一整天，
    报告里全是"已判定"，而实际上一条都没真的问过模型。
    """
    if llm_judge is not None and text is not None:
        judged = llm_judge(spec, text)
        if judged.decidable:
            return judged
        # 单次失败 → 如实上报，不静默降级（见上表）
        return judged

    if spec.fallback is None:
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            ReasonCode.MODEL_UNAVAILABLE,
            "该规则需要模型判断，但当前未接入模型，且规则未配置降级条件",
        )

    # 无模型时的确定性降级 —— 与 `match_mode` 的确定性分支**同一套实现**，
    # 因此降级结论的语义、证据定位、四态处理完全一致，不需要第二套逻辑。
    fallback = spec.fallback
    if isinstance(fallback, KeywordMatchConfig):
        return match_keyword(
            fallback, text=text, exclude_phrases=spec.exclude_phrases
        )
    return match_regex(fallback, text=text)


# ============================================================
# MatchResult → RuleEvaluation
# ============================================================


def _from_match(spec: RuleSpec, outcome: MatchResult) -> RuleEvaluation:
    """三态匹配结果 → 四态评价。

    ⚠️ `UNDECIDABLE` **只能**变成 `needs_review`。把它变成 `not_hit`
    是本项目反复出现的一类缺陷：报告说"这条规则没问题"，而事实是**没能判**。
    """
    if outcome.verdict is MatchVerdict.MATCHED:
        return RuleEvaluation(
            rule_code=spec.rule_code,
            rule_version=spec.rule_version,
            status=EvaluationStatus.HIT,
            risk_level=spec.risk_level,
            reason_code=outcome.reason_code or ReasonCode.CONDITION_MATCHED,
            reason_text=outcome.reason_text or "命中",
            located_text=outcome.located_text,
            hit_detail=outcome.detail,
        )

    if outcome.verdict is MatchVerdict.NOT_MATCHED:
        return RuleEvaluation(
            rule_code=spec.rule_code,
            rule_version=spec.rule_version,
            status=EvaluationStatus.NOT_HIT,
            risk_level=spec.risk_level,
            reason_code=outcome.reason_code or ReasonCode.CONDITION_NOT_MATCHED,
            reason_text=outcome.reason_text or "未命中",
            hit_detail=outcome.detail,
        )

    return _needs_review(
        spec,
        outcome.reason_code or ReasonCode.EVIDENCE_UNCERTAIN,
        outcome.reason_text or "证据不足，无法判断",
        detail=outcome.detail,
    )


def _not_applicable(
    spec: RuleSpec, applicability: ApplicabilityResult
) -> RuleEvaluation:
    return RuleEvaluation(
        rule_code=spec.rule_code,
        rule_version=spec.rule_version,
        status=EvaluationStatus.NOT_APPLICABLE,
        risk_level=spec.risk_level,
        reason_code=applicability.reason_code or ReasonCode.APPLICABILITY_NOT_MET,
        reason_text=applicability.reason_text or "该规则不适用于本合同",
        hit_detail={
            "applicability": applicability.applicability.value,
        },
    )


def _needs_review(
    spec: RuleSpec,
    reason_code: ReasonCode,
    reason_text: str,
    *,
    detail: dict[str, Any] | None = None,
) -> RuleEvaluation:
    return RuleEvaluation(
        rule_code=spec.rule_code,
        rule_version=spec.rule_version,
        status=EvaluationStatus.NEEDS_REVIEW,
        risk_level=spec.risk_level,
        reason_code=reason_code,
        reason_text=reason_text,
        hit_detail=detail or {},
    )
