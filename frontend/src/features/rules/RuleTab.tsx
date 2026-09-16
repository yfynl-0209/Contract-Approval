import { useState } from 'react'

import { asApiError, describeApiError } from '../../api/client'
import { useTaskId } from '../../app/useTaskId'
import {
  EVALUATION_STATUS_LABELS,
  REVIEW_STATUS_LABELS,
  RUN_STATUS_LABELS,
  riskLevelLabel,
} from '../../domain/labels'
import { formatLocalTime } from '../../domain/time'
import { useTaskDetail } from '../detail/queries'
import { EvaluationCard } from './EvaluationCard'
import { RuleFilters } from './RuleFilters'
import {
  filterEvaluations,
  groupEvaluations,
  readRunEvaluations,
  type RuleFiltersState,
} from './ruleEvidence'
import { useRun } from './queries'

/**
 * 模块 4：规则命中（M8 Task 6，设计 §4.4）。
 *
 * 目标：回答"我要关注什么"，并且**默认不展示噪音**。
 *
 * ## 数据源的选择（这一条决定了这个页面会不会撒谎）
 *
 * 用 `GET /api/runs/{id}`，**不用** `GET /api/evaluations`：
 *
 * | 维度 | `/api/runs/{id}` | `/api/evaluations` |
 * | --- | --- | --- |
 * | 计数 | **后端现算的聚合**（四态齐全、和列表同源） | 无（只能自己数已加载的那一页） |
 * | 评价 | **整批返回** | 分页 |
 * | 风险/完整性 | 有（`aggregate`） | 无 |
 *
 * 用分页接口渲染"四态 + 计数"时，数字来自**已加载的那一页**，
 * 而界面写着"不适用 31" —— 数据少于一页时两者永远相等，
 * 于是这个缺陷会一直活到数据量上来（M8 验收 4："统计与风险等级全部来自后端"）。
 *
 * ## 四态与折叠
 *
 * 顺序固定 `needs_review → hit → not_hit → not_applicable`；
 * 前两组默认展开，后两组折叠但**显示计数** —— 折叠不等于隐藏：
 * 用户要能一眼确认"系统确实评估了 N 条规则"。
 */
export function RuleTab(): JSX.Element {
  const { taskId } = useTaskId()
  const task = useTaskDetail(taskId)
  const [filters, setFilters] = useState<RuleFiltersState>({
    onlyActionable: false,
    query: '',
  })

  const runId = task.data?.latest_run_id ?? null
  const run = useRun(runId)

  if (task.isPending) {
    return <p aria-busy="true">正在加载任务…</p>
  }
  if (task.isError) {
    const description = describeApiError(asApiError(task.error))
    return (
      <div role="alert" style={{ padding: 'var(--space-3)' }}>
        <strong>{description.summary}</strong>
        <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
          {description.detail}——{description.action}
        </p>
        <button type="button" onClick={() => void task.refetch()}>
          重新加载
        </button>
      </div>
    )
  }
  if (runId === null) {
    return (
      <p style={{ color: 'var(--text-2)' }}>
        这份合同还没有审查批次。规则评价由规则引擎在一次批次运行中产生 ——
        批次的入口在外部调用方（工具 5）或后台作业，控制台不触发它。
      </p>
    )
  }
  if (run.isPending) {
    return <p aria-busy="true">正在加载批次评价…</p>
  }
  if (run.isError) {
    const description = describeApiError(asApiError(run.error))
    return (
      <div role="alert" style={{ padding: 'var(--space-3)' }}>
        <strong>{description.summary}</strong>
        <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
          {description.detail}——{description.action}
        </p>
        <button type="button" onClick={() => void run.refetch()}>
          重新加载
        </button>
      </div>
    )
  }

  const items = readRunEvaluations(run.data.evaluations)
  const visible = filterEvaluations(items, filters)
  const groups = groupEvaluations(visible)
  const { counts, overall_risk_level: overallRisk, review_status: reviewStatus } = run.data.aggregate
  // ⚠️ 计数**取自后端**（不数数组）：`counts` 的四个键由后端穷举初始化，
  // 因此"不适用 0"确实是 0，而不是"没有这个键"
  const total =
    counts.hit + counts.not_hit + counts.not_applicable + counts.needs_review
  const incomplete = run.data.run_status !== 'completed'

  return (
    <div>
      <header style={{ marginBottom: 'var(--space-3)' }}>
        <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'baseline', flexWrap: 'wrap' }}>
          {/* 批次主键不进正文："批次 #17"对审查人是噪音；排障时悬停可见 */}
          <strong title={`批次编号：${run.data.run_id}`}>审查批次</strong>
          <span style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
            v{run.data.version_no} · {RUN_STATUS_LABELS[run.data.run_status]}
            {run.data.started_at !== null && ` · ${formatLocalTime(run.data.started_at)}`}
          </span>
        </div>

        <div style={{ fontSize: 'var(--font-size-sm)', marginTop: 'var(--space-1)' }} data-testid="run-counts">
          命中 {counts.hit} · 待判断 {counts.needs_review} · 未命中 {counts.not_hit} · 不适用{' '}
          {counts.not_applicable}（共 {total} 条规则）
        </div>

        <div style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-xs)', marginTop: 'var(--space-1)' }}>
          总风险 {riskLevelLabel(overallRisk)} · 结论完整性 {REVIEW_STATUS_LABELS[reviewStatus]}
          {run.data.ruleset_version !== null && (
            <>
              {' · 规则集 '}
              {/* ⚠️ 64 位指纹直排会把整行撑乱：只露前 8 位，完整值悬停可见
                  （排障对版本时用得上，但不该占每个审查人的视线） */}
              <code title={run.data.ruleset_version}>{run.data.ruleset_version.slice(0, 8)}…</code>
            </>
          )}
        </div>

        {total !== items.length && (
          // 聚合（后端现算）与加载到的条目数不一致。理论上不会发生（整批返回），
          // 因此**一旦发生就该被看见**：它意味着"列出来的"与"统计的"不是同一批数据 ——
          // 而那种不一致最容易被读成"系统漏了几条"或者"数字是错的"
          <p
            role="status"
            data-testid="count-mismatch"
            style={{
              marginTop: 'var(--space-2)',
              padding: 'var(--space-2)',
              borderLeft: '4px solid var(--status-danger)',
              fontSize: 'var(--font-size-sm)',
            }}
          >
            统计与条目数不一致：聚合说有 {total} 条评价，而本页只加载到 {items.length} 条。
            上方的计数以后端聚合为准，下方分组按已加载的条目渲染。
          </p>
        )}

        {incomplete && (
          // ⚠️ 后端在 `/api/runs/{id}` 的说明里点明了这件事：把半截批次当完整结论，
          // 会得到一份"低风险"，因为我们**还没算完**
          <p
            role="status"
            style={{
              marginTop: 'var(--space-2)',
              padding: 'var(--space-2)',
              borderLeft: '4px solid var(--status-warn)',
              color: 'var(--text-2)',
              fontSize: 'var(--font-size-sm)',
            }}
          >
            这个批次还没跑完，下面的计数与结论**只反映已经落库的那部分评价** ——
            不要把当前结果当成最终结论。
          </p>
        )}
      </header>

      <RuleFilters
        filters={filters}
        onChange={setFilters}
        loaded={visible.length}
        total={items.length}
      />

      {items.length === 0 && (
        // ⚠️ 这句话只在**一处**说（而不是四个空组各说一遍）：
        // 重复四遍时它会被读成四个各自独立的问题，而事实是同一个
        <p
          role="status"
          data-testid="no-evaluations"
          style={{
            borderLeft: '4px solid var(--status-warn)',
            padding: 'var(--space-2)',
            fontSize: 'var(--font-size-sm)',
          }}
        >
          这个批次里没有任何评价记录 —— 这不等于「没有风险」，请检查批次是否真的跑过。
        </p>
      )}

      {groups.map((group) => (
        <details
          key={group.status}
          open={group.defaultOpen}
          data-status={group.status}
          style={{ marginBottom: 'var(--space-3)' }}
        >
          <summary style={{ cursor: 'pointer', fontWeight: 600 }}>
            {group.label}（{group.count}）
            {group.status === 'needs_review' && group.count > 0 && (
              <span style={{ fontWeight: 400, color: 'var(--text-2)' }}>
                —— 结论需要你来做
              </span>
            )}
          </summary>

          {group.items.length === 0 ? (
            <p style={{ color: 'var(--text-3)', fontSize: 'var(--font-size-sm)' }}>
              {emptyGroupNote(group.status, items.length, visible.length)}
            </p>
          ) : (
            <ul style={{ listStyle: 'none', padding: 0, margin: 'var(--space-2) 0 0' }}>
              {group.items.map((item) => (
                <EvaluationCard
                  key={`${item.rule_code}:${item.evaluation_status}`}
                  evaluation={item}
                  taskId={taskId}
                />
              ))}
            </ul>
          )}
        </details>
      ))}
    </div>
  )
}

/**
 * 空组的说明（`null` = 不显示说明）。
 *
 * ⚠️ 三种空**不是同一件事**，文案必须分开：
 *
 * | 情形 | 事实 | 说明 |
 * | --- | --- | --- |
 * | 没有任何评价 | 批次可能没跑或没落库 | 由页面顶部的**单条**通知说明（不在这里重复四遍） |
 * | 被筛选/搜索排掉 | 数据在，只是没显示 | "清掉筛选就能看到" |
 * | 真的没有这一类 | "系统评估过，结论就是不适用" | 空本身也是一个结论 |
 */
function emptyGroupNote(
  status: keyof typeof EVALUATION_STATUS_LABELS,
  total: number,
  visible: number,
): string | null {
  if (total === 0) {
    return null
  }
  if (visible < total) {
    return '当前筛选条件下没有这一类 —— 清掉筛选就能看到。'
  }
  return `系统评估过，结论是「${EVALUATION_STATUS_LABELS[status]}」—— 这一类为空本身也是一个结论。`
}
