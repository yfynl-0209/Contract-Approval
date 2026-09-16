"""OpenAI 兼容接口的 LLM 适配器（本地 Mock / 自建推理服务 / M11 的 GPU API 都走它）。

## 三条容易写错、写错了也不报错的约定

**① `max_retries=0`。** OpenAI SDK **默认自己重试 2 次**。留着它会让
"两次失败即降级"这条上层策略变成"实际最多 6 次"—— 而网关上看到的是"调用次数正常"。
重试次数是**业务策略**（由引擎决定），不是 SDK 的实现细节。

**② `response_format` 可关。** 它要求服务端支持 JSON 模式；
自建/量化的 OpenAI 兼容服务常常直接 400。留一个开关（`use_json_response_format`），
否则换一个后端就要改源码。关掉之后仍靠**schema 校验**兜底 —— 格式约束不是正确性的来源，
**校验**才是。

**③ 失败一律 `None`，且日志里不得出现提示词。**
提示词里装着**合同正文**，而计划 §12 要求"合同正文、OCR 全文、模型完整输入
**不进入普通日志**"。因此这里只记异常类型，绝不记 `system` / `user` / 响应内容。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

#: 不可用时的如实描述（进 `review_runs.model_version`，**不得为空串**）
UNAVAILABLE_MODEL_ID = "none:unavailable"


class OpenAiCompatibleLlm:
    """`LLMGateway` 的 OpenAI 兼容实现。

    Args:
        base_url / api_key / model: 三项**全非空**才算可用
            （与 `settings.llm_enabled` 同一判据 —— 只有 `base_url` 而没有模型名，
            调用必然失败，却会让 `available` 报"可用"）。
        client: 可注入的客户端，便于测试。为 `None` 时**惰性**构造真实客户端。
        use_json_response_format: 见模块 docstring ②。
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        timeout_seconds: float = 30.0,
        use_json_response_format: bool = True,
        client: Any | None = None,
    ) -> None:
        self._base_url = base_url.strip()
        self._api_key = api_key.strip()
        self._model = model.strip()
        self._timeout = timeout_seconds
        self._use_json_format = use_json_response_format
        self._client = client

    # ============================================================
    # 端口
    # ============================================================

    @property
    def available(self) -> bool:
        """三项配置**全非空**才算可用。

        ⚠️ 判据必须与 `settings.llm_enabled` 一致。不一致的后果很具体：
        引擎按 `available` 决定"走 fallback"，而客户端在这里却判定"不可用"——
        两边各有一套判断，结果是**既没问模型、也没走 fallback**，
        只剩一条 `needs_review`，而原因看起来像"模型判不了"。
        """
        if self._client is not None:
            return True
        return bool(self._base_url and self._api_key and self._model)

    @property
    def model_id(self) -> str:
        if not self.available:
            return UNAVAILABLE_MODEL_ID
        # 注入客户端但没给模型名时（测试里常见）不能拼出 `openai-compatible:` 这种
        # **有冒号没名字**的标识 —— 它非空、能通过"不得为空串"的检查，
        # 但读起来像个真名字。用 `unknown` 如实标注。
        return f"openai-compatible:{self._model or 'unknown'}"

    def extract_json(
        self, system: str, user: str, schema: type[BaseModel]
    ) -> BaseModel | None:
        if not self.available:
            return None

        content = self._chat(system, user, json_mode=self._use_json_format)
        if content is None:
            return None

        try:
            return schema.model_validate_json(content)
        except ValidationError as exc:
            # 校验不过 → None（**不补全、不纠正**）。只记字段名，不记内容：
            # 响应里可能含合同原文。
            logger.warning(
                "LLM 响应不符合 schema：缺失/非法字段 %s", _error_fields(exc)
            )
            return None

    def complete_text(self, system: str, user: str) -> str | None:
        if not self.available:
            return None
        return self._chat(system, user, json_mode=False)

    # ============================================================
    # 内部
    # ============================================================

    def _chat(self, system: str, user: str, *, json_mode: bool) -> str | None:
        """发一次请求；**任何失败都返回 `None`**，不抛异常（端口契约第 2 条）。"""
        try:
            response = self._client_or_create().chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                timeout=self._timeout,
                **({"response_format": {"type": "json_object"}} if json_mode else {}),
            )
        except Exception as exc:
            # ⚠️ 只记**异常类型**：`system` / `user` 里装着合同正文，不得进日志（§12）。
            # 把 str(exc) 也一并记下来是常见做法，但服务端的报错常把请求体回显出来 ——
            # 那等于绕开了脱敏。
            logger.warning("LLM 调用失败：%s", type(exc).__name__)
            return None

        choices = getattr(response, "choices", None) or []
        if not choices:
            logger.warning("LLM 响应没有 choices")
            return None
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if not isinstance(content, str) or not content.strip():
            logger.warning("LLM 响应内容为空")
            return None
        return content

    def _client_or_create(self) -> Any:
        """惰性构造真实客户端（SDK 的 import 与连接池都不该在模块导入时发生）。"""
        if self._client is None:
            from openai import OpenAI

            # ⚠️ `max_retries=0`：重试次数是**业务策略**（见模块 docstring ①）。
            self._client = OpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
                timeout=self._timeout,
                max_retries=0,
            )
        return self._client

    def close(self) -> None:
        """关闭底层客户端的连接池。**只关已经构造过的**（构造是惰性的）。

        与 `MockApprovalGateway.close()` 同一约定：进程退出时由组合根调用，
        不主动建一个再关 —— "从未用过网关"的进程不该凭空付一次构造开销。
        """
        client, self._client = self._client, None
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _error_fields(exc: ValidationError) -> list[str]:
    """只取**字段路径**，不取值 —— 值里可能含合同原文。"""
    return [
        ".".join(str(part) for part in error["loc"]) or "<root>"
        for error in exc.errors()
    ]
