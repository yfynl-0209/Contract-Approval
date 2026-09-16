"""身份提供方端口：把"请求里带的凭据"翻译成**已认证主体**（M7）。

实现有两类：开发期读显式请求头、生产校验 JWT。业务层与工具门面
只认 `Actor`，不关心身份是怎么解出来的 —— 因此新增一种身份来源
（OIDC、企业 SSO）时**不需要改任何业务代码**。

## 为什么入参是"请求头"而不是"请求对象"

端口若接收 FastAPI 的 `Request`，MCP 形态就没法用它 —— MCP 没有 HTTP 请求，
只有一份入参。收窄成 `Mapping[str, str]` 后，两种协议给的都是"一堆键值对"，
这个端口对两者都成立。

## 失败必须是 `AuthenticationError`

实现**不得**在解不出身份时返回 `None` 或抛出裸异常：

| 做法 | 后果 |
| --- | --- |
| 返回 `None` | 调用方必须记得判空，**漏判的那一处会以匿名身份继续执行** |
| 抛裸 `ValueError` | 被通用处理器翻译成 400"参数非法" —— 而正确状态码是 401 |
| 抛 `AuthenticationError` | 401 + `WWW-Authenticate`，调用方知道该去换一份身份 |
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from app.auth import Actor


@runtime_checkable
class IdentityProvider(Protocol):
    """凭据 → `Actor`。

    ⚠️ `runtime_checkable` 的 `isinstance` 只检查方法名，签名与语义由测试守。
    """

    def resolve(self, headers: Mapping[str, str]) -> Actor:
        """解析身份。

        Args:
            headers: 请求头（大小写不敏感的实现由各适配器自行负责 ——
                真实请求头来自 Starlette 的 `Headers`（不敏感），
                而测试里常传普通 dict（敏感），适配器必须**两边都对**）。

        Returns:
            已认证主体。

        Raises:
            AuthenticationError: 缺少凭据、凭据无效 / 过期、
                或凭据声明的租户与当前实例不一致。
        """
        ...

    def close(self) -> None:
        """释放长生命周期资源（如 JWKS 客户端的连接池）。"""
        ...
