"""LLM 适配器的实现测试（M5 / T2）。

## 与合约的分工

| 文件 | 管什么 |
| --- | --- |
| `tests/contract/llm_contract.py` | **语义**：任何实现都要满足（不 import 任何适配器） |
| 本文件 | **这两个实现各自的**东西：惰性构造、`max_retries=0`、调用计数、**日志脱敏**、SDK 的 import 范围 |

## 本文件守住的三类"写错了也不报错"

1. **SDK 自己重试**会把上层的"两次失败即降级"变成"实际最多 6 次"，
   而网关上看到的是"调用次数正常"；
2. **日志里出现提示词** —— 提示词装着**合同正文**，而计划 §12 要求它不得进普通日志。
   服务端报错常把请求体回显出来，所以连 `str(exc)` 都不能记；
3. **`available` 的判据与 `settings.llm_enabled` 不一致** ——
   两边各有一套判断的结果是"既没问模型、也没走 fallback"，只剩一条 `needs_review`。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field

from app.adapters.llm.mock_llm import MOCK_MODEL_ID, MockLlm
from app.adapters.llm.openai_compatible import (
    UNAVAILABLE_MODEL_ID,
    OpenAiCompatibleLlm,
)
from app.config import PROJECT_ROOT, settings
from app.ports.llm_gateway import LLMGateway
from contract.llm_contract import LLMGatewayContract, ScriptedLlm

APP_DIR = PROJECT_ROOT / "app"
LLM_ADAPTER_DIR = APP_DIR / "adapters" / "llm"


#: 合约用的最小 schema —— **与业务契约无关**：本文件只约束端口语义。
class _Schema(BaseModel):
    value: str = Field(min_length=1)


def _schema() -> type[BaseModel]:
    return _Schema


# ============================================================
# 假的 OpenAI SDK 客户端
# ============================================================


class _FakeCompletions:
    """只实现被用到的那一小块：`client.chat.completions.create(...)`。"""

    def __init__(self, owner: _FakeClient) -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> Any:
        self._owner.requests.append(kwargs)
        reply = self._owner.replies.pop(0) if self._owner.replies else None
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            return SimpleNamespace(choices=[])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))]
        )


class _FakeClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.replies: list[Any] = []
        self.chat = SimpleNamespace(completions=_FakeCompletions(self))


# ============================================================
# 1. 把语义合约套在两个实现上
# ============================================================


class TestMockLlmContract(LLMGatewayContract):
    """脚本化替身必须通过整套 LLM 合约。"""

    @pytest.fixture()
    def scripted(self) -> ScriptedLlm:
        gateway = MockLlm()
        return ScriptedLlm(
            gateway=gateway,
            feed_json=gateway.feed_json,
            feed_text=gateway.feed_text,
            calls=lambda: len(gateway.json_calls),
        )

    @pytest.fixture()
    def unavailable_gateway(self) -> LLMGateway:
        return MockLlm(available=False)


class TestOpenAiCompatibleContract(LLMGatewayContract):
    """真实适配器（注入假客户端）必须通过同一套合约。

    M11 的 GPU 实现会继承同一个基类 —— 这就是"可替换"的证明方式：
    不是读代码确认方法名对得上，而是**跑同一套测试**。
    """

    @pytest.fixture()
    def fake(self) -> _FakeClient:
        return _FakeClient()

    @pytest.fixture()
    def scripted(self, fake: _FakeClient) -> ScriptedLlm:
        gateway = OpenAiCompatibleLlm(client=fake, model="fake-model")
        return ScriptedLlm(
            gateway=gateway,
            feed_json=fake.replies.append,
            feed_text=fake.replies.append,
            calls=lambda: len(fake.requests),
        )

    @pytest.fixture()
    def unavailable_gateway(self) -> LLMGateway:
        # 三项配置全空 = 没配模型
        return OpenAiCompatibleLlm()


# ============================================================
# 2. MockLlm 的实现细节
# ============================================================


def test_mock_llm_records_every_call() -> None:
    """调用必须被记录 —— 断言"调用了几次"是判断"有没有真的去问模型"的唯一办法。"""
    gateway = MockLlm(json_responses=('{"value": "a"}',))

    gateway.extract_json("SYS", "USER", _schema())

    assert len(gateway.json_calls) == 1
    system, user, schema = gateway.json_calls[0]
    assert (system, user) == ("SYS", "USER")
    assert schema is _schema()


def test_mock_llm_falls_back_to_default_when_the_script_runs_out() -> None:
    """脚本用尽 → `default_json`（默认 `None` = 失败）。

    这条同时说明"脚本用尽不报错"是**刻意的**：否则无法表达
    "后面每一次都失败"这种最常见的场景。
    """
    gateway = MockLlm(json_responses=('{"value": "a"}',), default_json=None)

    assert gateway.extract_json("s", "u", _schema()) is not None
    assert gateway.extract_json("s", "u", _schema()) is None


def test_mock_llm_model_id_is_stable_and_has_no_runtime_data() -> None:
    """`model_id` 进批次幂等键：**不得**混入运行期数据。

    混入调用次数之类的抖动，会让同一份配置重跑两次得出**两个不同的幂等键** ——
    批次复用直接失效，而每一次都会"成功"。
    """
    gateway = MockLlm()
    before = gateway.model_id
    gateway.extract_json("s", "u", _schema())
    gateway.extract_json("s", "u", _schema())

    assert gateway.model_id == before == MOCK_MODEL_ID


# ============================================================
# 3. OpenAiCompatibleLlm 的实现细节
# ============================================================


@pytest.mark.parametrize(
    ("base_url", "api_key", "model"),
    [
        ("", "k", "m"),
        ("http://x", "", "m"),
        ("http://x", "k", ""),
        ("  ", "k", "m"),
    ],
    ids=["no-base-url", "no-key", "no-model", "blank-base-url"],
)
def test_available_requires_all_three(base_url: str, api_key: str, model: str) -> None:
    """三项**全非空**才算可用。

    只判"有没有 base_url"是很自然的第一版写法，后果是：
    引擎以为有模型（于是不走 fallback），而调用必然失败 ——
    结果是 9 条 llm 规则全部 `needs_review`，原因看起来像"模型判不了"。
    """
    gateway = OpenAiCompatibleLlm(base_url=base_url, api_key=api_key, model=model)

    assert gateway.available is False
    assert gateway.model_id == UNAVAILABLE_MODEL_ID


def test_model_id_contains_the_model_name() -> None:
    gateway = OpenAiCompatibleLlm(
        base_url="http://x", api_key="k", model="Qwen2.5-7B-Instruct"
    )

    assert gateway.model_id == "openai-compatible:Qwen2.5-7B-Instruct"


def test_model_id_is_truthful_when_the_model_name_is_missing() -> None:
    """注入客户端但没给模型名（测试里常见）→ `unknown`，不是 `openai-compatible:`。

    后者非空、能通过"不得为空串"的检查，但读起来**像个真名字**。
    """
    gateway = OpenAiCompatibleLlm(client=_FakeClient())

    assert gateway.model_id == "openai-compatible:unknown"


def test_availability_agrees_with_settings() -> None:
    """`available` 必须与 `settings.llm_enabled` 一致。

    ⚠️ 两边各有一套判断的后果很具体：引擎按 `available` 决定"走 fallback"，
    而客户端在这里判定"不可用" —— 于是**既没问模型、也没走 fallback**，
    只剩一条 `needs_review`，而原因看起来像"模型判不了"。
    """
    configured = OpenAiCompatibleLlm(
        base_url="http://x", api_key="k", model="m"
    )
    assert configured.available is True

    monkeypatched = settings.llm_base_url, settings.llm_api_key, settings.llm_model
    try:
        settings.llm_base_url = "http://x"
        settings.llm_api_key = "k"
        settings.llm_model = "m"
        assert settings.llm_enabled is True
    finally:
        (
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_model,
        ) = monkeypatched

    assert OpenAiCompatibleLlm().available is False
    assert settings.llm_enabled is False


def test_sdk_does_not_retry_internally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """构造真实客户端时必须显式 `max_retries=0`。

    SDK **默认自己重试 2 次**。留着它，上层的"两次失败即降级"会变成"实际最多 6 次"，
    而网关上看到的是"调用次数正常" —— 这属于**策略被实现悄悄改写**。
    """
    recorded: dict[str, Any] = {}

    class _Recorder:
        def __init__(self, **kwargs: Any) -> None:
            recorded.update(kwargs)
            self.chat = SimpleNamespace(completions=_FakeCompletions(_FakeClient()))

    import openai

    monkeypatch.setattr(openai, "OpenAI", _Recorder)

    gateway = OpenAiCompatibleLlm(base_url="http://x", api_key="k", model="m")
    gateway.complete_text("s", "u")

    assert recorded["max_retries"] == 0, "重试次数是业务策略，不得留给 SDK 默认值"
    assert recorded["base_url"] == "http://x"
    assert recorded["timeout"] == 30.0


def test_response_format_can_be_disabled() -> None:
    """关闭 `response_format` 后请求里不得再出现它。

    自建 / 量化的 OpenAI 兼容服务常常直接 400 拒绝该参数 ——
    没有开关就只能改源码。关掉之后仍靠 **schema 校验**兜底：
    格式约束不是正确性的来源，校验才是。
    """
    fake = _FakeClient()
    fake.replies.append('{"value": "a"}')
    gateway = OpenAiCompatibleLlm(
        client=fake, model="m", use_json_response_format=False
    )

    gateway.extract_json("s", "u", _schema())

    assert "response_format" not in fake.requests[0]


def test_empty_choices_is_a_failure_not_an_exception() -> None:
    """服务端返回空 `choices` → `None`，**不抛异常**。"""
    fake = _FakeClient()
    gateway = OpenAiCompatibleLlm(client=fake, model="m")

    assert gateway.extract_json("s", "u", _schema()) is None


def test_failure_logs_never_contain_the_prompt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⚠️ 日志里**不得**出现提示词或异常文本 —— 它们可能含合同正文。

    计划 §12：合同正文、OCR 全文、模型完整输入、访问令牌**不进入普通日志**。
    这里连 `str(exc)` 都不记，是因为**服务端报错常把请求体回显出来** ——
    记异常文本等于绕开了脱敏。
    """
    secret = "本合同总金额为人民币 800,000.00 元，甲方为某某集团有限公司"  # 合同正文
    fake = _FakeClient()
    fake.replies.append(RuntimeError(f"400 Bad Request: {secret}"))
    gateway = OpenAiCompatibleLlm(client=fake, model="m")

    with caplog.at_level(logging.WARNING):
        assert gateway.extract_json(secret, secret, _schema()) is None

    assert caplog.records, "失败必须留下痕迹，否则'模型一直失败'没人知道"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in logged, "日志里出现了合同正文"
    assert "RuntimeError" in logged, "至少要记下异常类型，否则无法排查"


# ============================================================
# 4. 源码级守卫：SDK 只允许出现在适配器目录
# ============================================================


def test_openai_sdk_is_only_imported_under_the_llm_adapter() -> None:
    """`openai` 只允许在 `app/adapters/llm/` 下 import。

    与 M4 给 PyMuPDF 加的那条守卫同源：**只写在文档里的纪律会随开发自然腐蚀**。
    一旦 `app/services/` 或 `app/rules/` 开始 import SDK，
    M11 换实现就从"加一个适配器"变成"改业务代码" ——
    而这正是分层要避免的事。
    """
    offenders: list[str] = []

    for path in sorted(APP_DIR.rglob("*.py")):
        if LLM_ADAPTER_DIR in path.parents:
            continue
        if "openai" in _imported_modules(path):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))

    assert not offenders, (
        "LLM SDK 被适配器目录之外的模块导入了："
        + "；".join(offenders)
        + "。请把 SDK 用法收进 app/adapters/llm/。"
    )


def test_the_guard_can_actually_find_imports() -> None:
    """守住上一条的**有效性**：它得真的能看出 import。

    一条永远返回空列表的实现也能让上一条通过 ——
    那正是"守卫空转"（M4 §0.16 的 `M4_ERROR_CODES` 就是这么失效的）。
    这里拿一个**已知会 import openai** 的文件来校准。
    """
    target = LLM_ADAPTER_DIR / "openai_compatible.py"

    assert target.exists(), "适配器不见了；若是有意删除，请同时删除本文件的守卫"
    # 该模块用的是函数内惰性 import，AST 一样能看出来
    assert "openai" in _imported_modules(target), (
        "守卫认不出这个文件里的 openai import —— 上一条断言因此是空转的"
    )


def _imported_modules(path: Path) -> set[str]:
    """收集模块里所有 import 的目标名（用 AST，不用正则）。

    正则会把字符串与注释里的 `import` 也算进来 ——
    而"禁止 import"这类规则最怕误报，一次误报就没人再信它了。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names
