"""生产期身份适配器：校验 JWT 并解出 `Actor`（M7）。

## 每一项校验都在防一件具体的事

| 校验 | 不做会怎样 |
| --- | --- |
| **签名** | 任何人不经签发方就能自己造一份"我是系统管理员"的令牌 |
| **`iss` 签发方** | 另一个系统签发的、同样有效的令牌可以直接拿来用 |
| **`aud` 受众** | 为**别的系统**签发的令牌被本系统接受（受众校验是这条防线） |
| **`exp` 有效期** | 一份泄露的令牌永久有效，且没有任何办法作废 |
| **`alg` 白名单** | 经典的 `alg=none` / 算法混淆攻击：把 RS256 改成 HS256，用公钥当密钥签名 |

最后一条尤其要紧：**算法必须来自配置，绝不能取自令牌本身**。
令牌里的 `alg` 是**攻击者可控输入**，按它去选验证方式等于让攻击者选门锁。

## 令牌不进日志

失败信息只用 PyJWT 自己的描述，**不拼接令牌原文**。
令牌一旦进了日志，任何能看日志的人都能冒充它 —— 而日志的访问面
通常比身份系统宽得多。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import jwt

from app.adapters.auth._headers import bearer_token
from app.auth import (
    Actor,
    AuthenticationError,
    AuthConfigurationError,
    IdentityProviderUnavailable,
)

#: 默认算法：非对称签名。**必须由配置给出，不得取自令牌。**
DEFAULT_ALGORITHMS: tuple[str, ...] = ("RS256",)

#: 默认的声明名。企业 IdP 的字段名不统一，因此全部可配。
DEFAULT_SUBJECT_CLAIM = "sub"
DEFAULT_NAME_CLAIM = "name"
DEFAULT_ROLES_CLAIM = "roles"
DEFAULT_TENANT_CLAIM = "tenant_id"

#: 解析时**必须存在**的声明。缺了就拒绝，而不是用默认值兜底 ——
#: 没有 `sub` 时"这令牌是谁的"根本无从谈起，兜底只会造出一个假身份。
REQUIRED_CLAIMS: tuple[str, ...] = ("exp", "iss", "aud", "sub")


class JwtIdentity:
    """JWT → `Actor`。密钥来源可为**静态公钥**或 **JWKS 端点**。"""

    def __init__(
        self,
        *,
        tenant_id: str,
        issuer: str,
        audience: str,
        public_key: str = "",
        jwks_url: str = "",
        algorithms: Sequence[str] = DEFAULT_ALGORITHMS,
        subject_claim: str = DEFAULT_SUBJECT_CLAIM,
        name_claim: str = DEFAULT_NAME_CLAIM,
        roles_claim: str = DEFAULT_ROLES_CLAIM,
        tenant_claim: str = DEFAULT_TENANT_CLAIM,
        leeway_seconds: int = 0,
        jwks_client: Any | None = None,
    ) -> None:
        if not (public_key.strip() or jwks_url.strip()):
            # ⚠️ 这里必须是 **AuthConfigurationError**，不是 AuthenticationError。
            #
            # 缺密钥是**运维配错**，不是"来者身份不可信"。抛 AuthenticationError
            # 会被翻译成 **401**，于是每个调用方都读到"我的令牌有问题"，
            # 分头去重新登录、去换令牌 —— 而真正该做的事是改配置，
            # 那件事**任何调用方都做不到**。故障排查就此被引向一个不存在的原因。
            #
            # AuthConfigurationError 刻意不是 AppError，因此不会被翻成 HTTP 响应，
            # 而是以 500 暴露出来：它就是这个进程自己的问题。
            raise AuthConfigurationError(
                "JWT 适配器既没有静态公钥也没有 JWKS 端点，无法验证签名。"
                "请配置 JWT_PUBLIC_KEY 或 JWT_JWKS_URL。"
            )
        self._tenant_id = tenant_id
        self._issuer = issuer
        self._audience = audience
        self._public_key = public_key
        self._jwks_url = jwks_url
        self._algorithms = tuple(algorithms)
        self._subject_claim = subject_claim
        self._name_claim = name_claim
        self._roles_claim = roles_claim
        self._tenant_claim = tenant_claim
        self._leeway = leeway_seconds
        # 惰性构造：只在真正用到 JWKS 时才建连接、才发第一次请求
        self._jwks_client = jwks_client

    # ---------- 密钥 ----------

    def _signing_key(self, token: str) -> str | Any:
        """取验签用的密钥。静态公钥优先，否则按 `kid` 去 JWKS 里找。"""
        if self._public_key.strip():
            return self._public_key
        client = self._jwks_client_instance()
        try:
            return client.get_signing_key_from_jwt(token).key
        except jwt.PyJWKClientConnectionError as exc:
            # 连不上 JWKS：**我们判断不了**这份令牌是否有效。
            # 报 401 会让调用方丢弃一份可能完全有效的令牌去重新登录。
            raise IdentityProviderUnavailable(
                f"无法获取签名公钥（JWKS 不可达）：{exc}"
            ) from exc
        except jwt.PyJWKClientError as exc:
            # 连得上但没有匹配的 kid：这是**令牌**的问题，不是环境的问题
            raise AuthenticationError(f"令牌的签名密钥无法解析：{exc}") from exc

    def _jwks_client_instance(self) -> Any:
        if self._jwks_client is None:
            self._jwks_client = jwt.PyJWKClient(self._jwks_url)
        return self._jwks_client

    # ---------- 解析 ----------

    def resolve(self, headers: Mapping[str, str]) -> Actor:
        """校验令牌并解出 `Actor`。

        Raises:
            AuthenticationError: 缺少 / 格式不符 / 签名或声明不合法 / 租户不符。
            IdentityProviderUnavailable: JWKS 不可达（**不是**认证失败）。
        """
        token = bearer_token(headers)
        if token is None:
            raise AuthenticationError(
                "缺少 `Authorization: Bearer <token>` 请求头。"
            )

        claims = self._decode(token)
        return self._actor_from_claims(claims)

    def _decode(self, token: str) -> dict[str, Any]:
        """解签并校验标准声明。任何失败都转成 `AuthenticationError`。"""
        try:
            return dict(
                jwt.decode(
                    token,
                    key=self._signing_key(token),
                    # ⚠️ 算法来自**配置**，不是令牌的 `alg` 头
                    algorithms=list(self._algorithms),
                    issuer=self._issuer,
                    audience=self._audience,
                    leeway=self._leeway,
                    options={"require": list(REQUIRED_CLAIMS)},
                )
            )
        except jwt.PyJWTError as exc:
            # 只带 PyJWT 的描述，**不带令牌原文**（见模块 docstring）
            raise AuthenticationError(f"令牌校验失败：{exc}") from exc

    def _actor_from_claims(self, claims: Mapping[str, Any]) -> Actor:
        actor_id = str(claims.get(self._subject_claim) or "").strip()
        if not actor_id:
            raise AuthenticationError(
                f"令牌缺少 {self._subject_claim!r} 声明，无法确定主体。"
            )

        display_name = str(claims.get(self._name_claim) or "").strip() or actor_id
        roles = _as_role_set(claims.get(self._roles_claim))
        tenant_id = self._tenant_from_claims(claims)

        return Actor(
            actor_id=actor_id,
            display_name=display_name,
            roles=roles,
            tenant_id=tenant_id,
        )

    def _tenant_from_claims(self, claims: Mapping[str, Any]) -> str:
        """校验租户声明，规则与开发期适配器一致（见 `DevHeaderIdentity`）。"""
        claimed = str(claims.get(self._tenant_claim) or "").strip()
        if not claimed:
            raise AuthenticationError(
                f"令牌缺少 {self._tenant_claim!r} 声明："
                "无法确定主体属于哪个租户，拒绝以默认租户处理。"
            )
        if claimed != self._tenant_id:
            raise AuthenticationError(
                f"令牌声明的租户 {claimed!r} 与当前实例的租户 "
                f"{self._tenant_id!r} 不一致。"
            )
        return claimed

    def close(self) -> None:
        """释放 JWKS 客户端（若已构造）。"""
        self._jwks_client = None


def _as_role_set(value: Any) -> frozenset[str]:
    """把角色声明规整成集合。

    支持三种常见形态：字符串（`"a b"` / `"a,b"`）、字符串列表、以及缺失。
    规整只做**去空白与大小写**，不做任何"猜测同义词"的映射 ——
    猜错的表现是**多给了权限**，而多给的权限不会有人报错。
    """
    if value is None:
        return frozenset()
    if isinstance(value, str):
        pieces = value.replace(",", " ").split()
        return frozenset(piece.strip().lower() for piece in pieces if piece.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(
            str(piece).strip().lower() for piece in value if str(piece).strip()
        )
    # 形态不认识（数字 / 对象）：当作"没有声明角色"，即**不给任何权限**
    return frozenset()
