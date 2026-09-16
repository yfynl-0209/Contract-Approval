"""M4 解析适配器测试（T4）。

本文件守住四类**不会自己报错**的失败：

1. **旋转页的坐标空间**：`rawdict` 给的是**未旋转**空间，而标准文档声明的是
   旋转后可见空间（§4.2）。少做一次映射时两个坐标系**各自都合法** ——
   只有在旋转页上才看得出来，表现是"证据框整体偏了"而文本内容完全正确。
2. **渲染参数**：`clip` 让偏移变成**整页平移**；多乘一次 `rotation_matrix`
   把旋转**撤销**。两者在 `rotation = 0` 的页面上**都是对的**，
   而夹具全是 `rotation = 0` —— 因此只能靠源码级断言。
3. **"什么都没找到" ≠ "找到了但读不准"**：合并成同一个空列表，
   会让读不准的页面被判成空白页，于是 `blocked` 门禁被绕过（§4.10）。
4. **两类 OCR 失败的重试性**：模型缺失是**配置错误**（确定性），
   推理抛错是**资源抖动**（瞬时）。搞反任一方向都不会有任何断言失败。
"""

from __future__ import annotations

import ast
import os
import time
from pathlib import Path

import fitz
import pytest

from app.adapters.parse.pymupdf_extractor import PyMuPdfExtractor
from app.adapters.parse.rapidocr_adapter import RapidOcrAdapter
from app.config import PROJECT_ROOT
from app.deadline import DeadlineExceeded, run_with_deadline
from app.enums import ErrorCode
from app.errors import PermanentError, TransientError
from app.ports.ocr_gateway import OcrPageResult, RenderTransform
from app.ports.pdf_extractor import PdfExtractor

PARSE_DIR = PROJECT_ROOT / "app" / "adapters" / "parse"
EXTRACTOR_PATH = PARSE_DIR / "pymupdf_extractor.py"


# ============================================================
# 辅助：现场合成 PDF（夹具全是单页 rotation=0，旋转用例只能现场造）
# ============================================================


def _pdf_bytes(
    *,
    pages: int = 1,
    width: float = 200.0,
    height: float = 100.0,
    text: str = "AB",
    rotation: int = 0,
    cropbox: tuple[float, float, float, float] | None = None,
    text_at: tuple[float, float] = (10.0, 20.0),
) -> bytes:
    doc = fitz.open()
    try:
        for _ in range(pages):
            page = doc.new_page(width=width, height=height)
            if text:
                # ⚠️ 带 CropBox 时文本必须落在**裁剪区之内**：`get_text` 会
                # 按 CropBox 裁掉区外的内容，于是"页面有字但抽不出来"。
                # 这不是缺陷，而是"看不见的内容就不该被抽出来"。
                page.insert_text(fitz.Point(*text_at), text, fontsize=12)
            if cropbox is not None:
                page.set_cropbox(fitz.Rect(*cropbox))
            if rotation:
                page.set_rotation(rotation)
        return doc.tobytes()
    finally:
        doc.close()


# ============================================================
# 1. 页面几何与旋转
# ============================================================


def test_geometry_uses_display_size_and_keeps_unrotated() -> None:
    """`width/height` 是**旋转后可见**尺寸，同时保留未旋转尺寸。

    只有可见尺寸时无法判断"宽高是不是被旋转换过" ——
    `100x200` 究竟来自"窄页面"还是"横页面转了 90°"，两者对 M8 画框的含义相反。
    """
    with PyMuPdfExtractor.open(_pdf_bytes(rotation=90)) as extractor:
        geometry = extractor.geometry(0)

    assert (geometry.width, geometry.height) == (100.0, 200.0)
    assert (geometry.unrotated_width, geometry.unrotated_height) == (200.0, 100.0)
    assert geometry.rotation == 90
    assert geometry.page == 1


def test_unrotated_page_keeps_raw_coordinates() -> None:
    """`rotation = 0` 时映射必须是**恒等**（rotation_matrix 是单位矩阵）。

    这条与下一条是一对：只测旋转页会漏掉"把未旋转页也转一次"的方向错误。
    """
    with PyMuPdfExtractor.open(_pdf_bytes()) as extractor:
        lines = extractor.lines(0)

    assert lines, "现场合成的文本页应当有文本行"
    first = lines[0].chars[0]
    assert first.text == "A"
    assert first.bbox[0] == pytest.approx(10.0, abs=0.5)
    assert first.bbox[1] == pytest.approx(7.1, abs=0.5)


def test_rotated_page_lines_land_in_display_space() -> None:
    """旋转 90° 后，字符 bbox 必须落在**可见矩形**内。

    ⚠️ 实测基准（PyMuPDF 1.25.1）：未旋转空间里的字符 `A` 在 `(10, 7.1)`，
    乘 `page.rotation_matrix` 后是 `(92.9, 10.0)`；而 `get_text` 的原始坐标
    仍是 `(10, 7.1)`。因此"没有做映射"与"做了映射"的 x 相差 80 多个点 ——
    这正是 M8 上表现为"框跑到页面另一头"的那个差异。
    """
    with PyMuPdfExtractor.open(_pdf_bytes(rotation=90)) as extractor:
        geometry = extractor.geometry(0)
        lines = extractor.lines(0)

    assert lines
    for line in lines:
        x0, y0, x1, y1 = line.bbox
        assert 0.0 <= x0 <= x1 <= geometry.width, f"行 bbox 水平越界：{line.bbox}"
        assert 0.0 <= y0 <= y1 <= geometry.height, f"行 bbox 垂直越界：{line.bbox}"
        for char in line.chars:
            cx0, cy0, cx1, cy1 = char.bbox
            assert 0.0 <= cx0 <= cx1 <= geometry.width, f"字符 bbox 越界：{char.bbox}"
            assert 0.0 <= cy0 <= cy1 <= geometry.height, f"字符 bbox 越界：{char.bbox}"

    # 未旋转空间里 x≈10；映射到显示空间后应当靠近右边缘（宽 100）
    first_char = lines[0].chars[0]
    assert first_char.bbox[0] > 50.0, (
        f"字符 {first_char.text!r} 的 bbox={first_char.bbox} 看起来仍是**未旋转**坐标"
    )


def test_lines_are_empty_when_page_has_no_text_layer() -> None:
    """没有文本层返回空元组 —— 那不是错误，而是"需要走 OCR"的信号。"""
    with PyMuPdfExtractor.open(_pdf_bytes(text="")) as extractor:
        assert extractor.lines(0) == ()


def test_cropbox_with_rotation_maps_correctly() -> None:
    """**验收 5 要求的组合用例：非零 CropBox 原点 + 90° 旋转。**

    单独测旋转、单独测偏移**都能靠碰巧通过**，组合起来才不会 ——
    这正是 §4.3 坚持用组合用例取证的原因。

    实测基准（PyMuPDF 1.25.1，介质框 300x200、CropBox `(10,20,210,120)`、rotation 90）
    —— 文本写在 `(20, 40)`，即裁剪区**之内**：

    ```text
    mediabox        = 300x200        ← 未裁剪、未旋转
    cropbox         = (10,20,210,120)
    rect（可见）    = 100x200        ← 已含 CropBox 与旋转
    char 'A' raw    = (10.0, 7.1)    ← 注意：**已相对 CropBox 原点**（20-10, 40-20）
      * rotation_matrix = (92.9, 10.0)
    get_pixmap(Matrix(2,2)) = 200x400 == rect × 2
    ```

    要点：**`page.rect` 已经把 CropBox 算进去了**，`rotation_matrix` 也是
    相对这个 rect 空间定义的，因此"用 rect 做渲染尺寸 + 用 rotation_matrix 映射文本"
    这一套在裁剪页上同样成立 —— 但这句话必须有证据，不能靠推断。

    另一条顺带实测到的行为：**`rawdict` 的坐标是相对 CropBox 原点**的，
    而且 `get_text` 会**裁掉裁剪区之外**的内容（区外的字"看不见"，
    于是也抽不出来）。构造夹具时把文本放在区外，症状是"页面有字却一行都抽不到"。
    """
    with PyMuPdfExtractor.open(
        _pdf_bytes(
            width=300,
            height=200,
            cropbox=(10, 20, 210, 120),
            rotation=90,
            text_at=(20.0, 40.0),
        )
    ) as extractor:
        geometry = extractor.geometry(0)
        lines = extractor.lines(0)
        image = extractor.render(0, dpi=144)

    # 可见尺寸按 CropBox 与旋转计算（100 = 120-20 裁剪高，200 = 210-10 裁剪宽）
    assert (geometry.width, geometry.height) == (100.0, 200.0)
    assert (geometry.unrotated_width, geometry.unrotated_height) == (300.0, 200.0)
    assert geometry.rotation == 90

    # 渲染尺寸严格等于可见矩形 × scale，与未裁剪页面的规则一致
    assert (image.transform.image_width, image.transform.image_height) == (200, 400)

    assert lines
    for line in lines:
        for bbox in [line.bbox, *(char.bbox for char in line.chars)]:
            x0, y0, x1, y1 = bbox
            assert 0.0 <= x0 <= x1 <= geometry.width, f"水平越界：{bbox}"
            assert 0.0 <= y0 <= y1 <= geometry.height, f"垂直越界：{bbox}"

    # 钉死整条链路：未做映射时 x≈10（贴着 CropBox 原点），映射后右缘到 92.9。
    # ⚠️ 基准值 92.9 是**角点映射**的结果，对应的是归一化 bbox 的 max x（`bbox[2]`），
    # 而不是 `bbox[0]` —— 旋转 90° 后一个字符的四个角在 x 上跨开，
    # 取 min x 得到的是 76.412。初版把角点值当成了 bbox 下界，断言自然失败；
    # 那是**期望值取错**，不是实现错。
    first = lines[0].chars[0]
    assert first.bbox[2] == pytest.approx(92.9, abs=1.0), (
        f"字符 {first.text!r} 的 bbox={first.bbox} 与实测基准不符"
    )
    assert first.bbox[1] == pytest.approx(10.0, abs=1.0)
    assert first.bbox[0] < first.bbox[2]

    # 像素 → PDF 点也必须在同一个 rect 空间里
    assert image.transform.to_pdf(200.0, 400.0) == pytest.approx((100.0, 200.0))


# ============================================================
# 2. 渲染契约
# ============================================================


def test_render_size_follows_display_rect() -> None:
    """渲染尺寸 = **可见矩形** × scale —— 证明 `get_pixmap` 已含旋转。

    实测：`200x100` 且 `rotation=90` 的页面，`rect` 是 `100x200`，
    `get_pixmap(Matrix(2,2))` 得到 `200x400`（= `rect × 2`）。
    若渲染出的是 `400x200`（= `mediabox × 2`），说明有人多乘了一次
    `rotation_matrix`，把旋转**撤销**了。
    """
    with PyMuPdfExtractor.open(_pdf_bytes(rotation=90)) as extractor:
        image = extractor.render(0, dpi=144)

    assert (image.transform.image_width, image.transform.image_height) == (200, 400)
    assert image.png[:8] == b"\x89PNG\r\n\x1a\n", "渲染结果应当是 PNG"


def test_render_transform_maps_pixels_back_to_pdf_points() -> None:
    """像素 → PDF 点：`scale = 2` 时像素 `(20, 40)` 对应 PDF 点 `(10, 20)`。"""
    with PyMuPdfExtractor.open(_pdf_bytes()) as extractor:
        image = extractor.render(0, dpi=144)

    assert image.transform.to_pdf(20.0, 40.0) == pytest.approx((10.0, 20.0))


def test_get_pixmap_call_has_no_clip_and_no_manual_rotation() -> None:
    """**源码级**断言：渲染调用不得带 `clip=`，也不得自己补旋转。

    ⚠️ 为什么不能靠"跑一遍看结果"：这两种写法在 `rotation = 0`
    且不裁切的页面上**都是对的**，而**全部夹具都是 `rotation = 0`**。
    只有旋转页或裁剪场景才暴露，而那时症状是"所有证据框一起偏移"。

    断言落在**包含 `get_pixmap` 的那个函数**体内（这里是 `_render_pixmap`），
    作用域最小 —— 改动别处不会误伤这条检查。

    ⚠️ 判据必须是 **AST**，不能是"源码里有没有出现这个词"。
    初版就是按文本判的，结果被 `_render_pixmap` **自己的 docstring** 绊倒 ——
    那段说明里正写着"不得出现 `clip`、也不得出现 `rotation_matrix`"。
    文本级检查分不清"提到"与"用了"。
    """
    source = EXTRACTOR_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    owner: dict[int, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for inner in ast.walk(node):
                owner[id(inner)] = node

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_pixmap"
    ]
    assert len(calls) == 1, f"应当只有一处渲染调用，实际 {len(calls)} 处"

    render_fn = owner[id(calls[0])]

    for call in (n for n in ast.walk(render_fn) if isinstance(n, ast.Call)):
        keywords = {keyword.arg for keyword in call.keywords}
        assert "clip" not in keywords, (
            f"渲染**不得**传 clip（第 {call.lineno} 行）—— "
            "像素原点会变，坐标偏移是整页平移而不是随机误差"
        )

    attributes = {
        node.attr for node in ast.walk(render_fn) if isinstance(node, ast.Attribute)
    }
    assert not attributes & {"rotation_matrix", "derotation_matrix"}, (
        "渲染**不得**再乘 rotation_matrix —— get_pixmap 已含旋转，再乘等于撤销它"
    )


def test_text_path_does_use_rotation_matrix() -> None:
    """反面断言：文本路径**必须**做旋转映射。

    少了它，文本坐标停留在未旋转空间 —— 与 OCR 路径分处两个坐标系，
    而两者单独看都合法。
    """
    source = EXTRACTOR_PATH.read_text(encoding="utf-8")

    assert "rotation_matrix" in source, (
        "文本层必须做未旋转 → 可见空间的映射（rawdict 给的是未旋转坐标）"
    )


# ============================================================
# 3. 异常 PDF 的分类（确定性 vs 瞬时）
# ============================================================


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        # ⚠️ 非 PDF **且** 非图片 → `DOCUMENT_FORMAT_UNRECOGNIZED`，**不是** `PDF_CORRUPT`。
        # T12 之前白名单只有 PDF，说"PDF 损坏"还算说得过去；现在白名单含
        # `image/png` 与 `image/jpeg`（§4.9 修-17），对一个根本不是 PDF 的字节流
        # 说"PDF 损坏"是**指错了对象** —— 而错误码要回答的正是"到底哪里不行"。
        (b"this is definitely not a pdf", ErrorCode.DOCUMENT_FORMAT_UNRECOGNIZED),
        # 带 `%PDF-` 标记却打不开：它**自称**是 PDF，因此仍是 `PDF_CORRUPT`
        (b"%PDF-1.7\n garbage body \n%%EOF", ErrorCode.PDF_CORRUPT),
    ],
)
def test_unopenable_document_is_permanent(data: bytes, expected: ErrorCode) -> None:
    """判据是**这个文件自称是什么**，不是"我们更希望它是什么"。

    两条引用必须都在：只留 `DOCUMENT_FORMAT_UNRECOGNIZED` 会让
    "PDF 文件损坏"这一类在统计里消失；只留 `PDF_CORRUPT` 则会让
    图片类失败被归错原因（对着一个 JPEG 说"PDF 损坏"）。
    """
    with pytest.raises(PermanentError) as excinfo:
        PyMuPdfExtractor.open(data)

    assert excinfo.value.code is expected
    assert excinfo.value.retryable is False, "重试不会让损坏的文件变好"


def test_encrypted_pdf_is_permanent() -> None:
    doc = fitz.open()
    try:
        doc.new_page(width=200, height=100).insert_text(
            fitz.Point(10, 20), "AB", fontsize=12
        )
        data = doc.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner"
        )
    finally:
        doc.close()

    with pytest.raises(PermanentError) as excinfo:
        PyMuPdfExtractor.open(data)

    assert excinfo.value.code is ErrorCode.PDF_ENCRYPTED


def test_too_many_pages_is_permanent() -> None:
    data = _pdf_bytes(pages=3)

    with pytest.raises(PermanentError) as excinfo:
        PyMuPdfExtractor.open(data, max_pages=2)

    assert excinfo.value.code is ErrorCode.PDF_TOO_MANY_PAGES


def test_oversized_render_is_permanent() -> None:
    """超限要在**分配之前**被拒 —— 等 pixmap 建出来再检查，
    进程可能已被 OOM 杀掉，那时连错误码都留不下。"""
    with PyMuPdfExtractor.open(_pdf_bytes(), max_render_pixels=1000) as extractor:
        with pytest.raises(PermanentError) as excinfo:
            extractor.render(0, dpi=200)

    assert excinfo.value.code is ErrorCode.PDF_TOO_LARGE_PIXELS


def test_extractor_is_closed_after_context_exit() -> None:
    with PyMuPdfExtractor.open(_pdf_bytes()) as extractor:
        assert extractor.page_count == 1

    with pytest.raises(RuntimeError, match="已关闭"):
        _ = extractor.page_count


def test_extractor_satisfies_the_port() -> None:
    with PyMuPdfExtractor.open(_pdf_bytes()) as extractor:
        assert isinstance(extractor, PdfExtractor)


# ============================================================
# 4. OCR 适配器（注入假引擎，不加载模型）
# ============================================================


class _FakeEngine:
    """形如 `engine(image) -> (result, elapsed)`，与 RapidOCR 的真实签名一致。"""

    def __init__(self, result: object) -> None:
        self._result = result
        self.calls = 0

    def __call__(self, image: bytes) -> tuple[object, list[float]]:
        self.calls += 1
        return self._result, [0.01]


def _quad(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def test_ocr_maps_quad_boxes_and_filters_by_confidence() -> None:
    engine = _FakeEngine(
        [
            (_quad(0, 0, 10, 5), "采购合同", 0.95),
            (_quad(0, 10, 10, 15), "糊掉的一行", 0.20),
        ]
    )
    adapter = RapidOcrAdapter(min_confidence=0.6, engine=engine)

    result = adapter.recognize(b"fake", page=1)

    assert [line.text for line in result.lines] == ["采购合同"]
    assert result.lines[0].bbox == (0.0, 0.0, 10.0, 5.0)
    assert result.lines[0].confidence == pytest.approx(0.95)
    assert result.page == 1


def test_ocr_tilted_quad_becomes_bounding_box() -> None:
    """倾斜的四点框取**外接矩形**，而不是假设四点就是矩形的四角。

    假设"四点即四角"会得到自相交的框 —— 而它仍然"看起来像个框"。
    """
    engine = _FakeEngine([([[0, 20], [10, 0], [20, 5], [10, 25]], "歪的", 0.9)])
    adapter = RapidOcrAdapter(engine=engine)

    bbox = adapter.recognize(b"fake", page=1).lines[0].bbox

    assert bbox == (0.0, 0.0, 20.0, 25.0)


def test_ocr_distinguishes_nothing_found_from_all_filtered() -> None:
    """**本文件最重要的一条**：空 `lines` 有两种含义，结论相反。

    | 情形 | `lines` | `detected_lines` | 结论 |
    | --- | --- | --- | --- |
    | 引擎什么都没找到 | 空 | `0` | `blank`（确实没内容） |
    | 找到了但全低于阈值 | 空 | `> 0` | **`uncertain`**（有内容但读不准） |

    合并成同一个空列表，会让第二种被判成空白页 ——
    而它的正确处置是 `blocked(OCR_UNRECOGNIZABLE)`（需求 §404）。
    这条通路**不会让任何断言失败**，除非显式区分这两个字段。
    """
    nothing = RapidOcrAdapter(engine=_FakeEngine(None)).recognize(b"fake", page=1)
    all_low = RapidOcrAdapter(min_confidence=0.6, engine=_FakeEngine(
        [(_quad(0, 0, 10, 5), "低置信", 0.11)]
    )).recognize(b"fake", page=1)

    assert nothing.lines == () and nothing.detected_lines == 0
    assert nothing.raw_confidence is None

    assert all_low.lines == (), "低于阈值的行不进入标准文档"
    assert all_low.detected_lines == 1, "但**检出量**必须保留"
    assert all_low.raw_confidence == pytest.approx(0.11)


def test_ocr_page_confidence_is_the_minimum_not_the_mean() -> None:
    """页级置信度取**最小**：均值会让"大部分行清楚、少数行乱码"的页面显得合格，
    而乱码行恰恰是最需要人看的那一行。"""
    engine = _FakeEngine(
        [
            (_quad(0, 0, 10, 5), "很好的一行", 0.99),
            (_quad(0, 10, 10, 15), "勉强的一行", 0.61),
            (_quad(0, 20, 10, 25), "还行的一行", 0.98),
        ]
    )
    result = RapidOcrAdapter(engine=engine).recognize(b"fake", page=1)

    assert result.confidence == pytest.approx(0.61)


def test_ocr_inference_failure_is_transient() -> None:
    """推理抛错按**瞬时**处理，但**不得伪装成超时**。

    ⚠️ 初版把所有异常都记成 `OCR_INFERENCE_TIMEOUT`。后果很具体：
    看板上"超时次数"这个数字失去了意义 —— 而它正是判断 Worker 是否健康的
    直接信号。现在非超时的失败单独记 `OCR_INFERENCE_FAILED`。
    """

    class _Boom:
        def __call__(self, image: bytes) -> object:
            raise RuntimeError("onnxruntime 崩了")

    adapter = RapidOcrAdapter(engine=_Boom())

    with pytest.raises(TransientError) as excinfo:
        adapter.recognize(b"fake", page=3)

    assert excinfo.value.code is ErrorCode.OCR_INFERENCE_FAILED
    assert excinfo.value.retryable is True, (
        "未登记为可重试时，一次资源抖动会把任务打成永久失败"
    )
    assert "3" in str(excinfo.value)


def test_ocr_timeout_is_reported_as_timeout() -> None:
    """**真的**超过执行时限时，才记 `OCR_INFERENCE_TIMEOUT`。

    没有时限时，引擎卡死会让 Worker **永远阻塞** ——
    症状只是"这个作业不动了"，既没有错误码也没有日志。
    """

    class _Slow:
        def __call__(self, image: bytes) -> object:
            time.sleep(5.0)
            return None, None

    adapter = RapidOcrAdapter(engine=_Slow(), timeout_seconds=0.2)

    with pytest.raises(TransientError) as excinfo:
        adapter.recognize(b"fake", page=7)

    assert excinfo.value.code is ErrorCode.OCR_INFERENCE_TIMEOUT
    assert excinfo.value.retryable is True


def test_render_timeout_is_transient_not_corrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """渲染超时必须记 `PDF_RENDER_TIMEOUT`（瞬时），**不是** `PDF_CORRUPT`。

    ⚠️ 初版把所有渲染异常都归成 `PDF_CORRUPT`。后果是：
    一次"渲染太慢"被判成"文件坏了"，本该重试的作业直接进入**确定性失败**；
    而 `PDF_RENDER_TIMEOUT` 变成一个"枚举里有、运行时永远写不出来"的死语义。
    """

    def slow_render(page: object, scale: float) -> object:
        time.sleep(5.0)
        raise AssertionError("不应走到这里")

    monkeypatch.setattr(PyMuPdfExtractor, "_render_pixmap", staticmethod(slow_render))

    with PyMuPdfExtractor.open(_pdf_bytes(), render_timeout_seconds=0.2) as extractor:
        with pytest.raises(TransientError) as excinfo:
            extractor.render(0, dpi=72)

    assert excinfo.value.code is ErrorCode.PDF_RENDER_TIMEOUT
    assert excinfo.value.retryable is True
    assert excinfo.value.code is not ErrorCode.PDF_CORRUPT, "渲染慢不等于文件坏了"


def test_render_failure_is_still_corrupt() -> None:
    """反面：渲染**抛错**（不是超时）仍然是确定性失败。只测超时会漏掉这条。

    ⚠️ 这里直接调用 `_render_pixmap` 并喂一个会抛错的页面对象，
    而不是 monkeypatch 它本身 —— 上一版就是那么写的，结果**把被测的那段
    转换逻辑整个替换掉了**，测的是一个不存在的实现（异常原样穿透，
    断言失败的原因与缺陷无关）。
    """

    class _BrokenPage:
        number = 0

        def get_pixmap(self, **kwargs: object) -> object:
            raise RuntimeError("内容流坏了")

    with pytest.raises(PermanentError) as excinfo:
        PyMuPdfExtractor._render_pixmap(_BrokenPage(), 1.0)

    assert excinfo.value.code is ErrorCode.PDF_CORRUPT
    assert excinfo.value.retryable is False


# ============================================================
# 6. 执行时限工具
# ============================================================


def test_deadline_returns_the_value() -> None:
    assert run_with_deadline(lambda: 42, timeout=5.0, label="fast") == 42


def test_deadline_raises_on_timeout() -> None:
    def slow() -> int:
        time.sleep(5.0)
        return 1

    with pytest.raises(DeadlineExceeded) as excinfo:
        run_with_deadline(slow, timeout=0.1, label="render-page-3")

    # 标签与超时值必须带上：反复超时时要靠它看出是哪一步慢
    assert "render-page-3" in str(excinfo.value)
    assert "0.1" in str(excinfo.value)


def test_deadline_propagates_the_original_error() -> None:
    """调用本身失败时**原样透传** —— 调用方靠这个区分"超时"与"失败"。"""

    def broken() -> int:
        raise ValueError("原始错误")

    with pytest.raises(ValueError, match="原始错误"):
        run_with_deadline(broken, timeout=5.0, label="broken")


def test_ocr_missing_engine_is_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    """引擎/模型缺失是**配置错误**，必须确定性失败。

    登记成可重试会让它重试到预算耗尽，把"模型路径写错"掩盖成"服务不稳定"。
    """
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "rapidocr_onnxruntime":
            raise ImportError("simulated missing package")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(PermanentError) as excinfo:
        RapidOcrAdapter()

    assert excinfo.value.code is ErrorCode.OCR_MODEL_UNAVAILABLE
    assert excinfo.value.retryable is False


def test_ocr_adapter_reports_engine_identity() -> None:
    """引擎名与版本进缓存键 —— 换引擎/换模型必须让缓存失效。"""
    adapter = RapidOcrAdapter(engine=_FakeEngine(None))

    assert adapter.engine == "rapidocr_onnxruntime"
    assert adapter.version


def test_ocr_result_carries_transform_context_separately() -> None:
    """几何信息在 `RenderTransform`，不重复挂在结果上。

    `OcrPageResult` 上**没有** `image_width`：只有那几项无法换回 PDF 点，
    换算是 `Point * ~matrix`（修-14 的全部要点）。
    """
    result = OcrPageResult(page=1, lines=())

    assert not hasattr(result, "image_width")
    transform = RenderTransform(
        matrix=(2.0, 0.0, 0.0, 2.0, 0.0, 0.0),
        image_width=10,
        image_height=10,
        dpi=144,
    )
    assert transform.image_width == 10


# ============================================================
# 5. 真实引擎（默认跳过；设 RUN_SLOW_OCR=1 启用）
# ============================================================


@pytest.mark.skipif(
    os.environ.get("RUN_SLOW_OCR") != "1",
    reason="真实 RapidOCR 推理较慢；设 RUN_SLOW_OCR=1 启用",
)
def test_real_rapidocr_reads_the_scan_fixture() -> None:
    """端到端：把扫描件夹具渲染后交给**真实** RapidOCR，读出中文。

    假引擎验证的是"我们的映射与过滤逻辑"，验证不了"引擎真的能读中文"。
    这条是那个缺口的唯一填补方式，因此它必须存在 —— 只是默认不跑。
    """
    import pathlib

    scan = pathlib.Path(PROJECT_ROOT) / "mock_approval" / "fixtures" / "contract_05_scan.pdf"
    with PyMuPdfExtractor.open(scan.read_bytes()) as extractor:
        image = extractor.render(0, dpi=200)

    adapter = RapidOcrAdapter(min_confidence=0.3)
    try:
        result = adapter.recognize(image.png, page=1)
    finally:
        adapter.close()

    joined = "".join(line.text for line in result.lines)
    assert result.detected_lines > 0, "真实引擎应当检出文本行"
    assert "合同" in joined, f"应当读出中文合同内容，实际读到：{joined[:120]!r}"

    # 坐标必须落在**像素**空间内，且能被变换回 PDF 点
    for line in result.lines:
        x0, y0, x1, y1 = line.bbox
        assert 0 <= x0 <= x1 <= image.transform.image_width
        assert 0 <= y0 <= y1 <= image.transform.image_height
