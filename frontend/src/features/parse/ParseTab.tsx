import { Suspense, lazy, useEffect, useState, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'

import { asApiError, describeApiError } from '../../api/client'
import type { ApiError } from '../../api/client'
import type { DocumentPage, FieldStatus, ParseRecord } from '../../api/contracts'
import { readEvidenceDeepLink } from '../../app/routes'
import { useTaskId } from '../../app/useTaskId'
import { useAttachments } from '../detail/queries'
import { EvidenceOverlay } from '../pdf/EvidenceOverlay'
import { blocksById, buildEvidenceBoxes, type EvidenceHighlight } from '../pdf/evidenceBoxes'
import { precisionLabel, type Viewport } from '../pdf/coordinateTransform'
import {
  describeContentFailure,
  describeDocumentFailure,
  type FailureDescription,
} from './failures'
import { fieldDisplayValue, readFieldSet, toFieldRows, type FieldRow } from './fields'
import { useAttachmentBytes, useParse, useParseDocument, useTaskParses } from './queries'

/**
 * 模块 3：解析结果 + 证据定位（M8 Task 5，设计 §4.3）。
 *
 * 目标：**验证 AI 的读取是否可信**，并一键跳到原文位置。
 *
 * ## 一屏里必须同时说清的三件事
 *
 * 1. **这条结论是什么状态**（四态）——`not_found` 与 `failed` 绝不合并；
 * 2. **证据在哪**（页 + 精度），能点过去；也能从框点回来；
 * 3. **这份证据有多可信**（文本/几何精度、是否 OCR、OCR 置信度）。
 *
 * ## 版本切换是"两个请求一起换"
 *
 * 字段与文档各是一个请求，**同一个 `parseId` 作键** —— 切换版本时两者同时重取。
 * 这是设计 §4.3 第 3 条（不同版本的证据不得混用）的落地方式：
 * 只换其中一个的话，界面会出现"v2 的字段 + v1 的坐标"，
 * 而它表现为"框的位置有点偏"，看不出是版本问题。
 *
 * 万一仍然对不上（证据指向的块不在本文档里），界面**如实说**
 * "不属于本次解析"而不画框（`evidenceBelongsToDocument`）。
 */
const PDF_DISPLAY_WIDTH = 560

/**
 * PDF 查看器**懒加载** —— 这是 `AppRoutes` 里预告过的唯一例外。
 *
 * 理由是实测出来的：把 `react-pdf` / `pdfjs-dist` 静态 import 进来之后，
 * 主包从 **249 kB 涨到 643 kB**（gzip 199 kB），而它们**只有这一个页面用得上**。
 * 于是每个打开"待办列表"的人都要先下载 400 kB 的 PDF 引擎 ——
 * 那是首屏最贵的一笔，却与他正在看的东西无关。
 *
 * 懒加载之后：主包回到原体积，查看器另成一个 chunk，只在**这一页真的能画框时**
 * 才去取（没有几何信息时根本不渲染查看器，走文本定位）。
 */
const PdfPageCanvas = lazy(() =>
  import('../pdf/PdfPageCanvas').then((module) => ({ default: module.PdfPageCanvas })),
)

export function ParseTab(): JSX.Element {
  const { taskId } = useTaskId()

  const versions = useTaskParses(taskId)
  const [selectedParseId, setSelectedParseId] = useState<number | null>(null)

  const versionItems = versions.data?.items ?? []
  const latestParseId = versionItems.length > 0 ? versionItems[0]?.parse_id ?? null : null
  const parseId = selectedParseId ?? latestParseId

  const parse = useParse(parseId)
  const documentQuery = useParseDocument(parseId)
  const attachments = useAttachments(taskId)

  // ⚠️ 深链：模块 4/5 的「查看原文」跳到**具体页码与块**（`routes.ts::evidenceDeepLink`）。
  // 没有它时那个链接只能落在工作台首页 —— 用户到了这一页还得自己找，
  // 而"找一遍"正是这个链接要省掉的事
  const [searchParams] = useSearchParams()
  const deepLink = readEvidenceDeepLink(searchParams)

  const [selectedFieldCode, setSelectedFieldCode] = useState<string | null>(null)
  const [pageOverride, setPageOverride] = useState<number | null>(deepLink.page)
  const [textMode, setTextMode] = useState(false)
  const [appliedBlock, setAppliedBlock] = useState<string | null>(null)

  // ⚠️ 字节的取法：从**附件列表**里找到与解析记录对应的那一行，用它的
  // `content_url`（不在前端拼路径 —— 存储实现换了路径形态不会跟着变）
  const attachmentRow =
    (attachments.data?.items ?? []).find(
      (row) => row.attachment_record_id === (parse.data?.attachment_record_id ?? -1),
    ) ?? null

  // ⚠️ `rows` 必须在**提前 return 之前**算出来：下面那个"深链块 → 字段"的
  // effect 要用它，而 React 不允许条件式地跳过 hook
  const basic = readFieldSet(parse.data?.basic_info ?? null)
  const clause = readFieldSet(parse.data?.clause_info ?? null)
  const rows = toFieldRows(basic, clause)

  useEffect(() => {
    // 块号来自模块 4 的规则证据：它**不一定**属于某个字段的证据
    // （关键词规则命中的片段可能落在没有字段引用的块里）。
    // 因此这里只做"能匹配就顺带选中字段"，匹配不到就只停在那一页 ——
    // 而不是把块号硬塞进字段选择，让用户看到一条莫名其妙的选中项。
    if (deepLink.blockId === null || appliedBlock === deepLink.blockId || rows.length === 0) {
      return
    }
    setAppliedBlock(deepLink.blockId)
    const owner = rows.find((row) =>
      row.field.evidence.some((span) => span.block_id === deepLink.blockId),
    )
    if (owner !== undefined) {
      setSelectedFieldCode(owner.fieldCode)
    }
  }, [appliedBlock, deepLink.blockId, rows])

  // 文档还没到、或全篇没有块时**不下载字节**：一份 20MB 的 PDF 拿下来却
  // 只能走文本定位，白等一次（而"等"的感觉会被归因到"这个页面很慢"）
  const pagesSoFar = documentQuery.data?.document.pages ?? []
  const noGeometryAnywhere =
    pagesSoFar.length === 0 || pagesSoFar.every((item) => item.blocks.length === 0)
  const bytes = useAttachmentBytes(
    noGeometryAnywhere ? null : (attachmentRow?.content_url ?? null),
  )

  if (versions.isPending) {
    return <p aria-busy="true">正在加载解析版本…</p>
  }
  if (versions.isError) {
    return <FailurePanel error={versions.error} onRetry={() => void versions.refetch()} />
  }
  if (latestParseId === null) {
    return (
      <p style={{ color: 'var(--text-2)' }}>
        这份合同还没有解析记录。解析由工具 4（或后台作业）触发，完成后这里会出现版本列表。
      </p>
    )
  }

  const readable = parse.data !== undefined && (basic !== null || clause !== null)

  const pages = documentQuery.data?.document.pages ?? []
  const blocks = blocksById(pages)

  const selectedRow = rows.find((row) => row.fieldCode === selectedFieldCode) ?? null
  const evidencePage = selectedRow?.field.evidence[0]?.page ?? 1
  const pageNo = pageOverride ?? evidencePage
  const page = pages.find((item) => item.page === pageNo) ?? pages[0] ?? null

  const highlights: readonly EvidenceHighlight[] = rows
    .filter((row) => row.field.evidence.length > 0)
    .map((row) => ({
      fieldCode: row.fieldCode,
      label: row.label,
      spans: row.field.evidence,
    }))

  const viewport: Viewport =
    page === null
      ? { width: PDF_DISPLAY_WIDTH, height: PDF_DISPLAY_WIDTH }
      : { width: PDF_DISPLAY_WIDTH, height: (PDF_DISPLAY_WIDTH * page.height) / page.width }

  const boxes =
    page === null
      ? []
      : buildEvidenceBoxes(highlights, page, viewport, blocks, selectedFieldCode)

  const selectField = (fieldCode: string): void => {
    setSelectedFieldCode(fieldCode)
    // 清掉手工翻页：选中字段后应当**跟着证据走**，否则"点了定位却没动"
    setPageOverride(null)
  }

  // 没有几何信息（这一页没有块）时自动降级为文本定位，并明确标注。
  // ⚠️ `bytes` 的加载中/失败**不算**降级：那是"原件还没到"，
  // 与"这一页根本没有坐标"是两件事，界面要说的话也不同
  const hasGeometry = page !== null && page.blocks.length > 0
  const autoDegraded = !hasGeometry
  const degraded = textMode || autoDegraded

  return (
    <div className="parse-layout">
      <div className="parse-col parse-col-fields">
        <VersionSelector
          versions={versionItems}
          selectedParseId={parseId}
          onSelect={(next) => {
            setSelectedParseId(next)
            // 换版本时清掉选中与翻页：它们的语义依附于**上一版的坐标**
            setSelectedFieldCode(null)
            setPageOverride(null)
          }}
        />
        <QualityPanel parse={parse.data ?? null} />
        {/* ⚠️ 顺序是**加载中 → 失败 → 结构不可读 → 正常**。
            少了第一条时，请求还在路上的那一瞬会渲染"字段结构不是当前版本能读的" ——
            一句**假话**，而且它指向的处置（重跑解析）与真实原因（等一下就好）无关 */}
        {parse.isPending ? (
          <p aria-busy="true">正在加载解析字段…</p>
        ) : parse.isError ? (
          <FailurePanel error={parse.error} onRetry={() => void parse.refetch()} />
        ) : !readable ? (
          <p role="alert" style={{ color: 'var(--status-warn)' }}>
            这份解析记录的字段结构不是当前版本能读的（可能来自旧 schema）。
            请以服务端接口为准，或重跑解析生成新版本。
          </p>
        ) : (
          <FieldList
            rows={rows}
            selectedFieldCode={selectedFieldCode}
            onSelect={selectField}
            onGoToPage={(pageNumber) => setPageOverride(pageNumber)}
          />
        )}
      </div>

      <div className="parse-col parse-col-pdf">
        {documentQuery.isPending ? (
          <p aria-busy="true">正在加载标准文档…</p>
        ) : documentQuery.isError ? (
          <DocumentFailure
            error={asApiError(documentQuery.error)}
            onRetry={() => void documentQuery.refetch()}
          />
        ) : page === null ? (
          <p style={{ color: 'var(--text-2)' }}>这份标准文档里没有页面。</p>
        ) : (
          <>
            {/* 粘性工具条：左栏滚多远，翻页控件都钉在右栏顶部 */}
            <div className="pdf-toolbar-sticky">
              <DocToolbar
                page={page}
                pageCount={pages.length}
                degraded={degraded}
                autoDegraded={autoDegraded}
                onGoTo={(next) => setPageOverride(next)}
                onToggleTextMode={() => setTextMode((current) => !current)}
              />
            </div>

            {degraded ? (
              <PageTextPanel page={page} highlights={highlights} />
            ) : bytes.error !== null ? (
              <ContentFailure
                error={bytes.error}
                onRetry={bytes.reload}
                retrying={bytes.loading}
              />
            ) : bytes.loading ? (
              <p aria-busy="true">正在加载 PDF 原件…</p>
            ) : bytes.data === null ? (
              <p style={{ color: 'var(--text-2)' }}>没有可渲染的 PDF 字节。</p>
            ) : (
              <Suspense fallback={<p aria-busy="true">正在加载 PDF 查看器…</p>}>
                <PdfPageCanvas data={bytes.data} page={page} viewport={viewport}>
                  <EvidenceOverlay boxes={boxes} onSelect={selectField} />
                </PdfPageCanvas>
              </Suspense>
            )}

            {page.page_status !== 'ok' && <PageStatusNote page={page} />}
          </>
        )}
      </div>
    </div>
  )
}

// ============================================================
// 版本与质量
// ============================================================

function VersionSelector({
  versions,
  selectedParseId,
  onSelect,
}: {
  readonly versions: readonly ParseRecord[]
  readonly selectedParseId: number | null
  readonly onSelect: (parseId: number) => void
}): JSX.Element {
  return (
    <label style={{ display: 'block', marginBottom: 'var(--space-2)' }}>
      解析版本
      <select
        value={selectedParseId ?? ''}
        onChange={(event) => onSelect(Number.parseInt(event.target.value, 10))}
        style={{ marginLeft: 'var(--space-2)' }}
      >
        {versions.map((version) => (
          <option key={version.parse_id} value={version.parse_id}>
            v{version.parse_version} · {version.parse_status}
            {version.quality.text_coverage === null
              ? ''
              : ` · 覆盖率 ${formatRatio(version.quality.text_coverage)}`}
          </option>
        ))}
      </select>
    </label>
  )
}

function QualityPanel({ parse }: { readonly parse: ParseRecord | null }): JSX.Element | null {
  if (parse === null) {
    return null
  }
  const { quality } = parse
  return (
    <div
      style={{
        fontSize: 'var(--font-size-xs)',
        color: 'var(--text-2)',
        marginBottom: 'var(--space-3)',
      }}
    >
      {/* ⚠️ 给**人**看的文案，不堆术语：
          覆盖率 100% = 文字层完整，<100% = 部分内容是 OCR 识别的；
          OCR 页数为 0 时不必说"0 页"，直接说"未用到 OCR"。
          引擎长编号（pymupdf-1.25.1+pipe-v1）是排障用的，收进悬停提示。 */}
      文本完整度 {quality.text_coverage === null ? '—' : percent(quality.text_coverage)}
      {' · '}
      {quality.ocr_pages === null || quality.ocr_pages === 0
        ? '未用到 OCR'
        : `${quality.ocr_pages} 页经 OCR 识别`}
      {quality.ocr_confidence !== null && (
        <>
          {' · '}
          {/* 明说"整份文档"：后端只有文档级的置信度，没有逐页的 ——
              写成"OCR 置信度"会让人以为它是当前这一页的数值 */}
          识别可信度 {quality.ocr_confidence.toFixed(2)}
        </>
      )}
      <span title={`解析引擎：${parse.parser_name} ${parse.parser_version}`}>
        {` · ${parse.parser_name}`}
      </span>
    </div>
  )
}

function formatRatio(value: number): string {
  return value.toFixed(2)
}

/** 覆盖率用百分比说人话（1 → 100%），比"1.00"直观。 */
function percent(value: number): string {
  return `${Math.round(value * 100)}%`
}

// ============================================================
// 字段列表
// ============================================================

/** 四态的呈现（设计 §4.3 表）。**每一态都有文字标签**，颜色只是辅助。 */
const STATUS_PRESENTATION: Readonly<
  Record<FieldStatus, { readonly mark: string; readonly label: string; readonly hint: string }>
> = {
  extracted: {
    mark: '✓',
    label: '已提取',
    hint: '可点「定位」到原文核对',
  },
  not_found: {
    mark: '∅',
    label: '未发现',
    hint: '文档可检索但未发现——请判断是否真的缺失',
  },
  uncertain: {
    mark: '⚠',
    label: '不确定',
    hint: '证据不足，需人工判断',
  },
  failed: {
    mark: '✗',
    label: '解析失败',
    hint: '这是读取失败，不是"合同没有这一项"——请联系管理员重跑解析',
  },
}

function statusColor(status: FieldStatus): string {
  switch (status) {
    case 'extracted':
      return 'var(--status-ok)'
    case 'not_found':
      return 'var(--text-3)'
    case 'uncertain':
      return 'var(--status-warn)'
    case 'failed':
      return 'var(--status-danger)'
  }
}

function FieldList({
  rows,
  selectedFieldCode,
  onSelect,
  onGoToPage,
}: {
  readonly rows: readonly FieldRow[]
  readonly selectedFieldCode: string | null
  readonly onSelect: (fieldCode: string) => void
  readonly onGoToPage: (page: number) => void
}): JSX.Element {
  if (rows.length === 0) {
    return <p style={{ color: 'var(--text-2)' }}>这份解析记录里没有字段。</p>
  }

  const groups: ReadonlyArray<{ readonly key: FieldRow['group']; readonly title: string }> = [
    { key: 'basic', title: '基本信息' },
    { key: 'clause', title: '条款' },
  ]

  return (
    <div data-testid="parse-fields">
      {groups.map((group) => {
        const groupRows = rows.filter((row) => row.group === group.key)
        if (groupRows.length === 0) {
          return null
        }
        return (
          <section key={group.key} aria-label={group.title} style={{ marginBottom: 'var(--space-4)' }}>
            <h2 style={{ fontSize: 'var(--font-size-sm)' }}>{group.title}</h2>
            <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
              {groupRows.map((row) => (
                <FieldListItem
                  key={row.fieldCode}
                  row={row}
                  selected={row.fieldCode === selectedFieldCode}
                  onSelect={onSelect}
                  onGoToPage={onGoToPage}
                />
              ))}
            </ul>
          </section>
        )
      })}
    </div>
  )
}

function FieldListItem({
  row,
  selected,
  onSelect,
  onGoToPage,
}: {
  readonly row: FieldRow
  readonly selected: boolean
  readonly onSelect: (fieldCode: string) => void
  readonly onGoToPage: (page: number) => void
}): JSX.Element {
  const presentation = STATUS_PRESENTATION[row.field.status]

  return (
    <li
      data-field-code={row.fieldCode}
      className={`field-item${selected ? ' hl' : ''}`}
      style={{ borderLeft: `3px solid ${statusColor(row.field.status)}` }}
    >
      <div className="field-head">
        <button
          type="button"
          onClick={() => onSelect(row.fieldCode)}
          aria-pressed={selected}
          className="field-name"
          style={{
            border: 'none',
            background: 'none',
            padding: 0,
            cursor: 'pointer',
            color: 'inherit',
          }}
        >
          {row.label}
        </button>

        <span
          style={{ color: statusColor(row.field.status), fontSize: 'var(--font-size-xs)' }}
          title={presentation.hint}
        >
          {presentation.mark} {presentation.label}
        </span>

        <span className="locate" style={{ border: 'none', background: 'none' }}>
          {fieldDisplayValue(row.field)}
        </span>
      </div>

      {row.field.reason_text !== null && (
        <div className="field-note">{row.field.reason_text}</div>
      )}

      {row.field.evidence.length > 0 && (
        <ul style={{ listStyle: 'none', padding: 0, margin: 'var(--space-2) 0 0' }}>
          {row.field.evidence.map((span, index) => (
            <li key={`${span.block_id}:${span.char_start}`} style={{ fontSize: 'var(--font-size-xs)' }}>
              <button
                type="button"
                onClick={() => {
                  onSelect(row.fieldCode)
                  onGoToPage(span.page)
                }}
                style={linkButtonStyle}
                aria-label={`定位到第 ${span.page} 页`}
              >
                定位 → 第 {span.page} 页
              </button>
              {' · '}
              {precisionLabel(span)}
              {index === 0 && span.text !== '' && (
                <span style={{ color: 'var(--text-2)' }}>「{truncate(span.text, 40)}」</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

function truncate(text: string, limit: number): string {
  return text.length <= limit ? text : `${text.slice(0, limit)}…`
}

// ============================================================
// 右侧：工具条 / 文本降级 / 页状态
// ============================================================

function DocToolbar({
  page,
  pageCount,
  degraded,
  autoDegraded,
  onGoTo,
  onToggleTextMode,
}: {
  readonly page: DocumentPage
  readonly pageCount: number
  readonly degraded: boolean
  readonly autoDegraded: boolean
  readonly onGoTo: (page: number) => void
  readonly onToggleTextMode: () => void
}): JSX.Element {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 'var(--space-2)',
        marginBottom: 'var(--space-2)',
        fontSize: 'var(--font-size-sm)',
      }}
    >
      <button type="button" onClick={() => onGoTo(page.page - 1)} disabled={page.page <= 1}>
        上一页
      </button>
      <span>
        第 {page.page} / {pageCount} 页
      </span>
      <button
        type="button"
        onClick={() => onGoTo(page.page + 1)}
        disabled={page.page >= pageCount}
      >
        下一页
      </button>

      <span style={{ marginLeft: 'auto' }}>
        {/* 证据来源必须可见：读屏用户同样需要知道"这条是 OCR 来的" */}
        {page.source === 'ocr' ? '本页来自 OCR' : '本页来自文本层'}
      </span>

      {!autoDegraded && (
        <button type="button" onClick={onToggleTextMode}>
          {degraded ? '显示 PDF' : '只看文本'}
        </button>
      )}
    </div>
  )
}

/**
 * 文本定位（降级路径）。
 *
 * ⚠️ 降级时**必须标注**"这里没有坐标"：不标注时用户会以为"PDF 里就是没有框"，
 * 而真相是这一页没有几何信息 —— 两者的下一步完全不同
 * （前者是"这条证据有问题"，后者是"换一页看或接受文本定位"）。
 */
function PageTextPanel({
  page,
  highlights,
}: {
  readonly page: DocumentPage
  readonly highlights: readonly EvidenceHighlight[]
}): JSX.Element {
  const ranges = highlights
    .flatMap((highlight) =>
      highlight.spans
        .filter((span) => span.page === page.page)
        .map((span) => ({
          start: span.char_start,
          end: span.char_end,
          label: highlight.label,
        })),
    )
    .sort((left, right) => left.start - right.start)

  const segments: Array<{ readonly text: string; readonly label: string | null }> = []
  let cursor = 0
  for (const range of ranges) {
    if (range.start < cursor) {
      continue
    }
    if (range.start > cursor) {
      segments.push({ text: page.text.slice(cursor, range.start), label: null })
    }
    segments.push({ text: page.text.slice(range.start, range.end), label: range.label })
    cursor = range.end
  }
  if (cursor < page.text.length) {
    segments.push({ text: page.text.slice(cursor), label: null })
  }

  return (
    <div data-testid="page-text">
      <p
        style={{
          background: 'var(--surface-2)',
          color: 'var(--text-2)',
          padding: 'var(--space-2)',
          fontSize: 'var(--font-size-xs)',
        }}
      >
        这一页没有可用的坐标信息，已降级为<strong>文本定位</strong>（画不出证据框）。
        下面高亮的是字段引用的原文片段。
      </p>
      <div
        style={{
          whiteSpace: 'pre-wrap',
          lineHeight: 1.6,
          maxHeight: '560px',
          overflow: 'auto',
          border: '1px solid var(--border)',
          padding: 'var(--space-3)',
        }}
      >
        {page.text === '' ? (
          <span style={{ color: 'var(--text-3)' }}>（这一页没有文本内容）</span>
        ) : (
          segments.map((segment, index) =>
            segment.label === null ? (
              <span key={index}>{segment.text}</span>
            ) : (
              <mark key={index} title={segment.label}>
                {segment.text}
              </mark>
            ),
          )
        )}
      </div>
    </div>
  )
}

/**
 * 页级状态（`page_status`）。
 *
 * ⚠️ `blank` 是"**可靠识别后**确认无文字"，`failed` 是"没读成" ——
 * 两者的下一步完全相反（一个是"这一页确实空"，一个是"联系管理员重跑"）。
 */
function PageStatusNote({ page }: { readonly page: DocumentPage }): JSX.Element {
  const description =
    page.page_status === 'blank'
      ? '这一页在可靠识别后确认没有文字。'
      : page.page_status === 'uncertain'
        ? '这一页有输出但置信度不足（可能漏字），结论仅供参考。'
        : '这一页没有读成（不是"空的"）—— 请重跑解析或联系管理员。'

  return (
    <p style={{ color: 'var(--status-warn)', fontSize: 'var(--font-size-xs)' }}>
      {description}
      {page.error_code !== null && (
        <>
          {' '}
          错误码 <code>{page.error_code}</code>
        </>
      )}
    </p>
  )
}

// ============================================================
// 失败态：**按 error_code 分支**（不是按 HTTP 状态）
// ============================================================

function DocumentFailure({
  error,
  onRetry,
}: {
  readonly error: ApiError
  readonly onRetry: () => void
}): JSX.Element {
  const description = describeDocumentFailure(error)
  // 「缺工件」是**可恢复**的（重跑解析就有了），而「记录不存在」重试永远不会成功 ——
  // 因此只在前者给按钮。给一个注定失败的按钮等于把用户的下一步引向错的方向。
  const recoverable = error.errorCode === 'OBJECT_NOT_FOUND' || error.retryable
  return (
    <Alert
      tone={error.errorCode === 'OBJECT_NOT_FOUND' ? 'warn' : 'danger'}
      description={description}
    >
      {recoverable ? (
        <button type="button" onClick={onRetry}>
          重新加载
        </button>
      ) : null}
    </Alert>
  )
}

function ContentFailure({
  error,
  onRetry,
  retrying,
}: {
  readonly error: ApiError
  readonly onRetry: () => void
  readonly retrying: boolean
}): JSX.Element {
  const description = describeContentFailure(error)
  const recoverable = error.errorCode !== 'OBJECT_NOT_FOUND' && error.retryable
  return (
    <Alert
      tone={error.errorCode === 'OBJECT_NOT_FOUND' ? 'warn' : 'danger'}
      description={description}
    >
      {recoverable ? (
        <button type="button" onClick={onRetry} disabled={retrying}>
          重新加载
        </button>
      ) : null}
    </Alert>
  )
}

/** 三段式的告警块（是什么 / 为什么 / 我现在能做什么）—— 模块 3 各处共用一份。 */
function Alert({
  tone,
  description,
  children,
}: {
  readonly tone: 'warn' | 'danger'
  readonly description: FailureDescription
  readonly children?: ReactNode
}): JSX.Element {
  return (
    <div
      role="alert"
      style={{
        borderLeft: `4px solid ${tone === 'warn' ? 'var(--status-warn)' : 'var(--status-danger)'}`,
        padding: 'var(--space-3)',
      }}
    >
      <strong>{description.summary}</strong>
      <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
        {description.detail}——{description.action}
      </p>
      {children}
    </div>
  )
}

function FailurePanel({
  error,
  onRetry,
}: {
  readonly error: unknown
  readonly onRetry: () => void
}): JSX.Element {
  return (
    <Alert tone="danger" description={describeApiError(asApiError(error))}>
      <button type="button" onClick={onRetry}>
        重新加载
      </button>
    </Alert>
  )
}

const linkButtonStyle = {
  border: 'none',
  background: 'none',
  color: 'var(--accent)',
  cursor: 'pointer',
  padding: 0,
  fontSize: 'var(--font-size-xs)',
} as const
