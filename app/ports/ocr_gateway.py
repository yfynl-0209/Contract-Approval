"""OCR 端口 —— 「给我一张页面图像，还我行级文本与**像素**坐标」。

## 坐标换算不在这里

换算需要渲染矩阵（含裁剪与旋转），属**解析侧**知识（设计文档 §4.3）。
要求每个 OCR 实现各自换算，等于把同一份几何知识复制到每个适配器里 ——
而几何错误的表现是"证据框整体偏移"，看不出是哪一份实现算错的。

因此本端口只负责"读字"，把结果表达在**它自己的像素坐标系**里；
换算统一由 `RenderTransform` 完成，且该对象只在解析管线里构造一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: 归一化包围盒：`(x0, y0, x1, y1)`，约定 `x0 <= x1`、`y0 <= y1`
BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class RenderTransform:
    """一次渲染的**完整**仿射变换与画布尺寸。

    ## 为什么把矩阵与它的逆放在同一个对象里

    正向（PDF 点 → 像素）用于"把已知坐标写到图像上"做验证；
    逆向（像素 → PDF 点）用于把 OCR 结果映回标准文档空间。
    两者必须来自**同一次**渲染 —— 分别构造就可能"正向用 A、逆向用 B"，
    而两半各自都看不出问题。

    `matrix` 是 PDF 的 6 分量仿射 `(a, b, c, d, e, f)`：

    ```text
    X = a*x + c*y + e
    Y = b*x + d*y + f
    ```

    ⚠️ 这里**不持有任何渲染库的矩阵对象**：`app/ports/` 禁止依赖 PyMuPDF 之类
    的第三方库（`tests/test_ports_and_errors.py` 有源码级守卫）。用 6 个 float
    既满足这条纪律，也让逆映射成为**纯 Python 可测**的 —— 不需要真的跑渲染器。
    """

    matrix: tuple[float, float, float, float, float, float]
    image_width: int
    image_height: int
    dpi: int
    #: OCR 结果所处的坐标系。恒为像素左上原点 —— 由渲染方式决定，不是实现的选择。
    bbox_space: str = "image-pixel-top-left"

    def to_pdf(self, x: float, y: float) -> tuple[float, float]:
        """像素坐标 → PDF 点（**旋转后可见空间**，即标准文档的坐标系）。"""
        a, b, c, d, e, f = self.matrix
        det = a * d - b * c
        if det == 0:
            raise ValueError("渲染矩阵不可逆（det=0），无法把像素坐标映回 PDF 点")

        dx, dy = x - e, y - f
        return ((d * dx - c * dy) / det, (-b * dx + a * dy) / det)

    def to_pdf_bbox(self, bbox: BBox) -> BBox:
        """像素包围盒 → PDF 包围盒。

        ⚠️ **四个角都要映**：旋转 90°/270° 时线性部分含负号，
        只映对角两点会得到**倒序**的框（x0 > x1），而它仍然"看起来像个框"。
        """
        x0, y0, x1, y1 = bbox
        xs: list[float] = []
        ys: list[float] = []
        for px, py in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            mx, my = self.to_pdf(px, py)
            xs.append(mx)
            ys.append(my)
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass(frozen=True)
class OcrLine:
    """一行识别结果 —— 坐标是**像素**，左上原点。

    `confidence` 为 `None` 表示引擎不提供置信度。**逐行**置信度是必需的：
    §4.2 的 `ocr_min_confidence` 判的是**单行**质量，用页级均值会让
    "大部分行很清楚、少数行是乱码"的页面被整体放行。
    """

    text: str
    bbox: BBox
    confidence: float | None = None


@dataclass(frozen=True)
class OcrPageResult:
    """一页的 OCR 结果。`confidence` 为 `None` 表示引擎不提供置信度。

    ⚠️ 这里**不含** `image_width` / `image_height` / 坐标空间声明 ——
    那三项（以及更关键的**渲染矩阵**）都在 `RenderTransform` 里。
    §4.1 初稿把它们列在本对象上，但**只有那三项无法换回 PDF 点**：
    换算需要矩阵（`Point * ~matrix`），这正是修-14 的全部要点。
    几何信息随**变换**走而不是随**结果**走，另一个好处是一次渲染只有一份真相。

    ## 为什么 `detected_lines` / `raw_confidence` 必须分开报

    只看 `lines` 无法区分两种**结论相反**的情形：

    | 情形 | `lines` | `detected_lines` | 正确结论 |
    | --- | --- | --- | --- |
    | 引擎什么都没找到 | 空 | `0` | `blank`（确实没内容） |
    | 找到了但置信度全低于阈值 | 空 | `> 0` | **`uncertain`**（有内容但读不准） |

    合并成一个"空列表"会让第二种被当成空白页 —— 而它的正确处置是
    `blocked(OCR_UNRECOGNIZABLE)`。"读不出来"与"没有内容"分不开，
    就是 §4.10 门禁被绕过的那条静默通路。

    `confidence` 取**最小值**而不是均值：均值会让"大部分行很清楚、少数行是乱码"
    的页面显得合格，而乱码行恰恰是最需要人看的那一行。
    """

    page: int  # 1-based
    #: 通过置信度阈值的行 —— 只有这些进入标准文档
    lines: tuple[OcrLine, ...]
    #: 通过阈值的行的**最小**置信度（取最小是保守选择，见下）
    confidence: float | None = None
    #: 引擎**检出**的行数（过滤前）。⚠️ 必须与 `lines` 分开报，见 docstring
    detected_lines: int = 0
    #: 检出行的**最小**置信度（过滤前）
    raw_confidence: float | None = None


@runtime_checkable
class OCRGateway(Protocol):
    """OCR 能力。实现方**不需要**知道 PDF、DPI 或页面旋转。"""

    @property
    def engine(self) -> str:
        """引擎标识（如 `rapidocr`）。参与解析缓存键 —— 换引擎必须让缓存失效。"""
        ...

    @property
    def version(self) -> str:
        """引擎 / 模型版本。同样参与缓存键，理由同上。"""
        ...

    def recognize(self, image_png: bytes, *, page: int) -> OcrPageResult:
        """识别**一页**整页图像。

        Args:
            image_png: PNG 编码的页面位图（整页，不裁切）。
            page: 页号（1-based），仅用于回填结果。

        Raises:
            AppError: 识别失败。**必须带 `.code`** —— 调度器靠它判断"重试还是立即阻塞"，
                没有码的异常只能被当成未知情况处理（重试性登记见 `app/enums.py`）。
        """
        ...

    def close(self) -> None:
        """释放引擎占用的资源（模型常驻内存，长跑进程需要显式释放）。"""
        ...
