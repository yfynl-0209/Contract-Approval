"""Mock 审批系统适配器测试（M3 / T3）。

用 `httpx.MockTransport` 在不启动真实服务的前提下，把
**错误映射表逐行钉住**——这是本文件的核心价值：

业务层永远不该看到 HTTP 状态码。若 500 被映射成确定性错误，
一次审批系统抖动就会把任务打成永久 `blocked`；若 404 被映射成瞬时错误，
一个不存在的附件就会被反复重试。两种错都不容易被业务代码发现，
只能在适配器这一层测。
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app.adapters.approval.mock_approval_gateway import MockApprovalGateway
from app.enums import ErrorCode, WriteStatus
from app.errors import AppError, PermanentGatewayError, TransientGatewayError
from app.ports.approval_gateway import ApprovalGateway, ApprovalReadGateway

Handler = Callable[[httpx.Request], httpx.Response]


def _gateway(
    handler: Handler,
    *,
    token: str = "demo-token",
    provider: str = "mock",
    tenant_id: str = "default",
) -> MockApprovalGateway:
    """构造一个走 MockTransport 的适配器，不启动任何服务。"""
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://mock.local",
        timeout=1.0,
    )
    return MockApprovalGateway(
        base_url="http://mock.local",
        token=token,
        provider=provider,
        tenant_id=tenant_id,
        timeout=1.0,
        client=client,
    )


def _status_handler(status: int) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": f"模拟 HTTP {status}"})

    return handler


# ============================================================
# 1. HTTP 状态码 → 端口异常（错误映射表逐行验证）
# ============================================================


@pytest.mark.parametrize(
    ("status", "expected_exception", "expected_code"),
    [
        (401, PermanentGatewayError, ErrorCode.AUTH_FAILED),
        (403, PermanentGatewayError, ErrorCode.AUTH_FAILED),
        (409, PermanentGatewayError, ErrorCode.IDEMPOTENCY_CONFLICT),
        (408, TransientGatewayError, ErrorCode.APPROVAL_API_TIMEOUT),
        # 429 是 4xx，但语义是"请稍后再来"——设计文档 §7.3 明确把限流列为瞬时错误。
        # 若落进"其他 4xx 一律确定性"的兜底，对方一限流我们就把任务打成永久失败。
        (429, TransientGatewayError, ErrorCode.APPROVAL_RATE_LIMITED),
        (500, TransientGatewayError, ErrorCode.APPROVAL_API_ERROR),
        (502, TransientGatewayError, ErrorCode.APPROVAL_API_ERROR),
        (503, TransientGatewayError, ErrorCode.APPROVAL_API_ERROR),
        (504, TransientGatewayError, ErrorCode.APPROVAL_API_TIMEOUT),
        # 4xx 中未列举的状态码：重试不会改变结果 → 确定性
        (418, PermanentGatewayError, ErrorCode.INVALID_GATEWAY_RESPONSE),
    ],
    ids=[
        "401鉴权失败",
        "403鉴权失败",
        "409幂等冲突",
        "408超时",
        "429限流",
        "500服务端错误",
        "502服务端错误",
        "503服务端错误",
        "504超时",
        "未预期4xx",
    ],
)
def test_http_status_maps_to_port_exception(
    status: int,
    expected_exception: type[Exception],
    expected_code: ErrorCode,
) -> None:
    """状态码必须映射成正确的异常类型与错误码。

    分类错误的两个方向都要付出代价：
    5xx 判成确定性 → 抖动即永久失败；404 判成瞬时 → 无意义重试。
    """
    gateway = _gateway(_status_handler(status))

    with pytest.raises(expected_exception) as excinfo:
        gateway.list_pending(limit=1)

    assert excinfo.value.code == expected_code


def test_rate_limit_is_transient_and_mentions_retry_after() -> None:
    """429 必须是**可重试**的瞬时错误，并把对方的 `Retry-After` 带上。

    这是 4xx 里唯一的例外，也最容易在"4xx 一律确定性"的兜底里被漏掉。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"detail": "请求过于频繁"}, headers={"retry-after": "30"}
        )

    gateway = _gateway(handler)

    with pytest.raises(TransientGatewayError) as excinfo:
        gateway.list_pending(limit=1)

    assert excinfo.value.code == ErrorCode.APPROVAL_RATE_LIMITED
    assert excinfo.value.retryable is True
    assert "30" in str(excinfo.value), "应把 Retry-After 一并带入消息，便于排障"


def test_every_error_status_raises_a_port_exception_with_code() -> None:
    """**不变量**：任何 4xx / 5xx 都必须转成带 `code` 的端口异常。

    T4 的调度器只能靠 `code` 判断"重试还是立即阻塞"。一旦某个状态码
    漏出裸异常或没有 `code`，分类责任就被丢回给调用方，
    而调用方根本没有足够信息去判断。

    刻意**逐个状态码**验证，而不是挑几个代表：429 就是这么被漏掉的 ——
    它落在兜底分支里，看起来"有处理"，实际分类是错的。
    这类漏洞只有穷举才能发现。
    """
    for status in range(400, 512):
        gateway = _gateway(_status_handler(status))

        with pytest.raises(AppError) as excinfo:
            gateway.list_pending(limit=1)

        assert isinstance(excinfo.value.code, ErrorCode), (
            f"HTTP {status} 没有映射成带 code 的端口异常"
        )


def test_no_raw_httpx_exception_escapes_the_adapter() -> None:
    """HTTP 层异常（非 TransportError 分支）也必须被兜住。

    `httpx.DecodingError`、`TooManyRedirects` 这类异常不属于 `TransportError`，
    若不显式兜底就会原样逃到业务层。这条测试守住"适配器不抛裸异常"这一点。
    """

    class _FakeHttpError(httpx.HTTPError):
        """模拟 httpx 层次里非 TransportError 的失败（版本无关）。"""

    for exception in (
        httpx.DecodingError("模拟响应解码失败"),
        _FakeHttpError("模拟其他 HTTP 层失败"),
    ):

        def handler(request: httpx.Request, _exc: Exception = exception) -> httpx.Response:
            raise _exc

        gateway = _gateway(handler)

        with pytest.raises(AppError) as excinfo:
            gateway.list_pending(limit=1)

        assert isinstance(excinfo.value.code, ErrorCode), (
            f"{type(exception).__name__} 没有被映射成带 code 的端口异常"
        )


def test_404_error_code_depends_on_endpoint() -> None:
    """同一个 404，在不同接口上必须给出**不同**的错误码。

    "审批单不存在"（单号写错）与"附件已被删除"（业务事实）
    排障动作完全不同，合并成一个码就再也分不清。
    """
    gateway = _gateway(_status_handler(404))

    with pytest.raises(PermanentGatewayError) as detail_error:
        gateway.get_detail("HT-2026-0001")
    assert detail_error.value.code == ErrorCode.INSTANCE_NOT_FOUND

    with pytest.raises(PermanentGatewayError) as attachment_error:
        gateway.download_attachment("HT-2026-0001", "A-1")
    assert attachment_error.value.code == ErrorCode.ATTACHMENT_MISSING


def test_read_timeout_maps_to_timeout_not_unreachable() -> None:
    """读超时必须映射成"超时"而不是"连不上"。

    `httpx.TimeoutException` 是 `TransportError` 的子类，
    捕获顺序写反就会全部落进"连不上"分支。这条测试专门钉住顺序。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("模拟读超时", request=request)

    gateway = _gateway(handler)

    with pytest.raises(TransientGatewayError) as excinfo:
        gateway.list_pending(limit=1)

    assert excinfo.value.code == ErrorCode.APPROVAL_API_TIMEOUT


def test_connect_error_maps_to_unreachable() -> None:
    """连不上 → `APPROVAL_UNREACHABLE`（仍是瞬时错误，会重试）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("模拟连接失败", request=request)

    gateway = _gateway(handler)

    with pytest.raises(TransientGatewayError) as excinfo:
        gateway.list_pending(limit=1)

    assert excinfo.value.code == ErrorCode.APPROVAL_UNREACHABLE
    assert excinfo.value.retryable is True


def test_exception_message_never_leaks_token() -> None:
    """异常消息不得包含令牌 —— 这些消息会进日志（§14 要求日志不泄露凭据）。

    做法：不在消息里拼接原始异常文本，原始异常通过 `raise ... from exc`
    挂在异常链上，排障时依然能看到。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("模拟连接失败", request=request)

    gateway = _gateway(handler, token="super-secret-token")

    with pytest.raises(TransientGatewayError) as excinfo:
        gateway.list_pending(limit=1)

    assert "super-secret-token" not in str(excinfo.value)


# ============================================================
# 2. 响应结构不符 → 确定性契约错误
# ============================================================


def test_non_json_body_is_contract_violation() -> None:
    """返回 HTML 错误页（网关常见行为）不能当成有效响应。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    gateway = _gateway(handler)

    with pytest.raises(PermanentGatewayError) as excinfo:
        gateway.list_pending(limit=1)

    assert excinfo.value.code == ErrorCode.INVALID_GATEWAY_RESPONSE


def test_json_array_at_top_level_is_contract_violation() -> None:
    """顶层是数组而非对象 → 契约不符。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    gateway = _gateway(handler)

    with pytest.raises(PermanentGatewayError) as excinfo:
        gateway.list_pending(limit=1)

    assert excinfo.value.code == ErrorCode.INVALID_GATEWAY_RESPONSE


def test_missing_items_array_is_contract_violation() -> None:
    """缺少 `items` 数组 → 契约不符，而不是当成"没有待办"。

    静默当成空列表会让"接口返回结构变了"这件事完全不可见。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total": 0})

    gateway = _gateway(handler)

    with pytest.raises(PermanentGatewayError) as excinfo:
        gateway.list_pending(limit=1)

    assert excinfo.value.code == ErrorCode.INVALID_GATEWAY_RESPONSE


def test_pending_item_missing_required_field_is_contract_violation() -> None:
    """待办项缺少 `approval_code`（去重键的来源）→ 契约不符。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"approval_title": "缺编号"}]})

    gateway = _gateway(handler)

    with pytest.raises(PermanentGatewayError) as excinfo:
        gateway.list_pending(limit=1)

    assert excinfo.value.code == ErrorCode.INVALID_GATEWAY_RESPONSE


# ============================================================
# 3. 字段映射
# ============================================================


def test_pending_maps_identity_from_adapter_config() -> None:
    """`provider` / `tenant_id` 由适配器按**自身身份**注入。

    它们是本系统侧的归属信息，不是外部系统返回的字段。
    若指望外部系统给，接入第二个企业时就会发现根本拿不到。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total": 1,
                "items": [
                    {
                        "approval_code": "HT-2026-0001",
                        "approval_title": "设备采购合同",
                        "applicant_name": "张三",
                        "apply_time": "2026-09-01 09:00:00",
                        "attachment_count": 2,
                    }
                ],
            },
        )

    gateway = _gateway(handler, provider="mock", tenant_id="tenant-a")
    items = gateway.list_pending(limit=10)

    assert len(items) == 1
    item = items[0]
    assert item.approval_code == "HT-2026-0001"
    assert item.approval_title == "设备采购合同"
    assert item.attachment_count == 2
    assert item.provider == "mock"
    assert item.tenant_id == "tenant-a"
    # 待办接口拿不到权威上下文，因此 DTO 里根本没有这几个字段 ——
    # 这从结构上保证了"刚拉取完的任务 context_status 必然是 missing"
    assert not hasattr(item, "our_party_name")


def test_detail_normalizes_blank_context_fields_to_none() -> None:
    """空串与纯空白必须归一为 `None`。

    否则"权威上下文是否完整"会被判成 `complete`，而实际什么都没拿到 ——
    方向敏感的规则会拿着空立场去判断，结论全线错位。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "approval_code": "HT-1",
                "approval_title": "服务合同",
                "applicant_name": "李四",
                "apply_time": "2026-09-02",
                "our_party_name": "某某科技有限公司",
                "our_party_contract_label": "party_b",
                "our_party_business_role": "",
                "contract_type": "   ",
                "form_data": {"amount": "100"},
                "attachments": [
                    {"attachment_id": "A-1", "file_name": "c.pdf", "file_type": "pdf"}
                ],
            },
        )

    gateway = _gateway(handler)
    detail = gateway.get_detail("HT-1")

    assert detail.context.our_party_name == "某某科技有限公司"
    assert detail.context.our_party_contract_label == "party_b"
    assert detail.context.our_party_business_role is None
    assert detail.context.contract_type is None
    assert detail.form_data == {"amount": "100"}
    assert len(detail.attachments) == 1
    # 字段缺失时按"可下载"处理：能不能下载应由下载结果说话，不该在这里猜
    assert detail.attachments[0].available is True


def test_detail_form_data_not_a_mapping_falls_back_to_empty() -> None:
    """`form_data` 只是透传的展示数据，结构异常不该让整个详情同步失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "approval_code": "HT-2",
                "form_data": "这不是对象",
                "attachments": [],
            },
        )

    gateway = _gateway(handler)
    detail = gateway.get_detail("HT-2")

    assert detail.form_data == {}


def test_download_reads_filename_and_normalizes_content_type() -> None:
    """文件名优先取报文头；`Content-Type` 必须去掉参数并转小写。

    `Application/PDF; charset=binary` 与 `application/pdf` 必须得到同一结果，
    否则 M4 的解析路由会因为报文头大小写差异走错分支。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"%PDF-1.4 fake",
            headers={
                "content-type": "Application/PDF; charset=binary",
                "content-disposition": 'attachment; filename="contract_scan.pdf"',
            },
        )

    gateway = _gateway(handler)
    downloaded = gateway.download_attachment("HT-1", "A-1")

    assert downloaded.content == b"%PDF-1.4 fake"
    assert downloaded.file_name == "contract_scan.pdf"
    assert downloaded.content_type == "application/pdf"


def test_download_falls_back_to_attachment_id_for_filename() -> None:
    """报文头没有文件名时退回 `{attachment_id}.pdf`，而不是空字符串。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"x", headers={"content-type": "application/pdf"}
        )

    gateway = _gateway(handler)
    downloaded = gateway.download_attachment("HT-1", "A-9")

    assert downloaded.file_name == "A-9.pdf"


# ============================================================
# 4. 评论能力
# ============================================================


def test_write_comment_sends_idempotency_key_and_bearer_token() -> None:
    """幂等键必须真的出现在请求体里，鉴权头必须带上。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"comment_id": "C-000001"})

    gateway = _gateway(handler, token="secret-token")
    gateway.write_comment("HT-1", "审查意见", idempotency_key="K-1", operator_name="审批人")

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["idempotency_key"] == "K-1"
    assert body["content"] == "审查意见"
    assert body["operator_name"] == "审批人"
    assert captured["authorization"] == "Bearer secret-token"


def test_write_comment_maps_replayed_flag() -> None:
    """`replayed=True` 是**正常的幂等表现**，不是错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"comment_id": "C-000001", "replayed": True, "idempotency_key": "K-1"},
        )

    gateway = _gateway(handler)
    result = gateway.write_comment("HT-1", "意见", idempotency_key="K-1")

    assert result.write_status is WriteStatus.SUCCESS
    assert result.external_comment_id == "C-000001"
    assert result.replayed is True


def test_get_write_result_finds_record_by_idempotency_key() -> None:
    """超时后"先查再决定重发"依赖这个能力。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "count": 2,
                "items": [
                    {"comment_id": "C-1", "idempotency_key": "OTHER"},
                    {"comment_id": "C-2", "idempotency_key": "K-TARGET"},
                ],
            },
        )

    gateway = _gateway(handler)
    found = gateway.get_write_result("HT-1", "K-TARGET")

    assert found is not None
    assert found.external_comment_id == "C-2"
    # 本次是"查询"不是"重放"，不应打上 replayed 标记
    assert found.replayed is False


def test_get_write_result_returns_none_when_absent() -> None:
    """查不到 → `None`（调用方据此判断"确实没写进去，可以安全重发"）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"count": 0, "items": []})

    gateway = _gateway(handler)

    assert gateway.get_write_result("HT-1", "K-MISSING") is None


# ============================================================
# 5. 端口满足关系
# ============================================================


def test_mock_gateway_satisfies_both_ports() -> None:
    """Mock 适配器同时具备读取与评论能力。"""
    gateway = _gateway(_status_handler(200))

    assert isinstance(gateway, ApprovalReadGateway)
    assert isinstance(gateway, ApprovalGateway)


def test_adapter_is_usable_as_context_manager() -> None:
    """适配器支持 `with` 语句，保证连接池被释放。"""
    with _gateway(_status_handler(200)) as gateway:
        assert gateway.provider == "mock"


# ============================================================
# 6. 厂商边界：非法业务标识必须被拒绝
# ============================================================


@pytest.mark.parametrize(
    "approval_code",
    [None, "   ", "", True, {"id": 1}, ["HT-1"]],
    ids=["None", "空白串", "空串", "布尔值", "对象", "数组"],
)
def test_invalid_approval_code_is_rejected_at_provider_boundary(
    approval_code: object,
) -> None:
    """非法审批编号必须在**适配器**就拒绝，不能等到写库。

    `None` 是最危险的一个：`str(None)` 得到 `"None"` ——
    一个**看起来完全合法**的字符串。它能顺利通过数据库的非空约束，
    变成一个假的审批任务，而唯一线索是一串英文引号，
    排查时几乎不可能被注意到。

    空白串虽然会被数据库 CHECK 拦住，但错误会推迟到写库时才暴露，
    报错信息也从"外部系统返回了空的审批编号"退化成一句约束冲突。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"items": [{"approval_code": approval_code}]}
        )

    gateway = _gateway(handler)

    with pytest.raises(PermanentGatewayError) as excinfo:
        gateway.list_pending(limit=1)

    assert excinfo.value.code == ErrorCode.INVALID_GATEWAY_RESPONSE


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (3, 3),
        ("2", 2),
        (1.0, 1),  # 整值浮点：JSON 里 1 与 1.0 在部分语言中不加区分
        (None, 0),  # 未提供 → 用默认值，不是错误
    ],
    ids=["零", "正常", "数字字符串", "整值浮点", "字段缺省"],
)
def test_attachment_count_accepts_valid_values(
    value: object, expected: int
) -> None:
    """非负整数（含数字字符串与整值浮点）应被接受。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"items": [{"approval_code": "HT-1", "attachment_count": value}]}
        )

    items = _gateway(handler).list_pending(limit=1)

    assert items[0].attachment_count == expected


@pytest.mark.parametrize(
    "value",
    [-1, "两个", True, "3.5", 1.9, 0.5],
    ids=["负数", "非数字", "布尔值", "字符串小数", "浮点1.9", "浮点0.5"],
)
def test_invalid_attachment_count_is_rejected(value: object) -> None:
    """附件数量非法必须报错 —— 数据库对这个字段没有约束，只能在边界拦。

    其中**浮点数**最隐蔽：`int(1.9)` 得到 `1`，一个看起来完全正常的数量。
    静默截断之后系统拿着错值继续跑，全程没有任何报错。
    """
    if isinstance(value, float) and value.is_integer():
        pytest.skip("整值浮点属于可接受取值，由另一条用例覆盖")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"items": [{"approval_code": "HT-1", "attachment_count": value}]}
        )

    with pytest.raises(PermanentGatewayError) as excinfo:
        _gateway(handler).list_pending(limit=1)

    assert excinfo.value.code == ErrorCode.INVALID_GATEWAY_RESPONSE


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("false", False),
        ("TRUE", True),
        ("False", False),
        ("1", True),
        ("0", False),
        (None, True),  # 未提供 → 按"可下载"处理，让下载结果说话
    ],
    ids=[
        "真", "假", "整数1", "整数0",
        "文本true", "文本false", "大写TRUE", "首字母大写False",
        "文本1", "文本0", "字段缺省",
    ],
)
def test_available_flag_parses_recognized_booleans(
    value: object, expected: bool
) -> None:
    """`available` 必须严格解析。

    ⚠️ 这正是不该用 `bool(value)` 的地方：

    | 外部值 | `bool()` | 正确结果 |
    | --- | --- | --- |
    | `"false"` | `True` ❌ | `False` |
    | `"0"` | `True` ❌ | `False` |

    语义颠倒的后果很具体：系统会去下载一个已被标记为不可用的附件，
    失败后再把原因归成"审批系统删了附件"—— 而真正的问题是字段类型漂移。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "approval_code": "HT-1",
                "attachments": [{"attachment_id": "A-1", "available": value}],
            },
        )

    detail = _gateway(handler).get_detail("HT-1")

    assert detail.attachments[0].available is expected


@pytest.mark.parametrize(
    "value",
    ["maybe", "是的", 2, -1, 1.5, [], {}],
    ids=["无法识别文本", "中文是", "整数2", "负数", "浮点", "数组", "对象"],
)
def test_unrecognized_available_flag_is_rejected(value: object) -> None:
    """认不出来时**报错而不是猜**。

    猜错的代价是把"不可用"读成"可用"，于是系统去下载一个注定失败的附件，
    再把失败归因到审批系统头上 —— 归因完全错位。
    类型漂移需要被看见，而不是被一个"合理默认值"掩盖过去。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "approval_code": "HT-1",
                "attachments": [{"attachment_id": "A-1", "available": value}],
            },
        )

    with pytest.raises(PermanentGatewayError) as excinfo:
        _gateway(handler).get_detail("HT-1")

    assert excinfo.value.code == ErrorCode.INVALID_GATEWAY_RESPONSE


def test_unrecognized_context_enum_maps_to_unknown_not_failure() -> None:
    """认不出的受控取值 → `unknown`，而不是报错。

    真实审批系统随时可能新增一种合同类型（例如把 `procurement` 拆成
    `direct_procurement`）。若此处硬失败，对方改一次配置就能把我们
    **整条拉取链路打断**。映射为 `unknown` 是安全降级：
    依赖它的规则会走 `needs_review`，而不是拿着错误方向去下结论。

    注意返回值**不是 None** —— `None` 表示"对方没给"，
    `unknown` 表示"给了但我不认识"，两者语义不同。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "approval_code": "HT-1",
                "contract_type": "direct_procurement",
                # 大小写不同应视为认得出
                "our_party_business_role": "Buyer",
                "our_party_contract_label": "Party_A",
                "attachments": [],
            },
        )

    detail = _gateway(handler).get_detail("HT-1")

    assert detail.context.contract_type == "unknown"
    assert detail.context.our_party_business_role == "buyer"
    assert detail.context.our_party_contract_label == "party_a"


def test_missing_context_enum_stays_none() -> None:
    """对方**没给** → `None`（缺失），与"给了但认不出"（`unknown`）区分开。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"approval_code": "HT-1", "attachments": []})

    detail = _gateway(handler).get_detail("HT-1")

    assert detail.context.contract_type is None
    assert detail.context.our_party_business_role is None


def test_attachment_without_file_name_derives_one_from_id() -> None:
    """文件名缺失 → 按附件编号派生。

    数据库要求 `file_name` 非空，派生比让整个详情同步失败更合适；
    它只是展示元数据，不参与任何判断。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"approval_code": "HT-1", "attachments": [{"attachment_id": "A-7"}]},
        )

    detail = _gateway(handler).get_detail("HT-1")

    assert detail.attachments[0].attachment_id == "A-7"
    assert detail.attachments[0].file_name == "A-7.pdf"


def test_attachment_without_id_is_rejected() -> None:
    """附件编号是**身份标识**，缺失必须报错（不同于展示用的文件名）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"approval_code": "HT-1", "attachments": [{"file_name": "c.pdf"}]},
        )

    with pytest.raises(PermanentGatewayError) as excinfo:
        _gateway(handler).get_detail("HT-1")

    assert excinfo.value.code == ErrorCode.INVALID_GATEWAY_RESPONSE


def test_attachments_not_an_array_is_contract_violation() -> None:
    """附件清单不是数组 → 契约不符，而不是当成"没有附件"。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"approval_code": "HT-1", "attachments": {"a": 1}}
        )

    with pytest.raises(PermanentGatewayError) as excinfo:
        _gateway(handler).get_detail("HT-1")

    assert excinfo.value.code == ErrorCode.INVALID_GATEWAY_RESPONSE


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_detail", ("   ",)),
        ("download_attachment", ("HT-1", "  ")),
        ("write_comment", ("  ", "审查意见")),
        ("get_write_result", ("  ", "K-1")),
    ],
    ids=["详情_实例号", "下载_附件号", "回写_实例号", "查询_实例号"],
)
def test_blank_path_identifier_is_rejected_before_any_request(
    method: str, args: tuple
) -> None:
    """空白标识必须在**发请求之前**就被拒绝。

    否则会拼出 `/api/instances//comments` 这类路径，外部系统返回 404，
    我们却报"审批单不存在"—— 归因完全错误，排障会被带到完全错误的方向。
    """
    requests_made = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        requests_made["count"] += 1
        return httpx.Response(200, json={})

    gateway = _gateway(handler)
    kwargs = {"idempotency_key": "K-1"} if method == "write_comment" else {}

    with pytest.raises(PermanentGatewayError) as excinfo:
        getattr(gateway, method)(*args, **kwargs)

    assert excinfo.value.code == ErrorCode.INVALID_GATEWAY_RESPONSE
    assert requests_made["count"] == 0, "空白标识不应发出任何 HTTP 请求"
