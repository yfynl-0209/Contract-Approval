"""字段与条款提取测试（M4 / T8，设计文档 §4.8）。

本文件守住六类**不会自己报错**的失败：

1. **把读取失败报成条款缺失**：扫描件有一页 OCR 失败 → 那一页上的"违约责任"读不到
   → 报"合同缺少违约责任条款"。而真相是**我们没能读到那一页**。
2. **金额用浮点**：精度损失在金额上不可接受，而它不会报错。
3. **中文数字判成"没有约定"**：合同里"三十日"是常态。
4. **白名单在契约层不生效**：一份不校验白名单的契约，等于把白名单降级成文档 ——
   而"防止拼写错误"正是白名单存在的唯一理由。
5. **字符级证据返回整块 bbox**：声明 `char`、画出整行，**两边都不报错**。
6. **覆盖度缺失**：需求规定的 8 项基本信息 / 8 类条款少了一项，
   只在被消费时才暴露，那时离写入已经很远。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.adapters.parse.pymupdf_extractor import PyMuPdfExtractor
from app.config import PROJECT_ROOT
from app.enums import BboxPrecision, FieldStatus, PageStatus, ReasonCode, TextPrecision
from app.ports.field_contract import (
    BasicInfoFieldSet,
    ClauseFieldSet,
    EvidenceSpan,
    ExtractedField,
)
from app.ports.parse_document import DocumentBlock, DocumentPage, StandardDocument
from app.rules.clauses import (
    ALL_CLAUSE_TYPES,
    REQUIRED_CLAUSE_TYPES,
    describe_clause,
    is_known_clause,
    is_required_clause,
)
from app.rules.fields import BASIC_INFO_FIELDS, DIRECT_FIELDS, DIRECT_RULE_FIELDS
from app.services.document_builder import DocumentBuilder
from app.services.field_extractor import FieldExtractor

FIXTURES = PROJECT_ROOT / "mock_approval" / "fixtures"


# ============================================================
# 辅助
# ============================================================


def _evidence(**overrides: object) -> EvidenceSpan:
    base: dict = {
        "page": 1,
        "block_id": "p1-b0",
        "text": "采购合同",
        "bbox": (10.0, 10.0, 50.0, 22.0),
        "char_start": 0,
        "char_end": 4,
        "text_precision": TextPrecision.CHAR,
        "bbox_precision": BboxPrecision.CHAR,
    }
    base.update(overrides)
    return EvidenceSpan(**base)


def _basic_fields(*extra: ExtractedField) -> tuple[ExtractedField, ...]:
    """一份"覆盖 8 项基本信息"的最小集合（全部 not_found）。"""
    return tuple(
        ExtractedField(field_code=code, status=FieldStatus.NOT_FOUND)
        for code in BASIC_INFO_FIELDS
    ) + extra


def _clause_fields(*extra: ExtractedField) -> tuple[ExtractedField, ...]:
    return tuple(
        ExtractedField(field_code=code, status=FieldStatus.NOT_FOUND)
        for code in sorted(REQUIRED_CLAUSE_TYPES)
    ) + extra


def _extract_from(name: str):
    with PyMuPdfExtractor.open((FIXTURES / name).read_bytes()) as extractor:
        document = DocumentBuilder(extractor).build()
    return FieldExtractor(document).extract()


def _document_with(statuses: tuple[PageStatus, ...], *, text: str = "") -> StandardDocument:
    """构造一份"页面状态受控"的文档，用来测四态的分界。"""
    pages = []
    for index, status in enumerate(statuses):
        blocks = ()
        page_text = ""
        if status is PageStatus.OK and text:
            page_text = text
            blocks = (
                DocumentBlock(
                    block_id=f"p{index + 1}-b0",
                    text=text,
                    bbox=(10.0, 10.0, 200.0, 22.0),
                    char_start=0,
                    char_end=len(text),
                    text_precision=TextPrecision.CHAR,
                    # ⚠️ 必须是 `block`：这里没给逐字符几何，而
                    # `bbox_precision=char` 且 `chars` 为空会被契约校验器当场拒掉。
                    bbox_precision=BboxPrecision.BLOCK,
                ),
            )
        pages.append(
            DocumentPage(
                page=index + 1,
                width=595.0,
                height=842.0,
                bbox_space="pdf-point-top-left",
                rotation=0,
                source="text",
                page_status=status,
                text=page_text,
                blocks=blocks,
            )
        )
    return StandardDocument(pages=tuple(pages))


# ============================================================
# 1. 契约：四态不变量与金额精度
# ============================================================


def test_extracted_without_evidence_is_rejected() -> None:
    """**没有证据的结论不可核验** —— 这是本项目的立身之本（"AI 出证据，人做决定"）。"""
    with pytest.raises(ValidationError, match="没有证据"):
        ExtractedField(field_code="amount", status=FieldStatus.EXTRACTED, evidence=())


def test_not_found_with_evidence_is_rejected() -> None:
    """"没找到"却带着证据是自相矛盾 —— 两者不可能同时为真。"""
    with pytest.raises(ValidationError, match="自相矛盾"):
        ExtractedField(
            field_code="amount",
            status=FieldStatus.NOT_FOUND,
            evidence=(_evidence(),),
        )


@pytest.mark.parametrize("status", [FieldStatus.FAILED, FieldStatus.UNCERTAIN])
def test_non_extracted_states_require_reason_code(status: FieldStatus) -> None:
    """`failed` / `uncertain` **必须**给 `reason_code`。

    否则 M5 无法据此判 `needs_review` —— 而"该人工看"的字段看起来像正常结论。
    """
    with pytest.raises(ValidationError, match="reason_code"):
        ExtractedField(field_code="amount", status=status)


def test_amount_rejects_scientific_notation() -> None:
    """金额不得用科学计数法 —— `str(1200000.0)` 会退化成 `1.2e+06`，
    而那个字符串**看起来仍然像个数字**。"""
    with pytest.raises(ValidationError, match="科学计数法"):
        ExtractedField(
            field_code="amount",
            status=FieldStatus.EXTRACTED,
            value_decimal="1.2e+06",
            evidence=(_evidence(),),
        )


def test_amount_rejects_non_decimal_string() -> None:
    with pytest.raises(ValidationError, match="合法十进制"):
        ExtractedField(
            field_code="amount",
            status=FieldStatus.EXTRACTED,
            value_decimal="一百二十万",
            evidence=(_evidence(),),
        )


def test_amount_keeps_decimal_precision() -> None:
    """十进制字符串能精确还原 —— 浮点表示做不到这件事。"""
    field = ExtractedField(
        field_code="amount",
        status=FieldStatus.EXTRACTED,
        value_decimal="1200000.01",
        currency="CNY",
        evidence=(_evidence(),),
    )

    assert Decimal(field.value_decimal) == Decimal("1200000.01")


# ============================================================
# 2. 契约：白名单与覆盖度（**在构造层生效**）
# ============================================================


def test_basic_info_set_rejects_unknown_field_code() -> None:
    """**这是白名单存在的唯一理由**：拼错的字段码必须构造不出来。

    初版只查重、不查合法性 —— 那样一份契约等于把白名单降级成文档，
    而"一个拼写错误变成'合同缺少预付款约定'"这条通路依然畅通。
    """
    with pytest.raises(ValidationError, match="未登记字段码"):
        BasicInfoFieldSet(
            fields=_basic_fields(
                ExtractedField(field_code="intellectual_propertys", status=FieldStatus.NOT_FOUND)
            )
        )


def test_basic_info_set_requires_all_eight_items() -> None:
    """**覆盖需求 8 项**是构造期不变量，不是靠某条测试记得断言。

    缺 `contract_title` 这类记录，要到被消费时才暴露 —— 那时离写入已很远。
    """
    partial = tuple(
        ExtractedField(field_code=code, status=FieldStatus.NOT_FOUND)
        for code in sorted(BASIC_INFO_FIELDS)
        if code != "contract_title"
    )
    with pytest.raises(ValidationError, match="缺少需求规定的信息项"):
        BasicInfoFieldSet(fields=partial)


def test_clause_set_rejects_unknown_clause_code() -> None:
    with pytest.raises(ValidationError, match="未登记条款码"):
        ClauseFieldSet(
            fields=_clause_fields(
                ExtractedField(field_code="totally_unknown", status=FieldStatus.NOT_FOUND)
            )
        )


def test_clause_set_requires_all_required_clauses() -> None:
    with pytest.raises(ValidationError, match="缺少需求规定的条款类别"):
        ClauseFieldSet(
            fields=tuple(
                ExtractedField(field_code=code, status=FieldStatus.NOT_FOUND)
                for code in sorted(REQUIRED_CLAUSE_TYPES)
                if code != "acceptance"
            )
        )


def test_clause_set_accepts_extension_clauses() -> None:
    """扩展类（`auto_renewal`）**允许**出现 —— 已有规则在消费它。"""
    fields = _clause_fields(
        ExtractedField(field_code="auto_renewal", status=FieldStatus.NOT_FOUND)
    )
    assert ClauseFieldSet(fields=fields).get("auto_renewal") is not None


def test_duplicate_field_codes_are_rejected() -> None:
    with pytest.raises(ValidationError, match="重复"):
        BasicInfoFieldSet(
            fields=_basic_fields(
                ExtractedField(field_code="amount", status=FieldStatus.NOT_FOUND)
            )
        )


# ============================================================
# 3. 契约：证据区间与 bbox 必须归一化
# ============================================================


def test_empty_evidence_span_is_rejected() -> None:
    """空区间能构造出一个"看起来像个证据"的对象，而它画出来是零面积。"""
    with pytest.raises(ValidationError, match="必须非空"):
        _evidence(char_start=3, char_end=3)


def test_reversed_evidence_span_is_rejected() -> None:
    with pytest.raises(ValidationError, match="必须非空"):
        _evidence(char_start=5, char_end=2)


def test_reversed_evidence_bbox_is_rejected() -> None:
    with pytest.raises(ValidationError, match="归一化"):
        _evidence(bbox=(50.0, 10.0, 10.0, 22.0))


def test_char_precision_requires_matching_text_length() -> None:
    """声明 `char` 时，证据文本长度必须等于区间长度。

    不等就说明"框与文字对不上" —— 而这正是"声明 char、画出整行"的特征。
    """
    with pytest.raises(ValidationError, match="框与文字对不上"):
        _evidence(text="一整行很长的文字", char_start=0, char_end=4)


# ============================================================
# 4. 条款与字段白名单本身
# ============================================================


def test_required_clause_types_are_exactly_eight() -> None:
    """需求规定 8 类 —— 数量写死。**交付与验收必须分开**。

    合并成一类时，"缺交付条款"与"缺验收条款"两条规则读到同一个键，
    结论必然一致 —— 而真实合同完全可能约定了交付却没约定验收。
    """
    assert len(REQUIRED_CLAUSE_TYPES) == 8
    assert {"delivery", "acceptance"} <= set(REQUIRED_CLAUSE_TYPES)
    assert "delivery_acceptance" not in ALL_CLAUSE_TYPES

    # 扩展类与需求类**分开声明**：混在一起时"需求覆盖"与"规则需要"会互相掩盖
    assert is_required_clause("payment")
    assert not is_required_clause("auto_renewal")
    assert is_known_clause("auto_renewal"), "它仍须可被规则引用"


def test_basic_info_fields_are_exactly_eight() -> None:
    """需求规定的 8 项基本信息 —— 含标题、编号与**两个日期**。

    初版把这 8 项与"规则计算所需字段"混在一个桶里，于是清单里
    **连 `contract_title` / `effective_date` 都没有** ——
    按那份清单实现，"覆盖 8 项"这条验收根本无法成立。
    """
    assert len(BASIC_INFO_FIELDS) == 8
    assert {"contract_title", "contract_number", "effective_date", "expiry_date"} <= set(
        BASIC_INFO_FIELDS
    )
    # 生效时间与签订时间**不是同一个业务概念**，不得互相顶替
    assert "sign_date" not in BASIC_INFO_FIELDS
    assert set(BASIC_INFO_FIELDS).isdisjoint(DIRECT_RULE_FIELDS)
    assert set(DIRECT_FIELDS) == set(BASIC_INFO_FIELDS) | set(DIRECT_RULE_FIELDS)


def test_unknown_clause_code_is_not_known() -> None:
    assert is_known_clause("intellectual_property")
    assert not is_known_clause("intellectual_propertys")
    assert describe_clause("intellectual_property") == "知识产权"
    assert describe_clause("intellectual_propertys") == "未知条款类型"


# ============================================================
# 5. 真实文本夹具上的提取
# ============================================================


def test_baseline_fixture_extracts_basic_info() -> None:
    basic = _extract_from("contract_01_clean.pdf").basic_info

    assert basic.schema_version == 1
    assert basic.get("contract_title").value_text == "采购合同"
    assert basic.get("contract_number").value_text == "HT-2026-0001"
    assert basic.get("party_a").value_text == "示例科技有限公司"
    assert basic.get("party_b").value_text == "某某设备有限公司"

    amount = basic.get("amount")
    assert amount.status is FieldStatus.EXTRACTED
    assert Decimal(amount.value_decimal) == Decimal("1200000.00")
    assert amount.currency == "CNY"
    assert amount.evidence and amount.evidence[0].page == 1

    # 生效 / 到期：这一份夹具里写全了，因此必须是提取到，而不是 not_found
    assert basic.get("effective_date").value_text == "2026-08-01"
    assert basic.get("expiry_date").value_text == "2027-07-31"


def test_other_fixtures_report_absent_dates_as_not_found() -> None:
    """反面：其余夹具**没有**这两个日期，必须如实判 `not_found`。

    只测"能提取到"会漏掉"把没有的东西报成有"这个方向；
    只测"缺失"会漏掉"把有的东西报成没有"。两边都要有真实夹具覆盖。
    """
    basic = _extract_from("contract_04_standard_goods.pdf").basic_info

    assert basic.get("effective_date").status is FieldStatus.NOT_FOUND
    assert basic.get("expiry_date").status is FieldStatus.NOT_FOUND


def test_chinese_numerals_are_parsed_not_treated_as_missing() -> None:
    """**"三十日"必须被解析出来，而不是判成"没有约定"。**"""
    basic = _extract_from("contract_01_clean.pdf").basic_info

    pay_days = basic.get("pay_days")
    assert pay_days.status is FieldStatus.EXTRACTED
    assert pay_days.value_decimal == "30"
    assert "三十" in pay_days.evidence[0].text

    acceptance = basic.get("acceptance_days")
    assert acceptance.status is FieldStatus.EXTRACTED
    assert acceptance.value_decimal == "10", "『十个工作日』应当解析为 10"


def test_absent_field_is_not_found_when_the_document_is_readable() -> None:
    basic = _extract_from("contract_01_clean.pdf").basic_info

    credit = basic.get("credit_code")
    assert credit.status is FieldStatus.NOT_FOUND
    assert credit.evidence == ()
    assert credit.reason_text


def test_missing_clause_is_reported_as_not_found() -> None:
    """**验收 7 的前半**：缺知识产权条款 → `not_found`（而不是 failed/uncertain）。"""
    clauses = _extract_from("contract_03_dev_no_ip.pdf").clause_info

    ip = clauses.get("intellectual_property")
    assert ip.status is FieldStatus.NOT_FOUND, (
        f"正文确实不含知识产权条款，应判 not_found，实际 {ip.status}"
    )
    assert ip.evidence == ()


def test_delivery_and_acceptance_are_separately_determined() -> None:
    """**交付与验收必须能被分别判定。**

    夹具里两句分别写着交付与验收，因此两条都该命中；
    而"缺 IP"的夹具里验收也在 —— 关键是两个键**各自独立**，
    而不是读同一个 `delivery_acceptance`。
    """
    clauses = _extract_from("contract_01_clean.pdf").clause_info

    assert clauses.get("delivery").status is FieldStatus.EXTRACTED
    assert clauses.get("acceptance").status is FieldStatus.EXTRACTED
    assert clauses.get("delivery").field_code != clauses.get("acceptance").field_code


def test_present_clauses_are_extracted_with_evidence() -> None:
    clauses = _extract_from("contract_01_clean.pdf").clause_info

    for code in ("payment", "liability", "confidentiality", "intellectual_property"):
        field = clauses.get(code)
        assert field.status is FieldStatus.EXTRACTED, f"{code} 应当被判为存在"
        assert field.evidence, f"{code} 必须带证据"
        assert field.evidence[0].text


def test_all_clauses_get_a_conclusion() -> None:
    """**验收 8 的条款部分**：每一类都必须有结论，不能只验一两条。"""
    clauses = _extract_from("contract_01_clean.pdf").clause_info

    assert {field.field_code for field in clauses.fields} == set(ALL_CLAUSE_TYPES)


def test_all_direct_fields_get_a_conclusion() -> None:
    """**验收 8 的基本信息部分**：`DIRECT_FIELDS` 每一项都必须有结论。

    逐项断言而不是抽查 —— 抽查漏掉的那一项会静默地没有结论，
    而 M5 引用它时只会得到"字段为空"。
    """
    basic = _extract_from("contract_01_clean.pdf").basic_info

    assert {field.field_code for field in basic.fields} == set(DIRECT_FIELDS)


def test_derived_fields_are_not_invented_here() -> None:
    """派生字段（如预付款比例）**不在**这里产出。

    它们由其他字段算出、没有独立原文片段，而证据必须回指原始字段 ——
    那是 M5 的事。在这里硬造一个没有证据的数字，
    等于让一个**猜出来的值**以 `extracted` 的身份进入规则评价。
    """
    basic = _extract_from("contract_02_prepay.pdf").basic_info

    assert basic.get("prepay_ratio") is None


# ============================================================
# 6. 证据几何：按精度分派（**本文件最重要的一条**）
# ============================================================


def test_char_evidence_bbox_is_the_union_of_matched_characters() -> None:
    """**字符级证据必须返回命中字符 bbox 的并集，而不是整块 bbox。**

    初版返回整块 bbox 却声明 `bbox_precision=char` ——
    M8 照 `char` 去画字符框，画出来是一整行，而**两边都不报错**。
    这是"声明与数据不一致"最典型的一种：两处单独看都正常。
    """
    result = _extract_from("contract_01_clean.pdf")
    pay_days = result.basic_info.get("pay_days")
    evidence = pay_days.evidence[0]

    assert evidence.bbox_precision is BboxPrecision.CHAR
    # ⚠️ 证据文本是**命中整段**（含"日内支付"这类上下文），不是只取取值 `三十`。
    # 上下文对人核对是必要的 —— 只给"三十"看不出它是"日内支付"还是"个工作日"。
    # 区间、文本长度、bbox 三者必须**指向同一段文字**，这条是硬约束。
    assert evidence.text == "三十日内支付"
    assert len(evidence.text) == evidence.char_end - evidence.char_start

    # 整块的框 vs 命中字符的框必须不同 —— 相同就说明没有求并集
    with PyMuPdfExtractor.open((FIXTURES / "contract_01_clean.pdf").read_bytes()) as ex:
        document = DocumentBuilder(ex).build()
    page = document.pages[0]
    block = next(b for b in page.blocks if b.block_id == evidence.block_id)
    assert block.bbox != evidence.bbox, "证据框仍是整块 bbox"
    assert evidence.bbox[2] - evidence.bbox[0] < block.bbox[2] - block.bbox[0]


def test_evidence_bbox_falls_back_to_block_when_not_char_aligned() -> None:
    """区间无法与字符几何对齐时，**降级为 `block` 精度**，而不是拿块 bbox 冒充 char。

    什么时候对不齐：块的 `chars` 没有覆盖它的整个字符区间 ——
    例如某段文字只有部分逐字符几何（契约允许 `chars` 为空，
    自然也就允许它只覆盖一部分）。那时求并集会**少半段**，
    而框看起来仍然像个框，只是短了。

    这个分支在解析正常的 PDF 上走不到（`DocumentBuilder` 产出的块
    总是逐字符覆盖的），因此**只能用构造出来的页面测** —— 但它必须被测：
    走不到的分支里最危险的一种就是"以为它会降级，其实它标了 char"。
    """
    from app.ports.parse_document import DocumentChar
    from app.services.document_builder import resolve_span

    block = DocumentBlock(
        block_id="p1-b0",
        text="abcd",
        bbox=(10.0, 10.0, 50.0, 22.0),
        char_start=0,
        char_end=4,
        text_precision=TextPrecision.CHAR,
        bbox_precision=BboxPrecision.CHAR,
        # 只有第一个字符有几何 —— 覆盖不全
        chars=(
            DocumentChar(text="a", bbox=(10.0, 10.0, 20.0, 22.0), char_start=0, char_end=1),
        ),
    )
    page = DocumentPage(
        page=1,
        width=100.0,
        height=50.0,
        bbox_space="pdf-point-top-left",
        rotation=0,
        source="text",
        page_status=PageStatus.OK,
        text="abcd",
        blocks=(block,),
    )

    resolved = resolve_span(page, 0, 4)

    assert resolved is not None
    assert resolved.bbox == block.bbox, "对不齐时必须退回块 bbox"
    assert resolved.bbox_precision is BboxPrecision.BLOCK, "精度必须**同步**降级"


def test_ocr_evidence_carries_line_precision_and_mapped_bbox() -> None:
    """扫描件走 OCR，证据精度**必须如实是 `line`**，且坐标已回映射。"""
    from app.ports.ocr_gateway import OcrLine, OcrPageResult, RenderTransform
    from app.ports.pdf_extractor import PageGeometry, PageImage

    class _Extractor:
        @property
        def page_count(self) -> int:
            return 1

        def geometry(self, index: int) -> PageGeometry:
            return PageGeometry(
                page=1,
                width=595.0,
                height=842.0,
                rotation=0,
                unrotated_width=595.0,
                unrotated_height=842.0,
            )

        def lines(self, index: int):
            return ()

        def render(self, index: int, *, dpi: int) -> PageImage:
            return PageImage(
                png=b"x",
                transform=RenderTransform(
                    matrix=(2.0, 0.0, 0.0, 2.0, 0.0, 0.0),
                    image_width=1190,
                    image_height=1684,
                    dpi=dpi,
                ),
            )

        def close(self) -> None:  # pragma: no cover
            pass

    class _Ocr:
        @property
        def engine(self) -> str:
            return "fake"

        @property
        def version(self) -> str:
            return "1"

        def recognize(self, image_png: bytes, *, page: int) -> OcrPageResult:
            return OcrPageResult(
                page=page,
                lines=(
                    OcrLine(
                        text="合同总金额：人民币 1,200,000.00 元",
                        bbox=(40.0, 40.0, 400.0, 60.0),
                        confidence=0.95,
                    ),
                ),
                confidence=0.95,
                detected_lines=1,
                raw_confidence=0.95,
            )

        def close(self) -> None:  # pragma: no cover
            pass

    document = DocumentBuilder(_Extractor(), ocr=_Ocr()).build()
    amount = FieldExtractor(document).extract().basic_info.get("amount")

    assert amount.status is FieldStatus.EXTRACTED
    assert amount.evidence[0].text_precision is TextPrecision.LINE
    assert amount.evidence[0].bbox_precision is BboxPrecision.LINE
    assert amount.evidence[0].bbox == (20.0, 20.0, 200.0, 30.0)


# ============================================================
# 7. 四态的分界
# ============================================================


def test_failed_page_turns_misses_into_failed_not_not_found() -> None:
    """**存在 `failed` 页时，未命中的字段必须是 `failed`。**

    这条守住的是最危险的那个误报：扫描件有一页 OCR 失败，
    那一页上的"违约责任"读不到 → 报"合同缺少违约责任条款" ——
    而真相是**我们没能读到那一页**。报告看起来完全正常。
    """
    document = _document_with((PageStatus.FAILED,))
    basic = FieldExtractor(document).extract().basic_info

    credit = basic.get("credit_code")
    assert credit.status is FieldStatus.FAILED
    assert credit.status is not FieldStatus.NOT_FOUND
    assert credit.reason_code is ReasonCode.EXTRACTION_FAILED
    assert "第 [1] 页" in (credit.reason_text or "")


def test_uncertain_page_turns_misses_into_uncertain() -> None:
    document = _document_with((PageStatus.UNCERTAIN,))
    basic = FieldExtractor(document).extract().basic_info

    credit = basic.get("credit_code")
    assert credit.status is FieldStatus.UNCERTAIN
    assert credit.reason_code is ReasonCode.EVIDENCE_UNCERTAIN


def test_blank_page_does_not_block_not_found() -> None:
    """反面：**单个 `blank` 页不是失败**。

    合同有空白背页很正常。把它也算成"没读到"，会让每一份带空白页的合同
    都无法给出 `not_found` 结论 —— 缺失类规则全部退化成 `needs_review`。
    """
    document = _document_with((PageStatus.OK, PageStatus.BLANK), text="采购合同")
    basic = FieldExtractor(document).extract().basic_info

    assert basic.get("credit_code").status is FieldStatus.NOT_FOUND


def test_extraction_still_works_when_a_later_page_failed() -> None:
    """有失败页**不影响已经读到的东西** —— 只影响"未命中"的结论。"""
    document = _document_with(
        (PageStatus.OK, PageStatus.FAILED), text="合同总金额：人民币 500.00 元"
    )
    basic = FieldExtractor(document).extract().basic_info

    amount = basic.get("amount")
    assert amount.status is FieldStatus.EXTRACTED
    assert Decimal(amount.value_decimal) == Decimal("500.00")


# ============================================================
# 8. 中文数字解析的边界
# ============================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [("十", 10), ("十五", 15), ("三十", 30), ("四十五", 45), ("九十", 90), ("7", 7)],
)
def test_chinese_numeral_conversion(text: str, expected: int) -> None:
    from app.services.field_extractor import _to_number

    assert _to_number(text) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 全角逗号 + 全角句点 + 全角零
        ("人民币800，000．0０元", "800000.00"),
        # ASCII 对照
        ("人民币 800,000.00 元", "800000.00"),
        # 全角数字（整串）
        ("人民币１２００元", "1200"),
    ],
)
def test_amount_is_folded_from_full_width_forms(raw: str, expected: str) -> None:
    """**全角数字与标点必须折叠** —— 这是配对夹具测试抓出来的真实缺陷。

    实测：扫描件里的 `800,000.00` 被 OCR 读成

    ```text
    人民币800，000．0０元
           ↑ U+FF0C 全角逗号   ↑ U+FF0E 全角句点   ↑ U+FF10 全角零
    ```

    而当时的正则只认 ASCII 的 `[\\d,]`，**匹配到 `800` 就停了** ——
    金额静默变成 `800`（差 1000 倍），随后被拿去和阈值比较，结论直接反转，
    而日志、证据、界面**全都"看起来正常"**。

    ⚠️ 这条快速用例**必须**存在：真正抓到它的是走真实 OCR 的配对夹具测试，
    而那条默认跳过。没有这条，修复就没有常规守卫，而它下一次照样会静默复发
    —— 并且**任何集成方都不会发现**，因为 800 也是一个合法的正数金额。

    同时断言**原文被保留**：`value_text` 是页面上真实印着的东西（人要拿它核对），
    折叠只用于 `value_decimal`。
    """
    from app.services.field_extractor import _parse_amount

    parsed = _parse_amount(raw)

    assert parsed is not None
    value_text, value_decimal, _currency = parsed
    assert value_decimal == expected
    assert value_text == raw, "原文必须原样保留，不得把折叠结果当成原文"


def test_numbers_fold_full_width_digits() -> None:
    """整数型字段同样要折叠：OCR 会把 `10` 读成全角 `１０`。"""
    from app.services.field_extractor import _to_number

    assert _to_number("１０") == 10
    assert _to_number("90") == 90


def test_unsupported_chinese_numeral_is_not_guessed() -> None:
    """超出范围的写法返回 `None`（→ `uncertain`），**绝不猜**。

    ⚠️ 这里**只测真正超出范围**的写法。写这份测试时我把 `三百零五` 当成了
    "不支持"，而实现其实能正确解析为 305 —— **是断言写错了，不是实现错了**。
    本实现的边界是**零~千**（没有 `万`）。
    """
    from app.services.field_extractor import _to_number

    assert _to_number("一万") is None, "万超出实现范围（零~千）"
    assert _to_number("若干") is None
    assert _to_number("") is None
    # 反面：范围内的更复杂写法**确实**能解析 —— 不要因为"看起来复杂"就漏掉它
    assert _to_number("三百零五") == 305
    assert _to_number("一千零二十") == 1020
