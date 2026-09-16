import type { TaskStatus } from '../../api/contracts'
import { TASK_STATUS_LABELS } from '../../domain/labels'

/**
 * 列表筛选条（M8 Task 3）。
 *
 * ## 用原生 `<select>`
 *
 * 不是为了省事：原生控件的键盘操作、读屏支持、移动端选择器都是白拿的，
 * 而自绘下拉要自己把这三样重新实现一遍（通常实现不全，且没人测）。
 * 这里的交互规模不值得那个代价。
 *
 * ## 筛选值只允许白名单
 *
 * 传出去的 `taskStatus` 只能是五个枚举值之一（页面在解析 URL 时已经过滤过）。
 * 顺带一提：非法值后端会返回 **400 而不是空列表** —— 两者的区别是
 * "这个过滤值不存在"与"没有符合条件的任务"，而空列表会把前者说成后者。
 */

const STATUS_OPTIONS: readonly TaskStatus[] = [
  'pending',
  'parsing',
  'reviewing',
  'blocked',
  'done',
]

export interface TaskFiltersProps {
  readonly status: TaskStatus | null
  readonly onStatusChange: (status: TaskStatus | null) => void
  readonly onRefresh: () => void
  readonly isRefreshing: boolean
}

export function TaskFilters({
  status,
  onStatusChange,
  onRefresh,
  isRefreshing,
}: TaskFiltersProps): JSX.Element {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 'var(--space-4)',
        marginBottom: 'var(--space-3)',
      }}
    >
      <label style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
        状态
        <select
          value={status ?? ''}
          onChange={(event) => {
            const value = event.target.value
            // 空串表示"全部"：这是**筛选的缺省**，而不是"某一种状态"
            onStatusChange(value === '' ? null : (value as TaskStatus))
          }}
        >
          <option value="">全部</option>
          {STATUS_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {TASK_STATUS_LABELS[option]}
            </option>
          ))}
        </select>
      </label>

      <button type="button" onClick={onRefresh} disabled={isRefreshing}>
        {isRefreshing ? '正在刷新…' : '刷新'}
      </button>
    </div>
  )
}
