"""LLM 组合根（M11 Task 1）：模型标识与判定钩子的**唯一**来源。

核心契约：入队方（REST / MCP）与执行方（Worker）的 `model_version`
都必须出自 `model_version_of()` —— 两边分叉时，批次会声明一个
它并没有使用的模型（这正是 M5 那条"声明与执行一致"防线的 LLM 版）。
"""

from __future__ import annotations

from app.config import Settings
from app.composition.llm_pipeline import build_llm_gateway, build_llm_judge
from app.ports.llm_gateway import NONE_MODEL_ID, model_version_of
from app.services.rule_service import DEFAULT_MODEL_VERSION


def test_no_model_configured_yields_none_gateway_and_the_legacy_version():
    empty = Settings(llm_base_url="", llm_api_key="", llm_model="")
    assert build_llm_gateway(empty) is None
    assert build_llm_judge(empty) is None
    assert model_version_of(None) == "none:fallback"
    assert model_version_of(None) == DEFAULT_MODEL_VERSION  # 两处常量必须是同一个值
    assert NONE_MODEL_ID == DEFAULT_MODEL_VERSION


def test_a_complete_configuration_yields_a_gateway_whose_id_is_the_version():
    cfg = Settings(
        llm_base_url="http://x/v1",
        llm_api_key="k",
        llm_model="qwen-plus",
    )
    gateway = build_llm_gateway(cfg)
    assert gateway is not None
    assert model_version_of(gateway) == "openai-compatible:qwen-plus"
    assert model_version_of(gateway) == gateway.model_id


def test_a_partial_configuration_is_none_not_broken():
    """只有 base_url 没有模型名 = 调用必然失败 —— 与 llm_enabled 同一判据，判 None。"""
    partial = Settings(llm_base_url="http://x/v1", llm_api_key="", llm_model="")
    assert build_llm_gateway(partial) is None
    assert build_llm_judge(partial) is None
    assert model_version_of(None) == "none:fallback"


def test_judge_is_none_when_gateway_is_none_but_built_when_present():
    """判定钩子与网关同源：None 网关 → None 钩子（纯规则模式守卫）。"""
    cfg = Settings(
        llm_base_url="http://x/v1", llm_api_key="k", llm_model="qwen-plus"
    )
    assert build_llm_judge(cfg) is not None
