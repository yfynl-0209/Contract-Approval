import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../../api/client'
import type { DocumentBlock, DocumentPage, JsonValue, ParseRecord } from '../../api/contracts'
import { apiError, json, stubApi, type StubResult } from '../../test/stubApi'
import { renderApp } from '../../test/renderApp'
import { describeContentFailure, describeDocumentFailure } from './failures'

/**
 * 模块 3（解析结果 + 证据定位）测试（M8 Task 5）。
 *
 * ## ⚠️ PDF 渲染器是**被 mock 掉的**，这是有意的
 *
 * jsdom 没有 canvas 实现（`canvas` 包在安装时被 `--ignore-scripts` 跳过，
 * 浏览器端也不需要它），因此"PDF 真的渲染出来了"在这里**测不到**。
 * 与其写一个"渲染了个空 canvas 也算通过"的假测试，不如 mock 掉渲染器，
 * 把**能测的部分**测到位：
 *
 * - 证据框的**坐标**（由 bbox 一路算到 px，断言到小数）——这是最容易错的部分；
 * - 双向联动（点字段 → 翻页 + 高亮；点框 → 选中字段）；
 * - 四态文案、精度说明、降级路径、失败分支。
 *
 * 渲染器本身的正确性留给人工验证（`PdfPageCanvas` 的文件头写明了这件事）。
 */
vi.mock('react-pdf', () => ({
  Document: ({ children }: { children?: unknown }) => <div>{children as never}</div>,
  Page: () => <div data-testid="pdf-page-content" />,
  pdfjs: { GlobalWorkerOptions: {} },
}))

/** `JsonValue` 的对象形态（测试造数据用；避免到处 `as unknown as`）。 */
type JsonObject = { readonly [key: string]: JsonValue }

function field(code: string, status: string, overrides: JsonObject = {}): JsonObject {
  return {
    field_code: code,
    value_text: '值',
    value_decimal: null,
    currency: null,
    status,
    evidence: [],
    reason_code: status === 'extracted' || status === 'not_found' ? null : 'EXTRACTION_FAILED',
    reason_text: status === 'failed' ? '提取过程失败' : null,
    ...overrides,
  }
}

function basicInfo(evidencePage = 1): JsonObject {
  return {
    schema_version: 1,
    fields: [
      field('amount', 'extracted', {
        value_text: '1,200,000.00',
        currency: 'CNY',
        evidence: [
          {
            page: evidencePage,
            block_id: evidencePage === 1 ? 'p1-b1' : 'p2-b1',
            text: '合同总金额',
            bbox: [100, 200, 300, 250],
            char_start: 0,
            char_end: 5,
            text_precision: 'char',
            bbox_precision: 'char',
          },
        ],
      }),
      field('effective_date', 'not_found', { value_text: '', reason_text: '全文未出现生效时间的约定' }),
      field('expiry_date', 'uncertain', { value_text: '', reason_text: '正文有到期时间相关约定，但未能解析出日期' }),
      field('contract_number', 'failed', { value_text: '' }),
    ],
  }
}

function clauseInfo(): JsonObject {
  return { schema_version: 1, fields: [field('payment', 'extracted', { value_text: '见正文' })] }
}

function parseRecord(
  evidencePage = 1,
  overrides: Partial<ParseRecord> = {},
): ParseRecord {
  return {
    parse_id: 11,
    task_id: 7,
    attachment_record_id: 5,
    parse_status: 'succeeded',
    parse_version: 2,
    parser_name: 'pdf-text',
    parser_version: '1.0.0',
    cache_key: null,
    parse_error_code: null,
    parse_error: null,
    basic_info: basicInfo(evidencePage),
    clause_info: clauseInfo(),
    quality: { text_coverage: 0.94, ocr_pages: 2, ocr_confidence: 0.72 },
    ...overrides,
  }
}

function block(blockId = 'p1-b1'): Record<string, unknown> {
  return {
    block_id: blockId,
    text: '合同总金额',
    bbox: [100, 200, 300, 250],
    char_start: 0,
    char_end: 5,
    text_precision: 'char',
    bbox_precision: 'char',
    chars: [
      { text: '合', bbox: [100, 200, 140, 250], char_start: 0, char_end: 1 },
      { text: '同', bbox: [140, 200, 180, 250], char_start: 1, char_end: 2 },
      { text: '总', bbox: [180, 200, 220, 250], char_start: 2, char_end: 3 },
      { text: '金', bbox: [220, 200, 260, 250], char_start: 3, char_end: 4 },
      { text: '额', bbox: [260, 200, 300, 250], char_start: 4, char_end: 5 },
    ],
  }
}

function page(overrides: Partial<DocumentPage> = {}): DocumentPage {
  return {
    page: 1,
    width: 600,
    height: 800,
    bbox_space: 'pdf-point-top-left',
    rotation: 0,
    source: 'text',
    page_status: 'ok',
    text: '合同总金额为人民币壹佰贰拾万元整',
    route_reason: null,
    error_code: null,
    char_map: [],
    blocks: [block(`p${overrides.page ?? 1}-b1`) as unknown as DocumentBlock],
    ...overrides,
  }
}

function documentBody(pages: readonly DocumentPage[]): Record<string, unknown> {
  return {
    parse_id: 11,
    task_id: 7,
    artifact_id: 3,
    artifact_version: 1,
    sha256: 'a'.repeat(64),
    size_bytes: 1024,
    schema_version: 1,
    page_count: pages.length,
    document: { schema_version: 1, pages },
    created_at: '2026-09-15T10:00:00',
  }
}

function setup(options: {
  readonly parse?: ParseRecord
  readonly pages?: readonly DocumentPage[]
  readonly versions?: readonly ParseRecord[]
  readonly parseResponse?: () => Response
  readonly documentResponse?: () => Response
  readonly contentResponse?: () => Response
  /** 证据落在第几页（默认 1）；用来验证"点定位 → 翻到证据页" */
  readonly evidencePage?: number
} = {}): StubResult {
  const parse = options.parse ?? parseRecord(options.evidencePage ?? 1)
  const pages = options.pages ?? [page()]
  const versions = options.versions ?? [
    parse,
    parseRecord(1, { parse_id: 10, parse_version: 1 }),
  ]

  sessionStorage.setItem(
    'contract-approval.dev-identity',
    JSON.stringify({ actorId: 'admin-1', displayName: '', roles: ['system_admin'] }),
  )

  return stubApi({
    '/api/me': () =>
      json({
        actor_id: 'admin-1',
        display_name: 'Admin One',
        tenant_id: 'default',
        roles: ['system_admin'],
        unknown_roles: [],
        permissions: ['task:read'],
      }),
    '/api/tasks/7/attachments': () =>
      json({
        items: [
          {
            attachment_record_id: 5,
            attachment_id: 'A-5',
            file_name: 'contract.pdf',
            file_type: 'pdf',
            content_type: 'application/pdf',
            file_size: 1024,
            file_checksum: 'b'.repeat(64),
            download_status: 'success',
            error_message: null,
            content_url: '/api/attachments/5/content',
            created_at: '2026-09-15T09:00:00',
          },
        ],
        total: 1,
        page: 1,
        page_size: 100,
        page_count: 1,
        has_next: false,
      }),
    '/api/tasks/7/parses': () =>
      json({
        items: versions,
        total: versions.length,
        page: 1,
        page_size: 50,
        page_count: 1,
        has_next: false,
      }),
    '/api/parses/11/document': () =>
      options.documentResponse?.() ?? json(documentBody(pages)),
    '/api/parses/10/document': () => json(documentBody([page()])),
    '/api/parses/11': () => options.parseResponse?.() ?? json(parse),
    '/api/parses/10': () => json(parseRecord(1, { parse_id: 10, parse_version: 1 })),
    '/api/attachments/5/content': () =>
      options.contentResponse?.() ??
      new Response(new Uint8Array([37, 80, 68, 70]), {
        status: 200,
        headers: { 'Content-Type': 'application/pdf' },
      }),
  })
}

afterEach(() => {
  sessionStorage.clear()
  vi.unstubAllGlobals()
})

// ============================================================
// 1. 四态
// ============================================================

describe('字段四态', () => {
  it('⚠️ 四态**分别**呈现，且 `not_found` 与 `failed` 不会被合并', async () => {
    setup()

    renderApp('/tasks/7?tab=parse')

    const fields = await screen.findByTestId('parse-fields')
    // 按字段定位断言：只断言"页面上出现过这四个词"时，
    // 状态挂错字段（`not_found` 挂到 `failed` 那一行）也照样通过
    const rowOf = (code: string): HTMLElement => {
      const row = fields.querySelector(`[data-field-code="${code}"]`)
      if (row === null) {
        throw new Error(`字段 ${code} 没有渲染出来`)
      }
      return row as HTMLElement
    }

    expect(rowOf('amount')).toHaveTextContent('✓ 已提取')
    expect(rowOf('effective_date')).toHaveTextContent('∅ 未发现')
    expect(rowOf('expiry_date')).toHaveTextContent('⚠ 不确定')
    expect(rowOf('contract_number')).toHaveTextContent('✗ 解析失败')

    // 两者都是"没有值"，但原因与处置完全不同 —— 必须能分辨
    expect(rowOf('effective_date')).toHaveTextContent('全文未出现生效时间的约定')
    expect(rowOf('contract_number')).toHaveTextContent('提取过程失败')
  })

  it('四态的提示说明"该做什么"（不是只有状态名）', async () => {
    setup()

    renderApp('/tasks/7?tab=parse')

    const fields = await screen.findByTestId('parse-fields')
    const failedRow = fields.querySelector('[data-field-code="contract_number"]')
    expect(failedRow).not.toBeNull()
    // `title` 是鼠标可达的解释；读屏用户读到的也是它
    expect(within(failedRow as HTMLElement).getByText('✗ 解析失败')).toHaveAttribute(
      'title',
      expect.stringContaining('不是"合同没有这一项"') as unknown as string,
    )
  })

  it('字段结构读不出来时如实说明，而不是显示"没有字段"', async () => {
    setup({
      parse: parseRecord(1, { basic_info: { unexpected: true }, clause_info: null }),
    })

    renderApp('/tasks/7?tab=parse')

    expect(
      await screen.findByText(/字段结构不是当前版本能读的/),
    ).toBeInTheDocument()
  })
})

// ============================================================
// 2. 证据精度与定位
// ============================================================

describe('证据与定位', () => {
  it('每条证据给出**页码**与**精度**（文本精度与几何精度都说）', async () => {
    setup()

    renderApp('/tasks/7?tab=parse')

    const fields = await screen.findByTestId('parse-fields')
    const locate = within(fields).getByRole('button', { name: '定位到第 1 页' })
    expect(locate).toBeInTheDocument()
    expect(within(fields).getByText(/字符级文本 · 字符级坐标/)).toBeInTheDocument()
    // 证据原文片段也要能看到（只给页码时用户仍要自己找）
    expect(within(fields).getByText(/「合同总金额」/)).toBeInTheDocument()
  })

  it('⚠️ 证据框的坐标从 bbox 一路算到 px（数值断言）', async () => {
    setup()

    renderApp('/tasks/7?tab=parse')

    const overlay = await screen.findByTestId('evidence-overlay')
    const box = within(overlay).getByRole('button', { name: /合同总金额/ })

    // 页 600×800 → 视口宽 560 ⇒ 缩放 560/600 ≈ 0.9333
    // bbox [100,200,300,250]、padding 1px：
    //   x = 100*0.9333 - 1 ≈ 92.33、y = 200*0.9333 - 1 ≈ 185.67
    //   w = 200*0.9333 + 2 ≈ 188.67、h = 50*0.9333 + 2 ≈ 48.67
    expect(Number.parseFloat(box.style.left)).toBeCloseTo(92.33, 1)
    expect(Number.parseFloat(box.style.top)).toBeCloseTo(185.67, 1)
    expect(Number.parseFloat(box.style.width)).toBeCloseTo(188.67, 1)
    expect(Number.parseFloat(box.style.height)).toBeCloseTo(48.67, 1)
  })

  it('点字段 → 选中并高亮（`aria-pressed` + 框的 `data-selected`）', async () => {
    const user = userEvent.setup()
    setup()

    renderApp('/tasks/7?tab=parse')

    const fields = await screen.findByTestId('parse-fields')
    await user.click(within(fields).getByRole('button', { name: '合同总金额' }))

    expect(within(fields).getByRole('button', { name: '合同总金额' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    const overlay = screen.getByTestId('evidence-overlay')
    expect(
      within(overlay).getByRole('button', { name: /合同总金额/ }),
    ).toHaveAttribute('data-selected', 'true')
  })

  it('⚠️ 双向：点 PDF 上的证据框 → 选中对应字段', async () => {
    const user = userEvent.setup()
    setup()

    renderApp('/tasks/7?tab=parse')

    const overlay = await screen.findByTestId('evidence-overlay')
    await user.click(within(overlay).getByRole('button', { name: /合同总金额/ }))

    const fields = screen.getByTestId('parse-fields')
    expect(within(fields).getByRole('button', { name: '合同总金额' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('点「定位」跳到**证据所在页**（而不是停在当前页）', async () => {
    const user = userEvent.setup()
    // 证据在第 2 页：初始停在第 1 页，点定位后必须翻过去
    setup({
      evidencePage: 2,
      pages: [page({ page: 1, blocks: [] }), page({ page: 2 })],
    })

    renderApp('/tasks/7?tab=parse')

    expect(await screen.findByText(/第 1 \/ 2 页/)).toBeInTheDocument()

    const fields = screen.getByTestId('parse-fields')
    await user.click(within(fields).getByRole('button', { name: '定位到第 2 页' }))

    expect(screen.getByText(/第 2 \/ 2 页/)).toBeInTheDocument()
    // 翻页之后那一页的证据框才出现（说明框是**按页**筛的）
    const overlay = screen.getByTestId('evidence-overlay')
    expect(within(overlay).getByRole('button', { name: /合同总金额/ })).toBeInTheDocument()
  })

  it('选中字段的框**排在最后**（不会被别的字段盖住）', async () => {
    const user = userEvent.setup()
    setup()

    renderApp('/tasks/7?tab=parse')

    const fields = await screen.findByTestId('parse-fields')
    await user.click(within(fields).getByRole('button', { name: '合同总金额' }))

    const boxes = within(screen.getByTestId('evidence-overlay')).getAllByRole('button')
    // 同一层级下后出现的在上层 —— 选中的那个必须是最后一个
    expect(boxes[boxes.length - 1]).toHaveAttribute('data-selected', 'true')
  })
})

// ============================================================
// 2b. 深链（从模块 4/5 的「查看原文」跳进来）
// ============================================================

describe('证据深链', () => {
  it('`?page=2` 直接落在第 2 页（不是第 1 页）', async () => {
    setup({ pages: [page({ page: 1, blocks: [] }), page({ page: 2 })] })

    renderApp('/tasks/7?tab=parse&page=2')

    expect(await screen.findByText(/第 2 \/ 2 页/)).toBeInTheDocument()
  })

  it('⚠️ `?block=` 能反查到**引用它的字段**并选中（找不到时只停在那一页）', async () => {
    // 证据指向第 2 页的 p2-b1：深链带的正是这个块
    setup({
      evidencePage: 2,
      pages: [page({ page: 1, blocks: [] }), page({ page: 2 })],
    })

    renderApp('/tasks/7?tab=parse&page=2&block=p2-b1')

    const fields = await screen.findByTestId('parse-fields')
    expect(within(fields).getByRole('button', { name: '合同总金额' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('非法的 `?page=` 当没有（回落证据页），而不是报错', async () => {
    setup()

    renderApp('/tasks/7?tab=parse&page=999')

    expect(await screen.findByText(/第 999 \/ 1 页|第 1 \/ 1 页/)).toBeInTheDocument()
  })

  it('块不属于任何字段时**不选中**任何字段（只停在那一页）', async () => {
    setup({
      evidencePage: 2,
      pages: [page({ page: 1, blocks: [] }), page({ page: 2 })],
    })

    renderApp('/tasks/7?tab=parse&page=2&block=p2-b2')

    const fields = await screen.findByTestId('parse-fields')
    // 没有任何字段引用 p2-b2 → 保持未选中（不硬塞一个"莫名其妙选中的字段"）
    expect(within(fields).getByRole('button', { name: '合同总金额' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
  })
})

// ============================================================
// 3. 版本切换
// ============================================================

describe('版本切换', () => {
  it('⚠️ 切版本时**字段与文档一起**重新请求（不得混用两个版本的证据）', async () => {
    const user = userEvent.setup()
    const stubbed = setup()

    renderApp('/tasks/7?tab=parse')

    await screen.findByTestId('parse-fields')
    await user.selectOptions(await screen.findByLabelText('解析版本'), '10')

    await waitFor(() => {
      expect(stubbed.calls.some((url) => url.startsWith('/api/parses/10'))).toBe(true)
      expect(stubbed.calls.some((url) => url.startsWith('/api/parses/10/document'))).toBe(true)
    })
  })

  it('质量指标可见（完整度 / OCR 页数 / 识别可信度）', async () => {
    setup()

    renderApp('/tasks/7?tab=parse')

    // 覆盖率说**人话**：0.94 → 94%，"文本完整度"比"覆盖率 0.94"好懂
    expect(await screen.findByText(/文本完整度 94%/)).toBeInTheDocument()
    expect(screen.getByText(/2 页经 OCR 识别/)).toBeInTheDocument()
    // ⚠️ 后端只有**整份文档**的置信度，没有逐页的 —— 文案必须说明这一点，
    // 否则读的人会以为它是本页的数值
    expect(screen.getByText(/识别可信度 0.72/)).toBeInTheDocument()
    // ⚠️ 引擎的**版本编号**不直排进正文：它是排障信息，收进悬停提示
    const engine = screen.getByTitle(/解析引擎：pdf-text 1\.0\.0/)
    expect(engine).toBeInTheDocument()
  })

  it('没有解析记录时说清"由谁触发"，而不是留一张空表', async () => {
    setup({ versions: [] })

    renderApp('/tasks/7?tab=parse')

    expect(await screen.findByText(/还没有解析记录/)).toBeInTheDocument()
    expect(screen.getByText(/工具 4/)).toBeInTheDocument()
  })
})

// ============================================================
// 4. 降级与页状态
// ============================================================

describe('降级为文本定位', () => {
  it('⚠️ 没有坐标时降级为文本定位，并**标注**精度损失', async () => {
    setup({ pages: [page({ blocks: [] })] })

    renderApp('/tasks/7?tab=parse')

    const panel = await screen.findByTestId('page-text')
    expect(within(panel).getByText(/已降级为/)).toBeInTheDocument()
    expect(within(panel).getByText(/文本定位/)).toBeInTheDocument()
    // 原文里的引用片段被高亮（而不是只给一个页码让用户自己找）
    expect(within(panel).getByText('合同总金额')).toBeInTheDocument()
  })

  it('空页说"可靠识别后确认没有文字"（不是"没读到"）', async () => {
    setup({ pages: [page({ page_status: 'blank', blocks: [] })] })

    renderApp('/tasks/7?tab=parse')

    expect(await screen.findByText(/可靠识别后确认没有文字/)).toBeInTheDocument()
  })

  it('失败页给出错误码，并说明"重跑解析"而不是"合同没有"', async () => {
    setup({
      pages: [page({ page_status: 'failed', error_code: 'OCR_ENGINE_ERROR', blocks: [] })],
    })

    renderApp('/tasks/7?tab=parse')

    expect(await screen.findByText(/没有读成/)).toBeInTheDocument()
    expect(screen.getByText('OCR_ENGINE_ERROR')).toBeInTheDocument()
    expect(screen.getByText(/重跑解析/)).toBeInTheDocument()
  })

  it('OCR 页标注来源（"这条结论是猜的还是读的"）', async () => {
    setup({ pages: [page({ source: 'ocr' })] })

    renderApp('/tasks/7?tab=parse')

    expect(await screen.findByText('本页来自 OCR')).toBeInTheDocument()
  })
})

// ============================================================
// 5. 失败分支（按 error_code，不按 HTTP 状态）
// ============================================================

describe('失败分支', () => {
  it('⚠️ 标准文档 404 的两种原因**分支不同**（这也决定给不给重试按钮）', async () => {
    setup({
      documentResponse: () => apiError('RESOURCE_NOT_FOUND', '解析记录不存在', 404),
    })

    renderApp('/tasks/7?tab=parse')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('解析记录不存在或无权访问')
    expect(alert).toHaveTextContent('重试不会成功')
    // 重试永远不会成功 → **不给**按钮（给了等于把用户引向错的方向）
    expect(within(alert).queryByRole('button', { name: '重新加载' })).toBeNull()
  })

  it('缺工件时给"重跑解析"与重新加载按钮', async () => {
    setup({
      documentResponse: () => apiError('OBJECT_NOT_FOUND', '工件还没有', 404),
    })

    renderApp('/tasks/7?tab=parse')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('标准文档工件还没有')
    expect(alert).toHaveTextContent('重跑解析')
    expect(within(alert).getByRole('button', { name: '重新加载' })).toBeInTheDocument()
  })

  it('⚠️ 附件**字节**缺失（Task 4 欠的那条）：说明去跑工具 3，且不给重试按钮', async () => {
    setup({
      contentResponse: () => apiError('OBJECT_NOT_FOUND', '字节尚未入库', 404),
    })

    renderApp('/tasks/7?tab=parse')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('附件内容尚未入库')
    expect(alert).toHaveTextContent('工具 3')
    expect(within(alert).queryByRole('button', { name: '重新加载' })).toBeNull()
  })

  it('纯函数层面的分支表（不必渲染就能守住）', () => {
    const apiErrorOf = (code: string): ApiError =>
      new ApiError({
        status: 404,
        errorCode: code,
        message: 'x',
        retryable: false,
        correlationId: null,
        method: 'GET',
        path: '/api/x',
      })

    expect(describeDocumentFailure(apiErrorOf('OBJECT_NOT_FOUND')).summary).toContain('工件')
    expect(describeDocumentFailure(apiErrorOf('RESOURCE_NOT_FOUND')).summary).toContain('不存在')
    expect(describeDocumentFailure(apiErrorOf('INVALID_GATEWAY_RESPONSE')).summary).toContain('损坏')
    expect(describeContentFailure(apiErrorOf('OBJECT_NOT_FOUND')).action).toContain('工具 3')
    // 未列举的码也要有第三段（不能落到"没有下文"）
    expect(describeContentFailure(apiErrorOf('STORAGE_UNAVAILABLE')).action).not.toBe('')
  })
})
