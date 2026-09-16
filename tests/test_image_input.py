"""图片附件支持（M4 / T12，设计文档 §4.9 修-17 / 修-24，验收 14d / 29）。

## 这两条验收为什么值得单独一个文件

需求 §4.9 明确要求支持 `image/png` / `image/jpeg`。而这一点**很容易被"蒙过去"**：

- OCR 引擎本来就能读图片 —— 于是"OCR 跑通了"会被当成"需求满足了"；
- 但**输入面**没变：附件白名单仍只有 `application/pdf`，
  真实调用会在 `ATTACHMENT_TYPE_NOT_ALLOWED` 上被挡住，**永远走不到 OCR**。

因此要验的不是"引擎能识别图片"，而是**这类附件真的能从入口走到结构化字段**。

## 最要紧的一条：坐标系必须仍然只有一个

图片有自己的坐标原点与像素尺度。若图片走自己的一套坐标，
同一份标准文档里就会出现**两种坐标系** —— 而两者单独看都合法，
表现是证据框整体偏移，**且只对图片类附件出现**（PDF 件完全正常）。
因此 §4.9 修-24 规定：图片**规范化为单页虚拟 PDF**，`bbox_space` 取值域保持不变。
"""

from __future__ import annotations

import os
from pathlib import Path

import fitz
import pytest

from app.adapters.parse.pymupdf_extractor import (
    IMAGE_ASSUMED_DPI,
    PyMuPdfExtractor,
)
from app.config import PROJECT_ROOT, settings
from app.enums import ErrorCode, PageStatus
from app.errors import PermanentError
from app.ports.ocr_gateway import OcrLine, OcrPageResult
from app.ports.parse_document import BBOX_SPACE, DocumentPage, StandardDocument
from app.services.document_builder import DocumentBuilder

FIXTURES_DIR = PROJECT_ROOT / "mock_approval" / "fixtures"


# ============================================================
# 夹具
# ============================================================


def _png_bytes(*, dpi: int = 300, page: int = 0, name: str = "contract_01_clean.pdf") -> bytes:
    """把一份真实夹具的某一页渲染成 PNG。"""
    with fitz.open(FIXTURES_DIR / name) as doc:
        pixmap = doc[page].get_pixmap(dpi=dpi)
        return pixmap.tobytes("png")


def _jpeg_bytes(*, dpi: int = 300, name: str = "contract_01_clean.pdf") -> bytes:
    with fitz.open(FIXTURES_DIR / name) as doc:
        pixmap = doc[0].get_pixmap(dpi=dpi)
        if pixmap.alpha:
            pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
        return pixmap.tobytes("jpg")


def _all_bbox_spaces() -> set[str | None]:
    """遍历**所有**输入形态，收集标准文档里出现过的 `bbox_space`。

    ⚠️ 收集范围必须包含图片：只遍历 PDF 件时，"图片引入了第二种坐标系"
    这件事**一个断言都不会失败** —— 而那恰恰是修-24 要防的东西。
    """
    spaces: set[str | None] = set()

    def collect(document: StandardDocument) -> None:
        for page in document.pages:
            spaces.add(page.bbox_space)

    # PDF 文本件（全部夹具）
    for path in sorted(FIXTURES_DIR.glob("*.pdf")):
        with PyMuPdfExtractor.open(path.read_bytes()) as extractor:
            collect(DocumentBuilder(extractor).build())

    # 图片（PNG / JPEG）
    for data in (_png_bytes(dpi=72), _jpeg_bytes(dpi=72)):
        with PyMuPdfExtractor.open(data) as extractor:
            collect(DocumentBuilder(extractor).build())

    return spaces


# ============================================================
# 1. 白名单（验收 14d 的前置：输入面必须真的开了）
# ============================================================


def test_whitelist_accepts_the_three_required_types() -> None:
    """白名单必须含三项。

    ⚠️ 这条是**前置条件**，不是形式检查：白名单没开时，
    后面所有"图片能识别"的测试都可以照常通过（它们直接调抽取器，
    绕过了附件入口）—— 而真实调用会在入口被拒。
    """
    allowed = settings.allowed_attachment_types

    assert "application/pdf" in allowed
    assert "image/png" in allowed
    assert "image/jpeg" in allowed


# ============================================================
# 2. 图片 → 单页虚拟 PDF（验收 14d）
# ============================================================


@pytest.mark.parametrize("kind", ["png", "jpeg"])
def test_image_becomes_a_one_page_document(kind: str) -> None:
    """图片必须被规范化为**单页**文档，且页面按假设 DPI 折算成磅。"""
    data = _png_bytes() if kind == "png" else _jpeg_bytes()

    with PyMuPdfExtractor.open(data) as extractor:
        assert extractor.page_count == 1

        geometry = extractor.geometry(0)
        # 300 dpi 渲染的 A4 → 2480×3508 像素 → 595×842 磅
        assert geometry.width == pytest.approx(595.0, abs=1.0), (
            f"{kind} 的虚拟页宽度不符：{geometry.width}"
        )
        assert geometry.height == pytest.approx(842.0, abs=1.0)
        assert geometry.rotation == 0

        # 渲染必须可用（OCR 链路的前提）：拿到 PNG 字节 + 一份可逆的变换
        rendered = extractor.render(0, dpi=96)
        assert rendered.png
        assert rendered.transform is not None


def test_assumed_dpi_only_scales_the_virtual_page() -> None:
    """假设 DPI 只改变虚拟页尺度。

    这条守的是"**页面尺度由我们说了算**"这件事 —— 而不是由图片自带的 DPI
    元数据说了算。实测过：`fitz.open(stream=png, filetype="pdf")` 会**成功**
    （PyMuPDF 按内容识别格式），于是图片一度绕过了 `_image_to_pdf`：
    带 300 dpi 元数据的 PNG 得到的页面**恰好也是 A4**，看起来完全正常，
    把"规范化其实没生效"这件事掩盖了过去 ——
    而手机照片（没有 DPI 元数据，按 72 算）会得到与像素等大的页面。

    因此判据是：**同一份内容、不同 dpi 渲染出的图片，虚拟页大小必须成比例**。
    这条在"由元数据决定"的实现下**会失败**（那时两者都会被元数据还原成同一个值）。
    """
    assert IMAGE_ASSUMED_DPI == 300.0

    with PyMuPdfExtractor.open(_png_bytes(dpi=300)) as big:
        width_300 = big.geometry(0).width
    with PyMuPdfExtractor.open(_png_bytes(dpi=150)) as small:
        width_150 = small.geometry(0).width

    assert width_300 == pytest.approx(595.2, abs=1.0)
    assert width_150 == pytest.approx(297.6, abs=1.0), (
        f"150 dpi 的图片虚拟页宽 {width_150} —— 若约为 595，说明尺度由图片自带的 "
        "DPI 元数据决定，`IMAGE_ASSUMED_DPI` 没生效"
    )


def test_unrecognized_bytes_report_the_format_code() -> None:
    """既不是 PDF 也不是图片 → `DOCUMENT_FORMAT_UNRECOGNIZED`。

    ⚠️ **不能报 `PDF_CORRUPT`**：白名单现在不止 PDF，
    说"PDF 损坏"是指错了对象 —— 而错误码要回答的正是"到底哪里不行"。
    """
    with pytest.raises(PermanentError) as excinfo:
        PyMuPdfExtractor.open(b"\x00\x01\x02 definitely not a document \xff\xfe")

    assert excinfo.value.code is ErrorCode.DOCUMENT_FORMAT_UNRECOGNIZED
    assert excinfo.value.retryable is False


def test_bytes_claiming_to_be_pdf_still_report_pdf_corrupt() -> None:
    """带 `%PDF-` 标记却打不开 → 仍然是 `PDF_CORRUPT`（它**自称**是 PDF）。

    这两条是一对：判据是"这个文件自称是什么"，不是"我们更希望它是什么"。
    都报 `DOCUMENT_FORMAT_UNRECOGNIZED` 会让"PDF 文件损坏"这一类
    在统计里消失；都报 `PDF_CORRUPT` 则会让图片类失败被归错原因。
    """
    with pytest.raises(PermanentError) as excinfo:
        PyMuPdfExtractor.open(b"%PDF-1.7\n garbage body \n%%EOF")

    assert excinfo.value.code is ErrorCode.PDF_CORRUPT


# ============================================================
# 3. 坐标系仍是唯一取值（验收 29 / §4.9 修-24）
# ============================================================


def test_bbox_space_has_exactly_one_value_across_all_inputs() -> None:
    """**遍历全部输入形态**后，`bbox_space` 的取值域**只有一个**。

    ⚠️ 断言的是**取值域的大小**，不是"每页都等于某个字面量"：
    后者在新增一种输入形态时不会被触发（新形态可能带着自己的坐标系进来），
    而前者会 —— 这正是修-24 要防的东西。

    只遍历 PDF 件时这条**形同不存在**。
    """
    spaces = _all_bbox_spaces()

    assert spaces == {BBOX_SPACE}, f"出现了一种以上坐标系：{spaces}"


def test_bbox_space_constant_agrees_with_the_model_literal() -> None:
    """常量与 `Literal` 必须一致 —— 否则"唯一取值"就变成**两处各说一套**。

    `BBOX_SPACE` 是给人用的引用点，而 `DocumentPage.bbox_space` 的 `Literal`
    才是真正的强制点。两者一旦漂移，代码里读到的"唯一取值"与
    构造时被接受的值就**不是同一个** —— 而两边各自看都合理。
    这里用"能不能构造出来"来判，而不是比对字符串：后者在
    类型注解被改写时不会失败。
    """
    from app.ports.parse_document import DocumentPage

    page = DocumentPage(
        page=1,
        width=595.0,
        height=842.0,
        bbox_space=BBOX_SPACE,
        rotation=0,
        source="ocr",
        page_status=PageStatus.BLANK,
    )
    assert page.bbox_space == BBOX_SPACE


class _ScriptedOcr:
    """脚本化 OCR：用来证明"图片确实被送进了 OCR"，不验识别质量。"""

    def __init__(self, text: str = "采购合同") -> None:
        self._text = text
        self.calls: list[int] = []

    def recognize(self, image_png: bytes, *, page: int) -> OcrPageResult:
        self.calls.append(page)
        return OcrPageResult(
            page=page,
            lines=(
                OcrLine(text=self._text, bbox=(10.0, 10.0, 90.0, 26.0), confidence=0.99),
            ),
            confidence=0.99,
            detected_lines=1,
            raw_confidence=0.99,
        )


def test_image_page_is_a_normal_document_page() -> None:
    """图片产出的页必须是**普通页面**，与 PDF 页在数据结构上毫无差别。"""
    ocr = _ScriptedOcr()

    with PyMuPdfExtractor.open(_png_bytes(dpi=300)) as extractor:
        document = DocumentBuilder(extractor, ocr=ocr).build()

    assert isinstance(document, StandardDocument)
    page = document.pages[0]
    assert isinstance(page, DocumentPage)
    assert page.page == 1
    assert page.bbox_space == BBOX_SPACE
    assert page.rotation == 0
    # 没有文本层 → 必须走 OCR 路径
    assert page.source == "ocr"
    assert ocr.calls == [1], "图片必须被送去 OCR"
    assert page.page_status is PageStatus.OK
    assert "采购合同" in page.text


def test_image_without_an_engine_fails_honestly() -> None:
    """没有 OCR 引擎时，图片页必须**如实失败** —— 而不是被当成空白页。

    ⚠️ 我初版把这条写成"图片不得判 `failed`"，那是**一条错的断言**：
    图片没有文本层是正常的，但"正常"不等于"一定能读出来"。
    引擎缺失时正确的结论就是 `FAILED` + `OCR_MODEL_UNAVAILABLE`。

    两种错法都要防：

    | 错法 | 后果 |
    | --- | --- |
    | 判成 `blank` | **谎报"这页没有内容"** —— 而它只是没被读过 |
    | 字段判成 `not_found` | 下游以为"合同里没有这一条"，缺条款类规则**误报** |

    因此这里断言的是**三件事同时成立**：走了 OCR、如实失败、错误码指向引擎缺失。
    """
    with PyMuPdfExtractor.open(_png_bytes(dpi=72)) as extractor:
        document = DocumentBuilder(extractor).build()  # 刻意不传 ocr

    page = document.pages[0]
    assert page.route_reason == "no_text_layer", "图片必须被路由到 OCR"
    # `error_code` 在标准文档里是**字符串**（它是序列化后的契约字段）
    assert page.error_code == ErrorCode.OCR_MODEL_UNAVAILABLE.value
    assert page.page_status is PageStatus.FAILED
    assert page.page_status is not PageStatus.BLANK, "不得把『没读过』报成『没有内容』"


# ============================================================
# 4. 端到端：图片真的能读到字（验收 14d，默认跳过）
# ============================================================


@pytest.mark.skipif(
    not os.environ.get("RUN_SLOW_OCR"),
    reason="真实 OCR 推理慢，用 RUN_SLOW_OCR=1 启用",
)
def test_image_goes_through_ocr_end_to_end() -> None:
    """**验收 14d**：`image/png` 走完 OCR 链路，读出真实文字。

    这条才是"受支持"的证据：前面几条只证明"输入面开了、坐标系没变"，
    而这里证明**内容真的读出来了**。
    """
    from app.adapters.parse.rapidocr_adapter import RapidOcrAdapter

    with PyMuPdfExtractor.open(_png_bytes()) as extractor:
        document = DocumentBuilder(extractor, ocr=RapidOcrAdapter()).build()

    page = document.pages[0]
    assert page.page_status is PageStatus.OK
    text = "".join(block.text for block in page.blocks)
    assert "采购合同" in text, f"OCR 未读出预期内容：{text[:120]!r}"
