"""命中证据的定位与落库形状（M5 / T6）。

## 三列的分工（`db/schema.sql` 的 `rule_hits`）

| 列 | 内容 |
| --- | --- |
| `evidence_text` | **主要**证据的原文片段（人看的） |
| `evidence_position` | 主要证据的位置 JSON |
| `evidence_json` | **全部**证据：`[{text, position}, ...]` —— 一条规则可能命中多处 |

## 定位不到 = **结论作废**

`located_text` 是匹配阶段给出的原文片段，它必须能在标准文档里找到。
找不到就**降级为 `needs_review`** —— 而不是留一个空证据继续往下走：

```text
rule_hits 里一行 status=hit、evidence_text 为 NULL
  → M8 画不出框、审批人无法核验
  → 而这条记录**看起来是一条完全正常的命中**
```

这与 M4 §4.6 里 LLM 的证据反向核验是**同一个要求**，
只不过那里核验的是**模型声称**的引用，这里核验的是**匹配给出**的片段。

## 三种命中各有各的证据来源

| 命中来自 | 证据取自 |
| --- | --- |
| `keyword` / `regex`（存在类） | `located_text` 在文档中的位置 |
| `expr`（数值比较） | **字段自身的证据** —— 比较结论没有独立原文片段 |
| `keyword`（缺失类 `absent=true`） | **没有任何片段可指**（结论是"全文都没有"），依据记在 `hit_detail` |

⚠️ 第三行容易被"每一条命中都必须有证据"这条规则误伤。
一份标准品采购合同"没有知识产权条款"是一条**合法且重要**的命中，
强行要求它给一段原文，只会逼出一个编造的证据 —— 或者让这条规则永远判不了。

## `locate()` 的严格性（已知边界）

`locate()` 按**逐字**匹配在 `page.text` 里查找。因此下面两类片段定位不到：

- 跨块的片段（中间夹着块分隔符 `\\n`，而匹配用的文本可能是另一种拼接方式）；
- 被 OCR 改动过空白的片段（模型复述时常吞掉换行）。

两者都会**降级为 `needs_review`**。这是**刻意偏保守**的选择：
定位不到就画不出框，画不出框的命中无法核验 —— 与其给一个"看起来有证据"的
空壳，不如如实说"判不了"。真实语料上若发现误降太多，再考虑做空白归一化
的映射（那需要维护"归一化后偏移 → 原偏移"的对应表，是另一件事）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Final

from app.enums import EvaluationStatus, ReasonCode
from app.ports.field_contract import EvidenceSpan, ExtractedField
from app.ports.parse_document import StandardDocument
from app.rules.evaluator import RuleEvaluation, RuleSpec
from app.schemas import ExprMatchConfig, KeywordMatchConfig
from app.services.document_builder import Evidence as LocatedEvidence
from app.services.document_builder import locate

#: `evidence_json` 最多存几条。
#:
#: ⚠️ 上限是**有意的**：一条到处出现的规则（比如关键词"甲方"）会命中几十上百处，
#: 不设限会让单行体积膨胀到影响查询，而**第 6 条之后的证据对审批人没有增量信息**。
#: 被截断时会在 `hit_detail` 里留下 `evidence_truncated`，不是静默丢弃。
MAX_EVIDENCE_ITEMS: Final[int] = 5


def attach_evidence(
    evaluation: RuleEvaluation,
    *,
    spec: RuleSpec,
    document: StandardDocument,
    fields: Mapping[str, ExtractedField],
) -> RuleEvaluation:
    """给一条评价补上证据；**补不上就如实降级**。**不提交、不落库。**

    Args:
        evaluation: `evaluate_rule()` 的结论。
        spec: 规则规格（用来判断"证据该从哪来"）。
        document: 批次对应的标准文档 —— 证据的坐标系就是它的坐标系。
        fields: 字段码 → 字段结论（`expr` 命中的证据来自这里）。

    Returns:
        带证据的评价；或**降级后**的 `needs_review`。
    """
    # 只有 `hit` 需要证据：`not_hit` / `not_applicable` 是"没有风险"，
    # `needs_review` 已经表达了"判不了" —— 给它们补证据没有意义。
    if evaluation.status is not EvaluationStatus.HIT:
        return evaluation

    spans, truncated = _collect_spans(evaluation, spec=spec, document=document, fields=fields)

    if not spans:
        if _is_absence_hit(spec):
            # 缺失类的命中**天然没有片段**可指：结论是"全文都没有"。
            # 它的依据是"检索过哪些词"，已经在 `hit_detail` 里了（T3 写入）。
            return evaluation
        return _downgrade(
            evaluation,
            "命中片段无法在标准文档中定位，无法核验 —— 该结论作废",
        )

    primary = _primary_of(spans)
    # ⚠️ `evidence_json` 的**第一条**就是主证据：`evidence_text` 与 `evidence_json[0]`
    # 必须指同一段文字。否则消费方（M8 的画框、接口的展示）各取所需时
    # 会拿到两段不同的"主证据" —— 而两边各自看都合理，差异是静默的。
    ordered = (primary,) + tuple(span for span in spans if span is not primary)

    detail = dict(evaluation.hit_detail)
    if truncated:
        detail["evidence_truncated"] = True
        detail["evidence_limit"] = MAX_EVIDENCE_ITEMS

    return replace(
        evaluation,
        evidence_text=primary.text,
        evidence_position=json.dumps(_position_of(primary), ensure_ascii=False),
        evidence_json=json.dumps(
            [{"text": span.text, "position": _position_of(span)} for span in ordered],
            ensure_ascii=False,
        ),
        hit_detail=detail,
    )


# ============================================================
# 证据来源
# ============================================================


def _collect_spans(
    evaluation: RuleEvaluation,
    *,
    spec: RuleSpec,
    document: StandardDocument,
    fields: Mapping[str, ExtractedField],
) -> tuple[tuple[EvidenceSpan, ...], bool]:
    """按来源收集证据片段，返回 `(片段, 是否被截断)`。"""
    if evaluation.located_text:
        located, truncated = _locate_all(document, evaluation.located_text)
        if located:
            return tuple(_to_span(item) for item in located), truncated

    # `located_text` 为空或定位不到 → `expr` 退回**字段自身的证据**。
    # 数值比较的结论（`prepay_ratio > 0.3`）本身没有原文片段，
    # 它的依据是**参与比较的那个字段** —— 这正是 fields.py 要求的
    # "派生字段的证据必须回指到参与计算的原始字段"。
    field_span = _field_evidence(spec, fields)
    if field_span is not None:
        return field_span, False

    return (), False


def _field_evidence(
    spec: RuleSpec, fields: Mapping[str, ExtractedField]
) -> tuple[EvidenceSpan, ...] | None:
    config = spec.config
    if not isinstance(config, ExprMatchConfig):
        return None
    field = fields.get(config.field)
    if field is None or not field.evidence:
        return None
    return field.evidence


def _locate_all(
    document: StandardDocument, needle: str
) -> tuple[list[LocatedEvidence], bool]:
    """定位**全部**出现位置（最多 `MAX_EVIDENCE_ITEMS` 条）。

    `locate()` 每次调用从头扫描，因此这里带上限 —— 否则一篇到处出现
    "甲方"的合同会让这条规则做 O(n²) 的扫描。
    """
    found: list[LocatedEvidence] = []
    for occurrence in range(MAX_EVIDENCE_ITEMS):
        item = locate(document, needle, occurrence=occurrence)
        if item is None:
            return found, False
        found.append(item)

    # 取满上限后**再多探一次**：能探到就说明被截断了，如实记下来。
    truncated = locate(document, needle, occurrence=MAX_EVIDENCE_ITEMS) is not None
    return found, truncated


def _to_span(item: LocatedEvidence) -> EvidenceSpan:
    """定位结果 → `EvidenceSpan`（**全项目共用的证据形状**，与 M4 字段证据一致）。

    ⚠️ 复用同一个模型而不是另建一个 dict 形状：
    `EvidenceSpan` **构造即校验**（区间非空、bbox 归一化），
    而"证据看起来像证据、其实是零面积"正是它要拦的东西。
    """
    return EvidenceSpan(
        page=item.page,
        block_id=item.block_id,
        text=item.text,
        bbox=item.bbox,
        char_start=item.char_start,
        char_end=item.char_end,
        text_precision=item.text_precision,
        bbox_precision=item.bbox_precision,
    )


def _position_of(span: EvidenceSpan) -> dict[str, Any]:
    """位置 JSON。

    ⚠️ 精度有**两个**（`text_precision` 与 `bbox_precision`），不是 `schema.sql`
    注释里写的单个 `precision`。M8 画框必须知道 **bbox** 那个：
    声明 `char` 却给出整行框，框会大到离谱而**两边都不报错**（见 `resolve_span`）。
    这里如实把两个都写出来。
    """
    return {
        "page": span.page,
        "block_id": span.block_id,
        "bbox": list(span.bbox),
        "char_start": span.char_start,
        "char_end": span.char_end,
        "text_precision": span.text_precision.value,
        "bbox_precision": span.bbox_precision.value,
    }


def _primary_of(spans: tuple[EvidenceSpan, ...]) -> EvidenceSpan:
    """主证据：**完整句子优先**于摘要行。

    ⚠️ 这是**排序**，不是过滤 —— 全部证据照旧保留在 `evidence_json` 里。

    为什么要排序：一份合同的**抬头**常有一行「付款方式：合同生效后预付 60%」，
    而正文条款是「甲方应…支付合同总金额的百分之六十作为预付款。」。
    按出现顺序取第一处时，主证据会落在**摘要**上，而验收 2 明确要求
    `evidence_text` 含「百分之六十作为预付款」—— 即**条款那一处**。
    人核对时也只会看条款，不会拿抬头摘要去核。

    判据是**句末标点**（`。` / `；` / `;`），不是关键词表：
    它是**排版事实**（摘要行不结句），不需要维护词表，也不会随合同类型漂移。
    候选全都不满足时退回原顺序 —— 兜底必须是确定的，否则主证据会随扫描顺序变化。
    """
    for span in spans:
        if span.text.rstrip().endswith(("。", "；", ";")):
            return span
    return spans[0]


def _is_absence_hit(spec: RuleSpec) -> bool:
    """是否是**缺失类**规则的命中（这类命中天然没有可指的原文片段）。

    ⚠️ 缺失有**两种写法**，必须都认：

    | 写法 | 例子 |
    | --- | --- |
    | `keyword` + `absent=true` | "全文没有知识产权条款" |
    | **`expr` + `op="is_null"`** | "未约定预付款比例"、"主体信息缺失" |

    ⚠️ 只认前者的后果是**一大类规则永远报不出命中**：`SUBJ_*_MISSING` /
    `AMOUNT_MISSING` / `PAY_CYCLE_MISSING` / `ACC_DEADLINE_MISSING` /
    `PAY_PREPAY_MISSING` 全是 `is_null` 型 —— 它们的结论是"合同里确实没有这一项"，
    自然**没有任何原文片段可指**，于是会被"命中必须有证据"这条规则
    一律降级成 `needs_review`。

    而 `needs_review` 的意思是"**判不了**"，与"确认缺失"是**相反**的结论：
    一份确实缺条款的合同，会因此永远报不出来，而报告上看不出这一点
    （它只显示"需人工确认"）。

    这个缺陷**在 T6 的单元测试里测不出来** —— 那些用例喂的是 keyword 规则；
    它是在 T10 的**批次级**测试里才暴露的（40 条真实规则里有 5 条是这种）。
    """
    config = spec.config
    if isinstance(config, KeywordMatchConfig):
        return config.absent
    return isinstance(config, ExprMatchConfig) and config.op == "is_null"


def _downgrade(evaluation: RuleEvaluation, reason_text: str) -> RuleEvaluation:
    """把无法核验的命中降级为 `needs_review`。

    ⚠️ 保留 `located_text` 与 `hit_detail`：它们正是"为什么定位不到"的线索，
    清掉就只剩一句结论，排查时得从头复现。
    """
    return replace(
        evaluation,
        status=EvaluationStatus.NEEDS_REVIEW,
        reason_code=ReasonCode.EVIDENCE_UNCERTAIN,
        reason_text=reason_text,
        hit_detail={
            **evaluation.hit_detail,
            "downgraded_from": EvaluationStatus.HIT.value,
            "unlocatable_text": evaluation.located_text,
        },
    )
