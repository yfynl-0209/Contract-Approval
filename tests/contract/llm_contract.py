"""LLM 端口的**语义合约**：任何实现都必须通过。

## 本模块不得 import 任何具体适配器

`tests/contract/test_contract_hygiene.py` 有源码级守卫。理由在那里写得很清楚：
一旦合约 import 了某个实现，它就从"LLM 网关的合约"退化成"那个适配器的测试"，
M11 的 GPU 实现要么无法复用，要么被迫去模仿另一个实现的内部行为。

## 合约要能构造**失败**，而失败不能靠真的调用外部服务

因此每个实现在自己的测试文件里提供一个 `ScriptedLlm`（"实现 + 喂料口"）。
喂料口是**闭包**而不是共享的 Protocol：两种实现构造失败的方式根本不同
（一个是响应队列，一个是假的 SDK 客户端），强行统一会变成为了合约好看而扭曲实现。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest
from pydantic import BaseModel, Field

from app.ports.llm_gateway import LLMGateway


# ============================================================
# 合约用的最小 schema（**不依赖业务契约** —— 合约只约束端口语义）
# ============================================================


class _Amount(BaseModel):
    value: str = Field(min_length=1)
    currency: str = Field(min_length=1)


class _AmountV2(BaseModel):
    """第二个 schema：用来证明实现**没有把 schema 写死**。"""

    value: str = Field(min_length=1)


@dataclass
class ScriptedLlm:
    """"实现 + 喂料口"的组合。

    Attributes:
        gateway: 被测实现。
        feed_json: 让**下一次** `extract_json` 看到这段原始文本（`None` = 模拟失败）。
        feed_text: 同上，用于 `complete_text`。
        calls: 到目前为止 `extract_json` 实际发起的次数
            —— 用来断言"没调模型时确实一次都没调"。
    """

    gateway: LLMGateway
    feed_json: Callable[[str | None], None]
    feed_text: Callable[[str | None], None]
    calls: Callable[[], int]


class LLMGatewayContract:
    """所有 `LLMGateway` 实现共享的语义断言。"""

    # ============================================================
    # 由各实现提供的夹具
    # ============================================================

    @pytest.fixture()
    def scripted(self) -> ScriptedLlm:
        raise NotImplementedError("实现测试必须提供 ScriptedLlm 夹具")

    @pytest.fixture()
    def unavailable_gateway(self) -> LLMGateway:
        raise NotImplementedError("实现测试必须提供『不可用』的实现")

    # ============================================================
    # 身份
    # ============================================================

    def test_model_id_is_non_empty(self, scripted: ScriptedLlm) -> None:
        """`model_id` 进 `review_runs.model_version`，**不得为空串**。

        空串的后果不是"少一个字段"，而是"这一批到底用没用模型"在库里看不出来 ——
        而那正是 M11 要按版本统计的东西。
        """
        assert scripted.gateway.model_id.strip(), "model_id 不得为空"

    def test_unavailable_implementation_still_describes_itself(
        self, unavailable_gateway: LLMGateway
    ) -> None:
        """不可用时**也要如实描述**，不能返回空串或 None。"""
        assert unavailable_gateway.available is False
        assert unavailable_gateway.model_id.strip()

    # ============================================================
    # `extract_json` 的四条契约
    # ============================================================

    def test_valid_json_yields_a_schema_instance(self, scripted: ScriptedLlm) -> None:
        """合规 JSON → `schema` 的**实例**（不是裸 dict）。"""
        scripted.feed_json('{"value": "800000.00", "currency": "CNY"}')

        result = scripted.gateway.extract_json("s", "u", _Amount)

        assert isinstance(result, _Amount)
        assert result.value == "800000.00"
        assert result.currency == "CNY"

    def test_schema_validation_failure_yields_none_not_a_partial_object(
        self, scripted: ScriptedLlm
    ) -> None:
        """缺字段 → `None`，**绝不**"尽力补全"。

        补出来的对象带着一个**看起来合法**的假值继续往下走，
        而调用方无法从返回值上看出它是编的 —— 这正是幻觉进入结论的通道。
        """
        scripted.feed_json('{"value": "800000.00"}')  # 少了 currency

        assert scripted.gateway.extract_json("s", "u", _Amount) is None

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "抱歉，我无法完成这个请求", "null", "{不是 JSON"],
        ids=["empty", "blank", "refusal", "json-null", "malformed"],
    )
    def test_non_conforming_content_yields_none(
        self, scripted: ScriptedLlm, raw: str
    ) -> None:
        """非 JSON / 拒答 / 空白 → `None`（**不抛异常**）。

        模型拒答是**常态**，不是故障：把它做成异常会让一条规则的判断不足
        变成一次任务级失败（计划 §7.3）。
        """
        scripted.feed_json(raw)

        assert scripted.gateway.extract_json("s", "u", _Amount) is None

    def test_a_failed_call_yields_none_and_does_not_raise(
        self, scripted: ScriptedLlm
    ) -> None:
        """单次失败（超时 / 限流 / 服务端异常）→ `None`，**不抛异常**。

        这是端口与 `OCRGateway` **刻意不同**的一条：OCR 失败是页级事实、要带错误码；
        LLM 失败是单条规则的判断不足，结论是 `needs_review` + `ReasonCode`。
        """
        scripted.feed_json(None)

        assert scripted.gateway.extract_json("s", "u", _Amount) is None

    def test_schema_is_honoured_not_hardcoded(self, scripted: ScriptedLlm) -> None:
        """实现**不得**把 schema 写死。

        写死的症状很隐蔽：换一条规则（另一个 schema）时，
        它要么总返回 `None`（看起来像"模型判不了"），要么按旧 schema 解析出**错位的字段**。
        """
        scripted.feed_json('{"value": "x"}')
        assert isinstance(scripted.gateway.extract_json("s", "u", _AmountV2), _AmountV2)

        scripted.feed_json('{"value": "x"}')
        # 同一个响应，换成要求 currency 的 schema → 应当**判不了**
        assert scripted.gateway.extract_json("s", "u", _Amount) is None

    def test_state_does_not_leak_between_calls(self, scripted: ScriptedLlm) -> None:
        """一次失败不得影响下一次成功（也不得相反）。"""
        scripted.feed_json(None)
        assert scripted.gateway.extract_json("s", "u", _AmountV2) is None

        scripted.feed_json('{"value": "ok"}')
        result = scripted.gateway.extract_json("s", "u", _AmountV2)
        assert isinstance(result, _AmountV2)

    # ============================================================
    # `complete_text`
    # ============================================================

    def test_complete_text_returns_str_or_none(self, scripted: ScriptedLlm) -> None:
        scripted.feed_text("本次审查命中 2 条高风险规则。")
        assert scripted.gateway.complete_text("s", "u") == "本次审查命中 2 条高风险规则。"

        scripted.feed_text(None)
        assert scripted.gateway.complete_text("s", "u") is None

    # ============================================================
    # 不可用实现的行为
    # ============================================================

    def test_unavailable_implementation_returns_none_for_both_methods(
        self, unavailable_gateway: LLMGateway
    ) -> None:
        """不可用时两个方法都返回 `None` —— 引擎据此走**规则 fallback**。

        ⚠️ 这里**不**断言"没有发起调用"：合约管不到实现内部，
        而"零调用"是各实现的性能保证，不是语义保证。
        （`MockLlm` 的调用计数在它自己的实现测试里断言。）
        """
        assert unavailable_gateway.extract_json("s", "u", _Amount) is None
        assert unavailable_gateway.complete_text("s", "u") is None
