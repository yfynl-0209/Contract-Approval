"""开发期身份适配器：**直接读请求头里的身份**（M7）。

## ⚠️ 这个适配器**无条件信任**调用方自称的身份

它没有任何凭据校验：谁都能把 `X-Actor-Id: admin` 发过来，然后就是 admin。
因此它**只在开发期可用**，生产环境启用它等于没有身份体系。

防线有三层，缺一不可：

| 层 | 位置 | 作用 |
| --- | --- | --- |
| 配置层 | `assert_auth_configuration` | `ENV=production` + `AUTH_MODE=dev` → **拒绝启动** |
| 组合根 | `app/api/deps.py` | 按 `AUTH_MODE` **选择**适配器，dev 适配器走不到生产分支 |
| 构造层 | 本文件 `__init__` | 构造时若 `env` 是生产 → 直接抛错（防御上面两层被绕过） |

第三层看起来多余，但它防的是"以后有人写了**另一个**组合根"：
那时前两层都不在那个新入口上，而这一层跟着类走。

## 身份的三种载体（M8 重设计时补充）

| 载体 | 格式 | 谁在用 |
| --- | --- | --- |
| `X-Actor-*` 头 | 三个独立头 | 脚本 / MCP / 既有测试 |
| `Authorization: Bearer dev.<base64url(JSON)>` | 一个标准凭据头 | 浏览器控制台（第一代方案） |
| `Cookie: dev_identity=<base64url(JSON)>` | 会话 Cookie | **浏览器控制台（现行）** |

为什么浏览器先后换了两种：某些浏览器扩展（如翻译类）会挂钩页面的 `fetch`
并**重发请求且丢掉 init.headers**——先是 `X-Actor-*` 被剥掉，换成标准
`Authorization` 后仍有个别请求头消失（哪个请求中招是间歇的，症状极度误导，
M8 演示现场实际发生）。Cookie 由**浏览器自动随请求携带**，不在 fetch 的
init 里，任何 fetch 钩子都动不了它——这是能想到的最强免疫。
载荷都是同一种 base64url JSON（`{sub, name?, roles?}`）。
优先级：`X-Actor-*` → `Authorization` → `Cookie`
（脚本与测试的行为不变；前两种同时存在时说明调用方自己发了两份，取显式头）。
"""
from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from app.adapters.auth._headers import header_value
from app.auth import Actor, AuthConfigurationError, AuthenticationError, is_production

#: 请求头名。**显式声明**，便于契约测试与调用方共用同一份常量。
HEADER_ACTOR_ID = "X-Actor-Id"
HEADER_ACTOR_NAME = "X-Actor-Name"
HEADER_ACTOR_ROLES = "X-Actor-Roles"
HEADER_TENANT_ID = "X-Tenant-Id"
HEADER_AUTHORIZATION = "Authorization"
HEADER_COOKIE = "Cookie"
#: 开发期 bearer 的前缀（不是 JWT：它没有签名，因为它**没有任何凭据可保护**）。
_DEV_BEARER_PREFIX = "Bearer dev."
#: 开发期身份 Cookie 名（前端 `api/identity.ts` 同名常量互为镜像）。
COOKIE_DEV_IDENTITY = "dev_identity"


class DevHeaderIdentity:
    """把请求头翻译成 `Actor`（开发期专用）。"""

    def __init__(self, *, tenant_id: str, env: str = "development") -> None:
        if is_production(env):
            raise AuthConfigurationError(
                f"拒绝在生产环境（ENV={env!r}）构造开发期身份适配器："
                "它不校验任何凭据，任何能访问端口的人都可以自称系统管理员。"
            )
        self._tenant_id = tenant_id

    def resolve(self, headers: Mapping[str, str]) -> Actor:
        """读请求头解析身份。

        三种载体（优先级见模块 docstring）：`X-Actor-*` → `Authorization` → Cookie。

        Raises:
            AuthenticationError: 所有载体都没有 / 载荷损坏，或声明的租户与当前实例不一致。
        """
        raw_id = header_value(headers, HEADER_ACTOR_ID)
        if raw_id is None or not raw_id.strip():
            claims = self._claims_from_bearer(headers)
            if claims is None:
                claims = self._claims_from_cookie(headers)
            if claims is None:
                # ⚠️ 缺身份**必须拒绝**，不能"默认成某个用户"或放行为匿名：
                # 后者会让漏传头的调用方照常跑通，而审计里记下的操作人不是他 ——
                # 等发现时，账已经错了很久了。
                raise AuthenticationError(
                    "缺少身份凭证：请提供 Authorization: Bearer dev.<base64url(JSON)>、"
                    f"dev_identity Cookie 或请求头 {HEADER_ACTOR_ID}"
                    "（AUTH_MODE=dev 时用于标识调用方）。"
                    "本服务不会在身份缺失时假定任何默认用户。"
                )
            return self._actor_from_claims(claims, headers)

        actor_id = raw_id.strip()

        raw_name = header_value(headers, HEADER_ACTOR_NAME)
        display_name = (raw_name or "").strip() or actor_id

        raw_roles = header_value(headers, HEADER_ACTOR_ROLES) or ""
        # 允许逗号或空白分隔：写文档的人和调接口的人对分隔符的直觉不一样，
        # 而"多写了个空格导致角色没生效"表现为**权限不足**，与真正的原因相去甚远。
        roles = frozenset(
            part for part in _split_roles(raw_roles) if part
        )

        tenant_id = self._resolve_tenant(headers)

        return Actor(
            actor_id=actor_id,
            display_name=display_name,
            roles=roles,
            tenant_id=tenant_id,
        )

    def _actor_from_claims(
        self, claims: dict[str, Any], headers: Mapping[str, str]
    ) -> Actor:
        """把 bearer 载荷翻译成 `Actor`（字段约束与 `X-Actor-*` 路径一致）。"""
        actor_id = str(claims.get("sub") or "").strip()
        if actor_id == "":
            raise AuthenticationError(
                "dev bearer 载荷缺少 sub（调用方标识）。"
            )
        display_name = str(claims.get("name") or "").strip() or actor_id
        roles_raw = claims.get("roles")
        roles = frozenset(
            part
            for part in _split_roles(
                ",".join(str(item) for item in roles_raw) if isinstance(roles_raw, list) else ""
            )
            if part
        )
        tenant_id = self._resolve_tenant(headers)
        return Actor(
            actor_id=actor_id,
            display_name=display_name,
            roles=roles,
            tenant_id=tenant_id,
        )

    def _claims_from_bearer(self, headers: Mapping[str, str]) -> dict[str, Any] | None:
        """从 `Authorization: Bearer dev.<载荷>` 解出 JSON 声明。

        没有 Authorization 头 → `None`（交给调用方走"缺身份"分支）；
        有但格式不对 → 直接 401 并说明期望格式，而不是静默当作匿名。
        """
        raw = header_value(headers, HEADER_AUTHORIZATION)
        if raw is None or not raw.strip():
            return None
        value = raw.strip()
        if not value.startswith(_DEV_BEARER_PREFIX):
            raise AuthenticationError(
                "Authorization 头不是本服务开发期身份支持的格式"
                "（期望 'Bearer dev.<base64url(JSON)>'；生产环境请使用 JWT 登录）。"
            )
        payload = value[len(_DEV_BEARER_PREFIX):].strip()
        try:
            padded = payload + "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise AuthenticationError(
                f"dev bearer 载荷无法解码：{exc}"
            ) from exc
        if not isinstance(claims, dict):
            raise AuthenticationError(
                "dev bearer 载荷必须是 JSON 对象（含 sub / 可选 name / roles）。"
            )
        return claims

    def _claims_from_cookie(self, headers: Mapping[str, str]) -> dict[str, Any] | None:
        """从 `dev_identity` Cookie 解出 JSON 声明（载荷与 bearer 相同）。

        没有 Cookie → `None`；有但损坏 → 401 并说明（这个 Cookie 是前端
        登录时写的，损坏只可能是代码缺陷或手工篡改，两种都该被看见）。
        """
        raw_cookie = header_value(headers, HEADER_COOKIE)
        if raw_cookie is None:
            return None
        wanted = f"{COOKIE_DEV_IDENTITY}="
        for part in raw_cookie.split(";"):
            item = part.strip()
            if not item.startswith(wanted):
                continue
            payload = item[len(wanted):].strip()
            try:
                padded = payload + "=" * (-len(payload) % 4)
                claims = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
            except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
                raise AuthenticationError(
                    f"dev_identity Cookie 无法解码：{exc}"
                ) from exc
            if not isinstance(claims, dict):
                raise AuthenticationError(
                    "dev_identity Cookie 载荷必须是 JSON 对象（含 sub / 可选 name / roles）。"
                )
            return claims
        return None

    def _resolve_tenant(self, headers: Mapping[str, str]) -> str:
        """确定本次请求的租户。

        v1 固定单租户，因此**声明的租户必须等于配置的租户**：

        - 不校验而直接采用声明值 → 未来接入第二个租户时，
          这里已经是一条**跨租户读取**的通道，而它看起来完全正常；
        - 静默改写成配置值 → 把"环境配错了"藏起来，
          调用方以为自己在访问 A 租户，实际拿到 B 租户的数据。
        """
        raw_tenant = header_value(headers, HEADER_TENANT_ID)
        if raw_tenant is None or not raw_tenant.strip():
            return self._tenant_id
        claimed = raw_tenant.strip()
        if claimed != self._tenant_id:
            raise AuthenticationError(
                f"请求声明的租户 {claimed!r} 与当前实例的租户 "
                f"{self._tenant_id!r} 不一致。"
            )
        return claimed

    def close(self) -> None:
        """无长生命周期资源。"""


def _split_roles(raw: str) -> list[str]:
    """按逗号或空白切分角色声明，并去掉空白与大小写差异。"""
    normalized = raw.replace(",", " ")
    return [part.strip().lower() for part in normalized.split()]
