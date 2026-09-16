"""PDF 抽取端口 —— 页面几何、文本层与整页渲染。

## 为什么与 OCR 端口分开，而不是合成一个

两者的**坐标空间不同**，混在一个接口里最容易出的错就是"把两个坐标系当成一个"：

| 来源 | 原始空间 | 到显示空间需要什么 |
| --- | --- | --- |
| 文本层（`rawdict`） | **未旋转** | 一次旋转映射 |
| 渲染像素（OCR） | 像素，左上原点 | **仅缩放**（渲染已含旋转） |

本端口对外**只承诺一件事**：返回的 bbox 一律已在**旋转后可见空间**
（即 §4.2 声明的 `pdf-point-top-left`）。映射收在实现里，
调用方就不必知道"哪条路径需要旋转、哪条不需要" —— 而这正是本轮反复出错的地方。

## 渲染契约

`render()` **不得**使用 `clip`，也不得自行补旋转（§4.3 修-32 / 修-21）。
需要限定区域时在**像素空间**裁切。本端口不提供"渲染一块"的能力，
是**有意的**：避免一个语义不确定的参数，比正确使用它更可靠。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.ports.ocr_gateway import BBox, RenderTransform


@dataclass(frozen=True)
class PageGeometry:
    """一页的几何。

    `width` / `height` 是**旋转后可见**尺寸（即 `page.rect`），
    `unrotated_*` 是未旋转尺寸（`page.mediabox`）。两者都留着：
    只有可见尺寸时无法判断"宽高是不是被旋转换过"。
    """

    page: int  # 1-based
    width: float
    height: float
    rotation: int  # 0 / 90 / 180 / 270
    unrotated_width: float
    unrotated_height: float


@dataclass(frozen=True)
class TextChar:
    """一个字符及其在**旋转后可见空间**的 bbox。"""

    text: str
    bbox: BBox


@dataclass(frozen=True)
class TextLine:
    """一行文本及其**逐字符**几何 —— 后者是 `BboxPrecision.CHAR` 的数据来源。"""

    bbox: BBox
    chars: tuple[TextChar, ...]

    @property
    def text(self) -> str:
        return "".join(char.text for char in self.chars)


@dataclass(frozen=True)
class PageImage:
    """整页渲染结果 + 回映射变换。

    两者**必须成对使用**：`png` 交给 OCR，`transform` 用来把 OCR 返回的像素坐标
    映回 PDF 点。分开传递时，调用方可能拿 A 页的图配 B 页的变换 ——
    而两页尺寸相同时这种错配**完全看不出来**。
    """

    png: bytes
    transform: RenderTransform


@runtime_checkable
class PdfExtractor(Protocol):
    """PDF 能力。实现方负责把两条坐标路径都归一化到可见空间。"""

    @property
    def page_count(self) -> int:
        """页数（1-based 编号对应 `geometry(0)`）。"""
        ...

    def geometry(self, index: int) -> PageGeometry:
        """第 `index` 页（**0-based**）的几何。"""
        ...

    def lines(self, index: int) -> tuple[TextLine, ...]:
        """第 `index` 页的文本行（bbox 已在旋转后可见空间）。

        页面没有文本层时返回**空元组** —— 那不是错误，而是"需要走 OCR"的信号。
        """
        ...

    def render(self, index: int, *, dpi: int) -> PageImage:
        """整页渲染（**不裁切**，见模块说明）。"""
        ...

    def close(self) -> None:
        """释放文档句柄。"""
        ...
