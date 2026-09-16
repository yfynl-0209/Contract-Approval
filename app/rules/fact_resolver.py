"""派生事实解析（M5 / T3a）—— `expr` 规则所需"量"的生产者。

## 为什么必须有它

`expr` 规则用**字符串**字段名引用解析结果，而 M4 的解析模块**只抽取直接字段**。
18 条 expr 规则里有 **6 条**读的是派生字段：

```text
prepay_ratio              → PAY_PREPAY_RATIO_HIGH_FOR_BUYER / _EXTREME / _LOW_FOR_SELLER / PAY_PREPAY_MISSING
liability_party_a_ratio   → LIAB_RATIO_HIGH_FOR_PARTY_A
liability_party_b_ratio   → LIAB_RATIO_HIGH_FOR_PARTY_B
```

没有生产者时，这 6 条规则在运行期**永远**拿不到输入，而表现是：

- `is_null` 类 → 报"未约定预付款"（**把实现缺口说成业务结论**）；
- 阈值类 → `not_found` → `needs_review`。

两者都**不报错**，报告上也看不出这是实现缺口。`app/rules/fields.py` 的白名单
能拦住拼错（`prepay_ratios`），但拦不住"字段合法却没人生产" —— 这是白名单的**能力边界**，
不是它的缺陷，所以需要另一道检查（`check_rules.py` 的生产者校验）。

## ⚠️ 上下文是判据的核心，不是可选的过滤

夹具里就有一句**陷阱**：

```text
"合同生效后支付百分之三十，验收合格后支付百分之六十，质保期满后支付百分之十。"   （HT-2026-0003 第三条）
```

这是**付款进度**，不是预付款。不限定上下文的实现会读出三个比例，其中 `0.6` 恰好
满足 `PAY_PREPAY_RATIO_HIGH_FOR_BUYER (prepay_ratio > 0.3)` —— 于是报出一条
**"预付款比例 60%"的高风险**，而这份合同的预付款是"未约定"。

因此：**比例必须与「预付」出现在同一句**。同一份夹具里还有一句更隐蔽的：

```text
"乙方应于收到预付款后四十五日内完成交付。"      （HT-2026-0002 第二条）
```

它含「预付」但**不含比例** —— 若把"含关键词"直接当作"存在比例条款"，
这条会被判成"有预付款约定但读不出比例"。所以判据是
**「预付」+ 比例或金额标记**（`_RATIO_MARKERS` / `_AMOUNT_MARKERS`）。

## 三档结论，**不猜**

| 情形 | `status` | 下游表现 |
| --- | --- | --- |
| 唯一确定的比例 | `extracted` | 阈值规则正常比较 |
| 多个**互相矛盾**的比例 | `uncertain` | `needs_review`（决策 ④） |
| 有条款、但读不出比例 | `uncertain` | `needs_review` |
| 通篇没有该条款 | `not_found` | 缺失类规则**才**能命中 |

⚠️ 第二、三行必须与第四行分开：把它们合并成 `not_found`，
`PAY_PREPAY_MISSING` 就会把"我们没读出来"报成**"合同没约定预付款"**。

## 按日计费不是总比例

```text
"乙方逾期交付的，每逾期一日按合同总金额的千分之一支付违约金。"   （HT-2026-0002 第三条）
```

`千分之一` 是**日费率**，不是"乙方违约金比例"。直接取用会让
`liability_party_b_ratio = 0.001` —— 一个**语义错了但看起来完全正常**的数
（`0.001` 是个合法比例，且会让 `> 0.3` 规则报 `not_hit`）。
这里把它记为**读不出总比例**（`uncertain`），并在 `reason_text` 里写明原因。

## 与 `app/rules/evidence.py` 的分工

本模块只负责**造出字段**（含证据与来源）。命中证据的落库形状是 T6 的事 ——
两者都产出 `EvidenceSpan`（`app/ports/field_contract.py`），
因此 M8 画框对"直接字段"与"派生字段"用的是同一套坐标。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from app.enums import FieldStatus, ReasonCode
from app.ports.field_contract import EvidenceSpan, ExtractedField
from app.ports.parse_document import DocumentPage, StandardDocument
from app.rules.fields import DERIVED_RULE_FIELDS
from app.services.document_builder import resolve_span
from app.textnorm import chinese_number, fold_numeric

# ------------------------------------------------------------
# 上下文标记
# ------------------------------------------------------------

#: 预付款的上下文关键词。命中它**才**可能产出 `prepay_ratio`。
PREPAY_KEYWORD: Final[str] = "预付"
#: 违约金的上下文关键词。
LIABILITY_KEYWORD: Final[str] = "违约金"

#: 「指向双方」的措辞：这些说法下**两边都承担**该比例。
#: `违约方` 也算 —— "违约方应支付 10%" 对甲乙双方都是同一条约定。
#:
#: ⚠️ **`对方` 不在这个列表里**。"甲方向对方支付"里的「对方」是**收款方**，
#: 承担义务的是**付款方**（甲方）。把它算成"双方"，会让
#: "甲方违约的，应向乙方支付…" 变成"甲乙各 30%" —— 乙方凭空多出一条约定，
#: 而且那条约定的证据指向一句根本没提乙方义务的句子。
#: 谁承担义务由下面的 `_OBLIGOR_PATTERNS` 判。
_BOTH_PARTY_MARKERS: Final[tuple[str, ...]] = ("任何一方", "双方", "违约方")
_PARTY_A_MARKER: Final[str] = "甲方"
_PARTY_B_MARKER: Final[str] = "乙方"

#: **谁是承担义务的那一方**。两个模式都不匹配时才退到"看提到了谁"。
#: 顺序有意义：先判"谁违约"，再判"谁付款"。
_OBLIGOR_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # 「甲方违约的」「乙方逾期交付的」—— 谁违约，谁承担
    re.compile(r"(甲方|乙方)[^，。；]{0,10}?(?:违约|逾期|迟延|延迟|违反|不履行)"),
    # 「甲方向乙方支付…违约金」—— 承担义务的是**付款方**
    re.compile(r"(甲方|乙方)[^，。；]{0,10}?(?:向|给)(?:甲方|乙方)[^，。；]{0,8}?支付"),
)

#: 比例写法。中文与阿拉伯数字**都要认**：OCR 与录入习惯都会混用。
#: 每一项是 `(模式, 除数, 是否为阿拉伯数字)` —— 第三项决定数字部分怎么读，
#: 把它写在表里而不是在循环里 `if` 判断，是为了加新写法时**只改一行**。
_RATIO_PATTERNS: Final[tuple[tuple[re.Pattern[str], Decimal, bool], ...]] = (
    (re.compile(r"百分之([零一二三四五六七八九十百千两]+)"), Decimal(100), False),
    (re.compile(r"千分之([零一二三四五六七八九十百千两]+)"), Decimal(1000), False),
    (re.compile(r"万分之([零一二三四五六七八九十百千两]+)"), Decimal(10000), False),
    (re.compile(r"(\d+(?:\.\d+)?)\s*[%％]"), Decimal(100), True),
)

#: 「这一句在定义比例」的标记。含「元/金额」也算 ——
#: 写了金额却没有比例时，结论必须是"读不出"（`uncertain`）而**不是**"没有约定"。
_RATIO_MARKERS: Final[tuple[str, ...]] = ("百分之", "千分之", "万分之", "%", "％")
_AMOUNT_MARKERS: Final[tuple[str, ...]] = ("元", "金额")

#: **按日/按月**计费的措辞。这类比例是费率，不是总比例。
_PER_UNIT_MARKERS: Final[tuple[str, ...]] = (
    "每逾期",
    "每迟延",
    "每延迟",
    "每日",
    "按日",
    "每一天",
    "按月",
)

#: 每页的句子切分点。⚠️ **不切逗号** ——
#: "乙方逾期交付的，每逾期一日按…千分之一支付违约金" 里，主语在逗号**之前**，
#: 切了逗号就丢掉归属，把乙方条款算成双方的。
_SENTENCE_SPLIT: Final[re.Pattern[str]] = re.compile(r"(?<=[。；;\n])")

#: 单个字段最多保留几处证据。
MAX_EVIDENCE_SPANS: Final[int] = 3


# ============================================================
# 句子与证据
# ============================================================


@dataclass(frozen=True)
class _Sentence:
    """页内一个句子，带**页内字符区间**（证据要靠它反推 bbox）。"""

    page: DocumentPage
    start: int
    end: int

    @property
    def text(self) -> str:
        return self.page.text[self.start : self.end]


def _sentences(document: StandardDocument) -> list[_Sentence]:
    """按句切分全文。区间是**页内**偏移，与 `DocumentBlock` 同一坐标系。"""
    out: list[_Sentence] = []
    for page in document.pages:
        cursor = 0
        for chunk in _SENTENCE_SPLIT.split(page.text):
            if chunk:
                # ⚠️ 去掉**尾部空白**再定区间：句末的 "\n" 落在块外，
                # 带上它会让 `block_at` 找不到所属块 → 白丢一个证据。
                trimmed = len(chunk.rstrip())
                if trimmed:
                    out.append(_Sentence(page, cursor, cursor + trimmed))
                cursor += len(chunk)
    return out


def _span_of(sentence: _Sentence) -> EvidenceSpan | None:
    """句子的证据。**复用 M4 的 `resolve_span`** —— 不自己算 bbox。

    跨块（`block_at` 返回 `None`）时返回 `None`：那说明这句话横跨两行，
    给它一个块级 bbox 会得到**一个覆盖不到全部文字**的框，
    而它看起来完全正常。宁可少一个证据，也不给一个错的。
    """
    resolved = resolve_span(sentence.page, sentence.start, sentence.end)
    if resolved is None:
        return None
    return EvidenceSpan(
        page=sentence.page.page,
        block_id=resolved.block.block_id,
        text=sentence.text,
        bbox=resolved.bbox,
        char_start=sentence.start,
        char_end=sentence.end,
        text_precision=resolved.text_precision,
        bbox_precision=resolved.bbox_precision,
    )


# ============================================================
# 比例解析
# ============================================================


def _ratios_in(text: str) -> list[tuple[Decimal, str]]:
    """一句里出现的全部比例，返回 `(值, 原文写法)`。

    ⚠️ 先做 NFKC 折叠：OCR 会把 `60%` 读成全角 `６０％`，
    而 `%` 与 `％` 在折叠后是同一个字符 —— 两套写法各写一条正则，
    只会在下一次遇到另一种全角组合时静默漏掉。
    """
    folded = fold_numeric(text)
    found: list[tuple[Decimal, str]] = []

    for pattern, scale, arabic in _RATIO_PATTERNS:
        for match in pattern.finditer(folded):
            digits = match.group(1)
            number = (
                _decimal_of(digits) if arabic else _decimal_of(chinese_number(digits))
            )
            if number is None:
                # 认得出写法却读不出数（如 `百分之若干`）→ 跳过这一处。
                # ⚠️ 不能就此判"没有约定"：`clause_seen` 已为真，
                # 结论会落在 `uncertain`，见 `_conclude`。
                continue
            found.append((number / scale, match.group(0)))

    return found


def _decimal_of(value: int | str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _looks_like_a_ratio_clause(text: str) -> bool:
    return any(marker in text for marker in _RATIO_MARKERS) or any(
        marker in text for marker in _AMOUNT_MARKERS
    )


def _is_per_unit(text: str) -> bool:
    return any(marker in text for marker in _PER_UNIT_MARKERS)


def _party_attribution(text: str) -> tuple[bool, bool]:
    """这句话的比例归谁：`(指向甲方, 指向乙方)`。

    三层，按顺序判：

    1. `任何一方` / `双方` / `违约方` → **两边都算**；
    2. 由 `_OBLIGOR_PATTERNS` 判**谁承担义务** —— 这一步不能省：
       "甲方违约的，应向**乙方**支付…30% 的违约金" 里，乙方是**收款方**，
       按"提到谁就算谁"会得出"甲乙各 30%"，于是乙方凭空多出一条约定，
       而那条约定的证据指向一句根本没提乙方义务的句子；
    3. 兜底：只提到一方就归它；提到双方、或都没提到 → 按**双方**记
       （一条不区分主体的违约金条款，对两边是同一个约定）。
    """
    if any(marker in text for marker in _BOTH_PARTY_MARKERS):
        return True, True

    for pattern in _OBLIGOR_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            who = match.group(1)
            return who == _PARTY_A_MARKER, who == _PARTY_B_MARKER

    has_a = _PARTY_A_MARKER in text
    has_b = _PARTY_B_MARKER in text
    if has_a != has_b:
        return has_a, has_b
    return True, True


# ============================================================
# 结论
# ============================================================


@dataclass(frozen=True)
class _Occurrence:
    value: Decimal
    raw: str
    sentence: _Sentence


def _conclude(
    field_code: str,
    occurrences: list[_Occurrence],
    *,
    clause_seen: bool,
    label: str,
) -> ExtractedField:
    """把一处/多处命中收成字段结论。"""
    by_value: dict[str, _Occurrence] = {}
    for item in occurrences:
        by_value.setdefault(str(item.value), item)

    if len(by_value) == 1:
        item = next(iter(by_value.values()))
        same = [o for o in occurrences if str(o.value) == str(item.value)]
        return ExtractedField(
            field_code=field_code,
            value_text=item.raw,
            value_decimal=str(item.value),
            status=FieldStatus.EXTRACTED,
            evidence=_spans_of(same),
            reason_text=_source_text(item, label),
        )

    if len(by_value) > 1:
        values = "、".join(
            f"{key}（{item.raw}，第 {item.sentence.page.page} 页）"
            for key, item in by_value.items()
        )
        return ExtractedField(
            field_code=field_code,
            status=FieldStatus.UNCERTAIN,
            reason_code=ReasonCode.EVIDENCE_UNCERTAIN,
            reason_text=(
                f"正文出现 {len(by_value)} 个互相矛盾的{label}：{values} —— "
                "取任何一个都是猜，须人工确认"
            ),
            # 互相矛盾的证据**全部**留下：审批人要同时看到两处才能判断谁对
            evidence=_spans_of(list(by_value.values())),
        )

    if clause_seen:
        return ExtractedField(
            field_code=field_code,
            status=FieldStatus.UNCERTAIN,
            reason_code=ReasonCode.EVIDENCE_UNCERTAIN,
            reason_text=(
                f"正文有{label}相关约定，但未能解析出比例（可能是按日/按月计费的费率，"
                "或写法不在支持范围内）—— 不得据此判『未约定』"
            ),
        )

    return ExtractedField(
        field_code=field_code,
        status=FieldStatus.NOT_FOUND,
        reason_text=f"全文未出现{label}的约定",
    )


def _spans_of(occurrences: list[_Occurrence]) -> tuple[EvidenceSpan, ...]:
    spans: list[EvidenceSpan] = []
    seen: set[tuple[int, int, int]] = set()
    for item in occurrences:
        key = (item.sentence.page.page, item.sentence.start, item.sentence.end)
        if key in seen:
            continue
        seen.add(key)
        span = _span_of(item.sentence)
        if span is not None:
            spans.append(span)
        if len(spans) >= MAX_EVIDENCE_SPANS:
            break
    return tuple(spans)


def _source_text(item: _Occurrence, label: str) -> str:
    """来源追踪：值、原文写法、页码、句子。"""
    sentence = item.sentence.text.strip()
    if len(sentence) > 48:
        sentence = sentence[:48] + "…"
    return f"{label} {item.value} 来自第 {item.sentence.page.page} 页：{sentence}"


def _undecidable(field_code: str, code: ReasonCode, text: str) -> ExtractedField:
    return ExtractedField(
        field_code=field_code,
        status=FieldStatus.UNCERTAIN,
        reason_code=code,
        reason_text=text,
    )


# ============================================================
# 三个派生字段
# ============================================================


def resolve_derived_fields(
    document: StandardDocument | None,
) -> dict[str, ExtractedField]:
    """产出全部派生字段。**每个字段都会有一条结论**（哪怕 `not_found`）。

    ⚠️ **不允许只产出"成功的那几个"**：缺项的字段在下游与"未约定"无法区分
    （见模块 docstring）。因此本函数返回的字典**恒定包含** `DERIVED_RULE_FIELDS`
    里的每一个字段码，`tests/test_rule_fact_resolver.py` 有断言守着它。

    Args:
        document: 批次对应的标准文档。`None` 表示解析产物不可用 ——
            此时三个字段都如实给 `uncertain`，而不是 `not_found`
            （"没读到"不等于"合同没约定"）。
    """
    if document is None:
        return {
            code: _undecidable(
                code,
                ReasonCode.EXTRACTION_FAILED,
                "解析产物不可用（没有标准文档），无法解析派生事实",
            )
            for code in DERIVED_RULE_FIELDS
        }

    sentences = _sentences(document)
    result = {"prepay_ratio": _resolve_prepay(sentences)}
    party_a, party_b = _resolve_liability(sentences)
    result["liability_party_a_ratio"] = party_a
    result["liability_party_b_ratio"] = party_b
    return result


def _resolve_prepay(sentences: list[_Sentence]) -> ExtractedField:
    occurrences: list[_Occurrence] = []
    clause_seen = False

    for sentence in sentences:
        if PREPAY_KEYWORD not in sentence.text:
            continue
        # 「预付」+ 比例/金额标记才算"在定义预付款比例"。
        # 只含「预付」的句子（"收到预付款后四十五日内完成交付"）不构成条款。
        if not _looks_like_a_ratio_clause(sentence.text):
            continue
        clause_seen = True
        for value, raw in _ratios_in(sentence.text):
            occurrences.append(_Occurrence(value, raw, sentence))

    return _conclude(
        "prepay_ratio", occurrences, clause_seen=clause_seen, label="预付款比例"
    )


def _resolve_liability(
    sentences: list[_Sentence],
) -> tuple[ExtractedField, ExtractedField]:
    a_occurrences: list[_Occurrence] = []
    b_occurrences: list[_Occurrence] = []
    a_clause = b_clause = False

    for sentence in sentences:
        if LIABILITY_KEYWORD not in sentence.text:
            continue
        to_a, to_b = _party_attribution(sentence.text)
        if not (to_a or to_b):
            continue
        if not _looks_like_a_ratio_clause(sentence.text):
            continue

        if to_a:
            a_clause = True
        if to_b:
            b_clause = True

        # ⚠️ 按日/按月计费的是**费率**，不是总比例 → 不计入取值，
        # 但 `clause_seen` 保持为真：结论是"读不出总比例"，不是"没有违约金条款"。
        if _is_per_unit(sentence.text):
            continue

        for value, raw in _ratios_in(sentence.text):
            if to_a:
                a_occurrences.append(_Occurrence(value, raw, sentence))
            if to_b:
                b_occurrences.append(_Occurrence(value, raw, sentence))

    return (
        _conclude(
            "liability_party_a_ratio",
            a_occurrences,
            clause_seen=a_clause,
            label="甲方违约金比例",
        ),
        _conclude(
            "liability_party_b_ratio",
            b_occurrences,
            clause_seen=b_clause,
            label="乙方违约金比例",
        ),
    )
