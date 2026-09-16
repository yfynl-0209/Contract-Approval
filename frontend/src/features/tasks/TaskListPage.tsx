import { useCallback, type KeyboardEvent } from 'react'
import { useSearchParams } from 'react-router-dom'

import { ApiError, describeApiError } from '../../api/client'
import type { TaskStatus, TaskSummary, TaskView } from '../../api/contracts'
import { formatLocalTime } from '../../domain/time'
import { parseListParams } from './listParams'
import {
  TaskCodeCell,
  TaskRiskCell,
  TaskStatusCell,
  TaskWritebackCell,
} from './TaskStatusCell'
import { TaskFilters } from './TaskFilters'
import { useTaskSummary, useTasks, type TaskQueryFilters } from './queries'

/**
 * 模块 1：待办调用（M8 Task 3）。
 *
 * 目标：一屏看清"哪些要我处理"，尤其是**为什么卡住**（设计 §4.1）。
 *
 * ## 筛选条件只走 URL，而且只认白名单
 *
 * 筛选与页码放进查询串（可分享、可后退），但**只允许两个键**：
 * `status`（五个枚举值之一）与 `page`（正整数）。
 * 非法取值**回落到默认**而不是报错 —— 链接可能来自旧版本或被手工改过，
 * 而用户想看的仍然是列表。
 *
 * ⚠️ **业务数据一律不进 URL**：合同标题、表单内容、正文都不许拼进来。
 * 这在 `client.ts::buildQuery` 有一道显式的闸门（对象值直接抛错），
 * 这里则是"本页面只往 URL 放这两个键"的来源。
 *
 * ## 总数、页数、风险、回写状态**全部来自后端**
 *
 * 前端数不到全量（只能数到这一页），因此"卡片 12 条"与"列表 3 行"
 * 只能由服务端给的两个数字保证一致。任何"前端再加一加"的写法都会
 * 在数据超过一页时开始自相矛盾。
 */

const PAGE_SIZE = 20

export function TaskListPage(): JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams()
  const params = parseListParams(searchParams)

  const filters: TaskQueryFilters = {
    page: params.page,
    pageSize: PAGE_SIZE,
    taskStatus: params.status,
  }

  const tasks = useTasks(filters)
  const summary = useTaskSummary()

  const applyParams = useCallback(
    (next: { page?: number; status?: TaskStatus | null }): void => {
      const status = next.status === undefined ? params.status : next.status
      const page = next.page ?? 1

      const built = new URLSearchParams()
      if (status !== null) {
        built.set('status', status)
      }
      if (page > 1) {
        // 查询串的值只能是字符串；页码是本页面放进 URL 的**唯一**数字
        built.set('page', String(page))
      }
      // 用 push（默认）而不是 replace：筛选与翻页是可以后退回看的操作，
      // 而"后退"在这里的预期正是"回到上一个筛选结果"
      setSearchParams(built)
    },
    [params.status, setSearchParams],
  )

  const refresh = useCallback((): void => {
    void tasks.refetch()
    void summary.refetch()
  }, [tasks, summary])

  const isRefreshing = tasks.isFetching || summary.isFetching

  return (
    <section aria-labelledby="task-list-title">
      <h1 id="task-list-title" style={{ marginBottom: 'var(--space-3)' }}>
        待办调用
      </h1>

      <SummaryCards summary={summary.data} />

      <TaskFilters
        status={params.status}
        onStatusChange={(status) => applyParams({ status, page: 1 })}
        onRefresh={refresh}
        isRefreshing={isRefreshing}
      />

      {tasks.isPending ? (
        <LoadingTable />
      ) : tasks.isError ? (
        <ErrorPanel error={tasks.error} onRetry={() => void tasks.refetch()} />
      ) : tasks.data.items.length === 0 ? (
        <EmptyPanel filtered={params.status !== null} />
      ) : (
        <>
          <TaskTable tasks={tasks.data.items} />
          <Pagination
            page={tasks.data.page}
            pageCount={tasks.data.page_count}
            total={tasks.data.total}
            hasNext={tasks.data.has_next}
            onGoTo={(page) => applyParams({ page })}
          />
        </>
      )}
    </section>
  )
}

// ============================================================
// 汇总卡片（数据全部来自 `/api/tasks/summary`）
// ============================================================

function SummaryCards({
  summary,
}: {
  readonly summary: TaskSummary | undefined
}): JSX.Element {
  if (summary === undefined) {
    // 汇总与列表是**两个**请求：列表先到时先渲染列表，卡片位置留白而不是消失 ——
    // 消失会让下面的内容跳一下，而用户正在读的就是那张表
    return (
      // ⚠️ 骨架**不带** aria-label：带了的瞬间 `findByLabelText('任务汇总')`
      // 就会命中骨架并返回，测试（和屏幕阅读器用户）拿到的是"还没有数据"的空壳
      <div aria-busy="true">
        <div className="summary-grid">
          {Array.from({ length: 5 }, (_, index) => (
            <div key={index} className="metric">
              <div className="metric-num" style={{ color: 'var(--border-strong)' }}>
                —
              </div>
              <div className="metric-label">正在统计…</div>
            </div>
          ))}
        </div>
      </div>
    )
  }

  const cards: ReadonlyArray<{ label: string; value: number; hint?: string; color: string }> = [
    { label: '待处理', value: summary.by_status.pending, color: 'var(--accent)' },
    {
      label: '审查中',
      // ⚠️ 两个后端计数的**和**，不是"前端数行"：解析中与审查中都属于"在跑"
      value: summary.by_status.parsing + summary.by_status.reviewing,
      hint: `解析中 ${summary.by_status.parsing} · 审查中 ${summary.by_status.reviewing}`,
      color: 'var(--status-violet)',
    },
    { label: '需人工', value: summary.by_status.blocked, color: 'var(--status-warn)' },
    { label: '已完成', value: summary.by_status.done, color: 'var(--status-ok)' },
    { label: '回写失败', value: summary.writeback_failed, color: 'var(--status-danger)' },
  ]

  return (
    <div aria-label="任务汇总">
      <div className="summary-grid">
        {cards.map((card) => (
          <div key={card.label} className="metric" style={{ '--mc': card.color } as React.CSSProperties}>
            <div className="metric-num" title={card.hint}>
              {card.value}
            </div>
            <div className="metric-label">{card.label}</div>
          </div>
        ))}
      </div>
      {/* 总数不做成第六张卡：5 张状态卡一行排满后，孤卡换行非常突兀 */}
      <div style={{ color: 'var(--text-3)', fontSize: 'var(--font-size-xs)' }}>
        共 <strong style={{ color: 'var(--text-2)' }}>{summary.total}</strong>{' '}
        条（全部状态合计）
      </div>
    </div>
  )
}

// ============================================================
// 表格
// ============================================================

function TaskTable({
  tasks,
}: {
  readonly tasks: readonly TaskView[]
}): JSX.Element {
  /**
   * 键盘行导航：上下箭头在行之间移动焦点。
   *
   * 表格里可点的是**编号链接**（唯一的 Tab 停靠点），箭头只是让密集表格里
   * 少按几次 Tab。焦点不会跑出表格 —— 到头就停住，而不是跳到页面别处
   * （那会让"我在表里移动"这件事失去可预测性）。
   */
  /**
   * ⚠️ 监听器挂在**链接**上而不是表格上（jsx-a11y 的要求，也是对的语义）：
   * 表格不是交互元素，"有键盘监听"的东西应该是可交互的。
   * 行为不变：事件从链接冒泡，处理逻辑照旧在整个表格里找行链接。
   */
  const handleKeyDown = (event: KeyboardEvent<HTMLAnchorElement>): void => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') {
      return
    }
    const table = event.currentTarget.closest('table')
    if (table === null) {
      return
    }
    const links = Array.from(
      table.querySelectorAll<HTMLAnchorElement>('[data-row-link]'),
    )
    const index = links.indexOf(document.activeElement as HTMLAnchorElement)
    if (index === -1) {
      return
    }
    const step = event.key === 'ArrowDown' ? 1 : -1
    const next = links[index + step]
    if (next === undefined) {
      return
    }
    event.preventDefault()
    next.focus()
  }

  return (
    <table aria-label="待办任务" className="table">
      <thead>
        <tr>
          <th scope="col">编号</th>
          <th scope="col">标题</th>
          <th scope="col">申请人</th>
          <th scope="col">附件</th>
          <th scope="col">状态</th>
          <th scope="col">总风险</th>
          <th scope="col">回写</th>
          <th scope="col">创建时间</th>
        </tr>
      </thead>
      <tbody>
        {tasks.map((task) => (
          <tr key={task.task_id}>
            <td>
              <TaskCodeCell task={task} onKeyDown={handleKeyDown} />
            </td>
            <td>{task.approval_title ?? '（无标题）'}</td>
            <td>{task.applicant_name ?? '—'}</td>
            <td>{task.attachment_count}</td>
            <td>
              <TaskStatusCell task={task} />
            </td>
            <td>
              <TaskRiskCell task={task} />
            </td>
            <td>
              <TaskWritebackCell task={task} />
            </td>
            <td className="sub">
              {/* 后端给的是**不带时区**的本地时间串，因此按本地时间格式化 */}
              <time dateTime={task.created_at ?? undefined}>
                {formatLocalTime(task.created_at)}
              </time>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Pagination({
  page,
  pageCount,
  total,
  hasNext,
  onGoTo,
}: {
  readonly page: number
  readonly pageCount: number
  readonly total: number
  readonly hasNext: boolean
  readonly onGoTo: (page: number) => void
}): JSX.Element {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 'var(--space-3)',
        marginTop: 'var(--space-3)',
        fontSize: 'var(--font-size-sm)',
      }}
    >
      {/* 这些数字**全部来自后端**：前端数不出全量，自己算一定会在数据超过一页时出错 */}
      <span>
        第 {page} 页 / 共 {pageCount} 页 · 共 {total} 条
      </span>
      <button type="button" onClick={() => onGoTo(page - 1)} disabled={page <= 1}>
        上一页
      </button>
      <button type="button" onClick={() => onGoTo(page + 1)} disabled={!hasNext}>
        下一页
      </button>
    </div>
  )
}

// ============================================================
// 加载 / 空 / 错
// ============================================================

function LoadingTable(): JSX.Element {
  return (
    <div aria-busy="true" aria-live="polite">
      {/* 保留表头：骨架屏"跳一下"的代价比多等半秒更大 —— 用户正要点的位置会变 */}
      <table aria-label="待办任务（加载中）" style={{ width: '100%' }}>
        <thead>
          <tr style={{ background: 'var(--surface-2)', textAlign: 'left' }}>
            <th scope="col">编号</th>
            <th scope="col">标题</th>
            <th scope="col">申请人</th>
            <th scope="col">附件</th>
            <th scope="col">状态</th>
            <th scope="col">总风险</th>
            <th scope="col">回写</th>
            <th scope="col">创建时间</th>
          </tr>
        </thead>
      </table>
      <p style={{ color: 'var(--text-2)' }}>正在加载…</p>
    </div>
  )
}

function EmptyPanel({ filtered }: { readonly filtered: boolean }): JSX.Element {
  return (
    <div className="empty">
      <p className="big">
        {filtered ? '当前筛选条件下没有任务。' : '暂无待办'}
      </p>
      <p>
        {filtered
          ? '换个状态筛一下，或刷新重试。'
          : '从审批系统同步待办由定时任务或外部调用方触发（工具 1）；本页只读已同步的任务。'}
      </p>
      {/* ⚠️ 这里**没有**「拉取待办」按钮：控制台只调 `/api/*`，
          而"触发一次拉取"目前只有 `/tools/list_pending_contract_approvals`
          （那是给外部系统的接口）。放一个点了没反应的按钮比不放更糟。 */}
    </div>
  )
}

function ErrorPanel({
  error,
  onRetry,
}: {
  readonly error: unknown
  readonly onRetry: () => void
}): JSX.Element {
  const description =
    error instanceof ApiError
      ? describeApiError(error)
      : { summary: '加载失败', detail: '', action: '稍后重试。' }

  return (
    <div className="notice notice-danger" role="alert">
      <div>
        {/* 三段式（§5.5）：是什么 / 为什么 / 我现在能做什么 */}
        <strong>{description.summary}</strong>
        <p>
          {description.detail === ''
            ? description.action
            : `${description.detail} —— ${description.action}`}
        </p>
        <button type="button" onClick={onRetry} className="btn btn-sm" style={{ background: 'var(--surface-0)' }}>
          重新加载
        </button>
      </div>
    </div>
  )
}

// 汇总卡改用 `summary-grid` / `metric` 类（styles/shell.css）后，
// 旧的行内布局常量不再需要；空态与错误面板也已改用 `.empty` / `.notice` 类。
