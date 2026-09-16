"""四状态评价引擎的测试（M5 / T4）。

## 本文件守住的三类"写错了也不报错"

1. **`not_applicable` 被升级成 `needs_review`**：不适用规则重新回到人工队列，
   于是"不适用"这一态失去减少噪声的作用；
2. **`not_hit` 吞掉"判不了"**：报告说"这条规则没问题"，而事实是**根本没能判**；
3. **模型失败被静默换成确定性答案**：模型挂了一整天，报告里全是"已判定"，
   而实际上一条都没真的问过模型。

三类都不会抛异常，只会让**结论静默地错**。
"""

from __future__ import annotations

import pytest

from app.enums import (
    BboxPrecision,
    EvaluationStatus,
    FieldStatus,
    MatchMode,
    ReasonCode,
    RiskLevel,
    TextPrecision,
)
from app.ports.field_contract import EvidenceSpan, ExtractedField
from app.rules.applicability import ReviewContext
from app.rules.evaluator import (
    RuleSpec,
    build_rule_spec,
    evaluate_rule,
)
from app.rules.matching import MatchResult, MatchVerdict
from app.schemas import ExprMatchConfig, KeywordMatchConfig, RuleConfigError

CURRENCY = "CNY"


# ============================================================
# 夹具
# ============================================================


def _span(text: str) -> EvidenceSpan:
    return EvidenceSpan(
        page=1,
        block_id="p1-b1",
        text=text,
        bbox=(0.0, 0.0, 10.0, 10.0),
        char_start=0,
        char_end=max(1, len(text)),
        text_precision=TextPrecision.CHAR,
        bbox_precision=BboxPrecision.CHAR,
    )


def _field(
    field_code: str,
    status: FieldStatus,
    *,
    value_decimal: str | None = None,
    value_text: str = "",
    currency: str | None = None,
) -> ExtractedField:
    payload: dict[str, object] = {
        "field_code": field_code,
        "status": status,
        "value_text": value_text,
        "value_decimal": value_decimal,
        "currency": currency,
    }
    if status is FieldStatus.EXTRACTED:
        payload["evidence"] = (_span(value_text or value_decimal or "x"),)
    elif status is FieldStatus.UNCERTAIN:
        payload["reason_code"] = ReasonCode.EVIDENCE_UNCERTAIN
    elif status is FieldStatus.FAILED:
        payload["reason_code"] = ReasonCode.EXTRACTION_FAILED
    return ExtractedField(**payload)  # type: ignore[arg-type]


def _spec(
    *,
    match_mode: str = "keyword",
    match_text: str,
    applies_when_json: str | None = None,
    fallback_match_json: str | None = None,
    exclude_text: str | None = None,
    risk_level: str = "high",
    rule_code: str = "R_TEST",
) -> RuleSpec:
    return build_rule_spec(
        rule_code=rule_code,
        rule_name="测试规则",
        risk_level=risk_level,
        rule_version=1,
        match_mode=match_mode,
        match_text=match_text,
        applies_when_json=applies_when_json,
        fallback_match_json=fallback_match_json,
        exclude_text=exclude_text,
    )


def _evaluate(spec: RuleSpec, **kwargs: object):
    defaults: dict[str, object] = {
        "context": ReviewContext(),
        "text": None,
        "fields": {},
        "default_currency": CURRENCY,
    }
    defaults.update(kwargs)
    return evaluate_rule(spec, **defaults)  # type: ignore[arg-type]


# ============================================================
# 1. 适用性之后直接返回，**不读字段**
# ============================================================


def test_not_applicable_is_not_upgraded_to_needs_review() -> None:
    """⚠️ 不适用规则**不得**因为"字段读不出来"而升级成 `needs_review`。

    规则库里就有这个场景：标准品采购合同不适用知识产权规则（`HT-2026-0004`）。
    若这里读了字段、发现 `party_a` 是 `uncertain` 就判 `needs_review`，
    那条规则会重新回到人工队列 —— 而它**本来就不该参与这次判断**。
    `not_applicable` 的全部价值就是减少噪声，被升级一次就没了。
    """
    spec = _spec(
        match_text='{"keywords": ["知识产权"], "absent": true}',
        applies_when_json='{"contract_types": ["development"]}',
    )

    result = _evaluate(
        spec,
        context=ReviewContext(contract_type="goods"),
        text=None,  # 正文/字段都刻意给最差的情况
        fields={"party_a": _field("party_a", FieldStatus.UNCERTAIN)},
    )

    assert result.status is EvaluationStatus.NOT_APPLICABLE
    assert result.reason_code is ReasonCode.APPLICABILITY_NOT_MET


def test_missing_context_makes_the_rule_undecidable() -> None:
    """合同类型没提供 → `needs_review(CONTEXT_MISSING)`，**不是**不适用。

    "判不了"与"不适用"处置完全不同：前者要人去补立场，后者到此为止。
    """
    spec = _spec(
        match_text='{"keywords": ["知识产权"]}',
        applies_when_json='{"contract_types": ["development"]}',
    )

    result = _evaluate(spec, context=ReviewContext(contract_type=None))

    assert result.status is EvaluationStatus.NEEDS_REVIEW
    assert result.reason_code is ReasonCode.CONTEXT_MISSING


def test_conflicting_party_context_makes_dependent_rules_undecidable() -> None:
    spec = _spec(
        match_text='{"keywords": ["预付款"]}',
        applies_when_json='{"our_contract_labels": ["party_a"]}',
    )

    result = _evaluate(
        spec,
        context=ReviewContext(
            our_contract_label="party_a", party_context_status="conflict"
        ),
    )

    assert result.status is EvaluationStatus.NEEDS_REVIEW
    assert result.reason_code is ReasonCode.CONTEXT_CONFLICT


# ============================================================
# 2. 确定性条件
# ============================================================


def test_keyword_hit_becomes_hit_with_evidence_hint() -> None:
    spec = _spec(match_text='{"keywords": ["自动续约"]}')

    result = _evaluate(spec, text="本合同期满后自动续约一年。")

    assert result.status is EvaluationStatus.HIT
    assert result.located_text == "自动续约"
    assert result.risk_level is RiskLevel.HIGH


def test_keyword_miss_becomes_not_hit_with_a_reason_code() -> None:
    """`not_hit` 也要有原因码 —— 否则"这条规则为什么没报警"只能去读中文。"""
    spec = _spec(match_text='{"keywords": ["自动续约"]}')

    result = _evaluate(spec, text="本合同期满即终止。")

    assert result.status is EvaluationStatus.NOT_HIT
    assert result.reason_code is ReasonCode.CONDITION_NOT_MATCHED


def test_null_text_makes_keyword_undecidable() -> None:
    spec = _spec(match_text='{"keywords": ["保密"]}')

    result = _evaluate(spec, text=None)

    assert result.status is EvaluationStatus.NEEDS_REVIEW
    assert result.reason_code is ReasonCode.EVIDENCE_UNCERTAIN


def test_exclude_text_is_applied_by_the_engine() -> None:
    """引擎必须把 `exclude_text` 传下去（它来自另一列，不在 `match_text` 里）。"""
    spec = _spec(
        match_text='{"keywords": ["自动续约"]}',
        exclude_text="不自动,不得自动",
    )

    result = _evaluate(spec, text="本合同期满后不自动续约。")

    assert result.status is EvaluationStatus.NOT_HIT


def test_expr_hit_and_miss() -> None:
    spec = _spec(match_mode="expr", match_text='{"field": "pay_days", "op": "gt", "value": 60}')

    hit = _evaluate(
        spec, fields={"pay_days": _field("pay_days", FieldStatus.EXTRACTED, value_decimal="90")}
    )
    miss = _evaluate(
        spec, fields={"pay_days": _field("pay_days", FieldStatus.EXTRACTED, value_decimal="30")}
    )

    assert hit.status is EvaluationStatus.HIT
    assert hit.hit_detail == {"actual": "90", "op": "gt", "threshold": "60.0"}
    assert miss.status is EvaluationStatus.NOT_HIT


def test_expr_not_found_is_needs_review_not_not_hit() -> None:
    """⚠️ **决策 ④ 在引擎层的落地**：阈值类规则遇到 `not_found` 必须 `needs_review`。

    判 `not_hit` 会输出"这条规则没问题"，而事实是**根本没能判**。

    对照（方向相反、因此走另一条路径）：`is_null` 遇到 `not_found` **就是命中** ——
    见 `tests/test_rule_matching.py`。
    """
    spec = _spec(
        match_mode="expr",
        match_text='{"field": "prepay_ratio", "op": "gt", "value": 0.3}',
    )

    result = _evaluate(
        spec,
        fields={"prepay_ratio": _field("prepay_ratio", FieldStatus.NOT_FOUND)},
    )

    assert result.status is EvaluationStatus.NEEDS_REVIEW
    assert result.reason_code is ReasonCode.EVIDENCE_UNCERTAIN
    assert result.status is not EvaluationStatus.NOT_HIT


def test_field_absent_from_the_contract_is_needs_review() -> None:
    """字段根本不在解析结果里 → 判不了（不是"缺失"、也不是"不成立"）。"""
    spec = _spec(match_mode="expr", match_text='{"field": "amount", "op": "gt", "value": 1}')

    result = _evaluate(spec, fields={})

    assert result.status is EvaluationStatus.NEEDS_REVIEW
    assert result.reason_code is ReasonCode.EXTRACTION_FAILED


# ============================================================
# 3. llm 模式：**没有模型是常态**，不是异常
# ============================================================


def test_llm_without_a_model_uses_the_rule_fallback() -> None:
    """**计划 §2.2**：无 GPU 也能完成全部业务开发 —— 9 条 llm 规则全都配了 fallback。

    因此这条路是**常态路径**。它必须与确定性分支给出同一套语义
    （同样的四态、同样的证据提示），而不是另一套"降级专用"逻辑。
    """
    spec = _spec(
        match_mode="llm",
        match_text='{"instruction": "判断违约金是否单向不利"}',
        fallback_match_json='{"keywords": ["乙方不承担"], "absent": false}',
    )

    result = _evaluate(spec, text="乙方不承担任何赔偿责任。", llm_judge=None)

    assert result.status is EvaluationStatus.HIT
    assert result.located_text == "乙方不承担"


def test_llm_without_a_model_and_without_fallback_is_model_unavailable() -> None:
    spec = _spec(
        match_mode="llm", match_text='{"instruction": "判断是否公平"}'
    )

    result = _evaluate(spec, text="任意文本", llm_judge=None)

    assert result.status is EvaluationStatus.NEEDS_REVIEW
    assert result.reason_code is ReasonCode.MODEL_UNAVAILABLE


def test_a_failed_model_call_does_not_silently_fall_back() -> None:
    """⚠️ **单次调用失败不得静默换成确定性答案**。

    合并两种情形的后果很隐蔽：模型挂了一整天，报告里全是"已判定"，
    而实际上**一条都没真的问过模型** —— 于是"模型质量"这个指标永远看不出问题。
    要改善只能显式重跑（`force`），而不是让引擎偷偷换答案。
    """
    spec = _spec(
        match_mode="llm",
        match_text='{"instruction": "判断违约金是否单向不利"}',
        fallback_match_json='{"keywords": ["乙方不承担"]}',
    )

    def failing_judge(_spec: RuleSpec, _text: str) -> MatchResult:
        return MatchResult(
            MatchVerdict.UNDECIDABLE, ReasonCode.MODEL_UNAVAILABLE, "模型调用失败"
        )

    result = _evaluate(
        spec, text="乙方不承担任何赔偿责任。", llm_judge=failing_judge
    )

    assert result.status is EvaluationStatus.NEEDS_REVIEW, (
        "有 fallback 也不能接手 —— 那会把'模型失败'伪装成'已判定'"
    )
    assert result.reason_code is ReasonCode.MODEL_UNAVAILABLE


def test_a_successful_model_judgement_is_used() -> None:
    spec = _spec(match_mode="llm", match_text='{"instruction": "判断是否公平"}')

    def judge(_spec: RuleSpec, _text: str) -> MatchResult:
        return MatchResult(
            MatchVerdict.NOT_MATCHED, ReasonCode.CONDITION_NOT_MATCHED, "模型判未命中"
        )

    result = _evaluate(spec, text="任意文本", llm_judge=judge)

    assert result.status is EvaluationStatus.NOT_HIT
    assert result.reason_text == "模型判未命中"


# ============================================================
# 4. 规格构造与不变量
# ============================================================


def test_build_rule_spec_parses_all_four_sources() -> None:
    """`RuleSpec` 的四份配置来自**四列**，缺一列都会让某条规则评不出来。"""
    spec = _spec(
        match_text='{"keywords": ["自动续约"]}',
        applies_when_json='{"our_business_roles": ["buyer"]}',
        exclude_text="不自动",
        risk_level="medium",
    )

    assert isinstance(spec.config, KeywordMatchConfig)
    assert spec.applies_when is not None
    assert spec.exclude_phrases == ("不自动",)
    assert spec.risk_level is RiskLevel.MEDIUM
    assert spec.is_llm is False


def test_build_rule_spec_rejects_illegal_config() -> None:
    with pytest.raises(RuleConfigError):
        _spec(match_text='{"keywords": []}')  # 空数组是非法写法


def test_every_state_carries_a_reason_code() -> None:
    """四态**都**要带稳定机器码 —— 不产出空码。

    空码的后果：统计与看板没法按原因聚合，而"为什么有 12 条 needs_review"
    变成只能人工一条条读中文。
    """
    cases = [
        _evaluate(_spec(match_text='{"keywords": ["x"]}'), text="x"),
        _evaluate(_spec(match_text='{"keywords": ["x"]}'), text="y"),
        _evaluate(_spec(match_text='{"keywords": ["x"]}'), text=None),
        _evaluate(
            _spec(
                match_text='{"keywords": ["x"]}',
                applies_when_json='{"contract_types": ["development"]}',
            ),
            context=ReviewContext(contract_type="goods"),
        ),
    ]

    assert {case.status for case in cases} == {
        EvaluationStatus.HIT,
        EvaluationStatus.NOT_HIT,
        EvaluationStatus.NEEDS_REVIEW,
        EvaluationStatus.NOT_APPLICABLE,
    }
    assert all(isinstance(case.reason_code, ReasonCode) for case in cases)


def test_rule_version_is_carried_into_the_evaluation() -> None:
    """评价必须记录**本次执行的规则版本**（规则随后被改，历史结论仍可复现）。"""
    spec = RuleSpec(
        rule_code="R",
        rule_name="n",
        risk_level=RiskLevel.LOW,
        rule_version=7,
        match_mode=MatchMode.KEYWORD,
        config=KeywordMatchConfig(keywords=["x"]),
    )

    result = _evaluate(spec, text="x")

    assert result.rule_version == 7


def test_expr_config_is_dispatched_by_type_not_by_string() -> None:
    """分派按**类型**（`isinstance`），不按 `match_mode` 字符串。

    按字符串分派时，新增一种匹配模式必须同步改两个地方（DTO 的映射表与引擎的
    分派），而漏改一处不会报错 —— 规则会落进"没有对应分支"或错误的分支。
    """
    spec = RuleSpec(
        rule_code="R",
        rule_name="n",
        risk_level=RiskLevel.LOW,
        rule_version=1,
        match_mode=MatchMode.KEYWORD,  # 刻意写错：真实 config 是 expr
        config=ExprMatchConfig(field="pay_days", op="gt", value=60),
    )

    result = _evaluate(
        spec,
        fields={"pay_days": _field("pay_days", FieldStatus.EXTRACTED, value_decimal="90")},
    )

    assert result.status is EvaluationStatus.HIT, (
        "config 是 ExprMatchConfig 就必须走 expr 分支，与 match_mode 字段无关"
    )
