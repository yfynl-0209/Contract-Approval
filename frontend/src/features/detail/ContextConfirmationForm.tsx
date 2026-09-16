import { useState, type FormEvent } from 'react'

import { ApiError, describeApiError } from '../../api/client'
import type {
  BusinessRole,
  ContextConflict,
  ContextCorrection,
  ContractLabel,
  ContractType,
  TaskView,
} from '../../api/contracts'
import {
  BUSINESS_ROLE_LABELS,
  CONTEXT_STATUS_LABELS,
  CONTEXT_STATUS_NOTES,
  CONTRACT_LABEL_LABELS,
  CONTRACT_TYPE_LABELS,
  labelOf,
} from '../../domain/labels'
import { useAuth } from '../../app/authContext'
import { useConfirmContext } from './queries'

/**
 * 权威审查上下文：**展示 + 确认 + 修正**（M8 Task 4，设计 §4.2）。
 *
 * ## 这一块为什么排在页面最前面
 *
 * 它回答"我们代表谁"。方向敏感的规则（预付款比例高对采购方是风险、
 * 对销售方不是）全靠它决定**该不该判**。立场错了，整份结论的方向就错了 ——
 * 而报告看起来完全正常。
 *
 * ## 四种状态必须**分别**呈现（§4.2）
 *
 * | 状态 | 呈现 | 可用操作 |
 * | --- | --- | --- |
 * | `complete` | 来自审批系统 | 确认 / 修正 |
 * | `missing` | **警告**：方向敏感的规则判不了 | **必须修正**（确认会被后端拒绝） |
 * | `conflict` | **警告**：声明与实际不一致 | **必须裁定**（= 修正） |
 * | `confirmed` | 已人工确认 | 再次修正 |
 *
 * ⚠️ `missing` 是"刚拉取完任务"的**正常**状态（M3 已定），不是异常。
 * 静默显示成普通字段时，用户会以为系统已经知道立场了。
 *
 * ## 为什么 `missing` / `conflict` 下表单**直接展开**
 *
 * 这两种状态没有可确认的对象（后端会 400），唯一的出路是给出四条事实。
 * 把表单藏在一个"修正"按钮后面，等于让用户先点一次才知道要填什么 ——
 * 而他已经在一个"系统不知道该判什么"的页面上，不该再多猜一步。
 */

/** 从任务上取当前四条事实，作为修正表单的初值。 */
function toCorrection(task: TaskView): ContextCorrection {
  return {
    our_party_name: task.our_party_name ?? '',
    our_party_contract_label: task.our_party_contract_label ?? 'unknown',
    our_party_business_role: task.our_party_business_role ?? 'unknown',
    contract_type: task.contract_type ?? 'unknown',
  }
}

const STATUS_MARKS: Readonly<Record<TaskView['context_status'], string>> = {
  complete: '✓',
  missing: '⚠',
  conflict: '⚠',
  confirmed: '✓',
}

export function ContextConfirmationForm({
  task,
}: {
  readonly task: TaskView
}): JSX.Element {
  const { can } = useAuth()
  const confirm = useConfirmContext(task.task_id)

  const mustCorrect =
    task.context_status === 'missing' || task.context_status === 'conflict'
  const [editing, setEditing] = useState(mustCorrect)
  const [draft, setDraft] = useState<ContextCorrection>(() => toCorrection(task))

  const allowed = can('result:confirm')
  const error = confirm.error instanceof ApiError ? confirm.error : null

  const handleSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault()
    if (draft.our_party_name.trim() === '') {
      return
    }
    confirm.mutate({ ...draft, our_party_name: draft.our_party_name.trim() })
  }

  return (
    <section
      aria-labelledby="context-title"
      data-context-status={task.context_status}
      style={{
        border: '1px solid var(--border)',
        borderLeft: `4px solid ${statusColor(task.context_status)}`,
        borderRadius: 'var(--radius)',
        padding: 'var(--space-4)',
        marginBottom: 'var(--space-4)',
      }}
    >
      <h2 id="context-title" style={{ marginBottom: 'var(--space-2)' }}>
        权威审查上下文
      </h2>

      <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
        {/* 标记 + 文字同时出现：颜色只是辅助（不许只靠颜色表达） */}
        <strong style={{ color: statusColor(task.context_status) }}>
          {STATUS_MARKS[task.context_status]} {CONTEXT_STATUS_LABELS[task.context_status]}
        </strong>
        {' —— '}
        {CONTEXT_STATUS_NOTES[task.context_status]}
      </p>

      {task.context_conflict !== null && (
        <ContextConflictTable conflict={task.context_conflict} />
      )}

      <dl style={gridStyle}>
        <dt>我方</dt>
        <dd style={valueStyle}>{task.our_party_name ?? '—'}</dd>
        <dt>合同标签</dt>
        <dd style={valueStyle}>
          {task.our_party_contract_label === null
            ? '—'
            : CONTRACT_LABEL_LABELS[task.our_party_contract_label]}
        </dd>
        <dt>业务角色</dt>
        <dd style={valueStyle}>
          {task.our_party_business_role === null
            ? '—'
            : BUSINESS_ROLE_LABELS[task.our_party_business_role]}
        </dd>
        <dt>合同类型</dt>
        <dd style={valueStyle}>
          {task.contract_type === null ? '—' : CONTRACT_TYPE_LABELS[task.contract_type]}
        </dd>
      </dl>

      {error !== null && (
        <p role="alert" style={{ color: 'var(--status-danger)' }}>
          {/* 三段式（§5.5）：是什么 / 为什么 / 我现在能做什么 */}
          {describeApiError(error).summary}——{describeApiError(error).action}
        </p>
      )}

      {!allowed && (
        <p style={{ color: 'var(--text-3)', fontSize: 'var(--font-size-xs)' }}>
          当前身份没有确认权限（`result:confirm`），只能查看。
        </p>
      )}

      {allowed && !editing && (
        <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
          <button
            type="button"
            disabled={confirm.isPending}
            onClick={() => confirm.mutate(undefined)}
          >
            {confirm.isPending ? '提交中…' : '确认该立场'}
          </button>
          <button type="button" onClick={() => setEditing(true)}>
            人工修正
          </button>
        </div>
      )}

      {allowed && editing && (
        <form onSubmit={handleSubmit} aria-label="修正权威审查上下文">
          <div style={gridStyle}>
            <label htmlFor="our_party_name">我方名称</label>
            <input
              id="our_party_name"
              value={draft.our_party_name}
              onChange={(event) =>
                setDraft({ ...draft, our_party_name: event.target.value })
              }
              required
            />

            <label htmlFor="our_party_contract_label">合同标签</label>
            <select
              id="our_party_contract_label"
              value={draft.our_party_contract_label}
              onChange={(event) =>
                setDraft({
                  ...draft,
                  our_party_contract_label: event.target.value as ContractLabel,
                })
              }
            >
              {optionsOf(CONTRACT_LABEL_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>

            <label htmlFor="our_party_business_role">业务角色</label>
            <select
              id="our_party_business_role"
              value={draft.our_party_business_role}
              onChange={(event) =>
                setDraft({
                  ...draft,
                  our_party_business_role: event.target.value as BusinessRole,
                })
              }
            >
              {optionsOf(BUSINESS_ROLE_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>

            <label htmlFor="contract_type">合同类型</label>
            <select
              id="contract_type"
              value={draft.contract_type}
              onChange={(event) =>
                setDraft({
                  ...draft,
                  contract_type: event.target.value as ContractType,
                })
              }
            >
              {optionsOf(CONTRACT_TYPE_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>

          <p style={{ color: 'var(--text-3)', fontSize: 'var(--font-size-xs)' }}>
            四条必须<strong>一起</strong>提交：只改其中两条时，剩下的还是旧值，
            「我方是谁」与「这对我是好是坏」可能自相矛盾。
          </p>

          <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
            <button type="submit" disabled={confirm.isPending}>
              {confirm.isPending ? '提交中…' : '提交并确认'}
            </button>
            {!mustCorrect && (
              <button type="button" onClick={() => setEditing(false)}>
                取消
              </button>
            )}
          </div>
        </form>
      )}
    </section>
  )
}

/**
 * 冲突双方对照表（设计 §4.2 要求 `conflict` 时"显示冲突双方"）。
 *
 * ⚠️ 光说"冲突了"没用：人要做的是**裁定哪一边对**，而他得先看到两个值。
 * 因此这里把两个来源逐项并排，并把**不一致的项**标出来（标记 + 文案，
 * 不只靠颜色）—— 否则他得自己逐行比四个字段。
 */
function ContextConflictTable({
  conflict,
}: {
  readonly conflict: ContextConflict
}): JSX.Element {
  const rows: ReadonlyArray<{
    readonly label: string
    readonly declared: string
    readonly confirmed: string
  }> = [
    {
      label: '我方名称',
      declared: conflict.declared.our_party_name ?? '—',
      confirmed: conflict.confirmed.our_party_name ?? '—',
    },
    {
      label: '合同标签',
      declared: labelOf(CONTRACT_LABEL_LABELS, conflict.declared.our_party_contract_label),
      confirmed: labelOf(
        CONTRACT_LABEL_LABELS,
        conflict.confirmed.our_party_contract_label,
      ),
    },
    {
      label: '业务角色',
      declared: labelOf(BUSINESS_ROLE_LABELS, conflict.declared.our_party_business_role),
      confirmed: labelOf(
        BUSINESS_ROLE_LABELS,
        conflict.confirmed.our_party_business_role,
      ),
    },
    {
      label: '合同类型',
      declared: labelOf(CONTRACT_TYPE_LABELS, conflict.declared.contract_type),
      confirmed: labelOf(CONTRACT_TYPE_LABELS, conflict.confirmed.contract_type),
    },
  ]

  return (
    <table
      aria-label="立场冲突对照"
      style={{ width: '100%', borderCollapse: 'collapse', marginBottom: 'var(--space-3)' }}
    >
      <caption
        style={{
          textAlign: 'left',
          color: 'var(--status-warn)',
          paddingBottom: 'var(--space-1)',
        }}
      >
        审批单声明与人工确认不一致 —— 请裁定哪一边对（回写已暂停）
      </caption>
      <thead>
        <tr style={{ background: 'var(--surface-2)', textAlign: 'left' }}>
          <th scope="col">业务事实</th>
          <th scope="col">审批单声明</th>
          <th scope="col">人工确认</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const differs = row.declared !== row.confirmed
          return (
            <tr key={row.label} style={{ borderBottom: '1px solid var(--border)' }}>
              <td>{row.label}</td>
              <td>{row.declared}</td>
              <td>
                {row.confirmed}
                {differs && (
                  <span
                    style={{ color: 'var(--status-warn)', paddingLeft: 'var(--space-2)' }}
                  >
                    ← 与声明不同
                  </span>
                )}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

/** 枚举映射 → `<option>` 用的键值对（**取值域来自类型**，不在这里再列一遍）。 */
function optionsOf(
  map: Readonly<Record<string, string>>,
): ReadonlyArray<readonly [string, string]> {
  return Object.entries(map)
}

function statusColor(status: TaskView['context_status']): string {
  switch (status) {
    case 'complete':
      return 'var(--status-ok)'
    case 'confirmed':
      return 'var(--accent)'
    case 'conflict':
      return 'var(--status-warn)'
    case 'missing':
      return 'var(--status-warn)'
  }
}

const gridStyle = {
  display: 'grid',
  gridTemplateColumns: 'max-content 1fr',
  gap: 'var(--space-2) var(--space-3)',
  alignItems: 'center',
  margin: '0 0 var(--space-3)',
} as const

const valueStyle = { margin: 0 } as const
