/**
 * 证据坐标 → 视口坐标的**唯一**变换（M8 Task 5）。
 *
 * ## 后端给的坐标系（先把这件事说清，否则写出来的变换一定是错的）
 *
 * `app/ports/parse_document.py`：
 *
 * - `bbox_space` 恒为 `"pdf-point-top-left"` —— **PDF 点、左上角原点**；
 * - `DocumentPage.width` / `height` 对应**旋转后可见页面**，
 *   且 `bbox` 一律**已换算到该空间**。
 *
 * ## ⚠️ 因此这里**不做旋转**（本文件最容易写错的一处）
 *
 * 计划里写着"写 0/90/180/270 旋转的数值测试"，很容易顺势在这里实现一个
 * `rotateBbox(bbox, rotation)`。那是**重复应用**：后端已经旋转过了。
 *
 * 这个错误的可怕之处在于它**几乎总是看不出来**：
 *
 * | 文档 | 表现 |
 * | --- | --- |
 * | `rotation = 0`（绝大多数） | 完全相同 —— 测试与肉眼都发现不了 |
 * | `rotation = 90/270`（扫描件） | 框整体错位、宽高互换，而页面**看起来正常** |
 *
 * 于是"我们支持旋转文档"这句话在真实扫描件到来之前一直是成立的。
 *
 * 正确做法：旋转**只发生在一个地方** —— 构造 PDF 渲染视口时，
 * 用**文档给的** `rotation`（而不是 PDF 自己的 `/Rotate`），
 * 使渲染方向与 bbox 所在的空间一致；此后几何只剩缩放。
 * 见 `pageRotationForRender()` 与 `PdfPageCanvas`。
 *
 * ## 另一条纪律：画不出框时**如实说**，不要就近画一个
 *
 * `clipRect()` 完全出界时返回 `null`（而不是零面积矩形）。
 * 一个零面积的框在界面上与"这里有证据"长得一样，而它什么都没指。
 */

import type { BBox, DocumentBlock, DocumentPage, EvidenceSpan } from '../../api/contracts'

/** 渲染视口（CSS 像素）。宽高必须与页面**同比例**，否则缩放不是单一的。 */
export interface Viewport {
  readonly width: number
  readonly height: number
}

export interface Rect {
  readonly x: number
  readonly y: number
  readonly width: number
  readonly height: number
}

/** 页几何（`DocumentPage` 里与坐标有关的那几项）。 */
export type PageGeometry = Pick<DocumentPage, 'page' | 'width' | 'height' | 'rotation' | 'bbox_space'>

/**
 * 页面 → 视口的缩放系数。
 *
 * 只取宽度方向：`PageGeometry.width/height` 与视口必然同比例
 * （视口是按同一比例算出来的）。取两个方向的**较小值**看似更"安全"，
 * 实际上会掩盖"视口比例算错了"这件事 —— 框会整体偏移一点，
 * 而没人会去查一个只偏几像素的问题。
 */
export function scaleFor(page: PageGeometry, viewport: Viewport): number {
  if (page.width <= 0) {
    throw new Error(`页宽必须为正，收到 ${page.width}`)
  }
  return viewport.width / page.width
}

/**
 * 证据/块的 bbox → 视口矩形（**只做缩放，不做旋转**，理由见文件头）。
 *
 * `padding` 让框比字符本身大一点点，便于看见 —— 但调用方**不得**用它
 * 去覆盖相邻文本：证据框的价值在于"它指的正好是那一段"。
 */
export function toViewportRect(
  bbox: BBox,
  page: PageGeometry,
  viewport: Viewport,
  padding = 0,
): Rect {
  const scale = scaleFor(page, viewport)
  const [x0, y0, x1, y1] = bbox
  return {
    x: x0 * scale - padding,
    y: y0 * scale - padding,
    width: (x1 - x0) * scale + padding * 2,
    height: (y1 - y0) * scale + padding * 2,
  }
}

/**
 * 裁到视口内。**完全在视口外时返回 `null`**。
 *
 * ⚠️ 完全出界返回 `null` 而不是零面积矩形：后者在界面上与"这里有一条证据"
 * 无法分辨（用户看到的是"没有框"，而正确结论是"这条证据不在这页/这个位置"）。
 */
export function clipRect(rect: Rect, viewport: Viewport): Rect | null {
  const x0 = Math.max(0, rect.x)
  const y0 = Math.max(0, rect.y)
  const x1 = Math.min(viewport.width, rect.x + rect.width)
  const y1 = Math.min(viewport.height, rect.y + rect.height)

  if (x1 <= x0 || y1 <= y0) {
    return null
  }
  return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 }
}

/**
 * 交给 PDF 渲染器的旋转角 —— **必须取文档的值**，不要取 PDF 自己的 `/Rotate`。
 *
 * 取了 PDF 的 `/Rotate` 时，两份旋转信息（文档的与 PDF 的）不一致的页
 * 会渲染成与 bbox 空间不同的方向，而框就整体错位；
 * 那种页在文档里是少数，因此同样"长期看不出来"。
 */
export function pageRotationForRender(page: PageGeometry): 0 | 90 | 180 | 270 {
  return page.rotation
}

/** 若干 bbox 的并集；空输入返回 `null`（没有证据就是没有证据）。 */
export function unionBbox(boxes: readonly BBox[]): BBox | null {
  if (boxes.length === 0) {
    return null
  }
  let [x0, y0, x1, y1] = boxes[0] as BBox
  for (const box of boxes) {
    x0 = Math.min(x0, box[0])
    y0 = Math.min(y0, box[1])
    x1 = Math.max(x1, box[2])
    y1 = Math.max(y1, box[3])
  }
  return [x0, y0, x1, y1]
}

/**
 * 运行时窄化：JSON 里的 `bbox` → `BBox`。
 *
 * 后端构造期已校验（`_Frozen._bbox_normalized`），但前端拿到的是
 * `unknown`：TS 不校验运行时。返回 `null` 时界面**如实说"没有可用坐标"**，
 * 而不是拿四个 `NaN` 去画一个看不见的框。
 */
export function readBbox(raw: unknown): BBox | null {
  if (!Array.isArray(raw) || raw.length !== 4) {
    return null
  }
  const numbers = raw.map((value) => (typeof value === 'number' ? value : Number.NaN))
  if (!numbers.every((value) => Number.isFinite(value))) {
    return null
  }
  const [x0, y0, x1, y1] = numbers as [number, number, number, number]
  if (x0 > x1 || y0 > y1) {
    // 倒序框"看起来仍像个框"（四个数、量级也对），只是画出来是负面积
    return null
  }
  return [x0, y0, x1, y1]
}

/**
 * 一条证据落在**块内**的逐字符矩形（仅当块带逐字符几何时非空）。
 *
 * ## 为什么需要它：跨行证据的并集框会盖住无关内容
 *
 * 一段证据常常跨行（"违约金按日万分之五计算"被换行拆成两行）。
 * 它的 `bbox` 是**并集** —— 那个矩形同时盖住了两行之间的空白与相邻文字，
 * 看起来像"证据是这一整段"，而实际只是其中两处片段。
 *
 * 因此：`bbox_precision === 'char'` 时按字符画（每行各自成框），
 * 否则退回块级单框（并**如实标注**几何精度，见 `evidenceRects`）。
 */
export function charRectsForRange(
  block: DocumentBlock,
  charStart: number,
  charEnd: number,
): readonly BBox[] {
  if (block.chars.length === 0) {
    return []
  }
  return block.chars
    .filter((char) => char.char_start >= charStart && char.char_end <= charEnd)
    .map((char) => char.bbox)
}

/**
 * 这条证据是否属于**当前**这份文档（判据：它指向的块在不在）。
 *
 * ⚠️ 界面上必须能说出这件事。字段来自某次解析、文档来自 `GET /api/parses/{id}/document`，
 * 两者**可能不是同一次**（用户切了版本、或字段来自另一版解析）。
 * 那时证据的 `block_id` 在当前文档里找不到 —— 而如果照旧按 `bbox` 画，
 * 界面会显示"一个位置略微不对的框"，把**版本对不上**这个真问题
 * 伪装成一个像素级的偏差（设计 §4.3 第 3 条：不同版本的证据不得混用）。
 */
export function evidenceBelongsToDocument(
  span: EvidenceSpan,
  blocksById: ReadonlyMap<string, DocumentBlock>,
): boolean {
  return blocksById.has(span.block_id)
}

/**
 * 一条证据 → 要画的矩形（**一行一个**，可能多个）。
 *
 * 各分支都是**如实的能力声明**：
 *
 * | 情形 | 画什么 | 为什么 |
 * | --- | --- | --- |
 * | `char` 且有逐字符几何，**同一行** | 这一行的并集框（一个） | 逐字画框会得到一串相邻小框，视觉上是一排虚线；而它想表达的只是"这一段" |
 * | `char` 但**跨行** | **每行一个**并集框 | 单框并集会盖住两行之间的空白与无关文字，看起来像"证据是这一整块" |
 * | `line` / `block` | 块的整框 | 只有这一档几何，画细了是**假装**有更高精度 |
 * | `bbox_precision = none` | **不画** | 画不出来就说画不出来 |
 * | 块不在当前文档里 | **不画** | 版本对不上（见 `evidenceBelongsToDocument`） |
 */
export function evidenceRects(
  span: EvidenceSpan,
  blocksById: ReadonlyMap<string, DocumentBlock>,
): readonly BBox[] {
  const block = blocksById.get(span.block_id)
  if (block === undefined) {
    return []
  }
  if (span.bbox_precision === 'none') {
    return []
  }
  if (span.bbox_precision === 'char') {
    const rects = charRectsForRange(block, span.char_start, span.char_end)
    const rows = groupByRow(rects)
    if (rows.length > 0) {
      return rows
    }
  }
  return [span.bbox]
}

/**
 * 字符框按**行**合并。
 *
 * 判据是"纵向是否重叠"而不是"`y0` 是否严格相等"：同一行的字符因为字高/上下标
 * 会有几像素差异，用相等判断会把一行拆成好几段（而那样看起来像"证据分成了几块"）。
 */
function groupByRow(rects: readonly BBox[]): readonly BBox[] {
  const rows: BBox[][] = []
  for (const rect of rects) {
    const row = rows.find((candidate) => {
      const first = candidate[0]
      if (first === undefined) {
        return false
      }
      // 纵向有重叠 → 同一行
      return rect[1] < first[3] && first[1] < rect[3]
    })
    if (row === undefined) {
      rows.push([rect])
    } else {
      row.push(rect)
    }
  }
  return rows.map((row) => unionBbox(row)).filter((bbox): bbox is BBox => bbox !== null)
}

/**
 * 证据的**精度说明**（必须与画出来的框一致）。
 *
 * 材料 §4.3：审查人需要知道"这条结论是猜的还是读的"。因此文案里
 * 同时带上文本精度与几何精度 —— 只说"有证据"等于把两种可信度混成一种。
 */
export function precisionLabel(span: EvidenceSpan): string {
  const text =
    span.text_precision === 'char'
      ? '字符级文本'
      : span.text_precision === 'line'
        ? '行级文本'
        : '无文本区间'
  const geometry =
    span.bbox_precision === 'char'
      ? '字符级坐标'
      : span.bbox_precision === 'line'
        ? '行级坐标'
        : span.bbox_precision === 'block'
          ? '块级坐标'
          : '无坐标'
  return `${text} · ${geometry}`
}
