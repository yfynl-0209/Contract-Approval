"""人工重试、运行日志与审计查询（M7 / Task 5）。

## 本模块的三个端点各自回答一个问题

| 端点 | 问题 | 权限 |
| --- | --- | --- |
| `POST /api/tasks/{id}/retry` | 这条卡住的任务怎么恢复 | `ops:retry` |
| `GET /api/logs/{task_id}` | 它到底经历了什么 | `task:read` |
| `GET /api/audit` | 谁对它做了什么决定 | `audit:read` |

## 为什么重试与日志不是同一个权限

日志是**过程证据**（谁都会遇到失败，法务审核人需要看自己那份合同卡在哪），
重试是**运维动作**（它会重新排作业、消耗重试预算、改任务状态）。
把前者也锁进管理员权限，会让审核人看不到自己的合同为什么没动 ——
于是他只能来问运维，而运维能看到的并不比日志更多。

## 为什么"审计"与"日志"是两个端点

`task_logs` 是**可回滚的运行日志**（与业务状态同事务写入），
`audit_events` 是**只追加的审计账**（谁在什么时候对什么做了关键动作）。
两者混成一个接口时，使用者无法区分"这件事发生过"与"这件事有人做过决定" ——
而后者才是追责与合规的凭据。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permissions
from app.api.views import iso, page_json
from app.auth import Actor, Permission
from app.models import AuditEvent, TaskLog
from app.schemas import RetryTaskRequest
from app.services import query_service
from app.services.retry_service import retry_task

router = APIRouter(prefix="/api", tags=["人工重试、日志与审计（M7）"])


# ============================================================
# 人工重试
# ============================================================


@router.post(
    "/tasks/{task_id}/retry",
    summary="人工重试阻塞任务（从失败检查点恢复）",
    description=(
        "把一条 `blocked` 任务从**当初失败的那一步**恢复，并留下审计与日志。\n\n"
        "| `blocked_stage` | 重跑 | 任务回到 |\n"
        "| --- | --- | --- |\n"
        "| `parse` | 重新入队解析作业 | `parsing` |\n"
        "| `rule` | 重新入队规则作业 | `reviewing` |\n"
        "| `result` | 重新入队结果作业（**不重跑规则**） | `reviewing` |\n"
        "| `writeback` | **只重新武装 Outbox 投递**，不碰解析与规则 | `reviewing` |\n\n"
        "⚠️ 回写失败**不新建作业**：回写意图早在 `comment_logs` + `outbox_events` 里，"
        "失败的是**送达**。再登记一次意图没有意义（幂等键相同 → 复用旧尝试），"
        "正确做法是把那条 Outbox 事件从 `failed` 置回 `pending` 并**归还重试预算**。\n\n"
        "⚠️ `pull` / `detail` / `download` 三种失败位置返回 **409 "
        "`RETRY_NOT_SUPPORTED`**：它们由工具 1–3 在**同步**路径上完成，"
        "恢复入口是重跑对应工具（重跑工具 3 成功后任务会自动从检查点恢复）。"
        "把这一步硬映射成一个 Worker 永远不会领取的作业，"
        "会让任务回到 `parsing` 后**永远停住**。\n\n"
        "**状态码**：任务不存在 / 跨租户 → 404；任务不在 `blocked` → 409；"
        "该失败位置没有可重跑的对象 → 409；缺少操作原因 → 400。"
    ),
)
def retry_task_route(
    task_id: int,
    payload: RetryTaskRequest,
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.OPS_RETRY)),
) -> dict[str, Any]:
    outcome = retry_task(
        session,
        tenant_id=actor.tenant_id,
        task_id=task_id,
        reason=payload.reason,
        actor=actor,
    )
    return {
        "task_id": outcome.task_id,
        "blocked_stage": outcome.blocked_stage,
        "resumed_status": outcome.resumed_status,
        "retry_count": outcome.retry_count,
        "reason": outcome.reason,
        # "这次重试做了什么"是调用方最需要的一行：它区分"排了一个作业"
        # 与"重新武装了一次投递" —— 从 blocked_stage 再推一遍会多出一份会漂移的判据。
        "action": outcome.action,
        "job_id": outcome.job_id,
        "job_type": outcome.job_type,
        "job_status": outcome.job_status,
        "attempt_id": outcome.attempt_id,
        "outbox_event_id": outcome.outbox_event_id,
        "status_url": f"/api/tasks/{outcome.task_id}",
    }


# ============================================================
# 运行日志
# ============================================================


@router.get(
    "/logs/{task_id}",
    summary="任务运行日志（最新在前）",
    description=(
        "返回某条任务的运行日志，可按级别、类型与**关联 ID** 过滤。\n\n"
        "⚠️ `correlation_id` 是本接口存在的关键：日志散在 API 与 Worker "
        "**两个进程**里，那个 ID 是唯一能把它们重新拼成一条链的键 —— "
        "不给这个过滤条件，排障的人只能拉全表再自己筛。\n\n"
        "`log_content` 已由 `LogService` 脱敏（键名黑名单 / 长文本摘要 / "
        "身份证手机邮箱模式），**合同正文与令牌不会出现在这里**。\n\n"
        "跨租户与不存在都返回 **404**（不给枚举线索）。"
    ),
)
def list_logs(
    task_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=query_service.DEFAULT_PAGE_SIZE, ge=1, le=query_service.MAX_PAGE_SIZE
    ),
    log_level: str | None = Query(default=None, description="debug/info/warning/error"),
    log_type: str | None = Query(default=None, description="system/pull/parse/rule/…"),
    correlation_id: str | None = Query(
        default=None, description="按关联 ID 过滤一次请求的全链路"
    ),
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    # 归属校验走 `get_task`：日志表自己没有 tenant_id，判据挂在任务上。
    query_service.get_task(session, tenant_id=actor.tenant_id, task_id=task_id)
    result = query_service.list_task_logs(
        session,
        task_id=task_id,
        page=page,
        page_size=page_size,
        log_level=log_level,
        log_type=log_type,
        correlation_id=correlation_id,
    )
    return page_json(result, _log_json)


def _log_json(row: TaskLog) -> dict[str, Any]:
    return {
        "log_id": row.id,
        "task_id": row.task_id,
        "log_level": row.log_level,
        "log_type": row.log_type,
        "log_content": row.log_content,
        "error_code": row.error_code,
        "correlation_id": row.correlation_id,
        "created_at": iso(row.created_at),
    }


# ============================================================
# 审计事件
# ============================================================


@router.get(
    "/audit",
    summary="审计事件（只追加，只读）",
    description=(
        "返回审计事件，按时间倒序。可按 `task_id` 与 `action` 过滤。\n\n"
        "⚠️ **系统级事件默认不可见**。M7 起 `audit_events.task_id` 可为空"
        "（规则变更影响所有任务，不属于任何一条任务）。"
        "默认只返回**本租户任务**上的事件 —— 想看全局配置变更请显式传 "
        "`include_system=true`。默认值选「看不到」是 fail-closed 的方向："
        "漏看一条只是少一点信息，多看到别人的是数据泄漏。\n\n"
        "`detail` 只含标识与摘要（`result_id` / `content_digest` / `changed_fields`），"
        "**不含合同正文**。\n\n"
        "`action` 是**白名单枚举**：拼错的值得到 400 而不是空列表。"
    ),
)
def list_audit(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=query_service.DEFAULT_PAGE_SIZE, ge=1, le=query_service.MAX_PAGE_SIZE
    ),
    task_id: int | None = Query(default=None, description="只看某条任务的事件"),
    action: str | None = Query(default=None, description="RESULT_CONFIRMED / …"),
    include_system: bool = Query(
        default=False, description="是否包含不属于任何任务的系统级事件（规则变更）"
    ),
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.AUDIT_READ)),
) -> dict[str, Any]:
    result = query_service.list_audit_events(
        session,
        tenant_id=actor.tenant_id,
        page=page,
        page_size=page_size,
        task_id=task_id,
        action=action,
        include_system=include_system,
    )
    return page_json(result, _audit_json)


def _audit_json(row: AuditEvent) -> dict[str, Any]:
    return {
        "event_id": row.id,
        "task_id": row.task_id,
        "actor_id": row.actor_id,
        "actor_name": row.actor_name,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "correlation_id": row.correlation_id,
        "detail": _json_or_none(row.detail_json),
        "created_at": iso(row.created_at),
    }


def _json_or_none(raw: str | None) -> Any:
    """库里的 JSON 文本 → 对象；解析不了返回 `None` 而不是抛错。

    一条 `detail_json` 损坏的记录，其动作与操作者仍然是有用的信息 ——
    让整个接口 500 会把"这条记录坏了"变成"审计接口坏了"。
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None
