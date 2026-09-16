"""适用性判断：解释 `applies_when_json`，回答"这条规则该不该判"。

这是规则评价流水线的前两步（spec §5.1 固定判定顺序）：

    ① 适用条件所需上下文缺失或冲突 → needs_review
    ② 适用条件明确不满足            → not_applicable
    ③ 通过                          → 交给风险条件判断（M5 实现 hit / not_hit）

**为什么"缺失"要单独成一态：**

一份标准商品采购合同没有知识产权条款，是**正常**的；
而一份软件开发合同没有知识产权条款，才是风险。
如果只有"适用 / 不适用"两态，第一种情况会被报成"知识产权条款缺失"——
这是纯粹的误报，会让审批人不再信任系统。

本模块是**纯函数**，不触碰数据库，便于单测，也能被 M5 的规则引擎直接复用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.enums import ContextStatus, ReasonCode, StrEnum
from app.schemas import AppliesWhenConfig

# 这些取值代表"没有可用信息"，等同于未提供。
# 注意 "other" 不在其中：它是一个**已确定**的取值，只是不在适用范围里，
# 因此应判为 not_applicable 而不是 needs_review。
_UNKNOWN_VALUES: Final[frozenset[str]] = frozenset({"", "unknown", "none"})


class Applicability(StrEnum):
    """适用性判断的三种结果。"""

    #: 明确适用，进入风险条件判断
    APPLICABLE = "applicable"
    #: 明确不适用，不参与本次风险判断（原因码 APPLICABILITY_NOT_MET）
    NOT_APPLICABLE = "not_applicable"
    #: 上下文缺失或冲突，**判不了**——必须交由人工判断，不能当成"未命中"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReviewContext:
    """一次审查所用的权威上下文。

    生产环境由 `review_runs.context_snapshot_json` 还原，
    测试与演示可直接构造。

    **两个核验状态必须分开**，这一点很关键：
    合同正文与审批单可能**只在"我方是谁"上冲突**，而合同类型并无歧义。
    若用一个总的 conflict 状态一刀切，就会把"合同类型类规则"
    （例如标准品采购是否适用知识产权规则）也一并判成"判不了"，
    无谓地扩大 `needs_review` 的范围，并让本可正常出结论的规则失去输出。

    它们对应 `contract_parses.basic_info_json` 里的两个保留键
    `_party_consistency` 与 `_contract_type_consistency`。

    `contract_text` 仅用于 `requires_any_keyword` 判断；不需要时应传 `None`，
    而不是空字符串——空字符串会被解释为"正文为空"，从而把适用性判为不可知。
    """

    contract_type: str | None = None
    our_contract_label: str | None = None
    our_business_role: str | None = None
    #: 合同正文（用于 requires_any_keyword）
    contract_text: str | None = None
    #: 我方立场（名称 / 合同标签 / 业务角色）的核验结果
    party_context_status: str | None = None
    #: 合同类型的核验结果
    contract_type_status: str | None = None


@dataclass(frozen=True)
class ApplicabilityResult:
    applicability: Applicability
    reason_code: ReasonCode | None = None
    reason_text: str | None = None

    @property
    def is_applicable(self) -> bool:
        return self.applicability is Applicability.APPLICABLE

    @property
    def needs_review(self) -> bool:
        """判不了 —— 必须交由人工，禁止自动回写。"""
        return self.applicability is Applicability.UNKNOWN

    @property
    def is_not_applicable(self) -> bool:
        return self.applicability is Applicability.NOT_APPLICABLE


def _is_unknown(value: str | None) -> bool:
    return value is None or value.strip().lower() in _UNKNOWN_VALUES


def _matches(values: list[StrEnum] | None, actual: str | None) -> bool:
    """actual 是否落在允许集合内（枚举与数据库字符串统一按值比较）。"""
    if not values:
        return True
    return actual in {str(v) for v in values}


def evaluate_applicability(
    applies: AppliesWhenConfig | None,
    context: ReviewContext,
) -> ApplicabilityResult:
    """判断规则在给定上下文下是否适用。

    返回 `ApplicabilityResult`；调用方按 `applicability` 决定
    该规则下一步走 `not_applicable`、`needs_review` 还是进入风险条件判断。

    判定顺序（不可调换）：
      1. 未配置适用条件 → APPLICABLE（全局适用，**不受任何立场冲突影响**）；
      2. **合同类型维度**：该维度冲突/缺失 → UNKNOWN；明确不匹配 → NOT_APPLICABLE；
      3. **立场维度**（合同标签 / 业务角色）：该维度冲突/缺失 → UNKNOWN；明确不匹配 → NOT_APPLICABLE；
      4. `requires_any_keyword`：正文不可用 → UNKNOWN；无关键词命中 → NOT_APPLICABLE。

    **只对规则真正声明了的维度做冲突检查。**

    这是本函数最关键的一条约定：一条只看合同类型的规则
    （例如"标准品采购合同是否适用知识产权规则"），
    不会因为我方立场有冲突就被判成"判不了"。
    立场冲突只应让**依赖立场的规则**进入 `needs_review`，
    立场无关的规则（主体缺失、金额缺失等）必须照常出结论——
    否则一次主体识别争议会让整份审查结论全部失效。
    """
    # ---- 1. 未配置适用条件 = 全局适用 ----
    if applies is None or applies.is_unrestricted:
        return ApplicabilityResult(Applicability.APPLICABLE)

    # ---- 2. 合同类型维度 ----
    if applies.contract_types:
        if context.contract_type_status == ContextStatus.CONFLICT.value:
            return ApplicabilityResult(
                Applicability.UNKNOWN,
                ReasonCode.CONTEXT_CONFLICT,
                "合同类型的声明与合同正文不一致，无法判断该规则是否适用",
            )
        if _is_unknown(context.contract_type):
            return ApplicabilityResult(
                Applicability.UNKNOWN,
                ReasonCode.CONTEXT_MISSING,
                "合同类型未提供，无法判断该规则是否适用",
            )
        if not _matches(applies.contract_types, context.contract_type):
            return ApplicabilityResult(
                Applicability.NOT_APPLICABLE,
                ReasonCode.APPLICABILITY_NOT_MET,
                f"合同类型为 {context.contract_type}，不在该规则的适用范围内",
            )

    # ---- 3. 立场维度（只在该规则确实依赖立场时才检查）----
    if applies.our_contract_labels or applies.our_business_roles:
        if context.party_context_status == ContextStatus.CONFLICT.value:
            return ApplicabilityResult(
                Applicability.UNKNOWN,
                ReasonCode.CONTEXT_CONFLICT,
                "我方立场的声明与合同正文不一致，无法判断该规则是否适用",
            )

        for values, actual, label in (
            (applies.our_contract_labels, context.our_contract_label, "我方合同标签"),
            (applies.our_business_roles, context.our_business_role, "我方业务角色"),
        ):
            if not values:
                continue

            if _is_unknown(actual):
                # 判不了 ≠ 不适用。若这里返回 not_applicable，
                # "缺少立场信息"就会被伪装成"这条规则不适用"，风险被静默吞掉。
                return ApplicabilityResult(
                    Applicability.UNKNOWN,
                    ReasonCode.CONTEXT_MISSING,
                    f"{label}未提供，无法判断该规则是否适用",
                )

            if not _matches(values, actual):
                return ApplicabilityResult(
                    Applicability.NOT_APPLICABLE,
                    ReasonCode.APPLICABILITY_NOT_MET,
                    f"{label}为 {actual}，不在该规则的适用范围内",
                )

    # ---- 4. 正文关键词触发条件 ----
    if applies.requires_any_keyword:
        if context.contract_text is None:
            return ApplicabilityResult(
                Applicability.UNKNOWN,
                ReasonCode.EVIDENCE_UNCERTAIN,
                "缺少合同正文，无法判断该规则是否适用",
            )
        if not any(k in context.contract_text for k in applies.requires_any_keyword):
            return ApplicabilityResult(
                Applicability.NOT_APPLICABLE,
                ReasonCode.APPLICABILITY_NOT_MET,
                "合同未出现适用该规则所需的关键内容",
            )

    return ApplicabilityResult(Applicability.APPLICABLE)
