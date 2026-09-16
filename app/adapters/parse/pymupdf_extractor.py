"""PyMuPDF 的页面几何、文本层与整页渲染。

## 三条硬约束，全部来自本机实测（PyMuPDF 1.25.1）

**① 渲染不传 `clip`**（修-32）。传了之后像素原点不再等于页面原点，而 `~render`
仍算出页面原点 —— 偏移是**整页平移**、不是随机误差，于是所有证据框一起错位，
而每一页看起来都"处理过了"。需要限定区域时在**像素空间**裁切。
> 避免一个参数，比正确使用它更可靠。

**② 不自己乘 `page.rotation_matrix`**（修-21）。`get_pixmap` **已经处理旋转**：

```text
页面 200x100、rotation = 90
  page.rect                = 100x200   ← 旋转后可见
  page.mediabox            = 200x100   ← 未旋转
  get_pixmap(Matrix(2,2))  = 200x400   ← 恰为 rect×2，旋转已在其中
  get_pixmap(rot_matrix * Matrix(2,2)) = 400x200   ← 旋转被**撤销**，画出横图
```

**③ 两条路径的映射方向不同，且只有一条需要旋转**（实测）：

| 来源 | 原始空间 | 到显示空间 |
| --- | --- | --- |
| `rawdict` 文本 | **未旋转**（字符 `A` 在 `(10, 7.1)`） | `Point * page.rotation_matrix` → `(92.9, 10.0)` |
| 渲染像素（OCR） | 像素，左上原点 | `Point * ~Matrix(scale, scale)` → **仅缩放** |

也就是 **OCR 那条路完全不需要旋转**（渲染已含），文本那条需要。
只处理一条，会让"同一段文字、两种来源"的坐标**分处两个坐标系**，
而两者单独看都合法 —— 于是证据框整体偏移，偏移方向还随页面旋转而变。
"""

from __future__ import annotations

from typing import Any

import fitz

from app.deadline import DeadlineExceeded, run_with_deadline
from app.enums import ErrorCode
from app.errors import PermanentError, TransientError
from app.ports.ocr_gateway import BBox, RenderTransform
from app.ports.pdf_extractor import PageGeometry, PageImage, TextChar, TextLine


#: 引擎版本。**参与 `parser_version` 进而参与 `cache_key`** ——
#: 升级 PyMuPDF 后解析结果可能变化，版本不进缓存键的话，
#: 旧缓存会被继续命中，**升级等于没发生**，而且不报错。
ENGINE_VERSION: str = f"pymupdf-{fitz.VersionBind}"

#: 图片规范化为虚拟 PDF 时**假设**的扫描分辨率（点 = 像素 × 72 / 本值）。
#:
#: ⚠️ 它**不影响坐标换算的正确性** —— 换算用的是 `RenderTransform` 里保存的完整
#: 渲染矩阵，页与像素的比值就在矩阵里（§4.3）。它只决定虚拟页的尺度：
#: 取太高会让虚拟页大到触发 `PDF_TOO_LARGE_PIXELS`（明明图片本身并不大），
#: 取太低会让渲染无谓地上采样（OCR 变慢，精度不变）。
IMAGE_ASSUMED_DPI: float = 300.0


class PyMuPdfExtractor:
    """`PdfExtractor` 的 PyMuPDF 实现。用 `open()` 构造，支持 `with`。"""

    def __init__(
        self,
        *,
        max_pages: int = 300,
        max_render_pixels: int = 60_000_000,
        render_timeout_seconds: float = 30.0,
    ) -> None:
        self._max_pages = max_pages
        self._max_render_pixels = max_render_pixels
        self._render_timeout = render_timeout_seconds
        self._doc: fitz.Document | None = None

    # ============================================================
    # 生命周期
    # ============================================================

    @classmethod
    def open(cls, data: bytes, **options: Any) -> PyMuPdfExtractor:
        """打开 PDF 字节。

        Raises:
            PermanentError: `PDF_CORRUPT`（打不开 / 无页面）、
                `PDF_ENCRYPTED`（需要口令）、`PDF_TOO_MANY_PAGES`。
        """
        extractor = cls(**options)
        extractor._load(data)
        return extractor

    def _load(self, data: bytes) -> None:
        doc = self._open_pdf_or_image(data)

        if doc.page_count == 0:
            doc.close()
            raise PermanentError("文档没有任何页面", code=ErrorCode.PDF_CORRUPT)

        if doc.needs_pass:
            # ⚠️ 判 `needs_pass` 而**不是** `is_encrypted`：后者对
            # "加密但不需要口令"的文档同样为真，而那种文档我们能正常读 ——
            # 用 `is_encrypted` 会把可读的文件拒之门外。
            doc.close()
            raise PermanentError("PDF 已加密且需要口令", code=ErrorCode.PDF_ENCRYPTED)

        if doc.page_count > self._max_pages:
            count = doc.page_count
            doc.close()
            raise PermanentError(
                f"页数 {count} 超过上限 {self._max_pages}",
                code=ErrorCode.PDF_TOO_MANY_PAGES,
            )

        self._doc = doc

    def _open_pdf_or_image(self, data: bytes) -> fitz.Document:
        """打开 PDF；不是 PDF 就按**图片**处理并规范化成单页虚拟 PDF。

        ## 为什么图片要变成虚拟 PDF（§4.9 修-24）

        坐标契约只有**一个**取值：`pdf-point-top-left`。
        若图片走自己的一套坐标，同一份标准文档里就会出现两种坐标系 ——
        而两者单独看都合法，表现是证据框整体偏移，且只对图片类附件出现。
        规范化之后，下游（字段提取器、M8 画框）**完全不需要知道**输入是 PDF 还是图片。

        ## 认不出格式时报哪个码

        取决于这个文件**自称**是什么：

        | 情形 | 错误码 |
        | --- | --- |
        | 带 `%PDF-` 标记却打不开 | `PDF_CORRUPT`（它说自己是 PDF） |
        | 连标记都没有、也不是可解码的图片 | **`DOCUMENT_FORMAT_UNRECOGNIZED`** |

        后者不能报 `PDF_CORRUPT` —— 白名单现在**不止 PDF**，
        说"PDF 损坏"是指错了对象，而错误码要回答的正是"到底哪里不行"。
        """
        # ⚠️ 必须**先按魔数显式分流**，不能让 `fitz.open` 自己去猜。
        #
        # 实测：`fitz.open(stream=png_bytes, filetype="pdf")` **会成功** ——
        # PyMuPDF 按**内容**自动识别格式，`filetype` 参数并不真的约束它。
        # 于是图片会绕过 `_image_to_pdf`，虚拟页尺寸改由**图片自带的 DPI 元数据**
        # 决定，而 `IMAGE_ASSUMED_DPI` 变成一段永远走不到的死代码：
        #
        # - 带 300 dpi 元数据的 PNG → 页面恰好是 A4，看起来"完全正常"，**掩盖了这件事**；
        # - 手机拍的照片（通常**没有** DPI 元数据，按 72 算）→ 页面与像素等大，
        #   4000 像素宽的照片得到 4000 磅宽的页面，渲染时直接撞 `PDF_TOO_LARGE_PIXELS`。
        if data.lstrip()[:5] == b"%PDF-":
            return self._open_pdf(data)

        try:
            return self._image_to_pdf(data)
        except Exception as exc:
            # 不是图片 —— 再给 PDF 一次机会（少数 PDF 前面带杂字节）
            doc: fitz.Document | None = None
            try:
                doc = self._open_pdf(data)
            except PermanentError:
                doc = None
            if doc is not None and doc.page_count > 0:
                return doc
            if doc is not None:
                doc.close()
            raise PermanentError(
                "文件既不是可解析的 PDF，也不是可识别的图片",
                code=ErrorCode.DOCUMENT_FORMAT_UNRECOGNIZED,
            ) from exc

    @staticmethod
    def _open_pdf(data: bytes) -> fitz.Document:
        try:
            return fitz.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise PermanentError(
                "PDF 无法打开（文件损坏）", code=ErrorCode.PDF_CORRUPT
            ) from exc

    @staticmethod
    def _image_to_pdf(data: bytes) -> fitz.Document:
        """图片字节 → **单页虚拟 PDF**（页面尺寸按 `IMAGE_ASSUMED_DPI` 折算）。

        ⚠️ 假设的分辨率**不影响坐标换算的正确性**：换算用的是
        `RenderTransform` 里保存的完整渲染矩阵，页与像素的比值就在矩阵里（§4.3）。
        它只决定"虚拟页的点"与"图片像素"的比例：

        - 取太高 → 虚拟页过大，渲染时触发 `PDF_TOO_LARGE_PIXELS`（明明图片本身不大）；
        - 取太低 → 渲染时无谓地上采样，OCR 变慢而精度不变。

        300 dpi 是扫描件的常见量级。
        """
        pixmap = fitz.Pixmap(data)
        if pixmap.alpha:
            # 带透明通道的 PNG 不能直接贴进页面；先落到 RGB
            pixmap = fitz.Pixmap(fitz.csRGB, pixmap)

        scale = 72.0 / IMAGE_ASSUMED_DPI
        doc = fitz.open()
        page = doc.new_page(
            width=pixmap.width * scale, height=pixmap.height * scale
        )
        page.insert_image(page.rect, pixmap=pixmap)
        return doc

    @property
    def _document(self) -> fitz.Document:
        if self._doc is None:
            raise RuntimeError("PDF 抽取器已关闭（close 之后不得再使用）")
        return self._doc

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    def __enter__(self) -> PyMuPdfExtractor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ============================================================
    # 读取
    # ============================================================

    @property
    def page_count(self) -> int:
        return self._document.page_count

    def geometry(self, index: int) -> PageGeometry:
        page = self._page(index)
        visible = page.rect
        raw = page.mediabox
        return PageGeometry(
            page=index + 1,
            width=round(visible.width, 3),
            height=round(visible.height, 3),
            rotation=int(page.rotation),
            unrotated_width=round(raw.width, 3),
            unrotated_height=round(raw.height, 3),
        )

    def lines(self, index: int) -> tuple[TextLine, ...]:
        """文本行 —— bbox 一律已换算到**旋转后可见空间**。

        页面没有文本层时返回空元组（那不是错误，而是"需要走 OCR"的信号）。
        """
        page = self._page(index)
        # 未旋转空间 → 可见空间。rotation 为 0 时它是单位矩阵，
        # 因此**不需要**分支：少一个分支就少一处可能写反的方向。
        rotate = page.rotation_matrix

        result: list[TextLine] = []
        for block in page.get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                chars = tuple(
                    TextChar(text=char["c"], bbox=_to_display(char["bbox"], rotate))
                    for span in line["spans"]
                    for char in span["chars"]
                )
                if not chars:
                    continue
                result.append(
                    TextLine(bbox=_to_display(line["bbox"], rotate), chars=chars)
                )
        return tuple(result)

    def render(self, index: int, *, dpi: int) -> PageImage:
        """整页渲染（**不裁切**）。

        Raises:
            PermanentError: `PDF_TOO_LARGE_PIXELS`（内存保护）、`PDF_CORRUPT`（渲染失败）。
            TransientError: `PDF_RENDER_TIMEOUT` —— 超过执行时限。**可重试**。
        """
        page = self._page(index)
        scale = dpi / 72.0

        # 在**分配之前**按页面尺寸推算并拒绝：等 pixmap 建出来再检查，
        # 进程可能已经被 OOM 杀掉 —— 那时连错误码都留不下。
        estimate = int(page.rect.width * scale) * int(page.rect.height * scale)
        if estimate > self._max_render_pixels:
            raise PermanentError(
                f"第 {index + 1} 页渲染像素约 {estimate} 超过上限 "
                f"{self._max_render_pixels}（内存保护）",
                code=ErrorCode.PDF_TOO_LARGE_PIXELS,
            )

        try:
            # ⚠️ `get_pixmap` 是**同步 C 调用，无法从外部中断**。没有这道时限时，
            # 一次卡死会让 Worker 永远阻塞，而且不留任何错误码 ——
            # 症状只是"这个作业不动了"。见 `deadline.py` 的能力边界说明。
            pixmap = run_with_deadline(
                lambda: self._render_pixmap(page, scale),
                timeout=self._render_timeout,
                label=f"render-page-{index + 1}",
            )
        except DeadlineExceeded as exc:
            # ⚠️ 必须是**瞬时**错误。归成 `PDF_CORRUPT` 会把"这次渲染太慢"
            # 判成"文件坏了" —— 于是本该重试的作业直接进入确定性失败。
            raise TransientError(
                f"第 {index + 1} 页渲染超过 {self._render_timeout:g} 秒",
                code=ErrorCode.PDF_RENDER_TIMEOUT,
            ) from exc

        try:
            transform = RenderTransform(
                # 只给缩放，**不给 clip、不补旋转**（见模块说明）
                matrix=(scale, 0.0, 0.0, scale, 0.0, 0.0),
                image_width=pixmap.width,
                image_height=pixmap.height,
                dpi=dpi,
            )
            return PageImage(png=pixmap.tobytes("png"), transform=transform)
        finally:
            del pixmap

    # ============================================================
    # 内部
    # ============================================================

    def _page(self, index: int) -> fitz.Page:
        doc = self._document
        if not 0 <= index < doc.page_count:
            raise ValueError(f"页序 {index} 越界（共 {doc.page_count} 页，0-based）")
        return doc[index]

    @staticmethod
    def _render_pixmap(page: fitz.Page, scale: float) -> fitz.Pixmap:
        """**唯一的**渲染调用点。

        刻意做成独立方法：`tests/test_parse_adapters.py` 会做源码级断言 ——
        这个函数体内**不得**出现 `clip`，也不得出现 `rotation_matrix`。
        约束放在最小的作用域里，改动别处不会误伤这条检查。
        """
        try:
            return page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        except Exception as exc:
            raise PermanentError(
                f"第 {page.number + 1} 页渲染失败", code=ErrorCode.PDF_CORRUPT
            ) from exc


def _to_display(bbox: tuple[float, ...], matrix: fitz.Matrix) -> BBox:
    """未旋转空间 bbox → 旋转后可见空间 bbox。

    ⚠️ 乘法顺序是 `Point * Matrix`，写反会直接抛 `ValueError`（这条反而容易发现）。
    ⚠️ **四个角都要映**：旋转 90°/270° 时线性部分含负号，
    只映对角两点会得到**倒序**的框（`x0 > x1`），而它仍然"看起来像个框"。
    """
    x0, y0, x1, y1 = bbox[0], bbox[1], bbox[2], bbox[3]
    xs: list[float] = []
    ys: list[float] = []
    for px, py in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
        point = fitz.Point(px, py) * matrix
        xs.append(point.x)
        ys.append(point.y)
    return (round(min(xs), 3), round(min(ys), 3), round(max(xs), 3), round(max(ys), 3))
