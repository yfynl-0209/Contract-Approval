"""标准文档构建与证据定位测试（M4 / T7，设计文档 §4.2 / §4.3 / §4.10）。

本文件守住五类**不会自己报错**的失败：

1. **路由判错**：页面上只有水印文字、正文是图片时，若按"有没有拿到文本"判断，
   会**静默跳过 OCR**，然后拿一行水印当正文去做规则评价 —— 报告看起来完整。
2. **块文本与偏移不一致**：证据区间指向正确文字，而框画在别处。
   两者单独看都正常，只有并排比对才看得出。
3. **OCR 像素坐标没换算**：直接当 PDF 点用，所有证据框整页偏移，
   而文本完全正确。
4. **NFC 映射缺失**：字符框整体向后错位，越到后面偏得越多。
5. **`blank` 与 `uncertain` 混淆**：读不准的页被判成空白页，
   于是 §4.10 的 `blocked` 门禁被绕过。

## 测试分两层，**不要混**

- **内容类**用 `_LOOSE` 阈值：只验"构建是否忠实"。
- **路由类与真实夹具**用**默认**阈值：验"阈值本身对不对"。

混在一起时，"假数据没通过阈值"会伪装成"构建逻辑错了" ——
这一条不是理论：写这份测试时正是如此，6 条内容测试同时失败，
而真正的问题（`min_coverage` 默认 0.3 把**所有**真实文本件都判成需要 OCR）
藏在里面，靠单独测量真实夹具的覆盖率才看出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.adapters.parse.pymupdf_extractor import PyMuPdfExtractor
from app.enums import BboxPrecision, ErrorCode, PageStatus, TextPrecision
from app.ports.ocr_gateway import BBox, OcrLine, OcrPageResult, RenderTransform
from app.ports.pdf_extractor import PageGeometry, PageImage, TextChar, TextLine
from app.services.document_builder import (
    DocumentBuilder,
    document_text,
    locate,
    page_offsets,
    slice_document,
)
from app.workflow.job_inputs import ParseOptions

#: 内容类测试的阈值：**几乎不设限**，只关心构建结果是否忠实。
_LOOSE = ParseOptions(min_chars=1, min_coverage=0.0, max_garbage_ratio=1.0)


# ============================================================
# 假适配器：让路由与坐标逻辑可被精确构造，不必每次合成真实 PDF
# ============================================================


@dataclass
class _FakePage:
    lines: tuple[TextLine, ...] = ()
    width: float = 200.0
    height: float = 100.0
    rotation: int = 0
    image: PageImage | None = None


@dataclass
class _FakeExtractor:
    pages: list[_FakePage] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def geometry(self, index: int) -> PageGeometry:
        page = self.pages[index]
        return PageGeometry(
            page=index + 1,
            width=page.width,
            height=page.height,
            rotation=page.rotation,
            unrotated_width=page.width,
            unrotated_height=page.height,
        )

    def lines(self, index: int) -> tuple[TextLine, ...]:
        return self.pages[index].lines

    def render(self, index: int, *, dpi: int) -> PageImage:
        image = self.pages[index].image
        assert image is not None, "测试没给这一页准备渲染结果"
        return image

    def close(self) -> None:  # pragma: no cover - 假实现
        pass


@dataclass
class _FakeOcr:
    result: OcrPageResult

    @property
    def engine(self) -> str:
        return "fake"

    @property
    def version(self) -> str:
        return "1"

    def recognize(self, image_png: bytes, *, page: int) -> OcrPageResult:
        return self.result

    def close(self) -> None:  # pragma: no cover - 假实现
        pass


def _char(text: str, x0: float, y0: float, x1: float, y1: float) -> TextChar:
    return TextChar(text=text, bbox=(x0, y0, x1, y1))


def _line(text: str, *, x0: float = 10.0, y0: float = 10.0, size: float = 10.0) -> TextLine:
    """一行文本，逐字符 bbox 依次排开。"""
    chars = tuple(
        _char(ch, x0 + index * size, y0, x0 + (index + 1) * size, y0 + size)
        for index, ch in enumerate(text)
    )
    return TextLine(bbox=(x0, y0, x0 + len(text) * size, y0 + size), chars=chars)


def _content(extractor: _FakeExtractor, *, ocr: object = None) -> DocumentBuilder:
    """内容类构建器：阈值放宽，只验构建忠实度。"""
    return DocumentBuilder(extractor, ocr=ocr, options=_LOOSE)  # type: ignore[arg-type]


def _transform(scale: float = 2.0, width: int = 400, height: int = 200) -> RenderTransform:
    return RenderTransform(
        matrix=(scale, 0.0, 0.0, scale, 0.0, 0.0),
        image_width=width,
        image_height=height,
        dpi=144,
    )


def _scan_extractor() -> _FakeExtractor:
    return _FakeExtractor(
        [_FakePage(lines=(), image=PageImage(png=b"x", transform=_transform()))]
    )


def _ocr_ok() -> _FakeOcr:
    return _FakeOcr(
        OcrPageResult(
            page=1,
            lines=(
                OcrLine(text="采购合同", bbox=(20.0, 40.0, 120.0, 60.0), confidence=0.95),
            ),
            confidence=0.95,
            detected_lines=1,
            raw_confidence=0.95,
        )
    )


# ============================================================
# 1. 文本层路径
# ============================================================


def test_text_page_uses_char_precision() -> None:
    """文本层的逐字符 bbox **真实存在**，因此如实标 `char`。"""
    document = _content(
        _FakeExtractor([_FakePage(lines=(_line("采购合同"),))])
    ).build()

    page = document.pages[0]
    assert page.source == "text"
    assert page.page_status is PageStatus.OK
    assert page.text == "采购合同"

    block = page.blocks[0]
    assert block.text_precision is TextPrecision.CHAR
    assert block.bbox_precision is BboxPrecision.CHAR
    assert len(block.chars) == 4, "逐字符几何必须落到 chars 里（否则就是空头承诺）"


def test_block_text_equals_slice_of_page_text() -> None:
    """**验收 1 的数据层形式**：`char_start/char_end` 切出的子串 == 块文本。

    这条在 `DocumentPage` 的校验器里也是不变量 —— 但校验器只在**构造时**生效，
    而这里的切片是事后重新算的，两者一起才算"证据可核验"。
    """
    document = _content(
        _FakeExtractor([_FakePage(lines=(_line("第一条"), _line("第二条")))])
    ).build()

    page = document.pages[0]
    assert page.text == "第一条\n第二条", "块之间的分隔符必须真的存在"
    for block in page.blocks:
        assert page.text[block.char_start : block.char_end] == block.text

    assert page.blocks[1].char_start == len("第一条") + 1


def test_locate_returns_evidence_with_coordinates() -> None:
    """**证据反向匹配**：给一段文字，回答"哪一页、哪个块、哪个区间、bbox"。"""
    document = _content(
        _FakeExtractor([_FakePage(lines=(_line("合同总金额"), _line("人民币 100 元")))])
    ).build()

    evidence = locate(document, "人民币 100 元")

    assert evidence is not None
    assert evidence.page == 1
    assert evidence.block_id == "p1-b1"
    assert evidence.text == "人民币 100 元"
    assert evidence.char_start == len("合同总金额") + 1
    assert evidence.text_precision is TextPrecision.CHAR
    # bbox 取**所在块**的框（块级证据）；不硬编码宽度，免得与假数据耦合
    assert evidence.bbox == document.pages[0].blocks[1].bbox
    assert evidence.bbox[0] == 10.0 and evidence.bbox[3] == 20.0


def test_locate_returns_none_when_absent() -> None:
    """找不到时返回 `None`，**不抛错** —— `not_found` 是结论，不是故障。"""
    document = _content(_FakeExtractor([_FakePage(lines=(_line("采购合同"),))])).build()

    assert locate(document, "违约责任") is None
    assert locate(document, "") is None


def test_slice_document_uses_global_offsets() -> None:
    document = _content(
        _FakeExtractor(
            [
                _FakePage(lines=(_line("AB"),)),
                _FakePage(lines=(_line("CD"),)),
            ]
        )
    ).build()

    assert document_text(document) == "AB\nCD"
    assert page_offsets(document) == (0, 3)
    assert slice_document(document, 0, 2) == "AB"
    assert slice_document(document, 3, 5) == "CD"


# ============================================================
# 2. OCR 路由（用**默认**阈值）
# ============================================================


def test_watermark_only_page_is_routed_to_ocr() -> None:
    """**验收 25 的用例**：只有少量水印文字、正文是图片。

    按"有没有拿到文本"判断时，这一页**有**文本 → 走文本路径 →
    拿一行水印当正文去做规则评价，而报告看起来完整。
    """
    watermark = _line("内部资料", x0=10.0, y0=10.0, size=5.0)
    extractor = _FakeExtractor([_FakePage(lines=(watermark,), width=600.0, height=800.0)])

    route = DocumentBuilder(extractor).route(0)

    assert route.needs_ocr is True
    assert route.reason is not None
    # 4 个字 < min_chars(20) 且覆盖率极低 —— 两项都该被判出来
    assert "min_chars" in route.reason
    assert "min_coverage" in route.reason


def test_normal_density_text_page_is_not_routed_to_ocr() -> None:
    """反面，而且这条**必须用真实量级的数据**：

    真实合同正文的覆盖率只有 0.09~0.13（见 `ParseOptions.min_coverage`）。
    阈值若按"文字该占页面多少"来设，就会把**每一份干净的文本 PDF**
    都判成"要走 OCR" —— 慢、精度掉到行级，没有引擎时直接失败。
    因此这里的假数据刻意贴近期真实密度。
    """
    lines = tuple(
        _line("这是一行足够长的合同正文内容文字", x0=20.0, y0=20.0 + index * 14, size=12.0)
        for index in range(10)
    )
    extractor = _FakeExtractor([_FakePage(lines=lines, width=595.0, height=842.0)])

    route = DocumentBuilder(extractor).route(0)

    assert route.needs_ocr is False, f"正常密度的文本页被判需要 OCR：{route.reason}"
    assert route.reason is None


def test_page_without_text_layer_is_routed_to_ocr() -> None:
    extractor = _FakeExtractor([_FakePage(lines=())])

    route = DocumentBuilder(extractor).route(0)

    assert route.needs_ocr is True
    assert route.reason == "no_text_layer"


def test_missing_ocr_engine_fails_the_page_honestly() -> None:
    """需要 OCR 却没有引擎 → **如实失败**，不拿不完整的文本层冒充成功。

    冒充的表现是"整份文档看起来读到了内容"，而缺的那些页没有任何痕迹。
    """
    document = DocumentBuilder(_FakeExtractor([_FakePage(lines=())]), ocr=None).build()

    page = document.pages[0]
    assert page.page_status is PageStatus.FAILED
    assert page.error_code == ErrorCode.OCR_MODEL_UNAVAILABLE.value


# ============================================================
# 3. OCR 路径与坐标换算
# ============================================================


def test_ocr_page_uses_line_precision_and_maps_pixels() -> None:
    """OCR 只有行级几何 → 标 `line`；坐标必须经 `RenderTransform` 回映射。

    ⚠️ 不换算时，像素坐标被当成 PDF 点 —— 所有证据框整页偏移，
    而文本完全正确。**这不是"没画框"，是"画在错的地方"。**
    """
    document = _content(_scan_extractor(), ocr=_ocr_ok()).build()

    page = document.pages[0]
    assert page.source == "ocr"
    assert page.page_status is PageStatus.OK
    assert page.text == "采购合同"

    block = page.blocks[0]
    assert block.text_precision is TextPrecision.LINE
    assert block.bbox_precision is BboxPrecision.LINE
    assert block.chars == (), "行级识别没有逐字符几何，不得声称有"
    # scale = 2 → 像素 (20,40,120,60) → PDF 点 (10,20,60,30)
    assert block.bbox == (10.0, 20.0, 60.0, 30.0)


def test_ocr_nothing_found_means_blank() -> None:
    """引擎**什么都没找到** → `blank`（可靠识别后确认无文字）。"""
    ocr = _FakeOcr(OcrPageResult(page=1, lines=(), detected_lines=0))

    page = _content(_scan_extractor(), ocr=ocr).build().pages[0]

    assert page.page_status is PageStatus.BLANK
    assert page.text == ""


def test_ocr_low_confidence_means_uncertain_not_blank() -> None:
    """**本文件最重要的一条**：检出了行但全不达标 → `uncertain`，**不是** `blank`。

    两者结论相反：`blank` 是"确实没有内容"，`uncertain` 是"有内容但读不准"。
    合并成"空页"会让 §4.10 的 `blocked(OCR_UNRECOGNIZABLE)` 门禁被绕过 ——
    于是基于一堆读不准的文字产出一份**看起来完整**的报告。
    """
    ocr = _FakeOcr(OcrPageResult(page=1, lines=(), detected_lines=3, raw_confidence=0.12))

    page = _content(_scan_extractor(), ocr=ocr).build().pages[0]

    assert page.page_status is PageStatus.UNCERTAIN
    assert page.page_status is not PageStatus.BLANK


def test_ocr_failure_is_recorded_with_its_code() -> None:
    """OCR 抛错 → 页 `failed` 且带**稳定错误码**，而不是无声地变成空页。"""
    from app.errors import TransientError

    class _Boom:
        @property
        def engine(self) -> str:
            return "boom"

        @property
        def version(self) -> str:
            return "0"

        def recognize(self, image_png: bytes, *, page: int) -> OcrPageResult:
            raise TransientError("推理超时", code=ErrorCode.OCR_INFERENCE_TIMEOUT)

        def close(self) -> None:  # pragma: no cover
            pass

    page = _content(_scan_extractor(), ocr=_Boom()).build().pages[0]

    assert page.page_status is PageStatus.FAILED
    assert page.error_code == ErrorCode.OCR_INFERENCE_TIMEOUT.value


# ============================================================
# 4. NFC 规范化与字符映射
# ============================================================


def test_combining_sequence_yields_span_of_two() -> None:
    """**验收 45**：`e` + 组合重音（2 码位）→ `é`（1 码位），
    且该字符的 bbox == 两个原始字符框的**并集**。

    没有映射时，第 N 个规范化字符找不到对应的原始 bbox ——
    表现是**字符框整体向后错位，越到后面偏得越多**，
    而文本与区间各自都是正确的。
    """
    line = TextLine(
        bbox=(10.0, 8.0, 22.0, 20.0),
        chars=(
            _char("e", 10.0, 10.0, 18.0, 20.0),
            _char("\u0301", 18.0, 8.0, 20.0, 20.0),  # 组合尖音符
        ),
    )
    document = _content(_FakeExtractor([_FakePage(lines=(line,))])).build()

    page = document.pages[0]
    assert page.text == "é", "NFC 应当把两个码位合成一个"
    assert page.blocks[0].text == "é"
    assert len(page.blocks[0].chars) == 1

    span = page.char_map[0]
    assert span.raw_start == 0 and span.raw_end == 2, f"跨度应为 2，实际 {span}"

    char = page.blocks[0].chars[0]
    assert char.text == "é"
    assert char.bbox == (10.0, 8.0, 20.0, 20.0), "必须是两个原始字符框的并集"
    assert char.char_start == 0 and char.char_end == 1


def test_already_composed_text_keeps_span_of_one() -> None:
    """反面：已经是单体字符时跨度必须是 1 —— 否则映射会被过度合并。"""
    document = _content(_FakeExtractor([_FakePage(lines=(_line("é"),))])).build()

    page = document.pages[0]
    assert page.text == "é"
    assert page.char_map[0].raw_start == 0 and page.char_map[0].raw_end == 1


# ============================================================
# 5. 多页与垃圾字符判据
# ============================================================


def test_pages_are_numbered_from_one_and_contiguous() -> None:
    document = _content(
        _FakeExtractor(
            [
                _FakePage(lines=(_line("A"),)),
                _FakePage(lines=(_line("B"),)),
                _FakePage(lines=(_line("C"),)),
            ]
        )
    ).build()

    assert [page.page for page in document.pages] == [1, 2, 3]
    assert document_text(document) == "A\nB\nC"


def test_control_characters_count_as_garbage() -> None:
    """控制字符与替换字符要计入乱码比例。"""
    from app.services.document_builder import _garbage_ratio

    assert _garbage_ratio("正常文本") == 0.0
    assert _garbage_ratio("\ufffd\ufffd\ufffd\ufffd") == 1.0
    assert _garbage_ratio("正常\ufffd") == pytest.approx(1 / 3)


def test_mojibake_is_not_detected_by_this_judgement() -> None:
    """**这是一条记录能力边界的测试，不是断言缺陷已解决。**

    "字体级乱码"（ToUnicode 映射错误）产出的仍然是**可打印**字符，
    本判据抓不到。它靠 `min_chars` / `min_coverage` 间接覆盖，
    或由 M8 的人工确认兜住。

    把它写成断言，是为了让下一个人**不会**以为乱码问题已经解决了。
    """
    from app.services.document_builder import _garbage_ratio

    mojibake = "æˆ‘æ–¹ä¸ºç”²æ–¹"  # UTF-8 被按 Latin-1 解码的典型产物
    assert _garbage_ratio(mojibake) == 0.0, "本判据抓不到字体级乱码 —— 这是已知边界"


def test_cross_block_range_has_no_single_evidence() -> None:
    """跨块的文字不返回"第一个相交的块"的框。

    跨块说明这段文字横跨两行，用一个块级 bbox 代表它会给出**覆盖不到全部文字**
    的框 —— 而它看起来完全正常。
    """
    from app.services.document_builder import block_at

    document = _content(
        _FakeExtractor([_FakePage(lines=(_line("第一条"), _line("第二条")))])
    ).build()
    page = document.pages[0]

    spanning = page.text.find("条\n第")
    assert spanning != -1, f"页明文里应当有跨行片段：{page.text!r}"
    assert block_at(page, spanning, spanning + 3) is None
    assert block_at(page, 0, 3) is not None


# ============================================================
# 6. 与真实适配器的集成（假适配器可能与现实偏离）
# ============================================================


def _real_pdf(name: str) -> bytes:
    from app.config import PROJECT_ROOT

    return (PROJECT_ROOT / "mock_approval" / "fixtures" / name).read_bytes()


def test_real_text_fixture_builds_char_blocks() -> None:
    """**真实文本件 + 默认阈值** —— 这一条是 `min_coverage` 那次缺陷的守卫。

    假数据可以调成任何样子，真实夹具不会。默认阈值若把干净文本件判成
    "要走 OCR"，这条立刻失败。
    """
    with PyMuPdfExtractor.open(_real_pdf("contract_03_dev_no_ip.pdf")) as extractor:
        builder = DocumentBuilder(extractor)
        assert builder.route(0).needs_ocr is False, (
            f"干净文本件被判需要 OCR：{builder.route(0).reason}"
        )
        document = builder.build()

    page = document.pages[0]
    assert page.source == "text"
    assert "软件开发与运维服务合同" in page.text
    assert page.blocks[0].bbox_precision is BboxPrecision.CHAR

    evidence = locate(document, "合同总金额")
    assert evidence is not None
    x0, y0, x1, y1 = evidence.bbox
    assert x1 > x0 and y1 > y0, "证据框必须有面积"


def test_real_scan_fixture_needs_ocr_without_engine() -> None:
    """真实扫描件：没有文本层 → 路由判"需要 OCR"（这里没给引擎，于是如实失败）。"""
    with PyMuPdfExtractor.open(_real_pdf("contract_05_scan.pdf")) as extractor:
        builder = DocumentBuilder(extractor)
        assert builder.route(0).needs_ocr is True
        document = builder.build()

    page = document.pages[0]
    assert page.source == "ocr"
    assert page.page_status is PageStatus.FAILED
    assert page.error_code == ErrorCode.OCR_MODEL_UNAVAILABLE.value


def test_thresholds_come_from_options_not_constants() -> None:
    """阈值来自作业参数。放宽后同一页应改走文本路径 —— 否则它们只是装饰。"""
    watermark = _line("内部资料", x0=10.0, y0=10.0, size=5.0)
    extractor = _FakeExtractor([_FakePage(lines=(watermark,), width=600.0, height=800.0)])

    assert DocumentBuilder(extractor).route(0).needs_ocr is True
    assert (
        DocumentBuilder(extractor, options=ParseOptions(min_chars=1, min_coverage=0.0))
        .route(0)
        .needs_ocr
        is False
    )
