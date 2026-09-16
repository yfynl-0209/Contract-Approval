# M11 LLM Integration and Model Qualification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 9 条 `llm` 规则真正调用模型，并把「这个模型合不合格」变成一个可执行、有退出码的判定。

**Architecture:** 新增一个组合根 `app/composition/llm_pipeline.py`，作为**模型标识与判定钩子的唯一来源** —— 入队方（REST / MCP）与执行方（Worker）都从它取，避免批次的 `model_version` 与实际使用的模型分叉。`model_version_of()` 放在端口层（`app/ports/llm_gateway.py`），因为 `model_id` 的契约本来就定义在那里。Worker 在派发 RULE 作业前**校验批次声明的模型与当前配置一致**，不一致显式失败。

**Tech Stack:** Python 3.11、`openai` SDK（OpenAI 兼容端点）、pytest、SQLite（脚本）／PostgreSQL（M9 之后）、vLLM（自建推理）。

**Spec:** `docs/superpowers/specs/2026-09-13-enterprise-optimization-design.md` §16 路线图第 9 条（M11：独立 GPU 服务器、OCR/LLM 推理 API 与断连恢复）；执行契约见 `app/ports/llm_gateway.py` 与 `app/rules/llm_judge.py` 的模块 docstring。

## Entry Gate（开始 M11 前必须满足）

- **M9 的验收脚本以退出码 `0` 通过**（PostgreSQL / Redis / MinIO / Compose）。本计划不修改任何基础设施，M9 未收口就开始会与 M9 在途改动撞车。
- M3–M8 的 `scripts/verify_m*.py` 退出码为 `0`。
- `.env` 里 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` **三项已填** —— 或明确接受"本任务只接线、不验收模型效果"。
- 保留工作区现有改动；发现与本计划重叠的未完成修改时先报告，不覆盖。

## 排期与依赖（为什么落在 M9 之后）

| 任务 | 依赖 | 说明 |
| --- | --- | --- |
| Task 1–5 | **只依赖 M9** | 不需要任何服务器：填一组云 API Key 就能在笔记本上跑完 |
| Task 6 | **依赖 M10** | 自建 GPU 推理服务要先有 M10 的部署底座（HTTPS / 发布脚本 / 备份） |

**编号沿用 §16 路线图的 M11，不重排。** 理由：`app/services/rule_service.py:528`、`app/ports/llm_gateway.py` 的 docstring、M5 计划 §2.2 与 M6/M7 计划都已按 **M11** 引用这件事；把 M11 改名成 M10、再把 M10 顺延，会制造一整批新的编号漂移 —— 而 M5 文档里那 5 处 `app/harness/policy.py` 的落点漂移刚刚才修完。

**可选的提前量：** Task 1–5 与 M10 无依赖。若希望尽早拿到"模型到底能不能用"的数据，可以在 M9 收口后先做 Task 1–5 再做 M10 —— 这个顺序还会顺带产出 Task 6 决定 GPU 规格所需要的实测数字（单次延迟、成功率）。

## Global Constraints

- **默认零配置可运行**：三项配置留空时，行为与今天逐字一致（`llm_judge is None` → 走规则自带的 `fallback_match_json`）。这条是"无 GPU 也能完成全部业务开发"的基础，不得回退。
- `model_version` 是**配置快照**，**不含**运行期数据（调用次数、成功与否）—— 它参与批次幂等，混入抖动会让同配置重跑得到不同的键（见 `app/ports/llm_gateway.py`）。
- **批次的 `model_version` 与执行时实际使用的模型必须一致。** 不一致时必须显式失败，不得静默执行 —— 否则会出现"报告声称用了 `qwen-plus`，实际 9 条规则全部走了确定性 fallback"，而库里两处都不报错。
- **不新增任何 LLM 相关的 `ErrorCode`。** `app/ports/llm_gateway.py` 已定：模型侧失败一律返回 `None`，结论是 `needs_review` + `ReasonCode`，不得伪装成流程故障。
- **普通日志不得出现提示词、合同正文、模型原始响应、凭据**（计划 §12）。适配器已按此实现，新增的日志点必须同样只记类型与长度。
- 组合根（`app/api/`、`app/composition/`）之外的层**不得 import 适配器**，由 `tests/test_source_invariants.py` 强制。
- 每个任务红-绿-重构，结束跑聚焦测试 + 全量回归。

---

## File Map

- Create `app/composition/llm_pipeline.py`：模型标识与判定钩子的唯一构造点。
- Create `scripts/check_llm_qualification.py`：模型合格性实测（有退出码）。
- Create `tests/test_composition_llm.py`、`tests/test_worker_llm_wiring.py`、`tests/test_model_version_wiring.py`。
- Modify `app/ports/llm_gateway.py`：新增 `NONE_MODEL_ID` 与 `model_version_of()`。
- Modify `app/adapters/llm/openai_compatible.py`：新增 `close()`。
- Modify `app/services/rule_service.py`：`DEFAULT_MODEL_VERSION` 改为引用端口常量。
- Modify `scripts/run_worker.py`：装配判定钩子 + 派发前校验模型版本。
- Modify `app/api/deps.py`、`app/api/tools.py`：REST 路径注入网关。
- Modify `app/tool_facade.py`：工具 5 冻结真实 `model_version`。
- Modify `app/mcp_server.py`、`scripts/run_mcp.py`：MCP 路径注入网关。
- Modify `app/config.py`、`.env.example`、`README.md`：暴露 JSON 模式开关并补文档。

### Task 1: 端口与组合根 —— 模型标识和判定钩子的唯一来源

**Files:**
- Modify: `app/ports/llm_gateway.py`
- Modify: `app/adapters/llm/openai_compatible.py`
- Modify: `app/services/rule_service.py:526-529`
- Modify: `app/config.py:83-86`、`.env.example:28-32`
- Create: `app/composition/llm_pipeline.py`
- Test: `tests/test_composition_llm.py`

**Interfaces:**
- Produces: `NONE_MODEL_ID: Final[str] = "none:fallback"` 与 `model_version_of(gateway: LLMGateway | None) -> str`（`app/ports/llm_gateway.py`）。
- Produces: `build_llm_gateway(settings: Settings) -> LLMGateway | None` 与 `build_llm_judge(settings: Settings) -> LlmJudge | None`（`app/composition/llm_pipeline.py`）。
- Produces: `Settings.llm_use_json_response_format: bool = True`。
- Produces: `OpenAiCompatibleLlm.close() -> None`。

- [ ] **Step 1: 写失败的测试**（`tests/test_composition_llm.py`）

```python
def test_no_model_configured_yields_none_gateway_and_the_legacy_version():
    empty = Settings(llm_base_url="", llm_api_key="", llm_model="")
    assert build_llm_gateway(empty) is None
    assert build_llm_judge(empty) is None
    assert model_version_of(None) == "none:fallback"
    assert model_version_of(None) == DEFAULT_MODEL_VERSION  # 两处常量必须是同一个值


def test_a_complete_configuration_yields_a_gateway_whose_id_is_the_version():
    cfg = Settings(llm_base_url="http://x/v1", llm_api_key="k", llm_model="qwen-plus")
    gateway = build_llm_gateway(cfg)
    assert gateway is not None
    assert model_version_of(gateway) == "openai-compatible:qwen-plus"
    assert model_version_of(gateway) == gateway.model_id
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest -q tests/test_composition_llm.py`
Expected: FAIL，`ImportError: cannot import name 'build_llm_gateway'`。

- [ ] **Step 3: 端口层新增常量与推导函数**

`app/ports/llm_gateway.py` 末尾追加（**放端口层而不是组合根**：`model_id` 的契约本来就定义在这个模块，把"没有网关时用什么版本号"也放这里，避免组合根与 `rule_service` 各写一个 `"none:fallback"` 字面量）：

```python
#: 没有接入模型时的 `model_version`。它是**配置快照**（"这一批没接模型"），
#: 不是运行结果 —— 单次调用失败记在规则的 `reason_code` 上，不改它。
NONE_MODEL_ID: Final[str] = "none:fallback"


def model_version_of(gateway: "LLMGateway | None") -> str:
    """批次的 `model_version`：**唯一**的推导处。

    ⚠️ 入队方（REST / MCP）与执行方（Worker）必须都调用它，
    而不是各自拼一个模型名 —— 两边分叉时，批次会声明一个它并没有使用的模型。
    """
    return NONE_MODEL_ID if gateway is None else gateway.model_id
```

同步把 `app/services/rule_service.py:529` 的常量改为引用（**一个来源，不再有两个字面量**）：

```python
DEFAULT_MODEL_VERSION: Final[str] = NONE_MODEL_ID
```

- [ ] **Step 4: 适配器补 `close()`，配置补 JSON 开关**

`app/adapters/llm/openai_compatible.py` —— SDK 客户端持有 httpx 连接池，进程退出要关（与 `MockApprovalGateway` 同一约定）：

```python
def close(self) -> None:
    """关闭底层客户端的连接池。**只关已经构造过的**（构造是惰性的）。"""
    client, self._client = self._client, None
    close = getattr(client, "close", None)
    if callable(close):
        close()
```

`app/config.py` 的 LLM 段（`llm_max_input_chars` 之后）新增：

```python
#: 是否要求服务端支持 JSON 模式。自建/量化的 OpenAI 兼容服务常常直接 400 ——
#: 关掉之后仍靠 schema 校验兜底：格式约束不是正确性的来源，**校验**才是。
llm_use_json_response_format: bool = True
```

`.env.example` 的 LLM 段补一行 `LLM_USE_JSON_RESPONSE_FORMAT=true`。

- [ ] **Step 5: 新建组合根**

`app/composition/llm_pipeline.py`：

```python
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
```

- [ ] **Step 6: 跑测试确认通过，再跑回归**

Run: `python -m pytest -q tests/test_composition_llm.py tests/test_source_invariants.py`
Expected: PASS（`test_composition_root_is_the_only_adapter_importer` 必须仍然通过 —— `app/composition/` 在允许名单内）

Run: `python -m pytest -q`
Expected: 全量通过，无新增失败。

- [ ] **Step 7: Commit**

```bash
git add app/ports/llm_gateway.py app/composition/llm_pipeline.py app/adapters/llm/openai_compatible.py app/services/rule_service.py app/config.py .env.example tests/test_composition_llm.py
git commit -m "feat(m11): single composition root for the LLM gateway and its model_version"
```

### Task 2: Worker 真正调用模型

**Files:**
- Modify: `scripts/run_worker.py:96-114`（`make_handler`）、`scripts/run_worker.py` 的 `main()`
- Test: `tests/test_worker_llm_wiring.py`

**Interfaces:**
- Consumes: `build_llm_judge(settings)`、`model_version_of(gateway)`（Task 1）
- Produces: `make_handler(storage, allowed_types=(), *, llm_judge=None) -> Handler`

- [ ] **Step 1: 写失败的测试**

```python
def test_the_rule_handler_forwards_the_judge(monkeypatch, factory, seeded_rule_job):
    """⚠️ 断言的是**接线**，不是"模型判得准不准" —— 后者是 Task 5 的事。

    `seeded_rule_job` 夹具：入队一条 RULE 作业（与 `tests/test_rule_service.py`
    既有的入队方式相同），因此走的是**生产同款**的领取路径，不是手搓 JobRun。
    """
    from app.worker import Worker
    from scripts import run_worker

    seen: dict[str, object] = {}

    def fake_run_rule_job(session, *, run_id, storage, llm_judge=None):
        seen["llm_judge"] = llm_judge
        seen["run_id"] = run_id

    monkeypatch.setattr(run_worker, "run_rule_job", fake_run_rule_job)

    judge = lambda spec, text: MatchResult(          # noqa: E731 - 固定结论的替身
        MatchVerdict.NOT_MATCHED, ReasonCode.CONDITION_NOT_MATCHED, "stub"
    )
    worker = Worker(
        factory,
        run_worker.make_handler(None, llm_judge=judge),   # RULE 分支不碰 storage
        job_types=[JobType.RULE],
    )
    assert worker.run_once() is True
    assert seen["llm_judge"] is judge, "判定钩子没被传下去 —— llm 规则会静默走 fallback"


def test_omitting_the_judge_keeps_today_behaviour(monkeypatch, factory, seeded_rule_job):
    """不传 = 没有模型 = 纯规则模式。这条是"零配置可运行"的守卫。"""
    from app.worker import Worker
    from scripts import run_worker

    seen: dict[str, object] = {"llm_judge": "未赋值"}

    def fake_run_rule_job(session, *, run_id, storage, llm_judge=None):
        seen["llm_judge"] = llm_judge

    monkeypatch.setattr(run_worker, "run_rule_job", fake_run_rule_job)

    worker = Worker(factory, run_worker.make_handler(None), job_types=[JobType.RULE])
    assert worker.run_once() is True
    assert seen["llm_judge"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest -q tests/test_worker_llm_wiring.py`
Expected: FAIL，`TypeError: make_handler() got an unexpected keyword argument 'llm_judge'`。

- [ ] **Step 3: 实现**

`scripts/run_worker.py` —— 签名加关键字参数（默认 `None`，现有调用点不受影响）：

```python
def make_handler(
    storage: ObjectStorage,
    allowed_types: tuple[str, ...] = (),
    *,
    llm_judge: LlmJudge | None = None,
) -> Handler:
```

RULE 分支（第 113 行）改为：

```python
            run_rule_job(
                run.session, run_id=run_id, storage=storage, llm_judge=llm_judge
            )
```

`main()` 里构建一次并复用（钩子是**无状态闭包**，可以跨作业复用）：

```python
    gateway = build_llm_gateway(settings)
    llm_judge = build_llm_judge(settings)
    print(f"[worker] RULE 作业使用的模型：{model_version_of(gateway)}")
    handler = make_handler(
        storage=storage, allowed_types=allowed_types, llm_judge=llm_judge
    )
```

⚠️ 打印的是 `model_version`（配置快照），**不是** Key —— 这条日志必须能进运维终端。

- [ ] **Step 4: 跑测试确认通过，再跑回归**

Run: `python -m pytest -q tests/test_worker_llm_wiring.py tests/test_rule_service.py`
Expected: PASS

Run: `python -m pytest -q`
Expected: 全量通过。

- [ ] **Step 5: 手工验证一次真调用**（需要已填 `.env`）

```powershell
python scripts/init_db.py --reset
python scripts/make_fixtures.py
python scripts/run_mock.py            # 终端 1
python scripts/run_api.py             # 终端 2
python scripts/run_worker.py          # 终端 3：应打印「RULE 作业使用的模型：openai-compatible:...」
```

走一遍工具 1 → 4 → 5，确认 `GET /api/runs/{run_id}` 里 9 条 `llm` 规则的 `detail.judged_by` 为 `llm`（而不是缺省走 fallback）。**这一步是"接线真的生效"的唯一证据**，pytest 全绿不能替代它。

- [ ] **Step 6: Commit**

```bash
git add scripts/run_worker.py tests/test_worker_llm_wiring.py
git commit -m "feat(m11): wire the llm judge into the RULE job handler"
```

### Task 3: 入队时冻结真实的 `model_version`（REST 与 MCP 两条路径）

**Files:**
- Modify: `app/tool_facade.py:421-449`
- Modify: `app/api/deps.py`（`get_llm` / `close_adapters` / `__all__`）
- Modify: `app/api/tools.py` 的工具 5 路由
- Modify: `app/mcp_server.py:155-155`（`build_mcp_server` 签名）与工具 5 注册处
- Modify: `scripts/run_mcp.py:187` 与 `:202`（两处 `build_mcp_server` 调用）
- Test: `tests/test_model_version_wiring.py`

**Interfaces:**
- Consumes: `model_version_of(gateway)`（Task 1）
- Produces: `tool_facade.run_contract_rules(case_id, *, session, actor, force=False, llm: LLMGateway | None = None)`
- Produces: `get_llm(request: Request) -> LLMGateway | None`（FastAPI 依赖，可被 `app.dependency_overrides` 覆盖）

- [ ] **Step 1: 写失败的测试**

```python
def test_the_batch_declares_the_model_that_will_actually_run(client, seeded_parse):
    """没接模型 → `none:fallback`；接了 → `openai-compatible:<model>`。"""
    no_model = run_contract_rules_tool(client, seeded_parse)
    assert run_model_version(no_model) == "none:fallback"

    with_llm = run_contract_rules_tool(client, seeded_parse, llm=StubGateway("qwen-plus"))
    assert run_model_version(with_llm) == "openai-compatible:qwen-plus"


def test_mcp_tool_five_exposes_the_same_model_version(...):
    """两条形态必须从同一个函数取版本 —— 分叉的后果见 llm_pipeline 的 docstring。"""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest -q tests/test_model_version_wiring.py`
Expected: FAIL，`run_contract_rules() got an unexpected keyword argument 'llm'`。

- [ ] **Step 3: 工具门面接受网关**

`app/tool_facade.py` —— 工具 5：

```python
def run_contract_rules(
    case_id: str,
    *,
    session: Session,
    actor: Actor,
    force: bool = False,
    llm: LLMGateway | None = None,
) -> dict[str, Any]:
    ...
    start = request_rule_run(
        session,
        parse_id=parse_id,
        context=context,
        model_version=model_version_of(llm),
        prompt_version=PROMPT_VERSION,
        force=force,
    )
```

导入：`from app.ports.llm_gateway import LLMGateway, model_version_of`。

⚠️ 只加这一个参数，**不要**把它变成"调用方传字符串版本号"—— 那正是 `run_rule_job` docstring 拒绝的形态：字符串可以随便传，而网关对象只能从组合根拿到。

- [ ] **Step 4: REST 组合根注入**

`app/api/deps.py`，完全照抄 `get_gateway` 的形态（惰性构造 + 挂 `app.state` 跨请求复用）：

```python
def get_llm(request: Request) -> LLMGateway | None:
    """按应用生命周期复用的 LLM 网关。`None` = 没接模型（合法配置）。

    ⚠️ 缓存的是 `None` 也要缓存：否则每个请求都会重新判断一次配置，
    而"有没有模型"在一次进程生命周期内不会变。
    """
    if not hasattr(request.app.state, "llm_gateway"):
        from app.composition.llm_pipeline import build_llm_gateway

        request.app.state.llm_gateway = build_llm_gateway(settings)
    return request.app.state.llm_gateway
```

`close_adapters` 里补：

```python
    llm = getattr(app.state, "llm_gateway", None)
    if llm is not None:
        llm.close()
```

`__all__` 加 `"get_llm"`。`app/api/tools.py` 工具 5 路由：

```python
def run_contract_rules(
    payload: RunContractRulesRequest,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.REVIEW_EXECUTE)),
    llm: LLMGateway | None = Depends(get_llm),
) -> dict[str, Any]:
    return tool_facade.run_contract_rules(
        str(payload.parse_id), session=session, actor=actor,
        force=payload.force, llm=llm,
    )
```

- [ ] **Step 5: MCP 路径注入**

`app/mcp_server.py` 的 `build_mcp_server` 签名加 `llm: LLMGateway | None = None`（**只加端口类型参数**，本模块不得 import 适配器），工具 5 处传 `llm=llm`。

`scripts/run_mcp.py` 两处调用（`:187` stdio、`:202` streamable-http）都传同一个网关：

```python
gateway_llm = build_llm_gateway(settings)
...
server = build_mcp_server(..., llm=gateway_llm)
```

- [ ] **Step 6: 跑测试确认通过，再跑回归**

Run: `python -m pytest -q tests/test_model_version_wiring.py tests/test_m7_contracts.py tests/test_source_invariants.py`
Expected: PASS（`test_source_invariants` 必须仍绿：`app/mcp_server.py` 没有新增适配器 import）

Run: `python -m pytest -q`
Expected: 全量通过。

- [ ] **Step 7: Commit**

```bash
git add app/tool_facade.py app/api/deps.py app/api/tools.py app/mcp_server.py scripts/run_mcp.py tests/test_model_version_wiring.py
git commit -m "feat(m11): freeze the real model_version at enqueue time on both transports"
```

### Task 4: 声明与执行一致 —— Worker 侧的模型版本校验

**Files:**
- Modify: `scripts/run_worker.py`（RULE 分支）
- Test: `tests/test_worker_llm_wiring.py`

**Interfaces:**
- Consumes: `model_version_of(gateway)`（Task 1）、`ReviewRun.model_version`
- Produces: `_ensure_model_matches_run(session, *, run_id: int, current_model_version: str) -> None`

- [ ] **Step 1: 写失败的测试**

```python
def test_a_run_declaring_qwen_fails_loudly_when_the_worker_has_no_model(work_dir):
    """批次声称用了模型，而执行方没有模型 —— 必须失败，不得静默走 fallback。"""
    run_id = seed_run_with_model_version(work_dir, "openai-compatible:qwen-plus")
    with pytest.raises(PermanentError, match="模型"):
        _ensure_model_matches_run(session, run_id=run_id, current_model_version="none:fallback")


def test_the_two_legacy_values_agree(work_dir):
    """M5–M10 期间入队的批次声明 `none:fallback`，接上模型后重跑必须报错而不是换答案。"""
    run_id = seed_run_with_model_version(work_dir, "none:fallback")
    with pytest.raises(PermanentError):
        _ensure_model_matches_run(session, run_id=run_id, current_model_version="openai-compatible:qwen-plus")


def test_matching_versions_pass(work_dir):
    run_id = seed_run_with_model_version(work_dir, "none:fallback")
    _ensure_model_matches_run(session, run_id=run_id, current_model_version="none:fallback")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest -q tests/test_worker_llm_wiring.py -k model_matches`
Expected: FAIL，`NameError: name '_ensure_model_matches_run' is not defined`。

- [ ] **Step 3: 实现**

`scripts/run_worker.py` —— 与 `app/services/rule_service.py::_ensure_rules_still_exist` 同一形态：

```python
def _ensure_model_matches_run(
    session: Session, *, run_id: int, current_model_version: str
) -> None:
    """批次的 `model_version` 与实际可用的模型**必须一致**。

    批次的模型版本在**入队时冻结**（M5 决策 ①）。执行时不一致意味着两种
    不可能同时为真的说法之一：批次声称用了 `qwen-plus` 而实际走了确定性
    fallback，或者反过来。两者都会让 `review_runs.model_version` 失去意义
    —— 而那一列正是统计"模型答得怎么样"的分组键。

    ⚠️ **不得**改成"按当前配置继续跑"：那会让同一份输入在两次执行之间
    得出不同的结论，而报告上两批都写着同一个 `run_id`。
    """
    row = session.get(ReviewRun, run_id)
    if row is None:
        raise PermanentError(
            f"批次 {run_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )
    if row.model_version != current_model_version:
        raise PermanentError(
            f"批次 {run_id} 声明的模型是 {row.model_version!r}，"
            f"而当前执行环境提供的模型是 {current_model_version!r} —— "
            "无法按批次声明的模型执行，也不会改用另一个模型重算",
            code=ErrorCode.UNEXPECTED_ERROR,
        )
```

RULE 分支在调用 `run_rule_job` **之前**插入：

```python
            _ensure_model_matches_run(
                run.session, run_id=run_id, current_model_version=model_version
            )
```

（`model_version` 由 `make_handler` 新增的关键字参数带入，`main()` 传 `model_version_of(gateway)`；测试可显式传。）

- [ ] **Step 4: 跑测试确认通过，再跑回归**

Run: `python -m pytest -q tests/test_worker_llm_wiring.py`
Expected: PASS

Run: `python -m pytest -q`
Expected: 全量通过。

- [ ] **Step 5: Commit**

```bash
git add scripts/run_worker.py tests/test_worker_llm_wiring.py
git commit -m "feat(m11): refuse to execute a run with a mismatched model version"
```

### Task 5: 模型合格性实测 —— 把"这个模型能不能用"变成退出码

**Files:**
- Create: `scripts/check_llm_qualification.py`
- Create: `tests/test_llm_qualification_script.py`
- Modify: `README.md`（用法与判据）

**Interfaces:**
- Consumes: `build_llm_gateway` / `build_llm_judge`（Task 1）、`load_active_rules` / `specs_by_code`（`app/services/rule_service.py`）、`RuleSpec.is_llm`
- Produces: 退出码 `0` = 模型合格；`1` = 有不合格项（逐项打印实测值与阈值）

**为什么直接调判定钩子而不是跑整条管线：** `make_llm_judge` 产出的 `judge(spec, text)` 已经**内含证据反向核验**（`_to_match_result` 里调 `_quote_is_in_text`），因此它单独就能回答"这个模型的逐字摘录能力够不够"。整条管线还要 PDF、OCR、字段提取，那些变量会掩盖模型本身的质量。⚠️ 代价是**绕过了 `applies_when` 的适用性判断**（那是评价引擎的职责）—— 这一点必须写进脚本 docstring，否则会被当成全景回归来读。

- [ ] **Step 1: 写样本与判据（脚本顶部常量）**

```python
@dataclass(frozen=True)
class Sample:
    name: str
    text: str
    #: 这份正文里**确实存在**的风险 → 必须判成 matched。空的表示"干净合同"。
    must_match: tuple[str, ...] = ()


SAMPLES: tuple[Sample, ...] = (
    Sample(
        name="一方独担违约责任",
        text=(
            "甲方与乙方就软件开发事宜达成如下协议。"
            "第三条 违约责任：本合同履行过程中产生的一切违约责任均由甲方承担，"
            "甲方应向乙方支付合同总价百分之三十的违约金；乙方在任何情形下均不承担违约责任。"
            "第四条 保密：双方对在合作中知悉的对方商业秘密承担保密义务，保密期限为本合同终止后三年。"
            "第五条 争议解决：双方协商不成的，提交合同签订地人民法院诉讼解决。"
        ),
        must_match=("LIAB_UNEQUAL_AGAINST_PARTY_A",),
    ),
    Sample(
        name="保密义务单方承担",
        text=(
            "甲方与乙方就数据处理服务事宜达成如下协议。"
            "第三条 保密：乙方应就本合同项下全部信息承担无限期保密义务，"
            "未经甲方书面同意不得向任何第三方披露；甲方对乙方提供的资料不承担任何保密义务。"
            "第四条 违约责任：双方按各自过错程度承担相应责任。"
        ),
        must_match=("CONF_UNILATERAL_AGAINST_PARTY_B",),
    ),
    Sample(
        name="条款均衡（假阳性探测）",
        text=(
            "甲方与乙方就办公用品采购事宜达成如下协议。"
            "第三条 违约责任：任何一方违反本合同约定的，应按实际损失向对方承担赔偿责任。"
            "第四条 保密：双方对在合作中知悉的对方商业秘密承担保密义务。"
            "第五条 验收：交付后三十日内完成验收，验收标准以双方确认的技术参数表为准。"
            "第六条 争议解决：双方协商不成的，提交合同签订地人民法院诉讼解决。"
        ),
        must_match=(),
    ),
)

#: 合格线（**刻意分开三条**，因为它们的失败原因完全不同）
MAX_UNAVAILABLE = 0        # 模型连合法 JSON 都给不出 → 端点/连通性/格式约束问题
MAX_FABRICATED = 1         # 引用在正文里找不到 → **模型的逐字摘录能力**不足
MIN_DECIDABLE_RATIO = 0.85 # 判出明确结论的比例
```

- [ ] **Step 2: 写实测循环**

```python
@dataclass(frozen=True)
class Row:
    sample: str
    rule: str
    result: MatchResult
    seconds: float


def judgeable_specs(session: Session) -> tuple[RuleSpec, ...]:
    """9 条 `llm` 规则的已解析规格。

    `is_llm` 用 `RuleSpec` 自身那个属性，而不是在这里比 `match_mode` 字符串
    —— 后者在 `MatchMode` 取值变化时会静默变小（少判几条规则而看不出来）。
    """
    specs = specs_by_code(load_active_rules(session))
    return tuple(spec for spec in specs.values() if spec.is_llm)


def run_checks(judge: Callable[[RuleSpec, str], MatchResult],
               specs: tuple[RuleSpec, ...]) -> tuple[Row, ...]:
    rows: list[Row] = []
    for sample in SAMPLES:
        for spec in specs:
            started = time.perf_counter()
            result = judge(spec, sample.text)
            rows.append(
                Row(sample.name, spec.rule_code, result, time.perf_counter() - started)
            )
    return tuple(rows)


def counts(rows: tuple[Row, ...]) -> dict[str, int]:
    """三条判据的**原始计数** —— 阈值在 `main()` 里比，测试直接断言计数。"""
    return {
        "total": len(rows),
        "unavailable": sum(
            r.result.reason_code is ReasonCode.MODEL_UNAVAILABLE for r in rows
        ),
        "fabricated": sum(
            r.result.detail.get("verdict_discarded")
            in {"evidence_not_found", "matched_without_evidence"}
            for r in rows
        ),
        "decidable": sum(r.result.decidable for r in rows),
    }


def missed_must_match(rows: tuple[Row, ...]) -> list[tuple[str, str]]:
    """正文里**确实存在**却没判出来的风险 —— 假阴性。"""
    return [
        (sample.name, code)
        for sample in SAMPLES
        for code in sample.must_match
        if not any(
            row.sample == sample.name and row.rule == code and row.result.matched
            for row in rows
        )
    ]
```

输出必须是 ASCII 标记（`OK` / `NG` / `--`），**不能用 emoji**：`scripts/verify_m5.py` 里已经踩过这个坑 —— 在 Windows 默认 GBK 终端打印 `⚠️` 会让脚本当场中止。

- [ ] **Step 3: 打印报告与退出码**

```text
model_version         : openai-compatible:qwen-plus
调用总数              : 27        (3 份样本 x 9 条 llm 规则)
明确结论              : 26/27     (阈值 >= 85%)
MODEL_UNAVAILABLE     : 0         (阈值 = 0)
证据引用被作废        : 0         (阈值 <= 1)
必判命中项            : 3/3
单次耗时              : p50 4.2s / max 11.8s / 合计 141.6s

[NG] 判不出：样本「条款均衡」规则 IP_TRANSFER_AWAY_FROM_PARTY_A
```

- [ ] **Step 4: 手工跑一次并记录基线**

Run: `python scripts/init_db.py --reset; python scripts/check_llm_qualification.py`
Expected: 打印报告；退出码 `0` 表示该 `LLM_MODEL` 可用。

⚠️ **「合计耗时」这一栏要留着。** 它等于"一份合同接上模型后的最坏批处理时间"（9 条规则串行）—— Task 6 决定 GPU 规格、以及将来是否要做并行/合批，依据都是这个数，不是估计值。

- [ ] **Step 5: 写测试（不打真实模型）**

用 `MockLlm` 构造三种失败的判定钩子，断言脚本的判定逻辑与退出码：

```python
def test_a_model_that_never_returns_valid_json_is_rejected(...):
    """全部 MODEL_UNAVAILABLE → 退出码 1，且原因指向连通性/格式，不是"合同没写"。"""

def test_a_model_that_paraphrases_its_evidence_is_rejected(...):
    """引用改写 → 证据作废条数超阈值 → 退出码 1。"""

def test_a_missing_must_match_is_rejected(...):
    """明确存在的风险条款没判出来 → 退出码 1，即使其余指标全绿。"""
```

Run: `python -m pytest -q tests/test_llm_qualification_script.py`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add scripts/check_llm_qualification.py tests/test_llm_qualification_script.py README.md
git commit -m "feat(m11): executable model qualification check with exit code"
```

### Task 6: 自建 GPU 推理服务接入与断连恢复（依赖 M10）

**Files:**
- Modify: `.env.example`、`README.md`、`CONTEXT.md`
- Modify: `scripts/check_llm_qualification.py`（增加 `--preflight` 端点检查）
- Test: `tests/test_llm_preflight.py`

**Interfaces:**
- Consumes: Task 1 的组合根、Task 5 的合格性脚本
- Produces: `check_endpoint(gateway) -> EndpointStatus`（`GET /v1/models` 探活 + 单次最小调用）

**前置：** M10 的部署底座已完成（HTTPS、发布脚本、备份）。本任务只把 `LLM_BASE_URL` 从云 API 指向自建端点。

- [ ] **Step 1: 起 vLLM 并钉住模型名**

```bash
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-8B \
  --served-model-name qwen3-8b \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.9 \
  --port 8000
```

`.env`：

```env
LLM_BASE_URL=http://<gpu-host>:8000/v1
LLM_API_KEY=EMPTY
LLM_MODEL=qwen3-8b
LLM_TIMEOUT_SECONDS=60
LLM_USE_JSON_RESPONSE_FORMAT=true
```

⚠️ **`LLM_MODEL` 必须逐字等于 `--served-model-name`。** 它进 `model_version`（`openai-compatible:qwen3-8b`），而那一列参与批次幂等 —— 名字对不上时，同一份配置会被算成"换过模型"，历史批次全部不再复用。

⚠️ 自建端点对 `response_format={"type":"json_object"}` 返回 400 时，把 `LLM_USE_JSON_RESPONSE_FORMAT=false`。**不要**因此改提示词或放宽 schema —— 格式约束不是正确性的来源，`LlmVerdict` 的校验才是。

- [ ] **Step 2: 写失败的测试**（`tests/test_llm_preflight.py`）

```python
def test_a_reachable_endpoint_that_answers_reports_ok(...): ...
def test_a_reachable_endpoint_whose_served_name_differs_is_rejected(...):
    """`GET /v1/models` 不含 `LLM_MODEL` 时**拒绝**：模型名不匹配等于批次
    声明的模型没有真的在被调用，而这在报告上完全看不出来。"""
def test_an_unreachable_endpoint_reports_a_connection_problem_not_a_model_problem(...): ...
```

- [ ] **Step 3: 实现 `--preflight`**

`python scripts/check_llm_qualification.py --preflight`：探活 → 校验 `served-model-name` → 只跑 **1 条**规则的 **1 次**调用 → 打印结论。**必须便宜**：它会被放进部署脚本，每次发布都跑。

- [ ] **Step 4: 验收断连恢复**

```powershell
# 1) 停掉 vLLM，确认工具 5 仍然返回 200（入队成功，不是失败）
# 2) 等 worker 执行：9 条 llm 规则应全部 needs_review(MODEL_UNAVAILABLE)
# 3) 确认回写被 M6 的门禁挡住：comment_logs 出现 not_written + WRITEBACK_POLICY_DENIED
# 4) 起回 vLLM，带 force=true 重跑工具 5
# 5) 确认新批次的 model_version 与旧批次一致、结论由 needs_review 变为可判定
```

⚠️ 第 3 步是这次验收**真正的价值**：它证明 M6 的门禁在真实模型故障下仍然挡住"没真判过"的结论。把实测输出留进 M12 的验收材料。

- [ ] **Step 5: Commit**

```bash
git add .env.example README.md CONTEXT.md scripts/check_llm_qualification.py tests/test_llm_preflight.py
git commit -m "feat(m11): self-hosted inference endpoint preflight and disconnect recovery"
```

---

## 本次不做（明确排除，避免范围蔓延）

- **9 条 LLM 规则的并行调用与合批。** 先做 Task 5，拿到单次耗时与成功率的实测值再决定 —— 现在估的数会决定一个可能不必要的并发改造。若要做的形态是：把 9 次串行改为有界并发（`ThreadPoolExecutor`，上限取 `settings` 可配），断言"并发后的结论与串行逐条相同"。
- **条款级分片（Map-Reduce）。** 解析产物里已有 `clause_info_json`（8+1 类，见 `app/rules/clauses.py`），切分是现成的；但那是为了突破 `llm_max_input_chars` 的 20,000 字上限，属于**能力扩展**而不是本次接线。
- **提示词调优。** 改了提示词**必须**同步改 `PROMPT_VERSION`（`app/rules/llm_judge.py:60`），否则同一份输入会复用旧批次的结论 —— 而库里两处都不报错。
- **模型微调。** `大模型项目实战.md` §4 只覆盖知识库 / 问数 / 客服三个项目，**不含合同审批项目**。本项目不需要微调。
- **多 Agent 编排。** 与 M11 的验收目标无关。

## 完成判据（M11 收口）

1. `python -m pytest -q` 全量通过。
2. `python scripts/check_llm_qualification.py` 退出码 `0`，且报告中"证据引用被作废"为 `0`。
3. 闭环演示中 `GET /api/runs/{run_id}` 的 9 条 `llm` 规则 `detail.judged_by == "llm"`。
4. **停掉模型端点后重跑工具 5**：规则进入 `needs_review(MODEL_UNAVAILABLE)`，回写被 `WRITEBACK_POLICY_DENIED` 挡住（Task 6 Step 4 的实测输出）。
5. 批次声明与执行模型不一致时**显式失败**（Task 4 的校验在 REST 与 MCP 两条路径上都有测试）。
6. `.env` 三项留空时，`scripts/verify_m5.py` / `verify_m6.py` 仍以退出码 `0` 通过 —— "零配置可运行"未被回退。
