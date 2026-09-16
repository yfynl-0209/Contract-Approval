#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MCP 服务启动入口（**组合根**）。

## 为什么身份在这里解，而不是在 `app/mcp_server.py`

`app/mcp_server.py` 不得 import 任何适配器
（`tests/test_source_invariants.py::test_composition_root_is_the_only_adapter_importer`），
而"用哪个身份适配器、字节存在哪里、解析引擎是什么版本"全是**接线决定** ——
它们只有一处该知道：组合根。与 `scripts/run_worker.py` 同一条约定。

## 两条传输

```powershell
# 1) stdio：给本机 MCP 客户端（Claude Desktop / IDE 插件）用
$env:MCP_ACTOR_ID = "legal-zhang"; $env:MCP_ACTOR_ROLES = "legal_reviewer"
.venv\\Scripts\\python.exe scripts/run_mcp.py --transport stdio

# 2) streamable-http：给能发 HTTP 的客户端用（身份逐个请求解）
.venv\\Scripts\\python.exe scripts/run_mcp.py --transport streamable-http --port 8765
```

| 传输 | 身份来源 | 为什么不同 |
| --- | --- | --- |
| `stdio` | **进程环境变量** → 组成请求头 → `resolve()` | 没有 HTTP 请求可读；客户端配置里本来就在传环境变量 |
| `streamable-http` | **每个请求的头** → `resolve()` | 端点被多个调用方共用，"启动时解一次"= 所有人共用第一个人的身份（**越权**） |

⚠️ 两条路径都走**同一个** `IdentityProvider` 端口：身份适配器只换地方用，
不换一套实现。否则"dev 模式下谁是管理员"的判据会出现两份。

## 配置（环境变量）

| 变量 | 用途 |
| --- | --- |
| `MCP_ACTOR_ID` | stdio 传输的身份标识（必填，缺失则**拒绝启动**） |
| `MCP_ACTOR_NAME` | 显示名（缺省等于 id） |
| `MCP_ACTOR_ROLES` | 角色声明，逗号或空白分隔（决定权限） |
| `MCP_TENANT_ID` | 租户（缺省用配置里的默认租户） |
| `MCP_AUTHORIZATION` | `AUTH_MODE=jwt` 时用：`Bearer <令牌>` |

⚠️ 这些变量与 `AUTH_MODE` **配套**：`AUTH_MODE=dev` 时读 `MCP_ACTOR_*`，
`AUTH_MODE=jwt` 时读 `MCP_AUTHORIZATION`。生产环境仍然由
`assert_auth_configuration` 拦住"用请求头冒名"这条路径。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

# 直接以 `python scripts/run_mcp.py` 运行时，sys.path[0] 是 scripts/，
# 因此显式加入项目根目录（与 run_worker.py / check_rules.py 同一约定）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.approval.mock_approval_gateway import MockApprovalGateway  # noqa: E402
from app.adapters.auth.dev_header_identity import (  # noqa: E402
    HEADER_ACTOR_ID,
    HEADER_ACTOR_NAME,
    HEADER_ACTOR_ROLES,
    HEADER_TENANT_ID,
)
from app.adapters.parse.pymupdf_extractor import ENGINE_VERSION  # noqa: E402
from app.adapters.storage.local_file_storage import LocalFileStorage  # noqa: E402
from app.api.deps import build_identity_provider  # noqa: E402
from app.auth import Actor, AuthConfigurationError  # noqa: E402
from app.config import settings  # noqa: E402
from app.mcp_server import (  # noqa: E402
    DEFAULT_PORT,
    SUPPORTED_TRANSPORTS,
    bind_request_headers,
    build_mcp_server,
)

#: 环境变量名。**显式列出来**，让"客户端该配什么"有一个可引用的清单，
#: 而不是散在几处 `os.environ.get(...)` 里。
ENV_ACTOR_ID = "MCP_ACTOR_ID"
ENV_ACTOR_NAME = "MCP_ACTOR_NAME"
ENV_ACTOR_ROLES = "MCP_ACTOR_ROLES"
ENV_TENANT_ID = "MCP_TENANT_ID"
ENV_AUTHORIZATION = "MCP_AUTHORIZATION"


def identity_headers(environ: Mapping[str, str]) -> dict[str, str]:
    """把 MCP 客户端给的环境变量翻成**一份请求头**。

    走请求头而不是直接构造 `Actor`，是为了让 stdio 与 HTTP 两条路径
    经过**同一个**身份适配器：`resolve()` 里的校验（角色声明、租户一致性、
    "缺身份必须拒绝"）因此只有一份实现。
    """
    headers: dict[str, str] = {}
    for header, key in (
        (HEADER_ACTOR_ID, ENV_ACTOR_ID),
        (HEADER_ACTOR_NAME, ENV_ACTOR_NAME),
        (HEADER_ACTOR_ROLES, ENV_ACTOR_ROLES),
        (HEADER_TENANT_ID, ENV_TENANT_ID),
    ):
        value = environ.get(key)
        if value is not None and value.strip():
            headers[header] = value.strip()
    return headers


def resolve_stdio_actor(
    environ: Mapping[str, str], *, provider=None
) -> Actor:
    """stdio 传输的**进程级身份**。

    Raises:
        AuthConfigurationError: 没有给出任何身份配置。
            ⚠️ 刻意不是 `AuthenticationError`：后者会被翻成"这次请求没带凭据"，
            而真正的问题是**启动配置缺了东西** —— 那件事调用方做什么都没用，
            必须让进程在开始服务之前就失败。
    """
    headers = identity_headers(environ)
    authorization = (environ.get(ENV_AUTHORIZATION) or "").strip()
    if authorization:
        headers["Authorization"] = authorization

    if not headers:
        raise AuthConfigurationError(
            f"stdio 传输缺少身份配置：请至少设置 {ENV_ACTOR_ID}"
            f"（AUTH_MODE=dev）或 {ENV_AUTHORIZATION}（AUTH_MODE=jwt）。"
            "**拒绝以匿名方式提供服务** —— 一个连「谁能调我」都答不出来的"
            " MCP 服务，任何能启动它的人都可以自称系统管理员。"
        )

    return (provider or build_identity_provider()).resolve(headers)


def build_http_app(server):
    """给 streamable-HTTP 应用套一层"把请求头绑进上下文"的中间件。

    ⚠️ 每请求都要绑定并**在用完后重置**：contextvar 会随任务复用残留，
    残留的表现是"下一个请求沿用了上一个请求的身份" —— 那是**越权**，
    而且从响应上看不出任何异常。

    真正的身份解析发生在 `app/mcp_server.py` 的 `resolve_actor()` 里 ——
    中间件只负责"把这一堆键值对交到那一步能看见的地方"。
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    app = server.streamable_http_app()

    class _BindRequestHeaders(BaseHTTPMiddleware):  # type: ignore[misc]
        async def dispatch(self, request, call_next):
            with bind_request_headers(dict(request.headers)):
                return await call_next(request)

    app.add_middleware(_BindRequestHeaders)
    return app


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合同审批审查系统的 MCP 服务（组合根）")
    parser.add_argument(
        "--transport",
        choices=list(SUPPORTED_TRANSPORTS),
        default="stdio",
        help=(
            "传输方式（默认 stdio）。⚠️ 需求要求的是 stdio 与 streamable-http；"
            "sse 只是顺带保留（部分旧客户端只支持它）"
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 传输的监听地址")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="HTTP 传输的监听端口"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    provider = build_identity_provider()
    gateway = MockApprovalGateway()
    storage = LocalFileStorage()

    try:
        if args.transport == "stdio":
            # stdio：进程级身份，构造时就解出来 —— 解不出来则**起不来**。
            actor = resolve_stdio_actor(os.environ, provider=provider)
            server = build_mcp_server(
                actor=actor,
                gateway=gateway,
                storage=storage,
                engine_version=ENGINE_VERSION,
            )
            print(
                f"[mcp] stdio 启动（身份 id={actor.actor_id}，"
                f"角色={'、'.join(sorted(actor.roles)) or '（无）'}，"
                f"租户={actor.tenant_id}）",
                flush=True,
            )
            server.run(transport="stdio")
            return 0

        server = build_mcp_server(
            identity_provider=provider,
            gateway=gateway,
            storage=storage,
            engine_version=ENGINE_VERSION,
            host=args.host,
            port=args.port,
        )
        app = build_http_app(server)

        import uvicorn

        print(
            f"[mcp] {args.transport} 启动：http://{args.host}:{args.port}"
            f"{server.settings.streamable_http_path}（身份逐个请求从请求头解析）",
            flush=True,
        )
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    finally:
        gateway.close()
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
