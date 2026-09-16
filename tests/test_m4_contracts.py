"""M4 的端口、DTO 与错误码契约（T3）。

本文件守住四件事，它们都不会**自己**报错：

1. **错误码的重试性必须逐码核对，不能抽查。**
   漏登记 `OCR_INFERENCE_TIMEOUT` 的表现是"一次推理超时把任务直接打死"，
   而日志里的错误码看起来完全正常 —— 它只是被当成确定性错误处理了。
   反向错误同样危险：把 `OCR_MODEL_UNAVAILABLE` 登记成可重试，会让一个
   **配置错误**重试到预算耗尽，把"模型路径写错"掩盖成"服务不稳定"。

2. **新增作业类型必须显式登记输入模型。**
   否则它会静默失去校验 —— 而入队看起来完全成功。

3. **标准文档的约束必须在构造时生效、读回时同样生效。**
   普通 `dataclass` 的注解不做运行时检查，`page_status="illegal"` 能创建成功，
   于是"非法状态写不进"这条验收根本无法成立。

4. **像素 → PDF 的逆映射必须四个角都映。**
   旋转 90°/270° 时只映对角两点会得到**倒序**的框（`x0 > x1`），
   而它仍然"看起来像个框"。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.enums import (
    BboxPrecision,
    ErrorCode,
    JobType,
    PageStatus,
    TextPrecision,
    is_retryable,
)
from app.ports.ocr_gateway import OCRGateway, OcrLine, OcrPageResult, RenderTransform
from app.ports.parse_document import (
    DocumentBlock,
    DocumentChar,
    DocumentPage,
    SourceSpan,
    StandardDocument,
)
from app.workflow.job_inputs import (
    INPUT_MODELS,
    PENDING_JOB_TYPES,
    JobInputError,
    ParseJobInput,
    ParseOptions,
    validate_job_input,
)

#: M4 新增的错误码。**写死在这里**是有意的：
#: 往 `ErrorCode` 里加码却忘了登记重试性时，这条会失败。
#:
#: ⚠️ **本集合是手工维护的，因此它自己也会漏。** 已经漏过一次：
#: T10 加的 `RESOURCE_NOT_FOUND` 当时没有登记进来，于是"加码必登记"这条守卫
#: **对它无声地失效了** —— 集合看起来是完整的，测试也全绿。
#: 因此新增错误码时，**必须同时加进这里**（这条注释本身就是那个提醒）。
M4_ERROR_CODES = {
    # 解析（T3）
    ErrorCode.DOCUMENT_EMPTY,
    ErrorCode.OCR_UNRECOGNIZABLE,
    ErrorCode.OCR_MODEL_UNAVAILABLE,
    ErrorCode.OCR_INFERENCE_TIMEOUT,
    ErrorCode.PDF_ENCRYPTED,
    ErrorCode.PDF_CORRUPT,
    ErrorCode.PDF_TOO_MANY_PAGES,
    ErrorCode.PDF_TOO_LARGE_PIXELS,
    ErrorCode.PDF_RENDER_TIMEOUT,
    ErrorCode.OCR_INFERENCE_FAILED,
    # Worker / 租约（T6）
    ErrorCode.LEASE_LOST,
    ErrorCode.UNEXPECTED_ERROR,
    # 输入格式（T10–T12）
    ErrorCode.DOCUMENT_FORMAT_UNRECOGNIZED,
    # 最小查询接口（T10）
    ErrorCode.RESOURCE_NOT_FOUND,
}

#: 其中**只有**这三个是瞬时错误：超时与推理抖动重试通常就好，其余都是确定性错误。
M4_RETRYABLE = {
    ErrorCode.OCR_INFERENCE_TIMEOUT,
    ErrorCode.OCR_INFERENCE_FAILED,
    ErrorCode.PDF_RENDER_TIMEOUT,
}


# ============================================================
# 1. 错误码与重试性
# ============================================================


def test_m4_error_codes_retryability_is_exhaustive() -> None:
    """逐码遍历 —— **不抽查**。

    抽查漏掉一个码的代价是：它在生产里被当成"不可重试"，
    任务在第一次超时就被打成永久失败，而所有日志都正常。
    """
    actual_retryable = {code for code in M4_ERROR_CODES if is_retryable(code)}

    assert actual_retryable == M4_RETRYABLE, (
        f"可重试集合不符：多出 {actual_retryable - M4_RETRYABLE}，"
        f"缺少 {M4_RETRYABLE - actual_retryable}"
    )


def test_m4_error_code_count_is_frozen() -> None:
    """码的数量写死：少写一个说明有码被顺手删掉了。"""
    assert len(M4_ERROR_CODES) == 14
    assert M4_ERROR_CODES <= set(ErrorCode)


# ============================================================
# 2. 作业输入模型
# ============================================================


def test_every_job_type_is_either_modelled_or_explicitly_pending() -> None:
    """每种作业类型要么有输入模型，要么被**显式**列为待补。

    没有这条，新增一种 `job_type` 会静默失去校验：
    作业照常入队、"成功"，只是参数没生效。
    """
    covered = set(INPUT_MODELS) | set(PENDING_JOB_TYPES)
    assert covered == set(JobType), f"未覆盖的作业类型：{set(JobType) - covered}"
    assert not (set(INPUT_MODELS) & set(PENDING_JOB_TYPES)), "不能既建模又待补"


def test_validate_job_input_fills_defaults() -> None:
    """落库的是**校验后**的完整输入（含默认值）。

    不写默认值的话，读回时无法区分"当时用的是默认 DPI"与"当时没记录 DPI"。
    """
    normalized = validate_job_input(JobType.PARSE, {**_VALID_PARSE_INPUT})

    assert normalized["parse_options"]["dpi"] == 200
    assert normalized["parse_options"]["min_chars"] == 20


def test_validate_job_input_rejects_unknown_field() -> None:
    """**拼错键必须被拒**，不能被静默当作默认值。"""
    payload = {**_VALID_PARSE_INPUT, "sourc_checksum": "typo"}

    with pytest.raises(JobInputError, match="输入非法"):
        validate_job_input(JobType.PARSE, payload)


def test_validate_job_input_rejects_missing_required_field() -> None:
    """`parse_id` 不可省 —— Worker 靠它找到要填充的占位行。"""
    payload = {key: value for key, value in _VALID_PARSE_INPUT.items() if key != "parse_id"}

    with pytest.raises(JobInputError, match="输入非法"):
        validate_job_input(JobType.PARSE, payload)


def test_validate_job_input_rejects_empty() -> None:
    with pytest.raises(JobInputError, match="作业输入不能为空"):
        validate_job_input(JobType.PULL, {})


def test_validate_job_input_rejects_unregistered_job_type() -> None:
    """未登记的作业类型必须**报错**，而不是"找不到模型就放行"。"""
    with pytest.raises(JobInputError, match="没有登记输入模型"):
        validate_job_input("brand_new_type", {"x": 1})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dpi", 1),  # 低于 72：渲染出来根本读不出字
        ("dpi", 100000),  # 高于 600：内存爆炸
        ("ocr_min_confidence", 5.0),  # 概率不可能大于 1
        ("ocr_min_confidence", -0.1),
        ("min_chars", 0),
        ("min_coverage", 1.5),
        ("max_garbage_ratio", -1),
    ],
)
def test_parse_options_rejects_out_of_range(field: str, value: object) -> None:
    """越界参数必须在**构造时**被拒。

    没有范围的数值参数等于把校验推迟到最难排查的时刻：
    `dpi=1` 能进库，问题只在渲染时浮现 —— 那时离"参数写错"已经很远了。
    """
    with pytest.raises(ValidationError):
        ParseOptions(**{field: value})


def test_parse_options_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError):
        ParseOptions(dpi=200, unknown_option=1)  # type: ignore[call-arg]


def test_parse_job_input_is_frozen() -> None:
    """输入**不可变**：作业创建后不再修改，重试沿用同一输入。"""
    payload = ParseJobInput(**_VALID_PARSE_INPUT)

    with pytest.raises(ValidationError):
        payload.parse_id = 2  # type: ignore[misc]


_VALID_PARSE_INPUT: dict = {
    "parse_id": 18,
    "attachment_record_id": 37,
    "source_checksum": "a" * 64,
    "object_key": f"sha256/aa/aa/{'a' * 64}.pdf",
    "content_type": "application/pdf",
}


# ============================================================
# 3. 枚举取值域
# ============================================================


def test_page_status_has_exactly_four_states() -> None:
    """四态齐备，且 `uncertain` **不可省**。

    只有 `blank` / `failed` 两档时，"读到了但读不准"只能二选一：
    归 `blank` 会丢掉内容，归 `failed` 会谎报故障 —— 两者都让下游无法正确处置。
    """
    assert {status.value for status in PageStatus} == {
        "ok",
        "blank",
        "uncertain",
        "failed",
    }


def test_precisions_have_distinct_domains() -> None:
    """`block` 只对几何有意义，**不得**出现在文本精度里。

    曾经共用一个枚举，代价是每个使用点都要额外说明"哪些值在这里非法"，
    而漏说明的地方就写出了 `TextPrecision = block` 这种矛盾。
    """
    assert "block" not in {item.value for item in TextPrecision}
    assert "block" in {item.value for item in BboxPrecision}


# ============================================================
# 4. 标准文档：构造即校验，读回也校验
# ============================================================


def _char(index: int = 0) -> DocumentChar:
    return DocumentChar(
        text="采",
        bbox=(60.0, 60.5, 72.0, 74.9),
        char_start=index,
        char_end=index + 1,
    )


def _block(**overrides: object) -> DocumentBlock:
    base: dict = {
        "block_id": "p1-b1",
        "text": "采购合同",
        "bbox": (60.0, 60.0, 200.0, 80.0),
        "char_start": 0,
        "char_end": 4,
        "text_precision": TextPrecision.CHAR,
        "bbox_precision": BboxPrecision.CHAR,
        "chars": (_char(),),
    }
    base.update(overrides)
    return DocumentBlock(**base)


def _page(**overrides: object) -> DocumentPage:
    base: dict = {
        "page": 1,
        "width": 595.0,
        "height": 842.0,
        "bbox_space": "pdf-point-top-left",
        "rotation": 0,
        "source": "text",
        "page_status": PageStatus.OK,
        # ⚠️ `text` 必须与块区间**一致**：`_block()` 用的是 [0, 4) 与 "采购合同"。
        # 留空会让新加的校验器（块文本 == 页文本切片）在这里就抛错，
        # 于是断言失败的原因与它想验的东西无关。
        "text": "采购合同",
        "blocks": (_block(),),
    }
    base.update(overrides)
    return DocumentPage(**base)


def test_page_status_illegal_value_is_rejected_at_construction() -> None:
    """**构造即失败** —— 这正是改用 Pydantic 的全部理由。

    用普通 `@dataclass` 时 `page_status="illegal"` 能创建成功，
    于是"非法状态写不进"这条验收无法成立。
    """
    with pytest.raises(ValidationError):
        _page(page_status="illegal")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rotation", 45),  # 只允许 0 / 90 / 180 / 270
        ("width", 0),  # 尺寸必须为正
        ("height", -1),
        ("bbox_space", "image-pixel-top-left"),  # 标准文档只有一个坐标系
        ("source", "guess"),
        ("page", 0),  # 1-based
        ("unknown_field", 1),
    ],
)
def test_document_page_rejects_illegal_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _page(**{field: value})


def test_bbox_must_be_normalized() -> None:
    """倒序的框**看起来仍然像个框**，只是画出来是负面积 —— 入口拒绝。"""
    with pytest.raises(ValidationError, match="归一化"):
        _block(bbox=(200.0, 60.0, 60.0, 80.0))


def test_char_precision_requires_chars() -> None:
    """`bbox_precision=char` 但 `chars` 为空 → 拒绝。

    这是整个契约里最要紧的一条：只有声明没有 `chars` 就是**空头承诺**，
    M8 拿到 `char` 却只能画块框，而验收 6 断言的正是"能画字符框"。
    """
    with pytest.raises(ValidationError, match="chars 为空"):
        _block(bbox_precision=BboxPrecision.CHAR, chars=())


def test_block_precision_does_not_require_chars() -> None:
    """反过来必须放行：只有块级几何时就该老老实实标 `block`。"""
    block = _block(bbox_precision=BboxPrecision.BLOCK, chars=())

    assert block.bbox_precision is BboxPrecision.BLOCK
    assert block.chars == ()


def test_source_span_rejects_empty_range() -> None:
    """区间为空说明映射算错了，不能静默通过。"""
    with pytest.raises(ValidationError, match="非空"):
        SourceSpan(raw_start=3, raw_end=3)


def test_source_span_expresses_many_to_one() -> None:
    """NFC 是多对一的：`e` + 组合重音（2 码位）→ `é`（1 码位）。

    用一个整数表示映射就表达不了这种情形，也算不出那个字符应有的**联合 bbox**。
    """
    span = SourceSpan(raw_start=10, raw_end=12)

    assert span.raw_end - span.raw_start == 2


def test_pages_must_be_contiguous() -> None:
    """缺页会让下游按页号索引时**静默错位**。"""
    with pytest.raises(ValidationError, match="连续"):
        StandardDocument(pages=(_page(page=1), _page(page=3)))


def test_standard_document_round_trips_and_validates_on_read() -> None:
    """落库再读回：内容一致，且**读回时同样校验**。

    只在构造时校验是不够的 —— 工件是从对象存储读回来的，
    一份被改过或写坏的工件必须在读回时被拦住，而不是流到 M8 才暴露。
    """
    document = StandardDocument(pages=(_page(),))

    restored = StandardDocument.model_validate_json(document.model_dump_json())

    assert restored == document

    bad = document.model_dump_json().replace('"rotation":0', '"rotation":45')
    with pytest.raises(ValidationError):
        StandardDocument.model_validate_json(bad)


# ============================================================
# 5. 渲染变换（纯几何，不需要真跑渲染器）
# ============================================================


def test_render_transform_inverse_maps_pixel_back_to_pdf() -> None:
    """实测基准：`scale=2` 时像素 `(20,40)` 对应 PDF 点 `(10,20)`。

    这个数值来自真实 PyMuPDF 渲染（`get_pixmap(matrix=Matrix(2,2))`），
    不是构造出来的 —— 因此它同时验证了"我们的逆映射与渲染器的正向一致"。
    """
    transform = RenderTransform(
        matrix=(2.0, 0.0, 0.0, 2.0, 0.0, 0.0),
        image_width=400,
        image_height=200,
        dpi=144,
    )

    assert transform.to_pdf(20.0, 40.0) == pytest.approx((10.0, 20.0))


def test_render_transform_bbox_maps_all_four_corners() -> None:
    """旋转 90° 的线性部分含负号，**只映对角两点会得到倒序的框**。

    用实测的 `page.rotation_matrix`（200x100 页面、rotation=90）：
    `Matrix(0, 1, -1, 0, 100, 0)`。
    """
    transform = RenderTransform(
        matrix=(0.0, 1.0, -1.0, 0.0, 100.0, 0.0),
        image_width=100,
        image_height=200,
        dpi=72,
    )

    pdf_bbox = transform.to_pdf_bbox((0.0, 0.0, 100.0, 200.0))

    x0, y0, x1, y1 = pdf_bbox
    assert x0 <= x1 and y0 <= y1, f"框必须归一化，收到 {pdf_bbox}"
    assert pdf_bbox == pytest.approx((0.0, 0.0, 200.0, 100.0))


def test_render_transform_rejects_singular_matrix() -> None:
    """不可逆的矩阵必须**当场报错**，而不是除出 `inf` 写进证据坐标。"""
    transform = RenderTransform(
        matrix=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        image_width=10,
        image_height=10,
        dpi=72,
    )

    with pytest.raises(ValueError, match="不可逆"):
        transform.to_pdf(1.0, 1.0)


def test_ocr_gateway_protocol_accepts_a_minimal_implementation() -> None:
    """端口只要求"读字"：实现方不需要知道 PDF、DPI 或页面旋转。"""

    class _FakeOcr:
        engine = "fake"
        version = "1.0"

        def recognize(self, image_png: bytes, *, page: int) -> OcrPageResult:
            return OcrPageResult(
                page=page, lines=(OcrLine(text="采购合同", bbox=(0, 0, 1, 1)),)
            )

        def close(self) -> None:
            pass

    assert isinstance(_FakeOcr(), OCRGateway)
