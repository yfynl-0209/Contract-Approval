"""脚本化的 LLM 测试替身。

## ⚠️ 它**不是**"无 GPU 开发"的替代方案

计划 §2.2 说"无 GPU 也能完成全部业务开发" —— 那条路靠的是
**规则自带的 `fallback_match_json`**（确定性降级），**根本不需要模型**。

本类的唯一用途是**测试**：需要确定性、可计数、可注入失败。
把它当成开发期模型用，会得到一份"看起来跑通了、实际什么都判不出来"的引擎 ——
因为一个假 LLM 不可能真的理解合同。

## 为什么要有"喂料口"（而不是固定返回值）

规则评价要覆盖"模型返回了不符合 schema 的 JSON"这类分支，
而那**不能靠真的调用外部服务**来构造。因此本类按**队列**返回：
脚本用尽后回落到 `default_*`（默认 `None` = 失败）。

⚠️ 队列用尽 → `default_*`，**不会报错**。这是刻意的取舍：
若把"脚本用尽"做成异常，就无法表达"后面每一次都失败"这种常见场景。
代价是"忘了配脚本"会静默变成失败 —— 因此每次调用都记录在 `json_calls` / `text_calls` 上，
断言调用次数即可发现。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

#: 模型标识 —— **不含运行期数据**（它进批次幂等键，见 `app/ports/llm_gateway.py`）
MOCK_MODEL_ID = "mock:deterministic"


class MockLlm:
    """`LLMGateway` 的脚本化实现。

    Args:
        available: 是否"有可用模型"。设成 `False` 可构造**没配模型**的场景
            （此时引擎应走规则 fallback，而不是把这条规则判成 `needs_review`）。
        json_responses: 每次 `extract_json` 依次取用的原始内容。
            取值可以是 `str`（JSON 文本）、`dict`/`BaseModel`（直接校验）、或 `None`（模拟失败）。
        text_responses: 同上，用于 `complete_text`。
        default_json / default_text: 脚本用尽后的回落值（默认 `None` = 失败）。
    """

    def __init__(
        self,
        *,
        available: bool = True,
        model_id: str = MOCK_MODEL_ID,
        json_responses: tuple[Any, ...] = (),
        text_responses: tuple[Any, ...] = (),
        default_json: Any = None,
        default_text: str | None = None,
    ) -> None:
        self._available = available
        self._model_id = model_id
        self._json = list(json_responses)
        self._text = list(text_responses)
        self._default_json = default_json
        self._default_text = default_text

        #: 每次调用都记录 —— 断言"调用了几次"是判断"有没有真的去问模型"的唯一办法
        self.json_calls: list[tuple[str, str, type[BaseModel]]] = []
        self.text_calls: list[tuple[str, str]] = []

    # ============================================================
    # 端口
    # ============================================================

    @property
    def available(self) -> bool:
        return self._available

    @property
    def model_id(self) -> str:
        return self._model_id

    def extract_json(
        self, system: str, user: str, schema: type[BaseModel]
    ) -> BaseModel | None:
        # ⚠️ **先记录、再判可用**：不可用时引擎本就不该调用它，
        # 因此"调用记录为空"是"引擎确实没去问模型"的直接证据；
        # 反过来若引擎还是调了，记录会留下痕迹 —— 两种情形都可观测。
        self.json_calls.append((system, user, schema))
        if not self._available:
            return None
        return _validate(self._take_json(), schema)

    def complete_text(self, system: str, user: str) -> str | None:
        self.text_calls.append((system, user))
        if not self._available:
            return None
        value = self._text.pop(0) if self._text else self._default_text
        return None if value is None else str(value)

    # ============================================================
    # 喂料口（公开：本类**就是**测试替身，测试需要能中途改脚本）
    # ============================================================

    def feed_json(self, raw: Any) -> None:
        """让**下一次** `extract_json` 看到 `raw`（`None` = 模拟失败）。"""
        self._json.append(raw)

    def feed_text(self, raw: str | None) -> None:
        """让**下一次** `complete_text` 看到 `raw`（`None` = 模拟失败）。"""
        self._text.append(raw)

    # ============================================================
    # 内部
    # ============================================================

    def _take_json(self) -> Any:
        return self._json.pop(0) if self._json else self._default_json


def _validate(raw: Any, schema: type[BaseModel]) -> BaseModel | None:
    """把脚本内容变成 `schema` 实例；**任何不合规都返回 `None`**。

    ⚠️ 校验失败**不得**"尽力补全"（`model_construct` 之类）：
    补出来的对象带着一个**看起来合法**的假值继续往下走，
    而调用方无法从返回值上看出它是编的。这与端口的契约第 3 条一致。
    """
    if raw is None:
        return None
    if isinstance(raw, schema):
        return raw
    try:
        if isinstance(raw, str):
            return schema.model_validate_json(raw)
        return schema.model_validate(raw)
    except ValidationError:
        return None
