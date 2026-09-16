"""派生事实解析的测试（M5 / T3a）。

## 本文件守住的五类"写错了也不报错"

1. **付款进度被当成预付款** —— 夹具 `contract_03_dev_no_ip.pdf` 里写着
   "合同生效后支付百分之三十，验收合格后支付百分之六十，质保期满后支付百分之十"。
   不限定上下文的实现会读出 `0.6`，恰好命中
   `PAY_PREPAY_RATIO_HIGH_FOR_BUYER (prepay_ratio > 0.3)` —— 报出一条
   **"预付款比例 60%"的高风险**，而这份合同的预付款是"未约定"。
2. **"读不出"被报成"未约定"** —— `PAY_PREPAY_MISSING`（`is_null`）会把实现缺口
   说成业务结论"合同没约定预付款"。
3. **按日费率被当成总比例** —— `contract_02_prepay.pdf` 的
   "每逾期一日按合同总金额的千分之一支付违约金" 会让
   `liability_party_b_ratio = 0.001`：一个**语义错了却完全合法**的数。
4. **多个矛盾比例只取一个** —— 取任何一个都是猜，必须 `uncertain`。
5. **字段合法却没有生产者** —— 白名单拦得住拼错，拦不住"没人生产"
   （`prepay_ratio` 等三个字段曾经整整缺席）。

前四类都不抛异常、不报错，只会让规则拿到**错的输入**。

## 为什么对着真实夹具写

判据是"上下文"（预付 / 违约金 / 按日），而上下文只有真实合同文本才检验得出：
自造一句"预付款比例 60%"证明不了"不会把付款进度误判为预付款"。
"""

from __future__ import annotations

import pytest

from app.config import PROJECT_ROOT
from app.enums import FieldStatus, PageStatus, ReasonCode, TextPrecision, BboxPrecision
from app.ports.parse_document import (
    BBOX_SPACE,
    DocumentBlock,
    DocumentPage,
    StandardDocument,
)
from app.rules.fact_resolver import resolve_derived_fields
from app.rules.fields import ALL_EXPR_FIELDS, DERIVED_RULE_FIELDS, producer_of

FIXTURES = PROJECT_ROOT / "mock_approval" / "fixtures"

#: 夹具文件名 → 内容（见 `mock_approval/contract_texts.py`）
BASELINE = "contract_01_clean.pdf"  # HT-2026-0001：无预付款，任何一方 10% 违约金
PREPAY = "contract_02_prepay.pdf"  # HT-2026-0002：预付款 60%（验收 2 的场景）
MILESTONES = "contract_03_dev_no_ip.pdf"  # HT-2026-0003：**付款进度** 30/60/10
STANDARD = "contract_04_standard_goods.pdf"  # HT-2026-0004：任何一方 5% 违约金


# ============================================================
# 两种构造：真实链路 / 受控文本
# ============================================================


def _from_fixture(name: str) -> StandardDocument:
    """**真实链路**：PDF → 抽取 → 标准文档。"""
    from app.adapters.parse.pymupdf_extractor import PyMuPdfExtractor
    from app.services.document_builder import DocumentBuilder

    with PyMuPdfExtractor.open((FIXTURES / name).read_bytes()) as extractor:
        return DocumentBuilder(extractor).build()


def _doc(*lines: str) -> StandardDocument:
    """受控文本：用来构造夹具里**没有**的写法（甲方单独承担、矛盾比例…）。

    每行一个块，块是页文本的精确切片 —— `DocumentPage` 的校验器会逐块断言这件事。
    """
    blocks: list[DocumentBlock] = []
    cursor = 0
    for index, line in enumerate(lines):
        if index:
            cursor += 1
        blocks.append(
            DocumentBlock(
                block_id=f"p1-b{index + 1}",
                text=line,
                bbox=(50.0, 100.0 + index * 20, 500.0, 112.0 + index * 20),
                char_start=cursor,
                char_end=cursor + len(line),
                text_precision=TextPrecision.CHAR,
                bbox_precision=BboxPrecision.LINE,
            )
        )
        cursor += len(line)

    page = DocumentPage(
        page=1,
        width=595.0,
        height=842.0,
        bbox_space=BBOX_SPACE,
        rotation=0,
        source="text",
        page_status=PageStatus.OK,
        text="\n".join(lines),
        blocks=tuple(blocks),
    )
    return StandardDocument(pages=(page,))


def _fact(document: StandardDocument | None, field_code: str):
    return resolve_derived_fields(document)[field_code]


# ============================================================
# 1. 预付款比例
# ============================================================


def test_prepay_ratio_is_read_from_the_prepay_clause() -> None:
    """**验收 2 的前提**：HT-2026-0002 的预付款比例必须是 0.6。

    没有这条，`PAY_PREPAY_RATIO_HIGH_FOR_BUYER` 根本拿不到输入，
    而验收 2「总风险 = high」却可能被**别的**高风险规则撞中而通过。
    """
    ratio = _fact(_from_fixture(PREPAY), "prepay_ratio")

    assert ratio.status is FieldStatus.EXTRACTED
    assert ratio.value_decimal == "0.6"
    assert ratio.evidence, "extracted 必须有证据"
    # ⚠️ 断言落在**证据**上而不是 `value_text`：这份合同有两处写同一个比例
    # （抬头的"预付 60%"与正文条款的"百分之六十"），两者的原文写法不同，
    # 而 `value_text` 只留第一处的写法。**证据必须包含正文条款那一处** ——
    # 抬头是摘要，条款才是可核验的依据。
    texts = [span.text for span in ratio.evidence]
    assert any("百分之六十" in text for text in texts), texts
    assert ratio.value_text in {"60%", "百分之六十"}


def test_prepay_ratio_evidence_points_at_a_real_block() -> None:
    """证据必须能反推回块 —— 否则 M8 画不出框。"""
    document = _from_fixture(PREPAY)
    ratio = _fact(document, "prepay_ratio")

    span = ratio.evidence[0]
    page = next(p for p in document.pages if p.page == span.page)
    assert page.text[span.char_start : span.char_end] == span.text
    assert any(block.block_id == span.block_id for block in page.blocks)


def test_payment_milestones_are_not_prepay() -> None:
    """⚠️ **本文件最要紧的一条。**

    `contract_03_dev_no_ip.pdf` 的第三条是**付款进度**：

    ```text
    "合同生效后支付百分之三十，验收合格后支付百分之六十，质保期满后支付百分之十。"
    ```

    不限定上下文的实现会读出 `0.3 / 0.6 / 0.1`，其中 `0.6` 命中
    `PAY_PREPAY_RATIO_HIGH_FOR_BUYER (prepay_ratio > 0.3)` ——
    报出一条**"预付款比例 60%"的高风险**，而这份合同**没有预付款约定**。

    所以断言必须落在 `not_found` 上，而不是"没报错"。
    """
    ratio = _fact(_from_fixture(MILESTONES), "prepay_ratio")

    assert ratio.status is FieldStatus.NOT_FOUND
    assert ratio.value_decimal is None


def test_baseline_has_no_prepay_agreement() -> None:
    ratio = _fact(_from_fixture(BASELINE), "prepay_ratio")

    assert ratio.status is FieldStatus.NOT_FOUND


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("第一条 甲方应支付合同总金额的百分之六十作为预付款。", "0.6"),
        ("第一条 甲方应支付合同总金额的 60% 作为预付款。", "0.6"),
        # OCR 会把 % 读成全角 ％
        ("第一条 甲方应支付合同总金额的 60％ 作为预付款。", "0.6"),
        # 全角数字
        ("第一条 甲方应支付合同总金额的 ６０％ 作为预付款。", "0.6"),
        ("第一条 预付款比例为百分之十。", "0.1"),
        ("第一条 预付款比例为百分之五。", "0.05"),
    ],
    ids=["chinese", "arabic", "fullwidth-percent", "fullwidth-digits", "ten", "five"],
)
def test_ratio_writing_variants(line: str, expected: str) -> None:
    """中文与阿拉伯数字**都要认**，全角也要折叠 —— 三套写法混用在真实合同里很常见。"""
    assert _fact(_doc(line), "prepay_ratio").value_decimal == expected


def test_clause_with_amount_but_no_ratio_is_uncertain_not_missing() -> None:
    """写了金额却没写比例 → **读不出**，不是"未约定"。

    ⚠️ 判 `not_found` 的后果：`PAY_PREPAY_MISSING` 会报出一条
    **"未约定预付款比例"** —— 而合同里明明写了预付款 80 万。
    把"我们读不出来"说成"合同没有"，是这类系统最不该犯的错。
    """
    ratio = _fact(_doc("第一条 甲方应支付人民币 800000 元作为预付款。"), "prepay_ratio")

    assert ratio.status is FieldStatus.UNCERTAIN
    assert ratio.reason_code is ReasonCode.EVIDENCE_UNCERTAIN
    assert "未约定" in ratio.reason_text or "不得据此判" in ratio.reason_text


def test_the_neighbour_sentence_mentioning_prepay_is_not_a_clause() -> None:
    """`contract_02` 里"乙方应于收到预付款后四十五日内完成交付"含「预付」但**不含比例**。

    把"含关键词"直接当成"存在比例条款"，这条会被判成
    "有预付款约定但读不出比例" —— 一个凭空的 `uncertain`。
    """
    ratio = _fact(
        _doc(
            "第一条 乙方应于收到预付款后四十五日内完成交付。",
            "第二条 双方应友好协商解决争议。",
        ),
        "prepay_ratio",
    )

    assert ratio.status is FieldStatus.NOT_FOUND


def test_conflicting_ratios_yield_uncertain_with_both_evidences() -> None:
    """**多个矛盾比例必须 `uncertain`** —— 取任何一个都是猜。

    而且**两处证据都要留下**：审批人要同时看到两条条款才能判断谁对，
    只留最后一条会让人以为合同里只有那一条。
    """
    ratio = _fact(
        _doc(
            "第一条 甲方应支付合同总金额的百分之六十作为预付款。",
            "第二条 预付款比例调整为百分之三十。",
        ),
        "prepay_ratio",
    )

    assert ratio.status is FieldStatus.UNCERTAIN
    assert ratio.reason_code is ReasonCode.EVIDENCE_UNCERTAIN
    assert ratio.value_decimal is None, "冲突时不得给一个值"
    assert len(ratio.evidence) == 2
    assert "0.6" in ratio.reason_text and "0.3" in ratio.reason_text


def test_the_same_ratio_written_twice_is_not_a_conflict() -> None:
    """同值重复出现不是冲突 —— 抬头摘要与正文条款一致是常态。"""
    ratio = _fact(
        _doc(
            "付款方式：合同生效后预付 60%，余款验收后支付",
            "第一条 甲方应支付合同总金额的百分之六十作为预付款。",
        ),
        "prepay_ratio",
    )

    assert ratio.status is FieldStatus.EXTRACTED
    assert ratio.value_decimal == "0.6"


# ============================================================
# 2. 违约金比例
# ============================================================


@pytest.mark.parametrize(
    ("name", "expected"),
    [(BASELINE, "0.1"), (STANDARD, "0.05")],
    ids=["baseline-10pct", "standard-goods-5pct"],
)
def test_any_party_wording_applies_to_both_sides(name: str, expected: str) -> None:
    """"任何一方违约，应向对方支付…百分之十" —— **两边都是 10%**。

    只判给其中一方，会让另一方的比例凭空变成"未约定"（进而触发缺失类规则误报）。
    """
    document = _from_fixture(name)
    party_a = _fact(document, "liability_party_a_ratio")
    party_b = _fact(document, "liability_party_b_ratio")

    assert party_a.value_decimal == expected
    assert party_b.value_decimal == expected


def test_party_a_only_wording_does_not_leak_to_party_b() -> None:
    ratio_a = _fact(_doc("第三条 甲方违约的，应向乙方支付合同总金额百分之三十的违约金。"), "liability_party_a_ratio")
    ratio_b = _fact(_doc("第三条 甲方违约的，应向乙方支付合同总金额百分之三十的违约金。"), "liability_party_b_ratio")

    assert ratio_a.value_decimal == "0.3"
    assert ratio_b.status is FieldStatus.NOT_FOUND, "只写了甲方，乙方不得跟着有值"


def test_party_b_only_wording_does_not_leak_to_party_a() -> None:
    lines = ("第三条 乙方逾期交付的，应向甲方支付合同总金额百分之二十的违约金。",)

    assert _fact(_doc(*lines), "liability_party_b_ratio").value_decimal == "0.2"
    assert _fact(_doc(*lines), "liability_party_a_ratio").status is FieldStatus.NOT_FOUND


def test_per_day_rate_is_not_a_total_ratio() -> None:
    """⚠️ `contract_02` 第三条："每逾期一日按合同总金额的千分之一支付违约金"。

    `千分之一` 是**日费率**，不是"乙方违约金比例"。直接取用会得到
    `liability_party_b_ratio = 0.001` —— 一个**语义错了却完全合法**的数
    （它会让 `> 0.3` 的规则报 `not_hit`，而真实情况是"合同里没有总比例约定"）。

    正确结论是 `uncertain`：有违约金条款，但读不出总比例 ——
    于是 `LIAB_RATIO_HIGH_FOR_PARTY_B` 走 `needs_review` 交人工，
    **不是** `not_hit`（那是"没问题"的意思）。
    """
    ratio = _fact(_from_fixture(PREPAY), "liability_party_b_ratio")

    assert ratio.status is FieldStatus.UNCERTAIN
    assert ratio.value_decimal is None, "日费率不得写成总比例"


def test_per_day_sentence_with_a_cap_still_yields_uncertain() -> None:
    """**已知能力边界**：同句既有日费率又有总上限时，本实现一律判 `uncertain`。

    `"…按日支付千分之一违约金，累计不超过合同总金额百分之二十。"` 里确实
    **有**一个总比例（20%），但本实现不区分 —— 整句按"按日计费"排除。

    为什么不修：要区分就得按逗号再切一次，而 `_SENTENCE_SPLIT` **刻意不切逗号**
    （切了会丢掉主语归属，把"乙方逾期交付的，…"算成双方的）。
    取舍是**宁可交人工，也不取一个可能错的数**：`uncertain` 让人来看，
    取错值则会让规则给出一个确定的、错的结论。

    这条测试的作用正是**把边界钉住**：将来若改成能识别上限，它会失败，
    那时才该改它 —— 而不是让"能做到"与"以为能做到"长期含糊着。
    """
    ratio = _fact(
        _doc("第三条 乙方逾期交付的，按日支付千分之一违约金，累计不超过合同总金额百分之二十。"),
        "liability_party_b_ratio",
    )

    assert ratio.status is FieldStatus.UNCERTAIN


# ============================================================
# 3. 产出契约
# ============================================================


def test_every_derived_field_always_gets_a_conclusion() -> None:
    """**每个派生字段都必须有一条结论**，哪怕 `not_found`。

    只产出"成功的那几个"时，缺项字段在下游与"未约定"无法区分 ——
    而那种缺陷不会抛异常，只会让规则拿到空输入。
    """
    result = resolve_derived_fields(_from_fixture(BASELINE))

    assert set(result) == set(DERIVED_RULE_FIELDS)
    for code, field in result.items():
        assert field.field_code == code


def test_no_document_is_uncertain_not_missing() -> None:
    """没有标准文档 → **读不出**，不是"合同没约定"。"""
    for code, field in resolve_derived_fields(None).items():
        assert field.status is FieldStatus.UNCERTAIN, code
        assert field.reason_code is ReasonCode.EXTRACTION_FAILED


def test_uncertain_fields_always_carry_a_reason_code() -> None:
    """`ExtractedField` 的校验器会拦，这里把它作为**显式**断言再钉一遍。"""
    for name in (PREPAY, MILESTONES):
        for code, field in resolve_derived_fields(_from_fixture(name)).items():
            if field.status is FieldStatus.UNCERTAIN:
                assert field.reason_code is not None, code


# ============================================================
# 4. 生产者守卫（白名单 ≠ 有人生产）
# ============================================================


def test_every_expr_field_has_a_runtime_producer() -> None:
    """⚠️ 评审点名的缺口：`check_rules.py` 只验字段在不在白名单，
    **没有验证真实链路能否产生字段**。

    白名单能拦住拼错（`prepay_ratios`），拦不住"字段合法却没人生产"：
    `prepay_ratio` / `liability_party_a_ratio` / `liability_party_b_ratio`
    曾经整整缺席，而 6 条 expr 规则在运行期永远拿不到输入 ——
    表现是 `is_null` 规则报"未约定"、阈值规则报"判不了"，
    **报告上完全看不出这是实现缺口**。

    `app/rules/fields.py` 在**导入时**就会拦住这件事，这里再断言一次：
    测试是给人看的契约，导入时的断言是给机器守的。
    """
    without_producer = sorted(code for code in ALL_EXPR_FIELDS if producer_of(code) is None)

    assert without_producer == []


def test_derived_fields_are_produced_by_the_fact_resolver() -> None:
    for code in DERIVED_RULE_FIELDS:
        assert producer_of(code) == "fact_resolver"


def test_the_real_chain_produces_every_expr_field() -> None:
    """**真实链路**必须能产出每一个 `expr` 字段 —— 评审点名要的就是这道检查。

    白名单与生产者表都是**静态**的（"登记过了"），而这里要的是**运行期证据**：
    真的跑一遍解析与事实解析，看字段有没有被生产出来。

    ⚠️ 静态登记与真实产出之间隔着一类查不出来的缺陷：字段登记了、
    生产者也没写错，但**解析器根本不产出这个字段**（连 `not_found` 都不给）。
    那时 `expr` 规则拿到的不是"合同未约定"，而是"这个键压根不存在" ——
    两者的处置完全不同（前者进业务结论，后者是**实现缺口**）。

    因此遍历**全部**夹具取并集：单看一份夹具会把"这份合同恰好没写"误判成缺口。
    """
    from app.services.field_extractor import FieldExtractor

    produced: set[str] = set()
    for path in sorted(FIXTURES.glob("*.pdf")):
        document = _from_fixture(path.name)
        extraction = FieldExtractor(document).extract()
        produced |= {field.field_code for field in extraction.basic_info.fields}
        produced |= {field.field_code for field in extraction.clause_info.fields}
        produced |= set(resolve_derived_fields(document))

    assert sorted(ALL_EXPR_FIELDS - produced) == []
