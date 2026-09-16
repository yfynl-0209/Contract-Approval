"""规则管理接口（M7 / Task 5）：查询、新建、**版本化**修改与激活前校验。

## 本模块只做形态转换

业务判断（能不能改、版本要不要升、配置合法吗）全在
`app/services/rule_admin_service.py`。这里只把请求体翻成服务参数、
把服务结果翻成 JSON —— 与 `app/api/tools.py` 同一条约定（README §4.1）。
一旦这里出现第一个业务 `if`，MCP 形态要么复制它、要么依赖它。

## 权限：全部走 `rule:manage`

规则是"系统怎么判"的**输入**：改一条规则会改变所有合同的结论。
它与"审这份合同"不是同一个权限层级，因此整个模块（**含只读查询**）
都要求 `rule:manage` —— 而 `rule:manage` 只授给系统管理员。

只读审计确实看不到规则定义，但这不是缺口：审计要的是"谁在什么时候改了什么"
（`GET /api/audit`），而不是规则当前的内容。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permissions
from app.api.views import page_json
from app.auth import Actor, Permission
from app.schemas import CreateRuleRequest, UpdateRuleRequest
from app.services import query_service, rule_admin_service

router = APIRouter(prefix="/api", tags=["规则管理（M7）"])


# ============================================================
# 激活前校验（"reload"）
# ============================================================
# ⚠️ 本路由**必须声明在** `/rules/{rule_code}` 之前。虽然两者的方法不同
# （这里是 POST、那里是 GET）因而不会互相遮蔽，但把更具体的路径写在前面
# 是 FastAPI 的稳定约定 —— 一旦有人日后给 `/rules/{rule_code}` 加上 POST，
# 遮蔽就是静默的（后注册的那一条永远不生效）。


@router.post(
    "/rules/reload",
    summary="激活前校验（规则集体检）",
    description=(
        "把**整批**规则（含停用）过一遍同一套判据，通过才给出当前规则集版本。\n\n"
        "## 为什么叫 reload 却只做校验\n\n"
        "规则**没有缓存**：每次评价都从 `review_rules` 现读。"
        "因此「重新加载」这个动作在实现上不存在 —— 真正需要的是一个**闸门**："
        "规则改完之后、被下一个批次用上之前，必须有一次「整批都合法吗」的检查。\n\n"
        "判据与 `scripts/check_rules.py` **完全相同**"
        "（`app/rules/validation.py`，两边共用一份实现）—— 命令行通过、"
        "这里也通过，不存在两份会分叉的检查。\n\n"
        "⚠️ 校验覆盖**停用**的规则：「先停用、再改、再启用」的流程里，"
        "停用期间配置是坏的不会被任何人发现，直到启用那一刻才炸 ——"
        "而那时它已经进了一个批次。\n\n"
        "不通过返回 **400**，消息里逐条列出**全部**问题（不是第一个）："
        "配置是人手写的，一次报一条会让人反复往返。"
    ),
)
def reload_rules(
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.RULE_MANAGE)),
) -> dict[str, Any]:
    return rule_admin_service.activate_validation(session, actor=actor).as_json()


# ============================================================
# 规则列表
# ============================================================


@router.get(
    "/rules",
    summary="规则列表（按执行顺序）",
    description=(
        "排序 `priority ASC, id ASC` —— 与引擎的**执行顺序一致**。\n\n"
        "⚠️ 按 `created_at` 排会让界面上的顺序与引擎里的顺序不同，"
        "而「界面说第 3 条先跑、引擎实际先跑第 7 条」是一种没人会去核对的偏差。\n\n"
        "`rule_status` / `rule_category` / `match_mode` 可选，"
        "取值都按白名单校验（拼错 → 400，不是空列表）。"
    ),
)
def list_rules(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=query_service.DEFAULT_PAGE_SIZE, ge=1, le=query_service.MAX_PAGE_SIZE
    ),
    rule_status: str | None = Query(default=None, description="active / inactive"),
    rule_category: str | None = Query(default=None, description="11 类之一"),
    match_mode: str | None = Query(
        default=None, description="keyword / regex / llm / expr"
    ),
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.RULE_MANAGE)),
) -> dict[str, Any]:
    result = rule_admin_service.list_rules(
        session,
        page=page,
        page_size=page_size,
        rule_status=rule_status,
        rule_category=rule_category,
        match_mode=match_mode,
    )
    return page_json(result, rule_admin_service.rule_view)


# ============================================================
# 新建
# ============================================================


@router.post(
    "/rules",
    summary="新建审查规则",
    description=(
        "新建一条规则。**先校验配置、再落库**：配置非法返回 400 且**一个字段都不写**。\n\n"
        "`rule_code` 是**稳定标识**：它进历史评价、进批次快照、进界面，"
        "因此建好之后不提供改名入口（改名等于让历史记录指向一个不存在的规则）。"
        "重复的 `rule_code` 返回 400，并提示改用「修改」。\n\n"
        "写不可变审计事件 `RULE_CREATED`（`task_id` 为空 —— 规则变更不属于任何任务）。"
    ),
)
def create_rule(
    payload: CreateRuleRequest,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.RULE_MANAGE)),
) -> dict[str, Any]:
    rule = rule_admin_service.create_rule(
        session,
        # `mode="json"`：枚举字段落库取**字符串值**，与数据库 CHECK 的取值域一致
        payload=payload.model_dump(mode="json"),
        actor=actor,
    )
    return rule_admin_service.rule_view(rule)


# ============================================================
# 详情
# ============================================================


@router.get(
    "/rules/{rule_code}",
    summary="规则详情",
    description="与列表行的形状**完全一致**。不存在 → 404 + `RULE_NOT_FOUND`。",
)
def get_rule(
    rule_code: str,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.RULE_MANAGE)),
) -> dict[str, Any]:
    return rule_admin_service.rule_view(
        rule_admin_service.get_rule(session, rule_code=rule_code)
    )


# ============================================================
# 修改（版本化）
# ============================================================


@router.patch(
    "/rules/{rule_code}",
    summary="修改审查规则（版本化）",
    description=(
        "只修改请求体里**显式提供**的字段（`exclude_unset`）。\n\n"
        "⚠️ 全字段可选的请求体若按「缺省即清空」处理，一次只想改名字的调用会把 "
        "`applies_when_json` 悄悄清成 `NULL` —— 规则于是从「仅对软件合同适用」"
        "退化成「全局适用」，而响应里看不出任何异常。"
        "显式传 `null` 仍然是**有意义的操作**（清空该字段）。\n\n"
        "## 版本化规则\n\n"
        "| 情形 | 结论 |\n"
        "| --- | --- |\n"
        "| 改了**判定语义**（`match_text` / `applies_when` / `risk_level` / "
        "`priority` …）、版本提升 | 允许 |\n"
        "| 改了判定语义、版本不变、该版本**已被审查引用过** | **409 "
        "`RULE_VERSION_IN_USE`**，提示提升版本 |\n"
        "| 改了判定语义、版本不变、该版本从未被引用 | 允许（首次使用前修正） |\n"
        "| 版本倒退 | 409（历史评价已按旧版本留痕） |\n\n"
        "判据是 `rule_hits.rule_version` 这个**评价当时的版本快照**："
        "存在同版本的评价行，就说明该版本已被真实审查引用过 ——"
        "此时就地改内容会让「版本 N 的含义」被静默改写。\n\n"
        "`rule_status`（启停用）不算判定语义变更，不需要换版本："
        "它进批次快照与 `ruleset_version`，启停用本身就会让后续批次换个规则集版本。\n\n"
        "**幂等**：一次没有实际变化的 PATCH 不是变更 —— 不换版本、不追加审计事件。"
        "响应的 `changed` 字段说明这次到底改没改。"
    ),
)
def update_rule(
    rule_code: str,
    payload: UpdateRuleRequest,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.RULE_MANAGE)),
) -> dict[str, Any]:
    rule, changed = rule_admin_service.update_rule(
        session,
        rule_code=rule_code,
        changes=payload.model_dump(mode="json", exclude_unset=True),
        actor=actor,
    )
    return {**rule_admin_service.rule_view(rule), "changed": changed}
