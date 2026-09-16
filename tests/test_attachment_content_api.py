"""附件字节与标准文档下发（M7 / Task 4）。

## 本文件守住的是"交付字节"这一类特有的失败

1. **路径泄漏** —— 响应里出现 `object_key` 或文件系统路径。
   它看起来只是"多了一个字段"，实际是一条**绕过鉴权的读取通道**：
   调用端拿它可以自己拼路径，而 M9 换 MinIO 时它还会变成破坏性变更。
2. **Range 边界写错** —— 越界返回 200 而不是 416，或 `Content-Range`
   的 `start-end` 与实体长度对不上。客户端据此拼出来的文件**看起来能打开**，
   只是尾部缺一段或错位。
3. **多区间被当成单区间** —— `bytes=0-9,20-29` 只给第一段时，
   调用端以为那就是全部内容，而它没有任何办法察觉。
4. **摘要不核验** —— 磁盘上的字节与记录不符时照常下发，
   于是 ETag 与实际内容不一致，缓存被永久污染，而两边都不知道。
5. **文件名直接进响应头** —— 外部系统的文件名带 `\\r\\n` 时可以注入响应头。

## 为什么用真实 `LocalFileStorage` 而不是假存储

本文件要验的正是"字节**经端口**流出、而**键不出现在响应里**"。
用假存储时，那条断言只是在断言自己的假实现。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.storage.local_file_storage import LocalFileStorage
from app.api.deps import get_actor, get_db, get_storage
from app.auth import Actor, Role
from app.config import PROJECT_ROOT, settings
from app.db import transactional_session
from app.enums import BboxPrecision, PageStatus, TextPrecision
from app.main import app
from app.models import ApprovalAttachment, ApprovalTask, ContractParse, ParseArtifact
from app.ports.object_storage import content_addressed_key
from app.ports.parse_document import (
    BBOX_SPACE,
    DocumentBlock,
    DocumentChar,
    DocumentPage,
    StandardDocument,
)

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"

#: 一段能被切成多个区间的假 PDF（不是真 PDF，本文件只关心字节）。
PDF_BYTES = b"%PDF-1.4\n" + bytes(range(256)) * 4 + b"\n%%EOF"

#: 带中文的文件名 —— 只给 `filename` 时它会被按 Latin-1 解成乱码。
CHINESE_NAME = "采购合同（扫描件）.pdf"

#: 带 CRLF 的文件名 —— 直接拼进响应头就是**响应头注入**。
INJECTING_NAME = 'evil.pdf\r\nX-Injected: yes'


def _actor(name: str, tenant_id: str, *, role: Role = Role.SYSTEM_ADMIN) -> Actor:
    return Actor(
        actor_id=name,
        display_name=name,
        roles=frozenset({role.value}),
        tenant_id=tenant_id,
    )


def _document() -> StandardDocument:
    """一份最小的标准文档：一页、一个块、两个字符（`bbox_precision=char`）。

    ⚠️ 刻意声明 `bbox_precision=char` 并给出 `chars`：契约里
    "声明 char 却没有 chars"是**构造即拒绝**的，本文件顺带证明
    下发路径不会把它悄悄抹平成块级精度。
    """
    text = "采购合同"
    chars = tuple(
        DocumentChar(
            text=character,
            bbox=(72.5 + index * 12.0, 100.25, 84.5 + index * 12.0, 112.25),
            char_start=index,
            char_end=index + 1,
        )
        for index, character in enumerate(text)
    )
    block = DocumentBlock(
        block_id="p1-b0",
        text=text,
        bbox=(72.5, 100.25, 120.5, 112.25),
        char_start=0,
        char_end=len(text),
        text_precision=TextPrecision.CHAR,
        bbox_precision=BboxPrecision.CHAR,
        chars=chars,
    )
    page = DocumentPage(
        page=1,
        width=595.0,
        height=842.0,
        bbox_space=BBOX_SPACE,
        rotation=0,
        source="text",
        page_status=PageStatus.OK,
        text=text,
        char_map=(),
        blocks=(block,),
    )
    return StandardDocument(schema_version=1, pages=(page,))


class _Harness:
    def __init__(self, work_dir: Path) -> None:
        path = work_dir / "content.db"
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
        self.storage = LocalFileStorage(work_dir / "storage" / "objects")
        self.client = TestClient(app)
        self.current = {"actor": _actor("admin-a", TENANT_A)}
        self.identity_enabled = True

    def install(self) -> None:
        def session_dependency():
            yield from transactional_session(self.factory())

        def actor_dependency():
            if not self.identity_enabled:
                # 复现"没有任何身份来源"：让依赖本身抛 401，
                # 而不是填一个匿名主体 —— 匿名主体会让端点**继续往下跑**，
                # 于是"未认证也能拿到字节"这条根本不会被发现。
                from app.auth import AuthenticationError

                raise AuthenticationError("本请求没有携带任何身份")

            return self.current["actor"]

        app.dependency_overrides[get_db] = session_dependency
        app.dependency_overrides[get_actor] = actor_dependency
        app.dependency_overrides[get_storage] = lambda: self.storage

    def uninstall(self) -> None:
        app.dependency_overrides.clear()
        self.engine.dispose()

    def act_as(self, actor: Actor) -> None:
        self.current["actor"] = actor

    @contextmanager
    def no_identity(self):
        self.identity_enabled = False
        try:
            yield
        finally:
            self.identity_enabled = True

    def session(self) -> Session:
        return self.factory()

    # --- 种子 ---

    def seed_task(
        self,
        *,
        instance_id: str = "HT-1",
        tenant_id: str = TENANT_A,
        run_status: str = "completed",
    ) -> int:
        with self.session() as session:
            task = ApprovalTask(
                provider="mock",
                tenant_id=tenant_id,
                instance_id=instance_id,
                approval_code=instance_id,
                task_status="reviewing",
                context_status="confirmed",
                our_party_name="我方公司",
                our_party_contract_label="party_a",
                our_party_business_role="buyer",
                contract_type="procurement",
            )
            session.add(task)
            session.commit()
            return task.id

    def seed_attachment(
        self,
        task_id: int,
        *,
        content: bytes = PDF_BYTES,
        file_name: str = CHINESE_NAME,
        download_status: str = "success",
        content_type: str = "application/pdf",
        store: bool = True,
        recorded_checksum: str | None = None,
    ) -> int:
        """落一条附件记录，并把字节**真的写进对象存储**。

        `store=False` 用来复现"记录有了、字节还没入库"。
        `recorded_checksum` 用来复现"库里的摘要与实际字节不符"。
        """
        digest = hashlib.sha256(content).hexdigest()
        key = content_addressed_key(digest, suffix="pdf")
        if store:
            self.storage.put(key, content, content_type=content_type)

        with self.session() as session:
            attachment = ApprovalAttachment(
                task_id=task_id,
                attachment_id="A-1",
                file_name=file_name,
                content_type=content_type,
                download_status=download_status,
                object_key=key if store else None,
                file_checksum=(
                    recorded_checksum if recorded_checksum is not None else digest
                ),
            )
            session.add(attachment)
            session.commit()
            return attachment.id

    def seed_parse_with_document(
        self, task_id: int, *, document: StandardDocument | None = None, store: bool = True
    ) -> tuple[int, str]:
        """落一条解析记录 + 标准文档工件，返回 `(parse_id, 工件原文)`。"""
        document = document or _document()
        raw = document.model_dump_json()
        data = raw.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        key = content_addressed_key(digest, suffix="json")
        if store:
            self.storage.put(key, data, content_type="application/json")

        with self.session() as session:
            parse = ContractParse(
                task_id=task_id,
                attachment_id=1,
                parse_status="succeeded",
                parse_version=1,
            )
            session.add(parse)
            session.flush()
            if store:
                session.add(
                    ParseArtifact(
                        parse_id=parse.id,
                        kind="standard_document",
                        object_key=key,
                        sha256=digest,
                        size_bytes=len(data),
                        content_type="application/json",
                        artifact_version=1,
                    )
                )
            session.commit()
            return parse.id, raw


@pytest.fixture()
def harness(work_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "storage_root", str(work_dir / "storage"))
    built = _Harness(work_dir)
    built.install()
    try:
        yield built
    finally:
        built.uninstall()


# ============================================================
# 1. 完整正文与响应头
# ============================================================


def test_full_body_carries_the_contract_headers(harness: _Harness) -> None:
    """**验收**：无 `Range` → 200 + 完整正文 + 一组契约头。"""
    task_id = harness.seed_task()
    attachment_id = harness.seed_attachment(task_id)

    response = harness.client.get(f"/api/attachments/{attachment_id}/content")

    assert response.status_code == 200
    assert response.content == PDF_BYTES
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-length"] == str(len(PDF_BYTES))
    assert response.headers["accept-ranges"] == "bytes"
    # ETag 是内容摘要，带引号（entity-tag 语法）
    assert response.headers["etag"] == f'"{hashlib.sha256(PDF_BYTES).hexdigest()}"'
    # 没有 nosniff 时，浏览器会把一份声称 application/pdf 的 HTML 当页面渲染
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment;")


def test_a_wrong_id_and_a_missing_object_are_different_machine_codes(
    harness: _Harness,
) -> None:
    """**验收**：两种 404 的机器码不同 —— 处置也不同。

    | 情况 | 码 | 正确处置 |
    | --- | --- | --- |
    | id 写错了 | `RESOURCE_NOT_FOUND` | 核对 id |
    | 字节还没入库 | `OBJECT_NOT_FOUND` | **先跑工具 3** |

    合成一个码时，调用方会把"还没下载"当成"id 错了"去改 id。
    """
    task_id = harness.seed_task()
    not_downloaded = harness.seed_attachment(task_id, store=False, download_status="pending")

    missing = harness.client.get("/api/attachments/424242/content")
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "RESOURCE_NOT_FOUND"

    not_yet = harness.client.get(f"/api/attachments/{not_downloaded}/content")
    assert not_yet.status_code == 404
    assert not_yet.json()["error_code"] == "OBJECT_NOT_FOUND"


# ============================================================
# 2. Range
# ============================================================


def test_a_valid_byte_range_returns_206_with_content_range(harness: _Harness) -> None:
    """**验收**：`bytes=0-99` → 206，且 `Content-Range` 与实体长度对得上。

    ⚠️ `Content-Range` 的 `start-end` 或总长写错时，客户端拼出来的文件
    **看起来能打开**，只是尾部缺一段或错位 —— 这是本文件最要紧的一条断言。
    """
    task_id = harness.seed_task()
    attachment_id = harness.seed_attachment(task_id)
    size = len(PDF_BYTES)

    response = harness.client.get(
        f"/api/attachments/{attachment_id}/content",
        headers={"Range": "bytes=0-99"},
    )

    assert response.status_code == 206
    assert response.content == PDF_BYTES[:100]
    assert response.headers["content-range"] == f"bytes 0-99/{size}"
    assert response.headers["content-length"] == "100"


def test_an_open_ended_range_runs_to_the_end(harness: _Harness) -> None:
    task_id = harness.seed_task()
    attachment_id = harness.seed_attachment(task_id)
    size = len(PDF_BYTES)

    response = harness.client.get(
        f"/api/attachments/{attachment_id}/content",
        headers={"Range": "bytes=100-"},
    )

    assert response.status_code == 206
    assert response.content == PDF_BYTES[100:]
    assert response.headers["content-range"] == f"bytes 100-{size - 1}/{size}"


def test_a_suffix_range_returns_the_last_bytes(harness: _Harness) -> None:
    """**验收**：`bytes=-100` 是**末尾 100 字节**，不是"从 100 开始"。

    这两种读法都"能返回 206 与一段数据"，因此写反时不会报错 ——
    只会返回另一段。
    """
    task_id = harness.seed_task()
    attachment_id = harness.seed_attachment(task_id)
    size = len(PDF_BYTES)

    response = harness.client.get(
        f"/api/attachments/{attachment_id}/content",
        headers={"Range": "bytes=-100"},
    )

    assert response.status_code == 206
    assert response.content == PDF_BYTES[-100:]
    assert response.headers["content-range"] == f"bytes {size - 100}-{size - 1}/{size}"


def test_a_range_past_the_end_is_416(harness: _Harness) -> None:
    """**验收**：越界 → **416**，且带 `Content-Range: bytes */总长`。

    返回 200（空正文或完整正文）都会让调用端以为自己拿对了。
    """
    task_id = harness.seed_task()
    attachment_id = harness.seed_attachment(task_id)
    size = len(PDF_BYTES)

    response = harness.client.get(
        f"/api/attachments/{attachment_id}/content",
        headers={"Range": f"bytes={size}-"},
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{size}"


def test_a_zero_length_suffix_is_416(harness: _Harness) -> None:
    """`bytes=-0` 按 RFC 9110 §14.1.1 不可满足，不是"最后 0 字节"。"""
    task_id = harness.seed_task()
    attachment_id = harness.seed_attachment(task_id)

    response = harness.client.get(
        f"/api/attachments/{attachment_id}/content", headers={"Range": "bytes=-0"}
    )

    assert response.status_code == 416


@pytest.mark.parametrize(
    "header",
    [
        "items=0-9",  # 单位不对：不是字节区间
        "bytes=abc-def",  # 语法错
        "bytes=0-9,20-29",  # 多区间：本实现不支持
        "bytes",  # 只有单位
        "bytes=",  # 空区间
    ],
)
def test_a_header_we_do_not_support_is_ignored_not_416(
    harness: _Harness, header: str
) -> None:
    """**验收**：不支持的 `Range` → **200 + 完整正文**（RFC 9110 §14.2 允许忽略）。

    ⚠️ 多区间刻意走到这一支，而**不是**"只给第一段"：
    只给第一段时调用端以为自己拿全了，而它没有任何办法察觉。
    完整正文至少是诚实的 —— 客户端会按 `Content-Length` 收完。
    """
    task_id = harness.seed_task()
    attachment_id = harness.seed_attachment(task_id)

    response = harness.client.get(
        f"/api/attachments/{attachment_id}/content", headers={"Range": header}
    )

    assert response.status_code == 200
    assert response.content == PDF_BYTES
    assert "content-range" not in response.headers


def test_a_range_not_starting_at_zero_still_verifies_the_whole_object(
    harness: _Harness,
) -> None:
    """区间请求照样核验**整份**摘要。

    ⚠️ 只核验被请求的那一段时，"磁盘上的文件被换成了另一份等长的内容"
    在这条路径上完全看不出来 —— 而 ETag 说的仍是旧摘要。
    """
    task_id = harness.seed_task()
    attachment_id = harness.seed_attachment(
        task_id, recorded_checksum="0" * 64
    )

    response = harness.client.get(
        f"/api/attachments/{attachment_id}/content", headers={"Range": "bytes=0-9"}
    )

    assert response.status_code == 500
    assert response.json()["error_code"] == "CHECKSUM_MISMATCH"


# ============================================================
# 3. 不下发路径
# ============================================================


def test_the_object_key_never_appears_anywhere_in_the_response(
    harness: _Harness,
) -> None:
    """**验收**：响应体与**每一个响应头**里都没有对象键或文件系统路径。

    只看响应体是不够的：`Content-Location` / `Link` 这类头同样能把它带出去，
    而它们"看起来只是元数据"。
    """
    task_id = harness.seed_task()
    attachment_id = harness.seed_attachment(task_id)

    with harness.session() as session:
        attachment = session.get(ApprovalAttachment, attachment_id)
        key = attachment.object_key

    response = harness.client.get(f"/api/attachments/{attachment_id}/content")

    blob = response.content.decode("latin-1") + json.dumps(dict(response.headers))
    assert key not in blob
    assert "storage" not in blob.lower(), "文件系统路径的任何片段都不该出现"
    assert "sha256/" not in blob, "对象键的前缀同样是指向存储布局的线索"


def test_a_filename_cannot_inject_response_headers(harness: _Harness) -> None:
    """**验收**：文件名里的 CRLF 被剔除，注入头不出现。

    文件名来自**外部审批系统**，是本模块唯一一处把外部字符串放进响应头的地方。
    """
    task_id = harness.seed_task()
    attachment_id = harness.seed_attachment(task_id, file_name=INJECTING_NAME)

    response = harness.client.get(f"/api/attachments/{attachment_id}/content")

    assert "x-injected" not in {name.lower() for name in response.headers}
    disposition = response.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition


def test_a_chinese_filename_is_carried_by_both_parameters(harness: _Harness) -> None:
    """**验收**：中文名走 `filename*`（UTF-8），同时留一个 ASCII 兜底。

    只给 `filename` 时中文被按 Latin-1 解成乱码；只给 `filename*` 时
    老客户端会退化成"下载一个无名的文件"。
    """
    task_id = harness.seed_task()
    attachment_id = harness.seed_attachment(task_id, file_name=CHINESE_NAME)

    disposition = harness.client.get(
        f"/api/attachments/{attachment_id}/content"
    ).headers["content-disposition"]

    assert "filename*=UTF-8''" in disposition
    assert "%E9%87%87%E8%B4%AD" in disposition, "中文必须被百分号编码"
    assert 'filename="' in disposition, "ASCII 兜底不能省"


# ============================================================
# 4. 身份与租户
# ============================================================


def test_reading_content_requires_an_identity(harness: _Harness) -> None:
    """**验收**：没有身份 → **401**（不是 200、不是 403）。

    401 说的是"去拿一份新身份"，403 说的是"带了也没用"。合并时
    客户端的处置有一半概率是错的。
    """
    task_id = harness.seed_task()
    attachment_id = harness.seed_attachment(task_id)

    with harness.no_identity():
        response = harness.client.get(f"/api/attachments/{attachment_id}/content")

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def test_a_read_only_auditor_may_read_content(harness: _Harness) -> None:
    """只读审计**能读** —— 否则"只读"这个角色名不副实。

    下载附件属于"看"，不属于"改"。把它也挡掉时，审计的人除了
    任务列表什么都看不到。
    """
    task_id = harness.seed_task()
    attachment_id = harness.seed_attachment(task_id)
    harness.act_as(_actor("auditor", TENANT_A, role=Role.READ_ONLY_AUDITOR))

    response = harness.client.get(f"/api/attachments/{attachment_id}/content")

    assert response.status_code == 200


def test_another_tenant_cannot_read_the_bytes(harness: _Harness) -> None:
    """**验收**：跨租户 → **404**，且响应里没有任何对方的字节。

    ⚠️ 断言"没有字节"而不只是"状态码是 404"：一次写错的实现
    可能先读了字节再判断归属，于是状态码对了、内容却在错误的响应体里漏出去了。
    """
    task_id = harness.seed_task(instance_id="HT-A", tenant_id=TENANT_A)
    attachment_id = harness.seed_attachment(task_id)
    harness.act_as(_actor("admin-b", TENANT_B))

    response = harness.client.get(f"/api/attachments/{attachment_id}/content")

    assert response.status_code == 404
    assert PDF_BYTES[:16] not in response.content
    assert "HT-A" not in response.text


# ============================================================
# 5. 标准文档
# ============================================================


def test_the_standard_document_is_delivered_verbatim(harness: _Harness) -> None:
    """**验收**：标准文档**原样下发**，坐标不被重算。

    判据是逐字段相等：`bbox` 的浮点值、`text_precision` / `bbox_precision`
    两个声明、`chars` 的逐字符几何都必须与工件里的一模一样。
    重算过一次的表现是"框偏了一点"，而那是最难被发现的一类错误。
    """
    task_id = harness.seed_task()
    parse_id, raw = harness.seed_parse_with_document(task_id)

    response = harness.client.get(f"/api/parses/{parse_id}/document")

    assert response.status_code == 200
    body = response.json()

    assert body["document"] == json.loads(raw), "下发的必须是工件原文"
    assert body["schema_version"] == 1
    assert body["page_count"] == 1

    block = body["document"]["pages"][0]["blocks"][0]
    assert block["bbox"] == [72.5, 100.25, 120.5, 112.25]
    assert block["bbox_precision"] == "char"
    assert block["text_precision"] == "char"
    assert len(block["chars"]) == 4, "逐字符几何不能被抹平成块级"
    assert block["chars"][0]["bbox"] == [72.5, 100.25, 84.5, 112.25]
    assert body["document"]["pages"][0]["bbox_space"] == "pdf-point-top-left"
    assert body["document"]["pages"][0]["rotation"] == 0


def test_the_document_response_does_not_leak_the_object_key(harness: _Harness) -> None:
    task_id = harness.seed_task()
    parse_id, _ = harness.seed_parse_with_document(task_id)

    with harness.session() as session:
        from sqlalchemy import select

        from app.models import ParseArtifact

        key = session.execute(select(ParseArtifact.object_key)).scalar_one()

    response = harness.client.get(f"/api/parses/{parse_id}/document")

    assert key not in response.text
    assert "sha256/" not in response.text


def test_a_parse_without_an_artifact_is_404_object_not_found(harness: _Harness) -> None:
    """没有工件 → `OBJECT_NOT_FOUND`：那份字节不存在，而不是 id 写错了。"""
    task_id = harness.seed_task()
    parse_id, _ = harness.seed_parse_with_document(task_id, store=False)

    response = harness.client.get(f"/api/parses/{parse_id}/document")

    assert response.status_code == 404
    assert response.json()["error_code"] == "OBJECT_NOT_FOUND"


def test_another_tenant_cannot_read_the_standard_document(harness: _Harness) -> None:
    task_id = harness.seed_task(instance_id="HT-A", tenant_id=TENANT_A)
    parse_id, _ = harness.seed_parse_with_document(task_id)
    harness.act_as(_actor("admin-b", TENANT_B))

    response = harness.client.get(f"/api/parses/{parse_id}/document")

    assert response.status_code == 404
    assert "采购合同" not in response.text


def test_a_checksum_mismatch_is_refused_instead_of_delivered(harness: _Harness) -> None:
    """工件摘要不符 → 拒绝下发。

    照常下发时，前端画出来的框来自一份**已经不是当初那份**的证据，
    而响应里没有任何线索指向这一点。
    """
    task_id = harness.seed_task()
    parse_id, _ = harness.seed_parse_with_document(task_id)

    with harness.session() as session:
        from sqlalchemy import select

        from app.models import ParseArtifact

        artifact = session.execute(select(ParseArtifact)).scalar_one()
        artifact.sha256 = "0" * 64
        session.commit()

    response = harness.client.get(f"/api/parses/{parse_id}/document")

    assert response.status_code == 500
    assert response.json()["error_code"] == "CHECKSUM_MISMATCH"
