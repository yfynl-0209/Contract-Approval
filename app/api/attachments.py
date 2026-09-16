"""附件内容与标准文档下发（M7 / Task 4）。

本模块关闭**缺口 G-1**：在此之前，附件字节没有可用的获取接口 ——
工具 3 只返回一个**受控临时物化路径**（`workspace/…`），
那是给同进程的解析器读的，不是给调用端读的。控制台要展示合同原文时，
除了那个路径之外没有任何选择，而把路径下发的后果是**绕过鉴权**。

## 两条下发路径，两种内容

| 接口 | 下发什么 | 为什么不复用另一个 |
| --- | --- | --- |
| `GET /api/attachments/{id}/content` | **原始字节**（PDF / 图片） | 前端要显示原件，需要原字节与 Range |
| `GET /api/parses/{id}/document` | **标准文档**（页 / 块 / 逐字符坐标） | 它是 M4 的结构化契约，前端据它画证据框 |

## 字节永远经 `ObjectStorage` 流出

本模块**不知道**字节存在磁盘上还是 MinIO 里，也不知道对象键长什么样 ——
`object_key` 与文件系统路径**一律不出现在响应体与响应头里**。
下发它们会让调用端依赖具体的存储布局，于是 M9 换 MinIO 变成破坏性变更；
更直接的是，一个可预测的路径等于一条绕过鉴权的读取通道。

## Range 支持到哪一步

支持单区间（`bytes=0-99` / `bytes=100-` / `bytes=-100`）。**多区间不支持**：
`bytes=0-9,20-29` 按 RFC 9110 §14.2 允许服务端忽略 `Range` 并返回完整实体，
这里就返回 **200 + 完整正文**（而不是自作主张只给第一段 —— 那会让调用端
以为自己拿全了）。

## 当前实现是"限制大小后**整份读取**并分片响应"

⚠️ **不要把这个实现描述成"从 `ObjectStorage` 流式读取"——它不是。**

`ObjectStorage` 端口目前只有 `get(key) -> bytes`，本模块的做法是：整份读进内存，
再按 `Range` 切出要发的那一段。**真正的对象存储流式 / Range 读取延后至 M9
的 MinIO 适配器实现**，届时由它扩展端口能力（如 `stat()` / `open_stream()` /
`read_range()`），而不是让 API 层先把整份字节下载到进程内存里再切。

当前这么做是可接受的，因为 `attachment_max_bytes`（默认 20MB）已经限定了上界：
一次请求最多占用一份附件大小的内存。但**这个上界是配置，不是架构**——
调大它不会报错，只会让内存占用跟着涨。

> **技术债（已登记到 M9 Task 4）**：M9 换 MinIO 时必须一并扩展端口，
> 否则"换成对象存储"只换了字节的存放位置，读取路径仍然是全量下载。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_storage, require_permissions
from app.api.views import iso, page_json
from app.auth import Actor, Permission
from app.enums import DownloadStatus, ErrorCode
from app.errors import PermanentError, PermanentStorageError
from app.models import ApprovalAttachment, ContractParse, ParseArtifact
from app.ports.object_storage import ObjectStorage
from app.ports.parse_document import StandardDocument
from app.services import query_service

router = APIRouter(prefix="/api", tags=["附件内容与标准文档（M7）"])

#: 标准文档工件的类型名（与 `app/services/parse_service.py` 写入侧同一取值）。
ARTIFACT_STANDARD_DOCUMENT = "standard_document"

#: `bytes=…`。**只认这一种单位**：`items=` 之类是别的资源类型，
#: 当作合法 Range 处理会得到一段客户端的字节区间。
_RANGE_PATTERN = re.compile(r"bytes=(?P<spec>\d*-\d*(?:,\d*-\d*)*)$")


@dataclass(frozen=True, slots=True)
class _Range:
    """一个**已解析并校验过**的字节区间（两端都含）。"""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


class _Unsatisfiable(Exception):
    """Range 语法合法、但在当前实体上无法满足 → 416。"""


def _parse_range(header: str | None, *, size: int) -> _Range | None:
    """`Range` 头 → 区间；`None` 表示"给完整实体"。

    Raises:
        _Unsatisfiable: 区间越出实体范围（`start >= size`）。
            语法错误、单位不对、多区间**不抛** —— 按 RFC 9110 §14.2 忽略该头，
            返回完整实体。把语法错误当成 416 会让一次拼错头的客户端
            永远拿不到内容，而它想要的其实是整份文件。
    """
    if header is None:
        return None

    match = _RANGE_PATTERN.fullmatch(header.strip())
    if match is None:
        return None

    spec = match.group("spec")
    if "," in spec:
        return None

    raw_start, _, raw_end = spec.partition("-")

    if not raw_start:
        # `bytes=-N`：**末尾 N 字节**。`N == 0` 不可满足（RFC 9110 §14.1.1）。
        suffix = int(raw_end)
        if suffix == 0:
            raise _Unsatisfiable
        start = max(0, size - suffix)
        return _Range(start=start, end=size - 1)

    start = int(raw_start)
    if start >= size:
        raise _Unsatisfiable

    if not raw_end:
        return _Range(start=start, end=size - 1)

    end = int(raw_end)
    if end < start:
        # `bytes=5-2` 语法合法但语义为空。当作不可满足，
        # 而不是悄悄换成一个"看起来合理"的区间。
        raise _Unsatisfiable
    return _Range(start=start, end=min(end, size - 1))


def _content_disposition(file_name: str | None) -> str:
    """`Content-Disposition`：**两种文件名一起给**。

    | 参数 | 给谁 | 为什么不能只留一个 |
    | --- | --- | --- |
    | `filename` | 老客户端 | 只给 `filename*` 时它们会退化成"下载一个无名的文件" |
    | `filename*` | 现代客户端 | 只给 `filename` 时中文名被按 Latin-1 解，得到乱码 |

    ⚠️ 必须先**剔除 CR / LF / 引号 / 反斜杠**：文件名来自外部审批系统，
    把原始值拼进响应头等于开了一条**响应头注入**的路
    （`文件名\r\nSet-Cookie: …`）。这是唯一一处把外部字符串直接放进头的地方，
    因此净化写在这里而不是指望上游。
    """
    name = (file_name or "").strip() or "attachment"
    cleaned = name.replace("\\", "").replace('"', "")
    cleaned = cleaned.replace("\r", "").replace("\n", "")

    # ASCII 兜底：非 ASCII 全丢掉。丢掉后为空时给一个固定名，
    # 而不是留下 `filename=""`（某些客户端会因此拒绝保存）。
    fallback = cleaned.encode("ascii", "ignore").decode("ascii").strip() or "attachment"

    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(cleaned, safe='')}"


def _assert_task_visible(
    session: Session, task_id: int, *, actor: Actor, what: str, ref: Any
) -> None:
    """归属校验：`404`（与"不存在"不可区分）。判据只有 `query_service` 一份。"""
    query_service.assert_visible(
        query_service.task_of(session, task_id),
        tenant_id=actor.tenant_id,
        what=what,
        ref=ref,
    )


def _read_object(
    storage: ObjectStorage, *, key: str, expected_sha256: str | None
) -> tuple[bytes, str]:
    """取回**整份**对象并核验摘要，返回 `(字节, 实际摘要)`。

    ⚠️ **这是全量读取，不是流式**（理由见模块 docstring 的"技术债"一节）。
    `Range` 是在拿到整份字节之后才切的。M9 扩展了端口的 `read_range()` 之后，
    这里应该是唯一需要改动的调用点。

    ⚠️ 核验不是可选的。ETag 用的是这个摘要，而调用端会拿它做缓存判据 ——
    磁盘上的字节若与记录不符（损坏、被外部进程改写），
    不核验就会下发一份**与 ETag 不符**的内容，且双方都不知道。
    "证据可核验"这条承诺正是在这里兑现的。

    ⚠️ 区间请求也核验**整份**：只核验被请求的那一段时，
    "磁盘上的文件被换成另一份等长的内容"在这条路径上完全看不出来 ——
    而 ETag 说的仍是旧摘要。

    Raises:
        PermanentStorageError: `CHECKSUM_MISMATCH` —— 取回的字节与记录的摘要不符。
    """
    data = storage.get(key)
    digest = hashlib.sha256(data).hexdigest()

    if expected_sha256 and digest != expected_sha256:
        raise PermanentStorageError(
            "取回的字节与记录的摘要不符：这份证据已经不可信，"
            "拒绝下发（否则调用端会拿到一份与 ETag 不符的内容）",
            code=ErrorCode.CHECKSUM_MISMATCH,
        )
    return data, digest


# ============================================================
# 附件字节
# ============================================================


@router.get(
    "/attachments/{attachment_id}/content",
    summary="下载附件原始字节（支持 Range）",
    description=(
        "按需下发附件的原始内容，**支持单区间 Range**。\n\n"
        "| 请求 | 响应 |\n"
        "| --- | --- |\n"
        "| 无 `Range` | **200** + 完整正文 |\n"
        "| `bytes=0-99` | **206** + `Content-Range: bytes 0-99/总长` |\n"
        "| `bytes=100-` | **206**，从 100 到末尾 |\n"
        "| `bytes=-100` | **206**，最后 100 字节 |\n"
        "| `bytes=999999-`（越界） | **416** + `Content-Range: bytes */总长` |\n"
        "| 语法错误 / 多区间 | **200** + 完整正文（按 RFC 9110 §14.2 忽略 `Range`） |\n\n"
        "**响应头**：`Content-Type` / `Content-Length` / `Accept-Ranges: bytes` /\n"
        "`ETag`（内容 SHA-256）/ `Content-Disposition`（带 UTF-8 文件名）/\n"
        "`X-Content-Type-Options: nosniff`。\n\n"
        "⚠️ **响应里不出现对象键或文件系统路径**（正文、响应头都没有）——\n"
        "下发可预测的路径等于开一条绕过鉴权的读取通道，也会让 M9 换 MinIO\n"
        "变成破坏性变更。\n\n"
        "⚠️ 附件记录存在但**字节还没入库**（工具 3 没跑过 / 下载失败）→\n"
        "404 + `OBJECT_NOT_FOUND`，与「id 写错了」的 `RESOURCE_NOT_FOUND` 区分开：\n"
        "前者的正确处置是**先跑工具 3**，后者是核对 id。"
    ),
)
def get_attachment_content(
    attachment_id: int,
    request: Request,
    session: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_storage),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> Response:
    attachment = session.get(ApprovalAttachment, attachment_id)
    if attachment is None:
        raise PermanentError(
            f"附件 {attachment_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )

    _assert_task_visible(
        session, attachment.task_id, actor=actor, what="附件", ref=attachment_id
    )

    if (
        attachment.download_status != DownloadStatus.SUCCESS.value
        or not attachment.object_key
    ):
        # ⚠️ 错误码**不是** `RESOURCE_NOT_FOUND`：附件记录确实存在，
        # 缺的是字节。两者的正确处置不同 —— 这里要去跑一次工具 3。
        raise PermanentError(
            f"附件 {attachment_id} 的字节尚未入库"
            f"（download_status={attachment.download_status!r}）——"
            "请先调用工具 3 下载该附件",
            code=ErrorCode.OBJECT_NOT_FOUND,
        )

    data, digest = _read_object(
        storage, key=attachment.object_key, expected_sha256=attachment.file_checksum
    )
    return _bytes_response(
        data=data,
        digest=digest,
        content_type=attachment.content_type or "application/octet-stream",
        file_name=attachment.file_name,
        range_header=request.headers.get("Range"),
    )


def _bytes_response(
    *,
    data: bytes,
    digest: str,
    content_type: str,
    file_name: str | None,
    range_header: str | None,
) -> Response:
    """把字节装成响应（含 Range 与缓存头）。"""
    size = len(data)

    headers: dict[str, str] = {
        "Accept-Ranges": "bytes",
        # `nosniff` 必须给：附件类型来自外部系统的声明，
        # 而浏览器"猜类型"会把一份声称 application/pdf 的 HTML 当页面渲染 ——
        # 那是存储型 XSS 的标准入口。
        "X-Content-Type-Options": "nosniff",
        # 摘要用引号包起来（RFC 9110 §8.8.3 的 entity-tag 语法）。
        # 不传 `W/`：这是内容摘要而不是弱校验，字节变了 ETag 就必须变。
        #
        # ⚠️ `digest` 是**实际发出的那些字节**的摘要（`_read_object` 取回后算的），
        # 因此这里成立的前提是：`data` 之后**没有被再加工过**。
        # 将来若在中间插入任何转换 / 脱敏 / 重新编码（例如图片转码、加水印），
        # **必须重新计算摘要** —— 沿用原文摘要时缓存与校验全部失效，
        # 而且失效方式无声：调用端拿到的 ETag 对应的是它**没有**收到的那些字节。
        "ETag": f'"{digest}"',
        "Content-Disposition": _content_disposition(file_name),
    }

    try:
        span = _parse_range(range_header, size=size)
    except _Unsatisfiable:
        return Response(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={**headers, "Content-Range": f"bytes */{size}"},
        )

    if span is None:
        headers["Content-Length"] = str(size)
        return Response(content=data, media_type=content_type, headers=headers)

    chunk = data[span.start : span.end + 1]
    headers["Content-Range"] = f"bytes {span.start}-{span.end}/{size}"
    headers["Content-Length"] = str(len(chunk))
    return Response(
        content=chunk,
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=content_type,
        headers=headers,
    )


# ============================================================
# 标准文档
# ============================================================


@router.get(
    "/tasks/{task_id}/attachments",
    summary="某任务的附件列表（元数据，不含字节）",
    description=(
        "返回该任务的附件元数据，供控制台的「附件」区块渲染每一行。\n\n"
        "⚠️ **响应里没有 `object_key`，也没有 `file_path`**：\n"
        "- `object_key` 是内部存储布局（`sha256/ab/cd/…`），下发会让调用端依赖具体实现，\n"
        "  使 M9 换 MinIO 从「实现替换」变成「破坏性变更」；\n"
        "- `file_path` 是**受控临时物化路径**，只有工具 3（外部系统）按需求返回它。\n"
        "两者都不是内容获取手段 —— 取字节走每行给出的 `content_url`。\n\n"
        "`download_status` 的三态与「为什么失败」分两处：状态在这里，"
        "失败原因在同行 `error_message`；而「这是外部事实还是我方故障」"
        "取决于**任务级** `last_error_code`（见 `GET /api/tasks/{id}` 的 "
        "`last_error_is_business_fact`）—— 附件行本身不携带原因码，"
        "在这里编一个出来等于凭空造判据。\n\n"
        "跨租户与任务不存在都返回 **404**。"
    ),
)
def list_task_attachments(
    task_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=query_service.DEFAULT_PAGE_SIZE, ge=1, le=query_service.MAX_PAGE_SIZE
    ),
    session: Session = Depends(get_db),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    # 先过归属门（不存在 / 跨租户 → 404），再列附件 ——
    # 顺序反了会得到"别人的任务返回空列表"，而空列表读起来是"这份合同没有附件"
    query_service.get_task(session, tenant_id=actor.tenant_id, task_id=task_id)
    result = query_service.list_attachments(
        session,
        tenant_id=actor.tenant_id,
        task_id=task_id,
        page=page,
        page_size=page_size,
    )
    return page_json(result, attachment_json)


def attachment_json(row: ApprovalAttachment) -> dict[str, Any]:
    """附件的对外形状。

    ⚠️ **只列白名单字段**，而不是 `{**row.__dict__}`：后者每次给模型加列时
    都会自动把它下发出去 —— `object_key` 与 `file_path` 就是这么漏出去的。
    """
    return {
        "attachment_record_id": row.id,
        # 外部系统的附件编号（TEXT）。与本系统主键**同名不同义**，
        # 因此两个字段同时给出、名字不同（§4.5 修-15）
        "attachment_id": row.attachment_id,
        "file_name": row.file_name,
        "file_type": row.file_type,
        "content_type": row.content_type,
        "file_size": row.file_size,
        "file_checksum": row.file_checksum,
        "download_status": row.download_status,
        "error_message": row.error_message,
        # 内容只在**这里**给地址：UI 不拼路径，存储实现换了它也不变
        "content_url": f"/api/attachments/{row.id}/content",
        "created_at": iso(row.created_at),
    }


@router.get(
    "/parses/{parse_id}/document",
    summary="读取标准文档（M4 契约，原样下发）",
    description=(
        "返回该次解析的标准文档工件：页尺寸、**坐标系**、`rotation`、\n"
        "文本块、逐字符几何与两个精度声明（`text_precision` / `bbox_precision`）。\n\n"
        "⚠️ **原样下发，不下发时重算坐标**。字段值直接取自工件：\n"
        "`bbox` 已经换算到 `bbox_space` 声明的坐标系（旋转后的可见页面），\n"
        "**消费方不得再应用一次 `rotation`** —— 应用两次的框看起来只是「偏了一点」，\n"
        "而那正是最难被发现的一类错误。\n\n"
        "⚠️ 工件在读取时**通过 M4 的契约校验**再下发：校验不是重算坐标，\n"
        "而是「这份证据是否仍然自洽」（块的偏移是否仍切得出块文本、\n"
        "声明 `bbox_precision=char` 的块是否真的带 `chars`）。\n"
        "跳过校验时，一份损坏的工件会一路走到前端，表现为框画错了位置。\n\n"
        "`artifact_version` 与 `schema_version` 都在响应里，\n"
        "消费方据此判断自己读到的是哪一版契约。\n\n"
        "跨租户与不存在都返回 **404**。"
    ),
)
def get_parse_document(
    parse_id: int,
    session: Session = Depends(get_db),
    storage: ObjectStorage = Depends(get_storage),
    actor: Actor = Depends(require_permissions(Permission.TASK_READ)),
) -> dict[str, Any]:
    parse = session.get(ContractParse, parse_id)
    if parse is None:
        raise PermanentError(
            f"解析记录 {parse_id} 不存在", code=ErrorCode.RESOURCE_NOT_FOUND
        )

    _assert_task_visible(
        session, parse.task_id, actor=actor, what="解析记录", ref=parse_id
    )

    artifact = session.execute(
        select(ParseArtifact)
        .where(
            ParseArtifact.parse_id == parse_id,
            ParseArtifact.kind == ARTIFACT_STANDARD_DOCUMENT,
        )
        # 重解析 / 换管线会各留一份，取**版本号最大**的那一份 ——
        # 这与 `rule_service` 读取侧的取法一致，两处取不同版本时
        # 规则跑的依据与前端画的框会来自两份文档。
        .order_by(ParseArtifact.artifact_version.desc())
    ).scalars().first()

    if artifact is None:
        raise PermanentError(
            f"解析记录 {parse_id} 还没有标准文档工件（解析尚未完成？）",
            code=ErrorCode.OBJECT_NOT_FOUND,
        )

    data, digest = _read_object(
        storage, key=artifact.object_key, expected_sha256=artifact.sha256
    )

    try:
        document = StandardDocument.model_validate_json(data)
    except ValueError as exc:
        # 损坏的工件 → 明确说"工件不合法"，而不是让前端拿到一份
        # 结构上看不出问题的 JSON 然后在画框时出错。
        raise PermanentError(
            f"解析记录 {parse_id} 的标准文档工件不合法：{exc}",
            code=ErrorCode.INVALID_GATEWAY_RESPONSE,
        ) from exc

    return {
        "parse_id": parse_id,
        "task_id": parse.task_id,
        "artifact_id": artifact.id,
        "artifact_version": artifact.artifact_version,
        # 工件自身的摘要：消费方要核验"我拿到的就是那份证据"时有依据
        "sha256": digest,
        "size_bytes": len(data),
        # 契约版本 —— 结构会随 M8 演化，没有它消费方无法区分"旧格式"与"数据损坏"
        "schema_version": document.schema_version,
        "page_count": len(document.pages),
        "document": document.model_dump(mode="json"),
        "created_at": iso(artifact.created_at),
    }
