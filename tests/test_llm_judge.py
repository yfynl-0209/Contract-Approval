"""`llm` 规则判定的测试（M5 / T5）。

## 本文件守住的四类"写错了也不报错"

1. **幻觉证据被采信** —— 模型编一段"合同原文"，结论照常命中，而审批人按图索骥找不到；
2. **没有出处的命中** —— 结论对，但**无法核验**，在本项目里与幻觉在使用上没有区别；
3. **模型失败被换成确定性答案** —— 模型挂了一整天，报告里全是"已判定"；
4. **截断后照常下结论** —— 被截掉的那半页里可能正写着违约条款，
   而下游分不清"判断为否"与"只读了一部分"。

四类都**不会**抛异常，只会让结论静默地错。
"""

from __future__ import annotations

import pytest

from app.adapters.llm.mock_llm import MockLlm
from app.enums import EvaluationStatus, ReasonCode
from app.rules.applicability import ReviewContext
from app.rules.evaluator import RuleSpec, build_rule_spec, evaluate_rule
from app.rules.llm_judge import PROMPT_VERSION, LlmVerdict, make_llm_judge
from app.rules.matching import MatchVerdict

#: 夹具正文（含一条明显的单向违约条款）
TEXT = "第八条 乙方不承担任何赔偿责任。\n第九条 保密义务在合同终止后继续有效三年。"


def _spec(**kwargs: object) -> RuleSpec:
    defaults = {
        "rule_code": "LIAB_UNEQUAL_TEST",
        "rule_name": "违约责任单向不利",
        "risk_level": "high",
        "rule_version": 1,
        "match_mode": "llm",
        "match_text": '{"instruction": "判断违约责任是否单向免除乙方责任"}',
    }
    defaults.update(kwargs)
    return build_rule_spec(**defaults)  # type: ignore[arg-type]


def _verdict(llm: MockLlm, **fields: object) -> None:
    """喂一条模型应答。"""
    llm.feed_json({"verdict": "undecidable", "reason": "默认", **fields})


# ============================================================
# 1. 什么时候**不构造** judge
# ============================================================


def test_no_gateway_means_no_judge() -> None:
    """没有接入模型 → `None`，引擎据此走规则自带的 fallback。

    ⚠️ 这与"模型调用失败"必须是**两条路**：前者是配置事实（安静降级），
    后者是运行期抖动（要被人看见）。
    """
    assert make_llm_judge(None, max_input_chars=1000) is None


def test_unavailable_gateway_means_no_judge() -> None:
    assert make_llm_judge(MockLlm(available=False), max_input_chars=1000) is None


# ============================================================
# 2. 证据反向核验（防幻觉）
# ============================================================


def test_a_hit_with_a_verifiable_quote_is_accepted() -> None:
    llm = MockLlm(
        json_responses=[
            {
                "verdict": "matched",
                "reason": "乙方完全不承担赔偿责任",
                "evidence_quote": "乙方不承担任何赔偿责任",
            }
        ]
    )
    judge = make_llm_judge(llm, max_input_chars=10_000)

    result = judge(_spec(), TEXT)

    assert result.verdict is MatchVerdict.MATCHED
    assert result.located_text == "乙方不承担任何赔偿责任"
    assert result.detail["judged_by"] == "llm"
    assert result.detail["prompt_version"] == PROMPT_VERSION


def test_a_hit_with_a_fabricated_quote_is_discarded() -> None:
    """⚠️ **防幻觉的核心**：证据必须真的出自正文。

    模型给出一个**很像**但合同里没有的片段 —— 若采信，审批人按图索骥找不到，
    而报告上写着"命中，证据：…"。计划 §10.1 第 ⑦ 步、§11.3 都把这件事定为硬约束。
    """
    llm = MockLlm(
        json_responses=[
            {
                "verdict": "matched",
                "reason": "乙方被完全免责",
                "evidence_quote": "乙方无需承担任何赔偿责任",  # 正文里没有这句
            }
        ]
    )
    judge = make_llm_judge(llm, max_input_chars=10_000)

    result = judge(_spec(), TEXT)

    assert result.verdict is MatchVerdict.UNDECIDABLE
    assert result.reason_code is ReasonCode.EVIDENCE_UNCERTAIN
    assert result.detail["verdict_discarded"] == "evidence_not_found"


def test_a_hit_without_any_quote_is_discarded() -> None:
    """命中**必须**带出处 —— 没有出处的结论无法核验。

    这条看起来偏严（模型答对了，只是没引用原文），但本项目的全部前提是
    "每一处结论都能被追问『依据在哪』"。**无法核验的命中与幻觉在使用上没有区别**。
    """
    llm = MockLlm(
        json_responses=[{"verdict": "matched", "reason": "确实不该这么约定"}]
    )
    judge = make_llm_judge(llm, max_input_chars=10_000)

    result = judge(_spec(), TEXT)

    assert result.verdict is MatchVerdict.UNDECIDABLE
    assert result.detail["verdict_discarded"] == "matched_without_evidence"


def test_whitespace_and_case_differences_are_tolerated() -> None:
    """只容忍**排版**差异：模型复述原文时常把换行与空格吞掉。"""
    llm = MockLlm(
        json_responses=[
            {
                "verdict": "matched",
                "reason": "r",
                "evidence_quote": "乙方不承担任何\n赔偿责任",  # 中间多了一个换行
            }
        ]
    )
    judge = make_llm_judge(llm, max_input_chars=10_000)

    assert judge(_spec(), TEXT).verdict is MatchVerdict.MATCHED


def test_rewording_is_not_tolerated() -> None:
    """**不容忍内容差异**：允许"改写"等于让这道关退化成"看起来像"。"""
    llm = MockLlm(
        json_responses=[
            {"verdict": "matched", "reason": "r", "evidence_quote": "乙方一概不负赔偿责任"}
        ]
    )
    judge = make_llm_judge(llm, max_input_chars=10_000)

    assert judge(_spec(), TEXT).verdict is MatchVerdict.UNDECIDABLE


# ============================================================
# 3. 其余两种应答
# ============================================================


def test_not_matched_needs_no_evidence() -> None:
    """未命中**不需要**出处 —— 要"证明某句话不存在"本就是另一类问题。"""
    llm = MockLlm(
        json_responses=[{"verdict": "not_matched", "reason": "违约责任对等"}]
    )
    judge = make_llm_judge(llm, max_input_chars=10_000)

    result = judge(_spec(), TEXT)

    assert result.verdict is MatchVerdict.NOT_MATCHED
    assert result.reason_code is ReasonCode.CONDITION_NOT_MATCHED


def test_model_can_say_it_cannot_tell() -> None:
    """模型也必须能说"判不了" —— 否则它只能在两态里二选一，那会**逼出一个错的答案**。"""
    llm = MockLlm(
        json_responses=[{"verdict": "undecidable", "reason": "条款表述含糊"}]
    )
    judge = make_llm_judge(llm, max_input_chars=10_000)

    result = judge(_spec(), TEXT)

    assert result.verdict is MatchVerdict.UNDECIDABLE
    assert result.reason_code is ReasonCode.EVIDENCE_UNCERTAIN


# ============================================================
# 4. 重试与失败
# ============================================================


def test_one_transient_failure_is_retried() -> None:
    """一次偶发抖动不该让规则失去判断。"""
    llm = MockLlm(
        json_responses=[
            None,  # 第一次失败
            {"verdict": "not_matched", "reason": "对等"},
        ]
    )
    judge = make_llm_judge(llm, max_input_chars=10_000)

    assert judge(_spec(), TEXT).verdict is MatchVerdict.NOT_MATCHED
    assert len(llm.json_calls) == 2, "应当恰好重试一次"


def test_two_failures_degrade_to_needs_review_not_to_the_fallback() -> None:
    """⚠️ 两次都失败 → `needs_review(MODEL_UNAVAILABLE)`。

    **不**回落到 `fallback_match_json`：那会让"本该用模型判的"看起来判过了，
    而且 M12 的模型质量统计会把 fallback 的答案**算成模型答案** ——
    于是"模型到底答得怎么样"这个指标永远失真。
    """
    llm = MockLlm(default_json=None)  # 每次调用都失败
    judge = make_llm_judge(llm, max_input_chars=10_000, attempts=2)

    result = judge(_spec(), TEXT)

    assert result.verdict is MatchVerdict.UNDECIDABLE
    assert result.reason_code is ReasonCode.MODEL_UNAVAILABLE
    assert len(llm.json_calls) == 2
    assert result.detail["attempts"] == 2


# ============================================================
# 5. 超长正文：拒绝判断，绝不截断
# ============================================================


def test_over_long_text_is_refused_without_calling_the_model() -> None:
    """⚠️ 正文超限 → **一次都不调用模型**，直接判不了。

    截断的后果是静默的：被截掉的那半页里可能正写着违约条款，
    而模型对剩下的部分给出一个**看起来很确定**的 `not_matched` ——
    下游完全分不清"判断为否"与"只读了一部分"。
    """
    llm = MockLlm(json_responses=[{"verdict": "not_matched", "reason": "r"}])
    judge = make_llm_judge(llm, max_input_chars=10)

    result = judge(_spec(), TEXT)

    assert result.verdict is MatchVerdict.UNDECIDABLE
    assert result.reason_code is ReasonCode.EVIDENCE_UNCERTAIN
    assert llm.json_calls == [], "超限时不得调用模型"
    assert result.detail["text_length"] == len(TEXT)


# ============================================================
# 6. 提示词与 schema
# ============================================================


def test_the_response_is_constrained_by_a_schema() -> None:
    """应答必须被 schema 约束 —— 否则"模型返回了一段散文"要靠字符串解析去兜。"""
    llm = MockLlm(json_responses=[{"verdict": "not_matched", "reason": "r"}])
    judge = make_llm_judge(llm, max_input_chars=10_000)

    judge(_spec(), TEXT)

    _system, _user, schema = llm.json_calls[0]
    assert schema is LlmVerdict


def test_the_prompt_carries_the_rule_instruction_and_the_text() -> None:
    llm = MockLlm(json_responses=[{"verdict": "not_matched", "reason": "r"}])
    judge = make_llm_judge(llm, max_input_chars=10_000)

    judge(_spec(), TEXT)

    system, user, _schema = llm.json_calls[0]
    assert "违约责任" in user, "提示词里必须有规则说明"
    assert TEXT in user, "提示词里必须有正文 —— 否则模型只能靠常识编"
    assert "evidence_quote" in system, "系统提示必须交代应答结构"


def test_judge_is_never_created_without_a_prompt_version() -> None:
    """`prompt_version` 进批次幂等键：改了提示词必须改它，否则会复用旧结论。"""
    assert PROMPT_VERSION.strip()


# ============================================================
# 7. 端到端接线：有 judge 时**优先用模型**，不用 fallback
# ============================================================


def test_the_model_judgement_wins_over_the_rule_fallback() -> None:
    spec = _spec(
        fallback_match_json='{"keywords": ["乙方不承担"], "absent": false}',
    )
    llm = MockLlm(
        json_responses=[
            {"verdict": "not_matched", "reason": "模型认为违约责任对等"}
        ]
    )
    judge = make_llm_judge(llm, max_input_chars=10_000)

    result = evaluate_rule(
        spec,
        context=ReviewContext(),
        text=TEXT,
        fields={},
        default_currency="CNY",
        llm_judge=judge,
    )

    assert result.status is EvaluationStatus.NOT_HIT, (
        "模型给了结论就必须用它 —— fallback 里明明能命中却没生效，才对"
    )
    assert result.reason_text == "模型认为违约责任对等"


def test_without_a_judge_the_same_rule_uses_its_fallback() -> None:
    """对照组：同一规则、没有模型时走 fallback（计划 §2.2 的"无 GPU 开发"路径）。"""
    spec = _spec(
        fallback_match_json='{"keywords": ["乙方不承担"], "absent": false}',
    )

    result = evaluate_rule(
        spec,
        context=ReviewContext(),
        text=TEXT,
        fields={},
        default_currency="CNY",
        llm_judge=None,
    )

    assert result.status is EvaluationStatus.HIT
    assert result.located_text == "乙方不承担"


def test_an_undecidable_model_verdict_falls_back_to_needs_review_end_to_end() -> None:
    """模型幻觉 → 端到端是 `needs_review`，**不是** `hit`、也不是 fallback 的 `hit`。"""
    spec = _spec(
        fallback_match_json='{"keywords": ["乙方不承担"], "absent": false}',
    )
    llm = MockLlm(
        json_responses=[
            {"verdict": "matched", "reason": "r", "evidence_quote": "合同里没有这句话"}
        ]
    )
    judge = make_llm_judge(llm, max_input_chars=10_000)

    result = evaluate_rule(
        spec,
        context=ReviewContext(),
        text=TEXT,
        fields={},
        default_currency="CNY",
        llm_judge=judge,
    )

    assert result.status is EvaluationStatus.NEEDS_REVIEW
    assert result.reason_code is ReasonCode.EVIDENCE_UNCERTAIN
