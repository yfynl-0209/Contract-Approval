"""风险聚合（M5 / T7）—— **现算，不落库**（决策 ⑦）。

## 这份结论是"算出来的"，不是"读出来的"

M6 才有 `review_results` 表。M5 的聚合**每次请求现算**（§4.4），
理由与决策 ⑦ 一致：一次审查的结论必须能被**同一批 `rule_hits` 重新算出**，
否则"结论"与"依据"会各自漂移，而漂移是静默的。

## 四条判据（§4.4）

```text
总风险   = max(命中评价的 risk_level)；无命中 → low
完整性   = 任一评价 needs_review → needs_review
计数     = hit / not_hit / not_applicable / needs_review（四者之和 == 规则总数）
关注点   = 命中项 + 待判断项（各带规则码、原因码、证据引用）
```

⚠️ **`needs_review` 不参与 `max`**：它表达的是"这条**判不了**"，不是"这条有风险"。
把它算进总风险，会让"供应商代码没解析出来"把整份合同顶成高风险；
**但不提高风险等级 ≠ 可以忽略** —— 它让 `review_status` 变成 `needs_review`，
即"这份结论**不完整**"。两条缺一不可（验收 14）。

## 两处刻意 fail-closed 的地方

**① 空评价列表 → `needs_review`，不是 `low`。**

```text
"零条规则被评价" 与 "四十条都不命中" 都得到 overall = low
→ 前者会输出一份"低风险"的结论，而事实上**我们什么都没查**
```

计数之和与规则总数的一致性由调用方（T8）负责；本模块对"空列表"的处理是
**如实说结论不完整**，而不是给出一个看起来正常的低风险。

**② 未知状态直接抛错，绝不静默丢弃。**

新增一个 `EvaluationStatus` 成员却忘了在计数里登记时，丢失的那一票会让
"四者之和 == 总数"这条断言失败 —— 但只有真去断言才发现。这里在**聚合时**就抛，
错误信息直指"往 `_COUNTED` 里加映射"。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.enums import EvaluationStatus, ReviewStatus, RiskLevel
from app.rules.evaluator import RuleEvaluation

#: 风险等级排序。⚠️ **不能用字符串比较**：`"high" < "low" < "medium"` 是按字母序，
#: 于是"高风险"会被判成最低 —— 而它看起来像个正常的比较结果。
_RANK: Final[dict[RiskLevel, int]] = {
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
}

#: 评价状态 → 计数项。**穷举**：漏一个会让"四者之和 == 总数"失败，
#: 而 `_count_of` 对未登记的状态直接抛错，不会静默丢。
_COUNTED: Final[dict[EvaluationStatus, str]] = {
    EvaluationStatus.HIT: "hit",
    EvaluationStatus.NOT_HIT: "not_hit",
    EvaluationStatus.NOT_APPLICABLE: "not_applicable",
    EvaluationStatus.NEEDS_REVIEW: "needs_review",
}


@dataclass(frozen=True)
class FocusPoint:
    """一条需要人看的东西：**命中项**或**待判断项**。"""

    rule_code: str
    status: EvaluationStatus
    risk_level: RiskLevel
    reason_code: str | None
    reason_text: str | None
    #: 证据引用（没有证据时为空 —— 缺失类命中**天然没有**可指的片段）
    evidence_text: str | None

    @property
    def needs_human(self) -> bool:
        return self.status is EvaluationStatus.NEEDS_REVIEW


@dataclass(frozen=True)
class Aggregate:
    """一次审查的汇总结论。**字段名与 §4.6 的返回形状一一对应。**"""

    overall_risk_level: RiskLevel
    review_status: ReviewStatus
    #: 四态计数。⚠️ **四个都在** —— `not_hit` 也要（验收 22）：
    #: 少了它，工具 6 就答不出"这条规则为什么没报警"。
    counts: dict[str, int]
    focus_points: tuple[FocusPoint, ...]
    summary: str

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def to_json(self) -> dict[str, Any]:
        """按 §4.6 的形状序列化（工具 5 的 `aggregate` 字段）。"""
        return {
            "overall_risk_level": self.overall_risk_level.value,
            "review_status": self.review_status.value,
            "counts": dict(self.counts),
            "focus_points": [
                {
                    "rule_code": point.rule_code,
                    "status": point.status.value,
                    "risk_level": point.risk_level.value,
                    "reason_code": point.reason_code,
                    "reason_text": point.reason_text,
                    "evidence_text": point.evidence_text,
                }
                for point in self.focus_points
            ],
            "summary": self.summary,
        }


def _count_of(status: EvaluationStatus) -> str:
    try:
        return _COUNTED[status]
    except KeyError as exc:  # pragma: no cover - 只有新增状态时才会走到
        raise ValueError(
            f"评价状态 {status} 没有登记进 `_COUNTED` —— "
            "新增 `EvaluationStatus` 成员时必须同时加计数映射，否则它会从计数里静默消失"
        ) from exc


def aggregate(evaluations: Sequence[RuleEvaluation]) -> Aggregate:
    """把一批规则评价汇总成结论。**纯函数**：不读库、不写库。

    Raises:
        ValueError: 出现未登记的评价状态（见模块 docstring ②）。
    """
    counts: dict[str, int] = {name: 0 for name in _COUNTED.values()}
    hits: list[RuleEvaluation] = []
    pending: list[RuleEvaluation] = []

    for evaluation in evaluations:
        counts[_count_of(evaluation.status)] += 1
        if evaluation.status is EvaluationStatus.HIT:
            hits.append(evaluation)
        elif evaluation.status is EvaluationStatus.NEEDS_REVIEW:
            pending.append(evaluation)

    overall = (
        max((item.risk_level for item in hits), key=lambda level: _RANK[level])
        if hits
        else RiskLevel.LOW
    )

    # ⚠️ 空评价列表：`overall` 会是 low，但那是**因为没有任何输入**，不是"查过、没问题"。
    has_input = bool(evaluations)
    review_status = (
        ReviewStatus.NEEDS_REVIEW
        if pending or not has_input
        else ReviewStatus.COMPLETE
    )

    focus = _focus_points(hits, pending)

    return Aggregate(
        overall_risk_level=overall,
        review_status=review_status,
        counts=counts,
        focus_points=focus,
        summary=_summary(overall, review_status, counts, hits, has_input),
    )


def _focus_points(
    hits: list[RuleEvaluation], pending: list[RuleEvaluation]
) -> tuple[FocusPoint, ...]:
    """命中项在前、待判断项在后；各自按风险降序、规则码升序。

    排序**必须确定性**：同一批评价每次生成的关注点顺序固定，
    否则"结论没变但顺序变了"会让下游（M8 的差异对比、人工复看）以为是新问题。
    """
    ordered = sorted(
        hits, key=lambda item: (-_RANK[item.risk_level], item.rule_code)
    ) + sorted(pending, key=lambda item: (-_RANK[item.risk_level], item.rule_code))

    return tuple(
        FocusPoint(
            rule_code=item.rule_code,
            status=item.status,
            risk_level=item.risk_level,
            reason_code=item.reason_code.value if item.reason_code else None,
            reason_text=item.reason_text,
            evidence_text=item.evidence_text,
        )
        for item in ordered
    )


def _summary(
    overall: RiskLevel,
    review_status: ReviewStatus,
    counts: dict[str, int],
    hits: list[RuleEvaluation],
    has_input: bool,
) -> str:
    """**确定性兜底摘要**（决策 ⑥）。

    LLM 摘要由 T8 在可用时替换；不可用时必须是这一段 ——
    而不是空串或"暂无摘要"。一段空的摘要让人无法区分
    "没有风险"与"摘要生成失败"。
    """
    if not has_input:
        return (
            "本次没有任何规则被评价，无法给出结论 —— "
            "这不等于『没有风险』，请检查批次是否真的跑过。"
        )

    parts = [
        f"共评价 {sum(counts.values())} 条规则："
        f"命中 {counts['hit']} 条、未命中 {counts['not_hit']} 条、"
        f"不适用 {counts['not_applicable']} 条、待人工确认 {counts['needs_review']} 条。"
    ]

    if hits:
        highest = sorted(
            hits, key=lambda item: (-_RANK[item.risk_level], item.rule_code)
        )[:3]
        listed = "、".join(f"{item.rule_code}（{item.risk_level.value}）" for item in highest)
        parts.append(f"最高风险为 {overall.value}，主要来自：{listed}。")
    else:
        parts.append("未发现命中项。")

    if review_status is ReviewStatus.NEEDS_REVIEW:
        # ⚠️ 必须明说"不完整"。只给等级不给完整性，读到"低风险"的人
        # 不会知道还有规则根本没判出来。
        parts.append("有规则未能判定，本结论不完整，须人工复核。")

    return "".join(parts)
