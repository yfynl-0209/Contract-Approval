"""命中证据定位的测试（M5 / T6）。

## 本文件守住的四类"写错了也不报错"

1. **定位不到的命中照样落库** —— 一行 `status=hit`、`evidence_text` 为 NULL：
   M8 画不出框、审批人无法核验，而这条记录**看起来完全正常**；
2. **缺失类命中被"每一条命中都要有证据"误伤** —— 它的结论是"全文都没有"，
   强行要一段原文只会逼出编造的输入，或让这条规则永远判不了；
3. **`expr` 命中丢掉了字段证据** —— 数值比较没有独立原文片段，
   它的依据是参与比较的**那个字段**；
4. **只写一个 `precision`** —— 精度有 text 与 bbox **两个**，
   M8 画框用的是 bbox 那个；写错会让框大到离谱而**两边都不报错**。

四种都不会抛异常，只会让证据静默地不可核验。

## 为什么不用真实 PDF 夹具

本文件直接构造 `StandardDocument`：证据定位要验的是**坐标系与切片**，
而不是 PDF 解析。手工构造能精确控制"片段在第几个字符"，
从而把断言写在**偏移量**上 —— 用夹具只能断言"能找到"，验不出偏移对不对。
"""

from __future__ import annotations

import json

import pytest

from app.enums import (
    BboxPrecision,
    EvaluationStatus,
    FieldStatus,
    PageStatus,
    ReasonCode,
    RiskLevel,
    TextPrecision,
)
from app.ports.field_contract import EvidenceSpan, ExtractedField
from app.ports.parse_document import (
    BBOX_SPACE,
    DocumentBlock,
    DocumentChar,
    DocumentPage,
    StandardDocument,
)
from app.rules.evidence import MAX_EVIDENCE_ITEMS, attach_evidence
from app.rules.evaluator import RuleEvaluation, RuleSpec, build_rule_spec

PHRASE = "乙方不承担"
LINE = "第八条 乙方不承担任何赔偿责任。"
OTHER_LINE = "第九条 保密义务持续三年。"


# ============================================================
# 构造标准文档
# ============================================================


def _block(
    block_id: str, text: str, start: int, *, char_level: bool = False
) -> DocumentBlock:
    """块是页文本的**精确切片**（`DocumentPage` 的校验器会逐块断言这件事）。"""
    end = start + len(text)
    if not char_level:
        return DocumentBlock(
            block_id=block_id,
            text=text,
            bbox=(50.0, 100.0, 500.0, 112.0),
            char_start=start,
            char_end=end,
            text_precision=TextPrecision.CHAR,
            bbox_precision=BboxPrecision.LINE,
        )

    # 逐字符 bbox：每个字 10pt 宽，这样"选中了哪几个字"可以从 bbox 反推出来
    chars = tuple(
        DocumentChar(
            text=char,
            bbox=(50.0 + index * 10, 100.0, 60.0 + index * 10, 112.0),
            char_start=start + index,
            char_end=start + index + 1,
        )
        for index, char in enumerate(text)
    )
    return DocumentBlock(
        block_id=block_id,
        text=text,
        bbox=(50.0, 100.0, 50.0 + len(text) * 10, 112.0),
        char_start=start,
        char_end=end,
        text_precision=TextPrecision.CHAR,
        bbox_precision=BboxPrecision.CHAR,
        chars=chars,
    )


def _page(page_no: int, lines: list[str], *, char_level: bool = False) -> DocumentPage:
    blocks: list[DocumentBlock] = []
    cursor = 0
    for index, line in enumerate(lines):
        if index:
            cursor += 1  # 块分隔符 "\n" 不属于任何块
        blocks.append(
            _block(f"p{page_no}-b{index + 1}", line, cursor, char_level=char_level)
        )
        cursor += len(line)

    return DocumentPage(
        page=page_no,
        width=595.0,
        height=842.0,
        bbox_space=BBOX_SPACE,
        rotation=0,
        source="text",
        page_status=PageStatus.OK,
        text="\n".join(lines),
        blocks=tuple(blocks),
    )


def _doc(*pages: DocumentPage) -> StandardDocument:
    return StandardDocument(pages=pages)


def _repeated_page(times: int) -> DocumentPage:
    """一页里出现 `times` 次 `PHRASE`（用来测上限与截断标记）。"""
    return _page(1, [PHRASE * times])


# ============================================================
# 夹具
# ============================================================


def _spec(match_mode: str = "keyword", match_text: str | None = None, **kwargs: object) -> RuleSpec:
    defaults: dict[str, object] = {
        "rule_code": "R_TEST",
        "rule_name": "测试规则",
        "risk_level": "high",
        "rule_version": 1,
        "match_mode": match_mode,
        "match_text": match_text or '{"keywords": ["乙方不承担"]}',
    }
    defaults.update(kwargs)
    return build_rule_spec(**defaults)  # type: ignore[arg-type]


def _evaluation(
    status: EvaluationStatus = EvaluationStatus.HIT,
    *,
    located_text: str | None = None,
    reason_code: ReasonCode = ReasonCode.CONDITION_MATCHED,
) -> RuleEvaluation:
    return RuleEvaluation(
        rule_code="R_TEST",
        rule_version=1,
        status=status,
        risk_level=RiskLevel.HIGH,
        reason_code=reason_code,
        reason_text="测试",
        located_text=located_text,
    )


def _field(field_code: str, text: str) -> ExtractedField:
    return ExtractedField(
        field_code=field_code,
        value_text=text,
        value_decimal="90",
        status=FieldStatus.EXTRACTED,
        evidence=(
            EvidenceSpan(
                page=1,
                block_id="p1-b1",
                text=text,
                bbox=(10.0, 20.0, 200.0, 32.0),
                char_start=2,
                char_end=2 + len(text),
                text_precision=TextPrecision.CHAR,
                bbox_precision=BboxPrecision.LINE,
            ),
        ),
    )


# ============================================================
# 1. 正常路径
# ============================================================


def test_keyword_hit_gets_locatable_evidence() -> None:
    document = _doc(_page(1, [LINE, OTHER_LINE]))

    result = attach_evidence(
        _evaluation(located_text=PHRASE), spec=_spec(), document=document, fields={}
    )

    assert result.status is EvaluationStatus.HIT, "证据找得到时不得降级"
    assert result.evidence_text == PHRASE

    position = json.loads(result.evidence_position or "{}")
    # "第八条 " 占 4 个字符 → 片段在页内的偏移是 [4, 9)
    assert position["page"] == 1
    assert (position["char_start"], position["char_end"]) == (4, 9)
    assert position["block_id"] == "p1-b2" or position["block_id"] == "p1-b1"


def test_offset_is_exact_not_just_found() -> None:
    """断言的是**偏移量**，不是"找到了" —— 这正是手工构造文档的理由。"""
    document = _doc(_page(1, [LINE, OTHER_LINE]))

    result = attach_evidence(
        _evaluation(located_text=PHRASE), spec=_spec(), document=document, fields={}
    )

    primary = json.loads(result.evidence_json or "[]")[0]
    start = primary["position"]["char_start"]
    end = primary["position"]["char_end"]

    page = document.pages[0]
    assert page.text[start:end] == PHRASE, "偏移切出来的子串必须**等于**证据文本"


def test_position_json_carries_both_precisions() -> None:
    """精度有**两个**：`text_precision` 与 `bbox_precision`。

    ⚠️ 只写一个 `precision`（`schema.sql` 的注释就是那么写的）是不够的：
    M8 画框必须知道 **bbox** 那个 —— 声明 `char` 却给出整行框，
    框会大到离谱，而**两边都不报错**（见 `document_builder.resolve_span`）。
    """
    document = _doc(_page(1, [LINE]))

    result = attach_evidence(
        _evaluation(located_text=PHRASE), spec=_spec(), document=document, fields={}
    )

    position = json.loads(result.evidence_position or "{}")
    assert position["bbox_precision"] == "line", "行级块只能声明行级 bbox 精度"
    assert position["text_precision"] == "char"
    assert len(position["bbox"]) == 4


def test_is_null_hit_needs_no_evidence_either() -> None:
    """⚠️ **缺失有两种写法**，`expr + is_null` 也是缺失 —— 同样没有片段可指。

    只认 `keyword + absent` 的后果是**一大类规则永远报不出命中**：
    `SUBJ_*_MISSING` / `AMOUNT_MISSING` / `PAY_CYCLE_MISSING` / `ACC_DEADLINE_MISSING` /
    `PAY_PREPAY_MISSING` 全是 `is_null` 型。它们的结论是"合同里确实没有这一项"，
    于是被"命中必须有证据"这条规则一律降级为 `needs_review` ——
    而那是"**判不了**"，与"确认缺失"是**相反**的结论：一份确实缺条款的合同永远报不出来，
    报告上只显示"需人工确认"。

    ⚠️ 这个缺陷**在 T6 的单元测试里测不出来**（那些用例喂的都是 keyword 规则），
    是 T10 的批次级测试才暴露的（真实 40 条里有 5 条属于这种）。
    因此这里补一条**快速守卫**：批次级那条要建库 + 跑 40 条规则，太重，拦不住下一次回归。
    """
    spec = _spec(match_mode="expr", match_text='{"field": "credit_code", "op": "is_null"}')
    evaluation = RuleEvaluation(
        rule_code="SUBJ_CREDIT_CODE_MISSING",
        rule_version=1,
        status=EvaluationStatus.HIT,
        risk_level=RiskLevel.LOW,
        reason_code=ReasonCode.CONDITION_MATCHED,
        reason_text="credit_code 未约定",
        located_text=None,
    )

    result = attach_evidence(
        evaluation, spec=spec, document=_doc(_page(1, [LINE])), fields={}
    )

    assert result.status is EvaluationStatus.HIT, "缺失类命中不得被降级"
    assert result.evidence_json is None


def test_char_level_block_yields_a_char_bbox() -> None:
    """块声明 `char` 精度时，bbox 必须是**选中字符的并集**，不是整行。"""
    document = _doc(_page(1, [LINE], char_level=True))

    result = attach_evidence(
        _evaluation(located_text=PHRASE), spec=_spec(), document=document, fields={}
    )

    position = json.loads(result.evidence_position or "{}")
    assert position["bbox_precision"] == "char"
    # 第 4 个字符起、共 5 个字，每字 10pt（见 `_block`）
    assert position["bbox"] == [90.0, 100.0, 140.0, 112.0]


def test_all_occurrences_are_collected_across_pages() -> None:
    document = _doc(_page(1, [LINE]), _page(2, [LINE]))

    result = attach_evidence(
        _evaluation(located_text=PHRASE), spec=_spec(), document=document, fields={}
    )

    items = json.loads(result.evidence_json or "[]")
    assert [item["position"]["page"] for item in items] == [1, 2]


def test_the_first_occurrence_is_the_primary_one() -> None:
    document = _doc(_page(1, [OTHER_LINE]), _page(2, [LINE]))

    result = attach_evidence(
        _evaluation(located_text=PHRASE), spec=_spec(), document=document, fields={}
    )

    assert json.loads(result.evidence_position or "{}")["page"] == 2
    assert result.evidence_text == PHRASE


def test_evidence_is_capped_and_the_truncation_is_recorded() -> None:
    """超过上限时**截断但留下痕迹** —— 不是静默丢弃。"""
    document = _doc(_repeated_page(MAX_EVIDENCE_ITEMS + 2))

    result = attach_evidence(
        _evaluation(located_text=PHRASE), spec=_spec(), document=document, fields={}
    )

    assert len(json.loads(result.evidence_json or "[]")) == MAX_EVIDENCE_ITEMS
    assert result.hit_detail["evidence_truncated"] is True
    assert result.hit_detail["evidence_limit"] == MAX_EVIDENCE_ITEMS


def test_within_the_cap_there_is_no_truncation_flag() -> None:
    document = _doc(_repeated_page(2))

    result = attach_evidence(
        _evaluation(located_text=PHRASE), spec=_spec(), document=document, fields={}
    )

    assert "evidence_truncated" not in result.hit_detail


# ============================================================
# 2. 定位不到 → **结论作废**
# ============================================================


def test_unlocatable_hit_is_downgraded_to_needs_review() -> None:
    """⚠️ **本文件最要紧的一条。**

    命中片段在标准文档里找不到 → 该结论**作废**。

    不降级的后果：`rule_hits` 里一行 `status=hit`、`evidence_text` 为 NULL，
    M8 画不出框、审批人无法核验 —— 而这条记录**看起来是一条完全正常的命中**。
    这与 M4 §4.6 里 LLM 证据反向核验是同一个要求，
    只不过那里核验的是模型**声称**的引用，这里核验的是匹配**给出**的片段。
    """
    document = _doc(_page(1, [LINE]))

    result = attach_evidence(
        _evaluation(located_text="合同里没有这句话"),
        spec=_spec(),
        document=document,
        fields={},
    )

    assert result.status is EvaluationStatus.NEEDS_REVIEW
    assert result.reason_code is ReasonCode.EVIDENCE_UNCERTAIN
    assert result.evidence_json is None
    assert result.hit_detail["downgraded_from"] == "hit"
    assert result.hit_detail["unlocatable_text"] == "合同里没有这句话", (
        "定位失败时的片段必须留下来 —— 它是排查的唯一线索"
    )


def test_hit_without_any_located_text_is_downgraded() -> None:
    """既没有 `located_text`、也不是缺失类的命中 → 判不了（不是"没问题"）。"""
    document = _doc(_page(1, [LINE]))

    result = attach_evidence(
        _evaluation(located_text=None), spec=_spec(), document=document, fields={}
    )

    assert result.status is EvaluationStatus.NEEDS_REVIEW


def test_cross_block_phrase_with_rewritten_whitespace_is_downgraded() -> None:
    """**已知边界**：跨块的片段若被改写了空白（空格 ↔ 换行），定位不到 → 降级。

    这是刻意偏保守的选择：定位不到就画不出框，画不出框的命中无法核验。
    真实语料上若发现误降太多，再考虑做空白归一化的偏移映射（那是另一件事）。
    """
    document = _doc(_page(1, [LINE, OTHER_LINE]))
    spanning = "赔偿责任。 第九条"  # 原文里是换行，这里写成空格

    result = attach_evidence(
        _evaluation(located_text=spanning), spec=_spec(), document=document, fields={}
    )

    assert result.status is EvaluationStatus.NEEDS_REVIEW


# ============================================================
# 3. 缺失类命中：**天然没有片段**，不得被误伤
# ============================================================


def test_absence_hit_needs_no_evidence_and_is_not_downgraded() -> None:
    """⚠️ "每一条命中都必须有证据"这条规则**不能**套在缺失类上。

    它的结论是"全文都没有" —— 没有可指的片段。强行要求一段原文，
    只会逼出一个编造的输入，或者让这条规则永远判不了。
    而它的依据（检索过哪些词）记在 `hit_detail` 里（T3 写入）。
    """
    spec = _spec(match_text='{"keywords": ["知识产权", "著作权"], "absent": true}')
    hit_detail = {"absent": True, "checked": ["知识产权", "著作权"]}
    evaluation = RuleEvaluation(
        rule_code="R_TEST",
        rule_version=1,
        status=EvaluationStatus.HIT,
        risk_level=RiskLevel.HIGH,
        reason_code=ReasonCode.CONDITION_MATCHED,
        reason_text="全文均未出现相关表述",
        located_text=None,
        hit_detail=hit_detail,
    )

    result = attach_evidence(
        evaluation, spec=spec, document=_doc(_page(1, [LINE])), fields={}
    )

    assert result.status is EvaluationStatus.HIT, "缺失类命中不得被降级"
    assert result.evidence_json is None
    assert result.hit_detail == hit_detail, "依据仍在 hit_detail 里，不得被抹掉"


# ============================================================
# 4. expr 命中：证据取自**字段自身**
# ============================================================


def test_expr_hit_uses_the_field_evidence() -> None:
    """数值比较没有独立原文片段，它的依据是**参与比较的那个字段**。

    这正是 `app/rules/fields.py` 对派生字段的要求："证据必须回指到参与计算的原始字段"。
    """
    spec = _spec(match_mode="expr", match_text='{"field": "pay_days", "op": "gt", "value": 60}')
    document = _doc(_page(1, [LINE]))

    result = attach_evidence(
        _evaluation(located_text=None),
        spec=spec,
        document=document,
        fields={"pay_days": _field("pay_days", "付款期限九十日")},
    )

    assert result.status is EvaluationStatus.HIT, "字段有证据就不该降级"
    assert result.evidence_text == "付款期限九十日"
    assert json.loads(result.evidence_position or "{}")["block_id"] == "p1-b1"


def test_expr_hit_without_field_evidence_is_downgraded() -> None:
    spec = _spec(match_mode="expr", match_text='{"field": "pay_days", "op": "gt", "value": 60}')

    result = attach_evidence(
        _evaluation(located_text=None),
        spec=spec,
        document=_doc(_page(1, [LINE])),
        fields={},  # 字段不在解析结果里
    )

    assert result.status is EvaluationStatus.NEEDS_REVIEW


# ============================================================
# 5. 其余三态：原样返回
# ============================================================


@pytest.mark.parametrize(
    "status",
    [EvaluationStatus.NOT_HIT, EvaluationStatus.NEEDS_REVIEW, EvaluationStatus.NOT_APPLICABLE],
    ids=["not_hit", "needs_review", "not_applicable"],
)
def test_non_hit_states_are_left_untouched(status: EvaluationStatus) -> None:
    """`not_hit` 是"没有风险"、`needs_review` 已经表达了"判不了" —— 都不需要证据。"""
    evaluation = _evaluation(
        status,
        reason_code=(
            ReasonCode.CONDITION_NOT_MATCHED
            if status is EvaluationStatus.NOT_HIT
            else ReasonCode.APPLICABILITY_NOT_MET
        ),
    )

    result = attach_evidence(
        evaluation, spec=_spec(), document=_doc(_page(1, [LINE])), fields={}
    )

    assert result == evaluation
    assert result.evidence_json is None
