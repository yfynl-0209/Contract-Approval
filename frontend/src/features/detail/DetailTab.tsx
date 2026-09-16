import { useState } from 'react'
import { Link } from 'react-router-dom'

import { asApiError, describeApiError } from '../../api/client'
import type { JsonValue, TaskDetail } from '../../api/contracts'
import { routes, workbenchPath } from '../../app/routes'
import { useTaskId } from '../../app/useTaskId'
import { formatLocalTime } from '../../domain/time'
import { displayValue, isSensitiveKey, maskValue } from '../../domain/masking'
import { AttachmentList } from './AttachmentList'
import { ContextConfirmationForm } from './ContextConfirmationForm'
import { useAttachments, useTaskDetail } from './queries'

/**
 * 模块 2：详情查看（M8 Task 4，设计 §4.2）。
 *
 * 区块顺序按**审查人的决策顺序**，不按数据库表顺序：
 * 立场 → 表单 → 附件 → 链路指针。
 *
 * ⚠️ 三个区块的数据来自**三个**请求（详情 / 附件），各自有自己的加载与失败态：
 * 一个区块失败不该让整页变成错误页 —— 那会让用户以为"这份合同打不开"，
 * 而实际只是附件列表出了点问题。
 */

export function DetailTab(): JSX.Element {
  const { taskId } = useTaskId()
  const detail = useTaskDetail(taskId)
  const attachments = useAttachments(taskId)

  if (detail.isPending) {
    return <p aria-busy="true">正在加载详情…</p>
  }

  if (detail.isError || detail.data === undefined) {
    return <ErrorPanel error={detail.error} onRetry={() => void detail.refetch()} />
  }

  const task: TaskDetail = detail.data

  return (
    <div>
      <ContextConfirmationForm task={task} />

      <section aria-labelledby="form-data-title" style={sectionStyle}>
        <h2 id="form-data-title">审批表单</h2>
        <FormDataSection data={task.form_data} />
      </section>

      <section aria-labelledby="attachments-title" style={sectionStyle}>
        <h2 id="attachments-title">附件</h2>
        {attachments.isPending ? (
          <p aria-busy="true">正在加载附件…</p>
        ) : attachments.isError ? (
          <p role="alert" style={{ color: 'var(--status-danger)' }}>
            附件列表加载失败：{describeApiError(asApiError(attachments.error)).action}
          </p>
        ) : (
          <AttachmentList
            attachments={attachments.data?.items ?? []}
            total={attachments.data?.total ?? 0}
            lastErrorCode={task.last_error_code}
            lastErrorIsBusinessFact={task.last_error_is_business_fact}
          />
        )}
      </section>

      <section aria-labelledby="chain-title" style={sectionStyle}>
        <h2 id="chain-title">链路指针</h2>
        <dl style={gridStyle}>
          <dt>合同文件</dt>
          <dd style={valueStyle}>
            {/* ⚠️ 不展示内部主键（`#1`）：审查人不关心第几条解析记录，
                只关心"有没有、能不能看"。要看数据进解析视角。 */}
            {task.latest_parse_id === null ? (
              '还没有解析'
            ) : (
              <Link to={workbenchPath(task.task_id, 'parse')}>已解析 · 查看</Link>
            )}
          </dd>
          <dt>审查批次</dt>
          <dd style={valueStyle}>
            {task.latest_run_id === null ? (
              '还没有审查'
            ) : (
              <Link to={workbenchPath(task.task_id, 'rules')}>已审查 · 查看</Link>
            )}
          </dd>
          <dt>审查结论</dt>
          <dd style={valueStyle}>
            {task.current_result_id === null ? (
              '还没有结论'
            ) : (
              <Link to={workbenchPath(task.task_id, 'result')}>已生成 · 查看</Link>
            )}
          </dd>
          <dt>创建时间</dt>
          <dd style={valueStyle}>
            <time dateTime={task.created_at ?? undefined}>
              {formatLocalTime(task.created_at)}
            </time>
          </dd>
          <dt>关联 ID</dt>
          <dd style={valueStyle}>
            {/*
              ⚠️ 展示但不复制到 URL：它是排障时给运维的键，
              粘进地址栏等于把它写进浏览器历史与访问日志（它本身不敏感，
              但"顺手把内部标识放 URL"这个习惯会蔓延到真正敏感的东西上）
            */}
            <code>{task.correlation_id ?? '—'}</code>
          </dd>
        </dl>
        <p style={{ fontSize: 'var(--font-size-xs)' }}>
          查看解析结果：
          <Link to={workbenchPath(task.task_id, 'parse')}>解析结果视角</Link>
          {' · '}
          <Link to={workbenchPath(task.task_id, 'rules')}>规则命中视角</Link>
          {' · '}
          <Link to={workbenchPath(task.task_id, 'result')}>结果处理视角</Link>
          {' · '}
          <Link to={routes.tasks}>返回列表</Link>
        </p>
      </section>
    </div>
  )
}



// ============================================================
// 审批表单（敏感值默认掩码）
// ============================================================

/**
 * 审批表单键值对。
 *
 * ⚠️ **敏感值默认掩码**（§4.2），点"显示"才展开。默认展示原文时，
 * 泄漏发生在**截图那一刻**（而控制台就是要被截图贴进审批流的），事后追不回。
 *
 * ⚠️ 值只存在于组件状态里：**不写 URL、不写 console、不上报**。
 */
export function FormDataSection({
  data,
}: {
  readonly data: JsonValue
}): JSX.Element {
  const entries = toEntries(data)

  if (entries === null) {
    return (
      <p style={{ color: 'var(--text-2)' }}>没有可展示的审批表单字段。</p>
    )
  }

  return (
    <dl style={gridStyle}>
      {entries.map(([name, value]) => (
        <FormDataRow key={name} name={name} value={value} />
      ))}
    </dl>
  )
}

function FormDataRow({
  name,
  value,
}: {
  readonly name: string
  readonly value: JsonValue
}): JSX.Element {
  const [revealed, setRevealed] = useState(false)
  const sensitive = isSensitiveKey(name)

  return (
    <>
      <dt>{name}</dt>
      <dd style={valueStyle}>
        <span>{sensitive && !revealed ? maskValue(value) : displayValue(value)}</span>
        {sensitive && (
          <button
            type="button"
            onClick={() => setRevealed((current) => !current)}
            aria-label={`${revealed ? '隐藏' : '显示'} ${name}`}
            style={linkButtonStyle}
          >
            {revealed ? '隐藏' : '显示'}
          </button>
        )}
      </dd>
    </>
  )
}

/** 把表单对象转成键值对列表；不是对象时返回 `null`（调用方给一句说明）。 */
function toEntries(data: JsonValue): ReadonlyArray<readonly [string, JsonValue]> | null {
  if (data === null || data === undefined) {
    return null
  }
  if (typeof data !== 'object' || Array.isArray(data)) {
    return null
  }
  const entries = Object.entries(data)
  return entries.length === 0 ? null : entries
}

// ============================================================
// 加载 / 错误
// ============================================================

function ErrorPanel({
  error,
  onRetry,
}: {
  readonly error: unknown
  readonly onRetry: () => void
}): JSX.Element {
  const description = describeApiError(asApiError(error))
  return (
    <div role="alert" style={{ padding: 'var(--space-4)' }}>
      <strong>{description.summary}</strong>
      <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
        {description.detail}——{description.action}
      </p>
      <button type="button" onClick={onRetry}>
        重新加载
      </button>
    </div>
  )
}

const sectionStyle = {
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius)',
  padding: 'var(--space-4)',
  marginBottom: 'var(--space-4)',
} as const

const gridStyle = {
  display: 'grid',
  gridTemplateColumns: 'max-content 1fr',
  gap: 'var(--space-2) var(--space-3)',
  margin: 0,
} as const

const valueStyle = { margin: 0 } as const

const linkButtonStyle = {
  border: 'none',
  background: 'none',
  color: 'var(--accent)',
  cursor: 'pointer',
  padding: '0 0 0 var(--space-2)',
  fontSize: 'var(--font-size-xs)',
} as const
