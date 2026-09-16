"""端口异常 → HTTP 响应的翻译层。

业务层只抛**带稳定错误码的异常**，各协议层各自翻译（REST 在这里，MCP 在 `mcp_server.py`）——
否则业务层返回 `503`，MCP 形态还得把它翻译回异常，两种协议的错误语义迟早分叉。

状态码问的是 **"这次调用本身完成了吗"**，而不是"业务上顺利吗"：

| 情况 | 状态码 |
| --- | --- |
| **业务事实**（附件不存在 / 空 / 超限 / 类型不符） | **200** |
| 目标不存在 | 404 |
| 与当前状态冲突 | 409 |
| 参数非法 | 400 |
| **本次请求没有可信身份**（没带 / 无效 / 过期） | **401** |
| **身份可信但缺少权限** | **403** |
| 外部系统拒绝了我们的凭据（出站方向） | 502 |
| 瞬时故障（超时 / 5xx / 存储抖动） | **503 + Retry-After** |
| 其他确定性失败 | 500 |

⚠️ **第一行最容易被写错**：附件被删除是**业务结论**，不是系统故障。
用 5xx 会让调用端当成抖动反复重试，任务永远等不到人工处理。
"""

from __future__ import annotations

from typing import Any

from app.enums import ErrorCode
from app.errors import AppError
from app.services.result_service import ResultInputError

# `BUSINESS_FACT_CODES` 的**定义**已移到 `app/errors.py`（业务错误层）：
# 分类是业务判断，工具门面也要用它，而门面不得依赖协议层。
# 这里转出，让既有的 `from app.api.errors import BUSINESS_FACT_CODES` 继续可用。
from app.errors import BUSINESS_FACT_CODES as BUSINESS_FACT_CODES  # noqa: PLC0414

#: 按可重试性推断会不准确的错误码，需显式指定状态码
_EXPLICIT_STATUS: dict[ErrorCode, int] = {
    ErrorCode.TASK_NOT_FOUND: 404,
    ErrorCode.INSTANCE_NOT_FOUND: 404,
    # `GET /api/jobs/{id}`、`GET /api/parses/{id}` 的目标不存在。
    # **必须显式登记**：不登记时它会落到"其他确定性失败 → 500"，
    # 于是"id 写错了"表现为"服务端错误"，调用方会去重试或报障，
    # 而正确处置只是核对一下 id。
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    # 「记录在库里，但对象存储里没有那份字节」——附件内容与解析工件都走它。
    # ⚠️ 与 `RESOURCE_NOT_FOUND` **不是一回事**，两者处置不同：
    #   前者是"id 写错了 → 核对 id"，后者是"这份附件的字节还没入库 → 先跑工具 3"。
    # 此前它没登记，于是 `request_rule_run` 里"解析没有标准文档工件"
    # 会落到"其他确定性失败 → 500"：一个纯数据问题看起来像服务端崩了。
    ErrorCode.OBJECT_NOT_FOUND: 404,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.INVALID_STATE_TRANSITION: 409,
    ErrorCode.AUTH_FAILED: 502,
    ErrorCode.INVALID_GATEWAY_RESPONSE: 502,
    # 入站身份与授权（M7）。**必须显式登记**：不登记时会落到
    # "其他确定性失败 → 500"，于是"没带令牌"看起来像"服务端崩了" ——
    # 调用方会去重试或报障，而正确处置只是**去拿一份身份**。
    ErrorCode.AUTHENTICATION_REQUIRED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    # 规则管理与人工重试（M7）。三个码各回答一个不同的问题，**必须分开登记**：
    #   404 = "这个 rule_code 不存在"（核对 code）
    #   400 = "配置写错了"（改配置；重发同一个请求永远不会成功）
    #   409 = "该版本已被批次引用"（提升 rule_version 再来）
    # 不登记时三者都会落到 500，于是"配置写错了"看起来像服务端故障。
    ErrorCode.RULE_NOT_FOUND: 404,
    ErrorCode.RULE_CONFIG_INVALID: 400,
    ErrorCode.RULE_VERSION_IN_USE: 409,
    # 「这一步的恢复入口不在本接口」：任务确实阻塞了，但重试它没有意义
    # （下载失败要去重跑工具 3）。409 而不是 400：请求本身没错，
    # 是**当前状态**不允许这个操作。
    ErrorCode.RETRY_NOT_SUPPORTED: 409,
}

#: 503 上的 `Retry-After`（秒）。这是**下压信号**而非精确调度 ——
#: 真正的重试由 Worker 按 `next_retry_at` 执行，调用端不应按此值重试。
RETRY_AFTER_SECONDS = 30

#: 401 上的 `WWW-Authenticate`。**这是 401 区别于 403 的协议级标志**：
#: 它告诉客户端"请带凭据再来一次"，而 403 表示"带了也没用"。
#: 只写 `Bearer` 而不带 realm/error 细节：那些字段进不了自动重试逻辑，
#: 却可能把令牌校验的失败原因（过期 / 签名不符）透露给未认证的调用方。
BEARER_CHALLENGE = "Bearer"


#: `ResultInputError.reason_code` → 状态码（M6）。
#:
#: ⚠️ **必须显式登记**，不能靠 `ValueError` 的兜底分支。`ResultInputError`
#: 继承 `ValueError`，于是它会落进"参数非法 → 400"那一支 ——
#: 而"批次还没跑完"（**409**，等一会儿就好）与"风险等级传错了"（**400**，
#: 调用方得改）在接口上会**长得一模一样**，调用方只能靠猜。
#: 更糟的是原因码在翻译中被换成 `INVALID_ARGUMENT`，机器判据就此丢失。
RESULT_REASON_STATUS: dict[str, int] = {
    # 目标对象不存在：正确处置是**核对 id**，不是重试、也不是报障
    ResultInputError.RESULT_NOT_FOUND: 404,
    # 与当前状态冲突：批次尚未完成，稍后重试是**有意义**的
    ResultInputError.RESULT_RUN_NOT_COMPLETED: 409,
    # 调用方传错了参数：重试同样的请求永远不会成功
    ResultInputError.RESULT_INPUT_MISMATCH: 400,
}

#: 未登记的原因码兜底状态码。用 400 而不是 500：
#: 新增原因码却忘了登记时，"调用方传错了"这个默认判断**至少方向是对的**，
#: 而 500 会让一个纯粹的登记遗漏看起来像服务端故障。
RESULT_REASON_FALLBACK_STATUS = 400


def http_status_for_result_error(error: ResultInputError) -> int:
    """`ResultInputError` → HTTP 状态码。"""
    return RESULT_REASON_STATUS.get(
        error.reason_code, RESULT_REASON_FALLBACK_STATUS
    )


def result_error_body(error: ResultInputError) -> dict[str, Any]:
    """`ResultInputError` 的响应体。

    `error_code` 用**原有的原因码**（`RESULT_RUN_NOT_COMPLETED` 等），
    不换成 `INVALID_ARGUMENT`：调用方与 MCP 形态都靠它决定重试还是放弃。
    """
    return {
        "outcome": "error",
        "error_code": error.reason_code,
        "message": str(error),
        # 恒为 False：三条原因码说的是"**先改变点什么**再回来"
        # （等批次跑完 / 核对 id / 改正参数），原样重发同一个请求没有意义。
        # 这不是"永久失败"，而是"重试不是正确的处置"。
        "retryable": False,
    }


def http_status_for(error: AppError) -> int:
    """把端口异常映射成 HTTP 状态码。"""
    explicit = _EXPLICIT_STATUS.get(error.code)
    if explicit is not None:
        return explicit
    # 其余按可重试性决定：重试有意义 → 503；无意义 → 500
    return 503 if error.retryable else 500


def error_body(error: AppError) -> dict[str, Any]:
    """统一的错误响应体。

    `error_code` 是**稳定的机器判据**，`message` 只给人看（调用端不应解析它）。
    """
    return {
        "outcome": "error",
        "error_code": str(error.code),
        "message": error.message,
        "retryable": error.retryable,
    }


def error_headers(error: AppError) -> dict[str, str]:
    """随状态码一起返回的响应头。"""
    status = http_status_for(error)
    if status == 503:
        return {"Retry-After": str(RETRY_AFTER_SECONDS)}
    if status == 401:
        # 401 必须带 WWW-Authenticate（RFC 9110 §15.5.2）：
        # 不带时部分客户端不会触发"取新令牌后重试"的逻辑，
        # 于是有一次**本可以自动恢复**的失败被当成了永久拒绝。
        return {"WWW-Authenticate": BEARER_CHALLENGE}
    return {}


def invalid_argument_body(message: str) -> dict[str, Any]:
    """参数非法的响应体。来源不是 `AppError`：服务层对调用方传入的空标识抛 `ValueError`。"""
    return {
        "outcome": "error",
        "error_code": "INVALID_ARGUMENT",
        "message": message,
        "retryable": False,
    }
