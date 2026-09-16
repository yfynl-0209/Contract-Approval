import type { RuleFiltersState } from './ruleEvidence'

/**
 * 模块 4 的筛选条（M8 Task 6）。
 *
 * ## ⚠️ 这是**客户端**筛选，因此必须说清它作用在哪一批数据上
 *
 * 它只筛**已加载**的那一批评价。界面上因此同时给出"已加载 / 共"两个数字 ——
 * 少了后者时，"筛完只剩 2 条"与"系统只评估了 2 条规则"在视觉上分不开，
 * 而这两种情况的下一步完全不同（前者换个筛选条件，后者去查批次）。
 *
 * （当前数据源 `/api/runs/{id}` 整批返回，两个数字应当相等；
 * 但**界面的写法不依赖这一点** —— 后端哪天改成分页，这里不会变成谎话。）
 */
export function RuleFilters({
  filters,
  onChange,
  loaded,
  total,
}: {
  readonly filters: RuleFiltersState
  readonly onChange: (next: RuleFiltersState) => void
  readonly loaded: number
  readonly total: number
}): JSX.Element {
  return (
    <div
      style={{
        display: 'flex',
        gap: 'var(--space-3)',
        alignItems: 'center',
        flexWrap: 'wrap',
        marginBottom: 'var(--space-3)',
        fontSize: 'var(--font-size-sm)',
      }}
    >
      <label style={{ display: 'flex', gap: 'var(--space-1)', alignItems: 'center' }}>
        <input
          type="checkbox"
          checked={filters.onlyActionable}
          onChange={(event) =>
            onChange({ ...filters, onlyActionable: event.target.checked })
          }
        />
        只看需处理（待判断 + 命中）
      </label>

      <label style={{ display: 'flex', gap: 'var(--space-1)', alignItems: 'center' }}>
        规则码
        <input
          type="search"
          value={filters.query}
          placeholder="按规则名或编号搜索"
          onChange={(event) => onChange({ ...filters, query: event.target.value })}
        />
      </label>

      <span style={{ marginLeft: 'auto', color: 'var(--text-3)', fontSize: 'var(--font-size-xs)' }}>
        已加载 {loaded} / 共 {total} 条
        {loaded !== total && '（筛选只作用于已加载的部分）'}
      </span>
    </div>
  )
}
