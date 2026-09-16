"""身份与授权（M7 / Task 1）。

本文件守三类东西，按"错了会不会有人发现"排序：

1. **fail-closed 的方向**：缺少凭据、凭据无效、租户不符、生产环境配了开发期身份
   —— 这些必须**拒绝**。它们错了的共同表现是"一切正常"，没有任何人会报障。
2. **401 与 403 必须可区分**：两者合并时，客户端对"我没登录"和"我没权限"
   只能做同一种处置，而正确处置截然不同（换令牌 vs 换账号）。
3. **未识别的角色不带来任何权限**：IdP 先上、本系统后跟时，
   不能因为"角色名不认识"就放行或拒服务。

JWT 部分用**自签密钥**离线验证：不需要真实 IdP，但签名/时效/签发方/受众/
算法混淆这几条防线都真的被执行到。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.api.deps import get_db, get_gateway, get_storage, require_permissions
from app.api.errors import error_headers, http_status_for
from app.auth import (
    Actor,
    AuthenticationError,
    AuthConfigurationError,
    AuthorizationError,
    IdentityProviderUnavailable,
    Permission,
    Role,
    assert_auth_configuration,
    has_permissions,
    permissions_for,
)
from app.adapters.auth.dev_header_identity import DevHeaderIdentity
from app.adapters.auth.jwt_identity import JwtIdentity
from app.errors import AppError
from app.main import app

#: 测试机的租户（与 `settings.tenant_id` 的默认值一致）
TENANT = "default"
ISSUER = "https://idp.example.com/"
AUDIENCE = "contract-approval"


def _actor(
    actor_id: str = "u-1",
    *,
    display_name: str | None = None,
    roles: list[str] | tuple[str, ...] = (),
    tenant_id: str = TENANT,
) -> Actor:
    """测试身份。默认 `actor_id == display_name`，断言时更好读。"""
    return Actor(
        actor_id=actor_id,
        display_name=display_name or actor_id,
        roles=frozenset(roles),
        tenant_id=tenant_id,
    )


# ============================================================
# 角色 → 权限映射
# ============================================================


def test_read_only_auditor_has_no_mutation_permissions() -> None:
    """只读审计**只能看**。

    这条是"权限泄漏"最可能的入口：给只读角色顺手加一条写入权限，
    功能上没人会发现 —— 直到有人用它改了不该改的东西。
    """
    granted = permissions_for([Role.READ_ONLY_AUDITOR.value])

    assert Permission.TASK_READ in granted
    assert Permission.AUDIT_READ in granted
    # 逐条断言而不是"数量相等"：数量相等在一个权限被调换成另一个时照样通过
    for forbidden in (
        Permission.RESULT_SAVE,
        Permission.RESULT_CONFIRM,
        Permission.WRITEBACK_EXECUTE,
        Permission.REVIEW_EXECUTE,
        Permission.RULE_MANAGE,
        Permission.OPS_RETRY,
    ):
        assert forbidden not in granted, f"只读角色不应拥有 {forbidden}"


def test_admin_is_a_superset_of_reviewer() -> None:
    reviewer = permissions_for([Role.LEGAL_REVIEWER.value])
    admin = permissions_for([Role.SYSTEM_ADMIN.value])

    assert reviewer < admin
    assert Permission.RULE_MANAGE in admin
    assert Permission.OPS_RETRY in admin


def test_role_permission_map_cannot_be_mutated_at_runtime() -> None:
    """映射表**只读**：运行时放开权限不会留下任何审计痕迹。"""
    from app.auth import ROLE_PERMISSIONS

    with pytest.raises(TypeError):
        ROLE_PERMISSIONS[Role.READ_ONLY_AUDITOR] = frozenset(  # type: ignore[index]
            {Permission.RULE_MANAGE}
        )


def test_unknown_role_grants_nothing_but_stays_visible() -> None:
    """未识别的角色 → **零权限**，但角色声明仍可读。

    丢弃（而不是报错）是刻意的：IdP 先上、本系统后跟时，
    那个人只是**少一些权限**（看得见的 403），而不是服务拒绝所有人。
    但角色名必须留着 —— 否则"我为什么被拒"就没有线索。
    """
    actor = _actor(roles=["typo_role", "legal_reviewer"])

    assert actor.permissions == permissions_for([Role.LEGAL_REVIEWER.value])
    assert actor.unknown_roles == frozenset({"typo_role"})


def test_unknown_role_alone_grants_nothing() -> None:
    actor = _actor(roles=["system_administrator"])  # 少了个下划线

    assert actor.permissions == frozenset()
    assert not actor.has(Permission.TASK_READ)


def test_has_requires_all_permissions_not_any() -> None:
    """`has(A, B)` 是"**同时**具备"。

    写成"其一"时，调用方读到的 `require_permissions(A, B)` 是"要 A 或 B"，
    而它想说的几乎总是"要 A 且 B"。
    """
    actor = _actor(roles=[Role.LEGAL_REVIEWER.value])

    assert actor.has(Permission.TASK_READ, Permission.RESULT_SAVE)
    assert not actor.has(Permission.TASK_READ, Permission.RULE_MANAGE)


def test_has_with_no_permissions_is_authenticated_only() -> None:
    """无参数 = "只要是已认证主体"，可用于把端点从匿名改为需登录。"""
    assert _actor().has()
    assert has_permissions(_actor(), Permission.TASK_READ) is False


# ============================================================
# 开发期适配器：请求头解析
# ============================================================


def test_missing_actor_header_is_rejected() -> None:
    """缺身份**必须拒绝**，不得默认成某个用户。"""
    provider = DevHeaderIdentity(tenant_id=TENANT)

    with pytest.raises(AuthenticationError) as excinfo:
        provider.resolve({})

    assert "X-Actor-Id" in str(excinfo.value)


def test_blank_actor_header_is_rejected() -> None:
    """纯空白的身份等同于没传：`" "` 不是一个人。"""
    provider = DevHeaderIdentity(tenant_id=TENANT)

    with pytest.raises(AuthenticationError):
        provider.resolve({"X-Actor-Id": "   "})


def test_header_lookup_is_case_insensitive() -> None:
    """普通 dict 是**大小写敏感**的，Starlette 的 Headers 不是。

    只按一种来源写，会出现"线上好、测试红"或反过来，
    而两种表现都不指向真正的原因（大小写）。
    """
    provider = DevHeaderIdentity(tenant_id=TENANT)

    actor = provider.resolve({"x-actor-id": "u-9", "x-actor-roles": "legal_reviewer"})

    assert actor.actor_id == "u-9"
    assert Permission.RESULT_SAVE in actor.permissions


def test_roles_accept_comma_or_space_separated() -> None:
    """两种分隔符都要认：写文档的人和调接口的人直觉不一样。"""
    provider = DevHeaderIdentity(tenant_id=TENANT)

    comma = provider.resolve(
        {"X-Actor-Id": "u-1", "X-Actor-Roles": "legal_reviewer,system_admin"}
    )
    space = provider.resolve(
        {"X-Actor-Id": "u-1", "X-Actor-Roles": "legal_reviewer system_admin"}
    )

    assert comma.roles == space.roles == frozenset({"legal_reviewer", "system_admin"})


def test_display_name_defaults_to_actor_id() -> None:
    provider = DevHeaderIdentity(tenant_id=TENANT)

    actor = provider.resolve({"X-Actor-Id": "u-1"})

    assert actor.display_name == "u-1"


def test_tenant_header_mismatch_is_rejected() -> None:
    """声明了**别的**租户 → 拒绝。

    直接采用声明值，未来就是一个跨租户读取通道，而它看起来完全正常。
    """
    provider = DevHeaderIdentity(tenant_id=TENANT)

    with pytest.raises(AuthenticationError) as excinfo:
        provider.resolve({"X-Actor-Id": "u-1", "X-Tenant-Id": "other-tenant"})

    assert "other-tenant" in str(excinfo.value)


def test_absent_tenant_header_uses_the_configured_tenant() -> None:
    provider = DevHeaderIdentity(tenant_id=TENANT)

    assert provider.resolve({"X-Actor-Id": "u-1"}).tenant_id == TENANT


def test_dev_adapter_refuses_to_be_constructed_in_production() -> None:
    """第三层防线：跟着类走，防的是"以后有人写了另一个组合根"。"""
    with pytest.raises(AuthConfigurationError):
        DevHeaderIdentity(tenant_id=TENANT, env="production")


# ============================================================
# 生产配置 fail-closed
# ============================================================


def test_production_with_dev_mode_is_refused() -> None:
    """生产环境选了开发期身份来源 → **拒绝启动**，不降级。"""
    with pytest.raises(AuthConfigurationError) as excinfo:
        assert_auth_configuration(env="production", auth_mode="dev")

    assert "AUTH_MODE" in str(excinfo.value)


def test_production_without_any_verification_key_is_refused() -> None:
    """没有公钥/JWKS 时无法验签 —— 任何自签令牌都会成为合法身份。"""
    with pytest.raises(AuthConfigurationError):
        assert_auth_configuration(env="production", auth_mode="jwt", jwt_issuer="i", jwt_audience="a")


def test_production_without_issuer_is_refused() -> None:
    with pytest.raises(AuthConfigurationError):
        assert_auth_configuration(
            env="production", auth_mode="jwt", jwt_public_key="k", jwt_audience="a"
        )


def test_production_without_audience_is_refused() -> None:
    """不校验受众 → 为**别的系统**签发的令牌可以直接拿来用。"""
    with pytest.raises(AuthConfigurationError):
        assert_auth_configuration(
            env="production", auth_mode="jwt", jwt_public_key="k", jwt_issuer="i"
        )


def test_production_with_complete_configuration_is_accepted() -> None:
    assert_auth_configuration(
        env="production",
        auth_mode="jwt",
        jwt_public_key="-----BEGIN PUBLIC KEY-----",
        jwt_issuer=ISSUER,
        jwt_audience=AUDIENCE,
    )


def test_unknown_auth_mode_is_refused_not_silently_downgraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AUTH_MODE` 拼错 → **抛错**，不得兜底成开发期适配器。

    这是最容易被写成"兜底到默认值"的一处：兜底后服务照常启动、
    照常响应，只是**无条件信任请求头**。一个尾随空格
    （`AUTH_MODE=jwt `）或大小写差异就足以让整个身份体系消失，
    而外部观察到的现象是"一切正常"。
    """
    from app.adapters.auth.dev_header_identity import DevHeaderIdentity as _Dev
    # M7 Task 6 起这个工厂是**公开**的（`build_identity_provider`）：MCP 的组合根
    # `scripts/run_mcp.py` 也要按 `AUTH_MODE` 选适配器。让脚本 import 一个私有函数，
    # 等于把"这是唯一的选择点"这件事藏起来。
    from app.api.deps import build_identity_provider
    from app.config import settings

    # JWT 模式需要完整配置才构造得出来；这里一并给上，
    # 否则下面断言的是"缺配置抛错"，而不是"取值本身被拒"
    monkeypatch.setattr(settings, "jwt_public_key", "-----BEGIN PUBLIC KEY-----")
    monkeypatch.setattr(settings, "jwt_issuer", ISSUER)
    monkeypatch.setattr(settings, "jwt_audience", AUDIENCE)

    # 先固定"合法值长什么样"，否则下面只断言"抛错"时，
    # 一个把**所有**取值都判为非法的实现也能通过
    monkeypatch.setattr(settings, "auth_mode", "jwt")
    assert isinstance(build_identity_provider(), JwtIdentity)
    monkeypatch.setattr(settings, "auth_mode", " JWT ")  # 空白与大小写不敏感
    assert isinstance(build_identity_provider(), JwtIdentity)
    monkeypatch.setattr(settings, "auth_mode", "dev")
    assert isinstance(build_identity_provider(), _Dev)

    for bad in ("jwtx", "none", "", "devv", "jwt,dev"):
        monkeypatch.setattr(settings, "auth_mode", bad)
        with pytest.raises(AuthConfigurationError):
            build_identity_provider()


def test_jwt_without_a_key_is_a_configuration_error_not_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AUTH_MODE=jwt` 但没配密钥 → **500 级配置错误**，不是 401。

    抛成 `AuthenticationError` 时，每个调用方都会读到"我的令牌有问题"
    并分头去重新登录 —— 而该做的事是改配置，**任何调用方都做不到**。
    一个排查不到自己身上的 401 会让所有人朝错误的方向跑。
    """
    from app.adapters.auth.jwt_identity import JwtIdentity as _Jwt

    with pytest.raises(AuthConfigurationError) as excinfo:
        _Jwt(tenant_id=TENANT, issuer=ISSUER, audience=AUDIENCE)

    # 刻意不是 AppError：它不能被翻译成 HTTP 响应，只能以 500 暴露
    assert not isinstance(excinfo.value, AppError)
    assert "JWT_PUBLIC_KEY" in str(excinfo.value)


def test_development_is_not_subject_to_production_rules() -> None:
    """开发期沿用默认配置即可跑起来（否则本地开发要先配一套 JWT）。"""
    assert_auth_configuration(env="development", auth_mode="dev")


def test_production_environment_name_is_matched_case_insensitively() -> None:
    """`ENV=Production` 拼写不同但意思一样 —— 漏判的代价是**带病上线**。"""
    with pytest.raises(AuthConfigurationError):
        assert_auth_configuration(env="  Production ", auth_mode="dev")


# ============================================================
# JWT（离线自签）
# ============================================================


@pytest.fixture(scope="module")
def signing_keys() -> dict[str, str]:
    """一对自签 RSA 密钥（模块级，生成一次约 100ms）。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return {"private": private_pem, "public": public_pem}


@pytest.fixture(scope="module")
def other_public_key() -> str:
    """**另一对**密钥的公钥 —— 用于"别人签的令牌"这一条。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _jwt_provider(public_key: str) -> JwtIdentity:
    return JwtIdentity(
        tenant_id=TENANT,
        issuer=ISSUER,
        audience=AUDIENCE,
        public_key=public_key,
    )


def _claims(**overrides: Any) -> dict[str, Any]:
    """一份合法的声明基线；各测试只覆盖自己关心的那一项。"""
    import time

    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "u-1",
        "name": "张伟",
        "roles": ["legal_reviewer"],
        "tenant_id": TENANT,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": now + 300,
        "iat": now,
    }
    claims.update(overrides)
    return claims


def _token(private_key: str, claims: dict[str, Any], algorithm: str = "RS256") -> str:
    return pyjwt.encode(claims, private_key, algorithm=algorithm)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_valid_token_is_accepted(signing_keys: dict[str, str]) -> None:
    provider = _jwt_provider(signing_keys["public"])

    actor = provider.resolve(_bearer(_token(signing_keys["private"], _claims())))

    assert actor.actor_id == "u-1"
    assert actor.display_name == "张伟"
    assert actor.tenant_id == TENANT
    assert Permission.RESULT_SAVE in actor.permissions


def test_token_signed_by_another_key_is_rejected(
    signing_keys: dict[str, str], other_public_key: str
) -> None:
    """**签名**这道防线：换了密钥的公钥就不该验得过。"""
    provider = _jwt_provider(other_public_key)

    with pytest.raises(AuthenticationError):
        provider.resolve(_bearer(_token(signing_keys["private"], _claims())))


def test_expired_token_is_rejected(signing_keys: dict[str, str]) -> None:
    """过期的令牌必须拒绝 —— 否则一份泄露的令牌永久有效且无法作废。"""
    provider = _jwt_provider(signing_keys["public"])
    import time

    expired = _claims(exp=int(time.time()) - 60)

    with pytest.raises(AuthenticationError):
        provider.resolve(_bearer(_token(signing_keys["private"], expired)))


def test_wrong_issuer_is_rejected(signing_keys: dict[str, str]) -> None:
    """别的系统签发的令牌不能直接用。"""
    provider = _jwt_provider(signing_keys["public"])

    with pytest.raises(AuthenticationError):
        provider.resolve(
            _bearer(_token(signing_keys["private"], _claims(iss="https://evil.example/")))
        )


def test_wrong_audience_is_rejected(signing_keys: dict[str, str]) -> None:
    """为**别的系统**签发的令牌不能拿来访问本系统。"""
    provider = _jwt_provider(signing_keys["public"])

    with pytest.raises(AuthenticationError):
        provider.resolve(
            _bearer(_token(signing_keys["private"], _claims(aud="another-service")))
        )


def _forge_hs256_token(secret: str, claims: dict[str, Any]) -> str:
    """手工造一枚 `alg=HS256`、以 `secret` 为对称密钥的令牌。

    **为什么不用 `pyjwt.encode`**：新版本 PyJWT 会以
    `InvalidKeyError("... should not be used as an HMAC secret")` 直接拒绝
    用 PEM 公钥当 HMAC 密钥来**生成**令牌。这是库在保护它的调用者，
    但攻击者当然不受这层保护 —— 他手上有 `hmac` 和 `hashlib`。
    用库来造，测的就是"库不肯帮我造"，而不是"**我们的校验器**会不会接受"。
    """
    import base64
    import hashlib
    import hmac
    import json

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    signing_input = (
        f"{b64(json.dumps({'alg': 'HS256', 'typ': 'JWT'}).encode())}."
        f"{b64(json.dumps(claims).encode())}"
    ).encode()
    signature = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{signing_input.decode()}.{b64(signature)}"


def test_algorithm_confusion_is_rejected(signing_keys: dict[str, str]) -> None:
    """**算法混淆攻击**：把 RS256 换成 HS256，用**公钥**当 HMAC 密钥签名。

    公钥是公开的，所以这种令牌攻击者能随便造。防线是"算法来自**配置**、
    绝不取自令牌的 `alg` 头"—— 本测试就是那条防线的证据：
    若校验器按令牌自报的 `alg` 选算法，这枚令牌会被验成合法，
    于是一个任何人都能伪造的身份就成立了。
    """
    provider = _jwt_provider(signing_keys["public"])
    forged = _forge_hs256_token(signing_keys["public"], _claims())

    with pytest.raises(AuthenticationError):
        provider.resolve(_bearer(forged))


def test_algorithm_confusion_forgery_is_actually_well_formed(
    signing_keys: dict[str, str],
) -> None:
    """上一条测试的**对照组**：手工造的那枚令牌本身语法正确。

    没有这一条，`_forge_hs256_token` 写错（比如 base64 少了 padding 处理）
    会让上一条测试**因为无关的原因**通过 —— 于是"算法混淆被挡住"这份结论
    就是假的，而它看起来完全正常。
    """
    import base64
    import hashlib
    import hmac
    import json

    forged = _forge_hs256_token(signing_keys["public"], _claims())
    header_segment, payload_segment, signature_segment = forged.split(".")
    pad = lambda segment: segment + "=" * (-len(segment) % 4)  # noqa: E731

    assert json.loads(base64.urlsafe_b64decode(pad(header_segment)))["alg"] == "HS256"
    assert json.loads(base64.urlsafe_b64decode(pad(payload_segment)))["sub"] == "u-1"
    # 独立重算一次 HMAC：签名段确实是"公钥当密钥"算出来的，
    # 而不是随手填的假字符串（后者会让上一条测试因为语法错误而通过）
    assert base64.urlsafe_b64decode(pad(signature_segment)) == hmac.new(
        signing_keys["public"].encode(),
        f"{header_segment}.{payload_segment}".encode(),
        hashlib.sha256,
    ).digest()


def test_missing_bearer_scheme_is_rejected(signing_keys: dict[str, str]) -> None:
    """`Basic` 之类的凭据不是令牌；送去解签只会得到语焉不详的解码错误。"""
    provider = _jwt_provider(signing_keys["public"])

    with pytest.raises(AuthenticationError):
        provider.resolve({"Authorization": "Basic dXNlcjpwYXNz"})


def test_missing_authorization_header_is_rejected(signing_keys: dict[str, str]) -> None:
    provider = _jwt_provider(signing_keys["public"])

    with pytest.raises(AuthenticationError):
        provider.resolve({})


def test_missing_tenant_claim_is_rejected(signing_keys: dict[str, str]) -> None:
    """缺租户声明时**不用默认租户兜底**：那会造出一个不属于任何租户的身份。"""
    provider = _jwt_provider(signing_keys["public"])
    claims = _claims()
    del claims["tenant_id"]

    with pytest.raises(AuthenticationError):
        provider.resolve(_bearer(_token(signing_keys["private"], claims)))


def test_jwt_tenant_mismatch_is_rejected(signing_keys: dict[str, str]) -> None:
    provider = _jwt_provider(signing_keys["public"])

    with pytest.raises(AuthenticationError):
        provider.resolve(
            _bearer(_token(signing_keys["private"], _claims(tenant_id="other-tenant")))
        )


def test_roles_claim_accepts_a_plain_string(signing_keys: dict[str, str]) -> None:
    """企业 IdP 的声明形态不统一，字符串形态也要认。"""
    provider = _jwt_provider(signing_keys["public"])

    actor = provider.resolve(
        _bearer(_token(signing_keys["private"], _claims(roles="system_admin")))
    )

    assert Permission.RULE_MANAGE in actor.permissions


def test_roles_claim_of_unknown_shape_grants_nothing(signing_keys: dict[str, str]) -> None:
    """形态不认识 → 当作"没有声明角色"，即不给任何权限（fail-closed）。"""
    provider = _jwt_provider(signing_keys["public"])

    actor = provider.resolve(_bearer(_token(signing_keys["private"], _claims(roles={"a": 1}))))

    assert actor.permissions == frozenset()


def test_jwks_unreachable_is_a_transient_error_not_401() -> None:
    """**JWKS 不可达 ≠ 令牌无效。**

    判成 401 会让调用方丢弃一份**可能完全有效**的令牌去重新登录，
    而正确处置只是稍后重试。
    """

    class _UnreachableJwks:
        def get_signing_key_from_jwt(self, _token: str) -> Any:
            raise pyjwt.PyJWKClientConnectionError("connection refused")

    provider = JwtIdentity(
        tenant_id=TENANT,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url="https://idp.example.com/.well-known/jwks.json",
        jwks_client=_UnreachableJwks(),
    )

    with pytest.raises(IdentityProviderUnavailable):
        provider.resolve(_bearer("any.token.value"))


def test_jwks_unreachable_maps_to_503_with_retry_after() -> None:
    """连带断言**HTTP 映射**：503 + Retry-After，而不是 401。"""
    error = IdentityProviderUnavailable("JWKS 不可达")

    assert http_status_for(error) == 503
    assert error_headers(error)["Retry-After"]


def test_rejected_token_text_never_appears_in_the_error(
    signing_keys: dict[str, str],
) -> None:
    """**被拒的令牌原文不得出现在错误消息里。**

    这条是"不许记录 bearer 令牌"那条要求的可执行形态。错误消息会进
    日志、进 500 响应体、进工单截图 —— 令牌一旦进去，就等于
    **把一份还能用的凭据抄送给了所有能看到日志的人**。
    写 `f"令牌 {token} 验签失败"` 是最自然的写法，也正因如此，
    它需要一条测试来挡住。

    三种拒绝路径都验：签名不符、已过期、以及**语法就是坏的**。
    """
    provider = _jwt_provider(signing_keys["public"])
    import time

    secret_marker = "super-secret-claim-value"
    bad_tokens = {
        "签名不符": _token(other_private_key(signing_keys), _claims()),
        "已过期": _token(signing_keys["private"], _claims(exp=int(time.time()) - 60, name=secret_marker)),
        "结构损坏": "not-a-jwt-at-all",
        "载荷含标记": _token(signing_keys["private"], _claims(aud="wrong", name=secret_marker)),
    }

    for label, token in bad_tokens.items():
        with pytest.raises(AuthenticationError) as excinfo:
            provider.resolve(_bearer(token))

        message = str(excinfo.value)
        assert token not in message, f"{label}：错误消息里出现了令牌原文"
        assert secret_marker not in message, f"{label}：错误消息里出现了载荷内容"


def other_private_key(signing_keys: dict[str, str]) -> str:
    """换一对密钥的私钥（用另一份自签密钥，避免污染模块级 fixture）。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def test_missing_key_from_jwks_is_401_not_503() -> None:
    """连得上但找不到对应 `kid` → 是**令牌**的问题，不是环境的问题。"""

    class _NoSuchKey:
        def get_signing_key_from_jwt(self, _token: str) -> Any:
            raise pyjwt.PyJWKClientError("Unable to find a signing key")

    provider = JwtIdentity(
        tenant_id=TENANT,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url="https://idp.example.com/.well-known/jwks.json",
        jwks_client=_NoSuchKey(),
    )

    with pytest.raises(AuthenticationError):
        provider.resolve(_bearer("any.token.value"))


# ============================================================
# 授权依赖
# ============================================================


def test_require_permissions_passes_for_an_allowed_role() -> None:
    dependency = require_permissions(Permission.RESULT_SAVE)
    actor = _actor(roles=[Role.LEGAL_REVIEWER.value])

    assert dependency(actor=actor) is actor


def test_require_permissions_denies_a_read_only_actor() -> None:
    dependency = require_permissions(Permission.RESULT_SAVE)

    with pytest.raises(AuthorizationError) as excinfo:
        dependency(actor=_actor(roles=[Role.READ_ONLY_AUDITOR.value]))

    assert Permission.RESULT_SAVE in str(excinfo.value)


def test_denial_message_names_the_unrecognised_roles() -> None:
    """被拒的人必然会问"我明明有那个角色" —— 线索必须在他手里。"""
    dependency = require_permissions(Permission.RESULT_SAVE)

    with pytest.raises(AuthorizationError) as excinfo:
        dependency(actor=_actor(roles=["legal_reviewr"]))  # 拼错一个字母

    message = str(excinfo.value)
    assert "legal_reviewr" in message
    assert "未被识别" in message


def test_require_permissions_with_no_arguments_admits_any_authenticated_actor() -> None:
    dependency = require_permissions()

    assert dependency(actor=_actor()).actor_id == "u-1"


def test_get_actor_attaches_the_actor_to_request_scope() -> None:
    """解析结果写进 `request.state`，供**依赖之外**的代码读取。

    关联 ID 中间件在最外层、异常处理器在依赖之外被调用，两者都拿不到
    `Actor` 对象；`request.state` 是它们唯一都能看到的地方。
    """
    from dataclasses import fields
    from types import SimpleNamespace

    from app.api.deps import get_actor

    class _StubRequest:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers
            self.state = SimpleNamespace()

    request = _StubRequest({"X-Actor-Id": "u-1", "X-Actor-Roles": "legal_reviewer"})

    actor = get_actor(request, provider=DevHeaderIdentity(tenant_id=TENANT))

    assert request.state.actor is actor
    # `Actor` 里**没有任何凭据字段**：挂上去的是解析结果，不是原始请求头。
    # 把请求头挂到 request.state 上，等于给未来任何一处日志或序列化
    # 留了一条把 `Authorization` 原文写出去的路。
    assert {f.name for f in fields(actor)} == {
        "actor_id",
        "display_name",
        "roles",
        "tenant_id",
    }


def test_authentication_and_authorization_errors_are_distinguishable() -> None:
    """401 与 403 的**机器判据**必须不同。

    合并时客户端只能对两种完全不同的处置二选一，必然有一半是错的。
    """
    unauthenticated = AuthenticationError("没有身份")
    forbidden = AuthorizationError("缺权限")

    assert http_status_for(unauthenticated) == 401
    assert http_status_for(forbidden) == 403
    assert unauthenticated.code != forbidden.code


# ============================================================
# HTTP 端到端：401 / 403 真的从接口出来
# ============================================================


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """真实的 app，但把数据库与外部适配器替换掉。

    **刻意不覆盖 `get_actor`**：本文件要验的正是身份依赖本身，
    覆盖掉它就等于把被考的代码换成了测试自己的桩。
    """
    app.dependency_overrides[get_db] = _no_database
    app.dependency_overrides[get_gateway] = lambda: None
    app.dependency_overrides[get_storage] = lambda: None
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        # 用 finally 而不是放在 yield 之后：断言失败时也要清干净，
        # 否则一次失败会让**后续**测试带着陈旧覆盖运行，红的地方就全错了
        app.dependency_overrides.clear()


def _no_database() -> Iterator[None]:
    """身份校验失败时端点根本不会执行，因此不需要真库。"""
    yield None


def test_request_without_identity_is_401(client: TestClient) -> None:
    response = client.post("/tools/save_review_result", json={"run_id": 1})

    assert response.status_code == 401
    # 401 必须带 WWW-Authenticate：不带时部分客户端不会触发"取新令牌后重试"
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_request_with_read_only_role_is_403_on_mutation(client: TestClient) -> None:
    """**只读角色执行写操作被后端拒绝（403）** —— 需求原文的落点。"""
    response = client.post(
        "/tools/save_review_result",
        json={"run_id": 1},
        headers={"X-Actor-Id": "auditor-1", "X-Actor-Roles": "read_only_auditor"},
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "PERMISSION_DENIED"
    # 403 不应带 WWW-Authenticate：它表示"带了也没用"
    assert "WWW-Authenticate" not in response.headers


def test_request_with_unrecognised_role_is_403_not_500(client: TestClient) -> None:
    """角色名不认识 → 403，**不能是 500**。

    500 会让"配置漂移"看起来像"服务端有 bug"，把排查带向完全错误的方向。
    """
    response = client.post(
        "/tools/save_review_result",
        json={"run_id": 1},
        headers={"X-Actor-Id": "u-1", "X-Actor-Roles": "legal_reviewr"},
    )

    assert response.status_code == 403


def test_request_with_mismatched_tenant_is_401(client: TestClient) -> None:
    response = client.post(
        "/tools/save_review_result",
        json={"run_id": 1},
        headers={"X-Actor-Id": "u-1", "X-Tenant-Id": "someone-else"},
    )

    assert response.status_code == 401


def test_writeback_requires_the_writeback_permission(client: TestClient) -> None:
    """工具 7 的权限与工具 6 **不同**：只读角色两个都拒，
    但要求的是各自的权限，不是同一个粗粒度开关。"""
    response = client.post(
        "/tools/write_approval_comment",
        json={"instance_id": "HT-1", "result_id": 1},
        headers={"X-Actor-Id": "auditor-1", "X-Actor-Roles": "read_only_auditor"},
    )

    assert response.status_code == 403


def test_authorised_actor_gets_past_the_auth_gate(client: TestClient) -> None:
    """有权限的主体**不会**被身份层拦住。

    它会被后面的业务层拒绝（这里没有真库），关键断言是
    **状态码不是 401/403** —— 身份与授权这一关确实放行了。
    """
    response = client.post(
        "/tools/save_review_result",
        json={"run_id": 1},
        headers={"X-Actor-Id": "reviewer-1", "X-Actor-Roles": "legal_reviewer"},
    )

    assert response.status_code not in (401, 403)
