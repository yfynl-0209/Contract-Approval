"""FastAPI 依赖：审批网关、对象存储与**身份**的装配点。

适配器是**长生命周期**的：`httpx.Client` 内部维护连接池，每请求新建会重复握手 TLS。
因此缓存在 `app.state` 上跨请求复用，进程退出时由 lifespan 统一关闭。

**惰性构造**而不是只靠 lifespan：`TestClient(app)` 不写 `with` 时不触发 lifespan，
只依赖 lifespan 初始化会拿到 `None` 并崩溃。

测试用 `app.dependency_overrides[get_gateway] = lambda: fake` 替换实现，业务代码不用改。
身份同理：`app.dependency_overrides[get_actor] = lambda: some_actor` ——
**不需要伪造请求头**，因为覆盖的是依赖本身。

## 本模块是**组合根**

`AUTH_MODE` → 具体身份适配器的选择只在这里发生（`_build_identity_provider`）。
业务代码只依赖 `app.ports.identity_provider.IdentityProvider`，
因此 M9 换成 OIDC、或接入企业 SSO 时，改的是这一处，不是每个端点。
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, Request

from app.adapters.approval.mock_approval_gateway import MockApprovalGateway
from app.adapters.auth.dev_header_identity import DevHeaderIdentity
from app.adapters.auth.jwt_identity import JwtIdentity
from app.adapters.storage.local_file_storage import LocalFileStorage
from app.auth import (
    Actor,
    AuthConfigurationError,
    AuthMode,
    Permission,
    require,
)
from app.config import settings
from app.ports.approval_gateway import ApprovalReadGateway
from app.ports.identity_provider import IdentityProvider
from app.ports.object_storage import ObjectStorage

#: 会话依赖定义在 `app/db.py`（它同时管理事务边界）。这里转出，
#: 让接口层只从 `app.api.deps` 取依赖，不必知道每个依赖住在哪。
from app.db import get_db as get_db  # noqa: PLC0414  (显式转出)

__all__ = [
    "build_identity_provider",
    "get_actor",
    "get_db",
    "get_gateway",
    "get_identity_provider",
    "get_storage",
    "require_permissions",
    "close_adapters",
]


def build_identity_provider() -> IdentityProvider:
    """按 `AUTH_MODE` 选择身份适配器（**组合根**）。

    ⚠️ 未知的 `AUTH_MODE` **必须抛错**，不能兜底成开发期适配器：
    兜底方向若选错，一个拼错的 `AUTH_MODE=jwt `（带空格）会静默降级成
    "无条件信任请求头" —— 而服务照常启动、照常响应，没人会注意到。
    """
    mode = settings.auth_mode.strip().lower()

    if mode == AuthMode.JWT.value:
        return JwtIdentity(
            tenant_id=settings.tenant_id,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            public_key=settings.jwt_public_key,
            jwks_url=settings.jwt_jwks_url,
            algorithms=settings.jwt_algorithm_list,
            subject_claim=settings.jwt_subject_claim,
            name_claim=settings.jwt_name_claim,
            roles_claim=settings.jwt_roles_claim,
            tenant_claim=settings.jwt_tenant_claim,
            leeway_seconds=settings.jwt_leeway_seconds,
        )

    if mode == AuthMode.DEV_HEADER.value:
        return DevHeaderIdentity(tenant_id=settings.tenant_id, env=settings.env)

    raise AuthConfigurationError(
        f"未知的 AUTH_MODE={settings.auth_mode!r}："
        f"只能是 {AuthMode.DEV_HEADER.value!r}（开发期，读请求头）"
        f"或 {AuthMode.JWT.value!r}（生产）。"
        "**不提供兜底默认值** —— 拼错配置时应当起不来，而不是默默换一种身份来源。"
    )


def get_identity_provider(request: Request) -> IdentityProvider:
    """按应用生命周期复用的身份提供方。"""
    provider = getattr(request.app.state, "identity_provider", None)
    if provider is None:
        provider = build_identity_provider()
        request.app.state.identity_provider = provider
    return provider


def get_actor(
    request: Request,
    provider: IdentityProvider = Depends(get_identity_provider),
) -> Actor:
    """当前请求的**已认证主体**。

    ⚠️ 解析失败时抛 `AuthenticationError` → **401**（不是 400、不是 500）。
    把它包成 400 会让"去换一份身份"这个正确处置变成"去改请求参数"。

    签名从 M6 的 `() -> str` 变成 `(request) -> Actor`，
    但工具 6/7 的 `Depends(get_actor)` **一行都没改** ——
    这正是 M6 把它做成依赖而不是模块级常量的理由。

    ## 顺手写进 `request.state`

    关联 ID 中间件、异常处理器都**跑在这个依赖之外**（中间件在最外层，
    异常处理器在依赖之外被调用），它们拿不到 `Actor` 对象。
    `request.state` 是两者都能看到的那一处，因此在这里写一次。

    ⚠️ **只放 `Actor`，不放原始请求头**：请求头里带着 `Authorization`，
    把它挂到 `request.state` 上，等于给未来任何一处日志/序列化
    留了一条把**令牌原文**写出去的路。`Actor` 是解析后的结果，
    里面没有任何凭据。
    """
    actor = provider.resolve(request.headers)
    request.state.actor = actor
    return actor


def require_permissions(*permissions: Permission) -> Callable[..., Actor]:
    """生成一个"要求指定权限"的依赖。

    用法：`actor: Actor = Depends(require_permissions(Permission.RESULT_SAVE))`。

    路由声明**权限**而不是角色（见 `app/auth.py` 的模块 docstring）；
    无参数表示"只要是**已认证**主体"—— 用它把某个端点从匿名改为需登录，
    而不额外要求具体权限。

    ⚠️ 判断本身在 `app/auth.py::require`，这里只做"把它接成 FastAPI 依赖"。
    在同一处写第二份判断，会让 REST 与 MCP 两个形态的拒绝消息**分叉** ——
    而分叉的那一份迟早会漏掉"角色名未被识别"这条线索。
    """

    def dependency(actor: Actor = Depends(get_actor)) -> Actor:
        return require(actor, *permissions)

    return dependency


def get_gateway(request: Request) -> ApprovalReadGateway:
    """按应用生命周期复用的审批系统网关。"""
    gateway = getattr(request.app.state, "approval_gateway", None)
    if gateway is None:
        gateway = MockApprovalGateway()
        request.app.state.approval_gateway = gateway
    return gateway


def get_storage(request: Request) -> ObjectStorage:
    """按应用生命周期复用的对象存储。"""
    storage = getattr(request.app.state, "object_storage", None)
    if storage is None:
        storage = LocalFileStorage()
        request.app.state.object_storage = storage
    return storage


def get_parser_engine_version() -> str:
    """解析引擎版本（进 `parser_version`，进而进 `cache_key`）。

    ⚠️ 适配器在**组合根**被 import —— 这是本项目"服务层不得依赖适配器"的
    唯一例外点，也正是它存在的理由：**只有组合根知道用了哪个实现**。
    让 `parse_service` 自己去问 `PyMuPDF` 的版本，服务层就绑死了实现，
    M9 换引擎会变成改业务代码。
    """
    from app.adapters.parse.pymupdf_extractor import ENGINE_VERSION

    return ENGINE_VERSION


def close_adapters(app: FastAPI) -> None:
    """关闭长生命周期适配器（由 lifespan 在进程退出时调用）。

    只关**已经构造过**的实例：惰性构造意味着进程可能从未用过网关（如只访问了 `/health`）。
    """
    gateway = getattr(app.state, "approval_gateway", None)
    if gateway is not None:
        gateway.close()

    # 身份提供方：JWT 实现持有 JWKS 客户端（连接池）
    provider = getattr(app.state, "identity_provider", None)
    if provider is not None:
        provider.close()
