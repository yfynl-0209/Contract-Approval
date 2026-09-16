"""`llm` 规则的判定实现 —— 提示词、schema 约束、**证据反向核验**、有界重试。

它产出一个 `LlmJudge` 闭包交给 `app/rules/evaluator.py` 使用；
`llm` 不可用时返回 `None`，引擎据此走**规则自带的 fallback**（见 T4 的 `_match_llm`）。

## ① 命中的结论**必须**带一份能在合同里找到的原文

计划 §10.1 第 ⑦ 步与 §11.3 把两件事定为硬约束：
**模型给出的证据必须在标准文档中真实存在**，且**禁止制造合同中不存在的证据**。

因此本模块做两道关，缺一不可：

| 情形 | 处置 |
| --- | --- |
| 模型说命中，但证据在正文中**找不到** | **结论作废** → `needs_review(EVIDENCE_UNCERTAIN)` |
| 模型说命中，却**没给证据** | **结论作废** → `needs_review(EVIDENCE_UNCERTAIN)` |

第二条容易被当成"过严"：模型答对了，只是没引用原文而已。但本项目的全部前提是
**"每一处结论都能被追问『依据在哪』"** —— 一个没有出处的命中，审批人无法核验，
它和幻觉在**使用上**没有区别。

## ② 正文超长时**拒绝判断**，绝不截断

截断的后果是**静默的**：被截掉的那半页里可能正好写着违约条款，
而模型对剩下的部分给出一个**看起来很确定**的 `not_matched`。
"判断不完整"与"判断为否"在下游完全无法区分。

因此超长直接返回 `needs_review`，并在原因里写清长度 ——
要支持长合同就得走分段检索，那是**另一件事**，不能靠截断了事。

## ③ 重试有界，且失败**不换成确定性答案**

`attempts=2`：一次偶发抖动不该让规则失去判断。两次都失败 → `MODEL_UNAVAILABLE`。

⚠️ 失败**不**回落到 `fallback_match_json`。计划 §6 的 T5 行写"两次失败即降级"，
本实现把它理解为**该规则进入 `needs_review`**，理由有三：

1. §4.2 已定：运行期失败**不得**静默换成确定性答案；
2. §11.4 的自动回写门禁正是靠 `needs_review` 挡住"没真判过"的结论；
3. 否则 M12 的模型质量统计会把 fallback 的答案**算成模型答案**，
   于是"模型到底答得怎么样"这个指标永远失真。

> 变量名里的 `degraded` 指的就是这件事：**降级为人工判断**，不是降级为另一个答案。
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, Field

from app.enums import ReasonCode
from app.ports.llm_gateway import LLMGateway
from app.rules.evaluator import LlmJudge, RuleSpec
from app.rules.matching import MatchResult, MatchVerdict
from app.textnorm import fold_numeric, squash_whitespace

#: 提示词版本 —— 进 `review_runs.prompt_version`，参与批次幂等（§4.5 六项之一）。
#: 改了提示词就必须改它，否则同一份"输入"会复用旧的批次结论。
PROMPT_VERSION: Final[str] = "v1"

#: 模型应答的**受约束结构**。三态与 `MatchVerdict` 对齐 —— 模型也必须能说"判不了"，
#: 否则它只能在"命中"与"未命中"里二选一，而那会**逼出一个错的答案**。
class LlmVerdict(BaseModel):
    """模型对一条规则的判断。"""

    model_config = {"extra": "forbid"}

    verdict: Literal["matched", "not_matched", "undecidable"]
    #: 中文短句，给人看
    reason: str = Field(min_length=1)
    #: 支撑结论的**原文片段**。`matched` 时必填，且必须能在合同正文中找到。
    evidence_quote: str = ""


_SYSTEM_PROMPT: Final[str] = (
    "你是合同风险审查助手。只依据给定的合同正文判断，不得依据常识或经验补充事实。\n"
    "必须输出 JSON，字段为：\n"
    '  verdict: "matched" | "not_matched" | "undecidable"\n'
    "  reason: 一句中文说明\n"
    "  evidence_quote: 支撑结论的**原文片段**（必须逐字来自正文；判不了时留空）\n"
    "判定为 matched 时，evidence_quote 必须非空且逐字来自正文；\n"
    "正文中找不到足够依据时，必须选 undecidable，不得猜测。"
)


def make_llm_judge(
    llm: LLMGateway | None,
    *,
    max_input_chars: int,
    attempts: int = 2,
) -> LlmJudge | None:
    """构造 `llm` 规则的判定钩子。

    Returns:
        `None` 表示**当前没有可用模型** —— 调用方据此走规则自带的 fallback。
        ⚠️ 这个返回值必须与"模型失败"区分开：前者是配置事实，后者是运行期抖动。
    """
    if llm is None or not llm.available:
        return None

    def judge(spec: RuleSpec, text: str) -> MatchResult:
        if len(text) > max_input_chars:
            return MatchResult(
                MatchVerdict.UNDECIDABLE,
                ReasonCode.EVIDENCE_UNCERTAIN,
                f"合同正文 {len(text)} 字，超过模型可完整处理的 {max_input_chars} 字，"
                "拒绝只读一部分就下结论",
                detail={"text_length": len(text), "max_input_chars": max_input_chars},
            )

        user_prompt = _user_prompt(spec, text)

        verdict: LlmVerdict | None = None
        for _ in range(max(1, attempts)):
            verdict = llm.extract_json(_SYSTEM_PROMPT, user_prompt, LlmVerdict)
            if verdict is not None:
                break

        if verdict is None:
            return MatchResult(
                MatchVerdict.UNDECIDABLE,
                ReasonCode.MODEL_UNAVAILABLE,
                f"模型连续 {max(1, attempts)} 次未能给出可用结果",
                detail={"attempts": max(1, attempts), "model_id": llm.model_id},
            )

        return _to_match_result(verdict, text=text, spec=spec)

    return judge


def _user_prompt(spec: RuleSpec, text: str) -> str:
    """规则说明 + 正文。**正文是必需的输入**，不是可选的上下文。"""
    instruction = getattr(spec.config, "instruction", "") or spec.rule_name
    return (
        f"规则名称：{spec.rule_name}\n"
        f"规则说明：{instruction}\n"
        f"风险等级：{spec.risk_level.value}\n\n"
        f"合同正文：\n{text}"
    )


def _to_match_result(verdict: LlmVerdict, *, text: str, spec: RuleSpec) -> MatchResult:
    detail = {"judged_by": "llm", "prompt_version": PROMPT_VERSION}

    if verdict.verdict == "undecidable":
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            ReasonCode.EVIDENCE_UNCERTAIN,
            f"模型无法可靠判断：{verdict.reason}",
            detail=detail,
        )

    if verdict.verdict == "not_matched":
        return MatchResult(
            MatchVerdict.NOT_MATCHED,
            ReasonCode.CONDITION_NOT_MATCHED,
            verdict.reason,
            detail=detail,
        )

    # ---- matched：证据是**必要条件**，不是附带信息 ----
    quote = verdict.evidence_quote.strip()
    if not quote:
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            ReasonCode.EVIDENCE_UNCERTAIN,
            "模型判定命中，但没有给出正文依据 —— 无出处的命中无法核验，作废",
            detail={**detail, "verdict_discarded": "matched_without_evidence"},
        )

    if not _quote_is_in_text(quote, text):
        return MatchResult(
            MatchVerdict.UNDECIDABLE,
            ReasonCode.EVIDENCE_UNCERTAIN,
            f"模型给出的依据在合同正文中找不到（{quote[:20]}…），结论作废",
            detail={
                **detail,
                "verdict_discarded": "evidence_not_found",
                "claimed_quote": quote[:80],
            },
        )

    return MatchResult(
        MatchVerdict.MATCHED,
        ReasonCode.CONDITION_MATCHED,
        verdict.reason,
        located_text=quote,
        detail=detail,
    )


def _quote_is_in_text(quote: str, text: str) -> bool:
    """模型给出的引用是否真的出自正文。

    ⚠️ 只容忍**排版**差异，不容忍**内容**差异：

    | 容忍 | 不容忍 |
    | --- | --- |
    | 空白（模型常把换行/空格吞掉） | 少字、多字、换词 |
    | 全角/半角（NFKC 折叠，OCR 场景常见） | 同义改写 |

    不折叠空白会让**正确的**引用被误判成幻觉；而只要允许"改写"，
    这道关就退化成"看起来像"—— 那正是幻觉要穿过的地方。
    """

    def normalize(value: str) -> str:
        return squash_whitespace(fold_numeric(value)).casefold()

    return normalize(quote) in normalize(text)
