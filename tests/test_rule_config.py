"""规则配置受控校验与适用性判断测试。

分三部分：

1. **配置解析**：未知键、非法枚举、正则编译失败、expr 字段名拼错、
   操作符与阈值不匹配——这些都必须**在加载阶段失败**。
2. **适用性判断**：验证"不适用"与"判不了"是两件事，
   以及方向敏感规则不会因甲乙方变化而反向误判。
3. **种子数据自检**：用真实的 `db/seed.sql` 建库，逐条校验 40 条规则，
   让种子数据的错误在 pytest 阶段暴露，而不是等 M5 运行时才发现。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.enums import ReasonCode
from app.rules.applicability import (
    Applicability,
    ReviewContext,
    evaluate_applicability,
)
from app.rules.scoping import (
    UNIVERSALLY_REQUIRED_MISSING_RULES,
    needs_scope,
)
from app.schemas import (
    AppliesWhenConfig,
    ExprMatchConfig,
    RuleConfig,
    RuleConfigError,
    parse_applies_when,
    parse_fallback_config,
    parse_match_config,
)

# ============================================================
# 1. 配置解析
# ============================================================


def test_applies_when_absent_or_null_means_unrestricted() -> None:
    """NULL / 空串 / {} 都表示"不限制"，不应报错。"""
    assert parse_applies_when(None, rule_code="R") is None
    assert parse_applies_when("   ", rule_code="R") is None
    assert parse_applies_when("{}", rule_code="R") is None


def test_applies_when_rejects_unknown_key() -> None:
    """键名拼错（contract_type 少了 s）必须报错，而不是被静默丢弃。

    否则规则会退化成"全局适用"，且几乎无法通过观察输出发现。
    """
    with pytest.raises(RuleConfigError, match="适用条件非法"):
        parse_applies_when('{"contract_type": ["procurement"]}', rule_code="R")


def test_applies_when_rejects_invalid_enum() -> None:
    """业务角色写了不存在的取值必须报错。"""
    with pytest.raises(RuleConfigError, match="适用条件非法"):
        parse_applies_when('{"our_business_roles": ["purchaser"]}', rule_code="R")


def test_applies_when_rejects_empty_list() -> None:
    """空数组有歧义（不限制？谁都不适用？），要求显式写 null。"""
    with pytest.raises(RuleConfigError, match="适用条件非法"):
        parse_applies_when('{"contract_types": []}', rule_code="R")


def test_applies_when_accepts_valid_config() -> None:
    config = parse_applies_when(
        '{"contract_types": ["development"], "our_business_roles": ["buyer"]}',
        rule_code="R",
    )
    assert config is not None
    assert config.is_unrestricted is False
    assert config.contract_types is not None


def test_match_config_keyword() -> None:
    config = parse_match_config(
        "keyword", '{"keywords": ["保密"], "absent": true}', rule_code="R"
    )
    assert getattr(config, "absent") is True


def test_match_config_rejects_bad_regex() -> None:
    """正则写错必须在加载阶段暴露，而不是等到匹配时才炸。"""
    with pytest.raises(RuleConfigError, match="match_text"):
        parse_match_config("regex", '{"pattern": "([unclosed"}', rule_code="R")


def test_match_config_rejects_unknown_match_mode() -> None:
    with pytest.raises(RuleConfigError, match="match_mode"):
        parse_match_config("fuzzy", '{"keywords": ["x"]}', rule_code="R")


def test_expr_rejects_unknown_field() -> None:
    """字段名拼错会静默退化成"字段为空"，进而被解释成"合同未约定"。

    一个拼写错误会变成一条错误的风险结论，因此必须在加载阶段拦截。
    """
    with pytest.raises(RuleConfigError, match="未知字段"):
        parse_match_config(
            "expr", '{"field": "prepay_ratios", "op": "gt", "value": 0.3}',
            rule_code="R",
        )


def test_expr_requires_value_for_comparison_ops() -> None:
    """`op: gt` 忘了写阈值必须报错。"""
    with pytest.raises(RuleConfigError, match="必须提供 value"):
        parse_match_config(
            "expr", '{"field": "prepay_ratio", "op": "gt"}', rule_code="R"
        )


def test_expr_rejects_value_for_null_ops() -> None:
    """`is_null` 是存在性判断，不应带阈值。"""
    with pytest.raises(RuleConfigError, match="不应提供 value"):
        parse_match_config(
            "expr", '{"field": "amount", "op": "is_null", "value": 1}',
            rule_code="R",
        )


def test_expr_accepts_valid_config() -> None:
    config = parse_match_config(
        "expr",
        '{"field": "prepay_ratio", "op": "gt", "value": 0.3}',
        rule_code="R",
    )
    assert isinstance(config, ExprMatchConfig)
    assert config.value == 0.3


def test_fallback_auto_detects_type() -> None:
    """fallback 按出现的键自动判定 keyword / regex。"""
    keyword = parse_fallback_config(
        '{"keywords": ["甲方不承担"], "absent": false}', rule_code="R"
    )
    assert keyword is not None and hasattr(keyword, "keywords")

    regex = parse_fallback_config('{"pattern": "不承担.{0,4}责任"}', rule_code="R")
    assert regex is not None and hasattr(regex, "pattern")


def test_fallback_requires_keywords_or_pattern() -> None:
    with pytest.raises(RuleConfigError, match="必须包含 keywords 或 pattern"):
        parse_fallback_config('{"instruction": "再问一次模型"}', rule_code="R")


def test_fallback_rejected_for_non_llm_rule() -> None:
    """确定性规则再配降级条件是配置冗余，会让人误判执行路径，必须报错。"""
    row = {
        "rule_code": "R-KEYWORD",
        "rule_name": "关键词规则",
        "rule_category": "测试",
        "risk_level": "low",
        "priority": 1,
        "rule_version": 1,
        "match_mode": "keyword",
        "applies_when_json": None,
        "match_text": '{"keywords": ["保密"]}',
        "fallback_match_json": '{"keywords": ["保密义务"]}',
    }
    with pytest.raises(RuleConfigError, match="不需要 fallback_match_json"):
        RuleConfig.from_row(row)


# ============================================================
# 2. 适用性判断
# ============================================================


def test_unrestricted_rule_is_always_applicable() -> None:
    result = evaluate_applicability(None, ReviewContext())
    assert result.applicability is Applicability.APPLICABLE


def test_ip_rule_not_applicable_for_standard_procurement() -> None:
    """核心误报场景：标准商品采购合同没有知识产权条款是正常的。

    必须是 not_applicable，而不是"知识产权条款缺失"。
    """
    applies = AppliesWhenConfig(contract_types=["software_service", "development"])  # type: ignore[arg-type]
    result = evaluate_applicability(
        applies, ReviewContext(contract_type="procurement")
    )
    assert result.applicability is Applicability.NOT_APPLICABLE
    assert result.reason_code is ReasonCode.APPLICABILITY_NOT_MET


def test_ip_rule_applicable_for_development_contract() -> None:
    """同一规则在软件开发合同下必须适用。"""
    applies = AppliesWhenConfig(contract_types=["software_service", "development"])  # type: ignore[arg-type]
    result = evaluate_applicability(applies, ReviewContext(contract_type="development"))
    assert result.applicability is Applicability.APPLICABLE


def test_missing_business_role_yields_unknown_not_not_applicable() -> None:
    """上下文缺失 → needs_review（判不了），绝不能伪装成"不适用"。

    否则"缺少立场信息"就会静默吞掉风险。
    """
    applies = AppliesWhenConfig(our_business_roles=["buyer"])  # type: ignore[arg-type]
    result = evaluate_applicability(applies, ReviewContext(our_business_role=None))
    assert result.applicability is Applicability.UNKNOWN
    assert result.reason_code is ReasonCode.CONTEXT_MISSING
    assert result.needs_review is True


def test_unknown_enum_value_treated_as_missing() -> None:
    applies = AppliesWhenConfig(our_business_roles=["buyer"])  # type: ignore[arg-type]
    result = evaluate_applicability(
        applies, ReviewContext(our_business_role="unknown")
    )
    assert result.applicability is Applicability.UNKNOWN


def test_other_role_is_not_applicable_not_unknown() -> None:
    """"other" 是**已确定**的取值，只是不在范围内 → not_applicable。"""
    applies = AppliesWhenConfig(our_business_roles=["buyer"])  # type: ignore[arg-type]
    result = evaluate_applicability(applies, ReviewContext(our_business_role="other"))
    assert result.applicability is Applicability.NOT_APPLICABLE


def test_party_conflict_only_affects_party_dependent_rules() -> None:
    """立场冲突只应让**依赖立场**的规则进入 needs_review。

    这是本函数最关键的一条约定：一次主体识别争议不该让整份审查结论全部作废。
    一条只看合同类型的规则（例如"标准品采购合同是否适用知识产权规则"）
    与"我方是甲方还是乙方"无关，必须照常出结论。
    """
    type_only_rule = AppliesWhenConfig(contract_types=["development"])  # type: ignore[arg-type]
    party_rule = AppliesWhenConfig(our_contract_labels=["party_a"])  # type: ignore[arg-type]

    context = ReviewContext(
        contract_type="development",
        our_contract_label="party_a",
        party_context_status="conflict",
    )

    # 只依赖合同类型 → 不受立场冲突影响
    assert (
        evaluate_applicability(type_only_rule, context).applicability
        is Applicability.APPLICABLE
    )
    # 依赖合同标签 → 判不了
    party_result = evaluate_applicability(party_rule, context)
    assert party_result.applicability is Applicability.UNKNOWN
    assert party_result.reason_code is ReasonCode.CONTEXT_CONFLICT


def test_contract_type_conflict_only_affects_type_dependent_rules() -> None:
    """合同类型冲突只影响依赖合同类型的规则，反向亦然。"""
    type_rule = AppliesWhenConfig(contract_types=["development"])  # type: ignore[arg-type]
    role_rule = AppliesWhenConfig(our_business_roles=["buyer"])  # type: ignore[arg-type]

    context = ReviewContext(
        contract_type="development",
        our_business_role="buyer",
        contract_type_status="conflict",
    )
    assert (
        evaluate_applicability(type_rule, context).reason_code
        is ReasonCode.CONTEXT_CONFLICT
    )
    assert (
        evaluate_applicability(role_rule, context).applicability
        is Applicability.APPLICABLE
    )


def test_unrestricted_rules_unaffected_by_any_conflict() -> None:
    """没有任何适用条件的规则（主体缺失、金额缺失等）必须照常出结论。"""
    context = ReviewContext(
        contract_type="procurement",
        party_context_status="conflict",
        contract_type_status="conflict",
    )
    assert evaluate_applicability(None, context).applicability is (Applicability.APPLICABLE)


def test_direction_sensitive_rules_do_not_invert() -> None:
    """方向敏感规则按角色拆分：买方规则不会在卖方场景下反向误判。

    同一个"预付款比例"字段：
      - 我方为采购方（付款方）→ 高比例是风险 → 适用；
      - 我方为销售方（收款方）→ 高比例是利好 → 该规则不适用。
    """
    buyer_rule = AppliesWhenConfig(our_business_roles=["buyer"])  # type: ignore[arg-type]

    as_buyer = evaluate_applicability(buyer_rule, ReviewContext(our_business_role="buyer"))
    as_seller = evaluate_applicability(
        buyer_rule, ReviewContext(our_business_role="seller")
    )

    assert as_buyer.applicability is Applicability.APPLICABLE
    assert as_seller.applicability is Applicability.NOT_APPLICABLE


def test_requires_any_keyword_gate() -> None:
    """没有内容触发词的合同直接判不适用，压制"缺失类"规则误报。"""
    applies = AppliesWhenConfig(requires_any_keyword=["个人信息", "数据处理"])
    hit = evaluate_applicability(applies, ReviewContext(contract_text="涉及用户数据处理"))
    miss = evaluate_applicability(applies, ReviewContext(contract_text="标准商品买卖"))
    unknown = evaluate_applicability(applies, ReviewContext(contract_text=None))

    assert hit.applicability is Applicability.APPLICABLE
    assert miss.applicability is Applicability.NOT_APPLICABLE
    assert unknown.applicability is Applicability.UNKNOWN


# ============================================================
# 3. 种子数据自检（用真实的 db/seed.sql）
# ============================================================

REQUIRED_CATEGORIES = {
    "预付款比例",
    "付款周期",
    "自动续约",
    "违约责任",
    "管辖地",
    "主体信息缺失",
    "金额缺失",
    "保密缺失",
    "数据处理",
    "知识产权",
    "验收标准缺失",
}


def _load_all(conn: sqlite3.Connection) -> list[RuleConfig]:
    rows = conn.execute(
        "SELECT * FROM review_rules ORDER BY priority, id"
    ).fetchall()
    return [RuleConfig.from_row(dict(row)) for row in rows]


def test_seed_rules_all_parse(seeded_conn: sqlite3.Connection) -> None:
    """40 条种子规则必须全部通过受控校验。

    这条测试的价值：seed.sql 里任何一个键名拼错、字段名拼错、
    枚举写错，都会在这里被捕获，而不是等到 M5 运行时才发现。
    """
    configs = _load_all(seeded_conn)
    assert len(configs) == 40


def test_seed_rules_cover_eleven_categories(seeded_conn: sqlite3.Connection) -> None:
    """"11 类覆盖"说的**规则库的覆盖范围**，不是每份合同都触发 11 类。"""
    categories = {
        row["rule_category"]
        for row in seeded_conn.execute("SELECT DISTINCT rule_category FROM review_rules")
    }
    assert REQUIRED_CATEGORIES <= categories


def test_seed_rule_codes_unique(seeded_conn: sqlite3.Connection) -> None:
    codes = [row["rule_code"] for row in seeded_conn.execute("SELECT rule_code FROM review_rules")]
    assert len(codes) == len(set(codes))


def test_seed_direction_sensitive_rules_declare_conditions(
    seeded_conn: sqlite3.Connection,
) -> None:
    """方向敏感规则必须声明依赖的立场条件，否则会静默给出反向结论。"""
    configs = _load_all(seeded_conn)
    sensitive = [c for c in configs if c.is_direction_sensitive]

    # 预付款/付款周期/违约/管辖地/保密单方/知识产权 均按角色或标签拆分
    assert len(sensitive) >= 12, f"方向敏感规则数量异常：{len(sensitive)}"

    # 每条方向敏感规则都必须限定在明确的标签或角色上
    for config in sensitive:
        applies = config.applies_when
        assert applies is not None
        assert applies.our_contract_labels or applies.our_business_roles, (
            f"{config.rule_code} 标记为方向敏感但没有声明立场条件"
        )


def test_pay_prepay_missing_does_not_fire_for_a_buyer_side_contract(
    seeded_conn: sqlite3.Connection,
) -> None:
    """⚠️ **验收 19 的前提**：基线合同没有预付款约定，而"买方不需要预付"**不是风险**。

    `HT-2026-0001` 是 `contract_type=procurement` + `our_party_business_role=buyer`，
    正文是「双方约定验收合格后三十日内支付合同总金额」—— **全额验收后付款**。
    对采购方而言这是**有利**条件（垫资风险在收款方），因此这条 medium 规则**不得**命中，
    否则"低风险基线"会被推到 medium，而验收 19 断言的是 `overall_risk_level == 'low'`。

    ⚠️ **上一条通用守卫（`is_direction_sensitive`）抓不到它**，这一点值得记下来：
    那个属性是**从 `applies_when` 反推出来的** —— 没声明立场条件的规则自然报 `False`，
    于是"方向敏感守卫"对它**静默不适用**。
    **一条从被检对象自身推导出来的判据，永远发现不了该对象的缺失。**

    所以这里断言的是**语义**（谁不适用），而不是"它声明了什么"。
    """
    configs = {config.rule_code: config for config in _load_all(seeded_conn)}
    rule = configs["PAY_PREPAY_MISSING"]

    buyer = ReviewContext(contract_type="procurement", our_business_role="buyer")
    assert (
        evaluate_applicability(rule.applies_when, buyer).applicability
        is Applicability.NOT_APPLICABLE
    ), "我方是采购方时，『没有预付款约定』不是风险"

    seller = ReviewContext(contract_type="procurement", our_business_role="seller")
    assert (
        evaluate_applicability(rule.applies_when, seller).applicability
        is Applicability.APPLICABLE
    ), "我方是收款方时，没有预付款约定才是风险（垫资）"


def test_seed_llm_rules_have_explicit_fallback(
    seeded_conn: sqlite3.Connection,
) -> None:
    """llm 规则必须给出确定性降级条件。

    没有 fallback 时，无模型环境下这条规则只能返回 needs_review；
    宁可它显式声明降级路径，也不要让"没报风险"变成假象。
    """
    configs = _load_all(seeded_conn)
    llm_rules = [c for c in configs if c.match_mode == "llm"]

    assert llm_rules, "种子规则中应至少有一条 llm 规则"
    for config in llm_rules:
        assert config.fallback_condition is not None, (
            f"{config.rule_code} 是 llm 规则但缺少 fallback_match_json"
        )


def test_seed_missing_clause_rules_are_scoped(
    seeded_conn: sqlite3.Connection,
) -> None:
    """"缺失类"规则（absent=true）必须限定适用范围。

    否则会在不相关的合同上大量误报，例如：
    标准品采购合同报"知识产权缺失"、纯货物买卖报"数据处理缺失"。

    唯一例外见 `app/rules/scoping.py`：任何合同都应当具备的条款
    （违约责任、争议解决）允许全局适用。
    """
    rows = seeded_conn.execute(
        "SELECT rule_code, applies_when_json, match_text FROM review_rules "
        "WHERE match_mode = 'keyword'"
    ).fetchall()

    for row in rows:
        match = json.loads(row["match_text"])
        if not match.get("absent"):
            continue
        if not needs_scope(row["rule_code"]):
            continue  # 政策白名单内的通用条款，允许全局适用
        assert row["applies_when_json"], (
            f"{row['rule_code']} 是缺失类规则但未限定适用范围，会产生误报"
        )


def test_scoping_whitelist_entries_exist_in_seed(
    seeded_conn: sqlite3.Connection,
) -> None:
    """政策白名单里的 rule_code 必须真实存在于种子规则中。

    防止改名后白名单悄悄失效——那会让"未限定范围"的检查形同虚设。
    """
    codes = {
        row["rule_code"]
        for row in seeded_conn.execute("SELECT rule_code FROM review_rules")
    }
    assert UNIVERSALLY_REQUIRED_MISSING_RULES <= codes, (
        "政策白名单中存在种子规则里不存在的 rule_code："
        f"{sorted(UNIVERSALLY_REQUIRED_MISSING_RULES - codes)}"
    )
