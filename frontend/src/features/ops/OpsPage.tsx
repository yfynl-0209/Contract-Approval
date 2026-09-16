import { useState } from 'react'

import { asApiError, describeApiError } from '../../api/client'
import type { ApiError } from '../../api/client'
import { useAuth } from '../../app/authContext'
import { BLOCKED_STAGE_LABELS, JOB_STATUS_LABELS, labelOf } from '../../domain/labels'
import { formatLocalTime } from '../../domain/time'
import { describeRetryFailure, jobSummary, retrySummary, type FailureText } from './opsState'
import { useAudit, useJobs, useLogs, useRetry, type AuditFilters, type JobListFilters, type LogFilters } from './queries'

/**
 * 扩展页：运行管理（M8 Task 8）—— 作业 / 日志 / 审计 / 检查点重试。
 *
 * ## 三个查询、三种权限
 *
 * 日志是**过程证据**（法务审核人需要看自己的合同卡在哪，`task:read` 即可）；
 * 重试是**运维动作**（重新排作业、消耗预算、改任务状态，`ops:retry`）；
 * 审计是**追责账**（只追加，`audit:read`）。把前两者也锁进管理员权限，
 * 审核人就只能去问运维 —— 而运维能看到的并不比日志更多。
 *
 * ## 这一页**没有**"编辑任务状态"的入口（材料的硬要求）
 *
 * 任务状态只能由流程（作业成功/失败）与检查点重试改变。
 * 给一个状态下拉框，等于把状态机从系统手里拿走 ——
 * 而"blocked 的任务被人手改成 reviewing"看起来像恢复了，实际什么都没跑。
 */
export function OpsPage(): JSX.Element {
  const { status: authStatus, can } = useAuth()

  const canRetry: boolean | null = authStatus === 'ready' ? can('ops:retry') : null
  const canAudit: boolean | null = authStatus === 'ready' ? can('audit:read') : null

  const [jobTaskId, setJobTaskId] = useState('')
  const [jobStatus, setJobStatus] = useState<string | null>(null)
  const jobFilters: JobListFilters = {
    taskId: jobTaskId === '' ? null : (Number.parseInt(jobTaskId, 10) || null),
    jobStatus,
  }

  const [logTaskId, setLogTaskId] = useState('')
  const [logLevel, setLogLevel] = useState<string | null>(null)
  const [logType, setLogType] = useState<string | null>(null)
  const [correlationId, setCorrelationId] = useState('')
  const logFilters: LogFilters = {
    logLevel,
    logType,
    correlationId: correlationId === '' ? null : correlationId,
  }
  const logTaskIdNumber = logTaskId === '' ? null : (Number.parseInt(logTaskId, 10) || null)

  const [auditTaskId, setAuditTaskId] = useState('')
  const [auditAction, setAuditAction] = useState<string | null>(null)
  const [includeSystem, setIncludeSystem] = useState(false)
  const auditFilters: AuditFilters = {
    taskId: auditTaskId === '' ? null : (Number.parseInt(auditTaskId, 10) || null),
    action: auditAction,
    includeSystem,
  }

  const canQuery = authStatus === 'ready'
  const jobs = useJobs(jobFilters, canQuery)
  const logs = useLogs(logTaskIdNumber ?? 0, logFilters, canQuery && logTaskIdNumber !== null)
  const audit = useAudit(auditFilters, canQuery && canAudit === true)

  return (
    <div>
      <header style={{ marginBottom: 'var(--space-3)' }}>
        <h1 style={{ fontSize: 'var(--font-size-md)' }}>运行管理</h1>
        <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
          作业是"系统现在在干什么"，日志是"它经历了什么"，审计是"谁做了什么决定"。
          任务状态**没有**手工编辑入口 —— 它只能由流程与检查点重试改变。
        </p>
      </header>

      <section aria-label="人工重试" style={{ marginBottom: 'var(--space-4)' }}>
        <h2 style={{ fontSize: 'var(--font-size-sm)' }}>检查点重试</h2>
        <RetryPanel enabled={canRetry} />
      </section>

      <section aria-label="作业" style={{ marginBottom: 'var(--space-4)' }}>
        <h2 style={{ fontSize: 'var(--font-size-sm)' }}>作业</h2>
        <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap', marginBottom: 'var(--space-2)' }}>
          <label>
            任务编号
            <input
              type="number"
              min={1}
              value={jobTaskId}
              onChange={(event) => setJobTaskId(event.target.value)}
              placeholder="留空看全部"
            />
          </label>
          <label>
            状态
            <select
              value={jobStatus ?? ''}
              onChange={(event) => setJobStatus(event.target.value === '' ? null : event.target.value)}
            >
              <option value="">全部</option>
              {(Object.keys(JOB_STATUS_LABELS) as (keyof typeof JOB_STATUS_LABELS)[]).map((status) => (
                <option key={status} value={status}>
                  {JOB_STATUS_LABELS[status]}
                </option>
              ))}
            </select>
          </label>
        </div>
        <JobList state={jobs} />
      </section>

      <section aria-label="运行日志" style={{ marginBottom: 'var(--space-4)' }}>
        <h2 style={{ fontSize: 'var(--font-size-sm)' }}>运行日志</h2>
        <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap', marginBottom: 'var(--space-2)' }}>
          <label>
            任务编号
            <input
              type="number"
              min={1}
              value={logTaskId}
              onChange={(event) => setLogTaskId(event.target.value)}
              placeholder="必填"
            />
          </label>
          <label>
            级别
            <select
              value={logLevel ?? ''}
              onChange={(event) => setLogLevel(event.target.value === '' ? null : event.target.value)}
            >
              <option value="">全部</option>
              <option value="debug">debug</option>
              <option value="info">info</option>
              <option value="warning">warning</option>
              <option value="error">error</option>
            </select>
          </label>
          <label>
            类型
            <input
              value={logType ?? ''}
              onChange={(event) => setLogType(event.target.value === '' ? null : event.target.value)}
              placeholder="pull / parse / rule / …"
            />
          </label>
          <label>
            关联 ID
            <input
              value={correlationId}
              onChange={(event) => setCorrelationId(event.target.value)}
              placeholder="按一次请求的全链路过滤"
            />
          </label>
        </div>
        <LogList state={logs} hasTaskId={logTaskIdNumber !== null} />
      </section>

      <section aria-label="审计" style={{ marginBottom: 'var(--space-4)' }}>
        <h2 style={{ fontSize: 'var(--font-size-sm)' }}>审计</h2>
        {canAudit === false ? (
          <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
            审计需要 `audit:read` 权限（系统管理员）。审计账是**只追加**的追责凭据，
            不是运行日志 —— 后者请看上面的「运行日志」。
          </p>
        ) : canAudit === null ? (
          <p aria-busy="true" style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
            正在获取身份…
          </p>
        ) : (
          <>
            <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap', marginBottom: 'var(--space-2)' }}>
              <label>
                任务编号
                <input
                  type="number"
                  min={1}
                  value={auditTaskId}
                  onChange={(event) => setAuditTaskId(event.target.value)}
                  placeholder="留空看全部任务"
                />
              </label>
              <label>
                动作
                <input
                  value={auditAction ?? ''}
                  onChange={(event) => setAuditAction(event.target.value === '' ? null : event.target.value)}
                  placeholder="RESULT_CONFIRMED / …（白名单，拼错 400）"
                />
              </label>
              <label style={{ display: 'flex', gap: 'var(--space-1)', alignItems: 'center' }}>
                <input
                  type="checkbox"
                  checked={includeSystem}
                  onChange={(event) => setIncludeSystem(event.target.checked)}
                />
                包含系统级事件（规则变更）
              </label>
            </div>
            <AuditList state={audit} />
          </>
        )}
      </section>
    </div>
  )
}

// ============================================================
// 重试
// ============================================================

function RetryPanel({ enabled }: { readonly enabled: boolean | null }): JSX.Element {
  const [taskId, setTaskId] = useState('')
  const [reason, setReason] = useState('')
  const retry = useRetry()

  if (enabled === false) {
    return (
      <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
        重试需要 `ops:retry` 权限（系统管理员）：它会重新排作业、消耗重试预算、改变任务状态。
        这不是安全边界 —— 后端独立校验，界面只是不给你一个点了会失败的动作。
      </p>
    )
  }
  if (enabled === null) {
    return <p aria-busy="true" style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>正在获取身份…</p>
  }

  const taskIdNumber = taskId === '' ? null : (Number.parseInt(taskId, 10) || null)
  const reasonFilled = reason.trim() !== ''

  return (
    <div>
      <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-xs)' }}>
        可恢复的检查点：
        {Object.keys(BLOCKED_STAGE_LABELS)
          .filter((stage) => ['parse', 'rule', 'result', 'writeback'].includes(stage))
          .map((stage) => BLOCKED_STAGE_LABELS[stage])
          .join(' / ')}
        {' —— '}拉取 / 详情 / 下载由工具 1–3 同步完成，**没有**可重跑的作业。
      </p>

      <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <label>
          任务编号
          <input
            type="number"
            min={1}
            value={taskId}
            onChange={(event) => setTaskId(event.target.value)}
          />
        </label>
        <label style={{ flex: '1 1 280px' }}>
          重试原因（进审计账，必填）
          <input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="为什么现在要重试"
            style={{ width: '100%' }}
          />
        </label>
        <button
          type="button"
          onClick={() => {
            if (taskIdNumber !== null) {
              retry.mutate({ taskId: taskIdNumber, reason })
            }
          }}
          disabled={taskIdNumber === null || !reasonFilled || retry.isPending}
          title={!reasonFilled ? '原因必填：它会进入审计账' : undefined}
        >
          {retry.isPending ? '重试中…' : '从检查点恢复'}
        </button>
      </div>

      {retry.data !== undefined && !retry.isPending && (
        <div
          role="status"
          data-testid="retry-outcome"
          style={{
            borderLeft: '4px solid var(--status-ok)',
            padding: 'var(--space-2)',
            marginTop: 'var(--space-2)',
            fontSize: 'var(--font-size-sm)',
          }}
        >
          <strong>{retrySummary(retry.data).headline}</strong>
          <ul style={{ margin: 'var(--space-1) 0 0', paddingLeft: '1.2em' }}>
            {retrySummary(retry.data).lines.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      )}

      {retry.isError && <RetryFailure error={asApiError(retry.error)} onRetry={() => retry.mutate({ taskId: Number.parseInt(taskId, 10), reason })} />}
    </div>
  )
}

function RetryFailure({
  error,
  onRetry,
}: {
  readonly error: ApiError
  readonly onRetry: () => void
}): JSX.Element {
  const description: FailureText = describeRetryFailure(error)
  return (
    <div
      role="alert"
      style={{
        borderLeft: '4px solid var(--status-danger)',
        padding: 'var(--space-2)',
        marginTop: 'var(--space-2)',
        fontSize: 'var(--font-size-sm)',
      }}
    >
      <strong>{description.summary}</strong>
      <p style={{ color: 'var(--text-2)' }}>{description.detail}</p>
      <p style={{ color: 'var(--text-2)' }}>{description.action}</p>
      {error.retryable && (
        <button type="button" onClick={onRetry}>
          再试一次
        </button>
      )}
    </div>
  )
}

// ============================================================
// 三个列表
// ============================================================

function JobList({
  state,
}: {
  readonly state: ReturnType<typeof useJobs>
}): JSX.Element {
  if (state.isPending) {
    return <p aria-busy="true" style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>正在加载作业…</p>
  }
  if (state.isError) {
    return <QueryFailure error={asApiError(state.error)} onRetry={() => void state.refetch()} />
  }
  if (state.data.items.length === 0) {
    return <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>当前筛选条件下没有作业。</p>
  }
  return (
    <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: 'var(--font-size-sm)' }}>
      {state.data.items.map((job) => (
        <li
          key={job.job_id}
          style={{ borderBottom: '1px solid var(--border)', padding: 'var(--space-1) 0' }}
        >
          #{job.job_id} · 任务 {job.task_id} · {jobSummary(job)}
        </li>
      ))}
    </ul>
  )
}

function LogList({
  state,
  hasTaskId,
}: {
  readonly state: ReturnType<typeof useLogs>
  readonly hasTaskId: boolean
}): JSX.Element {
  if (!hasTaskId) {
    return (
      <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
        输入任务编号以查看日志 —— 日志按任务归属，不提供全表浏览。
      </p>
    )
  }
  if (state.isPending) {
    return <p aria-busy="true" style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>正在加载日志…</p>
  }
  if (state.isError) {
    return <QueryFailure error={asApiError(state.error)} onRetry={() => void state.refetch()} />
  }
  if (state.data.items.length === 0) {
    return <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>没有匹配的日志。</p>
  }
  return (
    <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: 'var(--font-size-xs)' }}>
      {state.data.items.map((row) => (
        <li key={row.log_id} style={{ borderBottom: '1px solid var(--border)', padding: 'var(--space-1) 0' }}>
          <span style={{ color: 'var(--text-3)' }}>{formatLocalTime(row.created_at)}</span>
          {' '}
          <b>{row.log_level}</b> [{labelOf(LOG_TYPE_FALLBACK, row.log_type)}]
          {row.error_code !== null && <code> {row.error_code}</code>}
          <div style={{ whiteSpace: 'pre-wrap' }}>
            {/*
              ⚠️ 日志内容**只**出现在这里：不进 URL、不进 console、不做错误上报
              （材料 §Global Constraints）。它已由后端脱敏，但脱敏不是
              "可以到处复制"的授权。
            */}
            {row.log_content}
          </div>
          {row.correlation_id !== null && (
            <span style={{ color: 'var(--text-3)' }}>关联 {row.correlation_id}</span>
          )}
        </li>
      ))}
    </ul>
  )
}

/** 日志类型的兜底映射（后端类型是开放集合，未登记的原样显示）。 */
const LOG_TYPE_FALLBACK: Readonly<Record<string, string>> = {
  system: '系统',
  pull: '拉取',
  parse: '解析',
  rule: '规则',
}

function AuditList({
  state,
}: {
  readonly state: ReturnType<typeof useAudit>
}): JSX.Element {
  if (state.isPending) {
    return <p aria-busy="true" style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>正在加载审计…</p>
  }
  if (state.isError) {
    return <QueryFailure error={asApiError(state.error)} onRetry={() => void state.refetch()} />
  }
  if (state.data.items.length === 0) {
    return (
      <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
        没有匹配的审计事件。系统级事件（规则变更）默认**不显示** —— 需要时勾选「包含系统级事件」。
      </p>
    )
  }
  return (
    <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: 'var(--font-size-xs)' }}>
      {state.data.items.map((row) => (
        <li key={row.event_id} style={{ borderBottom: '1px solid var(--border)', padding: 'var(--space-1) 0' }}>
          <b>{row.action}</b>
          {' · '}
          {row.actor_name ?? row.actor_id ?? '（系统）'}
          {row.task_id !== null && ` · 任务 ${row.task_id}`}
          {row.target_type !== null && ` · ${row.target_type} #${row.target_id ?? '—'}`}
          {' · '}
          {formatLocalTime(row.created_at)}
        </li>
      ))}
    </ul>
  )
}

function QueryFailure({
  error,
  onRetry,
}: {
  readonly error: ApiError
  readonly onRetry: () => void
}): JSX.Element {
  const description = describeApiError(error)
  return (
    <div role="alert" style={{ fontSize: 'var(--font-size-sm)' }}>
      {description.summary}——{description.action}
      {error.retryable && (
        <button type="button" onClick={onRetry} style={{ marginLeft: 'var(--space-2)' }}>
          重新加载
        </button>
      )}
    </div>
  )
}
