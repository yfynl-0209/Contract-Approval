"""标准文档构建与证据定位（设计文档 §4.2 / §4.3 / §4.10）。

本模块把"一个 PDF"变成"一份带坐标的标准文档"，承担三件事：

## ① 逐页 OCR 路由（§4.2 修-19）

"这一页要不要走 OCR"**不能靠"有没有拿到文本"**这种布尔判断 ——
它会让两类页面**静默跳过 OCR**：

- 页面上只有少量**水印 / 页眉页脚**文字，正文是图片；
- 页面文本层是**乱码**（错误编码嵌入的字体）。

因此用三个可配置阈值（`min_chars` / `min_coverage` / `max_garbage_ratio`），
**任一不满足即走 OCR**，并把不满足的项写进 `route_reason` ——
每一页都能回答"我为什么走了 OCR / 为什么没走"。

## ② 两条坐标路径统一换算（§4.3 修-21）

| 来源 | 原始空间 | 到显示空间 |
| --- | --- | --- |
| 文本层 `rawdict` | **未旋转** | 由适配器经 `rotation_matrix` 映射 |
| 渲染像素（OCR） | 像素，左上原点 | `RenderTransform.to_pdf_bbox` |

本模块**只负责 OCR 那一条**（文本那条在适配器里就完成了）。
两者最终都落在 `pdf-point-top-left`（旋转后可见空间），
因此下游不必知道某段文字来自哪条路径。

## ③ NFC 规范化与字符映射（§4.2 修-20）

区间必须在**规范化之后**的文本上计算，否则同一字符的不同码位会让区间漂移。
但规范化会**改变字符数量**（`e` + 组合重音 → `é`，两个码位变一个），
而 `chars[]` 里的 bbox 来自**原始**字符。没有映射时，第 N 个规范化字符
找不到对应的原始 bbox —— 表现是**字符框整体向后错位，越到后面偏得越多**，
而文本与区间各自都是正确的。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from app.enums import BboxPrecision, ErrorCode, PageStatus, TextPrecision
from app.errors import AppError
from app.ports.ocr_gateway import BBox, OCRGateway
from app.ports.parse_document import (
    BBOX_SPACE,
    DocumentBlock,
    DocumentChar,
    DocumentPage,
    SourceSpan,
    StandardDocument,
)
from app.ports.pdf_extractor import PageGeometry, PdfExtractor, TextChar, TextLine
from app.workflow.job_inputs import ParseOptions

#: 同一页内块（行）之间的分隔符。它**不属于任何块** ——
#: 块区间指向自己的文字，因此切片仍然精确。
BLOCK_SEPARATOR = "\n"

#: 页面正文为空时的 `route_reason`
NO_TEXT_LAYER = "no_text_layer"


@dataclass(frozen=True)
class PageRoute:
    """一页的路由判定 —— **可解释**：必须能回答"为什么走 OCR"。"""

    needs_ocr: bool
    reason: str | None


@dataclass(frozen=True)
class Evidence:
    """证据定位结果（M5 字段提取与 M8 画框消费）。

    `char_start` / `char_end` 是**页内**半开区间，与 `DocumentBlock` 一致 ——
    两者用同一个坐标系，否则"从证据反推块"要多做一次换算，而那种换算
    最容易在跨页时错位。
    """

    page: int
    block_id: str
    char_start: int
    char_end: int
    text: str
    bbox: BBox
    text_precision: TextPrecision
    bbox_precision: BboxPrecision


class DocumentBuilder:
    """把 `PdfExtractor`（+ 可选 `OCRGateway`）构建成 `StandardDocument`。

    Args:
        extractor: PDF 抽取端口。
        ocr: OCR 端口。**可以为 `None`** —— 纯文本 PDF 不需要它；
            而需要它却没有时，那一页会**如实失败**（`OCR_MODEL_UNAVAILABLE`），
            而不是拿一段不完整的文本层冒充成功。
        options: 逐页路由阈值与渲染参数。
    """

    def __init__(
        self,
        extractor: PdfExtractor,
        *,
        ocr: OCRGateway | None = None,
        options: ParseOptions | None = None,
    ) -> None:
        self._extractor = extractor
        self._ocr = ocr
        self._options = options or ParseOptions()

    # ============================================================
    # 入口
    # ============================================================

    def build(self) -> StandardDocument:
        """逐页构建。页号从 1 起连续（`StandardDocument` 会校验）。"""
        pages = [self._build_page(index) for index in range(self._extractor.page_count)]
        return StandardDocument(pages=tuple(pages))

    def route(self, index: int) -> PageRoute:
        """单独取一页的路由判定（便于测试与排障，不必构建整份文档）。"""
        return self._route(self._extractor.geometry(index), self._extractor.lines(index))

    # ============================================================
    # 路由
    # ============================================================

    def _route(self, geometry: PageGeometry, lines: tuple[TextLine, ...]) -> PageRoute:
        """文本层够不够用？—— 三个阈值**任一不满足即走 OCR**。"""
        options = self._options
        chars = [char for line in lines for char in line.chars]

        if not chars:
            # 没有原生文本 ≠ 空白页：扫描件的每一页都是这样，
            # 而它们恰恰最需要 OCR。所以这里只是"文本层不可用"。
            return PageRoute(needs_ocr=True, reason=NO_TEXT_LAYER)

        failed: list[str] = []

        count = len(chars)
        if count < options.min_chars:
            # 少量水印 / 页眉页脚：有字，但正文是图片
            failed.append(f"min_chars({count}<{options.min_chars})")

        page_area = geometry.width * geometry.height
        covered = sum(_area(char.bbox) for char in chars)
        coverage = min(1.0, covered / page_area) if page_area > 0 else 0.0
        if coverage < options.min_coverage:
            # 只有几个字散布在大页面上：文字面积远小于页面
            failed.append(f"min_coverage({coverage:.4f}<{options.min_coverage})")

        garbage = _garbage_ratio("".join(char.text for char in chars))
        if garbage > options.max_garbage_ratio:
            failed.append(f"max_garbage_ratio({garbage:.4f}>{options.max_garbage_ratio})")

        return PageRoute(needs_ocr=bool(failed), reason=",".join(failed) or None)

    # ============================================================
    # 页构建
    # ============================================================

    def _build_page(self, index: int) -> DocumentPage:
        geometry = self._extractor.geometry(index)
        lines = self._extractor.lines(index)
        route = self._route(geometry, lines)

        if route.needs_ocr:
            return self._build_ocr_page(index, geometry, route)
        return self._build_text_page(index, geometry, lines)

    def _base_page(self, index: int, geometry: PageGeometry, **fields: object) -> dict:
        return {
            "page": index + 1,
            "width": geometry.width,
            "height": geometry.height,
            "bbox_space": BBOX_SPACE,
            "rotation": geometry.rotation,
            **fields,
        }

    def _build_text_page(
        self, index: int, geometry: PageGeometry, lines: tuple[TextLine, ...]
    ) -> DocumentPage:
        """文本层路径：逐字符 bbox 真实存在，因此**如实**标 `char`。"""
        blocks: list[DocumentBlock] = []
        char_map: list[SourceSpan] = []
        offset = 0

        for line_no, line in enumerate(lines):
            text, spans, mapped = _normalize_with_map(line.chars)
            if not text:
                continue
            start = offset
            end = start + len(text)
            blocks.append(
                DocumentBlock(
                    block_id=f"p{index + 1}-b{line_no}",
                    text=text,
                    bbox=line.bbox,
                    char_start=start,
                    char_end=end,
                    text_precision=TextPrecision.CHAR,
                    bbox_precision=BboxPrecision.CHAR,
                    chars=tuple(
                        DocumentChar(
                            text=char,
                            bbox=bbox,
                            char_start=start + position,
                            char_end=start + position + 1,
                        )
                        for position, (char, bbox) in enumerate(mapped)
                    ),
                )
            )
            char_map.extend(spans)
            offset = end + len(BLOCK_SEPARATOR)

        return DocumentPage(
            **self._base_page(
                index,
                geometry,
                source="text",
                page_status=PageStatus.OK,
                text=BLOCK_SEPARATOR.join(block.text for block in blocks),
                char_map=tuple(char_map),
                blocks=tuple(blocks),
            )
        )

    def _build_ocr_page(
        self, index: int, geometry: PageGeometry, route: PageRoute
    ) -> DocumentPage:
        """OCR 路径：坐标**只**经 `RenderTransform` 换算，不做任何自己的几何推导。"""
        if self._ocr is None:
            # 需要 OCR 却没有引擎 —— 如实失败。
            # 拿一段不完整的文本层冒充成功，会让"整份文档"看起来读到了内容，
            # 而缺的那些页没有任何痕迹。
            return DocumentPage(
                **self._base_page(
                    index,
                    geometry,
                    source="ocr",
                    page_status=PageStatus.FAILED,
                    route_reason=route.reason,
                    error_code=ErrorCode.OCR_MODEL_UNAVAILABLE.value,
                )
            )

        try:
            image = self._extractor.render(index, dpi=self._options.dpi)
            result = self._ocr.recognize(image.png, page=index + 1)
        except AppError as exc:
            return DocumentPage(
                **self._base_page(
                    index,
                    geometry,
                    source="ocr",
                    page_status=PageStatus.FAILED,
                    route_reason=route.reason,
                    error_code=exc.code.value,
                )
            )

        if result.detected_lines == 0:
            # 引擎什么都没找到 → **可靠识别后确认无文字**。
            # 这与"读不出来"是相反的结论，见下面的 UNCERTAIN。
            return DocumentPage(
                **self._base_page(
                    index,
                    geometry,
                    source="ocr",
                    page_status=PageStatus.BLANK,
                    route_reason=route.reason,
                )
            )

        # ⚠️ 阈值由**作业参数**决定，而不是适配器的默认值：
        # 适配器那道过滤是引擎级的下限，作业级阈值才是本页的判据。
        accepted = [
            line
            for line in result.lines
            if (line.confidence or 0.0) >= self._options.ocr_min_confidence
        ]
        if not accepted:
            # 检出了行，但没有一行达标 —— **有内容但读不准**。
            # 与 BLANK 的区别就是 §4.10 门禁的输入：这里必须判 blocked，
            # 而不是当成"空页"继续往下走。
            return DocumentPage(
                **self._base_page(
                    index,
                    geometry,
                    source="ocr",
                    page_status=PageStatus.UNCERTAIN,
                    route_reason=route.reason,
                )
            )

        blocks = []
        offset = 0
        for line_no, line in enumerate(accepted):
            text = line.text
            start = offset
            end = start + len(text)
            blocks.append(
                DocumentBlock(
                    block_id=f"p{index + 1}-b{line_no}",
                    text=text,
                    # ⚠️ 必须经变换回映射。OCR 给的是**像素**坐标，
                    # 直接当 PDF 点用会让所有证据框错位，而文本完全正确。
                    bbox=image.transform.to_pdf_bbox(line.bbox),
                    char_start=start,
                    char_end=end,
                    # 行级识别没有逐字符几何 —— 如实标 line，不标 char。
                    # 标 char 就是空头承诺：M8 会去画字符框，而数据里没有。
                    text_precision=TextPrecision.LINE,
                    bbox_precision=BboxPrecision.LINE,
                    chars=(),
                )
            )
            offset = end + len(BLOCK_SEPARATOR)

        return DocumentPage(
            **self._base_page(
                index,
                geometry,
                source="ocr",
                page_status=PageStatus.OK,
                text=BLOCK_SEPARATOR.join(block.text for block in blocks),
                route_reason=route.reason,
                blocks=tuple(blocks),
            )
        )


# ============================================================
# 文本与证据
# ============================================================


def document_text(document: StandardDocument) -> str:
    """整份文档的明文（页间以一个 `\\n` 连接，与 §4.2 的约定一致）。"""
    return BLOCK_SEPARATOR.join(page.text for page in document.pages)


def page_offsets(document: StandardDocument) -> tuple[int, ...]:
    """各页明文在 `document_text` 中的起始偏移。"""
    offsets: list[int] = []
    cursor = 0
    for page in document.pages:
        offsets.append(cursor)
        cursor += len(page.text) + len(BLOCK_SEPARATOR)
    return tuple(offsets)


def slice_document(document: StandardDocument, char_start: int, char_end: int) -> str:
    """按**全文**偏移取一段文本。"""
    return document_text(document)[char_start:char_end]


def locate(
    document: StandardDocument, needle: str, *, occurrence: int = 0
) -> Evidence | None:
    """在标准文档里找一段文字，返回带坐标的证据。

    这是"证据定位"的反向入口：M5 拿到一条条款文本（或正则命中），
    需要回答"它在哪一页、哪个块、哪个字符区间、bbox 是多少"。

    Returns:
        `Evidence`；找不到时返回 `None`（**不是**抛错 —— 调用方要能区分
        "没找到"与"出错了"，前者是 `not_found` 的依据）。
    """
    if not needle:
        return None

    for page in document.pages:
        start = page.text.find(needle)
        while start != -1:
            if occurrence == 0:
                return _evidence_at(page, start, start + len(needle))
            occurrence -= 1
            start = page.text.find(needle, start + 1)
    return None


def _evidence_at(page: DocumentPage, start: int, end: int) -> Evidence | None:
    resolved = resolve_span(page, start, end)
    if resolved is None:
        return None
    return Evidence(
        page=page.page,
        block_id=resolved.block.block_id,
        char_start=start,
        char_end=end,
        text=page.text[start:end],
        bbox=resolved.bbox,
        text_precision=resolved.text_precision,
        bbox_precision=resolved.bbox_precision,
    )


def block_at(page: DocumentPage, start: int, end: int) -> DocumentBlock | None:
    """包含 `[start, end)` 的块。

    ⚠️ 跨块的区间返回 `None` 而不是"第一个相交的块"：
    跨块说明这段文字横跨两行（甚至两页），此时用一个块级 bbox 代表它
    会给出**一个覆盖不到全部文字**的框 —— 而它看起来完全正常。
    """
    for block in page.blocks:
        if block.char_start <= start and end <= block.char_end:
            return block
    return None


@dataclass(frozen=True)
class ResolvedSpan:
    """一段页内区间解析出的几何与**如实标注**的精度。"""

    block: DocumentBlock
    bbox: BBox
    text_precision: TextPrecision
    bbox_precision: BboxPrecision


def resolve_span(page: DocumentPage, start: int, end: int) -> ResolvedSpan | None:
    """把页内字符区间解析成 **bbox + 精度**。**这是唯一的解析入口。**

    `locate()` 与字段提取器都调它，各自不再复制一份 ——
    复制的那一份一定会先偏离（本缺陷就是这么来的：两处各写了一遍 `_evidence()`，
    两处都把块 bbox 当成字符级证据返回）。

    ## 为什么必须按精度分派

    文本页的块声明 `bbox_precision = char`，而块上存的是**整行** bbox。
    直接返回块 bbox，就会出现"**声明 char、画出整行**"：
    M8 照 `char` 去画字符框，画出来的却是一整行，而**两边都不报错**。

    | 情形 | bbox | 声明的精度 |
    | --- | --- | --- |
    | 块是 `char` 且区间**正好**是若干完整字符 | 这些字符 bbox 的**并集** | `char` |
    | 块是 `char` 但区间对不齐字符边界 | 整块 bbox | **降级为 `block`** |
    | 块本来就是 `line` / `block` | 整块 bbox | 原样 |

    对不齐时**降级**而不是"凑合着标 char"：区间可能从半个字符开始，
    那时并集会**少半个字** —— 框看起来仍然像个框，只是短了。
    """
    block = block_at(page, start, end)
    if block is None:
        return None

    if block.bbox_precision is not BboxPrecision.CHAR or not block.chars:
        return ResolvedSpan(block, block.bbox, block.text_precision, block.bbox_precision)

    selected = [
        char for char in block.chars if char.char_start >= start and char.char_end <= end
    ]
    aligned = (
        bool(selected)
        and len(selected) == end - start
        and selected[0].char_start == start
        and selected[-1].char_end == end
    )
    if not aligned:
        return ResolvedSpan(block, block.bbox, block.text_precision, BboxPrecision.BLOCK)

    return ResolvedSpan(
        block,
        _union([char.bbox for char in selected]),
        block.text_precision,
        BboxPrecision.CHAR,
    )


# ============================================================
# 内部工具
# ============================================================


def _area(bbox: BBox) -> float:
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _garbage_ratio(text: str) -> float:
    """不可打印字符的占比。

    ⚠️ **能力边界**：本判据只能抓控制字符、格式字符与替换字符（`U+FFFD`）。
    "字体级乱码"（ToUnicode 映射错误）产出的仍然是**可打印**字符，
    本判据抓不到 —— 那种页面靠 `min_chars` / `min_coverage` 间接覆盖，
    或由 M8 的人工确认兜住。**不要**因为这条存在就以为乱码问题已解决。
    """
    if not text:
        return 0.0
    garbage = 0
    for char in text:
        if char.isspace():
            continue
        if char == "\ufffd" or unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co", "Cn"}:
            garbage += 1
    return garbage / len(text)


def _union(bboxes: list[BBox]) -> BBox:
    xs0 = min(box[0] for box in bboxes)
    ys0 = min(box[1] for box in bboxes)
    xs1 = max(box[2] for box in bboxes)
    ys1 = max(box[3] for box in bboxes)
    return (xs0, ys0, xs1, ys1)


def _normalize_with_map(
    chars: tuple[TextChar, ...],
) -> tuple[str, list[SourceSpan], list[tuple[str, BBox]]]:
    """NFC 规范化，并给出"规范化字符 → 原始区间 + 联合 bbox"。

    按**组合序列**切分原文：一个基字符 + 紧随其后的组合标记（`Mn` / `Mc` / `Me`）
    属于同一个簇，NFC 会把它们合成 1..k 个字符。于是每个规范化字符都能指回
    它来自的**整段**原文。

    ⚠️ 一个簇合成多个字符时，它们共享同一个 `SourceSpan` 与同一个联合 bbox ——
    这是**正确**的：那段原文的几何就是那一块，拆不开。

    Returns:
        `(规范化文本, 每个规范化字符的原始区间, 每个规范化字符的 (文本, 联合 bbox))`。
        三个返回值**一一对应**，长度相同。
    """
    normalized: list[str] = []
    spans: list[SourceSpan] = []
    mapped: list[tuple[str, BBox]] = []

    index = 0
    total = len(chars)
    while index < total:
        start = index
        index += 1
        while index < total and unicodedata.combining(chars[index].text):
            index += 1

        composed = unicodedata.normalize("NFC", "".join(c.text for c in chars[start:index]))
        if not composed:
            continue

        span = SourceSpan(raw_start=start, raw_end=index)
        bbox = _union([chars[position].bbox for position in range(start, index)])
        for char in composed:
            normalized.append(char)
            spans.append(span)
            mapped.append((char, bbox))

    return "".join(normalized), spans, mapped


__all__ = [
    "BLOCK_SEPARATOR",
    "DocumentBuilder",
    "Evidence",
    "PageRoute",
    "ResolvedSpan",
    "block_at",
    "document_text",
    "locate",
    "page_offsets",
    "resolve_span",
    "slice_document",
]
