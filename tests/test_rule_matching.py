"""确定性条件匹配的测试（M5 / T3）。

## 本文件守住的三类"写错了也不报错"

1. **把"判不了"压成 `not_hit`** —— 阈值类规则遇到 `not_found` 会输出"这条规则没问题"，
   而事实是**根本没能判**（决策 ④）；
2. **浮点阈值** —— 规则库里就有 `{"op": "lt", "value": 0.1}`，
   二进制误差会让"正好 10%"被报成"低于 10%"；
3. **币种不可比却照样比大小** —— `USD 200,000 > 1,000,000` 会得出一个
   **看起来很确定**的错误结论。

三类都**不会**让程序报错，只会让结论静默地错。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.enums import BboxPrecision, FieldStatus, ReasonCode, TextPrecision
from app.ports.field_contract import EvidenceSpan, ExtractedField
from app.rules.matching import (
    MatchVerdict,
    match_expr,
    match_keyword,
    match_regex,
    parse_exclude_text,
)
from app.schemas import ExprMatchConfig, KeywordMatchConfig, RegexMatchConfig

DEFAULT_CURRENCY = "CNY"


# ============================================================
# 构造夹具
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
    """按字段契约的**不变量**构造（`extracted` 必须有证据；`uncertain`/`failed` 必须给原因码）。"""
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


def _expr(field: str, op: str, value: object = None) -> ExprMatchConfig:
    return ExprMatchConfig(field=field, op=op, value=value)  # type: ignore[arg-type]


# ============================================================
# 1. keyword
# ============================================================


def test_keyword_hit_reports_the_matched_phrase() -> None:
    result = match_keyword(
        KeywordMatchConfig(keywords=["自动续约"]), text="本合同期满后自动续约一年。"
    )

    assert result.verdict is MatchVerdict.MATCHED
    assert result.located_text == "自动续约"


def test_keyword_miss_is_not_matched() -> None:
    result = match_keyword(
        KeywordMatchConfig(keywords=["自动续约"]), text="本合同期满即终止。"
    )

    assert result.verdict is MatchVerdict.NOT_MATCHED
    assert result.reason_code is ReasonCode.CONDITION_NOT_MATCHED


def test_absent_rule_hits_when_nothing_is_found() -> None:
    """缺失类：全文无相关表述 → **命中**。"""
    result = match_keyword(
        KeywordMatchConfig(keywords=["违约责任", "赔偿"], absent=True),
        text="本合同仅约定交付与付款。",
    )

    assert result.verdict is MatchVerdict.MATCHED
    assert result.detail["absent"] is True


def test_absent_rule_does_not_hit_when_the_clause_exists() -> None:
    result = match_keyword(
        KeywordMatchConfig(keywords=["违约责任"], absent=True),
        text="第八条 违约责任：违约方应赔偿损失。",
    )

    assert result.verdict is MatchVerdict.NOT_MATCHED


def test_exclude_phrase_suppresses_a_hit() -> None:
    """命中关键词、但正文明确否定了该情形 → **不命中**。

    实测库里的两条规则正是为此存在：`keywords=["自动续约"]` +
    `exclude_text="不自动,不得自动,不再自动"` —— 若没有否定词表，
    "本合同**不自动**续约"会被报成"自动续约风险"，是纯粹的误报。
    """
    result = match_keyword(
        KeywordMatchConfig(keywords=["自动续约"]),
        text="本合同期满后不自动续约，需另行协商。",
        exclude_phrases=parse_exclude_text("不自动,不得自动,不再自动"),
    )

    assert result.verdict is MatchVerdict.NOT_MATCHED
    assert result.detail["excluded_by"] == ["不自动"]


def test_exclude_phrase_does_not_apply_to_absent_rules() -> None:
    """⚠️ 否定词表**仅对存在类**生效。

    对缺失类，"出现且未被否定"才是"不缺失" —— 那是**另一个规则**，
    不该用同一个字段表达。这里把行为写死，避免后来者"顺手"让它也生效，
    从而静默改变缺失类规则的语义。
    """
    result = match_keyword(
        KeywordMatchConfig(keywords=["违约责任"], absent=True),
        text="第八条 违约责任：违约方应赔偿损失。",
        exclude_phrases=("违约责任",),
    )

    assert result.verdict is MatchVerdict.NOT_MATCHED, "出现即表示不缺失，与否定词无关"


def test_keyword_without_text_is_undecidable() -> None:
    """正文不可用 → **判不了**，不是"未命中"。"""
    result = match_keyword(KeywordMatchConfig(keywords=["保密"]), text=None)

    assert result.verdict is MatchVerdict.UNDECIDABLE
    assert result.reason_code is ReasonCode.EVIDENCE_UNCERTAIN


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, ()), ("", ()), ("   ", ()), ("不自动, 不得自动 ,不再自动", ("不自动", "不得自动", "不再自动"))],
)
def test_parse_exclude_text(raw: str | None, expected: tuple[str, ...]) -> None:
    assert parse_exclude_text(raw) == expected


# ============================================================
# 2. regex
# ============================================================


def test_regex_matches_a_fragment_not_the_whole_string() -> None:
    """配置写的是**片段**，因此用 `re.search` 而不是 `fullmatch`。"""
    result = match_regex(
        RegexMatchConfig(pattern=r"人民币\s*[\d,]+\.\d{2}\s*元"),
        text="合同总金额：人民币 800,000.00 元（含税）。",
    )

    assert result.verdict is MatchVerdict.MATCHED
    assert result.located_text == "人民币 800,000.00 元"


def test_regex_miss_and_missing_text() -> None:
    assert (
        match_regex(RegexMatchConfig(pattern=r"\d{4}年"), text="无年份").verdict
        is MatchVerdict.NOT_MATCHED
    )
    assert (
        match_regex(RegexMatchConfig(pattern=r"\d"), text=None).verdict
        is MatchVerdict.UNDECIDABLE
    )


# ============================================================
# 3. expr —— 存在性判断：**状态就是答案**
# ============================================================


@pytest.mark.parametrize(
    ("status", "op", "expected", "expected_code"),
    [
        # extracted = 有；not_found = 没有（且**只有这一种**"没有"）
        (FieldStatus.EXTRACTED, "is_null", MatchVerdict.NOT_MATCHED, ReasonCode.CONDITION_NOT_MATCHED),
        (FieldStatus.NOT_FOUND, "is_null", MatchVerdict.MATCHED, ReasonCode.CONDITION_MATCHED),
        (FieldStatus.EXTRACTED, "not_null", MatchVerdict.MATCHED, ReasonCode.CONDITION_MATCHED),
        (FieldStatus.NOT_FOUND, "not_null", MatchVerdict.NOT_MATCHED, ReasonCode.CONDITION_NOT_MATCHED),
        # ⚠️ 判不了的两档：把它们当成"没有"就是缺失类规则误报的根源
        (FieldStatus.UNCERTAIN, "is_null", MatchVerdict.UNDECIDABLE, ReasonCode.EVIDENCE_UNCERTAIN),
        (FieldStatus.FAILED, "is_null", MatchVerdict.UNDECIDABLE, ReasonCode.EXTRACTION_FAILED),
    ],
)
def test_existence_ops_are_decided_by_status(
    status: FieldStatus, op: str, expected: MatchVerdict, expected_code: ReasonCode
) -> None:
    result = match_expr(
        _expr("party_a", op),
        field=_field("party_a", status, value_text="某某集团有限公司"),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.verdict is expected
    assert result.reason_code is expected_code


# ============================================================
# 4. expr —— 数值比较
# ============================================================


def test_float_threshold_is_folded_from_its_shortest_repr() -> None:
    """⚠️ `lt 0.1` 遇到**恰好 0.1** 的比例必须**不命中**。

    规则库里真有这一条（`PAY_PREPAY_RATIO_LOW_FOR_SELLER`），
    而 `match_text` 是 JSON —— 阈值落到 Python 是 `float`，
    也就是 `0.1000000000000000055511151231257827`。

    `Decimal(0.1)` 直接比：`Decimal("0.1") < 0.10000000000000000551…` → **True**，
    于是"预付款比例正好 10%"被报成"低于 10%"。**差一个 epsilon，静默误报。**

    修法是阈值经 `str()` 还原成写规则的人本来想写的那个数。
    """
    result = match_expr(
        _expr("prepay_ratio", "lt", 0.1),
        field=_field("prepay_ratio", FieldStatus.EXTRACTED, value_decimal="0.10"),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.verdict is MatchVerdict.NOT_MATCHED, (
        "0.10 不小于 0.1 —— 若判成命中，说明阈值被当成了二进制浮点"
    )


def test_gte_threshold_holds_at_the_boundary() -> None:
    """同族的另一半：`gte 0.3` 遇到恰好 0.3 必须**命中**（朴素实现会判 False）。"""
    result = match_expr(
        _expr("prepay_ratio", "gte", 0.3),
        field=_field("prepay_ratio", FieldStatus.EXTRACTED, value_decimal="0.30"),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.verdict is MatchVerdict.MATCHED


def test_numeric_detail_records_the_comparison_as_strings() -> None:
    """`hit_detail_json` 是给人核对"到底比了什么"的，因此**数值必须是字符串**。

    用 float 会把这份记账数据也拖回二进制误差 —— 那就失去了核对的意义。

    ⚠️ 阈值写成 `60`，记账里是 `"60.0"`：`ExprMatchConfig.value` 的联合类型是
    `float | str`，JSON 里的整数会被 pydantic 归一成 `float`。
    数值上等价，这里把它**写死**是为了说明"记账显示的是**参与比较的那个数**"，
    而不是配置文本的逐字拷贝（这一点在核对时容易误以为规则写错了）。
    """
    result = match_expr(
        _expr("pay_days", "gt", 60),
        field=_field("pay_days", FieldStatus.EXTRACTED, value_decimal="90"),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.detail == {"actual": "90", "op": "gt", "threshold": "60.0"}
    assert all(
        isinstance(value, str) for key, value in result.detail.items() if key != "op"
    )


@pytest.mark.parametrize("op", ["gt", "gte", "lt", "lte", "eq"])
def test_not_found_is_undecidable_for_every_numeric_op(op: str) -> None:
    """**决策 ④**：阈值类规则遇到 `not_found` **一律**判不了 —— 逐 op 遍历，不抽查。

    抽查会漏掉"某个 op 忘了处理"这种缺陷，而它的表现是
    **某一条规则静静地把"没能判"说成"没问题"**。

    注意方向相反的一面（同一条字段状态，两类规则含义相反）：
    对**缺失类**规则，`not_found` 恰恰**就是命中** —— 那条路径是
    `is_null`（存在性判断），与本条不共用逻辑。
    """
    result = match_expr(
        _expr("pay_days", op, 60),
        field=_field("pay_days", FieldStatus.NOT_FOUND),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.verdict is MatchVerdict.UNDECIDABLE
    assert result.reason_code is ReasonCode.EVIDENCE_UNCERTAIN


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (FieldStatus.UNCERTAIN, ReasonCode.EVIDENCE_UNCERTAIN),
        (FieldStatus.FAILED, ReasonCode.EXTRACTION_FAILED),
    ],
)
def test_weak_evidence_is_undecidable_for_numeric_ops(
    status: FieldStatus, expected_code: ReasonCode
) -> None:
    result = match_expr(
        _expr("amount", "gt", 1_000_000),
        field=_field("amount", status, currency="CNY"),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.verdict is MatchVerdict.UNDECIDABLE
    assert result.reason_code is expected_code


def test_money_comparison_requires_a_known_currency() -> None:
    """金额没有币种 → **不可比**（不是"按人民币算"）。

    缺币种时"默认按本币"看起来很方便，实则是把一个**未知**当成**已知** ——
    而结论会以"命中/未命中"的形式显得很确定。
    """
    result = match_expr(
        _expr("amount", "gt", 1_000_000),
        field=_field("amount", FieldStatus.EXTRACTED, value_decimal="2000000", currency=None),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.verdict is MatchVerdict.UNDECIDABLE
    assert result.reason_code is ReasonCode.CURRENCY_NOT_COMPARABLE


def test_money_comparison_rejects_a_foreign_currency() -> None:
    """⚠️ 币种不同时必须**判不了** —— `USD 200,000 > 1,000,000` 是无意义的比较。

    朴素实现会得出 `False` → `not_hit` → "这条规则没问题"，
    而它其实**根本不可比**。M4 §4.8 把币种拆成独立字段，为的就是让这件事可判断。
    """
    result = match_expr(
        _expr("amount", "gt", 1_000_000),
        field=_field("amount", FieldStatus.EXTRACTED, value_decimal="200000", currency="USD"),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.verdict is MatchVerdict.UNDECIDABLE
    assert result.reason_code is ReasonCode.CURRENCY_NOT_COMPARABLE
    assert result.detail["actual_currency"] == "USD"


def test_money_comparison_proceeds_in_the_running_currency() -> None:
    result = match_expr(
        _expr("amount", "gt", 1_000_000),
        field=_field("amount", FieldStatus.EXTRACTED, value_decimal="2000000.00", currency="CNY"),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.verdict is MatchVerdict.MATCHED
    assert result.detail["actual"] == "2000000.00"


def test_ratio_fields_are_not_currency_checked() -> None:
    """比例类字段是**无量纲**的，不该被币种检查拦住。"""
    result = match_expr(
        _expr("prepay_ratio", "gt", 0.3),
        field=_field("prepay_ratio", FieldStatus.EXTRACTED, value_decimal="0.60"),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.verdict is MatchVerdict.MATCHED


def test_non_numeric_threshold_is_undecidable() -> None:
    """阈值不是数字 → `THRESHOLD_NOT_CONFIGURED`，**不是**"按 0 比较"。

    按 0 比较会静默产出一个 `not_hit`，把配置错误伪装成"这条没问题"。
    """
    result = match_expr(
        _expr("pay_days", "gt", "六十天"),
        field=_field("pay_days", FieldStatus.EXTRACTED, value_decimal="90"),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.verdict is MatchVerdict.UNDECIDABLE
    assert result.reason_code is ReasonCode.THRESHOLD_NOT_CONFIGURED


def test_extracted_without_a_numeric_value_is_undecidable() -> None:
    result = match_expr(
        _expr("pay_days", "gt", 60),
        field=_field("pay_days", FieldStatus.EXTRACTED, value_text="九十天"),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.verdict is MatchVerdict.UNDECIDABLE
    assert result.reason_code is ReasonCode.EVIDENCE_UNCERTAIN


def test_contains_compares_text_values() -> None:
    result = match_expr(
        _expr("party_a", "contains", "集团"),
        field=_field("party_a", FieldStatus.EXTRACTED, value_text="某某集团有限公司"),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.verdict is MatchVerdict.MATCHED
    assert result.located_text == "集团"


def test_missing_field_in_the_contract_is_undecidable() -> None:
    """字段根本不在解析结果里 → `EXTRACTION_FAILED`（不是"缺失"）。"""
    result = match_expr(
        _expr("amount", "is_null"), field=None, default_currency=DEFAULT_CURRENCY
    )

    assert result.verdict is MatchVerdict.UNDECIDABLE
    assert result.reason_code is ReasonCode.EXTRACTION_FAILED


def test_decimals_are_compared_exactly_not_via_float() -> None:
    """大额金额必须精确比较（`Decimal` 而不是 `float`）。"""
    result = match_expr(
        _expr("amount", "eq", "12345678901234.56"),
        field=_field(
            "amount",
            FieldStatus.EXTRACTED,
            value_decimal="12345678901234.56",
            currency="CNY",
        ),
        default_currency=DEFAULT_CURRENCY,
    )

    assert result.verdict is MatchVerdict.MATCHED
    assert result.detail["actual"] == Decimal("12345678901234.56").__str__()
