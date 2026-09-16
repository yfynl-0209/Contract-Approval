"""确定性条件匹配 —— 规则评价流水线的第 ③ 步。

三种模式（`keyword` / `regex` / `expr`）都返回同一个 `MatchResult`，
因此下游（evaluator）不必为每种模式各写一套分支 —— **分支越多，漏掉一个分支的机会越多**。

本模块是**纯函数**：不碰数据库、不读配置、不记日志。输入是规则配置 + 文本/字段，输出是结论。

## 三态而不是布尔

`MatchResult` 有三档：`MATCHED` / `NOT_MATCHED` / **`UNDECIDABLE`**。

⚠️ 合并成布尔是本模块最容易犯、且**错了不报错**的一处：

```text
预付款比例 > 30% 这条规则，遇到"合同金额没解析出来"
  → 判 False（"条件不成立"）→ 结论 not_hit → 输出"这条规则没问题"
  → 而事实是**根本没能判**
```

反过来：**缺失类**规则遇到 `not_found` **恰恰就是命中**。
同一个字段状态在两类规则下含义相反，因此"判不了"必须能表达出来，
而不能压进布尔值里。

## 数值比较一律走 `Decimal`，且阈值按 `str()` 折

`review_rules.match_text` 是 **JSON**，所以阈值 `0.1` 落到 Python 时是 `float` ——
也就是二进制的 `0.1000000000000000055511151231257827`。

⚠️ 这**不是理论问题**，库里就有这样一个规则：

```json
{"field": "prepay_ratio", "op": "lt", "value": 0.1}      -- PAY_PREPAY_RATIO_LOW_FOR_SELLER
```

若直接 `Decimal(0.1)` 去比一个**恰好等于 1/10** 的比例（派生字段由十进制算出），
`Decimal("0.1") < 0.10000000000000000551…` 得到 **True** ——
于是"预付款比例正好是 10%"被报成"低于 10%"，**差一个 epsilon，静默误报**。

因此阈值必须经 `Decimal(str(value))` 还原成**写规则的人本来想写的那个数**：
`str(0.1) == "0.1"`（Python 的 repr 是最短往返表示）→ `Decimal("0.1")` ✅。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

from app.enums import FieldStatus, ReasonCode
from app.ports.field_contract import ExtractedField
from app.rules.fields import MONEY_FIELDS
from app.schemas import ExprMatchConfig, KeywordMatchConfig, RegexMatchConfig

#: 存在性判断：`is_null` / `not_null` —— 它们按**字段状态**判，不比较数值
EXISTENCE_OPS: Final[frozenset[str]] = frozenset({"is_null", "not_null"})

#: 数值类比较（其余非存在性操作符是文本类，如 `contains`）
_NUMERIC_OPS: Final[frozenset[str]] = frozenset({"gt", "gte", "lt", "lte", "eq"})


class MatchVerdict(StrEnum):
    """条件匹配的三种结果。**不是布尔** —— 见模块 docstring。"""

    MATCHED = "matched"
    NOT_MATCHED = "not_matched"
    #: **判不了**：证据不足、模型不可用、币种不可比、阈值没配好……
    #: 它的正确处理是 `needs_review`，**不是** `not_hit`
    UNDECIDABLE = "undecidable"


@dataclass(frozen=True)
class MatchResult:
    """一次条件匹配的结果。

    Attributes:
        verdict: 三态结论。
        reason_code: `UNDECIDABLE` 时**必须**有（否则下游无法判 `needs_review` 的原因）。
        reason_text: 给审批人的中文解释。
        located_text: 命中的原文片段（供证据定位）。`None` 表示"命中了但无独立片段"
            （如 `expr` 的比较结论）。
        detail: 进 `rule_hits.hit_detail_json` 的计算过程。
            ⚠️ 数值一律用**字符串**（`Decimal` 不能直接进 JSON）；
            用 float 会把这个记账字段也拖回二进制误差里。
    """

    verdict: MatchVerdict
    reason_code: ReasonCode | None = None
    reason_text: str | None = None
    located_text: str | None = None
    detail: dict[str, Any] = dataclass_field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return self.verdict is MatchVerdict.MATCHED

    @property
    def decidable(self) -> bool:
        return self.verdict is not MatchVerdict.UNDECIDABLE


# ============================================================
# keyword
# ============================================================


def parse_exclude_text(raw: str | None) -> tuple[str, ...]:
    """`review_rules.exclude_text` 是**逗号分隔**的中文短语（实测：`不自动,不得自动,不再自动`）。

    放在本模块而不是解析侧：它的**唯一**用途就是 keyword 匹配，
    解析规则与消费它的地方待在一起，改一处不会漏另一处。
    """
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def match_keyword(
    config: KeywordMatchConfig,
    *,
    text: str | None,
    exclude_phrases: tuple[str, ...] = (),
) -> MatchResult:
    """关键词匹配。

    Args:
        text: 待匹配的文本。**粒度由调用方决定**：整篇正文，或某一个文本块。
            传 `None` 表示正文不可用（解析失败 / 未过质量门禁）。
            ⚠️ 否定词的判断跟着这个粒度走：整篇正文会让"别处出现否定词"也压掉命中，
            按块传则精确到该块 —— 两种都合理，但**必须由调用方想清楚**，
            而不是在这里用一个窗口启发式去猜。
        exclude_phrases: 否定词表；**仅对 `absent=False` 生效**（见下）。

    `absent=True` 是**缺失类**规则："全文均未出现 → 命中"。
    它与否定词表**互斥**：对缺失类，"出现且未被否定"才是"不缺失"，
    那是另一个规则，不该用同一个字段表达 —— 因此这里直接忽略否定词，
    并由配置校验（`scripts/check_rules.py`）保证两者不同时出现。
    """
    if text is None:
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            ReasonCode.EVIDENCE_UNCERTAIN,
            "合同正文不可用，无法判断该规则是否命中",
        )

    hits = [keyword for keyword in config.keywords if keyword in text]

    if config.absent:
        if hits:
            return MatchResult(
                MatchVerdict.NOT_MATCHED,
                ReasonCode.CONDITION_NOT_MATCHED,
                f"正文已出现 {hits[0]!r}，该条款并不缺失",
                located_text=hits[0],
                detail={"absent": True, "found": hits},
            )
        return MatchResult(
            MatchVerdict.MATCHED,
            ReasonCode.CONDITION_MATCHED,
            "全文均未出现相关表述",
            detail={"absent": True, "checked": list(config.keywords)},
        )

    if not hits:
        return MatchResult(
            MatchVerdict.NOT_MATCHED,
            ReasonCode.CONDITION_NOT_MATCHED,
            "未出现相关表述",
            detail={"found": []},
        )

    # 否定词：命中关键词、但正文明确排除了该情形 → **不命中**。
    # 实测这两条规则正是为此存在（"自动续约" vs "不自动续约"）。
    excluded = [phrase for phrase in exclude_phrases if phrase in text]
    if excluded:
        return MatchResult(
            MatchVerdict.NOT_MATCHED,
            ReasonCode.CONDITION_NOT_MATCHED,
            f"正文出现否定表述 {excluded[0]!r}，不构成该风险",
            located_text=excluded[0],
            detail={"found": hits, "excluded_by": excluded},
        )

    return MatchResult(
        MatchVerdict.MATCHED,
        ReasonCode.CONDITION_MATCHED,
        f"正文出现 {hits[0]!r}",
        located_text=hits[0],
        detail={"found": hits},
    )


# ============================================================
# regex
# ============================================================


def match_regex(config: RegexMatchConfig, *, text: str | None) -> MatchResult:
    """正则匹配（`re.search` —— 配置写的是**片段**，不是整串）。

    `regex` 在 40 条规则里**一条都没用**，但仍必须实现：
    配置能力已经承诺给用户，留一个"能填、运行时没人执行"的分支，
    比不支持更糟 —— M3 的 `DownloadStatus.FAILED` 就是这种形态（存在却从未被写入）。
    """
    if text is None:
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            ReasonCode.EVIDENCE_UNCERTAIN,
            "合同正文不可用，无法执行正则匹配",
        )

    # 配置在构造期已编译校验过（`RegexMatchConfig._must_compile`），这里不会抛。
    match = re.search(config.pattern, text)
    if match is None:
        return MatchResult(
            MatchVerdict.NOT_MATCHED,
            ReasonCode.CONDITION_NOT_MATCHED,
            "未匹配到该模式",
            detail={"pattern": config.pattern},
        )

    return MatchResult(
        MatchVerdict.MATCHED,
        ReasonCode.CONDITION_MATCHED,
        "匹配到该模式",
        located_text=match.group(0),
        detail={"pattern": config.pattern, "matched": match.group(0)},
    )


# ============================================================
# expr
# ============================================================


def match_expr(
    config: ExprMatchConfig,
    *,
    field: ExtractedField | None,
    default_currency: str,
) -> MatchResult:
    """字段比较（存在性 / 数值 / 文本包含）。

    Args:
        field: 该字段的解析结果。`None` 表示**解析结果里没有这个字段**。
        default_currency: 本项目运行币种。金额类字段的比较只有在该币种下才有意义
            —— 由调用方传入（单一来源），不在本模块里写死。

    ## 判定顺序（不可调换）

    1. **存在性操作符**（`is_null` / `not_null`）：**字段状态本身就是答案** ——
       `extracted` = 有，`not_found` = 没有，`uncertain` / `failed` = **判不了**；
    2. 其余操作符：字段必须 `extracted`。`not_found` → **判不了**（决策 ④），
       `uncertain` / `failed` → **判不了**；
    3. 金额类字段先过**币种检查**，再比较数值。

    ⚠️ 第 2 步是 M5 最容易写错的地方：`not_found` 一律不是"条件不成立"。
    对阈值类规则它意味着"没能判"，而对缺失类规则它是"命中" ——
    后者由第 1 步（存在性操作符）承担，两条路径**不共用**判断。
    """
    if field is None:
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            ReasonCode.EXTRACTION_FAILED,
            f"解析结果里没有字段 {config.field}",
        )

    if config.op in EXISTENCE_OPS:
        return _match_existence(config, field)

    # ---- 以下均要求字段确实取到了值 ----
    if field.status is FieldStatus.NOT_FOUND:
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            ReasonCode.EVIDENCE_UNCERTAIN,
            f"{config.field} 未在合同中找到，无法比较 —— "
            "这不等于『条件不成立』",
            detail={"status": field.status.value, "op": config.op},
        )
    if field.status is FieldStatus.UNCERTAIN:
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            ReasonCode.EVIDENCE_UNCERTAIN,
            f"{config.field} 存在疑似内容但证据不足，无法比较",
            detail={"status": field.status.value, "op": config.op},
        )
    if field.status is FieldStatus.FAILED:
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            ReasonCode.EXTRACTION_FAILED,
            f"{config.field} 提取失败，无法比较",
            detail={"status": field.status.value, "op": config.op},
        )

    if config.op == "contains":
        return _match_contains(config, field)

    return _match_numeric(config, field, default_currency)


# ============================================================
# expr 的子路径
# ============================================================


def _match_existence(config: ExprMatchConfig, field: ExtractedField) -> MatchResult:
    """`is_null` / `not_null`：**状态就是答案**。

    | 状态 | 存在？ | 说明 |
    | --- | --- | --- |
    | `extracted` | 是 | |
    | `not_found` | 否 | **只有这一种"否"** —— 它是"可靠检索后确实没有" |
    | `uncertain` / `failed` | **判不了** | 把它们当成"否"，就是缺失类规则误报的根源 |
    """
    if field.status is FieldStatus.EXTRACTED:
        present = True
    elif field.status is FieldStatus.NOT_FOUND:
        present = False
    else:
        decided_by = (
            ReasonCode.EXTRACTION_FAILED
            if field.status is FieldStatus.FAILED
            else ReasonCode.EVIDENCE_UNCERTAIN
        )
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            decided_by,
            f"{config.field} 的解析状态为 {field.status.value}，无法判断是否存在"
            " —— 不能据此报『未约定』",
            detail={"status": field.status.value, "op": config.op},
        )

    matched = present if config.op == "not_null" else not present
    return MatchResult(
        MatchVerdict.MATCHED if matched else MatchVerdict.NOT_MATCHED,
        ReasonCode.CONDITION_MATCHED if matched else ReasonCode.CONDITION_NOT_MATCHED,
        f"{config.field} {'已' if present else '未'}约定",
        detail={"status": field.status.value, "present": present, "op": config.op},
    )


def _match_contains(config: ExprMatchConfig, field: ExtractedField) -> MatchResult:
    """文本包含。比较的是 `value_text`（原文），不是数值。"""
    needle = "" if config.value is None else str(config.value)
    haystack = field.value_text
    if not haystack:
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            ReasonCode.EVIDENCE_UNCERTAIN,
            f"{config.field} 没有可比较的文本值",
            detail={"status": field.status.value, "op": config.op},
        )

    matched = needle in haystack
    return MatchResult(
        MatchVerdict.MATCHED if matched else MatchVerdict.NOT_MATCHED,
        ReasonCode.CONDITION_MATCHED if matched else ReasonCode.CONDITION_NOT_MATCHED,
        f"{config.field} {'包含' if matched else '不包含'} {needle!r}",
        located_text=needle if matched else None,
        detail={"actual": haystack, "op": config.op, "threshold": needle},
    )


def _match_numeric(
    config: ExprMatchConfig, field: ExtractedField, default_currency: str
) -> MatchResult:
    """数值比较 —— 三条前置检查，缺一不可。"""
    # ---- ① 金额类字段：币种不可比就不比 ----
    if config.field in MONEY_FIELDS:
        currency = (field.currency or "").strip()
        if not currency:
            return MatchResult(
                MatchVerdict.UNDECIDABLE,
                ReasonCode.CURRENCY_NOT_COMPARABLE,
                f"{config.field} 没有币种，无法与阈值比较大小",
                detail={"op": config.op, "expected_currency": default_currency},
            )
        if currency.upper() != default_currency.strip().upper():
            return MatchResult(
                MatchVerdict.UNDECIDABLE,
                ReasonCode.CURRENCY_NOT_COMPARABLE,
                f"{config.field} 的币种为 {currency}，与本项目运行币种 "
                f"{default_currency} 不同，数值不可比",
                detail={
                    "actual_currency": currency,
                    "expected_currency": default_currency,
                    "op": config.op,
                },
            )

    # ---- ② 实际值 ----
    actual = _as_decimal(field.value_decimal)
    if actual is None:
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            ReasonCode.EVIDENCE_UNCERTAIN,
            f"{config.field} 状态为已抽取，但没有可比较的数值",
            detail={"status": field.status.value, "op": config.op},
        )

    # ---- ③ 阈值 ----
    threshold = _as_decimal(config.value)
    if threshold is None:
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            ReasonCode.THRESHOLD_NOT_CONFIGURED,
            f"规则未配置可比较的阈值（op={config.op}，value={config.value!r}）",
            detail={"op": config.op, "raw_value": repr(config.value)},
        )

    matched = _compare(actual, threshold, config.op)
    return MatchResult(
        MatchVerdict.MATCHED if matched else MatchVerdict.NOT_MATCHED,
        ReasonCode.CONDITION_MATCHED if matched else ReasonCode.CONDITION_NOT_MATCHED,
        f"{config.field} = {actual}，阈值 {config.op} {threshold}",
        # ⚠️ 数值用**字符串**：`Decimal` 不能进 JSON，而 float 会把这份
        # 记账数据也拖回二进制误差里 —— 它就是给人核对"到底比了什么"的。
        detail={
            "actual": str(actual),
            "op": config.op,
            "threshold": str(threshold),
        },
    )


def _compare(actual: Decimal, threshold: Decimal, op: str) -> bool:
    """六种数值比较的**唯一出口**。

    用一个函数收口（而不是在调用处写六个 `if`）：
    新增操作符时只改这里，不会漏掉某条分支里的比较写反。
    """
    if op == "gt":
        return actual > threshold
    if op == "gte":
        return actual >= threshold
    if op == "lt":
        return actual < threshold
    if op == "lte":
        return actual <= threshold
    if op == "eq":
        return actual == threshold
    raise ValueError(f"未实现的数值操作符：{op!r}")  # pragma: no cover - 由 DTO 取值域保证


def _as_decimal(value: Any) -> Decimal | None:
    """把字段值 / 阈值变成 `Decimal`；**不能安全转换时返回 `None`**。

    ⚠️ `float` 必须先 `str()` 再进 `Decimal`：
    `Decimal(0.1)` 得到的是 `0.1000000000000000055511151231257827`，
    而写规则的人想写的是 `0.1`。见模块 docstring 里那条真实规则。
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        # bool 是 int 的子类，但"True > 0.5"没有业务含义 —— 当成配置错误
        return None
    if isinstance(value, float):
        return Decimal(str(value))
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
