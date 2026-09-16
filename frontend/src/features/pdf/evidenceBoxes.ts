/**
 * 证据高亮框的构造（纯函数，M8 Task 5）。
 *
 * 与 `coordinateTransform.ts` 分开的理由：这里做的是**筛选与呈现决定**
 * （这条证据属于哪一页、要不要标成选中、它有没有坐标），
 * 那里只做几何。混在一起时，"几何错了"与"筛错了页"两种缺陷长得一样。
 */

import type { DocumentBlock, DocumentPage, EvidenceSpan } from '../../api/contracts'
import {
  clipRect,
  evidenceBelongsToDocument,
  evidenceRects,
  precisionLabel,
  toViewportRect,
  type Rect,
  type Viewport,
} from './coordinateTransform'

/** 一个字段（或条款）要显示的证据集合。 */
export interface EvidenceHighlight {
  readonly fieldCode: string
  /** 人读名称（用于无障碍标签与选中提示） */
  readonly label: string
  readonly spans: readonly EvidenceSpan[]
}

export interface EvidenceBox {
  readonly key: string
  readonly fieldCode: string
  readonly label: string
  readonly rect: Rect
  readonly precision: string
  readonly selected: boolean
  /** 证据文本（选中时用于在框旁给出可读内容） */
  readonly text: string
}

/** 一条证据的归属结论 —— 界面据此决定"画框"还是"如实说画不出"。 */
export type EvidencePlacement =
  | { readonly kind: 'drawn'; readonly boxes: readonly EvidenceBox[] }
  | { readonly kind: 'other-page'; readonly page: number }
  | { readonly kind: 'no-geometry' }
  | { readonly kind: 'not-in-document' }

/**
 * 一条证据在当前页上的落点。
 *
 * 四种结论**互斥且穷尽**，界面必须分别给出不同的话：
 * 关键是后三种都不能画框，而它们的原因完全不同
 * （在别的页 / 没有坐标 / 不属于这次解析）。
 */
export function placeEvidence(
  span: EvidenceSpan,
  fieldCode: string,
  label: string,
  page: DocumentPage,
  viewport: Viewport,
  blocksById: ReadonlyMap<string, DocumentBlock>,
  selected: boolean,
): EvidencePlacement {
  if (span.page !== page.page) {
    return { kind: 'other-page', page: span.page }
  }
  if (!evidenceBelongsToDocument(span, blocksById)) {
    return { kind: 'not-in-document' }
  }

  const rects = evidenceRects(span, blocksById)
  if (rects.length === 0) {
    return { kind: 'no-geometry' }
  }

  const boxes: EvidenceBox[] = []
  rects.forEach((bbox, index) => {
    const rect = clipRect(toViewportRect(bbox, page, viewport, 1), viewport)
    if (rect === null) {
      // 完全落在视口外：**不画**，也不假装有一条零面积的证据
      return
    }
    boxes.push({
      key: `${fieldCode}:${span.block_id}:${span.char_start}:${index}`,
      fieldCode,
      label,
      rect,
      precision: precisionLabel(span),
      selected,
      text: span.text,
    })
  })

  if (boxes.length === 0) {
    return { kind: 'no-geometry' }
  }
  return { kind: 'drawn', boxes }
}

/**
 * 当前页上所有字段的证据框。
 *
 * 排序上，**选中项放最后**：同一层级下后出现的元素在上层，
 * 于是选中的框不会被别的字段盖住 —— 而"点了定位却看不见框"
 * 会让人以为定位功能坏了。
 */
export function buildEvidenceBoxes(
  highlights: readonly EvidenceHighlight[],
  page: DocumentPage,
  viewport: Viewport,
  blocksById: ReadonlyMap<string, DocumentBlock>,
  selectedFieldCode: string | null,
): readonly EvidenceBox[] {
  const boxes: EvidenceBox[] = []

  for (const highlight of highlights) {
    const selected = highlight.fieldCode === selectedFieldCode
    for (const span of highlight.spans) {
      const placement = placeEvidence(
        span,
        highlight.fieldCode,
        highlight.label,
        page,
        viewport,
        blocksById,
        selected,
      )
      if (placement.kind === 'drawn') {
        boxes.push(...placement.boxes)
      }
    }
  }

  return [...boxes.filter((box) => !box.selected), ...boxes.filter((box) => box.selected)]
}

/** 文档的全部块（跨页），键为 `block_id` —— 证据靠它找回自己的几何。 */
export function blocksById(
  pages: readonly DocumentPage[],
): ReadonlyMap<string, DocumentBlock> {
  const map = new Map<string, DocumentBlock>()
  for (const page of pages) {
    for (const block of page.blocks) {
      // ⚠️ 重复的 `block_id` 会被后来的覆盖。后端保证 id 唯一（形如 `p3-b12`），
      // 但真出现重复时宁可留下第一份：那是先出现（页号更小）的那一页，
      // 而"永远画在最后一页"会让错误看起来像"这页的证据特别多"
      if (!map.has(block.block_id)) {
        map.set(block.block_id, block)
      }
    }
  }
  return map
}
