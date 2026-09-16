"""七个工具的规范门面契约（M7 / Task 2）。

## 本文件守住的是"门面这一层"特有的那几件事

七个工具各自的业务行为已由 `test_api_tools.py` / `test_parse_api.py` /
`test_m6_api.py` 覆盖。这里只测**门面作为契约层**才有的属性：

1. **签名逐字**（需求 6.5）—— 参数名、顺序、有无默认值。
   名字一改，按需求写代码的调用方（含 MCP 客户端）就在运行时才失败，
   而 REST 侧的 Pydantic 会把它默默挡在 422 上，看起来只是"参数写错了"。
2. **企业上下文只能是关键字专属** —— `session` / `actor` / `gateway` 这些
   若占了一个位置参数，需求的签名就**静默地**多出几位，
   而 `save_review_result("1", "low", ...)` 这类按需求写的调用会**错位**：
   传进去的字符串落进 `summary_text`，接口照样 200。
3. **权限在两种协议**同**一个关口判定** —— REST 有 `Depends`，MCP 没有。
   把判定只写在 REST 侧，MCP 形态就完全没有门（而它不会报错）。
   这里用"一碰就炸"的替身参数证明：**判定发生在任何副作用之前**。
4. **REST 与门面同源** —— 同一份输入走两条路，响应体**逐字相等**。
   端点若自己"顺手补一个字段"，两份协议就开始分叉，
   而分叉的一方永远看起来是正常的。

## 为什么"逐字相等"而不是"包含关系"

包含关系（`assert rest.items() <= facade.items()`）能通过的情形里，
有一种正是缺陷：端点**多下发**了 `object_key` 或文件系统路径。
那类泄漏在包含关系下是一个"额外字段"，在相等关系下是一次失败。
"""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import tool_facade
from app.adapters.storage.local_file_storage import LocalFileStorage
from app.api.deps import (
    get_actor,
    get_db,
    get_gateway,
    get_parser_engine_version,
    get_storage,
)
from app.auth import Actor, AuthorizationError, Permission, Role, require
from app.config import PROJECT_ROOT, settings
from app.db import transactional_session
from app.main import app
from app.models import ApprovalAttachment, ApprovalTask, ContractParse, ReviewRun
from app.ports.approval_gateway import (
    ApprovalDetailDTO,
    AttachmentDTO,
    AuthoritativeContextDTO,
    DownloadedAttachmentDTO,
    PendingApprovalDTO,
)
from app.services.result_service import confirm_result

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"
SEED_FILE = PROJECT_ROOT / "db" / "seed.sql"

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF"

#: 需求 6.5 写下的七个签名。`True` 表示该参数在需求里**有默认值**。
#:
#: 写成数据而不是散在断言里，是为了让"需求改了一个字"表现为**一处**修改，
#: 而不是去找哪几条 `assert` 需要跟着动。
REQUIRED_SIGNATURE: Mapping[str, tuple[tuple[str, bool], ...]] = {
    "list_pending_contract_approvals": (("limit", True),),
    "get_contract_approval": (("instance_id", False),),
    "download_contract_attachment": (
        ("instance_id", False),
        ("attachment_id", False),
        ("file_name", True),
    ),
    "parse_contract_document": (("document_id", False),),
    "run_contract_rules": (("case_id", False),),
    "save_review_result": (
        ("case_id", False),
        ("overall_risk_level", False),
        ("summary_text", False),
        ("focus_points_json", False),
        ("comment_text", False),
    ),
    "write_approval_comment": (
        ("instance_id", False),
        ("review_id", False),
    ),
}

#: 按需求签名构造的**合法**位置参数，供权限与转换用例使用。
_VALID_POSITIONALS: Mapping[str, tuple[Any, ...]] = {
    "list_pending_contract_approvals": (),
    "get_contract_approval": ("HT-1",),
    "download_contract_attachment": ("HT-1", "A-1"),
    "parse_contract_document": ("1",),
    "run_contract_rules": ("1",),
    "save_review_result": ("1", "low", "摘要", '["关注点一"]', "回写正文"),
    "write_approval_comment": ("HT-1", "1"),
}

#: 零权限主体：任何工具都应被它拒掉。
#: 刻意**不用** `read_only_auditor` —— 那个角色带着 `task:read`，
#: 用它测"全部被拒"会得到三条通过，于是这条用例既没测到"全拒"、
#: 也掩盖了"哪几个工具其实是读工具"这个事实。
_NO_ROLE_ACTOR = Actor(
    actor_id="nobody",
    display_name="nobody",
    roles=frozenset(),
    tenant_id="default",
)

#: 只读审计：**能读不能写**。用来证明门面的权限矩阵区分了这两类工具。
_AUDITOR_ACTOR = Actor(
    actor_id="auditor",
    display_name="auditor",
    roles=frozenset({Role.READ_ONLY_AUDITOR.value}),
    tenant_id="default",
)

#: 全权限主体。业务路径的用例用它，让身份层在这些用例里"透明"。
_FULL_ACCESS_ACTOR = Actor(
    actor_id="test-actor",
    display_name="test-actor",
    roles=frozenset({Role.SYSTEM_ADMIN.value}),
    tenant_id="default",
)


@contextmanager
def _same_boundary_as_get_db(session: Session) -> Iterator[Session]:
    """把请求级事务边界包成 `with` —— 与 `app/db.py::get_db` **逐字同一份实现**。

    测试里另写一份事务语义时，两边一旦分叉，测出来的行为与生产不一致，
    而这种不一致表现为"门面少写了几行证据"，不会自己报错。
    """
    yield from transactional_session(session)


class _Exploding:
    """一碰就炸的替身：证明"权限判定发生在任何副作用之前"。

    如果哪天 `_require` 被挪到某个函数体的中间（比如换算 id 之后），
    这个替身会在**那一行**抛 `AssertionError` 而不是静静地通过。
    用 `None` 做不到这件事：`None` 被传给服务层时可能很久以后才炸，
    也可能被某个 `or` 兜住，于是"顺序错了"没有任何症状。
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"权限判定之前就触碰了依赖（.{name}）—— 拒绝路径上不该产生任何副作用"
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("权限判定之前就调用了依赖 —— 拒绝路径上不该产生任何副作用")


# ============================================================
# 1. 签名契约（需求 6.5 逐字）
# ============================================================


def test_facade_exposes_exactly_the_seven_requirement_names() -> None:
    """恰好七个，名字逐字不变。多一个少一个都不算交付。"""
    assert tool_facade.TOOL_NAMES == tuple(REQUIRED_SIGNATURE)


@pytest.mark.parametrize("tool", sorted(REQUIRED_SIGNATURE))
def test_positional_parameters_match_the_requirement_verbatim(tool: str) -> None:
    """位置参数：名称、**顺序**、有无默认值三项逐字一致。

    ⚠️ 顺序也要断言。只看名字集合时，把 `download_contract_attachment` 的
    `instance_id` 与 `attachment_id` 对调仍然通过 —— 而那两个都是字符串，
    调用方传反了会拿到"附件不存在"，排查方向直接跑偏。
    """
    signature = inspect.signature(getattr(tool_facade, tool))

    actual: list[tuple[str, bool]] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            continue
        assert parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, (
            f"{tool}.{name} 既不是位置参数也不是关键字专属参数（{parameter.kind}）——"
            "需求的签名只有这两种形态"
        )
        actual.append((name, parameter.default is not inspect.Parameter.empty))

    assert actual == list(REQUIRED_SIGNATURE[tool])


@pytest.mark.parametrize("tool", sorted(REQUIRED_SIGNATURE))
def test_enterprise_context_is_keyword_only(tool: str) -> None:
    """企业上下文（`session` / `actor` / …）**必须**是关键字专属。

    它们若占位置，需求的签名就静默多出几位；而按需求写的调用会**错位**，
    且错位后常常仍然能跑（都是字符串时），只是把 id 塞进了 `summary_text`。
    """
    signature = inspect.signature(getattr(tool_facade, tool))
    requirement_names = {name for name, _ in REQUIRED_SIGNATURE[tool]}

    extra = set(signature.parameters) - requirement_names
    assert extra, f"{tool} 没有任何企业上下文参数？门面至少要接 session 与 actor"

    for name in extra:
        assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{tool}.{name} 是位置参数，但它不在需求 6.5 的签名里 —— "
            "按需求写的调用会在这一位错位"
        )


def test_required_permissions_covers_exactly_the_seven_tools() -> None:
    """权限表与工具表**同源**。

    漏一条时 `REQUIRED_PERMISSIONS[tool]` 会 `KeyError` —— 在门面里表现为 500。
    但那已经不是"少了一道门"，而是"这个工具根本调不通"；
    真正的危险是**多**一条没人用的映射，它让"七个工具都要鉴权"看起来成立。
    """
    assert set(tool_facade.REQUIRED_PERMISSIONS) == set(tool_facade.TOOL_NAMES)
    assert all(
        isinstance(permission, Permission)
        for permission in tool_facade.REQUIRED_PERMISSIONS.values()
    )


# ============================================================
# 2. 门面是两种协议共同的关口
# ============================================================


@pytest.mark.parametrize("tool", sorted(REQUIRED_SIGNATURE))
def test_single_gate_rejects_before_any_side_effect(tool: str) -> None:
    """零权限主体在**任何副作用之前**被拒。

    ⚠️ 关键字专属参数全部塞"一碰就炸"的替身：只要判定被挪到换算 id 之后、
    或某个函数在 `_require` 之前碰了 `session`，这条用例立刻变红。
    """
    signature = inspect.signature(getattr(tool_facade, tool))
    keyword_only = {
        name: _Exploding()
        for name, parameter in signature.parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        and name != "actor"
    }

    with pytest.raises(AuthorizationError):
        getattr(tool_facade, tool)(
            *_VALID_POSITIONALS[tool], actor=_NO_ROLE_ACTOR, **keyword_only
        )


def test_read_only_auditor_is_admitted_to_read_tools_and_denied_the_mutating_ones() -> None:
    """只读角色的边界恰好是"读 / 写"，不是"全部拒绝"。

    只断言"全被拒"时，一个把 `task:read` 也拿掉的实现照样通过 ——
    而那种实现会让只读审计**什么都看不到**，角色名不副实。
    """
    read_tools = {
        "list_pending_contract_approvals",
        "get_contract_approval",
        "download_contract_attachment",
    }
    mutating_tools = set(REQUIRED_SIGNATURE) - read_tools

    for tool in sorted(read_tools):
        require(_AUDITOR_ACTOR, tool_facade.REQUIRED_PERMISSIONS[tool])

    for tool in sorted(mutating_tools):
        with pytest.raises(AuthorizationError):
            require(_AUDITOR_ACTOR, tool_facade.REQUIRED_PERMISSIONS[tool])


def test_unknown_role_gets_a_useful_403_instead_of_a_500() -> None:
    """拼错的角色名 → 403 且**把角色原话带出来**，不是 500。

    只写"拒绝"时，一个角色名拼错的人会反复确认自己"明明有那个角色" ——
    线索（本系统不认识这个名字）在我们手里却没告诉他。
    """
    actor = Actor(
        actor_id="typo",
        display_name="typo",
        roles=frozenset({"legal-reviewer"}),  # 少了中间的下划线
        tenant_id="default",
    )

    with pytest.raises(AuthorizationError) as excinfo:
        getattr(tool_facade, "save_review_result")(
            *_VALID_POSITIONALS["save_review_result"],
            session=_Exploding(),
            actor=actor,
        )

    message = str(excinfo.value)
    assert "legal-reviewer" in message, "被丢弃的角色原话必须出现在拒绝原因里"
    assert "未被识别" in message


# ============================================================
# 3. 需求形态 → 本系统形态（唯一的转换点）
# ============================================================


@pytest.mark.parametrize(
    "value",
    [
        12,  # 已经是 int：本系统的主键形态，不是需求形态
        " 12",  # 前导空白：某些解析器会接受，于是"该传哪种"没有正确答案
        "12\n",
        "12.0",  # 浮点写法
        "+12",
        "0x0c",
        "１２",  # 全角数字：int() 能解析，正则不能 —— 两种写法必须一致地拒
        "1_2",
        "",
        "0",  # 主键从 1 开始
        "-1",
        "abc",
    ],
)
def test_legacy_id_accepts_only_the_decimal_string_from_the_requirement(
    value: Any,
) -> None:
    """只认十进制数字串。别的写法一律 `ValueError`（→ 400）。

    ⚠️ 用 `run_contract_rules` 走真实路径而不是直接调 `_legacy_id`：
    转换点是不是**真的**在门面入口上，只有从工具签名进去才测得出来。
    """
    with pytest.raises(ValueError) as excinfo:
        tool_facade.run_contract_rules(
            value, session=_Exploding(), actor=_FULL_ACCESS_ACTOR
        )

    assert "case_id" in str(excinfo.value)


def test_legacy_id_rejects_an_int_with_a_message_naming_the_canonical_target() -> None:
    """传 `int` 是**调用方**的错误，且消息要说清它指向本系统的什么。

    直接 `re.fullmatch(12)` 会抛 `TypeError` → 500，于是"id 类型写错了"
    看起来像"服务端崩了"。
    """
    with pytest.raises(ValueError) as excinfo:
        tool_facade.run_contract_rules(
            12, session=_Exploding(), actor=_FULL_ACCESS_ACTOR
        )

    message = str(excinfo.value)
    assert "contract_parses.id" in message, "必须说清这个 id 在本系统里指的是什么"


@pytest.mark.parametrize(
    "value",
    [
        '{"a": 1}',  # 对象
        "[1, 2]",  # 数字数组
        '["ok", {"a": 1}]',  # 混合
        '"关注点"',  # 裸字符串
        "不是 JSON",
        '["未闭合"',
    ],
)
def test_focus_points_json_must_be_a_json_string_array(value: str) -> None:
    """关注点只接受**字符串数组**的 JSON。

    允许对象或数字数组会让关注点在下游被渲染成 `[object Object]` ——
    而那看起来像模型输出的乱码，排查方向会指向模型而不是这里。
    """
    with pytest.raises(ValueError):
        tool_facade.save_review_result(
            "1",
            "low",
            "摘要",
            value,
            "正文",
            session=_Exploding(),
            actor=_FULL_ACCESS_ACTOR,
        )


def test_focus_points_accepts_the_documented_shape() -> None:
    """对照组：合法输入**通过转换**（在真的碰到 session 之前）。

    ⚠️ 没有这条时，"一律抛 ValueError"的缺陷会让上面那组用例全绿。
    """
    with pytest.raises(AssertionError, match="副作用"):
        tool_facade.save_review_result(
            "1",
            "low",
            "摘要",
            '["付款条件须与验收挂钩"]',
            "正文",
            session=_Exploding(),
            actor=_FULL_ACCESS_ACTOR,
        )


# ============================================================
# 4. 源码守卫：门面不得反向依赖协议层
# ============================================================


def test_facade_never_imports_the_http_layer() -> None:
    """门面不 import `fastapi` / `app.api`。

    一旦 import，MCP 形态就为了调用工具而必须构造请求对象、理解状态码 ——
    而 `app.api.errors` 的存在会让这条路径**看起来能用**，直到有人
    在没有 HTTP 上下文的进程里调它。
    """
    source = (PROJECT_ROOT / "app" / "tool_facade.py").read_text(encoding="utf-8")
    imports = [
        line
        for line in source.splitlines()
        if re.match(r"\s*(from|import)\s", line)
    ]

    offenders = [
        line
        for line in imports
        if re.search(r"\b(fastapi|starlette|app\.api)\b", line)
    ]
    assert offenders == [], f"门面不得依赖协议层，实际有：{offenders}"


# ============================================================
# 5. REST 与门面同源（parity）
# ============================================================
#
# 两套**独立**装配：一套只走 REST，一套只走门面。
# 不用同一套跑两遍 —— 那样第二遍会落在"重复调用"的分支上
# （工具 5/6/7 会答 `reused`），比较的就成了两条不同的路径。
#
# 两套共用同一个 `storage_root` 与同一个 `work_dir`，
# 因此 `file_path` 这种含路径的字段也能逐字比较。


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
    """与 `test_api_tools.py` 同形的假网关（`tests/` 不是包，无法跨文件复用）。"""

    provider = "mock"
    tenant_id = "default"

    def __init__(self) -> None:
        self.pendings = [_pending("HT-1"), _pending("HT-2")]
        self.detail = _detail("HT-1")

    def list_pending(self, limit: int) -> list[PendingApprovalDTO]:
        return list(self.pendings)[:limit]

    def get_detail(self, instance_id: str) -> ApprovalDetailDTO:
        return self.detail

    def download_attachment(
        self, instance_id: str, attachment_id: str
    ) -> DownloadedAttachmentDTO:
        return DownloadedAttachmentDTO(
            content=PDF_BYTES, file_name="contract.pdf", content_type="application/pdf"
        )


class _Harness:
    """一套完整装配：临时库（交付的 `schema.sql`）+ 假网关 + 本地存储。"""

    def __init__(self, work_dir: Path, name: str, storage_root: Path) -> None:
        path = work_dir / f"{name}.db"
        conn = sqlite3.connect(path)
        try:
            conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            # 规则来自 seed.sql：不带它时 `review_rules` 是空表，
            # 工具 5 会成功地建出一个**零条规则**的批次 ——
            # 两条路径照样相等，但相等的是"都没做审查"。
            conn.executescript(SEED_FILE.read_text(encoding="utf-8"))
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
        self.storage = LocalFileStorage(storage_root / "objects")
        self.client = TestClient(app)

    # --- REST 侧：把依赖换成这一套 ---

    def install(self) -> None:
        app.dependency_overrides[get_db] = self._session_dependency
        app.dependency_overrides[get_gateway] = lambda: self.gateway
        app.dependency_overrides[get_storage] = lambda: self.storage
        app.dependency_overrides[get_parser_engine_version] = lambda: "test-engine-1.0"
        app.dependency_overrides[get_actor] = lambda: _FULL_ACCESS_ACTOR

    def uninstall(self) -> None:
        app.dependency_overrides.clear()
        self.engine.dispose()

    def _session_dependency(self):
        yield from transactional_session(self.factory())

    # --- 门面侧：显式传参，不经过任何协议层 ---

    def session(self) -> Session:
        return self.factory()

    def run(self, call):
        """在**与请求同一份**事务边界下执行一次门面调用。

        `transactional_session` 的语义（业务失败也提交）必须与 REST 侧一致，
        否则两边写下的证据行不同，比较出来的差异是测试自己造的。
        这里用 `yield from` 包一层，与 `app/db.py::get_db` 的写法**逐字同形** ——
        直接 `session.close()` 会把门面写下的每一行都回滚掉。
        """
        session = self.session()
        with _same_boundary_as_get_db(session):
            return call(session)

    # --- 种子 ---

    def seed_task(self, instance_id: str = "HT-1") -> int:
        with self.session() as session:
            task = ApprovalTask(
                provider="mock",
                tenant_id="default",
                instance_id=instance_id,
                approval_code=instance_id,
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

    def seed_attachment(self, task_id: int) -> int:
        with self.session() as session:
            attachment = ApprovalAttachment(
                task_id=task_id,
                attachment_id="A-1",
                file_name="contract.pdf",
                content_type="application/pdf",
                download_status="success",
                object_key="sha256/aa/aa/deadbeef.pdf",
                file_checksum="a" * 64,
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

    def save_and_confirm_result(self, run_id: int) -> int:
        """经服务层落一份已确认的结果（工具 7 的前置）。

        ⚠️ 走服务函数而不是 REST：本文件测的是门面与 REST 的**同源性**，
        用第三个接口铺前置会让失败原因混进"到底是哪一层坏了"。
        """
        session = self.session()
        try:
            from app.services.result_service import save_review_result as save_service

            saved = save_service(
                session,
                run_id=run_id,
                overall_risk_level="low",
                summary_text="审查摘要",
                focus_points_json=["关注点一"],
                comment_text="回写正文",
                actor=_FULL_ACCESS_ACTOR,
            )
            confirm_result(
                session, result_id=saved.result_id, actor=_FULL_ACCESS_ACTOR
            )
            session.commit()
            return saved.result_id
        finally:
            session.close()


@pytest.fixture()
def parity(work_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """两套独立装配（REST / 门面），共用同一个存储根与工作目录。"""
    storage_root = work_dir / "storage"
    monkeypatch.setattr(settings, "storage_root", str(storage_root))
    monkeypatch.setattr(settings, "attachment_allowed_types", "application/pdf")
    monkeypatch.setattr(settings, "attachment_max_bytes", 1024 * 1024)

    rest = _Harness(work_dir, "rest", storage_root)
    direct = _Harness(work_dir, "direct", storage_root)
    rest.install()
    try:
        yield rest, direct
    finally:
        rest.uninstall()
        direct.engine.dispose()


def _assert_same_body(rest_body: dict, facade_result: dict) -> None:
    """逐字相等 —— 多一个字段也是分叉（见模块 docstring）。"""
    assert rest_body == facade_result, (
        "REST 与门面的响应体必须**逐字相等**。"
        f"只在 REST 侧出现的键：{sorted(set(rest_body) - set(facade_result))}；"
        f"只在门面侧出现的键：{sorted(set(facade_result) - set(rest_body))}"
    )


def test_tool1_rest_and_facade_agree(parity) -> None:
    rest, direct = parity

    response = rest.client.post(
        "/tools/list_pending_contract_approvals", json={"limit": 10}
    )
    assert response.status_code == 200

    facade = direct.run(
        lambda session: tool_facade.list_pending_contract_approvals(
            10, session=session, gateway=direct.gateway, actor=_FULL_ACCESS_ACTOR
        )
    )

    _assert_same_body(response.json(), facade)


def test_tool2_rest_and_facade_agree(parity) -> None:
    rest, direct = parity

    response = rest.client.post(
        "/tools/get_contract_approval", json={"instance_id": "HT-1"}
    )
    assert response.status_code == 200

    facade = direct.run(
        lambda session: tool_facade.get_contract_approval(
            "HT-1", session=session, gateway=direct.gateway, actor=_FULL_ACCESS_ACTOR
        )
    )

    _assert_same_body(response.json(), facade)


def test_tool3_rest_and_facade_agree(parity) -> None:
    rest, direct = parity
    for harness in (rest, direct):
        harness.seed_task()

    response = rest.client.post(
        "/tools/download_contract_attachment",
        json={"instance_id": "HT-1", "attachment_id": "A-1"},
    )
    assert response.status_code == 200

    facade = direct.run(
        lambda session: tool_facade.download_contract_attachment(
            "HT-1",
            "A-1",
            None,
            session=session,
            gateway=direct.gateway,
            storage=direct.storage,
            actor=_FULL_ACCESS_ACTOR,
        )
    )

    _assert_same_body(response.json(), facade)


def test_tool4_rest_and_facade_agree(parity) -> None:
    rest, direct = parity
    attachment_ids = []
    for harness in (rest, direct):
        task_id = harness.seed_task()
        attachment_ids.append(harness.seed_attachment(task_id))

    response = rest.client.post(
        "/tools/parse_contract_document", json={"document_id": attachment_ids[0]}
    )
    assert response.status_code == 200

    facade = direct.run(
        lambda session: tool_facade.parse_contract_document(
            str(attachment_ids[1]),
            session=session,
            engine_version="test-engine-1.0",
            actor=_FULL_ACCESS_ACTOR,
        )
    )

    _assert_same_body(response.json(), facade)


def test_tool5_rest_and_facade_agree(parity) -> None:
    rest, direct = parity
    parse_ids = []
    for harness in (rest, direct):
        task_id = harness.seed_task()
        attachment_id = harness.seed_attachment(task_id)
        parse_ids.append(harness.seed_parse(task_id, attachment_id))

    response = rest.client.post(
        "/tools/run_contract_rules", json={"parse_id": parse_ids[0]}
    )
    assert response.status_code == 200

    facade = direct.run(
        lambda session: tool_facade.run_contract_rules(
            str(parse_ids[1]), session=session, actor=_FULL_ACCESS_ACTOR
        )
    )

    _assert_same_body(response.json(), facade)


def test_tool6_rest_and_facade_agree(parity) -> None:
    rest, direct = parity
    run_ids = []
    for harness in (rest, direct):
        task_id = harness.seed_task()
        attachment_id = harness.seed_attachment(task_id)
        parse_id = harness.seed_parse(task_id, attachment_id)
        run_ids.append(harness.seed_run(task_id, parse_id))

    body = {
        "run_id": run_ids[0],
        "overall_risk_level": "low",
        "summary_text": "审查摘要",
        "focus_points": ["关注点一"],
        "comment_text": "回写正文",
    }
    response = rest.client.post("/tools/save_review_result", json=body)
    assert response.status_code == 200, response.text

    facade = direct.run(
        lambda session: tool_facade.save_review_result(
            str(run_ids[1]),
            "low",
            "审查摘要",
            json.dumps(["关注点一"], ensure_ascii=False),
            "回写正文",
            session=session,
            actor=_FULL_ACCESS_ACTOR,
        )
    )

    _assert_same_body(response.json(), facade)


def test_tool7_rest_and_facade_agree(parity) -> None:
    rest, direct = parity
    result_ids = []
    for harness in (rest, direct):
        task_id = harness.seed_task()
        attachment_id = harness.seed_attachment(task_id)
        parse_id = harness.seed_parse(task_id, attachment_id)
        run_id = harness.seed_run(task_id, parse_id)
        result_ids.append(harness.save_and_confirm_result(run_id))

    response = rest.client.post(
        "/tools/write_approval_comment",
        json={"instance_id": "HT-1", "result_id": result_ids[0]},
    )
    assert response.status_code == 200

    facade = direct.run(
        lambda session: tool_facade.write_approval_comment(
            "HT-1", str(result_ids[1]), session=session, actor=_FULL_ACCESS_ACTOR
        )
    )

    _assert_same_body(response.json(), facade)


# ============================================================
# 6. REST 是形态转换，不吞参数
# ============================================================


def test_rest_forwards_the_enterprise_context_it_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REST 收下的企业上下文必须**原样**到达门面。

    ⚠️ 这两条最容易"静默失效"：`force` 与 `parse_options` 都有默认值，
    端点忘了转发时接口照样 200、照样返回一个批次 ——
    只是**强制重跑变成了复用**、**解析配置变成了默认配置**，
    而两者都不会有任何症状。
    """

    class _Sentinel:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    seen: dict[str, Any] = {}

    def fake_parse(document_id, **kwargs):
        seen["parse"] = {"document_id": document_id, **kwargs}
        return {"outcome": "queued", "task_ref": None, "result_url": None, "cache_hit": False}

    def fake_rules(case_id, **kwargs):
        seen["rules"] = {"case_id": case_id, **kwargs}
        return {"outcome": "queued", "task_ref": None, "result_url": None, "cache_hit": False}

    monkeypatch.setattr(tool_facade, "parse_contract_document", fake_parse)
    monkeypatch.setattr(tool_facade, "run_contract_rules", fake_rules)

    def _unused_session():
        # ⚠️ 必须覆盖 `get_db`：端点即使不用 session，FastAPI 也会解析这个依赖 ——
        # 不覆盖时它会连上**真实交付库**（`data/app.db`），
        # 于是"转发了什么参数"的测试顺带往生产库里开了一次事务。
        yield None

    client = TestClient(app)
    app.dependency_overrides[get_actor] = lambda: _FULL_ACCESS_ACTOR
    app.dependency_overrides[get_db] = _unused_session
    try:
        parsed = client.post(
            "/tools/parse_contract_document",
            json={"document_id": 7, "parse_options": {"dpi": 300}},
        )
        ruled = client.post(
            "/tools/run_contract_rules", json={"parse_id": 9, "force": True}
        )
    finally:
        app.dependency_overrides.clear()

    assert parsed.status_code == 200, parsed.text
    assert ruled.status_code == 200, ruled.text

    assert seen["parse"]["document_id"] == "7", "需求把 document_id 定义为字符串"
    assert seen["parse"]["parse_options"] is not None, "解析配置被端点吞掉了"
    assert seen["parse"]["parse_options"].dpi == 300

    assert seen["rules"]["case_id"] == "9"
    assert seen["rules"]["force"] is True, "force 被端点吞掉了 —— 强制重跑会静默变成复用"
