"""LLM 端口 —— 「给我一段受约束的指令，还我符合 schema 的结构化结果，或 `None`」。

## 本端口**不抛异常**，与 OCR 端口刻意不同

| 端口 | 失败怎么表达 | 为什么 |
| --- | --- | --- |
| `OCRGateway` | **抛 `AppError` + 错误码** | 失败是**页级**事实，页本身要携带它（`page_status='failed'` + `error_code`） |
| `LLMGateway` | **返回 `None`** | 失败是**单条规则的判断不足**，结论是 `needs_review` + `ReasonCode`。抛 `ErrorCode` 会把它伪装成**流程故障**，而计划 §7.3 明确要求"判断不足**不得**伪装成流程故障" |

⚠️ 因此 **M5 不新增任何 LLM 相关的 `ErrorCode`**。
这处"看起来少了一个码"是**有意的**，不是遗漏 —— 写在这里，避免后来者顺手补上。

## `available` 与「返回 `None`」必须分开

| | 含义 | 引擎的处置 |
| --- | --- | --- |
| `available is False` | **配置层面**没有可用模型 | 走**规则自带的 `fallback_match_json`**（确定性降级） |
| 返回 `None` | **运行期**单次失败 | 只让**本条规则** `needs_review` |

合并成一个信号，会把"本来就没配模型"与"模型这次抖了一下"当成同一件事：
前者该安静降级，后者需要被人看见（否则一次配置错误会以"结果变差了"的形式消失）。

## 为什么身份要暴露 `model_id`

它进 `review_runs.model_version`，进而参与**批次幂等**（设计文档 §4.5 的六项之一）。
因此它必须是**配置快照**（"接的是哪个模型"），**不含**运行期数据（"这次成没成"）——
否则同一份配置重跑两次会得出两个不同的幂等键。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


@runtime_checkable
class LLMGateway(Protocol):
    """受约束的 LLM 能力。

    实现方**不需要**知道规则、批次、字段契约或标准文档 ——
    它只负责"按给定指令产出符合 schema 的结果"。
    """

    @property
    def available(self) -> bool:
        """是否有可用模型 —— **配置层面**，不是"这次调用成没成"。"""
        ...

    @property
    def model_id(self) -> str:
        """模型标识（进 `review_runs.model_version`）。

        Contract:
            - **不得为空字符串**：不可用时也要如实描述（如 `none:unavailable`）。
              空串会让"这一批到底用没用模型"在库里看不出来，而那正是 M11 要统计的东西；
            - **不得含运行期数据**（调用次数、成功与否）：它参与幂等键，
                混入抖动会让同配置重跑得到不同的键。
        """
        ...

    def extract_json(
        self, system: str, user: str, schema: type[BaseModel]
    ) -> BaseModel | None:
        """按 `schema` 提取结构化结果。

        Returns:
            通过校验的 `schema` 实例；**任何失败都是 `None`**（见模块 docstring）。

        Contract:
            1. 返回值**要么是 `None`，要么是 `schema` 的实例** ——
               **绝不**返回裸 `dict`，也**绝不**返回缺字段的半成品；
            2. **不抛异常**：模型侧的任何失败（超时、限流、返回非 JSON、
               schema 校验不过）一律 `None`；
            3. `schema` 校验失败 → `None`，**不得**"尽力补全" ——
               补出来的对象会带着一个**看起来合法**的假值往下走。
        """
        ...

    def complete_text(self, system: str, user: str) -> str | None:
        """生成一段中文文本（摘要 / 关注点）。失败返回 `None`。

        Contract: 同 `extract_json` 的第 2 条 —— 不抛异常。
        """
        ...
