"""端口层与异常层测试（M3 / T2）。

本文件覆盖三件事：

1. **异常分类必须"写错就炸"** —— 把确定性错误塞进 `TransientError`（或反之）
   在构造时就抛 `ValueError`，而不是等到运行期表现为"重试无意义"或"永久失败"。
2. **端口拆分真的有用** —— 只读实现不必伪造评论能力，这是拆两个端口的全部目的。
3. **对象键不可构造目录穿越** —— 内容寻址键最终会变成文件路径的一部分。

另外附带一条**架构约束的机器化检查**（`app/ports/` 禁止导入外部 SDK）：
只靠口头约定，第一个赶时间的人就会在端口里 `import httpx`。
"""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath

import pytest

from app.config import PROJECT_ROOT
from app.enums import ErrorCode
from app.errors import (
    AppError,
    AttachmentValidationError,
    GatewayError,
    PermanentGatewayError,
    PermanentStorageError,
    StorageError,
    TransientGatewayError,
    TransientStorageError,
)
from app.ports.approval_gateway import (
    ApprovalCommentGateway,
    ApprovalDetailDTO,
    ApprovalGateway,
    ApprovalReadGateway,
    AuthoritativeContextDTO,
    PendingApprovalDTO,
)
from app.ports.object_storage import content_addressed_key

# ============================================================
# 1. 异常层级
# ============================================================


def test_retryable_is_derived_from_error_code() -> None:
    """可重试性由**错误码**派生，不是构造参数。

    若允许调用方传入 `retryable`，就会出现"同一个错误码，
    有人当可重试、有人当不可重试"的分裂，而调度器只能看错误码做批量统计。
    """
    assert TransientGatewayError(
        "超时", code=ErrorCode.APPROVAL_API_TIMEOUT
    ).retryable is True
    assert PermanentGatewayError(
        "实例不存在", code=ErrorCode.INSTANCE_NOT_FOUND
    ).retryable is False


def test_transient_error_rejects_permanent_code() -> None:
    """把确定性错误塞进瞬时异常，必须**构造期就失败**。"""
    with pytest.raises(ValueError, match="只能承载瞬时错误码"):
        TransientGatewayError("附件已被删除", code=ErrorCode.ATTACHMENT_MISSING)


def test_permanent_error_rejects_transient_code() -> None:
    """把瞬时错误塞进确定性异常，会让一次网络抖动把任务打成永久失败。"""
    with pytest.raises(ValueError, match="只能承载确定性错误码"):
        PermanentStorageError("存储后端 5xx", code=ErrorCode.STORAGE_UNAVAILABLE)


def test_storage_error_classification_is_actionable() -> None:
    """存储类错误的瞬时/确定性分界必须能直接指导"重试还是放弃"。

    这是最容易被写错的一处：
    "存储超时"重试通常就好了；"路径越界"重试一万次仍然非法。
    """
    temporary = TransientStorageError(
        "存储后端暂时不可用", code=ErrorCode.STORAGE_UNAVAILABLE
    )
    permanent = PermanentStorageError(
        "对象键越界", code=ErrorCode.STORAGE_PATH_INVALID
    )

    assert temporary.retryable is True
    assert permanent.retryable is False


def test_error_str_carries_the_error_code() -> None:
    """字符串形式必须带错误码，否则日志里只剩一段中文，无法统计。"""
    error = PermanentGatewayError(
        "审批单 HT-1 不存在", code=ErrorCode.INSTANCE_NOT_FOUND
    )
    assert str(error) == "[INSTANCE_NOT_FOUND] 审批单 HT-1 不存在"


def test_all_concrete_errors_share_a_common_base() -> None:
    """所有具体异常都能被一个 `except AppError` 兜住（调度器统一处理）。

    否则调度器只能逐个类型列举，新增一种异常就会漏处理。
    """
    cases = [
        (TransientGatewayError, ErrorCode.APPROVAL_API_TIMEOUT),
        (PermanentGatewayError, ErrorCode.INSTANCE_NOT_FOUND),
        (TransientStorageError, ErrorCode.STORAGE_UNAVAILABLE),
        (PermanentStorageError, ErrorCode.STORAGE_PATH_INVALID),
        (AttachmentValidationError, ErrorCode.ATTACHMENT_TOO_LARGE),
    ]
    for error_class, code in cases:
        error = error_class("测试", code=code)
        assert isinstance(error, AppError)
        assert isinstance(error, Exception)


def test_gateway_and_storage_errors_are_attributable() -> None:
    """排障时要能一眼看出问题在审批系统还是存储——两者查的地方完全不同。"""
    gateway_error = TransientGatewayError(
        "连不上", code=ErrorCode.APPROVAL_UNREACHABLE
    )
    storage_error = PermanentStorageError(
        "校验和不符", code=ErrorCode.CHECKSUM_MISMATCH
    )

    assert isinstance(gateway_error, GatewayError)
    assert not isinstance(gateway_error, StorageError)
    assert isinstance(storage_error, StorageError)
    assert not isinstance(storage_error, GatewayError)


# ============================================================
# 2. DTO
# ============================================================


def test_dto_is_immutable() -> None:
    """DTO 必须不可变：它们会被多个模块共享，可变对象会带来难以定位的串改。"""
    dto = PendingApprovalDTO(
        provider="mock",
        tenant_id="default",
        instance_id="HT-2026-0001",
        approval_code="HT-2026-0001",
        approval_title="采购合同",
        applicant_name="张三",
        apply_time="2026-09-01 09:00:00",
        attachment_count=2,
    )
    with pytest.raises(FrozenInstanceError):
        dto.approval_title = "改一下"  # type: ignore[misc]


def test_authoritative_context_allows_missing_fields() -> None:
    """四项业务事实都可能缺失。

    缺失时相关规则判 `needs_review`，**不是**任务 `blocked`——
    否则在拿不到这些字段的审批系统里，本系统会完全不可用。
    """
    context = AuthoritativeContextDTO()

    assert context.our_party_name is None
    assert context.our_party_contract_label is None
    assert context.our_party_business_role is None
    assert context.contract_type is None


def test_detail_dto_form_data_is_not_shared_between_instances() -> None:
    """`form_data` 必须用 `default_factory`，否则所有实例会共享同一个 dict。"""

    def build(instance_id: str) -> ApprovalDetailDTO:
        return ApprovalDetailDTO(
            instance_id=instance_id,
            approval_code=instance_id,
            approval_title="",
            applicant_name="",
            apply_time="",
            context=AuthoritativeContextDTO(),
        )

    first, second = build("A"), build("B")
    first.form_data["amount"] = "100"

    assert second.form_data == {}, "两个 DTO 实例共享了同一个 form_data"


# ============================================================
# 3. 端口拆分：只读实现不必伪造评论能力
# ============================================================


class _ReadOnlyGateway:
    """一个只实现读取能力的假实现。

    模拟真实场景：企业只给了审批系统的读取权限。
    """

    # 身份是端口的**数据成员**，因此假实现也必须提供
    provider = "fake"
    tenant_id = "default"

    def list_pending(self, limit: int) -> list[PendingApprovalDTO]:
        return []

    def get_detail(self, instance_id: str) -> ApprovalDetailDTO:
        raise NotImplementedError

    def download_attachment(self, instance_id: str, attachment_id: str):
        raise NotImplementedError


class _FullGateway(_ReadOnlyGateway):
    """同时具备读取与评论能力。"""

    def write_comment(self, instance_id, content, *, idempotency_key, operator_name=None):
        raise NotImplementedError

    def get_write_result(self, instance_id, idempotency_key):
        return None


def test_read_only_gateway_needs_no_comment_ability() -> None:
    """只读实现**不需要**伪造 `write_comment` / `get_write_result`。

    这正是把端口拆成两个的全部意义：若读写同属一个 Protocol，
    只读了权限的系统就得写出两个永远返回空的方法——
    那比直接缺失更糟，因为它看起来是能用的。
    """
    gateway = _ReadOnlyGateway()

    assert isinstance(gateway, ApprovalReadGateway)
    assert not isinstance(gateway, ApprovalCommentGateway)


def test_full_gateway_satisfies_both_ports() -> None:
    gateway = _FullGateway()

    assert isinstance(gateway, ApprovalReadGateway)
    assert isinstance(gateway, ApprovalCommentGateway)
    assert isinstance(gateway, ApprovalGateway)


def test_port_isinstance_check_only_covers_member_presence() -> None:
    """诚实记录 `runtime_checkable` 的**局限**：它只看成员是否存在，不校验签名。

    因此适配器真正的语义一致性由合约测试保证（企业化设计 §15.1 第 3 层），
    而不是靠这个 `isinstance`。这里把局限写成测试，避免后来者误以为它很强。
    """

    class _WrongSignature:
        provider = "fake"
        tenant_id = "default"

        def list_pending(self) -> str:  # 少参数、返回类型也不对
            return "not a list"

        def get_detail(self):
            return None

        def download_attachment(self):
            return None

    # 签名完全不对，但成员齐了，isinstance 依然通过
    assert isinstance(_WrongSignature(), ApprovalReadGateway)


# ============================================================
# 4. 内容寻址键
# ============================================================


def test_content_addressed_key_layout() -> None:
    """键的层级必须稳定：`sha256/<前2位>/<次2位>/<完整摘要>.<ext>`。"""
    digest = "a" * 64
    assert content_addressed_key(digest, suffix="pdf") == f"sha256/aa/aa/{digest}.pdf"


def test_content_addressed_key_normalizes_case_and_leading_dot() -> None:
    """摘要大小写、扩展名前导点都应被归一 —— 否则同一份文件会生成两个键。"""
    digest = "AB" + "c" * 62
    key = content_addressed_key(digest, suffix=".PDF")

    assert key == f"sha256/ab/cc/{digest.lower()}.pdf"


def test_content_addressed_key_defaults_to_bin() -> None:
    """没有扩展名时退化为 `.bin`，而不是生成一个以点结尾的键。"""
    assert content_addressed_key("b" * 64).endswith(".bin")


@pytest.mark.parametrize(
    "invalid_digest",
    ["", "abc", "a" * 63, "a" * 65, "z" * 64, "a" * 63 + "!", "g" * 64],
    ids=["空串", "过短", "63 位", "65 位", "非十六进制字母", "含符号", "越界字母"],
)
def test_content_addressed_key_rejects_invalid_digest(invalid_digest: str) -> None:
    """摘要必须严格校验。

    对象键最终会变成文件路径的一部分，且 `sha256` 前缀目录是按它的前几位切的；
    不校验就等于把路径拼接交给外部输入。
    """
    with pytest.raises(ValueError):
        content_addressed_key(invalid_digest)


@pytest.mark.parametrize(
    "bad_suffix",
    ["../etc/passwd", "p d f", "pd/f", "pd-f", "pd.f"],
    ids=["目录穿越", "含空格", "含斜杠", "含连字符", "含多余点"],
)
def test_content_addressed_key_rejects_path_traversal_suffix(
    bad_suffix: str,
) -> None:
    """扩展名不得构造目录穿越——这是"内容寻址键"最容易漏掉的攻击面。"""
    with pytest.raises(ValueError):
        content_addressed_key("c" * 64, suffix=bad_suffix)


def test_generated_key_cannot_escape_storage_root() -> None:
    """任何合法输入生成的对象键都必须是安全相对路径。"""
    for digest in ("0" * 64, "f" * 64, "A1" + "b2" * 31):
        key = content_addressed_key(digest, suffix="pdf")

        assert ".." not in key
        assert not key.startswith("/")
        assert not PurePosixPath(key).is_absolute()


# ============================================================
# 5. 架构约束的机器化检查
# ============================================================

#: 端口层禁止出现的顶层模块名
_FORBIDDEN_IN_PORTS = frozenset(
    {
        # 外部 SDK —— 只允许出现在 app/adapters/
        "httpx",
        "minio",
        "fitz",
        "pymupdf",
        "rapidocr_onnxruntime",
        "openai",
        "redis",
        "celery",
        # Web 框架与持久化 —— 端口层不应感知传输与存储细节
        "fastapi",
        "sqlalchemy",
    }
)

#: 端口层禁止依赖的本项目模块（依赖方向必须单向）
_FORBIDDEN_PROJECT_MODULES = frozenset(
    {"app.models", "app.db", "app.services", "app.adapters", "app.api"}
)


def _imported_modules(node: ast.AST) -> list[str]:
    """取出一条 import 语句引入的模块名（相对导入返回空）。"""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level:  # 相对导入（from . import x）不参与判定
            return []
        return [node.module or ""]
    return []


def test_ports_do_not_import_external_sdks_or_inner_layers() -> None:
    """`app/ports/` 只能依赖标准库与 `app.enums`。

    这是"换厂商只改 Adapter"这条承诺的**机器化保证**。
    只靠口头约定，第一个赶时间的人就会在端口里 `import httpx`，
    从此端口与某个具体 SDK 绑死，而代码评审不一定每次都能拦住。

    同时它也守住了依赖方向：端口在 services 之下，
    若端口反过来 import `app.services`，依赖就成环了。
    """
    ports_dir = PROJECT_ROOT / "app" / "ports"
    assert ports_dir.is_dir(), "端口目录不存在"

    problems: list[str] = []
    for path in sorted(ports_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for module in _imported_modules(node):
                top_level = module.split(".")[0]
                if top_level in _FORBIDDEN_IN_PORTS:
                    problems.append(f"{path.name} 导入了外部 SDK：{module}")
                if module in _FORBIDDEN_PROJECT_MODULES:
                    problems.append(f"{path.name} 依赖了内层模块：{module}")

    assert not problems, "端口层违反依赖约束：" + "；".join(problems)
