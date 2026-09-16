import { describe, expect, it } from 'vitest'

import type { BBox, DocumentBlock, EvidenceSpan } from '../../api/contracts'
import {
  charRectsForRange,
  clipRect,
  evidenceBelongsToDocument,
  evidenceRects,
  pageRotationForRender,
  precisionLabel,
  readBbox,
  scaleFor,
  toViewportRect,
  type PageGeometry,
} from './coordinateTransform'

/**
 * 坐标变换的数值测试（M8 Task 5）。
 *
 * ## 为什么这一组必须**手算数字**
 *
 * 画框错了的表现是"框的位置看起来不太对"，而"不太对"没法当断言。
 * 因此这里全部用**能口算的例子**：页 600×800、视口 600×800（缩放 1）
 * 或 1200×1600（缩放 2），每个断言都能在纸上验算。
 *
 * ## 本文件最重要的一条：旋转**不在这里做**
 *
 * 计划里要求"写 0/90/180/270 旋转的测试"，很容易顺势实现一个
 * `rotateBbox(...)`。那是**重复应用** —— 后端的 `bbox` 已经换算到
 * 旋转后可见页的空间了（`app/ports/parse_document.py`：
 * "`width`/`height` 对应旋转后可见页面；`bbox` 一律已换算到该空间，
 * M8 直接用它画框，**不得再应用一次 `rotation`**"）。
 *
 * 下面那条 `rotation 不参与变换` 的用例把它钉死：同一个 bbox 在四种
 * `rotation` 下必须得到**完全相同**的矩形。它同时是一个反例的证明 ——
 * 再多转一次时 90°/270° 会把宽高互换，而 rotation=0（绝大多数文档）
 * 完全看不出来，于是这个错误可以活很久。
 */

const PAGE: PageGeometry = {
  page: 1,
  width: 600,
  height: 800,
  rotation: 0,
  bbox_space: 'pdf-point-top-left',
}

const VIEWPORT = { width: 600, height: 800 }

function charBlocks(): readonly DocumentBlock[] {
  // 两行文字，各 3 个字符；行内字符等宽 10，行高 20
  return [
    {
      block_id: 'p1-b1',
      text: '违约金',
      bbox: [100, 200, 130, 220],
      char_start: 0,
      char_end: 3,
      text_precision: 'char',
      bbox_precision: 'char',
      chars: [
        { text: '违', bbox: [100, 200, 110, 220], char_start: 0, char_end: 1 },
        { text: '约', bbox: [110, 200, 120, 220], char_start: 1, char_end: 2 },
        { text: '金', bbox: [120, 200, 130, 220], char_start: 2, char_end: 3 },
      ],
    },
    {
      block_id: 'p1-b2',
      text: '万分之五',
      bbox: [100, 224, 140, 244],
      char_start: 4,
      char_end: 8,
      text_precision: 'char',
      bbox_precision: 'char',
      chars: [
        { text: '万', bbox: [100, 224, 110, 244], char_start: 4, char_end: 5 },
        { text: '分', bbox: [110, 224, 120, 244], char_start: 5, char_end: 6 },
        { text: '之', bbox: [120, 224, 130, 244], char_start: 6, char_end: 7 },
        { text: '五', bbox: [130, 224, 140, 244], char_start: 7, char_end: 8 },
      ],
    },
  ]
}

function span(overrides: Partial<EvidenceSpan> = {}): EvidenceSpan {
  return {
    page: 1,
    block_id: 'p1-b1',
    text: '违约金',
    bbox: [100, 200, 130, 220],
    char_start: 0,
    char_end: 3,
    text_precision: 'char',
    bbox_precision: 'char',
    ...overrides,
  }
}

describe('缩放与原点', () => {
  it('原点在**左上**：y 越大越靠下，不做任何翻转', () => {
    // 同一列的上下两处，下面的那个 y 必须更大
    const top = toViewportRect([100, 100, 200, 120], PAGE, VIEWPORT)
    const bottom = toViewportRect([100, 700, 200, 720], PAGE, VIEWPORT)

    expect(top.y).toBe(100)
    expect(bottom.y).toBe(700)
    expect(bottom.y).toBeGreaterThan(top.y)
  })

  it('缩放 = 视口宽 / 页宽，四个分量一起缩', () => {
    const viewport = { width: 1200, height: 1600 }

    expect(scaleFor(PAGE, viewport)).toBe(2)

    const rect = toViewportRect([100, 200, 130, 220], PAGE, viewport)

    // 手算：x=200、y=400、w=(130-100)*2=60、h=(220-200)*2=40
    expect(rect).toEqual({ x: 200, y: 400, width: 60, height: 40 })
  })

  it('非整数缩放保留小数（不提前取整）', () => {
    const viewport = { width: 300, height: 400 }

    const rect = toViewportRect([100, 200, 101, 201], PAGE, viewport)

    expect(rect.x).toBeCloseTo(50)
    expect(rect.width).toBeCloseTo(0.5)
  })

  it('页宽为 0 时**抛错**，而不是算出 `Infinity` 画一个巨框', () => {
    expect(() => scaleFor({ ...PAGE, width: 0 }, VIEWPORT)).toThrow(/页宽/)
  })

  it('padding 让框略微变大（四下各加 padding）', () => {
    const rect = toViewportRect([100, 200, 130, 220], PAGE, VIEWPORT, 2)

    expect(rect).toEqual({ x: 98, y: 198, width: 34, height: 24 })
  })
})

describe('⚠️ 旋转不参与变换（后端已把 bbox 换算到可见页空间）', () => {
  it('同一个 bbox 在 0/90/180/270 下得到**完全相同**的矩形', () => {
    const bbox: BBox = [100, 200, 300, 250]
    const rects = ([0, 90, 180, 270] as const).map((rotation) =>
      toViewportRect(bbox, { ...PAGE, rotation }, VIEWPORT),
    )

    expect(rects[1]).toEqual(rects[0])
    expect(rects[2]).toEqual(rects[0])
    expect(rects[3]).toEqual(rects[0])
  })

  it('反例：再多转一次 90° 会把宽高互换 —— 而 rotation=0 时完全看不出来', () => {
    const [x0, y0, x1, y1] = [100, 200, 300, 250] as BBox

    // 若在这里实现旋转，90° 的"正确"结果会是这样（宽高互换 + 坐标重排）
    const doubleRotated = {
      width: (y1 - y0) * 1,
      height: (x1 - x0) * 1,
    }

    const actual = toViewportRect([x0, y0, x1, y1], { ...PAGE, rotation: 90 }, VIEWPORT)

    expect(actual.width).toBe(200)
    expect(actual.height).toBe(50)
    // 这才是"重复旋转"会得到的东西 —— 与我们的结果不同，正是本用例的意义
    expect(doubleRotated.width).not.toBe(actual.width)
    expect(doubleRotated.height).not.toBe(actual.height)
  })

  it('旋转角**原样**交给渲染器（取文档的值，不取 PDF 自己的 /Rotate）', () => {
    expect(pageRotationForRender({ ...PAGE, rotation: 0 })).toBe(0)
    expect(pageRotationForRender({ ...PAGE, rotation: 90 })).toBe(90)
    expect(pageRotationForRender({ ...PAGE, rotation: 180 })).toBe(180)
    expect(pageRotationForRender({ ...PAGE, rotation: 270 })).toBe(270)
  })
})

describe('裁剪', () => {
  it('部分出界 → 裁到视口边界', () => {
    const rect = { x: -20, y: 700, width: 100, height: 150 }

    expect(clipRect(rect, VIEWPORT)).toEqual({ x: 0, y: 700, width: 80, height: 100 })
  })

  it('完全出界 → `null`（**不是**零面积矩形）', () => {
    // 零面积矩形在界面上与"这里有一条证据"分辨不出来，而它什么都没指
    expect(clipRect({ x: 700, y: 0, width: 50, height: 50 }, VIEWPORT)).toBeNull()
    expect(clipRect({ x: 0, y: -100, width: 50, height: 50 }, VIEWPORT)).toBeNull()
  })

  it('刚好贴边 → `null`（没有可画的面积）', () => {
    expect(clipRect({ x: 600, y: 0, width: 10, height: 10 }, VIEWPORT)).toBeNull()
  })
})

describe('字符级精度', () => {
  it('只取**完整落在**区间内的字符框', () => {
    const [block] = charBlocks()

    expect(charRectsForRange(block as DocumentBlock, 0, 3)).toHaveLength(3)
    expect(charRectsForRange(block as DocumentBlock, 1, 3)).toHaveLength(2)
    // 区间只覆盖"约"的一部分时**不取**它：不完整字符框会谎报"证据包含这个字"
    expect(charRectsForRange(block as DocumentBlock, 0, 2)).toHaveLength(2)
  })

  it('没有逐字符几何时**退回块框**，而不是画一个空集', () => {
    const blocks = charBlocks()
    const byId = new Map(blocks.map((block) => [block.block_id, block]))
    const lineSpan = span({ bbox_precision: 'line' })

    expect(evidenceRects(lineSpan, byId)).toEqual([[100, 200, 130, 220]])
  })

  it('同一行的字符**合并成一个框**（逐字画框会得到一排小框，只是噪音）', () => {
    const byId = new Map(charBlocks().map((block) => [block.block_id, block]))

    const secondLine = span({
      block_id: 'p1-b2',
      char_start: 4,
      char_end: 8,
      bbox: [100, 224, 140, 244],
    })

    expect(evidenceRects(secondLine, byId)).toEqual([[100, 224, 140, 244]])
  })

  it('⚠️ 跨行证据 → **每行一个**框（单框并集会盖住行间的无关内容）', () => {
    // 一个块里两行（y 300–320 与 y 324–344）：整段证据跨行
    const twoRowBlock: DocumentBlock = {
      block_id: 'p1-b3',
      text: '违约方应支付',
      bbox: [100, 300, 140, 344],
      char_start: 0,
      char_end: 6,
      text_precision: 'char',
      bbox_precision: 'char',
      chars: [
        { text: '违', bbox: [100, 300, 110, 320], char_start: 0, char_end: 1 },
        { text: '约', bbox: [110, 300, 120, 320], char_start: 1, char_end: 2 },
        { text: '方', bbox: [120, 300, 130, 320], char_start: 2, char_end: 3 },
        { text: '应', bbox: [100, 324, 110, 344], char_start: 3, char_end: 4 },
        { text: '支', bbox: [110, 324, 120, 344], char_start: 4, char_end: 5 },
        { text: '付', bbox: [120, 324, 130, 344], char_start: 5, char_end: 6 },
      ],
    }
    const byId = new Map([[twoRowBlock.block_id, twoRowBlock]])

    const rects = evidenceRects(
      span({ block_id: 'p1-b3', char_start: 0, char_end: 6, bbox: [100, 300, 130, 344] }),
      byId,
    )

    expect(rects).toHaveLength(2)
    expect(rects[0]).toEqual([100, 300, 130, 320])
    expect(rects[1]).toEqual([100, 324, 130, 344])
  })

  it('没有逐字符几何时**退回块框**，而不是画一个空集', () => {
    const blocks = charBlocks()
    const byId = new Map(blocks.map((block) => [block.block_id, block]))
    const lineSpan = span({ bbox_precision: 'line' })

    expect(evidenceRects(lineSpan, byId)).toEqual([[100, 200, 130, 220]])
  })

  it('几何精度是 `line` 时**不**画逐字符框（不假装更高精度）', () => {
    const blocks = charBlocks()
    const byId = new Map(blocks.map((block) => [block.block_id, block]))

    const rects = evidenceRects(span({ bbox_precision: 'line' }), byId)

    expect(rects).toHaveLength(1)
  })

  it('`bbox_precision=none` → **不画**', () => {
    const byId = new Map(charBlocks().map((block) => [block.block_id, block]))

    expect(evidenceRects(span({ bbox_precision: 'none' }), byId)).toEqual([])
  })

  it('⚠️ 证据指向的块不在**当前**文档里 → 不画，且要能说出原因', () => {
    const byId = new Map(charBlocks().map((block) => [block.block_id, block]))
    const stale = span({ block_id: 'p9-b9' })

    // 照旧按 bbox 画，会把"字段与文档不是同一次解析"伪装成"框偏了一点"
    expect(evidenceRects(stale, byId)).toEqual([])
    expect(evidenceBelongsToDocument(stale, byId)).toBe(false)
    expect(evidenceBelongsToDocument(span(), byId)).toBe(true)
  })
})

describe('运行时窄化', () => {
  it('`readBbox` 只接受四个有限数且已归一化', () => {
    expect(readBbox([1, 2, 3, 4])).toEqual([1, 2, 3, 4])
    // 倒序框"看起来仍像个框"（四个数、量级也对），只是画出来是负面积
    expect(readBbox([4, 2, 1, 1])).toBeNull()
    expect(readBbox([1, 2, 3])).toBeNull()
    expect(readBbox([1, 2, 3, Number.NaN])).toBeNull()
    expect(readBbox('1,2,3,4')).toBeNull()
    expect(readBbox(null)).toBeNull()
  })
})

describe('精度说明（必须与画出来的框一致）', () => {
  it('文本精度与几何精度**同时**说明', () => {
    expect(precisionLabel(span())).toBe('字符级文本 · 字符级坐标')
    expect(precisionLabel(span({ text_precision: 'line', bbox_precision: 'block' }))).toBe(
      '行级文本 · 块级坐标',
    )
    expect(precisionLabel(span({ text_precision: 'none', bbox_precision: 'none' }))).toBe(
      '无文本区间 · 无坐标',
    )
  })
})
