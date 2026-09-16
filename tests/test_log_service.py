"""日志服务测试（M3 / T5）。

日志是最容易"顺手泄露"的地方：调试时把整个请求体打进去很自然，
而那是**不可逆的**——日志会被采集、归档、跨系统同步，
一次泄露的合同正文或令牌再也收不回来。

因此本文件的重点不是"能写日志"，而是两条硬约束：

1. **合同正文 / 凭据 / 个人信息不得原文落库**（四层脱敏，逐层验证）；
2. **`level=error` 必须带错误码**（没有错误码的失败无法被统计与自动分类）。

同时有一条**反向**用例：**SHA-256 摘要必须保留**。
脱敏做过头会毁掉可追溯性——那同样是缺陷，只是不容易被注意到。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.enums import ErrorCode, LogLevel, LogType
from app.models import TaskLog
from app.services.log_service import (
    MASK,
    LogService,
    redact_text,
    redact_value,
    summarize_long_text,
)

#: 一份"看起来像合同正文"的长文本：500 字，含金额、主体、保密条款
CONTRACT_TEXT = (
    "第一条 合同主体：甲方示例科技有限公司，乙方某某贸易有限公司。"
    "第二条 合同金额：人民币壹佰贰拾万元整（￥1,200,000.00）。"
    "第三条 付款方式：合同签订之日起三十日内，甲方向乙方支付预付款百分之六十。"
    "第四条 保密义务：双方对本合同内容及履行过程中知悉的商业秘密负有保密义务，"
    "保密期限为合同终止后五年。" * 3
)


@pytest.fixture()
def session(work_dir: Path):
    engine = create_engine(
        f"sqlite:///{(work_dir / 'logs.db').as_posix()}", future=True
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db_session:
            yield db_session
    finally:
        engine.dispose()


@pytest.fixture()
def service(session: Session) -> LogService:
    return LogService(session, operator="合同审批人")


def _reload(session: Session) -> list[TaskLog]:
    session.expire_all()
    return list(session.execute(select(TaskLog)).scalars().all())


# ============================================================
# 1. 基本写入
# ============================================================


def test_log_persists_structured_fields(session: Session, service: LogService) -> None:
    """日志的级别、类型、错误码都落在**结构化列**上，而不是拼进正文。"""
    service.log(
        log_type=LogType.DOWNLOAD,
        message="附件下载完成",
        task_id=None,
        level=LogLevel.INFO,
    )
    service.log_error(
        log_type=LogType.DOWNLOAD,
        message="附件在审批系统中已被删除",
        error_code=ErrorCode.ATTACHMENT_MISSING,
    )
    session.commit()

    entries = _reload(session)
    assert len(entries) == 2

    info_entry = next(e for e in entries if e.log_level == LogLevel.INFO.value)
    assert info_entry.log_type == LogType.DOWNLOAD.value
    assert info_entry.error_code is None

    error_entry = next(e for e in entries if e.log_level == LogLevel.ERROR.value)
    assert error_entry.error_code == ErrorCode.ATTACHMENT_MISSING.value, (
        "错误码必须落在独立列上：塞进正文就只能靠正则考古来统计"
    )


def test_operator_prefix_is_recorded(session: Session, service: LogService) -> None:
    """轻量身份留痕：`task_logs` 没有操作人列，因此用 `[operator]` 前缀。"""
    service.log(log_type=LogType.RULE, message="规则热更新完成")
    service.log(
        log_type=LogType.RULE, message="规则热更新完成", operator="系统管理员"
    )
    session.commit()

    entries = _reload(session)
    contents = {entry.log_content for entry in entries}

    assert "[合同审批人] 规则热更新完成" in contents
    assert "[系统管理员] 规则热更新完成" in contents


def test_log_returns_flushed_entry(session: Session, service: LogService) -> None:
    """返回的对象主键可用，便于调用方在后续日志里引用。"""
    entry = service.log(log_type=LogType.PULL, message="拉取开始")

    assert entry.id is not None
    assert entry.log_type == LogType.PULL.value


# ============================================================
# 2. 参数校验
# ============================================================


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"message": "   "}, "日志消息不能为空"),
        ({"message": "x", "level": "whatever"}, "未知的日志级别"),
        ({"message": "x", "log_type": "whatever"}, "未知的日志类型"),
        (
            {"message": "x", "error_code": "NOT_A_CODE"},
            "未知的错误码",
        ),
    ],
    ids=["空白消息", "非法级别", "非法类型", "非法错误码"],
)
def test_invalid_arguments_are_rejected(
    service: LogService, kwargs: dict, match: str
) -> None:
    """参数非法属于编程错误，抛 `ValueError` 而不是业务异常。"""
    kwargs.setdefault("log_type", LogType.SYSTEM)

    with pytest.raises(ValueError, match=match):
        service.log(**kwargs)


def test_error_level_requires_error_code(service: LogService) -> None:
    """`level=error` 却不给错误码 → 拒绝。

    这条规则的存在理由：error 日志没有错误码，意味着系统里出现了一个
    **无法分类的失败**。而"所有失败都带稳定错误码"正是调度器能够
    判断"重试还是立即阻塞"的前提。若当时确实没有合适的码，
    那是"该补一个码"的信号，不是"可以绕过规则"。
    """
    with pytest.raises(ValueError, match="必须提供 error_code"):
        service.log(
            log_type=LogType.PARSE, message="解析失败", level=LogLevel.ERROR
        )


def test_warning_level_does_not_require_error_code(service: LogService) -> None:
    """只有 `error` 强制要错误码 —— warning 可能只是提示性信息。"""
    entry = service.log(
        log_type=LogType.PARSE, message="OCR 置信度偏低", level=LogLevel.WARNING
    )

    assert entry.log_level == LogLevel.WARNING.value
    assert entry.error_code is None


# ============================================================
# 3. 脱敏：第 ① 层 —— 键名黑名单
# ============================================================


def test_sensitive_keys_are_masked() -> None:
    """命中键名黑名单的字段整值掩码。"""
    redacted = redact_value(
        {
            "access_token": "abc123",
            "id_card": "110101199001011234",
            "phone": "13800138000",
            "file_name": "合同.pdf",
        }
    )

    assert redacted["access_token"] == MASK
    assert redacted["id_card"] == MASK
    assert redacted["phone"] == MASK
    # 无关字段保持原样：掩掉它只会让日志失去价值
    assert redacted["file_name"] == "合同.pdf"


def test_sensitive_key_matching_is_case_insensitive() -> None:
    """厂商标识大小写不一，黑名单匹配必须不区分大小写。"""
    redacted = redact_value({"Authorization": "Bearer x", "API_KEY": "k"})

    assert redacted["Authorization"] == MASK
    assert redacted["API_KEY"] == MASK


def test_sensitive_keys_are_masked_in_nested_structures() -> None:
    """嵌套字典与列表里的敏感字段同样要被掩掉。

    只处理顶层是最常见的漏法：真实 payload 往往是
    `{"headers": {...}, "items": [{...}]}` 这种形状。
    """
    redacted = redact_value(
        {
            "request": {"headers": {"authorization": "Bearer secret"}},
            "items": [
                {"attachment_id": "A-1", "phone": "13800138000"},
                {"attachment_id": "A-2"},
            ],
        }
    )

    assert redacted["request"]["headers"]["authorization"] == MASK
    assert redacted["items"][0]["phone"] == MASK
    assert redacted["items"][0]["attachment_id"] == "A-1"


def test_original_payload_is_not_mutated() -> None:
    """脱敏返回副本，不修改调用方的对象。

    就地改写会让调用方在不知情的情况下拿到被打码的数据 ——
    它可能还要用这份数据去写库。
    """
    original = {"access_token": "abc123", "file_name": "合同.pdf"}

    redact_value(original)

    assert original["access_token"] == "abc123"


def test_deeply_nested_structure_is_bounded() -> None:
    """嵌套过深时截断，避免环形或恶意结构把脱敏过程拖垮。"""
    payload: dict = {}
    cursor = payload
    for _ in range(12):
        cursor["child"] = {}
        cursor = cursor["child"]

    redacted = redact_value(payload)

    assert "<嵌套过深>" in str(redacted)


# ============================================================
# 4. 脱敏：第 ② 层 —— 长文本只留摘要（合同正文的主防线）
# ============================================================


def test_long_text_is_replaced_by_digest_reference() -> None:
    """**合同正文不得原文落库。**

    注意这里的取值路径是 `note` —— 一个**不在黑名单里**的普通字段名。
    这说明：与其判断"这段文字是不是合同正文"，不如规定
    "够长的文本一律只留摘要"。**判断会漏，规则不会。**
    """
    entry_content = redact_value({"note": CONTRACT_TEXT})

    rendered = str(entry_content)
    assert CONTRACT_TEXT not in rendered
    assert "长文本" in rendered
    assert "sha256=" in rendered


def test_long_text_summary_keeps_traceability() -> None:
    """摘要必须可核对：同样的内容得到同样的摘要，不同内容得到不同摘要。

    脱敏不是丢信息 —— 我们仍然能回答"两次日志说的是不是同一份内容"。
    """
    same_a = summarize_long_text(CONTRACT_TEXT)
    same_b = summarize_long_text(CONTRACT_TEXT)
    different = summarize_long_text(CONTRACT_TEXT + "补充条款")

    assert same_a == same_b
    assert same_a != different
    assert str(len(CONTRACT_TEXT)) in same_a


def test_log_with_contract_text_in_payload_never_stores_it(
    session: Session, service: LogService
) -> None:
    """端到端：把合同正文放进 payload，落库正文里**不得**出现原文。"""
    service.log(
        log_type=LogType.PARSE,
        message="解析完成",
        payload={"note": CONTRACT_TEXT, "page_count": 12},
    )
    session.commit()

    content = _reload(session)[0].log_content

    assert CONTRACT_TEXT not in content
    assert "第一条 合同主体" not in content
    assert "预付款百分之六十" not in content
    # 非敏感信息与摘要引用应当保留，否则日志就没用了
    assert "解析完成" in content
    assert "page_count" in content
    assert "长文本" in content


# ============================================================
# 5. 脱敏：第 ③ 层 —— 文本模式
# ============================================================


@pytest.mark.parametrize(
    ("raw", "marker"),
    [
        ("联系申请人 13800138000 确认", "<手机号>"),
        ("申请人身份证 110101199001011234", "<身份证>"),
        ("收件邮箱 zhangsan@example.com", "<邮箱>"),
        ("请求头 Authorization: Bearer abc.def-ghi_123", "Bearer <凭据>"),
    ],
    ids=["手机号", "身份证", "邮箱", "Bearer 令牌"],
)
def test_text_patterns_are_masked(raw: str, marker: str) -> None:
    """短字段里夹带的个人信息也要被抹掉。

    这些内容往往出现在**消息文本**里而不是结构化字段里，
    只靠键名黑名单挡不住。
    """
    assert marker in redact_text(raw)
    assert raw not in redact_text(raw)


def test_text_patterns_are_masked_inside_log_content(
    session: Session, service: LogService
) -> None:
    """端到端：消息文本里的个人信息同样不得落库。"""
    service.log(
        log_type=LogType.PULL,
        message="申请人电话 13800138000，邮箱 zhangsan@example.com",
    )
    session.commit()

    content = _reload(session)[0].log_content

    assert "13800138000" not in content
    assert "zhangsan@example.com" not in content


# ============================================================
# 6. 反向约束：不该脱敏的必须保留
# ============================================================


def test_sha256_digest_is_preserved() -> None:
    """**SHA-256 摘要必须保留** —— 它本身就是脱敏后的引用。

    掩掉它等于毁掉可追溯性：我们将无法再核对"这份日志说的是不是这一份文件"。
    脱敏做过头同样是缺陷，只是更不容易被注意到。

    实现上靠文本模式两侧的 `\\b` 边界：64 位十六进制串内部没有单词边界，
    因此不会被身份证规则（`\\d{17}[\\dXx]`）误伤。
    """
    digest = "4320ee10797704009f25f2cab1dbb89b2da9dd9e4283fce92f77a1086f7493d9"

    redacted = redact_value({"file_checksum": digest, "object_key": digest})

    assert redacted["file_checksum"] == digest
    assert redacted["object_key"] == digest


def test_business_identifiers_are_preserved() -> None:
    """审批编号与附件编号是定位问题的唯一线索，必须保留。"""
    redacted = redact_value(
        {"approval_code": "HT-2026-0001", "attachment_id": "A-5002", "page": 3}
    )

    assert redacted == {
        "approval_code": "HT-2026-0001",
        "attachment_id": "A-5002",
        "page": 3,
    }


# ============================================================
# 7. 脱敏：第 ④ 层 —— 总量上限
# ============================================================


def test_overlong_content_is_truncated_with_marker(session: Session) -> None:
    """最终正文超长时截断，并标注原文长度。

    这是最后一道兜底：即使某一层的规则失效、拼出了超长正文，
    也不会把几千字的合同全文整个写进日志。

    这里刻意**不传操作人**，让长度可精确预测 ——
    `[operator]` 前缀会改变"原文 N 字符"里的 N，
    用固定数字断言会变成一条脆弱的测试。
    """
    service = LogService(session)
    message = "长" * 5000

    entry = service.log(log_type=LogType.PARSE, message=message)

    assert len(entry.log_content) == 2000 + len("…[已截断，原文 5000 字符]")
    assert entry.log_content.startswith("长")
    assert entry.log_content.endswith("[已截断，原文 5000 字符]")


def test_truncation_length_includes_operator_prefix(session: Session) -> None:
    """带操作人时，标记里的长度是**渲染后**的总长度。

    这不是缺陷，而是刻意的：报告"实际写入的正文有多长"比报告
    "原始 message 有多长"更有助于排查 —— 操作人前缀也是正文的一部分。
    """
    service = LogService(session, operator="合同审批人")
    message = "长" * 5000

    entry = service.log(log_type=LogType.PARSE, message=message)

    prefix = "[合同审批人] "
    assert entry.log_content.endswith(f"[已截断，原文 {len(prefix) + 5000} 字符]")


def test_normal_content_is_not_truncated(service: LogService) -> None:
    """正常长度的日志不受影响 —— 兜底规则不该干扰日常使用。"""
    entry = service.log(log_type=LogType.RULE, message="命中 3 条规则，总风险为高")

    assert "已截断" not in entry.log_content
