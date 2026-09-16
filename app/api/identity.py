"""当前身份与权限查询（M8 / Task 2）。

## 为什么需要这个接口

前端要**按权限渲染导航**（"规则管理"对只读审计不显示），而它自己**算不出来**：

| 让它自己算 | 后果 |
| --- | --- |
| 从 JWT 声明里解角色再套一张表 | 角色→权限映射在浏览器里有了**第二份实现**。两份漂移时表现为"入口在、点了 403"（或反过来"有权限却看不到入口"），而**两边都不报错** |
| 试错（发一个请求看是不是 403） | 每个页面启动时都要多打一轮请求，且"没权限"与"网络失败"在界面上分不开 |

因此权限**由服务端算好下发**（`Actor.permissions` 的同一份实现，见 `app/auth.py`）。

## ⚠️ 这是唯一一个**只要求已认证**的 `/api` 端点

不挂 `require_permissions(...)`：每个已认证主体都有权知道"我是谁、我能做什么"。
把它锁进某个权限，会让"我到底有没有权限"这件事在**回答之前**先被拒一次 ——
用户看到的是 403，而这个 403 恰恰不说明他缺什么。

## 为什么 `unknown_roles` 一定下发

IdP 先上了新角色、本系统还没发布对应映射时，该角色**不被识别也不带来权限**
（fail-closed，见 `Actor.permissions`）。此时用户看到的是"这个按钮没了"，
而唯一的线索就是"你有一个角色我不认识"。不发它，排障只能去猜。

## 不下发的东西

响应里**没有** `Authorization`、没有任何请求头原文、没有令牌。
`Actor` 是解析后的结果，本身不含凭据（`app/api/deps.py::get_actor` 也按这条纪律
只把 `Actor` 放进 `request.state`）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.deps import get_actor
from app.auth import Actor

router = APIRouter(prefix="/api", tags=["身份（M8）"])


@router.get(
    "/me",
    summary="当前身份与权限（前端按它渲染导航）",
    description=(
        "返回当前请求的已认证主体：标识、显示名、租户、角色声明与**已展开的权限**。\n\n"
        "`permissions` 是服务端按 `app/auth.py::ROLE_PERMISSIONS` 算出的结果，"
        "**前端不得自己从 `roles` 推** —— 两份判据漂移时不会有任何报错。\n\n"
        "⚠️ 前端的按权限隐藏**只是提示**（材料 §5.3）：真正的判据在每个端点的 "
        "`require_permissions`，手敲 URL 直达页面依然会被后端拒绝。\n\n"
        "**401** = 没有可信身份（去拿一份身份）；**不会返回 403**："
        "知道自己是谁不需要任何权限。"
    ),
)
def get_me(actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    return {
        "actor_id": actor.actor_id,
        "display_name": actor.display_name,
        "tenant_id": actor.tenant_id,
        # 排序后输出：集合的迭代顺序不保证稳定，而"同一份身份两次请求
        # 得到顺序不同的权限列表"会让前端的缓存对比与测试断言变成偶发失败。
        "roles": sorted(actor.roles),
        "unknown_roles": sorted(actor.unknown_roles),
        "permissions": sorted(actor.permissions),
    }
