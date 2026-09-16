"""Mock 审批系统适配器 —— 对接**独立进程**的外部 mock 服务（端口 8001）。

走真实 HTTP、真实 Bearer 鉴权，因此能真实经历超时 / 500 / 404 故障。
本文件承担全部协议细节，业务层只看到内部 DTO 与 `Transient/PermanentGatewayError`，
**永远不接触 HTTP 状态码与厂商字段名**。

错误映射（企业化设计 §4.6）：

| 外部返回 | 映射 | 分类 |
| --- | --- | --- |
| `401` / `403` | `PermanentGatewayError(AUTH_FAILED)` | 确定性 |
| `404` | `PermanentGatewayError(调用方指定)` | 确定性 |
| `409` | `PermanentGatewayError(IDEMPOTENCY_CONFLICT)` | 确定性 |
| `5xx` | `TransientGatewayError(APPROVAL_API_ERROR)` | 瞬时 |
| `504` / `408` | `TransientGatewayError(APPROVAL_API_TIMEOUT)` | 瞬时 |
| 连不上 / 读超时 | `TransientGatewayError(APPROVAL_UNREACHABLE / ..._TIMEOUT)` | 瞬时 |
| 非合法 JSON / 缺字段 | `PermanentGatewayError(INVALID_GATEWAY_RESPONSE)` | 确定性 |

**`404` 的错误码由调用方指定**：查审批单是 `INSTANCE_NOT_FOUND`，下载附件是
`ATTACHMENT_MISSING` —— 差别必须保留，否则排不开"单号错了"和"附件被删了"。
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.config import settings
from app.enums import (
    BusinessRole,
    ContractLabel,
    ContractType,
    ErrorCode,
    StrEnum,
    WriteStatus,
)
from app.errors import PermanentGatewayError, TransientGatewayError
from app.ports.approval_gateway import (
    ApprovalDetailDTO,
    AttachmentDTO,
    AuthoritativeContextDTO,
    DownloadedAttachmentDTO,
    PendingApprovalDTO,
    WriteCommentResultDTO,
)

#: 从 Content-Disposition 里取文件名，兼容 filename 与 filename*（RFC 5987）
_FILENAME_PATTERN = re.compile(
    r"""filename\*?=(?:UTF-8'')?"?([^";]+)"?""", re.IGNORECASE
)


class MockApprovalGateway:
    """实现 `ApprovalReadGateway` 与 `ApprovalCommentGateway`。

    两个能力由同一个类提供是**当前实现的巧合**，不是端口的假设：
    只读的审批系统实现只需实现读接口那三个方法。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        provider: str | None = None,
        tenant_id: str | None = None,
        timeout: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """缺省参数取 `app.config.settings`。

        `provider` / `tenant_id` 由适配器按**自身身份**注入 DTO ——
        它们是本系统侧的归属信息，不是外部系统返回的字段。
        `client` 允许注入，便于测试用 `MockTransport` 验证错误映射而无需起真实服务。
        """
        self._base_url = (base_url or settings.mock_approval_base_url).rstrip("/")
        self._token = token if token is not None else settings.mock_approval_token
        self._provider = provider or settings.approval_provider
        self._tenant_id = tenant_id or settings.tenant_id
        self._timeout = (
            timeout if timeout is not None else settings.gateway_timeout_seconds
        )

        # 复用连接池。鉴权头**不在这里挂**而是在 `_request` 里逐次带上：
        # 外部注入客户端（如测试的 MockTransport）时，鉴权仍是适配器自己的契约。
        self._client = client or httpx.Client(
            base_url=self._base_url, timeout=self._timeout
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        """释放连接池。进程退出或适配器不再使用时调用。"""
        self._client.close()

    def __enter__(self) -> MockApprovalGateway:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    # ------------------------------------------------------------------
    # 读取能力
    # ------------------------------------------------------------------
    def list_pending(self, limit: int) -> list[PendingApprovalDTO]:
        """拉取待处理审批单列表。"""
        response = self._request("GET", "/api/instances/pending", params={"limit": limit})
        self._raise_for_status(response, not_found_code=ErrorCode.INSTANCE_NOT_FOUND)

        payload = self._parse_json(response)
        items = payload.get("items")
        if not isinstance(items, list):
            raise PermanentGatewayError(
                "待办响应缺少 items 数组，厂商响应结构与预期不符",
                code=ErrorCode.INVALID_GATEWAY_RESPONSE,
            )

        return [self._to_pending(item) for item in items]

    def get_detail(self, instance_id: str) -> ApprovalDetailDTO:
        """查询审批单详情（含权威审查上下文与附件清单）。"""
        instance_key = _required_identifier(instance_id, field="instance_id")
        response = self._request("GET", f"/api/instances/{instance_key}")
        self._raise_for_status(response, not_found_code=ErrorCode.INSTANCE_NOT_FOUND)
        payload = self._parse_json(response)

        # 详情响应里的审批编号优先；缺失时退回调用方传入的实例号
        code_value = _required_identifier(
            payload.get("approval_code") or instance_key, field="approval_code"
        )

        raw_attachments = payload.get("attachments", [])
        if not isinstance(raw_attachments, list):
            raise PermanentGatewayError(
                f"附件清单应为数组，实际为 {type(raw_attachments).__name__}",
                code=ErrorCode.INVALID_GATEWAY_RESPONSE,
            )

        return ApprovalDetailDTO(
            instance_id=instance_key,
            approval_code=code_value,
            approval_title=(_optional_str(payload.get("approval_title")) or ""),
            applicant_name=(_optional_str(payload.get("applicant_name")) or ""),
            apply_time=(_optional_str(payload.get("apply_time")) or ""),
            context=AuthoritativeContextDTO(
                # 我方名称是自由文本，只需非空白
                our_party_name=_optional_str(payload.get("our_party_name")),
                # 这三个是受控取值：认不出来时归入 unknown，而不是让写库失败
                our_party_contract_label=_normalize_enum(
                    payload.get("our_party_contract_label"),
                    ContractLabel,
                    field="our_party_contract_label",
                ),
                our_party_business_role=_normalize_enum(
                    payload.get("our_party_business_role"),
                    BusinessRole,
                    field="our_party_business_role",
                ),
                contract_type=_normalize_enum(
                    payload.get("contract_type"),
                    ContractType,
                    field="contract_type",
                ),
            ),
            form_data=_optional_mapping(payload.get("form_data")),
            attachments=tuple(_to_attachment(item) for item in raw_attachments),
        )

    def download_attachment(
        self, instance_id: str, attachment_id: str
    ) -> DownloadedAttachmentDTO:
        """下载附件字节流。"""
        instance_key = _required_identifier(instance_id, field="instance_id")
        attachment_key = _required_identifier(attachment_id, field="attachment_id")

        response = self._request(
            "GET",
            f"/api/instances/{instance_key}/attachments/{attachment_key}/download",
        )
        self._raise_for_status(response, not_found_code=ErrorCode.ATTACHMENT_MISSING)

        return DownloadedAttachmentDTO(
            content=response.content,
            file_name=(
                self._filename_from_headers(response) or f"{attachment_key}.pdf"
            ),
            content_type=_normalize_content_type(
                response.headers.get("content-type", "")
            ),
        )

    # ------------------------------------------------------------------
    # 评论能力
    # ------------------------------------------------------------------
    def write_comment(
        self,
        instance_id: str,
        content: str,
        *,
        idempotency_key: str,
        operator_name: str | None = None,
    ) -> WriteCommentResultDTO:
        """写入审查意见（外部系统按幂等键去重）。"""
        instance_key = _required_identifier(instance_id, field="instance_id")
        # 幂等键为空串会让外部系统把它当成"同一个键"，从而把不同请求误判为重放
        idempotency_key = _required_identifier(idempotency_key, field="idempotency_key")

        response = self._request(
            "POST",
            f"/api/instances/{instance_key}/comments",
            json={
                "content": content,
                "idempotency_key": idempotency_key,
                "operator_name": operator_name,
            },
        )
        self._raise_for_status(response, not_found_code=ErrorCode.INSTANCE_NOT_FOUND)
        payload = self._parse_json(response)

        return WriteCommentResultDTO(
            write_status=WriteStatus.SUCCESS,
            external_comment_id=_optional_str(payload.get("comment_id")),
            # 外部系统识别出重放时会带上该标记；这属于**正常的幂等行为**，不是错误
            replayed=bool(payload.get("replayed", False)),
            response_text=_compact_json(payload),
        )

    def get_write_result(
        self, instance_id: str, idempotency_key: str
    ) -> WriteCommentResultDTO | None:
        """按幂等键查询写入结果。

        ⚠️ 超时**不等于**对方没写成功；直接重发可能产生重复评论，
        因此超时后必须先查（本方法），查不到才重发。

        mock 只提供"列出全部评论"，故在客户端侧按幂等键过滤；
        真实系统若有按键查询接口，换成直接查询即可，**业务层语义不变**。
        """
        instance_key = _required_identifier(instance_id, field="instance_id")
        idempotency_key = _required_identifier(idempotency_key, field="idempotency_key")

        response = self._request("GET", f"/api/instances/{instance_key}/comments")
        self._raise_for_status(response, not_found_code=ErrorCode.INSTANCE_NOT_FOUND)

        payload = self._parse_json(response)
        items = payload.get("items")
        if not isinstance(items, list):
            raise PermanentGatewayError(
                "评论列表响应缺少 items 数组，厂商响应结构与预期不符",
                code=ErrorCode.INVALID_GATEWAY_RESPONSE,
            )

        for item in items:
            if isinstance(item, dict) and item.get("idempotency_key") == idempotency_key:
                return WriteCommentResultDTO(
                    write_status=WriteStatus.SUCCESS,
                    external_comment_id=_optional_str(item.get("comment_id")),
                    # 本次是"查询"而不是"重放"，因此不为 replayed 打标；
                    # 查到即表示此前已写入成功，调用方据此避免重复写
                    replayed=False,
                    response_text=_compact_json(item),
                )
        return None

    # ------------------------------------------------------------------
    # 内部：请求与错误映射
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """发请求，把**传输层**异常转成端口异常。

        异常消息里**不含原始异常文本**（只留 URL 与超时值），原始异常通过
        `raise ... from exc` 挂在异常链上 —— 既不把请求头（含令牌）写进日志，
        也不损失可排障性。
        """
        headers = {
            "Authorization": f"Bearer {self._token}",
            **(kwargs.pop("headers", None) or {}),
        }

        try:
            return self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            # 必须放在 TransportError 之前：TimeoutException 是它的子类
            raise TransientGatewayError(
                f"调用审批系统超时（{self._timeout}s）：{method} {path}",
                code=ErrorCode.APPROVAL_API_TIMEOUT,
            ) from exc
        except httpx.TransportError as exc:
            raise TransientGatewayError(
                f"无法连接审批系统 {self._base_url}：{method} {path}",
                code=ErrorCode.APPROVAL_UNREACHABLE,
            ) from exc
        except httpx.HTTPError as exc:
            # 兜底**不变量**：适配器绝不能把裸的 httpx 异常抛给业务层 ——
            # 那种异常没有 `.code`，调度器无法判断"重试还是立即阻塞"。
            # 按**瞬时**处理：重试有界、代价可控；判成永久失败会让任务过早 blocked。
            raise TransientGatewayError(
                f"调用审批系统失败：{method} {path}",
                code=ErrorCode.APPROVAL_API_ERROR,
            ) from exc

    def _raise_for_status(
        self, response: httpx.Response, *, not_found_code: ErrorCode
    ) -> None:
        """把 HTTP 状态码映射为端口异常（§4.6 映射表）。"""
        status = response.status_code
        if status < 400:
            return

        detail = _extract_detail(response)

        if status in (401, 403):
            raise PermanentGatewayError(
                f"审批系统鉴权失败（HTTP {status}）：{detail}",
                code=ErrorCode.AUTH_FAILED,
            )
        if status == 404:
            raise PermanentGatewayError(detail, code=not_found_code)
        if status == 409:
            raise PermanentGatewayError(
                f"幂等键冲突：{detail}", code=ErrorCode.IDEMPOTENCY_CONFLICT
            )
        if status == 429:
            # ⚠️ 429 是**限流**，不是"请求有问题"（§7.3 归为瞬时错误）。
            # 若落进下面的"其他 4xx 一律确定性"兜底分支，对方一限流我们就把任务打成永久失败。
            retry_after = response.headers.get("retry-after")
            hint = f"，对方建议等待 {retry_after}s" if retry_after else ""
            raise TransientGatewayError(
                f"审批系统限流（HTTP 429）{hint}：{detail}",
                code=ErrorCode.APPROVAL_RATE_LIMITED,
            )
        if status in (408, 504):
            raise TransientGatewayError(
                f"审批系统响应超时（HTTP {status}）：{detail}",
                code=ErrorCode.APPROVAL_API_TIMEOUT,
            )
        if status >= 500:
            raise TransientGatewayError(
                f"审批系统服务端错误（HTTP {status}）：{detail}",
                code=ErrorCode.APPROVAL_API_ERROR,
            )

        # 4xx 中未列举的状态码：重试不会改变结果，按确定性错误处理
        raise PermanentGatewayError(
            f"审批系统返回未预期的状态码 {status}：{detail}",
            code=ErrorCode.INVALID_GATEWAY_RESPONSE,
        )

    def _parse_json(self, response: httpx.Response) -> dict[str, Any]:
        """解析 JSON 响应体，非对象结构一律视为契约不符。"""
        try:
            payload = response.json()
        except ValueError as exc:
            raise PermanentGatewayError(
                "审批系统返回的不是合法 JSON，响应结构与预期不符",
                code=ErrorCode.INVALID_GATEWAY_RESPONSE,
            ) from exc

        if not isinstance(payload, dict):
            raise PermanentGatewayError(
                f"审批系统返回的 JSON 顶层应为对象，实际为 {type(payload).__name__}",
                code=ErrorCode.INVALID_GATEWAY_RESPONSE,
            )
        return payload

    def _to_pending(self, raw: Any) -> PendingApprovalDTO:
        """把待办项映射成内部 DTO。

        `approval_code` 必须在这里校验，不能等到写库：`None` 被 `str()` 成 `"None"`
        会顺利通过数据库的非空约束，变成一个**看起来完全合法**的假审批任务。
        """
        if not isinstance(raw, dict):
            raise PermanentGatewayError(
                f"待办项应为对象，实际为 {type(raw).__name__}",
                code=ErrorCode.INVALID_GATEWAY_RESPONSE,
            )

        # mock 的"实例号"就是审批编号；真实系统里两者常不同（流程实例 vs 业务单号），
        # 那时只改这一处映射即可 —— 这正是"字段映射收在适配器里"的价值。
        code_value = _required_identifier(raw.get("approval_code"), field="approval_code")

        return PendingApprovalDTO(
            provider=self._provider,
            tenant_id=self._tenant_id,
            instance_id=code_value,
            approval_code=code_value,
            approval_title=(_optional_str(raw.get("approval_title")) or ""),
            applicant_name=(_optional_str(raw.get("applicant_name")) or ""),
            apply_time=(_optional_str(raw.get("apply_time")) or ""),
            attachment_count=_non_negative_int(
                raw.get("attachment_count"), field="attachment_count"
            ),
        )

    @staticmethod
    def _filename_from_headers(response: httpx.Response) -> str | None:
        """从 `Content-Disposition` 提取文件名。

        报文头里的文件名比 URL 里的附件编号更贴近"人看到的文件"，
        因此优先用它；取不到时才退回 `{attachment_id}.pdf`。
        """
        disposition = response.headers.get("content-disposition", "")
        match = _FILENAME_PATTERN.search(disposition)
        return match.group(1).strip() if match else None


# ============================================================
# 模块级小工具
# ============================================================


def _optional_str(value: Any) -> str | None:
    """转成字符串；空值与纯空白一律返回 None。

    统一在这里把 `""` 归成 `None`：后面判断"权威上下文是否完整"时，
    空串必须与缺失等价，否则会判成 `complete` 而实际什么都没拿到。
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_identifier(value: Any, *, field: str) -> str:
    """必填业务标识：必须是**非空白**的字符串或整数。

    ## 为什么不能直接 `str()`

    对外部值无脑 `str()` 会制造三类假数据：

    | 外部值 | `str()` 结果 | 后果 |
    | --- | --- | --- |
    | `None` | `"None"` | **看起来完全合法**的字符串，顺利通过数据库的非空约束，创建出一个假的审批任务 |
    | `"   "` | `"   "` | 会被数据库 CHECK 拦住，但错误推迟到写库时才暴露，报错信息也从"外部系统返回了空的审批编号"退化成一句约束冲突 |
    | `123` | `"123"` | 这是合理的（编号可能就是数字），因此允许 int |

    第一行最危险：`"None"` 不违反任何约束，唯一的线索是一串英文引号，
    排查时几乎不可能被注意到。校验必须放在**厂商边界**，
    而不是等到数据已经进了库再靠约束兜。

    Raises:
        PermanentGatewayError: 值为空、为布尔、类型不符或去掉空白后为空。
    """
    if value is None:
        raise PermanentGatewayError(
            f"外部系统未提供 {field}（返回空值）",
            code=ErrorCode.INVALID_GATEWAY_RESPONSE,
        )
    # bool 是 int 的子类：True 会被 str() 成 "True"，同样属于假数据
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PermanentGatewayError(
            f"{field} 类型不符：期望字符串或整数，实际为 {type(value).__name__}",
            code=ErrorCode.INVALID_GATEWAY_RESPONSE,
        )

    text = str(value).strip()
    if not text:
        raise PermanentGatewayError(
            f"{field} 为空白字符串", code=ErrorCode.INVALID_GATEWAY_RESPONSE
        )
    return text


def _non_negative_int(value: Any, *, field: str, default: int = 0) -> int:
    """非负整数。

    缺省（`None`）时返回 `default` —— 字段没给不等于给了错值；
    但给了非法值必须报错：`-1` 个附件、`"两个"` 个附件都会让后续逻辑失真，
    而数据库对此没有约束。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise PermanentGatewayError(
            f"{field} 类型不符：期望整数，实际为布尔值",
            code=ErrorCode.INVALID_GATEWAY_RESPONSE,
        )
    if isinstance(value, float):
        # ⚠️ `int(1.9)` 得到 `1`，**静默截断**。
        # 截断出来的数量看起来完全正常，于是系统拿着一个错误的值继续跑，
        # 没有任何报错。类型漂移必须当成契约错误暴露出来。
        if not value.is_integer():
            raise PermanentGatewayError(
                f"{field} 期望整数，实际为带小数的浮点数 {value!r}："
                f"静默截断会产生看起来正常但实际错误的值",
                code=ErrorCode.INVALID_GATEWAY_RESPONSE,
            )
        # `1.0` 这种整值浮点可以接受：JSON 里 1 与 1.0 在部分语言中不加区分
        value = int(value)

    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PermanentGatewayError(
            f"{field} 不是合法整数：{value!r}",
            code=ErrorCode.INVALID_GATEWAY_RESPONSE,
        ) from exc

    if parsed < 0:
        raise PermanentGatewayError(
            f"{field} 不能为负数：{parsed}",
            code=ErrorCode.INVALID_GATEWAY_RESPONSE,
        )
    return parsed


#: 可识别的布尔文本取值（小写）
_TRUE_TOKENS = frozenset({"true", "1", "yes", "y", "t"})
_FALSE_TOKENS = frozenset({"false", "0", "no", "n", "f"})


def _boolean(value: Any, *, field: str, default: bool) -> bool:
    """严格解析布尔值，**不接受无法识别的取值**。

    ⚠️ 不能用 `bool(value)`：它只判断"是否为空"，于是 `"false"` 与 `"0"` 都是 `True`。
    对 `available` 这种**决定"能不能下载"**的字段，语义颠倒是实打实的错 ——
    会去下载一个已标记不可用的附件，失败后再把原因归成"审批系统删了附件"，
    而真正的问题是字段类型漂移，归因完全错位。

    认不出来时**报错而不是猜**：类型漂移需要被看见，不该被"合理默认值"掩盖。

    Raises:
        PermanentGatewayError: 取值不是布尔、不是 0/1、也不是可识别的布尔文本。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise PermanentGatewayError(
            f"{field} 期望布尔值，实际为整数 {value}（只接受 0 或 1）",
            code=ErrorCode.INVALID_GATEWAY_RESPONSE,
        )
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False

    raise PermanentGatewayError(
        f"{field} 不是可识别的布尔值：{value!r}",
        code=ErrorCode.INVALID_GATEWAY_RESPONSE,
    )


def _normalize_enum(
    value: Any, enum_class: type[StrEnum], *, field: str
) -> str | None:
    """把外部返回的枚举值归一化到受控取值域。

    两种"对不上"**分开处理**：**未提供**（`None` / 空串）→ `None`，语义是"缺失"；
    **提供了但没认出来** → `unknown`，语义是"给了但我不认识"。

    第二种**刻意不报错**：真实系统可能随时新增一种合同类型（如把 `procurement`
    拆成 `direct_procurement`），硬失败会让对方改一次配置就打断整条拉取链路。
    映射为 `unknown` 是安全降级 —— 依赖它的规则走 `needs_review`，
    而不是拿一个错误的方向去下结论。

    返回值恒为合法枚举值，因此不会触发数据库 CHECK 拒绝。
    """
    text = _optional_str(value)
    if text is None:
        return None

    lowered = text.lower()
    for member in enum_class:
        if member.value == lowered:
            return member.value

    # 认不出来但确实给了值 → unknown（而不是 None）
    return str(enum_class.UNKNOWN.value)


def _optional_mapping(value: Any) -> dict[str, Any]:
    """确保拿到的是字典；不是字典时返回空字典而不是抛错。

    `form_data` 只是透传的展示数据，缺失不该让整个详情同步失败。
    """
    return value if isinstance(value, dict) else {}


def _to_attachment(raw: Any) -> AttachmentDTO:
    """把附件项映射成内部 DTO。

    `attachment_id` 是身份标识，必须严格校验；
    `file_name` 只是展示元数据，缺失时按编号派生 ——
    数据库要求它非空，派生比让整个详情同步失败更合适。
    """
    if not isinstance(raw, dict):
        raise PermanentGatewayError(
            f"附件项应为对象，实际为 {type(raw).__name__}",
            code=ErrorCode.INVALID_GATEWAY_RESPONSE,
        )

    attachment_id = _required_identifier(
        raw.get("attachment_id"), field="attachment_id"
    )

    return AttachmentDTO(
        attachment_id=attachment_id,
        file_name=_optional_str(raw.get("file_name")) or f"{attachment_id}.pdf",
        file_type=(_optional_str(raw.get("file_type")) or "").lower(),
        # 缺省按 True 处理：字段缺失时"能不能下载"应由下载结果说话，
        # 而不是被这里猜成 False。
        # ⚠️ 但**已给出**的取值必须严格解析：`bool("false")` 是 True，
        #    会把"不可用"读成"可用"，然后错误地归因到审批系统头上。
        available=_boolean(raw.get("available"), field="available", default=True),
    )


def _normalize_content_type(raw: str) -> str:
    """归一 Content-Type：去参数、转小写。

    `Application/PDF; charset=binary` 与 `application/pdf` 必须得到同一结果，
    否则 M4 的解析路由会因为报文头的大小写差异走错分支。
    """
    return raw.split(";")[0].strip().lower() or "application/octet-stream"


def _compact_json(payload: Any) -> str:
    """紧凑 JSON 字符串，用于落库留痕。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _extract_detail(response: httpx.Response) -> str:
    """尽力从错误响应里取出可读说明，取不到时退回状态码文本。"""
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])
    return f"HTTP {response.status_code}"
