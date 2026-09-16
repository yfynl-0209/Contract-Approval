import { Link, useParams, useSearchParams } from 'react-router-dom'
import { Suspense } from 'react'

import { formatLocalTime } from '../../domain/time'
import {
  WORKBENCH_TABS,
  WORKBENCH_TAB_LABELS,
  parseWorkbenchTab,
  routes,
  workbenchPath,
  type WorkbenchTab,
} from '../../app/routes'
import { useTaskDetail } from '../detail/queries'
import { TaskStatusCell, TaskWritebackCell, TaskRiskCell } from '../tasks/TaskStatusCell'
import { DetailTab } from '../detail/DetailTab'
import { ParseTab } from '../parse/ParseTab'
import { ResultTab } from '../result/ResultTab'
import { RuleTab } from '../rules/RuleTab'

/**
 * 任务工作台：**模块 2–5 的容器**（M8 Task 1；页头按前端重设计原型补齐）。
 *
 * ## 为什么四个视角收在一个路由里
 *
 * 它们是**同一份合同的四种看法**，审查人需要在"看到一条命中 → 跳证据 →
 * 确认结果"之间来回切。做成四个顶级页面会强迫用户反复进入退出并丢失上下文
 * （材料 §3）。
 *
 * ## ⚠️ tab 链接用 `replace`，不是 `push`
 *
 * 切换视角**不应产生新的历史记录**：否则用户点三次 tab 之后按"后退"，
 * 会依次退过三个视角才回到列表 —— 而他心里想的是"返回待办列表"。
 *
 * ## 页头数据与各页签共享同一份缓存
 *
 * 页头的状态徽章需要 `TaskDetail`，取数用与模块 2 **相同的 queryKey**
 * （`queryKeys.task(taskId)`）——从详情页签切过来时**不发新请求**，
 * 直接吃缓存；反过来先落在解析页签时取到的详情，详情页签也直接可用。
 */
export function TaskWorkbench(): JSX.Element {
  const { taskId } = useParams<{ taskId: string }>()
  const [searchParams] = useSearchParams()

  if (taskId === undefined) {
    // 路由表保证 `:taskId` 一定存在；真缺了说明路由被改动，
    // 这时**明确报错**比渲染一个"没有 id 的工作台"更容易定位。
    throw new Error('路由 /tasks/:taskId 缺少 taskId 参数')
  }

  const activeTab = parseWorkbenchTab(searchParams.get('tab'))
  const ActiveTab = TAB_ELEMENTS[activeTab]

  return (
    <section aria-labelledby="workbench-title">
      <nav aria-label="面包屑" style={{ marginBottom: 'var(--space-3)' }}>
        <Link to={routes.tasks} className="btn btn-sm">
          ← 返回待办列表
        </Link>
      </nav>

      <WorkbenchHeader taskId={taskId} />
      <div role="tablist" aria-label="任务视角" className="tabs">
        {WORKBENCH_TABS.map((tab) => {
          const selected = tab === activeTab
          return (
            <Link
              key={tab}
              to={workbenchPath(taskId, tab)}
              replace
              role="tab"
              aria-selected={selected}
              className={`tab${selected ? ' active' : ''}`}
            >
              {WORKBENCH_TAB_LABELS[tab]}
            </Link>
          )
        })}
      </div>

      <div role="tabpanel" style={{ paddingTop: 'var(--space-2)' }}>
        <Suspense fallback={null}>
          <ActiveTab />
        </Suspense>
      </div>
    </section>
  )
}

const TAB_ELEMENTS: Readonly<Record<WorkbenchTab, () => JSX.Element>> = {
  detail: DetailTab,
  parse: ParseTab,
  rules: RuleTab,
  result: ResultTab,
}

/**
 * 工作台页头：标题 + 元信息（合同编号 / 标题 / 申请人 / 更新时间）+ 状态徽章组。
 *
 * 徽章直接复用模块 1 的单元格组件（`TaskStatusCell` 等）——
 * 同一种状态在列表与工作台必须是**同一个词 + 同一种配色**，
 * 两份实现迟早漂移成"列表说审查中、工作台说进行中"。
 * 加载中显示骨架而非消失：下面的内容不会跳。
 */
function WorkbenchHeader({ taskId }: { readonly taskId: string }): JSX.Element {
  const detail = useTaskDetail(Number(taskId))

  return (
    <div className="wb-head">
      <div>
        {/* ⚠️ 标题用**业务信息**（合同标题，缺省回退单号），
            不用 URL 里的内部主键 —— "任务 #1"对审查人是噪音；
            数据没到之前给中性标题，不闪现一个假编号 */}
        <h1 id="workbench-title" className="wb-title" style={{ marginBottom: 'var(--space-1)' }}>
          {detail.data !== undefined
            ? (detail.data.approval_title ?? detail.data.approval_code)
            : '合同审查详情'}
        </h1>
        {detail.data !== undefined ? (
          <div className="wb-meta">
            {/* 标题已经是 h1 时单号降为元信息；标题缺省（h1=单号）时不再重复 */}
            {detail.data.approval_title !== null && (
              <span className="mono">{detail.data.approval_code}</span>
            )}
            {detail.data.applicant_name !== null && (
              <span>申请人：{detail.data.applicant_name}</span>
            )}
            <span>更新于 {formatLocalTime(detail.data.updated_at)}</span>
          </div>
        ) : (
          <div className="wb-meta" aria-busy="true">
            <span>正在加载任务信息…</span>
          </div>
        )}
      </div>

      <div className="wb-chips">
        {detail.data !== undefined ? (
          <>
            <TaskStatusCell task={detail.data} />
            <TaskRiskCell task={detail.data} />
            <TaskWritebackCell task={detail.data} />
          </>
        ) : null}
      </div>
    </div>
  )
}
