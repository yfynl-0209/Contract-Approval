"""当前身份接口 `GET /api/me`（M8 / Task 2）。

## 本文件守住的是"前端从哪里得知自己的权限"

材料 §5.3 允许前端按权限隐藏入口，但**判据必须来自后端**。本文件守三件事：

1. **权限是服务端算的**：`permissions` 必须等于 `permissions_for(roles)` 的结果，
   而不是端点自己写一套映射 —— 前端拿到的若与后端判定的不是同一个东西，
   表现就是"入口在、点了 403"。
2. **未识别的角色不带来权限，但必须可见**：IdP 先上、本系统后跟时，
   用户看到的是"按钮没了"，唯一的线索是 `unknown_roles`。
3. **只要求已认证**：连一个角色都不认识的主体也要能读到自己的身份 ——
   否则"我为什么没权限"这个问题在**被回答之前**就返回了 403，
   而那个 403 恰恰不说明他缺什么。

⚠️ 本接口不碰数据库，因此测试**不需要建库**；用 `dependency_overrides[get_actor]`
替换身份即可。另有一条用例**不覆盖依赖**，走真实的 `AUTH_MODE=dev` 请求头路径 ——
覆盖依赖能测出"端点怎么用身份"，但测不出"身份怎么从请求进来"。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_actor
from app.auth import Actor, Permission, Role, permissions_for
from app.main import app


def _actor(
    actor_id: str = "u-1",
    *,
    roles: list[str] | None = None,
    tenant_id: str = "default",
) -> Actor:
    return Actor(
        actor_id=actor_id,
        display_name=f"{actor_id}-显示名",
        roles=frozenset(roles or ()),
        tenant_id=tenant_id,
    )


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """替换身份依赖的客户端；用例结束后**清干净**覆盖。"""
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _as(test_client: TestClient, actor: Actor) -> None:
    app.dependency_overrides[get_actor] = lambda: actor


# ============================================================
# 形状与来源
# ============================================================


def test_me_returns_identity_and_server_computed_permissions(client: TestClient) -> None:
    """权限与 `permissions_for` 逐字一致 —— 判据只有一份。"""
    actor = _actor(roles=[Role.SYSTEM_ADMIN.value])
    _as(client, actor)

    response = client.get("/api/me")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()

    assert body["actor_id"] == "u-1"
    assert body["display_name"] == "u-1-显示名"
    assert body["tenant_id"] == "default"
    assert body["roles"] == ["system_admin"]
    assert body["unknown_roles"] == []
    assert body["permissions"] == sorted(
        permission.value for permission in permissions_for(actor.roles)
    )


def test_permissions_are_ordered_stably(client: TestClient) -> None:
    """同一份身份两次请求得到**逐字相同**的权限列表。

    集合的迭代顺序不保证稳定，而顺序抖动会让前端的缓存对比与断言变成偶发失败。
    """
    _as(client, _actor(roles=[Role.SYSTEM_ADMIN.value]))

    first = client.get("/api/me").json()["permissions"]
    second = client.get("/api/me").json()["permissions"]

    assert first == second == sorted(first)


def test_read_only_auditor_cannot_see_admin_entries(client: TestClient) -> None:
    """只读审计**没有**任何管理类权限（前端的隐藏依据）。"""
    _as(client, _actor(roles=[Role.READ_ONLY_AUDITOR.value]))

    permissions = set(client.get("/api/me").json()["permissions"])

    assert permissions == {Permission.TASK_READ.value, Permission.AUDIT_READ.value}
    for forbidden in (
        Permission.RULE_MANAGE.value,
        Permission.OPS_RETRY.value,
        Permission.RESULT_CONFIRM.value,
        Permission.WRITEBACK_EXECUTE.value,
    ):
        assert forbidden not in permissions


# ============================================================
# 未识别的角色（fail-closed 但可见）
# ============================================================


def test_unknown_role_grants_nothing_but_stays_visible(client: TestClient) -> None:
    """未知角色：不给权限，但**必须**出现在 `unknown_roles` 里。

    它错了的表现是"这个人莫名其妙少了一堆入口"，而账面上没有任何异常 ——
    唯一的线索就是这一行。
    """
    _as(client, _actor(roles=["future_new_role"]))

    body = client.get("/api/me").json()

    assert body["permissions"] == []
    assert body["unknown_roles"] == ["future_new_role"]
    assert body["roles"] == ["future_new_role"]


def test_known_and_unknown_roles_coexist(client: TestClient) -> None:
    """混合声明：认识的生效、不认识的被丢弃，两者都要能看见。"""
    _as(client, _actor(roles=[Role.SYSTEM_ADMIN.value, "legacy_role"]))

    body = client.get("/api/me").json()

    assert body["unknown_roles"] == ["legacy_role"]
    assert Permission.RULE_MANAGE.value in body["permissions"]


# ============================================================
# 认证（不是鉴权）
# ============================================================


def test_authenticated_but_roleless_actor_can_read_own_identity(
    client: TestClient,
) -> None:
    """一个角色都没有的主体也能读自己的身份 —— **不返回 403**。

    "我为什么没权限"这个问题必须在**能回答**之后才拒；
    在回答之前就 403 的接口，等同于把唯一的解释渠道也关掉了。
    """
    _as(client, _actor(roles=[]))

    response = client.get("/api/me")

    assert response.status_code == 200
    assert response.json()["permissions"] == []


def test_missing_credentials_are_401_not_403() -> None:
    """走**真实**的 dev 请求头路径：缺 `X-Actor-Id` → 401。

    401 与 403 的区别是客户端的处置：前者"去拿一份身份"，后者"换账号或找管理员"。
    `/api/me` 永远不该返回 403。
    """
    with TestClient(app) as test_client:
        response = test_client.get("/api/me")

    assert response.status_code == 401
    assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_dev_headers_resolve_to_the_same_identity_shape() -> None:
    """不覆盖依赖：`X-Actor-Id` / `X-Actor-Roles` 头真的会生效。

    ⚠️ 显示名这里刻意用 **ASCII**（`Li Hua`）：HTTP 头是 latin-1 字节串，
    非 ASCII 值连**发出去**都做不到（httpx 抛 `UnicodeEncodeError`，
    浏览器会按 latin-1 编码成 mojibake）。

    这是 `AUTH_MODE=dev` 适配器的固有限制，不是缺陷：它是**开发期**身份来源，
    生产用 JWT —— 那里的姓名在 JSON 声明里，Unicode 完全正常。
    前端的对策是**非 ASCII 显示名干脆不发**（服务端回落到 `actor_id`），
    见 `frontend/src/app/AuthProvider.tsx`，而不是自己发明一种编码。
    """
    with TestClient(app) as test_client:
        response = test_client.get(
            "/api/me",
            headers={
                "X-Actor-Id": "li-hua",
                "X-Actor-Name": "Li Hua",
                "X-Actor-Roles": "legal_reviewer",
                "X-Tenant-Id": "default",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["actor_id"] == "li-hua"
    assert body["display_name"] == "Li Hua"
    assert Permission.RESULT_CONFIRM.value in body["permissions"]


def test_dev_bearer_authorization_is_accepted() -> None:
    """浏览器控制台走 `Authorization: Bearer dev.<base64url(JSON)>`。

    为什么多这一种载体：浏览器扩展（翻译/隐私类）会剥掉 `X-Actor-*`
    这类非标准自定义头，而 `Authorization` 是标准凭据头、扩展不会碰——
    M8 演示现场的真实故障。载荷与前端 `api/identity.ts` 的编码互为镜像。
    """
    import base64
    import json

    claims = {"sub": "li-hua", "name": "Li Hua", "roles": ["legal_reviewer"]}
    payload = base64.urlsafe_b64encode(
        json.dumps(claims).encode()
    ).decode().rstrip("=")
    with TestClient(app) as test_client:
        response = test_client.get(
            "/api/me",
            headers={"Authorization": f"Bearer dev.{payload}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["actor_id"] == "li-hua"
    assert body["display_name"] == "Li Hua"
    assert Permission.RESULT_CONFIRM.value in body["permissions"]


def test_malformed_dev_bearer_is_401_with_explanation() -> None:
    """带了 Authorization 但不是 dev 格式 → 401 并说明期望格式。

    不能静默当作匿名：那样"扩展剥了头"和"令牌写错"两种原因会被
    合并成同一个说不清的失败。
    """
    with TestClient(app) as test_client:
        response = test_client.get(
            "/api/me",
            headers={"Authorization": "Bearer not-a-dev-token"},
        )

    assert response.status_code == 401
    assert "Bearer dev." in response.json()["message"]


def test_dev_identity_cookie_is_accepted() -> None:
    """`dev_identity` Cookie 是第三种载体：浏览器自动携带，fetch 钩子动不了。

    为什么需要它：翻译类扩展挂钩页面的 fetch 并重发请求且**丢掉
    init.headers**——先是 `X-Actor-*` 被剥、换成 `Authorization` 后仍有
    个别请求头消失（哪个请求中招是间歇的）。Cookie 不在 fetch 的 init 里，
    是能想到的最强免疫。
    """
    import base64
    import json

    claims = {"sub": "li-hua", "name": "Li Hua", "roles": ["legal_reviewer"]}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    with TestClient(app) as test_client:
        response = test_client.get(
            "/api/me",
            cookies={"dev_identity": payload},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["actor_id"] == "li-hua"
    assert body["display_name"] == "Li Hua"
    assert Permission.RESULT_CONFIRM.value in body["permissions"]


def test_malformed_dev_identity_cookie_is_401_with_explanation() -> None:
    """Cookie 损坏 → 401 并说明，而不是静默当匿名。

    这个 Cookie 是前端登录时写的：损坏只可能是代码缺陷或手工篡改，
    两种都该被看见。
    """
    with TestClient(app) as test_client:
        response = test_client.get(
            "/api/me",
            cookies={"dev_identity": "not-base64-json!!"},
        )

    assert response.status_code == 401
    assert "dev_identity" in response.json()["message"]


# ============================================================
# 不泄漏凭据
# ============================================================


def test_response_carries_no_credentials() -> None:
    """响应里不出现令牌或请求头原文。

    `Actor` 是解析后的结果、不含凭据；这条断言守的是"以后有人顺手加一个
    `raw_headers` 便于排障" —— 那等于给浏览器与前端日志开了一条
    把 `Authorization` 写出去的路。
    """
    with TestClient(app) as test_client:
        response = test_client.get(
            "/api/me",
            headers={
                "X-Actor-Id": "li-hua",
                "Authorization": "Bearer super-secret-token",
            },
        )

    assert response.status_code == 200
    assert "super-secret-token" not in response.text
    assert "authorization" not in response.text.lower()
