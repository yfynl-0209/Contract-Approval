"""字段与条款提取（设计文档 §4.8 修-8）—— **确定性**解析器 + 四态判定。

## 为什么必须是确定性的

本模块**不做任何猜测**：模式命中并解析成功 → `extracted`；命中但解析不了 →
`uncertain`；可靠检索过确实没有 → `not_found`。没有"大概是这样"这一档 ——
有的话它就会以 `extracted` 的身份进入规则评价，而**没有人看得出它是猜的**。

## `not_found` 与 `failed` 的分界（本项目最要紧的一处）

`not_found` 只在**可靠检索过、确实没有**时成立。所以本模块先算一个前置条件：
**这份文档有没有"我们没能读到的部分"？**

| 文档状态 | 未命中的字段 |
| --- | --- |
| 全部页面 `ok` / `blank` | `not_found` |
| 存在 `uncertain` 页 | **`uncertain`**（`EVIDENCE_UNCERTAIN`） |
| 存在 `failed` 页 | **`failed`**（`EXTRACTION_FAILED`） |

不分这三档的后果很具体：扫描件里有一页 OCR 失败，那一页上的"违约责任"读不到，
于是报"合同缺少违约责任条款" —— 而真相是**我们没能读到那一页**。
缺失类规则误报的根源就在这里，且报告看起来完全正常。

## 中文数字

合同正文里"三十日""四十五日"是常态（夹具里就是这样）。只认阿拉伯数字的话，
这些字段会全部落进 `not_found` —— **把"写成了中文数字"判成"没有约定"**。
因此这里做一个小范围的确定性转换（零~千），转换失败才判 `uncertain`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.enums import FieldStatus, ReasonCode
from app.ports.field_contract import (
    BasicInfoFieldSet,
    ClauseFieldSet,
    EvidenceSpan,
    ExtractedField,
)
from app.ports.parse_document import DocumentBlock, DocumentPage, StandardDocument
from app.rules.clauses import ALL_CLAUSE_TYPES, keywords_for
from app.rules.fields import DIRECT_FIELDS
from app.services.document_builder import block_at, resolve_span
from app.textnorm import chinese_number, fold_numeric

#: 阿拉伯数字或中文数字。中文部分限定在"零~千"，够覆盖合同里的常见写法
_NUM = r"(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十百千]+)"

#: 币种文字 → ISO 代码。合同里"人民币 1,200,000 元"比"CNY"更常见
_CURRENCY_WORDS: dict[str, str] = {
    "人民币": "CNY",
    "美元": "USD",
    "欧元": "EUR",
    "日元": "JPY",
    "港币": "HKD",
    "港元": "HKD",
}

# ⚠️ 中文数字表**不在这里** —— 已上移到 `app/textnorm.py`（`chinese_number`）。
#
# M5 的派生事实解析（`百分之六十` 这类比例）同样需要它，而 `app/rules/`
# **不能** import 本模块（`rules → services` 是反向依赖，且本模块已 import
# `app.rules`，会成环）。各写一份的话，两份只在**同时被改对时**才一致 ——
# 而它们分处两个模块、由不同时间的人维护。


@dataclass(frozen=True)
class ExtractionResult:
    """一次提取的全部结论。两个 `FieldSet` 共用 §4.8 的同一套结构。"""

    basic_info: FieldSet
    #: 条款结论（8 类）
    clause_info: FieldSet


@dataclass(frozen=True)
class _Match:
    page: DocumentPage
    block: DocumentBlock
    start: int
    end: int
    value: str


class FieldExtractor:
    """从标准文档里提取 8 项基本信息（`DIRECT_FIELDS`）与 8 类条款。"""

    def __init__(self, document: StandardDocument) -> None:
        self._document = document
        self._blocker = _searchability(document)

    # ============================================================
    # 入口
    # ============================================================

    def extract(self) -> ExtractionResult:
        # ⚠️ 用**两个专门的**根模型而不是裸 `FieldSet`：
        # 它们会校验字段码在白名单里、且覆盖需求规定的那几项 ——
        # 于是"白名单"与"覆盖度"这两条**在构造时**就成立，
        # 而不是靠某条测试记得去断言（那份 JSON 是长期存放的，
        # 缺项要到被消费时才暴露，那时离写入已经很远）。
        return ExtractionResult(
            basic_info=BasicInfoFieldSet(fields=tuple(self._basic_info())),
            clause_info=ClauseFieldSet(fields=tuple(self._clauses())),
        )

    # ============================================================
    # 基本信息
    # ============================================================

    def _basic_info(self) -> list[ExtractedField]:
        """逐个 **直接抽取字段** 产出结论。

        ⚠️ **派生字段**（`DERIVED_FIELDS`）不在这里产出：它们由其他字段算出，
        没有独立原文片段，而证据必须回指到参与计算的原始字段 ——
        那是 M5 的事（它才持有完整的字段值）。
        在这里硬造一个"派生值"，等于让一个没有证据的数字以 `extracted` 的身份
        进入规则评价。
        """
        results: list[ExtractedField] = []
        for field_code in DIRECT_FIELDS:
            results.append(self._extract_field(field_code))
        return results

    def _extract_field(self, field_code: str) -> ExtractedField:
        if field_code == "contract_title":
            # 标题没有"键"可以匹配 —— 它是**首行的整行文字**，走单独的判据。
            return self._extract_title()

        pattern = _FIELD_PATTERNS.get(field_code)
        match = _search(self._document, pattern) if pattern is not None else None

        if match is None:
            return self._miss(field_code)

        parsed = _parse_field(field_code, match.value)
        if parsed is None:
            # 命中了但解析不了：**不猜**，如实标 uncertain。
            # 猜的话它会以 extracted 的身份进入规则评价，而没人看得出它是猜的。
            return ExtractedField(
                field_code=field_code,
                value_text=match.value,
                status=FieldStatus.UNCERTAIN,
                evidence=(_evidence(match),),
                reason_code=ReasonCode.EVIDENCE_UNCERTAIN,
                reason_text=f"定位到疑似取值 {match.value!r}，但无法确定为该字段的值",
            )

        value_text, value_decimal, currency = parsed
        return ExtractedField(
            field_code=field_code,
            value_text=value_text,
            value_decimal=value_decimal,
            currency=currency,
            status=FieldStatus.EXTRACTED,
            evidence=(_evidence(match),),
        )

    def _extract_title(self) -> ExtractedField:
        """合同标题 = **首行的整行文字**。

        判据必须写死在这里、而不只是"取第一行"，因为第一行是**版式**概念：
        版式一变（页眉、印章、批注），第一行就不再是标题。
        因此额外要求它**不含键值分隔符** —— `合同编号：…` 这类字段行
        不可能是标题。不满足时判 `uncertain` 而不是硬当成标题：
        标题被填成一个编号，审查意见的抬头就是错的，而**没有任何报错**。
        """
        page = self._document.pages[0] if self._document.pages else None
        if page is None or not page.blocks:
            return self._miss("contract_title")

        block = page.blocks[0]
        text = block.text.strip()
        looks_like_field = "：" in text or ":" in text
        if not text or looks_like_field or len(text) > 40:
            return ExtractedField(
                field_code="contract_title",
                value_text=text,
                status=FieldStatus.UNCERTAIN,
                evidence=(),
                reason_code=ReasonCode.EVIDENCE_UNCERTAIN,
                reason_text=f"首行 {text!r} 不像合同标题（可能缺失，或版式与预期不同）",
            )

        return ExtractedField(
            field_code="contract_title",
            value_text=text,
            status=FieldStatus.EXTRACTED,
            evidence=(_block_evidence(page, block),),
        )

    def _miss(self, field_code: str) -> ExtractedField:
        """没找到 —— 但**能不能说"确实没有"要由检索覆盖度决定**。"""
        if self._blocker is not None:
            code, text = self._blocker
            return ExtractedField(
                field_code=field_code,
                status=(
                    FieldStatus.FAILED
                    if code is ReasonCode.EXTRACTION_FAILED
                    else FieldStatus.UNCERTAIN
                ),
                reason_code=code,
                reason_text=text,
            )
        return ExtractedField(
            field_code=field_code,
            status=FieldStatus.NOT_FOUND,
            reason_text="已对全文可靠检索，未出现该字段",
        )

    # ============================================================
    # 条款
    # ============================================================

    def _clauses(self) -> list[ExtractedField]:
        """8 类条款的存在性判定。"""
        results: list[ExtractedField] = []
        for clause_code in sorted(ALL_CLAUSE_TYPES):
            results.append(self._extract_clause(clause_code))
        return results

    def _extract_clause(self, clause_code: str) -> ExtractedField:
        for keyword in keywords_for(clause_code):
            match = _search_literal(self._document, keyword)
            if match is None:
                continue
            # 证据取**整个块**：条款的存在由"这一行写了它"来支撑，
            # 只框住关键词本身会给出一个过窄的框，人看不出上下文。
            return ExtractedField(
                field_code=clause_code,
                value_text=match.block.text,
                status=FieldStatus.EXTRACTED,
                evidence=(_evidence(match, whole_block=True),),
            )
        return self._miss(clause_code)


# ============================================================
# 检索覆盖度
# ============================================================


def _searchability(document: StandardDocument) -> tuple[ReasonCode, str] | None:
    """这份文档**能不能**支撑 `not_found` 结论？

    Returns:
        `None` 表示可以（全部页面都可靠读过）；
        否则返回 `(原因码, 说明)` —— 调用方据此把"未命中"降级为
        `failed` / `uncertain`，而不是报缺失。
    """
    failed = [page.page for page in document.pages if page.page_status.value == "failed"]
    if failed:
        return (
            ReasonCode.EXTRACTION_FAILED,
            f"第 {failed} 页未能读取，无法断言该字段不存在",
        )

    uncertain = [
        page.page for page in document.pages if page.page_status.value == "uncertain"
    ]
    if uncertain:
        return (
            ReasonCode.EVIDENCE_UNCERTAIN,
            f"第 {uncertain} 页识别结果不可靠，无法断言该字段不存在",
        )

    return None


# ============================================================
# 检索
# ============================================================


def _search(document: StandardDocument, pattern: re.Pattern[str]) -> _Match | None:
    for page in document.pages:
        for hit in pattern.finditer(page.text):
            block = block_at(page, hit.start(), hit.end())
            if block is None:
                # 命中跨块（跨行）：用一个块级 bbox 代表它会给出
                # **覆盖不到全部文字**的框，而它看起来完全正常。跳过。
                continue
            return _Match(
                page=page,
                block=block,
                start=hit.start(),
                end=hit.end(),
                value=hit.group(1).strip(),
            )
    return None


def _search_literal(document: StandardDocument, needle: str) -> _Match | None:
    for page in document.pages:
        start = page.text.find(needle)
        if start == -1:
            continue
        end = start + len(needle)
        block = block_at(page, start, end)
        if block is None:
            continue
        return _Match(page=page, block=block, start=start, end=end, value=needle)
    return None


def _evidence(match: _Match, *, whole_block: bool = False) -> EvidenceSpan:
    """构造证据。

    ⚠️ **几何与精度一律由 `resolve_span` 决定**，这里不再自己拼 ——
    初版在这里（以及 `document_builder.locate()` 里）**各写了一遍**，
    两处都把整块 bbox 当成字符级证据返回，于是出现
    "声明 `char`、画出整行"：M8 照 `char` 去画字符框，画出来是一整行，
    而两边都不报错。**复制的那一份一定会先偏离。**
    """
    start = match.block.char_start if whole_block else match.start
    end = match.block.char_end if whole_block else match.end
    resolved = resolve_span(match.page, start, end)
    if resolved is None:  # pragma: no cover - `_search` 已保证区间落在块内
        raise ValueError(f"证据区间 [{start}, {end}) 不在任何块内")
    return EvidenceSpan(
        page=match.page.page,
        block_id=resolved.block.block_id,
        text=match.block.text if whole_block else match.page.text[start:end],
        bbox=resolved.bbox,
        char_start=start,
        char_end=end,
        text_precision=resolved.text_precision,
        bbox_precision=resolved.bbox_precision,
    )


def _block_evidence(page: DocumentPage, block: DocumentBlock) -> EvidenceSpan:
    """整块证据（标题这类"整行就是结论"的字段用）。"""
    resolved = resolve_span(page, block.char_start, block.char_end)
    assert resolved is not None, "块的自身区间必然落在自己内部"
    return EvidenceSpan(
        page=page.page,
        block_id=block.block_id,
        text=block.text,
        bbox=resolved.bbox,
        char_start=block.char_start,
        char_end=block.char_end,
        text_precision=resolved.text_precision,
        bbox_precision=resolved.bbox_precision,
    )


# ============================================================
# 取值解析
# ============================================================


#: 整数型字段。它们刻意**也**写进 `value_decimal`：消费方（M5 的阈值规则）
#: 只认这一个数值槽位，分两套读法会让"金额能比大小、天数不能"成为隐性约束。
_NUMERIC_FIELDS = frozenset(
    {
        "pay_days",
        "acceptance_days",
        "renew_term_months",
        "renew_notice_days",
        "confidentiality_years",
    }
)


def _parse_field(
    field_code: str, raw: str
) -> tuple[str, str | None, str | None] | None:
    """把命中的原文片段解析成 `(value_text, value_decimal, currency)`。

    Returns:
        `None` 表示**解析不确定** —— 调用方据此判 `uncertain`，**不猜**。
    """
    if field_code == "amount":
        return _parse_amount(raw)

    if field_code == "currency":
        # ⚠️ 必须**先**于数值解析：币种原文是 `CNY` 这种字母串，
        # 走 `_to_number` 会解析失败，于是每个币种字段都被判成 uncertain。
        code = _currency_of(raw)
        return None if code is None else (raw, None, code)

    if field_code in _NUMERIC_FIELDS:
        number = _to_number(raw)
        if number is None:
            return None
        return (str(number), str(number), None)

    return (raw, None, None)


def _fold_numeric(raw: str) -> str:
    """把 **NFKC** 折叠后的文本用于**数值解析**（不用于页文本）。

    ⚠️ **实测依据**（这处缺陷是被配对夹具测试抓出来的）：扫描件里的
    `800,000.00` 被 OCR 读成：

    ```text
    人民币800，000．0０元
           ↑ U+FF0C 全角逗号   ↑ U+FF0E 全角句点   ↑ U+FF10 全角零
    ```

    而当时的正则只认 ASCII 的 `[\\d,]`，匹配到 `800` 就停了 ——
    **金额静默变成 800**（差 1000 倍），接着被拿去和阈值比较，结论直接反转，
    而日志、证据、界面全都"看起来正常"。

    ⚠️ **只能用在捕获到的值上，不能用在页文本上**：NFKC 会**改变字符串长度**
    （`㍿` 会展开成 4 个字符），把它施加到整篇文本上会让所有
    `char_start/char_end` 与 `bbox` 一起错位。捕获值的偏移不参与任何坐标计算，
    因此在这里折叠是安全的。

    实现已收进 `app.textnorm`：M5 的 LLM 证据核验要用**同一个**折叠，
    两份实现只在同时被改对时才一致（M4 已经因为"同一个判断各写一遍"栽过一次）。
    """
    return fold_numeric(raw)


def _parse_amount(raw: str) -> tuple[str, str | None, str | None] | None:
    """"人民币 1,200,000.00 元" → `("1200000.00", "CNY")`。

    金额走 `Decimal` 而不是 `float`：`float("1200000.00")` 在更大或更小的数上
    会出现精度损失，而**金额上不可接受**。

    `value_text` 保留**原文**（含全角形式）—— 那是页面上真实印着的东西，
    人要拿它去核对；`value_decimal` 是折叠后的十进制数值。两者分工不可混。
    """
    digits = re.search(r"([\d,]+(?:\.\d+)?)", _fold_numeric(raw))
    if digits is None:
        return None
    try:
        amount = Decimal(digits.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    if amount <= 0:
        # 0 元或负数不是"合同总金额"的合理取值 —— 报解析不出，而不是照抄。
        return None
    return (raw, format(amount, "f"), _currency_of(raw))


def _currency_of(raw: str) -> str | None:
    for word, code in _CURRENCY_WORDS.items():
        if word in raw:
            return code
    found = re.search(r"\b([A-Z]{3})\b", raw)
    return found.group(1) if found else None


def _to_number(raw: str) -> int | None:
    """阿拉伯数字（含**全角**）或中文数字 → 整数。

    ⚠️ 同样要先折叠：OCR 会把 `10` 读成全角 `１０`（U+FF10…），
    不折叠的话这类字段会落到 `uncertain` —— 而它其实完全读对了。
    """
    folded = _fold_numeric(raw)
    digits = re.fullmatch(r"\d+", folded)
    if digits is not None:
        return int(folded)
    return _chinese_int(folded)


def _chinese_int(text: str) -> int | None:
    """中文数字 → 整数，范围零~千。**解析不了就返回 `None`（不猜）**。

    ⚠️ 实现已上移到 `app/textnorm.py`（`chinese_number`）—— M5 的派生事实解析
    （`百分之六十` 这类比例）同样需要它，而 `app/rules/` **不能** import 本模块
    （那是反向依赖，且本模块已 import `app.rules`，会成环）。
    两处各写一份的话，两份只在**同时被改对时**才一致。

    保留这个同名薄封装，是因为本模块内的调用点与既有测试都按这个名字用；
    它**不是另一套规则**，行为与 `chinese_number` 完全一致。
    """
    return chinese_number(text)


# ============================================================
# 字段模式表
# ============================================================
# 每个模式**必须且只能有一个捕获组**，它就是取值本身。
# 模式写得过窄会把存在的字段判成缺失（误报），过宽会把无关文字当成取值 ——
# 两者都会让规则拿到错误输入，因此每一条都对着真实夹具的写法写。

_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "party_a": re.compile(r"甲方[（(][^）)]*[）)][：:]\s*(\S+?)(?=\s|$)"),
    "party_b": re.compile(r"乙方[（(][^）)]*[）)][：:]\s*(\S+?)(?=\s|$)"),
    "contract_number": re.compile(r"合同编号[：:]\s*(\S+)"),
    # 日期统一按 ISO 取；写成"2026年8月1日"的合同**取不到**，
    # 那时会落到 not_found —— 而它是真的存在于正文里的。
    # 这是**已知范围**：先把最常见的写法做对，日期格式的归一化留到需要时再补
    # （补的时候必须同时改这里与 `_parse_field`，否则值取了但没归一化）。
    "effective_date": re.compile(r"生效(?:日期|时间)[：:]\s*(\d{4}-\d{2}-\d{2})"),
    "expiry_date": re.compile(r"(?:到期|有效期至)(?:日期|时间)?[：:]\s*(\d{4}-\d{2}-\d{2})"),
    "credit_code": re.compile(r"统一社会信用代码[：:]\s*([0-9A-Z]{18})"),
    # 取值是**整段**（含币种文字），由 `_parse_amount` 再解析
    "amount": re.compile(r"合同总金额[：:]\s*([^\n]+)"),
    "currency": re.compile(r"结算币种[：:]\s*([A-Za-z]{3})"),
    # "验收合格后三十日内支付" / "十个工作日内支付"
    "pay_days": re.compile(rf"({_NUM})\s*(?:个)?(?:工作)?日内支付"),
    # "收到货物后十个工作日内完成验收"
    "acceptance_days": re.compile(rf"({_NUM})\s*(?:个)?(?:工作)?日内完成验收"),
    "renew_term_months": re.compile(rf"自动续(?:约|期)\s*({_NUM})\s*个月"),
    "renew_notice_days": re.compile(rf"提前\s*({_NUM})\s*日[^。\n]{{0,12}}异议"),
    "confidentiality_years": re.compile(rf"保密(?:期|义务)[^。\n]{{0,16}}?({_NUM})\s*年"),
}


__all__ = ["ExtractionResult", "FieldExtractor"]
