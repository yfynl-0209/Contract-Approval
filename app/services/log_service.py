"""业务事件日志 —— 全项目写 `task_logs` 的唯一入口。

两条硬约束（企业化设计 §14）：

1. **稳定错误码结构化落库**：`error_code` 独立成列，不塞进 `log_content`。
   自由文本无法统计、无法告警、无法断言 —— 否则"本周有多少次超时"会退化成
   对日志做正则考古。本模块**拒绝** `level=error` 却不给错误码的调用。
2. **脱敏四层防护**：① 键名黑名单 → ② 值长度阈值（超 120 字符只留摘要）
   → ③ 文本模式（身份证 / 手机 / 邮箱 / Bearer）→ ④ 总量截断。

**第 ② 层是关键**：与其判断"这段是不是合同正文"，不如规定"够长的一律只留摘要"——
判断会漏，规则不会。摘要不是丢信息：仍能核对"两次日志说的是不是同一份内容"。

刻意**不**脱敏：SHA-256 摘要（它本身就是脱敏后的引用，掩掉等于毁掉可追溯性）、
审批编号与附件编号（定位问题的唯一线索）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.context import get_correlation_id
from app.enums import ErrorCode, LogLevel, LogType
from app.models import TaskLog

#: 掩码：命中键名黑名单时整值替换
MASK = "***"

#: 单值内联上限（字符）。超过则只留摘要 —— 这是"合同正文不进日志"的主要防线。
DEFAULT_MAX_INLINE_VALUE_CHARS = 120

#: 单条日志正文上限（字符）
DEFAULT_MAX_CONTENT_CHARS = 2000

#: 嵌套深度上限：防止恶意/环形结构把脱敏过程拖垮
_MAX_DEPTH = 6

#: 键名子串黑名单（小写比较）。**宁可多掩，不可漏掩**：
#: 掩掉一个无害字段只是少一点信息，漏掉一个令牌就是安全事件。
SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    # ---- 凭据 ----
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "signature",
    "private_key",
    "access_key",
    "bearer",
    # ---- 个人信息 ----
    "id_card",
    "idcard",
    "id_no",
    "identity",
    "phone",
    "mobile",
    "telephone",
    "email",
    "address",
    "bank",
    "account_no",
    "card_no",
    # ---- 正文与全文 ----
    "contract_text",
    "full_text",
    "raw_text",
    "ocr_text",
    "document_text",
    "clause_text",
)

#: 文本层模式替换（按顺序应用）
_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Bearer 令牌：即使整段请求被误记，凭据也要被抹掉
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "Bearer <凭据>"),
    # 身份证（18 位，末位可能是 X）
    (re.compile(r"\b\d{17}[\dXx]\b"), "<身份证>"),
    # 手机号（中国大陆）
    (re.compile(r"\b1[3-9]\d{9}\b"), "<手机号>"),
    # 邮箱
    (re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w.\-]+\b"), "<邮箱>"),
)


# ============================================================
# 脱敏
# ============================================================


def redact_text(text: str) -> str:
    """对文本做模式级脱敏（第 ③ 层）。"""
    result = text
    for pattern, replacement in _TEXT_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def summarize_long_text(text: str) -> str:
    """把长文本替换为"长度 + 摘要"引用（第 ② 层）。需要全文时按 `object_key` 取。"""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"<长文本 {len(text)} 字符 sha256={digest}>"


def _is_sensitive_key(key: str) -> bool:
    """键名是否命中黑名单（子串匹配，大小写不敏感）。"""
    lowered = key.lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_SUBSTRINGS)


def redact_value(
    value: Any,
    *,
    max_inline_chars: int = DEFAULT_MAX_INLINE_VALUE_CHARS,
    _depth: int = 0,
) -> Any:
    """递归脱敏任意结构，返回可安全序列化的副本。

    原始对象**不被修改**：调用方可能还要拿它做别的事。
    """
    if _depth > _MAX_DEPTH:
        return "<嵌套过深>"

    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for raw_key, raw_item in value.items():
            key = str(raw_key)
            if _is_sensitive_key(key):
                redacted[key] = MASK
            else:
                redacted[key] = redact_value(
                    raw_item, max_inline_chars=max_inline_chars, _depth=_depth + 1
                )
        return redacted

    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            redact_value(item, max_inline_chars=max_inline_chars, _depth=_depth + 1)
            for item in value
        ]

    if isinstance(value, str):
        # 先看长度：够长就整体换成摘要。
        # 与其判断"这是不是合同正文"，不如规定"够长的一律只留摘要"——
        # 判断会漏，规则不会。
        if len(value) > max_inline_chars:
            return summarize_long_text(value)
        return redact_text(value)

    return value


def render_log_content(
    message: str,
    payload: Mapping[str, Any] | None = None,
    *,
    max_inline_chars: int = DEFAULT_MAX_INLINE_VALUE_CHARS,
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
) -> str:
    """把消息与结构化上下文渲染成最终落库的正文。

    顺序：拼接 → 文本模式脱敏 → 总长截断。截断放最后，保证脱敏作用于完整内容。
    """
    text = message.strip()

    if payload:
        safe_payload = redact_value(payload, max_inline_chars=max_inline_chars)
        rendered = json.dumps(safe_payload, ensure_ascii=False, default=str)
        text = f"{text} | {rendered}"

    text = redact_text(text)

    if len(text) > max_content_chars:
        text = f"{text[:max_content_chars]}…[已截断，原文 {len(text)} 字符]"

    return text


# ============================================================
# 日志服务
# ============================================================


class LogService:
    """写 `task_logs` 的服务。

    与业务状态**同事务**写入（调用方提交，日志才落地），回滚时一起回滚 —— 这是刻意的：
    `task_logs` 是运行日志。不可回滚的审计事件属 `audit_events`（M6），两者不混用。

    操作人作为 `[operator]` 前缀写进正文（需求文档 2.4.9 未给 `task_logs` 操作人字段）。

    **关联 ID 自动携带**（§4.7）：来自 `app.context` 的 contextvar，不接受参数。
    做成参数的话，每一处新增的日志调用都可能忘记传 —— 而漏传的表现是
    "日志少了一段"，不报错、不告警，只在排障时发现追不下去。
    """

    def __init__(
        self,
        session: Session,
        *,
        operator: str | None = None,
        max_inline_chars: int = DEFAULT_MAX_INLINE_VALUE_CHARS,
        max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
    ) -> None:
        """
        Args:
            session: 数据库会话。
            operator: 默认操作人（轻量身份），可被单次调用覆盖。
            max_inline_chars: 单值内联上限。
            max_content_chars: 单条正文上限。
        """
        self._session = session
        self._operator = operator
        self._max_inline_chars = max_inline_chars
        self._max_content_chars = max_content_chars

    def log(
        self,
        *,
        log_type: LogType | str,
        message: str,
        task_id: int | None = None,
        level: LogLevel | str = LogLevel.INFO,
        error_code: ErrorCode | str | None = None,
        payload: Mapping[str, Any] | None = None,
        operator: str | None = None,
    ) -> TaskLog:
        """写一条日志。

        Returns:
            已 flush 的 `TaskLog`（主键可用；提交由调用方负责）。

        Raises:
            ValueError: 参数非法。这类都是**编程错误**，不是业务失败：
                - `message` 为空白；
                - `level` / `log_type` / `error_code` 不在受控取值内；
                - `level=error` 却没给 `error_code`（见下方说明）。
        """
        level_value = _as_enum(LogLevel, level, "日志级别")
        type_value = _as_enum(LogType, log_type, "日志类型")
        code_value = (
            None if error_code is None else _as_enum(ErrorCode, error_code, "错误码")
        )

        message_text = (message or "").strip()
        if not message_text:
            raise ValueError("日志消息不能为空")

        if level_value is LogLevel.ERROR and code_value is None:
            # 这条规则的存在理由：error 日志却没有错误码，
            # 意味着系统里出现了一个**无法分类的失败**——
            # 而"所有失败都带稳定错误码"正是 §7.3 与调度器分类能力的前提。
            # 若当时确实没有合适的码，那是"该补一个码"的信号，不是"可以绕过规则"。
            raise ValueError(
                "level=error 的日志必须提供 error_code："
                "没有错误码的失败无法被统计、告警与自动分类"
            )

        actor = (operator or self._operator or "").strip()
        head = f"[{actor}] {message_text}" if actor else message_text

        content = render_log_content(
            head,
            payload,
            max_inline_chars=self._max_inline_chars,
            max_content_chars=self._max_content_chars,
        )

        entry = TaskLog(
            task_id=task_id,
            log_level=level_value.value,
            log_type=type_value.value,
            log_content=content,
            error_code=None if code_value is None else code_value.value,
            # 【自动带上】关联 ID（§4.7）：不在这里做参数，是为了让"漏传"不可能发生。
            # 未绑定时为 `None`（CLI / 测试 / Worker 未注入）—— 不凭空生成，
            # 那会把"同一次请求的全链路"拆成互不相干的碎片。
            correlation_id=get_correlation_id(),
        )
        self._session.add(entry)
        self._session.flush()
        return entry

    def log_error(
        self,
        *,
        log_type: LogType | str,
        message: str,
        error_code: ErrorCode | str,
        task_id: int | None = None,
        payload: Mapping[str, Any] | None = None,
        operator: str | None = None,
    ) -> TaskLog:
        """写一条 `error` 日志（把"级别 + 错误码必须成对"固化成一次调用）。"""
        return self.log(
            log_type=log_type,
            message=message,
            task_id=task_id,
            level=LogLevel.ERROR,
            error_code=error_code,
            payload=payload,
            operator=operator,
        )


def _as_enum(enum_class: type, value: Any, label: str) -> Any:
    """把取值规范化为受控枚举成员。

    Raises:
        ValueError: 取值不在枚举内（属编程错误）。
    """
    if isinstance(value, enum_class):
        return value
    try:
        return enum_class(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_class)
        raise ValueError(f"未知的{label}：{value!r}（允许：{allowed}）") from exc
