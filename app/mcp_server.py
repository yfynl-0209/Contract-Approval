"""七个工具的 **MCP 形态**（M7 / Task 6）。

## 本模块只做两件事：运输与错误翻译

业务判断**一行都没有**：每个工具就是"解出身份 → 开事务 → 调
`app/tool_facade` → 返回 dict"。REST 与 MCP 因此共用同一套业务实现 ——
若各自演一遍，"业务逻辑"就有了两份，而它们的分叉方式是"某一边忘了改"：
两种协议对同一份输入给出不同结论，且没有任何一处看得出这是分叉。

    app/api/tools.py   （REST） ─┐
                                 ├─→ app/tool_facade.py → app/services/*
    app/mcp_server.py  （MCP） ─┘

⚠️ **本模块不得 import `app.api` / `fastapi` / 任何适配器**：

- `app.api`：会把 MCP 形态拖进 HTTP 框架（见 `tests/test_m7_contracts.py`
  的 `test_facade_never_imports_the_http_layer` 同一条理由）；
- 适配器：`tests/test_source_invariants.py::test_composition_root_is_the_only_adapter_importer`
  要求适配器**只在组合根**被接线。MCP 的组合根是 `scripts/run_mcp.py`。

## 身份：**启动即失败，绝不匿名**

MCP 的两条传输的身份来源不同，但都走**同一个** `IdentityProvider` 端口：

| 传输 | 身份从哪来 |
| --- | --- |
| `stdio` | 客户端在启动配置里给的环境变量 → 组成一份请求头 → `resolve()` |
| `streamable-http` | 每个请求的 `Authorization` / 身份请求头 → `resolve()` |

两种来源**只能给一个**（`actor` 或 `identity_provider`）。两个都给时哪个生效
是任意的，而"任意"意味着另一份被**静默忽略** —— 于是运维改了配置却看不到任何变化。
两个都不给则**构造即抛 `AuthConfigurationError`**：一个连"谁能调我"都答不出来的
MCP 服务不该起来（与 M7 Task 1 的生产 `fail-closed` 同一条约定）。

## 错误翻译：`outcome` 是唯一的机器判据

每个工具**总是**返回一个 JSON 对象，形状统一：

| 情况 | 返回什么 | 为什么 |
| --- | --- | --- |
| 业务结论（`blocked` 附件缺失 / `reused` 幂等复用 / `queued` 长任务） | 正常结果，`outcome` 说明结论 | 调用**成功了**，只是结论是"这单做不下去" |
| 可预期业务错误（404 / 409 / 503 / 参数非法） | `{"outcome": "error", "error_code": …, "message": …, "retryable": …}` | 机器码必须原样送达（见下） |
| 代码缺陷（`TypeError` …） | **原样抛出** → SDK 的 `isError=true` | 缺陷不该伪装成业务结论 |

### ⚠️ 为什么**不**用 `ToolError` 表达业务错误

FastMCP 会把**任何**从工具函数抛出的异常重新包装一遍：

```python
raise ToolError(f"Error executing tool {self.name}: {e}")     # tools/base.py
```

于是稳定的 JSON 载荷前面被焊上一句英文前缀，调用方要拿机器码就得先做字符串处理 ——
**而这正是本项目在 `app/api/errors.py` 里已经拒绝过一次的做法**
（把机器码塞进自由文本，等于要求每个调用方对文案做正则）。

`isError` 因此留给**协议层**的失败：工具名不存在、入参不符合 inputSchema
（这些由 SDK 在调用我们的函数**之前**判定）。业务错误的判据是 `outcome == "error"`，
与 `outcome == "blocked"` 是同一套词汇 —— 七种工具因此有统一的结果形状，
调用方（含模型）不需要为"失败"再学一种解析方式。
"""

# ⚠️ **本模块刻意不使用 `from __future__ import annotations`。**
#
# FastMCP 生成工具的参数 schema 时，直接读 `inspect.signature(fn).parameters[*].annotation`
# 并调用 `issubclass(annotation, Context)`（`mcp/server/fastmcp/tools/base.py`）。
# 延迟注解（PEP 563）会把注解变成**字符串**，于是它在**注册工具时**就抛
# `TypeError: issubclass() arg 1 must be a class` —— 服务根本起不来。
# 本项目其他模块都用延迟注解（为了前向引用与可读性），这里是一处**必须的例外**，
# 且它是"起不来"而不是"悄悄用了错误的 schema"，因此不会有人误以为它已经生效。
#
# 代价只有一处：注解里不能写尚未定义的名字（本模块没有这种情况）。

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from mcp.server.fastmcp import FastMCP
from sqlalchemy.orm import Session

from app import tool_facade
from app.auth import Actor, AuthConfigurationError, AuthenticationError
from app.context import (
    CORRELATION_ID_HEADER,
    correlation_scope,
    is_valid_correlation_id,
)
from app.db import SessionLocal, transactional_session
from app.errors import AppError
from app.ports.approval_gateway import ApprovalReadGateway
from app.ports.identity_provider import IdentityProvider
from app.ports.llm_gateway import LLMGateway
from app.ports.object_storage import ObjectStorage
from app.schemas import ParseOptions
from app.services.result_service import ResultInputError

__all__ = [
    "DEFAULT_PORT",
    "SUPPORTED_TRANSPORTS",
    "bind_request_headers",
    "build_mcp_server",
]

#: HTTP 传输默认监听端口。**刻意不是 8000**：REST 的默认端口是 8000，
#: 两者撞上时后起的那个会静默失败（"端口被占用"看起来像"服务起不来"）。
DEFAULT_PORT = 8765

#: 支持的传输。`sse` 保留是因为 MCP 客户端生态里仍有只支持它的实现，
#: 但它**不是**需求要求的两种之一，只是顺带可用。
SUPPORTED_TRANSPORTS: tuple[str, ...] = ("stdio", "streamable-http", "sse")

#: HTTP 传输时，每个请求的请求头（stdio 下始终为 `None`）。
#:
#: ⚠️ 用 contextvar 而不是"启动时解一次"：HTTP 端点是被**多个调用方**共用的，
#: 启动时解一次等于让所有人共用第一个请求的身份 —— 那是**越权**，
#: 而且从响应上看不出任何异常。
_HEADERS: ContextVar[Mapping[str, str] | None] = ContextVar(
    "mcp_request_headers", default=None
)

#: `transactional_session` 是生成器函数；套上 `contextmanager` 就是请求级事务。
#:
#: ⚠️ **必须复用同一份实现**（`app/db.py`）：REST 侧"业务失败也提交"这条语义
#: （失败记录要留下）是刻意的，MCP 侧另写一份时，分叉方式是
#: "某天给其中一边加了回滚规则" —— 而另一边不报错，只是失败变得无迹可查。
_scoped_session = contextmanager(transactional_session)


@contextmanager
def bind_request_headers(headers: Mapping[str, str]) -> Iterator[None]:
    """在 `with` 块内把请求头暴露给工具函数（HTTP 传输用）。

    ⚠️ 退出时**必须**重置：contextvar 会随线程/任务复用而残留，
    残留的表现是"下一个请求用上一个请求的身份"——**越权**，且不报错。
    """
    token: Token[Mapping[str, str] | None] = _HEADERS.set(dict(headers))
    try:
        yield
    finally:
        _HEADERS.reset(token)


def build_mcp_server(
    *,
    gateway: ApprovalReadGateway,
    storage: ObjectStorage,
    engine_version: str,
    actor: Actor | None = None,
    identity_provider: IdentityProvider | None = None,
    session_factory: Callable[[], Session] = SessionLocal,
    llm: LLMGateway | None = None,
    name: str = "contract-approval-system",
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
) -> FastMCP:
    """组装 MCP 服务，注册**恰好七个**工具。

    Args:
        actor: 进程级身份（stdio 传输：客户端配置里的身份）。
        identity_provider: 逐请求身份（HTTP 传输：从请求头解）。
        engine_version: 解析引擎版本（工具 4 要用）。由**组合根**给出：
            它来自具体解析适配器，而这个模块不得 import 适配器。
        gateway / storage: 端口实现。同样由组合根注入。

    Raises:
        AuthConfigurationError: 两种身份来源都没给 —— **拒绝构造**，
            而不是构造一个"谁都能调"的服务。
        ValueError: 两种身份来源都给了（哪一份生效是任意的）。
    """
    if actor is not None and identity_provider is not None:
        raise ValueError(
            "MCP 身份来源只能给一个：同时给 actor 与 identity_provider 时，"
            "哪一份生效是任意的 —— 于是被忽略的那一份改了也看不出任何变化。"
            "stdio 用 actor，streamable-http 用 identity_provider。"
        )
    if actor is None and identity_provider is None:
        raise AuthConfigurationError(
            "MCP 服务没有身份来源：stdio 传输必须在启动配置里给出身份，"
            "streamable-http 传输必须配置身份提供方。"
            "**拒绝以匿名方式提供服务** —— 一个连「谁能调我」都答不出来的 MCP 服务，"
            "任何能连上它的人都可以自称系统管理员。"
        )

    server = FastMCP(name, host=host, port=port, instructions=(
        "合同审批审查工具服务。七个工具的名称与最低参数与需求 2.4.10 逐字一致。\n"
        "系统只生成风险审查意见，**不代替人工作出审批通过或驳回决定**。"
    ))

    def resolve_actor() -> Actor:
        """解出本次调用的身份。

        Raises:
            AuthenticationError: HTTP 传输下请求没有可用凭据，
                或 stdio 传输下进程身份缺失（后者在构造时已被拦下）。
        """
        if actor is not None:
            return actor
        headers = _HEADERS.get()
        if headers is None:
            raise AuthenticationError(
                "这次 MCP 调用没有可用的身份来源：stdio 传输必须由启动配置给出身份，"
                "HTTP 传输必须带凭据请求头"
            )
        assert identity_provider is not None  # 构造时已校验
        return identity_provider.resolve(headers)

    def invoke(action: Callable[[Session, Actor], dict[str, Any]]) -> dict[str, Any]:
        """一次工具调用的**全部**公共部分：身份、事务、关联 ID、错误翻译。

        ⚠️ 身份解析也在 `_guard` 内：它可能抛 `AuthenticationError`
        （HTTP 传输下没带凭据），而那同样是一条要给调用方看的**稳定判据**。
        放在外面时它会以 SDK 包装过的英文前缀返回，机器码就此丢失。
        """
        # 关联 ID：HTTP 传输优先用调用方给的那个（与 REST 中间件同一约定），
        # 其余情况生成一个 —— 否则 MCP 侧的日志在 `task_logs` 里
        # 与"同一次审批的全链路"接不上。
        headers = _HEADERS.get() or {}
        supplied = headers.get(CORRELATION_ID_HEADER)
        with correlation_scope(
            supplied if is_valid_correlation_id(supplied) else None
        ):
            with _scoped_session(session_factory()) as session:
                return _guard(lambda: action(session, resolve_actor()))

    # ------------------------------------------------------------
    # 工具 1–7：名称与最低参数与需求 2.4.10 逐字一致
    # ------------------------------------------------------------

    @server.tool(
        name="list_pending_contract_approvals",
        description="拉取待审批合同（工具 1）。重复拉取无害：已存在的任务只刷新字段。",
    )
    def list_pending_contract_approvals(limit: int = 20) -> dict[str, Any]:
        return invoke(
            lambda session, actor: tool_facade.list_pending_contract_approvals(
                limit, session=session, gateway=gateway, actor=actor
            )
        )

    @server.tool(
        name="get_contract_approval",
        description="查询审批单详情（工具 2），并同步权威审查上下文与附件元数据。",
    )
    def get_contract_approval(instance_id: str) -> dict[str, Any]:
        return invoke(
            lambda session, actor: tool_facade.get_contract_approval(
                instance_id, session=session, gateway=gateway, actor=actor
            )
        )

    @server.tool(
        name="download_contract_attachment",
        description=(
            "下载合同附件（工具 3）。附件本身有问题时返回 outcome=blocked ——"
            "那是业务结论，不是调用失败。"
        ),
    )
    def download_contract_attachment(
        instance_id: str, attachment_id: str, file_name: str | None = None
    ) -> dict[str, Any]:
        return invoke(
            lambda session, actor: tool_facade.download_contract_attachment(
                instance_id,
                attachment_id,
                file_name,
                session=session,
                gateway=gateway,
                storage=storage,
                actor=actor,
            )
        )

    @server.tool(
        name="parse_contract_document",
        description=(
            "解析合同文档（工具 4，**长任务**）。立即返回可轮询的 task_ref；"
            "`document_id` 是本系统主键（approval_attachments.id），不是外部附件编号。"
        ),
    )
    def parse_contract_document(
        document_id: str, parse_options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        options = (
            None if parse_options is None else ParseOptions.model_validate(parse_options)
        )
        return invoke(
            lambda session, actor: tool_facade.parse_contract_document(
                document_id,
                session=session,
                engine_version=engine_version,
                actor=actor,
                parse_options=options,
            )
        )

    @server.tool(
        name="run_contract_rules",
        description=(
            "执行合同规则审查（工具 5，**长任务**）。`case_id` 是解析编号"
            "（contract_parses.id）—— 与工具 6 的同名参数指向不同对象。"
        ),
    )
    def run_contract_rules(case_id: str, force: bool = False) -> dict[str, Any]:
        return invoke(
            lambda session, actor: tool_facade.run_contract_rules(
                case_id, session=session, actor=actor, force=force, llm=llm
            )
        )

    @server.tool(
        name="save_review_result",
        description=(
            "保存审查结果（工具 6，同步）。`case_id` 是批次编号（review_runs.id）。"
            "风险等级必须与批次聚合一致，否则被拒（RESULT_INPUT_MISMATCH）。"
        ),
    )
    def save_review_result(
        case_id: str,
        overall_risk_level: str,
        summary_text: str,
        focus_points_json: list[str] | str,
        comment_text: str,
    ) -> dict[str, Any]:
        # ⚠️ 这个参数**不是** `str`，尽管需求 2.4.10 把它定义为 JSON 字符串。
        #
        # FastMCP 在**校验之前**会做一次 JSON 预解析
        # （`utilities/func_metadata.py::pre_parse_json`，为的是兼容"把数组当 JSON
        # 字符串传"的客户端）：任何**能解析成 JSON 的字符串**都会被换成解析后的值。
        # 于是声明成 `str` 时，按需求传 `"[]"` 会被换成列表 `[]`，
        # 再按 `str` 校验 → `isError=true`，**按需求写的调用方永远调不通**。
        #
        # 因此这里收两种形态并在边界归一：字符串（需求形态）原样交给门面，
        # 列表（模型自然会产出的形态）序列化一次。参数**名字**仍是需求那一个。
        focus_points = (
            focus_points_json
            if isinstance(focus_points_json, str)
            else json.dumps(list(focus_points_json), ensure_ascii=False)
        )
        return invoke(
            lambda session, actor: tool_facade.save_review_result(
                case_id,
                overall_risk_level,
                summary_text,
                focus_points,
                comment_text,
                session=session,
                actor=actor,
            )
        )

    @server.tool(
        name="write_approval_comment",
        description=(
            "回写审批意见（工具 7）。返回时**评论还没写出去** ——"
            "本地只登记意图，送达由 Outbox 派发器负责；门禁拒绝时 outcome=blocked。"
        ),
    )
    def write_approval_comment(instance_id: str, review_id: str) -> dict[str, Any]:
        return invoke(
            lambda session, actor: tool_facade.write_approval_comment(
                instance_id, review_id, session=session, actor=actor
            )
        )

    return server


# ============================================================
# 错误翻译
# ============================================================


def _guard(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """调一次门面，把**可预期的业务错误**翻成稳定的错误载荷。

    ⚠️ 只翻三类：`AppError`、`ResultInputError`、
    以及本仓库对"调用方参数错误"的既有约定 `ValueError`。
    代码缺陷（`TypeError`、`KeyError`）**原样抛出** —— 让 SDK 把它标成
    `isError=true`，而不是伪装成一次正常的业务拒绝。
    """
    try:
        return call()
    except (AppError, ResultInputError, ValueError) as exc:
        return _error_payload(exc)


def _error_payload(exc: AppError | ResultInputError | ValueError) -> dict[str, Any]:
    """错误 → 与成功结果**同一形状**的载荷（`outcome="error"` + 稳定机器码）。

    ⚠️ `ResultInputError` **必须单独判、且排在 `ValueError` 之前**：
    它是 `ValueError` 的子类，落进兜底分支后原因码会被换成
    `INVALID_ARGUMENT` —— 于是 REST 答 `RESULT_RUN_NOT_COMPLETED`（等它跑完再来），
    MCP 答 `INVALID_ARGUMENT`（改参数），**两种协议对同一次调用给出相反的处置方向**。
    这正是"REST 与 MCP 必须返回同一套业务结果语义"要防的那件事。
    """
    if isinstance(exc, ResultInputError):
        return {
            "outcome": "error",
            "error_code": exc.reason_code,
            "message": str(exc),
            # 三条原因码说的都是"**先改变点什么**再回来"
            # （等批次跑完 / 核对 id / 改正参数），原样重发没有意义。
            # 与 `app/api/errors.py::result_error_body` 逐字一致。
            "retryable": False,
        }

    if isinstance(exc, AppError):
        code, message, retryable = str(exc.code), exc.message, exc.retryable
    else:
        # 其余 `ValueError`：调用方传错了参数（id 不是数字串、关注点不是 JSON 数组）。
        # 与 REST 侧的 `ValueError` 处理器给出**同一个**码。
        code, message, retryable = "INVALID_ARGUMENT", str(exc), False

    return {
        "outcome": "error",
        "error_code": code,
        "message": message,
        "retryable": retryable,
    }
