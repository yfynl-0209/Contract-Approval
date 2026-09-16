"""合格性脚本的判定逻辑（M11 Task 5）—— **不打真实模型**。

用三种失败形态的判定钩子构造 `Row`，断言 `decide()` 的阈值行为与
脚本 `main()` 的退出码。真实模型的"手感"由人工跑
`python scripts/check_llm_qualification.py` 记录基线（README）。
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.enums import ReasonCode
from app.rules.matching import MatchResult, MatchVerdict
from scripts import check_llm_qualification as qual


def _match() -> MatchResult:
    return MatchResult(MatchVerdict.MATCHED, ReasonCode.CONDITION_MATCHED, "ok")


def _unavailable() -> MatchResult:
    return MatchResult(
        MatchVerdict.UNDECIDABLE,
        ReasonCode.MODEL_UNAVAILABLE,
        "模型不可用",
        detail={"judged_by": "llm"},
    )


def _fabricated() -> MatchResult:
    return MatchResult(
        MatchVerdict.UNDECIDABLE,
        ReasonCode.EVIDENCE_UNCERTAIN,
        "引用不在正文里",
        detail={"judged_by": "llm", "verdict_discarded": "evidence_not_found"},
    )


def _all_rows(results) -> tuple:
    """把 (sample, rule, result) 展开成 Row 元组（规则码可随意，判定不看码）。"""
    from scripts.check_llm_qualification import Row

    return tuple(
        Row(sample, rule, result, 0.1) for sample, rule, result in results
    )


# ------------------------------------------------------------
# 计数
# ------------------------------------------------------------


def test_counts_categorize_the_three_failure_modes() -> None:
    rows = _all_rows(
        [
            ("s1", "R1", _match()),
            ("s1", "R2", _unavailable()),
            ("s2", "R1", _fabricated()),
        ]
    )
    counts = qual.counts(rows)

    # ⚠️ fabricated 的 verdict 是 UNDECIDABLE（引用作废 = 判不了）—— 不算 decidable
    assert counts == {"total": 3, "unavailable": 1, "fabricated": 1, "decidable": 1}


def test_missed_must_match_lists_false_negatives() -> None:
    rows = _all_rows(
        [
            # 样本 1 的必判项命中；样本 2 的没命中
            ("一方独担违约责任", "LIAB_UNEQUAL_AGAINST_PARTY_A", _match()),
            ("保密义务单方承担", "CONF_UNILATERAL_AGAINST_PARTY_B", _match()),
        ]
    )
    # 只保留必判相关的行：删掉样本 2 的命中，让它成为假阴性
    rows = tuple(r for r in rows if r.rule != "CONF_UNILATERAL_AGAINST_PARTY_B")

    missed = qual.missed_must_match(rows)

    assert missed == [("保密义务单方承担", "CONF_UNILATERAL_AGAINST_PARTY_B")]


# ------------------------------------------------------------
# 判定阈值
# ------------------------------------------------------------


def test_a_perfect_model_passes() -> None:
    rows = _all_rows(
        [
            ("一方独担违约责任", "LIAB_UNEQUAL_AGAINST_PARTY_A", _match()),
            ("保密义务单方承担", "CONF_UNILATERAL_AGAINST_PARTY_B", _match()),
            ("条款均衡（假阳性探测）", "R1", _match()),
        ]
    )
    ok, reasons = qual.decide(rows)
    assert ok is True and reasons == []


def test_a_model_that_never_returns_valid_json_is_rejected() -> None:
    """全部 MODEL_UNAVAILABLE → 不合格，原因指向连通性/格式，不是"合同没写"。"""
    rows = _all_rows(
        [("s", f"R{i}", _unavailable()) for i in range(9)]
    )
    ok, reasons = qual.decide(rows)

    assert ok is False
    assert any("MODEL_UNAVAILABLE" in reason for reason in reasons)
    assert any("端点/连通性" in reason for reason in reasons)


def test_a_model_that_paraphrases_its_evidence_is_rejected() -> None:
    """引用改写 → 证据作废条数超阈值（>1）→ 不合格。"""
    rows = _all_rows(
        [("s", f"R{i}", _fabricated() if i < 2 else _match()) for i in range(9)]
    )
    ok, reasons = qual.decide(rows)

    assert ok is False
    assert any("逐字摘录能力" in reason for reason in reasons)


def test_a_missing_must_match_is_rejected_even_when_other_metrics_are_green() -> None:
    """明确存在的风险条款没判出来 → 不合格，即使其余指标全绿。"""
    rows = _all_rows(
        [
            ("一方独担违约责任", "LIAB_UNEQUAL_AGAINST_PARTY_A", _match()),
            # 保密义务单方承担的必判项没判出来（但判成了明确的 not_hit —— 不算不可判）
            (
                "保密义务单方承担",
                "CONF_UNILATERAL_AGAINST_PARTY_B",
                MatchResult(
                    MatchVerdict.NOT_MATCHED,
                    ReasonCode.CONDITION_NOT_MATCHED,
                    "没看出问题",
                ),
            ),
            ("条款均衡（假阳性探测）", "R1", _match()),
        ]
    )
    ok, reasons = qual.decide(rows)

    assert ok is False
    assert any(
        "保密义务单方承担" in reason and "CONF_UNILATERAL_AGAINST_PARTY_B" in reason
        for reason in reasons
    )


def test_undecidable_ratio_below_threshold_is_rejected() -> None:
    """明确结论比例 < 85% → 不合格。"""
    results = [(_match() if i < 7 else _unavailable()) for i in range(9)]
    rows = _all_rows([("s", f"R{i}", r) for i, r in enumerate(results)])
    counts = qual.counts(rows)
    assert counts["decidable"] / counts["total"] < qual.MIN_DECIDABLE_RATIO

    ok, reasons = qual.decide(rows)
    assert ok is False
    assert any("明确结论比例" in reason for reason in reasons)


def test_declared_undecidables_do_not_count_into_the_ratio() -> None:
    """主题缺失/信息不可得的行（样本显式豁免）**不计入**可判定分母。

    对"正文没有知识产权条款"的合同判 needs_review 转人工，
    是系统的设计输出，不是模型缺陷 —— 否则一条诚实的模型
    会被 85% 的阈值误杀（deepseek-flash 首次实测正是 63%）。
    """
    rows = _all_rows(
        [
            ("一方独担违约责任", "LIAB_UNEQUAL_AGAINST_PARTY_A", _match()),
            ("一方独担违约责任", "IP_TRANSFER_AWAY_FROM_PARTY_A", _unavailable_like_undecidable()),
            ("保密义务单方承担", "CONF_UNILATERAL_AGAINST_PARTY_B", _match()),
        ]
    )
    ratio, decided, denominator = qual.decidability_ratio(rows)

    assert denominator == 2, "豁免行（IP 主题缺失）不应计入分母"
    assert decided == 2 and ratio == 1.0
    ok, reasons = qual.decide(rows)
    assert ok is True and reasons == []


def _unavailable_like_undecidable() -> MatchResult:
    """判不了（豁免场景）但**不是** MODEL_UNAVAILABLE —— 两类不可混淆。"""
    return MatchResult(
        MatchVerdict.UNDECIDABLE,
        ReasonCode.EVIDENCE_UNCERTAIN,
        "模型无法可靠判断：正文未涉及该主题",
        detail={"judged_by": "llm"},
    )


# ------------------------------------------------------------
# main() 的配置缺失分支（不打模型）
# ----------------------------------------------------------


def test_main_returns_2_when_llm_is_not_configured(monkeypatch) -> None:
    """三项留空 = "没得测"（退出码 2），不是"模型不合格"（退出码 1）。"""
    monkeypatch.setattr(qual.settings, "llm_base_url", "")
    monkeypatch.setattr(qual.settings, "llm_api_key", "")
    monkeypatch.setattr(qual.settings, "llm_model", "")

    assert qual.main([]) == 2
