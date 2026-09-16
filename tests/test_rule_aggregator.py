"""风险聚合的测试（M5 / T7）。

## 本文件守住的四类"写错了也不报错"

1. **`needs_review` 被算进总风险** —— "供应商代码没解析出来"会把整份合同顶成高风险；
   反过来，**只算总风险、不提完整性**，会让读到"低风险"的人不知道还有规则根本没判出来。
   两头都要防（验收 14 的两半）。
2. **风险等级按字符串比大小** —— 字母序是 `"high" < "low" < "medium"`，
   于是"高风险"会被判成**最低**，而它看起来像个正常的比较结果。
3. **`not_hit` 不计数** —— 表上只有三个计数列，于是"三计数之和 = 总数"这种写法会
   把 `not_hit` 悄悄当成 0 并**通过**。少了它，工具 6 就答不出"这条规则为什么没报警"。
4. **空评价列表被当成低风险** —— "零条规则被评价"与"四十条都不命中"得到同一个 `low`，
   而前者意味着**我们什么都没查**。
"""

from __future__ import annotations

from app.enums import EvaluationStatus, ReasonCode, ReviewStatus, RiskLevel
from app.rules.aggregator import _COUNTED, aggregate
from app.rules.evaluator import RuleEvaluation


def _evaluation(
    rule_code: str,
    status: EvaluationStatus = EvaluationStatus.HIT,
    risk_level: RiskLevel = RiskLevel.MEDIUM,
    *,
    reason_code: ReasonCode | None = ReasonCode.CONDITION_MATCHED,
    evidence_text: str | None = None,
) -> RuleEvaluation:
    return RuleEvaluation(
        rule_code=rule_code,
        rule_version=1,
        status=status,
        risk_level=risk_level,
        reason_code=reason_code,
        reason_text=f"{rule_code} 的说明",
        evidence_text=evidence_text,
    )


# ============================================================
# 1. 总风险
# ============================================================


def test_overall_risk_is_the_max_of_hits() -> None:
    result = aggregate(
        [
            _evaluation("R_LOW", risk_level=RiskLevel.LOW),
            _evaluation("R_MED", risk_level=RiskLevel.MEDIUM),
            _evaluation("R_HIGH", risk_level=RiskLevel.HIGH),
        ]
    )

    assert result.overall_risk_level is RiskLevel.HIGH


def test_risk_ranking_is_not_alphabetical() -> None:
    """⚠️ `"high" < "low" < "medium"` 是按字母序 —— 用字符串比大小会把高风险判成最低。

    这也是为什么聚合不写成 `max(risk_level)`：那样**能跑、不报错**，
    只是结论反了。这条用 `{low, high}` 卡住它：字母序下 `max` 会返回 `"low"`。
    """
    result = aggregate(
        [
            _evaluation("R_LOW", risk_level=RiskLevel.LOW),
            _evaluation("R_HIGH", risk_level=RiskLevel.HIGH),
        ]
    )

    assert result.overall_risk_level is RiskLevel.HIGH


def test_no_hits_means_low() -> None:
    result = aggregate(
        [
            _evaluation("R_A", EvaluationStatus.NOT_HIT, RiskLevel.HIGH),
            _evaluation("R_B", EvaluationStatus.NOT_APPLICABLE, RiskLevel.HIGH),
        ]
    )

    assert result.overall_risk_level is RiskLevel.LOW
    assert result.review_status is ReviewStatus.COMPLETE


def test_not_applicable_risk_level_does_not_leak_into_the_total() -> None:
    """不适用规则的 `risk_level` 与本次结论无关 —— 它连判都没判。"""
    result = aggregate([_evaluation("R_A", EvaluationStatus.NOT_APPLICABLE, RiskLevel.HIGH)])

    assert result.overall_risk_level is RiskLevel.LOW


# ============================================================
# 2. 完整性（验收 14 的两半）
# ============================================================


def test_needs_review_does_not_raise_the_risk_but_marks_incomplete() -> None:
    """**验收 14**：只有一条 medium 命中 + 一条 `needs_review`
    → 总风险 `medium`（`needs_review` **不**提高等级），但完整性为 `needs_review`。

    两半都断言，是因为只做前一半会让"结论不完整"这件事**消失**：
    读报告的人看到一个 `medium`，不会知道还有规则根本没判出来。
    """
    result = aggregate(
        [
            _evaluation("R_MED", risk_level=RiskLevel.MEDIUM),
            _evaluation(
                "R_PENDING",
                EvaluationStatus.NEEDS_REVIEW,
                RiskLevel.HIGH,
                reason_code=ReasonCode.MODEL_UNAVAILABLE,
            ),
        ]
    )

    assert result.overall_risk_level is RiskLevel.MEDIUM, "needs_review 不得提高总风险"
    assert result.review_status is ReviewStatus.NEEDS_REVIEW
    assert "不完整" in result.summary


def test_pending_only_contract_is_low_but_incomplete() -> None:
    """一条都没命中、但有一条判不了 —— **不是**"低风险、没问题"。"""
    result = aggregate(
        [_evaluation("R_PENDING", EvaluationStatus.NEEDS_REVIEW, RiskLevel.MEDIUM)]
    )

    assert result.overall_risk_level is RiskLevel.LOW
    assert result.review_status is ReviewStatus.NEEDS_REVIEW


def test_empty_evaluations_are_not_a_clean_low_risk() -> None:
    """⚠️ **零条评价**与"四十条都不命中"都会得到 `low`。

    前者意味着**我们什么都没查**，却会输出一份看起来正常的"低风险"结论。
    因此空输入一律判"结论不完整"，且摘要必须点明原因。
    """
    result = aggregate([])

    assert result.overall_risk_level is RiskLevel.LOW
    assert result.review_status is ReviewStatus.NEEDS_REVIEW
    assert "没有任何规则被评价" in result.summary
    assert result.total == 0


# ============================================================
# 3. 四态计数（验收 1 / 22）
# ============================================================


def test_four_counts_sum_to_the_total() -> None:
    """**验收 1 的算式**：四者之和 == 规则总数。

    ⚠️ 不能写成"三个计数之和 == 40" —— 那样会把 `not_hit` 当成 0 并**通过**。
    """
    evaluations = [
        _evaluation("R_1"),
        _evaluation("R_2", EvaluationStatus.NOT_HIT),
        _evaluation("R_3", EvaluationStatus.NOT_APPLICABLE),
        _evaluation("R_4", EvaluationStatus.NEEDS_REVIEW),
        _evaluation("R_5", EvaluationStatus.NOT_HIT),
    ]

    result = aggregate(evaluations)

    assert result.counts == {
        "hit": 1,
        "not_hit": 2,
        "not_applicable": 1,
        "needs_review": 1,
    }
    assert result.total == len(evaluations)


def test_not_hit_is_really_counted() -> None:
    """单独立一条：`not_hit` 必须**真的**被数出来（验收 22）。

    工具 6 要回答"这条规则为什么没报警"，靠的就是这批记录 ——
    把它们从计数里省掉，等于把"未命中"与"没跑过"混成一件事。
    """
    result = aggregate([_evaluation("R_A", EvaluationStatus.NOT_HIT)])

    assert result.counts["not_hit"] == 1
    assert result.total == 1


def test_counted_mapping_is_exhaustive() -> None:
    """**每一个 `EvaluationStatus` 都必须有计数项。**

    漏一个的后果是那一票**从计数里静默消失** —— 而"四者之和 == 总数"这条断言
    只有真去断言才发现。这里把穷举性本身钉住：新增状态却忘了加映射时，这条会失败。
    """
    assert set(_COUNTED) == set(EvaluationStatus)


# ============================================================
# 4. 关注点与摘要
# ============================================================


def test_focus_points_are_hits_then_pending_sorted_by_risk() -> None:
    result = aggregate(
        [
            _evaluation("R_LOW_HIT", risk_level=RiskLevel.LOW),
            _evaluation("R_HIGH_HIT", risk_level=RiskLevel.HIGH),
            _evaluation("R_PENDING", EvaluationStatus.NEEDS_REVIEW, RiskLevel.HIGH),
            _evaluation("R_NOT_HIT", EvaluationStatus.NOT_HIT),
        ]
    )

    assert [point.rule_code for point in result.focus_points] == [
        "R_HIGH_HIT",
        "R_LOW_HIT",
        "R_PENDING",
    ]
    assert result.focus_points[-1].needs_human is True


def test_focus_point_carries_code_reason_and_evidence() -> None:
    result = aggregate(
        [_evaluation("R_A", risk_level=RiskLevel.HIGH, evidence_text="百分之六十作为预付款")]
    )

    point = result.focus_points[0]
    assert point.rule_code == "R_A"
    assert point.reason_code == ReasonCode.CONDITION_MATCHED.value
    assert point.evidence_text == "百分之六十作为预付款"


def test_a_focus_point_may_have_no_evidence() -> None:
    """缺失类命中**天然没有**可指的片段 —— 不得因此报错或丢掉这条关注点。"""
    result = aggregate(
        [_evaluation("R_ABSENT", risk_level=RiskLevel.MEDIUM, evidence_text=None)]
    )

    assert result.focus_points[0].evidence_text is None


def test_ordering_is_deterministic() -> None:
    """同一批评价两次聚合，关注点顺序必须一致。

    顺序不稳定会让下游（M8 的差异对比、人工复看）把"顺序变了"当成新问题。
    """
    evaluations = [
        _evaluation("R_Z", risk_level=RiskLevel.HIGH),
        _evaluation("R_A", risk_level=RiskLevel.HIGH),
        _evaluation("R_M", risk_level=RiskLevel.MEDIUM),
    ]

    first = [point.rule_code for point in aggregate(evaluations).focus_points]
    second = [point.rule_code for point in aggregate(list(reversed(evaluations))).focus_points]

    assert first == second == ["R_A", "R_Z", "R_M"]


def test_summary_names_the_level_and_the_counts() -> None:
    result = aggregate(
        [
            _evaluation("R_A", risk_level=RiskLevel.HIGH),
            _evaluation("R_B", EvaluationStatus.NOT_HIT),
        ]
    )

    assert "high" in result.summary
    assert "命中 1 条" in result.summary
    assert "未命中 1 条" in result.summary


def test_to_json_matches_the_tool5_shape() -> None:
    """§4.6 的 `aggregate` 形状：四个顶层键，`counts` 四个都在。"""
    payload = aggregate([_evaluation("R_A", risk_level=RiskLevel.HIGH)]).to_json()

    assert set(payload) == {
        "overall_risk_level",
        "review_status",
        "counts",
        "focus_points",
        "summary",
    }
    assert set(payload["counts"]) == {"hit", "not_hit", "not_applicable", "needs_review"}
    assert payload["overall_risk_level"] == "high"
