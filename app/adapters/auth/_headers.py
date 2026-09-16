"""请求头读取的小工具（仅 `app/adapters/auth/` 内部使用）。"""

from __future__ import annotations

from collections.abc import Mapping


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    """**大小写不敏感**地取一个请求头，不存在时返回 `None`。

    ⚠️ 不能直接 `headers.get(name)`：

    | 来源 | 大小写行为 |
    | --- | --- |
    | Starlette `Headers`（真实请求） | 不敏感，`get` 怎么写都能取到 |
    | 普通 `dict`（测试、MCP 入参） | **敏感**，`"x-actor-id"` 取不到 `"X-Actor-Id"` |

    只按一种来源写，就会出现"线上好、测试红"或反过来 ——
    而这两种表现都不指向真正的原因（大小写），排障会先去怀疑别处。
    """
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def bearer_token(headers: Mapping[str, str]) -> str | None:
    """从 `Authorization` 头里取出 Bearer 令牌，缺失或格式不符时返回 `None`。

    只认 `Bearer`（大小写不敏感）：把 `Basic` 之类的凭据当令牌送去解签，
    只会得到一个语焉不详的解码错误，而真正的问题是**用错了认证方式**。
    """
    raw = header_value(headers, "Authorization")
    if raw is None:
        return None
    scheme, _, token = raw.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None
