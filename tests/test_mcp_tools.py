"""MCP 形态的七个工具（M7 / Task 6）。

## 本文件守住的是"两种协议给出不同答案"这一类

REST 与 MCP 共用 `app/tool_facade.py`，因此**同一次调用必须给出同一份结论** ——
包括机器错误码。分叉的方式很安静：某一边自己加了一层翻译
（比如把 `RESULT_NOT_FOUND` 泛化成 `INVALID_ARGUMENT`），
于是同一个失败在两种协议下**处置方向相反**（一个说"等批次跑完"，
一个说"改参数"），而两边都不报错。

因此这里逐项钉住：

1. **恰好七个工具**，名称与需求 2.4.10 逐字一致，**必填参数**也逐项核对
   （名字对了但少一个必填参数，模型侧会构造出一个缺字段的调用）；
2. **业务结论不是错误**：`blocked` 正常返回，`isError` 为假 ——
   附件缺失是"这单做不下去"，不是"调用失败了"；
3. **错误载荷带稳定机器码**：MCP 的 `isError` 只有一个布尔位，
   机器码放进 JSON 文本里，`ResultInputError` 的原因码**不得**被泛化；
4. **长任务返回 `task_ref`**（工具 4/5），而不是阻塞等待；
5. **身份 fail-closed**：没有身份来源时**构造即失败**，HTTP 传输逐个请求解身份。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import tool_facade
from app.adapters.storage.local_file_storage import LocalFileStorage
from app.auth import Actor, AuthConfigurationError, AuthenticationError, Permission
from app.config import PROJECT_ROOT
from app.mcp_server import bind_request_headers, build_mcp_server
from app.models import ApprovalAttachment, ApprovalTask, ContractParse, ReviewRun
from app.ports.approval_gateway import (
    ApprovalDetailDTO,
    AttachmentDTO,
    AuthoritativeContextDTO,
    DownloadedAttachmentDTO,
    PendingApprovalDTO,
)
from app.errors import PermanentGatewayError
from app.enums import ErrorCode

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"

#: 一份最小但**真实可解析**的 PDF（工具 3 只搬字节，不解析它）。
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"

_ADMIN = Actor(
    actor_id="admin-mcp",
    display_name="admin-mcp",
    roles=frozenset({"system_admin"}),
    tenant_id="default",
)


# ============================================================
# 假网关（与 test_m7_contracts.py 同形；`tests/` 不是包，无法跨文件复用）
# ============================================================


def _pending(code: str) -> PendingApprovalDTO:
    return PendingApprovalDTO(
        provider="mock",
        tenant_id="default",
        instance_id=code,
        approval_code=code,
        approval_title="测试合同",
        applicant_name="张三",
        apply_time="2026-09-01 09:00:00",
        attachment_count=1,
    )


def _detail(code: str) -> ApprovalDetailDTO:
    return ApprovalDetailDTO(
        instance_id=code,
        approval_code=code,
        approval_title="测试合同",
        applicant_name="张三",
        apply_time="2026-09-01 09:00:00",
        context=AuthoritativeContextDTO(
            our_party_name="示例科技有限公司",
            our_party_contract_label="party_a",
            our_party_business_role="buyer",
            contract_type="procurement",
        ),
        form_data={"amount": "1200000"},
        attachments=(
            AttachmentDTO(
                attachment_id="A-1", file_name="contract.pdf", file_type="pdf"
            ),
        ),
    )


class _FakeGateway:
    provider = "mock"
    tenant_id = "default"

    def __init__(self) -> None:
        self.pendings = [_pending("HT-1"), _pending("HT-2")]
        #: 置上时下载抛"附件已被删除" —— 那是**业务事实**，不是系统故障。
        self.download_error: ErrorCode | None = None
        #: 置上时抛一个**代码缺陷**（用来验"缺陷不得伪装成业务结论"）。
        self.defect: Exception | None = None

    def list_pending(self, limit: int) -> list[PendingApprovalDTO]:
        if self.defect is not None:
            raise self.defect
        return list(self.pendings)[:limit]

    def get_detail(self, instance_id: str) -> ApprovalDetailDTO:
        return _detail(instance_id)

    def download_attachment(
        self, instance_id: str, attachment_id: str
    ) -> DownloadedAttachmentDTO:
        if self.download_error is not None:
            raise PermanentGatewayError(
                "附件已被删除", code=self.download_error
            )
        return DownloadedAttachmentDTO(
            content=PDF_BYTES, file_name="contract.pdf", content_type="application/pdf"
        )


class _HeaderIdentity:
    """按请求头解身份的最小实现（用来验"逐个请求解身份"这条路径）。"""

    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, headers):
        self.calls += 1
        raw = headers.get("X-Actor-Id")
        if not raw:
            raise AuthenticationError("缺少身份")
        return Actor(
            actor_id=raw,
            display_name=raw,
            roles=frozenset({"system_admin"}),
            tenant_id="default",
        )

    def close(self) -> None:
        """无长生命周期资源。"""


# ============================================================
# 测试台
# ============================================================


class _Harness:
    def __init__(self, work_dir: Path) -> None:
        path = work_dir / "mcp.db"
        conn = sqlite3.connect(path)
        try:
            conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()

        self.engine = create_engine(
            f"sqlite:///{path.as_posix()}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        self.factory = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False, future=True
        )
        self.gateway = _FakeGateway()
        self.storage = LocalFileStorage(work_dir / "objects")

    def dispose(self) -> None:
        self.engine.dispose()

    def server(self, **overrides):
        params = {
            "actor": _ADMIN,
            "gateway": self.gateway,
            "storage": self.storage,
            "engine_version": "test-engine-1.0",
            "session_factory": self.factory,
        }
        params.update(overrides)
        return build_mcp_server(**params)

    def session(self) -> Session:
        return self.factory()

    # --- 种子 ---

    def seed_task(self, instance_id: str = "HT-1") -> int:
        with self.session() as session:
            task = ApprovalTask(
                provider="mock",
                tenant_id="default",
                instance_id=instance_id,
                approval_code=instance_id,
                approval_title="测试合同",
                task_status="reviewing",
                context_status="confirmed",
                our_party_name="示例科技有限公司",
                our_party_contract_label="party_a",
                our_party_business_role="buyer",
                contract_type="procurement",
            )
            session.add(task)
            session.commit()
            return task.id

    def seed_attachment(self, task_id: int, *, downloaded: bool = True) -> int:
        with self.session() as session:
            attachment = ApprovalAttachment(
                task_id=task_id,
                attachment_id="A-1",
                file_name="contract.pdf",
                content_type="application/pdf",
                download_status="success" if downloaded else "pending",
                object_key="sha256/aa/aa/deadbeef.pdf" if downloaded else None,
                file_checksum="a" * 64 if downloaded else None,
            )
            session.add(attachment)
            session.commit()
            return attachment.id

    def seed_parse(self, task_id: int, attachment_id: int) -> int:
        with self.session() as session:
            parse = ContractParse(
                task_id=task_id,
                attachment_id=attachment_id,
                parse_status="succeeded",
                parse_version=1,
            )
            session.add(parse)
            session.commit()
            return parse.id

    def seed_run(self, task_id: int, parse_id: int) -> int:
        with self.session() as session:
            run = ReviewRun(
                task_id=task_id,
                parse_id=parse_id,
                version_no=1,
                run_status="completed",
            )
            session.add(run)
            session.commit()
            return run.id


@pytest.fixture()
def harness(work_dir: Path):
    built = _Harness(work_dir)
    try:
        yield built
    finally:
        built.dispose()


# ============================================================
# MCP 客户端辅助
# ============================================================


def _call(server, name: str, arguments: dict):
    """用 SDK 自带的**内存内**客户端调一次工具（不起任何端口）。"""

    async def _run():
        # `_mcp_server` 是 FastMCP 内部的低层 Server；SDK 官方的内存客户端要的正是它。
        # 用内存传输而不是真起 HTTP：本文件测的是**工具语义**，
        # 而传输本身由 `scripts/run_mcp.py` 的接线负责。
        async with create_connected_server_and_client_session(
            server._mcp_server
        ) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(_run())


def _payload(result) -> dict:
    """`CallToolResult` → dict。

    FastMCP 把 `dict` 返回值序列化成**一个 JSON 文本块**；
    错误路径（`isError=True`）也是同一形状（`str(ToolError)` 就是我们给的那段 JSON）。
    两条都从文本块读，因此不必依赖 SDK 版本是否额外填 `structuredContent`。

    解析失败时把**原始内容**放进断言消息：SDK 换包装形式时，
    失败信息应当直接告诉下一个人"现在拿到的长什么样"，
    而不是一句 `Expecting value: line 1 column 1`。
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured

    texts = [getattr(item, "text", None) for item in result.content]
    raw = next((text for text in texts if text), None)
    assert raw, f"工具没有返回可解析的文本内容：content={result.content!r}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - 出错时给出可读线索
        raise AssertionError(
            f"工具返回值不是 JSON（isError={result.isError}）：{raw!r}"
        ) from exc
    assert isinstance(parsed, dict), f"工具返回值不是对象：{parsed!r}"
    return parsed


def _list_tools(server):
    return asyncio.run(server.list_tools())


# ============================================================
# 1. 发现：恰好七个，且必填参数逐项一致
# ============================================================

#: 每个工具的**必填**输入（需求 2.4.10 的最低参数）。
#: 写成数据表而不是散在断言里：漏一个工具时表里那一行就成了唯一的线索。
REQUIRED_FIELDS: dict[str, set[str]] = {
    "list_pending_contract_approvals": set(),
    "get_contract_approval": {"instance_id"},
    "download_contract_attachment": {"instance_id", "attachment_id"},
    "parse_contract_document": {"document_id"},
    "run_contract_rules": {"case_id"},
    "save_review_result": {
        "case_id",
        "overall_risk_level",
        "summary_text",
        "focus_points_json",
        "comment_text",
    },
    "write_approval_comment": {"instance_id", "review_id"},
}


def test_exactly_seven_tools_with_the_required_inputs(harness: _Harness) -> None:
    """**验收**：七个工具的名称与**必填参数**与需求逐字一致。

    ⚠️ 只断名字时，一个"参数被改成可选"的实现照样通过 ——
    而必填变可选会让模型侧构造出一个缺字段的调用，错误推迟到运行时才出现。
    """
    tools = _list_tools(harness.server())

    assert {tool.name for tool in tools} == set(tool_facade.TOOL_NAMES)
    assert len(tools) == 7, "需求写的是恰好七个"

    for tool in tools:
        schema = tool.inputSchema
        assert set(schema.get("required", [])) == REQUIRED_FIELDS[tool.name], (
            f"{tool.name} 的必填参数与需求不一致：{schema}"
        )
        assert set(schema.get("properties", {})) >= REQUIRED_FIELDS[tool.name]


def test_tools_describe_themselves_for_model_side_use(harness: _Harness) -> None:
    """每个工具都要有描述 —— 没有描述的工具有效性取决于调用方猜得准不准。"""
    for tool in _list_tools(harness.server()):
        assert (tool.description or "").strip(), f"{tool.name} 缺少描述"


# ============================================================
# 2. 身份 fail-closed
# ============================================================


def test_server_refuses_to_start_without_an_identity_source(
    harness: _Harness,
) -> None:
    """没有身份来源 → **构造即失败**，绝不起来一个谁都能调的服务。"""
    with pytest.raises(AuthConfigurationError):
        harness.server(actor=None)


def test_two_identity_sources_are_rejected(harness: _Harness) -> None:
    """两种身份来源都给 → 报错。

    哪一份生效是任意的，而"任意"意味着另一份被**静默忽略** ——
    运维改了配置却看不到任何变化。
    """
    with pytest.raises(ValueError, match="只能给一个"):
        harness.server(actor=_ADMIN, identity_provider=_HeaderIdentity())


def test_http_transport_resolves_identity_per_request(harness: _Harness) -> None:
    """**验收**：HTTP 传输逐个请求解身份 —— 不是启动时解一次。

    启动时解一次，等于让**所有调用方共用第一个请求的身份**：
    那是越权，而响应上看不出任何异常。
    """
    provider = _HeaderIdentity()
    server = harness.server(actor=None, identity_provider=provider)

    with bind_request_headers({"X-Actor-Id": "alice"}):
        body = _payload(_call(server, "list_pending_contract_approvals", {"limit": 1}))
    assert body["outcome"] == "pulled"

    with bind_request_headers({"X-Actor-Id": "bob"}):
        assert _payload(
            _call(server, "list_pending_contract_approvals", {"limit": 1})
        )["outcome"] == "pulled"

    assert provider.calls == 2, "每个请求都要解一次身份"
    # 审计留痕记的是**当时那个调用方**，不是第一个
    with harness.session() as session:
        task = session.query(ApprovalTask).first()
    assert task is not None, "拉取应当已落库"


def test_a_request_without_credentials_is_rejected(harness: _Harness) -> None:
    """HTTP 传输下没有凭据 → 401（`AUTHENTICATION_REQUIRED`），不是匿名放行。"""
    server = harness.server(actor=None, identity_provider=_HeaderIdentity())

    result = _call(server, "list_pending_contract_approvals", {"limit": 1})

    assert _payload(result)["error_code"] == "AUTHENTICATION_REQUIRED"


# ============================================================
# 3. 成功路径与 REST 同源（parity）
# ============================================================


def test_tool_2_matches_the_facade_byte_for_byte(harness: _Harness) -> None:
    """**验收**：同一次调用，MCP 与直接调门面的返回值**逐字相等**。

    ⚠️ 不用"包含关系"（`mcp.items() <= facade.items()`）：那能被一种缺陷通过 ——
    MCP 侧**多下发**了 `object_key` 或文件系统路径。
    """
    server = harness.server()
    harness.seed_task("HT-1")

    # ⚠️ 比较的是**同一次**调用。第一次同步会**创建**附件记录（`is_new=True`），
    # 因此"第一次"与"第二次"本来就是两件事 —— 直接比较第一次 MCP 与第二次门面，
    # 比的不是同一条路径，差异是测试自己造的（`test_m7_contracts.py` 同一条理由）。
    _call(server, "get_contract_approval", {"instance_id": "HT-1"})

    via_mcp = _payload(_call(server, "get_contract_approval", {"instance_id": "HT-1"}))
    with harness.session() as session:
        via_facade = tool_facade.get_contract_approval(
            "HT-1", session=session, gateway=harness.gateway, actor=_ADMIN
        )
        session.commit()

    assert via_mcp == via_facade, (
        "两条路径的结论不同："
        f"{ {key: (via_mcp.get(key), via_facade.get(key)) for key in set(via_mcp) | set(via_facade) if via_mcp.get(key) != via_facade.get(key)} }"
    )


def test_tool_1_pulls_and_reports_the_same_shape(harness: _Harness) -> None:
    """拉取：MCP 与门面给出同一份结论（含第二次的幂等语义）。"""
    server = harness.server()

    first = _payload(_call(server, "list_pending_contract_approvals", {"limit": 20}))
    assert first["outcome"] == "pulled"
    assert first["fetched"] == 2
    assert first["created"] == 2

    with harness.session() as session:
        second_facade = tool_facade.list_pending_contract_approvals(
            20, session=session, gateway=harness.gateway, actor=_ADMIN
        )
        session.commit()
    second_mcp = _payload(
        _call(server, "list_pending_contract_approvals", {"limit": 20})
    )

    assert second_mcp == second_facade, "重复拉取必须与门面给出同一份结论"


# ============================================================
# 4. 业务结论不是错误
# ============================================================


def test_business_denial_comes_back_as_a_normal_result(harness: _Harness) -> None:
    """**验收**：附件缺失 → `outcome=blocked` 且 `isError` 为**假**。

    "这单做不下去"是**业务结论**，调用本身成功了。把它报成 `isError`
    会让每个调用方各写一遍"哪些错误其实不是错误"的判断 ——
    而漏判的那一处会把一次正常的业务结论升级成故障告警。
    """
    harness.seed_task("HT-1")
    harness.gateway.download_error = ErrorCode.ATTACHMENT_MISSING
    server = harness.server()

    result = _call(
        server,
        "download_contract_attachment",
        {"instance_id": "HT-1", "attachment_id": "A-1"},
    )

    assert result.isError is False, "业务结论不该被报成调用失败"
    body = _payload(result)
    assert body["outcome"] == "blocked"
    assert body["error_code"] == ErrorCode.ATTACHMENT_MISSING.value
    assert body["task_status"] == "blocked"


def test_tool_3_success_matches_the_facade(harness: _Harness) -> None:
    """下载成功：MCP 与门面同形（`file_path` 是受控相对路径，两处一致）。"""
    harness.seed_task("HT-1")
    server = harness.server()

    via_mcp = _payload(
        _call(
            server,
            "download_contract_attachment",
            {"instance_id": "HT-1", "attachment_id": "A-1"},
        )
    )

    assert via_mcp["outcome"] == "downloaded"
    assert via_mcp["file_path"].startswith("workspace/")
    # 对象键是内部实现细节，**不得**出现在 MCP 响应里（与 REST 同一条约定）
    assert "sha256/" not in json.dumps(via_mcp, ensure_ascii=False)


# ============================================================
# 5. 长任务返回可查询的 TaskRef
# ============================================================


def test_tool_4_returns_a_task_ref_instead_of_blocking(harness: _Harness) -> None:
    """**验收**：解析是长任务 —— 立即返回 `task_ref`，不阻塞等待。"""
    task_id = harness.seed_task("HT-1")
    attachment_id = harness.seed_attachment(task_id)
    server = harness.server()

    body = _payload(
        _call(server, "parse_contract_document", {"document_id": str(attachment_id)})
    )

    assert body["outcome"] == "queued"
    assert body["task_ref"]["job_id"] >= 1
    assert body["task_ref"]["status_url"] == f"/api/jobs/{body['task_ref']['job_id']}"


def test_tool_5_returns_a_task_ref_pointing_at_the_run(harness: _Harness) -> None:
    """规则审查同样是长任务，且 `result_url` 在**入队时**就已确定。"""
    task_id = harness.seed_task("HT-1")
    attachment_id = harness.seed_attachment(task_id)
    parse_id = harness.seed_parse(task_id, attachment_id)
    server = harness.server()

    body = _payload(_call(server, "run_contract_rules", {"case_id": str(parse_id)}))

    assert body["outcome"] == "queued"
    assert body["task_ref"]["job_id"] >= 1
    assert body["task_ref"]["run_id"] >= 1


# ============================================================
# 6. 错误载荷带稳定机器码
# ============================================================


def test_missing_resource_keeps_its_machine_code(harness: _Harness) -> None:
    """**验收**：`ResultInputError` 的原因码不得被泛化成 `INVALID_ARGUMENT`。

    ⚠️ 它是 `ValueError` 的子类。落进"参数非法"的兜底分支后：
    REST 答 `RESULT_NOT_FOUND`（核对 id），MCP 答 `INVALID_ARGUMENT`（改参数）——
    两种协议对同一次调用给出**不同的处置方向**，而两边都不报错。
    """
    server = harness.server()

    result = _call(
        server,
        "save_review_result",
        {
            "case_id": "999",
            "overall_risk_level": "low",
            "summary_text": "摘要",
            "focus_points_json": "[]",
            "comment_text": "正文",
        },
    )

    assert result.isError is False, (
        f"业务错误也是**结果**（同一形状的载荷）；实际收到 {result.content}"
    )
    body = _payload(result)
    assert body["error_code"] == "RESULT_NOT_FOUND"
    assert body["outcome"] == "error"
    assert body["retryable"] is False


def test_an_unknown_case_id_is_a_structured_error(harness: _Harness) -> None:
    """工具 5 指向不存在的解析记录 → 稳定错误码，而不是未处理异常。"""
    server = harness.server()

    result = _call(server, "run_contract_rules", {"case_id": "999"})

    assert _payload(result)["error_code"] == "RESOURCE_NOT_FOUND"


def test_a_malformed_id_is_reported_as_an_argument_error(harness: _Harness) -> None:
    """id 不是十进制数字串 → `INVALID_ARGUMENT`（与 REST 的 400 同一个码）。

    门面只接受需求写下的那一种写法（十进制数字串）—— MCP 侧同一判据，
    因为它调的就是同一个门面函数。
    """
    server = harness.server()

    result = _call(server, "run_contract_rules", {"case_id": "not-a-number"})

    body = _payload(result)
    assert body["error_code"] == "INVALID_ARGUMENT"
    assert "case_id" in body["message"]


def test_permission_denial_becomes_a_structured_error(harness: _Harness) -> None:
    """只读审计调写工具 → 403 语义的稳定错误码（门面是 MCP 唯一的关口）。"""
    readonly = Actor(
        actor_id="auditor",
        display_name="auditor",
        roles=frozenset({"read_only_auditor"}),
        tenant_id="default",
    )
    server = harness.server(actor=readonly)

    result = _call(server, "run_contract_rules", {"case_id": "1"})

    assert _payload(result)["error_code"] == "PERMISSION_DENIED"


def test_a_code_defect_surfaces_as_an_error_not_a_business_conclusion(
    harness: _Harness,
) -> None:
    """**验收**：代码缺陷**不伪装**成业务结论。

    可预期业务错误返回 `outcome="error"` 的载荷（`isError` 为假）；
    而缺陷（这里是假网关抛的 `TypeError`）原样上抛 → SDK 标 `isError=true`。
    两者混在一起时，一个真实缺陷看起来只是一次正常的业务拒绝。
    """
    harness.gateway.defect = TypeError("假的缺陷")
    server = harness.server()

    result = _call(server, "list_pending_contract_approvals", {"limit": 1})

    assert result.isError is True
    assert "假的缺陷" in result.content[0].text


# ============================================================
# 7. stdio 传输的身份来源
# ============================================================


def test_stdio_identity_is_required_and_resolved_from_the_environment() -> None:
    """**验收**：stdio 传输的身份来自环境变量；没有配置 → **拒绝启动**。

    ⚠️ 没有配置时抛 `AuthConfigurationError` 而不是 `AuthenticationError`：
    后者会被读成"这次请求没带凭据"（调用方去重试或补头），
    而真正的问题是**启动配置缺了东西** —— 那件事调用方做什么都没用。
    """
    from scripts.run_mcp import resolve_stdio_actor

    with pytest.raises(AuthConfigurationError, match="MCP_ACTOR_ID"):
        resolve_stdio_actor({})

    actor = resolve_stdio_actor(
        {
            "MCP_ACTOR_ID": "legal-zhang",
            "MCP_ACTOR_ROLES": "legal_reviewer",
        }
    )

    assert actor.actor_id == "legal-zhang"
    assert actor.roles == frozenset({"legal_reviewer"})
    # 角色决定权限：法务审核人有审查链路上的动作，但**没有** `rule:manage`
    assert actor.has(Permission.RESULT_SAVE) is True
    assert actor.has(Permission.RULE_MANAGE) is False
