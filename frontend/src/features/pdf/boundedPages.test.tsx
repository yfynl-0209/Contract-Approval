import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DocumentPage } from '../../api/contracts'
import { json, stubApi } from '../../test/stubApi'
import { renderApp } from '../../test/renderApp'

/**
 * 有界 DOM 回归（M8 Task 9 / 验收"100 页夹具"）。
 *
 * 模块 3 的渲染策略是**一次只渲染一页**（上一页/下一页），不是"窗口化的长列表"：
 * 证据定位的交互是"跳到某一页"，而不是"滚动浏览 100 页"。
 * 这条测试守住的是它的**边界**：不管文档有多少页，DOM 里的页数恒为 1 ——
 * 100 页文档若是逐页全渲染，DOM 会随页数线性膨胀，而界面看起来"还能用"，
 * 直到某个 500 页的扫描件让浏览器死掉。
 */
vi.mock('react-pdf', () => ({
  Document: ({ children }: { children?: unknown }) => <div>{children as never}</div>,
  Page: () => <div data-testid="pdf-page-content" />,
  pdfjs: { GlobalWorkerOptions: {} },
}))

function pages(count: number, evidencePage: number): readonly DocumentPage[] {
  return Array.from({ length: count }, (_, index) => {
    const page = index + 1
    const hasEvidence = page === evidencePage
    return {
      page,
      width: 600,
      height: 800,
      bbox_space: 'pdf-point-top-left',
      rotation: 0,
      source: 'text',
      page_status: 'ok',
      text: hasEvidence ? '合同总金额为人民币壹佰贰拾万元整' : `第 ${page} 页的正文`,
      route_reason: null,
      error_code: null,
      char_map: [],
      // 第 2 页**没有块**（用来验证文本降级也只渲染一页）；
      // 其余页都有块（让"有几何 → 走 PDF 渲染"成为常态路径）
      blocks:
        page === 2
          ? []
          : [
              {
                block_id: `p${page}-b1`,
                text: hasEvidence ? '合同总金额' : `第 ${page} 页的正文`,
                bbox: [100, 200, 300, 250],
                char_start: 0,
                char_end: hasEvidence ? 5 : 6,
                text_precision: 'char',
                bbox_precision: 'char',
                chars: Array.from({ length: hasEvidence ? 5 : 6 }, (_, i) => ({
                  text: '合同总金额为人民币壹佰贰拾万元整'.charAt(i),
                  bbox: [100 + i * 40, 200, 140 + i * 40, 250],
                  char_start: i,
                  char_end: i + 1,
                })),
              },
            ],
    }
  })
}

function parseRecord(evidencePage: number): Record<string, unknown> {
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
    basic_info: {
      schema_version: 1,
      fields: [
        {
          field_code: 'amount',
          value_text: '1,200,000.00',
          value_decimal: null,
          currency: 'CNY',
          status: 'extracted',
          reason_code: null,
          reason_text: null,
          evidence: [
            {
              page: evidencePage,
              block_id: `p${evidencePage}-b1`,
              text: '合同总金额',
              bbox: [100, 200, 300, 250],
              char_start: 0,
              char_end: 5,
              text_precision: 'char',
              bbox_precision: 'char',
            },
          ],
        },
      ],
    },
    clause_info: { schema_version: 1, fields: [] },
    quality: { text_coverage: 0.9, ocr_pages: 0, ocr_confidence: null },
  }
}

function setup(pages100: readonly DocumentPage[]): void {
  sessionStorage.setItem(
    'contract-approval.dev-identity',
    JSON.stringify({ actorId: 'r1', displayName: '', roles: ['legal_reviewer'] }),
  )
  stubApi({
    '/api/me': () =>
      json({
        actor_id: 'r1',
        display_name: 'R1',
        tenant_id: 'default',
        roles: ['legal_reviewer'],
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
        items: [parseRecord(37)],
        total: 1,
        page: 1,
        page_size: 50,
        page_count: 1,
        has_next: false,
      }),
    '/api/parses/11': () => json(parseRecord(37)),
    '/api/parses/11/document': () =>
      json({
        parse_id: 11,
        task_id: 7,
        artifact_id: 3,
        artifact_version: 1,
        sha256: 'a'.repeat(64),
        size_bytes: 1024,
        schema_version: 1,
        page_count: pages100.length,
        document: { schema_version: 1, pages: pages100 },
        created_at: '2026-09-15T10:00:00',
      }),
    '/api/attachments/5/content': () =>
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

describe('100 页文档的有界渲染', () => {
  it('⚠️ DOM 里的页数恒为 1（不随文档页数增长）', async () => {
    const user = userEvent.setup()
    setup(pages(100, 37))

    renderApp('/tasks/7?tab=parse')

    const fields = await screen.findByTestId('parse-fields')
    await user.click(within(fields).getByRole('button', { name: '定位到第 37 页' }))

    expect(await screen.findByText(/第 37 \/ 100 页/)).toBeInTheDocument()

    const canvases = document.querySelectorAll('[data-testid="pdf-page"]')
    expect(canvases).toHaveLength(1)

    // 证据框只属于当前页（别的 99 页不产生任何框）
    const overlay = document.querySelector('[data-testid="evidence-overlay"]')
    expect(overlay).not.toBeNull()
    expect(overlay?.querySelectorAll('button')).toHaveLength(1)
  })

  it('翻页后 DOM 仍是 1 页，且旧页的内容被替换（不累积）', async () => {
    const user = userEvent.setup()
    setup(pages(100, 37))

    renderApp('/tasks/7?tab=parse')

    const fields = await screen.findByTestId('parse-fields')
    await user.click(within(fields).getByRole('button', { name: '定位到第 37 页' }))

    await screen.findByText(/第 37 \/ 100 页/)
    await user.click(screen.getByRole('button', { name: '下一页' }))

    expect(await screen.findByText(/第 38 \/ 100 页/)).toBeInTheDocument()
    expect(document.querySelectorAll('[data-testid="pdf-page"]')).toHaveLength(1)
  })

  it('文本降级也只渲染**当前页**（100 份正文不会同时进 DOM）', async () => {
    const user = userEvent.setup()
    setup(pages(100, 37))

    renderApp('/tasks/7?tab=parse')

    // 第 1 页有块 → 走 PDF 渲染，没有文本面板
    await screen.findByTestId('parse-fields')
    await waitFor(() => {
      expect(document.querySelectorAll('[data-testid="pdf-page"]')).toHaveLength(1)
    })
    expect(document.querySelectorAll('[data-testid="page-text"]')).toHaveLength(0)

    // 第 2 页没有块 → 降级为文本定位，且只有这一页的文本
    await user.click(screen.getByRole('button', { name: '下一页' }))
    const panel = await screen.findByTestId('page-text')
    expect(panel).toHaveTextContent('第 2 页的正文')
    expect(document.querySelectorAll('[data-testid="page-text"]')).toHaveLength(1)
  })
})
