"""LLM 管线的组合：配置 → 网关 → 判定钩子。

## 为什么这两件事必须出自同一个函数

`model_version` 进 `review_runs`，参与**批次幂等**（六项之一）。它必须描述
"这一批**实际**用了哪个模型"。若入队方自己拼一个模型名、执行方自己建一个钩子，
两边读的即使是同一份 `.env` 也仍会分叉：Worker 起在一台没有 `LLM_API_KEY`
的机器上时，批次声明 `openai-compatible:qwen-plus`，9 条规则却**全部**走了
确定性 fallback —— 而库里两处都不报错。

这与 M5 的 `_ensure_rules_still_exist` 防的是同一类漂移：**批次的声明与
实际执行必须一致**。因此模型标识与钩子共用一个入口。
"""

from __future__ import annotations

from app.adapters.llm.openai_compatible import OpenAiCompatibleLlm
from app.config import Settings
from app.ports.llm_gateway import LLMGateway
from app.rules.evaluator import LlmJudge
from app.rules.llm_judge import make_llm_judge


def build_llm_gateway(settings: Settings) -> LLMGateway | None:
    """按配置构造网关。**三项配置全空 = 没接模型**（合法配置，不是错误）。"""
    if not settings.llm_enabled:
        return None
    return OpenAiCompatibleLlm(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        use_json_response_format=settings.llm_use_json_response_format,
    )


def build_llm_judge(settings: Settings) -> LlmJudge | None:
    """规则引擎要的判定钩子。`None` = 没有模型 → 走规则自带的 fallback。"""
    return make_llm_judge(
        build_llm_gateway(settings), max_input_chars=settings.llm_max_input_chars
    )
