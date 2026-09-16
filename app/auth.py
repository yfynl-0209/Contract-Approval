"""身份与授权的**协议中立**核心（M7）。

## 为什么这一层不认识 FastAPI

REST 与 MCP **必须**执行同一套授权，而 MCP 形态不经过 FastAPI 的路由依赖。
把授权写成 FastAPI 依赖是最省事的做法，代价是 MCP 只能"再实现一遍"——
于是两套协议的权限语义迟早分叉：同一个角色在 REST 下能回写、
在 MCP 下被拒（或反过来）。而**分叉的权限比没有权限更危险**，
因为它会让人以为"这里已经管住了"。

所以本模块只放**纯判定**：角色 → 权限映射、`Actor`、`has_permissions`、
以及生产配置的 fail-closed 校验。协议相关的部分（HTTP 401/403 与依赖注入）
在 `app/api/deps.py`。

## 权限而不是角色

路由声明**权限**（`require_permissions(Permission.RESULT_SAVE)`），不判断角色字符串。
判角色字符串会让"哪些角色能保存结果"这个决定散落在每个端点上，
新增一个角色时要靠搜索才能改全 —— 漏掉的那一处不会报错，只是**少拦了一次**。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from app.enums import ErrorCode
from app.errors import PermanentError, TransientError


class Role(StrEnum):
    """企业侧角色（需求 §6「权限」：法务审核人 / 系统管理员 / 只读审计）。

    ⚠️ v1 **不建 users 表**：角色来自身份提供方的令牌声明，
    而不是本地用户表。因此"这个人是什么角色"的真相在 IdP 那边，
    本系统只负责**按声明授权**。
    """

    LEGAL_REVIEWER = "legal_reviewer"
    SYSTEM_ADMIN = "system_admin"
    READ_ONLY_AUDITOR = "read_only_auditor"


class Permission(StrEnum):
    """授权的最小单位。**路由声明它，而不是声明角色。**"""

    TASK_READ = "task:read"
    REVIEW_EXECUTE = "review:execute"
    RESULT_SAVE = "result:save"
    RESULT_CONFIRM = "result:confirm"
    WRITEBACK_EXECUTE = "writeback:execute"
    RULE_MANAGE = "rule:manage"
    OPS_RETRY = "ops:retry"
    AUDIT_READ = "audit:read"


#: 只读审计：**只能看**。刻意不含任何 `*:execute` / `*:save` 类权限 ——
#: "只读"这个角色名本身就是它的全部授权，多给一条就名不副实。
_AUDITOR_PERMISSIONS = frozenset(
    {
        Permission.TASK_READ,
        Permission.AUDIT_READ,
    }
)

#: 法务审核人：审查链路上的全部**业务动作**，但不含规则管理与运维重试。
#: 规则是"系统怎么判"的输入，改规则能间接改变所有合同的结论 ——
#: 因此它与"审这份合同"不是同一个权限层级。
_REVIEWER_PERMISSIONS = frozenset(
    {
        Permission.TASK_READ,
        Permission.REVIEW_EXECUTE,
        Permission.RESULT_SAVE,
        Permission.RESULT_CONFIRM,
        Permission.WRITEBACK_EXECUTE,
    }
)

#: 系统管理员：审核人的全部权限 + 规则管理 + 运维重试 + 审计查询。
_ADMIN_PERMISSIONS = _REVIEWER_PERMISSIONS | frozenset(
    {
        Permission.RULE_MANAGE,
        Permission.OPS_RETRY,
        Permission.AUDIT_READ,
    }
)

#: 角色 → 权限集。**只读**（`MappingProxyType`）：运行时改这张表
#: 等于在不重启的情况下放开权限，而那种改动不会留下任何审计痕迹。
ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = MappingProxyType(
    {
        Role.LEGAL_REVIEWER: _REVIEWER_PERMISSIONS,
        Role.SYSTEM_ADMIN: _ADMIN_PERMISSIONS,
        Role.READ_ONLY_AUDITOR: _AUDITOR_PERMISSIONS,
    }
)


@dataclass(frozen=True, slots=True)
class Actor:
    """一次请求背后**已认证**的主体。

    ⚠️ 冻结（`frozen=True`）：身份一旦解出就不允许在请求处理途中被改写。
    可变身份会让"这个动作是谁做的"变成一个取决于**读取时机**的问题。

    `roles` 保存的是令牌里**原样**的角色声明（不是映射后的枚举），
    这样审计与排障能看到"当初到底声明了什么"，而不是我们**理解成了什么**。
    """

    actor_id: str
    display_name: str
    roles: frozenset[str]
    tenant_id: str

    @property
    def permissions(self) -> frozenset[Permission]:
        """本主体实际拥有的权限：**已识别角色**的并集。

        ⚠️ 未识别的角色**被丢弃**（而不是报错），于是它**不会**带来任何权限。
        这是刻意的 fail-closed 方向：IdP 先上了新角色、本系统还没发布对应映射时，
        该角色的人**少一些权限**（看得见的 403），
        而不是"被当成有权限"或"整个服务拒绝所有人登录"。
        被丢弃的角色仍留在 `unknown_roles` 里，便于排障时回答"为什么我被拒了"。
        """
        return permissions_for(self.roles)

    @property
    def unknown_roles(self) -> frozenset[str]:
        """令牌里声明了、但本系统不认识的角色的角色名。"""
        return frozenset(role for role in self.roles if not _is_known_role(role))

    def has(self, *permissions: Permission) -> bool:
        """是否**同时**具备所列权限。无参数表示"只要是已认证主体"。

        ⚠️ 语义是"全部满足"而不是"满足其一"：写成"其一"时，
        `require_permissions(A, B)` 会被读成"要 A 或 B"，
        而调用方想说的几乎总是"要 A 且 B"。
        """
        granted = self.permissions
        return all(permission in granted for permission in permissions)

    def __str__(self) -> str:
        """给人看的表示（日志行）。**机器判据请用 `actor_id`。**"""
        return self.display_name


def _is_known_role(role: str) -> bool:
    """角色名是否是本系统认识的角色。

    `Role(role)` 对未知值抛 `ValueError`，因此必须**先判断再转换** ——
    直接 `Role(role)` 会让一个未知角色变成 500，
    而正确的表现是"这个身份少一些权限"。
    """
    try:
        Role(role)
    except ValueError:
        return False
    return True


def has_permissions(actor: Actor, *permissions: Permission) -> bool:
    """`Actor.has` 的函数形式，供**组合与测试**使用（含 MCP 形态）。"""
    return actor.has(*permissions)


def permissions_for(roles: Iterable[str]) -> frozenset[Permission]:
    """角色名集合 → 权限并集（未识别角色静默丢弃，同 `Actor.permissions`）。"""
    granted: set[Permission] = set()
    for role in roles:
        if _is_known_role(role):
            granted |= ROLE_PERMISSIONS[Role(role)]
    return frozenset(granted)


# ============================================================
# 认证 / 授权失败
# ============================================================
# 两者**必须**分开，因为调用方的正确处置完全不同：
#
# | 情况 | 状态码 | 处置 |
# | --- | --- | --- |
# | 没带令牌 / 令牌无效 / 过期 | 401 | **去拿一份新的身份**再来 |
# | 身份有效但没有这个权限 | 403 | 换个账号，或找管理员加权限 |
#
# 合并成一个码（或一律 403）时，"我没登录"与"我登录了但没权限"
# 在客户端看起来一样 —— 于是客户端的处置是二分之一的概率做错。


class AuthenticationError(PermanentError):
    """本次请求**没有可信身份** → 401。

    ⚠️ 与 `ErrorCode.AUTH_FAILED` **不是一回事**：那个码的含义是
    "**审批系统**拒绝了我们的凭据"（出站方向，映射 502，属于服务端配置问题）。
    复用它会得到一个荒谬的映射：调用方没带令牌，返回 502"上游凭据有问题"。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.AUTHENTICATION_REQUIRED)


class AuthorizationError(PermanentError):
    """身份可信，但**缺少所需权限** → 403。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.PERMISSION_DENIED)


def require(actor: Actor, *permissions: Permission) -> Actor:
    """校验主体是否具备全部权限；不满足则抛 `AuthorizationError`。

    **这是权限判断的唯一实现**，两处调用它：

    | 调用方 | 为什么需要 |
    | --- | --- |
    | `app/api/deps.py::require_permissions` | REST 形态：在依赖里判，请求进端点前就被拒 |
    | `app/tool_facade.py` 的七个工具 | MCP 形态没有 FastAPI 依赖，门面是它唯一的关口 |

    写两份的代价不是"多几行"，而是两份的**消息格式会分叉** ——
    于是同一个越权请求走 REST 和走 MCP 得到两种不同的解释，
    而其中一种迟早会漏掉"角色名未被识别"这条线索。

    Raises:
        AuthorizationError: 缺少任意一项所需权限。
    """
    if actor.has(*permissions):
        return actor

    missing = "、".join(sorted(str(item) for item in permissions))
    # 报出"实际拥有什么"与"有没有被丢弃的角色"，是为了回答
    # 使用者必然会问的那个问题：**我为什么被拒了？**
    # 只说"缺少 X"时，一个角色名拼错的人会反复确认自己"明明有那个角色"，
    # 而线索（角色名没被识别）就在我们手里却没告诉他。
    detail = (
        f"主体 {actor.actor_id!r} 缺少权限：{missing}；"
        f"当前拥有：{_describe(actor.permissions)}"
    )
    if actor.unknown_roles:
        detail += (
            f"；⚠️ 以下角色声明**未被识别**（因此未带来任何权限）："
            f"{_describe(actor.unknown_roles)}"
        )
    raise AuthorizationError(detail)


def _describe(values: frozenset) -> str:
    """把集合渲染成稳定的可读文本（空集也要说清楚，而不是留白）。"""
    return "、".join(sorted(str(item) for item in values)) if values else "（无）"


class IdentityProviderUnavailable(TransientError):
    """**判断不了**令牌是否有效（JWKS / 内省服务暂时不可达）→ 503 + Retry-After。

    与 `AuthenticationError` 的区别是最要紧的一处：
    前者是"这份令牌无效"，后者是"我现在没法判断"。
    都写成 401 时，调用方会对一次**可能完全有效**的令牌执行登出，
    而正确处置只是稍后重试。
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.IDENTITY_PROVIDER_UNAVAILABLE)


class AuthConfigurationError(RuntimeError):
    """身份配置在生产环境下不可用。

    ⚠️ 刻意**不继承** `AppError`：它是**启动期**错误，不是某个请求的业务错误。
    继承 `AppError` 会让它被异常处理器翻译成一个 HTTP 响应 ——
    于是"生产环境配错了身份"表现为"某个接口偶尔 500"，
    而不是**进程根本起不来**。前者可以带病上线，后者不行。
    """


class AuthMode(StrEnum):
    """身份解析方式。"""

    #: 开发期：读显式请求头。**绝不允许在生产启用**（见 `assert_auth_configuration`）。
    DEV_HEADER = "dev"
    #: 生产：校验 JWT。
    JWT = "jwt"


#: 视为"生产"的环境名。多写几个同义词，是因为漏判的代价是**带病上线**。
PRODUCTION_ENVS: frozenset[str] = frozenset({"production", "prod"})


def is_production(env: str) -> bool:
    """环境名是否表示生产（大小写与空白不敏感）。"""
    return env.strip().lower() in PRODUCTION_ENVS


def assert_auth_configuration(
    *,
    env: str,
    auth_mode: str,
    jwt_public_key: str = "",
    jwt_jwks_url: str = "",
    jwt_issuer: str = "",
    jwt_audience: str = "",
) -> None:
    """生产环境下的身份配置校验。**不通过就抛，绝不降级为匿名访问。**

    参数都是**基本类型**而不是 `Settings`：这样它可以脱离配置单例被测试，
    也不会让本模块反向依赖 `app.config`。

    Raises:
        AuthConfigurationError: 生产环境选择了开发期身份来源，或 JWT 配置不完整。
    """
    if not is_production(env):
        return

    if auth_mode != AuthMode.JWT.value:
        raise AuthConfigurationError(
            f"生产环境（ENV={env!r}）不允许 AUTH_MODE={auth_mode!r}："
            "开发期身份适配器**无条件信任**请求头里的身份，"
            "任何能访问该端口的人都可以自称系统管理员。"
            f"请设置 AUTH_MODE={AuthMode.JWT.value}。"
        )

    if not (jwt_public_key.strip() or jwt_jwks_url.strip()):
        raise AuthConfigurationError(
            "生产环境使用 JWT 身份，但既没有配置 JWT_PUBLIC_KEY 也没有 "
            "JWT_JWKS_URL —— 无法验证签名，任何自签令牌都会被当成合法身份。"
            "**拒绝启动**，而不是降级为不验签。"
        )

    if not jwt_issuer.strip():
        raise AuthConfigurationError(
            "生产环境缺少 JWT_ISSUER：不校验签发方时，"
            "任何持有任意有效签名密钥的一方都能为本系统签发身份。"
        )

    if not jwt_audience.strip():
        raise AuthConfigurationError(
            "生产环境缺少 JWT_AUDIENCE：不校验受众时，"
            "为**其他系统**签发的令牌可以直接拿来访问本系统。"
        )
