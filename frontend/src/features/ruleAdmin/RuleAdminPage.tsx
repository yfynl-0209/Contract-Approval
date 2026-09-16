import { useState } from 'react'

import { asApiError, describeApiError } from '../../api/client'
import type { RuleRow } from '../../api/contracts'
import { useAuth } from '../../app/authContext'
import {
  MATCH_MODE_LABELS,
  missingCategories,
  describeRuleFailure,
  extraCategories,
  REQUIRED_RULE_CATEGORIES,
  RULE_STATUS_LABELS,
} from './ruleEdit'
import { useActivateValidation, useRules, type RuleListFilters } from './queries'
import { riskLevelLabel } from '../../domain/labels'
import { RuleForm } from './RuleForm'

/**
 * 扩展页：规则管理（M8 Task 8）。
 *
 * ## 权限形态与五个核心模块不同
 *
 * 后端把**整个模块**（含只读查询）锁在 `rule:manage` 上（只授给系统管理员）：
 * 规则是"系统怎么判"的输入，改一条会改变**所有**合同的结论。
 * 因此对非管理员，这一页给的是**解释**，而不是一张永远空的表 ——
 * 空表让人以为"还没有规则"，而真相是"你没有权限看"。
 *
 * 同理，未就绪/无权限时**不发列表请求**：那些请求注定 403，
 * 而控制台对着一个必败的接口重试，只会把"权限问题"伪装成"服务不稳定"。
 *
 * ## 激活前校验是这一页的核心动作
 *
 * 它不是"重新加载"（规则没有缓存），而是**闸门**：整批配置合法吗、
 * 11 类覆盖齐不齐。校验**包含停用的规则**（"先停用、再改、再启用"的流程里，
 * 停用期间的坏配置只有在启用那一刻才炸 —— 而那时它已经进了一个批次）。
 */
export function RuleAdminPage(): JSX.Element {
  const { status: authStatus, can } = useAuth()
  const [filters, setFilters] = useState<RuleListFilters>({
    page: 1,
    pageSize: 100,
    ruleStatus: null,
    ruleCategory: null,
    matchMode: null,
  })
  const [query, setQuery] = useState('')
  const [editingCode, setEditingCode] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  const canManage: boolean | null = authStatus === 'ready' ? can('rule:manage') : null
  const activate = useActivateValidation()

  // ⚠️ 权限未就绪 / 无权限时不发请求：它们注定 403，
  // 而对必败接口的自动重试会把"权限问题"伪装成"服务不稳定"
  const rules = useRules({ ...filters, page: 1 }, canManage === true)

  if (canManage === false) {
    return (
      <div style={{ padding: 'var(--space-3)' }}>
        <h1 style={{ fontSize: 'var(--font-size-md)' }}>规则管理</h1>
        <p>
          规则管理需要 <code>rule:manage</code> 权限（系统管理员）。
          规则是「系统怎么判」的输入 —— 改一条会影响**所有**合同的结论，
          因此它与「审这份合同」不是同一个权限层级。
        </p>
        <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
          这不是界面隐藏：后端对全部规则接口（含只读查询）独立校验权限。
          需要看规则对结论的影响，请到任务工作台的「规则命中」页。
        </p>
      </div>
    )
  }

  if (canManage === null || rules.isPending) {
    return <p aria-busy="true">正在加载规则…</p>
  }
  if (rules.isError) {
    const description = describeApiError(asApiError(rules.error))
    return (
      <div role="alert" style={{ padding: 'var(--space-3)' }}>
        <strong>{description.summary}</strong>
        <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
          {description.detail}——{description.action}
        </p>
        <button type="button" onClick={() => void rules.refetch()}>
          重新加载
        </button>
      </div>
    )
  }

  // 搜索只作用于**已加载**的这一页（后端没有 rule_code 过滤参数）
  const needle = query.trim().toLowerCase()
  const rows = rules.data.items.filter(
    (row) =>
      needle === '' ||
      row.rule_code.toLowerCase().includes(needle) ||
      row.rule_name.toLowerCase().includes(needle),
  )

  return (
    <div>
      <header style={{ marginBottom: 'var(--space-3)' }}>
        <h1 style={{ fontSize: 'var(--font-size-md)' }}>规则管理</h1>
        <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
          列表按**执行顺序**排列（优先级小的先跑）—— 与引擎一致，不是按创建时间。
          共 {rules.data.total} 条，本页显示 {rules.data.items.length} 条。
        </p>
      </header>

      <ValidationReport state={activate} />

      <div
        style={{
          display: 'flex',
          gap: 'var(--space-2)',
          alignItems: 'center',
          flexWrap: 'wrap',
          marginBottom: 'var(--space-3)',
          fontSize: 'var(--font-size-sm)',
        }}
      >
        <label>
          状态
          <select
            value={filters.ruleStatus ?? ''}
            onChange={(event) =>
              setFilters({ ...filters, ruleStatus: event.target.value === '' ? null : event.target.value })
            }
          >
            <option value="">全部</option>
            <option value="active">启用</option>
            <option value="inactive">停用</option>
          </select>
        </label>

        <label>
          匹配模式
          <select
            value={filters.matchMode ?? ''}
            onChange={(event) =>
              setFilters({ ...filters, matchMode: event.target.value === '' ? null : event.target.value })
            }
          >
            <option value="">全部</option>
            {(Object.keys(MATCH_MODE_LABELS) as (keyof typeof MATCH_MODE_LABELS)[]).map((mode) => (
              <option key={mode} value={mode}>
                {MATCH_MODE_LABELS[mode]}
              </option>
            ))}
          </select>
        </label>

        <label>
          类别
          <select
            value={filters.ruleCategory ?? ''}
            onChange={(event) =>
              setFilters({
                ...filters,
                ruleCategory: event.target.value === '' ? null : event.target.value,
              })
            }
          >
            <option value="">全部</option>
            {REQUIRED_RULE_CATEGORIES.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>

        <label>
          搜索
          <input
            type="search"
            value={query}
            placeholder="规则码 / 名称"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>

        <span style={{ marginLeft: 'auto', color: 'var(--text-3)', fontSize: 'var(--font-size-xs)' }}>
          {needle !== '' && `搜索作用于已加载的 ${rules.data.items.length} 条 · `}
          {rules.data.has_next && '还有更多（未在本页显示）'}
        </span>

        {!creating && (
          <button type="button" onClick={() => setCreating(true)}>
            新建规则
          </button>
        )}
      </div>

      {creating && <RuleForm mode="create" onClose={() => setCreating(false)} />}

      {rows.length === 0 ? (
        <p style={{ color: 'var(--text-2)' }}>
          {needle !== '' || filters.ruleStatus !== null || filters.matchMode !== null
            ? '当前筛选条件下没有规则 —— 清掉筛选再试。'
            : '还没有任何规则。规则由种子数据或「新建规则」产生。'}
        </p>
      ) : (
        <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
          {rows.map((row) =>
            editingCode === row.rule_code ? (
              <li key={row.rule_code}>
                <RuleForm mode="edit" rule={row} onClose={() => setEditingCode(null)} />
              </li>
            ) : (
              <RuleItem
                key={row.rule_code}
                row={row}
                onEdit={() => setEditingCode(row.rule_code)}
              />
            ),
          )}
        </ul>
      )}
    </div>
  )
}

/**
 * 激活前校验的报告（或失败时的**全部**问题）。
 *
 * ⚠️ 失败消息是后端逐条列出的（一次给全，不是第一个），因此原文
 * `pre-wrap` 完整显示 —— 截断它等于让人反复往返。
 */
function ValidationReport({
  state,
}: {
  readonly state: ReturnType<typeof useActivateValidation>
}): JSX.Element {
  const report = state.data

  return (
    <section aria-label="激活前校验" style={{ marginBottom: 'var(--space-3)' }}>
      <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'center' }}>
        <h2 style={{ fontSize: 'var(--font-size-sm)', margin: 0 }}>激活前校验</h2>
        <button type="button" onClick={() => state.mutate()} disabled={state.isPending}>
          {state.isPending ? '校验中…' : '运行校验'}
        </button>
      </div>

      {state.isError && (
        <ValidationFailure error={asApiError(state.error)} />
      )}

      {report !== undefined && !state.isPending && (
        <div data-testid="ruleset-report" style={{ fontSize: 'var(--font-size-sm)' }}>
          <p style={{ margin: 'var(--space-1) 0' }}>
            共 {report.total} 条（启用 {report.active} / 停用 {report.inactive}）
            {' · '}
            规则集版本 <code>{report.ruleset_version.slice(0, 12)}…</code>
            {' —— '}
            <b>配置通过</b>，下一个批次会带上这个版本。
          </p>
          <CoverageNote categories={report.categories} />
        </div>
      )}
    </section>
  )
}

function ValidationFailure({ error }: { readonly error: ReturnType<typeof asApiError> }): JSX.Element {
  const description = describeRuleFailure(error)
  return (
    <div
      role="alert"
      data-testid="validation-failure"
      style={{
        borderLeft: '4px solid var(--status-danger)',
        padding: 'var(--space-2)',
        marginTop: 'var(--space-2)',
      }}
    >
      <strong>{description.summary}</strong>
      <p style={{ whiteSpace: 'pre-wrap', fontSize: 'var(--font-size-sm)' }}>{description.detail}</p>
      <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>{description.action}</p>
    </div>
  )
}

/**
 * 11 类覆盖情况。
 *
 * ⚠️ "缺哪几类"由**后端的激活前校验**下结论（它会进 400 消息）；
 * 这里是对照需求 11 类的**提前提示**，不是第二份判据 ——
 * 所以后端报了问题、这里却说"齐了"时，以校验为准。
 */
function CoverageNote({
  categories,
}: {
  readonly categories: Readonly<Record<string, number>>
}): JSX.Element | null {
  const missing = missingCategories(categories)
  const extras = extraCategories(categories)

  return (
    <p style={{ margin: 0, color: 'var(--text-2)', fontSize: 'var(--font-size-xs)' }}>
      {missing.length === 0 ? (
        '需求规定的 11 类风险都有规则覆盖。'
      ) : (
        <>
          ⚠️ 需求 11 类里有 {missing.length} 类没有任何规则：
          {missing.join('、')}
          {' —— '}激活前校验会把它列为问题。
        </>
      )}
      {extras.length > 0 && <>（另有 {extras.length} 个非需求类别：{extras.join('、')}）</>}
    </p>
  )
}

function RuleItem({
  row,
  onEdit,
}: {
  readonly row: RuleRow
  readonly onEdit: () => void
}): JSX.Element {
  return (
    <li
      style={{
        borderBottom: '1px solid var(--border)',
        padding: 'var(--space-2) 0',
      }}
    >
      <div style={{ display: 'flex', gap: 'var(--space-2)', alignItems: 'baseline', flexWrap: 'wrap' }}>
        <span style={{ color: 'var(--text-3)', fontSize: 'var(--font-size-xs)' }}>
          #{row.priority}
        </span>
        <strong>{row.rule_code}</strong>
        <span>{row.rule_name}</span>
        <span
          style={{ fontSize: 'var(--font-size-xs)', color: riskColor(row.risk_level) }}
        >
          {riskLevelLabel(row.risk_level)}
        </span>
        <span style={{ fontSize: 'var(--font-size-xs)', color: 'var(--text-2)' }}>
          {MATCH_MODE_LABELS[row.match_mode]}
          {row.rule_category !== null && ` · ${row.rule_category}`}
        </span>
        <span style={{ fontSize: 'var(--font-size-xs)', color: 'var(--text-2)' }}>
          v{row.rule_version}
        </span>
        <span
          data-status={row.rule_status}
          style={{ fontSize: 'var(--font-size-xs)', marginLeft: 'auto' }}
        >
          {RULE_STATUS_LABELS[row.rule_status]}
        </span>
        <button type="button" onClick={onEdit}>
          修改
        </button>
      </div>

      <details style={{ marginTop: 'var(--space-1)' }}>
        <summary style={{ cursor: 'pointer', fontSize: 'var(--font-size-xs)', color: 'var(--text-2)' }}>
          配置详情
        </summary>
        <dl style={{ margin: 'var(--space-1) 0 0', fontSize: 'var(--font-size-xs)' }}>
          <dt>匹配内容</dt>
          <dd style={{ whiteSpace: 'pre-wrap' }}>{row.match_text}</dd>
          {row.applies_when_json !== null && (
            <>
              <dt>适用条件</dt>
              <dd style={{ whiteSpace: 'pre-wrap' }}>{row.applies_when_json}</dd>
            </>
          )}
          {row.fallback_match_json !== null && (
            <>
              <dt>降级条件</dt>
              <dd style={{ whiteSpace: 'pre-wrap' }}>{row.fallback_match_json}</dd>
            </>
          )}
          {row.exclude_text !== null && (
            <>
              <dt>排除词</dt>
              <dd>{row.exclude_text}</dd>
            </>
          )}
          {row.suggestion_text !== null && (
            <>
              <dt>修改建议</dt>
              <dd>{row.suggestion_text}</dd>
            </>
          )}
        </dl>
      </details>
    </li>
  )
}

function riskColor(level: RuleRow['risk_level']): string {
  switch (level) {
    case 'high':
      return 'var(--status-danger)'
    case 'medium':
      return 'var(--status-warn)'
    default:
      return 'var(--status-ok)'
  }
}
