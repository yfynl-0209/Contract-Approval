"""mock 审批系统 —— 独立 FastAPI 服务（端口 8001）。

它模拟的是**外部企业审批系统**，而不是本项目的内部模块：

- 提供待办拉取、审批详情、附件下载、评论回写四类接口；
- 待办数据携带 4 个**权威业务事实字段**（我方名称 / 合同标签 / 业务角色 / 合同类型）——
  这些是"申请人填写并经审批流程确认"的信息，不是从合同文本推断的；
- 支持按需注入故障（500 / 超时 / 404），用于演示 `blocked` 与人工重试；
- 评论回写按 `idempotency_key` 幂等：**同键重复调用返回第一次的结果**（设计决议 D6）。

启动：
    python -m uvicorn mock_approval.main:app --host 127.0.0.1 --port 8001 --reload
    或 scripts/run_mock.ps1
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from mock_approval.fault_inject import Fault, FaultMode, faults
from mock_approval.sample_pdf import build_for_kind
from mock_approval.store import (
    CONTRACTS_DIR,
    FIXTURES_DIR,
    Attachment,
    WriteOutcome,
    store,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

#: 与 .env 中的 MOCK_APPROVAL_TOKEN 保持一致
TOKEN = os.getenv("MOCK_APPROVAL_TOKEN", "demo-token")

app = FastAPI(
    title="Mock 审批系统（外部对接方）",
    version="0.1.0",
    description=(
        "模拟企业审批系统，供合同审批审查系统对接。\n\n"
        "**注意**：本服务不是审查系统的一部分，而是它的**外部对接方**，"
        "因此是独立进程、独立端口，且不依赖审查系统的任何代码。"
    ),
)


# ============================================================
# 鉴权与故障注入
# ============================================================


def require_token(authorization: str | None = Header(default=None)) -> None:
    """校验 Bearer Token。

    模拟真实企业审批系统的鉴权：调用方必须带凭证。
    """
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="未授权：请携带正确的 Bearer Token")


def raise_if_faulted(target: str, instance_id: str | None = None) -> None:
    """若当前注册了作用于该请求的故障，则按故障类型抛出对应错误。"""
    fault = faults.find(target, instance_id)
    if fault is None:
        return

    if fault.mode == FaultMode.TIMEOUT.value:
        # 先阻塞再返回：让调用方真实经历"超时"，而不是立刻拿到错误码
        time.sleep(fault.delay_seconds)
        raise HTTPException(504, detail=f"[注入故障] 调用超时：{fault.message or target}")
    if fault.mode == FaultMode.NOT_FOUND.value:
        raise HTTPException(
            404, detail=f"[注入故障] 资源不存在：{fault.message or target}"
        )
    raise HTTPException(500, detail=f"[注入故障] 服务端错误：{fault.message or target}")


def _read_attachment_bytes(attachment: Attachment) -> tuple[bytes, str]:
    """读取附件内容，并说明**来源**。返回 `(字节, 来源标记)`。

    优先级：

    1. `data/contracts/` —— 真实合同（M12 黄金合同集 / 生产接入），放进去即生效；
    2. `mock_approval/fixtures/` —— **M4 起的中文合成合同**（提交进仓库的静态夹具）；
    3. 兜底：动态生成 ASCII 占位 PDF（M2 遗留路径）。

    ## 为什么把来源一起返回

    第 3 条**必须是可观测的**。夹具缺失时静默回落到 ASCII 占位 PDF，
    症状是"解析器什么都抽不出来"—— 而病因是"夹具没生成"，两者离得很远，
    中间没有任何环节会报错。把来源写进响应头之后，
    一条测试就能断言"**演示实例的附件全部来自夹具**"，
    让这条回落路径在正常流程里不可能被静默走到。
    """
    real_path = CONTRACTS_DIR / attachment.file_name
    if real_path.exists():
        return real_path.read_bytes(), "contracts"

    fixture_path = FIXTURES_DIR / attachment.file_name
    if fixture_path.exists():
        return fixture_path.read_bytes(), "fixture"

    return build_for_kind(attachment.content_kind), "generated"


# ============================================================
# 请求模型
# ============================================================


class CommentRequest(BaseModel):
    content: str = Field(min_length=1, description="回写的审查意见正文")
    idempotency_key: str = Field(
        min_length=8,
        description="幂等键；同键重复调用返回第一次的结果，不重复生成评论",
    )
    operator_name: str | None = Field(default=None, description="操作人（留痕用）")


class FaultRequest(BaseModel):
    target: str = Field(description="目标接口：list_pending / get_detail / download / write_comment / *")
    mode: str = Field(default=FaultMode.HTTP_500.value, description="http_500 / timeout / not_found")
    instance_id: str | None = Field(default=None, description="只对该审批单生效；为空则对该接口全部生效")
    delay_seconds: float = Field(default=5.0, description="timeout 模式下阻塞的秒数")
    message: str = Field(default="", description="附加说明，会写进错误响应")


# ============================================================
# 系统
# ============================================================


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    """无需鉴权，便于启动脚本探测服务是否就绪。"""
    return {"status": "ok", "service": "mock-approval", "token_required": True}


# ============================================================
# 审批单
# ============================================================


@app.get("/api/instances/pending", tags=["审批单"], summary="拉取待处理审批单列表")
def list_pending(
    limit: int = Query(default=20, ge=1, le=100),
    _: None = Depends(require_token),
) -> dict:
    raise_if_faulted("list_pending")
    items = store.list_pending(limit)
    return {"total": len(items), "items": [item.to_pending_dict() for item in items]}


@app.get("/api/instances/{instance_id}", tags=["审批单"], summary="查询审批单详情")
def get_detail(instance_id: str, _: None = Depends(require_token)) -> dict:
    raise_if_faulted("get_detail", instance_id)
    instance = store.get(instance_id)
    if instance is None:
        raise HTTPException(404, detail=f"审批单 {instance_id} 不存在")
    return instance.to_detail_dict()


@app.get(
    "/api/instances/{instance_id}/attachments/{attachment_id}/download",
    tags=["审批单"],
    summary="下载合同附件",
)
def download_attachment(
    instance_id: str, attachment_id: str, _: None = Depends(require_token)
) -> Response:
    raise_if_faulted("download", instance_id)

    attachment = store.find_attachment(instance_id, attachment_id)
    if attachment is None:
        raise HTTPException(404, detail=f"附件 {attachment_id} 不存在")
    if not attachment.available:
        # 模拟"附件在审批系统中已被删除" —— 审查系统应据此进入 blocked
        raise HTTPException(404, detail=f"附件 {attachment_id} 在审批系统中已被删除")

    content, source = _read_attachment_bytes(attachment)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{attachment.file_name}"',
            "X-Attachment-Id": attachment.attachment_id,
            "X-Content-Kind": attachment.content_kind,
            # fixture / contracts / generated —— 让"夹具缺失时静默回落"变得可观测
            "X-Fixture-Source": source,
        },
    )


# ============================================================
# 评论回写
# ============================================================


@app.post(
    "/api/instances/{instance_id}/comments",
    tags=["评论"],
    summary="回写评论（按 idempotency_key 幂等）",
)
def write_comment(
    instance_id: str, payload: CommentRequest, _: None = Depends(require_token)
) -> dict:
    """写入审查意见。

    幂等语义（决议 D6 ＋ 幂等键的通用契约）：

    - 同键 + 同审批单 + 同内容 → 返回**第一次**的结果，`replayed=true`；
    - 同键 + 内容或审批单不同   → **409**，既不写入，也不返回别人的结果。

    第二种情形是对初版的修正：把"同键但内容不同"当成重放，
    会让调用方拿到别人的评论却以为是自己写的，比重复写入更危险。
    """
    raise_if_faulted("write_comment", instance_id)

    if store.get(instance_id) is None:
        raise HTTPException(404, detail=f"审批单 {instance_id} 不存在")

    result, outcome = store.write_comment(
        instance_id=instance_id,
        content=payload.content,
        idempotency_key=payload.idempotency_key,
        operator_name=payload.operator_name,
    )

    if outcome is WriteOutcome.CONFLICT:
        raise HTTPException(
            status_code=409,
            detail=(
                "幂等键冲突：该 idempotency_key 已用于另一个请求（审批单或内容不同）。"
                "为避免把他人的意见误认为已写入，本次请求被拒绝；"
                "请为新请求生成新的幂等键。"
            ),
        )

    return result  # type: ignore[return-value]


@app.get(
    "/api/instances/{instance_id}/comments",
    tags=["评论"],
    summary="查看已回写的评论（演示与验收用）",
)
def list_comments(instance_id: str, _: None = Depends(require_token)) -> dict:
    return {
        "instance_id": instance_id,
        "count": store.comment_count(instance_id),
        "items": store.list_comments(instance_id),
    }


# ============================================================
# 故障注入
# ============================================================


@app.get("/api/faults", tags=["故障注入"], summary="查看当前已注册的故障")
def list_faults(_: None = Depends(require_token)) -> dict:
    items = faults.list_all()
    return {"count": len(items), "items": [fault.to_dict() for fault in items]}


@app.post("/api/faults", tags=["故障注入"], summary="注入故障")
def add_fault(payload: FaultRequest, _: None = Depends(require_token)) -> dict:
    try:
        fault = faults.set(Fault(**payload.model_dump()))
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return fault.to_dict()


@app.delete("/api/faults", tags=["故障注入"], summary="清除故障（不传 target 则清空全部）")
def clear_faults(target: str | None = None, _: None = Depends(require_token)) -> dict:
    return {"cleared": faults.clear(target)}
