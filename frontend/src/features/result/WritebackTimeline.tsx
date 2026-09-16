import type { ApiError } from '../../api/client'
import type { TaskDetail, WritebackAttempt } from '../../api/contracts'
import { formatLocalTime } from '../../domain/time'
import {
  isWritebackInFlight,
  taskStatusHint,
  taskWritebackLabel,
  writebackView,
} from './resultState'

/**
 * 回写状态与投递进度（M8 Task 7，设计 §5.1、验收 10）。
 *
 * ## 三个层级必须分开说
 *
 * | 层级 | 字段 | 回答 |
 * | --- | --- | --- |
 * | **任务** | `task.writeback.task_write_status` | 这份合同最后写成了没有 |
 * | **尝试** | `attempt.write_status` | 这次回写走到哪一步 |
 * | **投递** | `attempt.delivery` | 派发器试了几次、下次什么时候、上次为什么失败 |
 *
 * 只说"任务：写失败"会让人直接去重试，而**门禁拒绝**的重试永远是白试
 * （它等的那个确认不会因为重试而出现）。因此拒绝用**中性提示**呈现，
 * 并且原因码（稳定机器码）与人读文本都给。
 *
 * ## 回写不是控制台发起的
 *
 * 控制台只调 `/api/*`，而"登记回写意图"是**工具 7**（`/tools/*`）。
 * 这里因此只呈现状态，并明确写出"谁在推进它" —— 否则用户会
 * 在这页上找那个并不存在的"回写"按钮。
 */
export function WritebackTimeline({
  task,
  attempt,
  attemptLoading,
  attemptError,
}: {
  readonly task: TaskDetail
  readonly attempt: WritebackAttempt | null
  readonly attemptLoading: boolean
  readonly attemptError: ApiError | null
}): JSX.Element {
  const summary = taskWritebackLabel(task.writeback)
  const view = writebackView(attempt)
  const statusHint = taskStatusHint(task.task_status)

  return (
    <section aria-label="回写状态" style={{ marginBottom: 'var(--space-4)' }}>
      <h2 style={{ fontSize: 'var(--font-size-sm)' }}>回写状态</h2>

      <p data-testid="writeback-task-level" style={{ margin: 'var(--space-1) 0' }}>
        任务级：<strong>{summary.label}</strong>
        {summary.hint !== '' && (
          <span style={{ color: 'var(--text-2)' }}> —— {summary.hint}</span>
        )}
      </p>

      {statusHint !== '' && (
        <p style={{ color: 'var(--status-warn)', fontSize: 'var(--font-size-sm)' }}>{statusHint}</p>
      )}

      {attemptError !== null ? (
        <p role="alert" style={{ fontSize: 'var(--font-size-sm)' }}>
          回写尝试加载失败：{attemptError.message}
          {attemptError.retryable ? '（稍后会自动重试）' : '——请联系管理员。'}
        </p>
      ) : attemptLoading ? (
        <p aria-busy="true" style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
          正在加载回写尝试…
        </p>
      ) : attempt === null ? (
        <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
          还没有回写尝试。回写由外部调用方（工具 7）或后台作业登记，
          <b>控制台不发起回写</b> —— 这里只呈现状态与投递进度。
        </p>
      ) : (
        <div data-testid="writeback-attempt-level" style={{ fontSize: 'var(--font-size-sm)' }}>
          <p style={{ margin: 'var(--space-1) 0' }}>
            尝试 #{attempt.attempt_id}（第 {attempt.attempt_no} 次）：
            <strong>{view.attemptLabel}</strong>
            {attempt.operator_name !== null && ` · 操作人 ${attempt.operator_name}`}
            {attempt.created_at !== null && ` · ${formatLocalTime(attempt.created_at)}`}
          </p>

          {view.reasonCode !== null && (
            <p
              style={{
                margin: 'var(--space-1) 0',
                // ⚠️ 拒绝**不是错误**：用中性提示，避免"红色 = 出故障"的误读
                borderLeft: `3px solid ${view.rejected ? 'var(--status-warn)' : 'var(--status-danger)'}`,
                paddingLeft: 'var(--space-2)',
              }}
            >
              {view.rejected ? '门禁拒绝（未发起回写）' : '失败原因'}：
              <code>{view.reasonCode}</code>
              {view.reasonText !== null && <> · {view.reasonText}</>}
              {view.rejected && (
                <span style={{ color: 'var(--text-2)' }}>
                  {' '}
                  —— 重试不会成功，先按上面的原因处理（例如去确认结果）。
                </span>
              )}
            </p>
          )}

          {view.deliveryLabel !== null && (
            <p style={{ margin: 'var(--space-1) 0', color: 'var(--text-2)' }}>
              投递：{view.deliveryLabel}
              {view.nextRetryAtText !== null && ` · 下次重试 ${view.nextRetryAtText}`}
              {attempt.delivery?.last_error_code !== null &&
                attempt.delivery?.last_error_code !== undefined &&
                ` · 上次错误 ${attempt.delivery.last_error_code}`}
            </p>
          )}

          {view.exhausted && (
            <p role="status" style={{ color: 'var(--status-warn)', fontSize: 'var(--font-size-sm)' }}>
              重试预算已耗尽 —— 任务会停在 <code>blocked</code>（回写），
              恢复点是回写本身（不会重跑解析与规则）。
            </p>
          )}

          {/*
            ⚠️ 判据与查询里的 `refetchInterval` **共用同一个函数**：
            各写一份时，"界面说在等"与"实际还在轮询"会分叉 ——
            表现为进度提示一直亮着而请求早已停止（或反过来）。
          */}
          {isWritebackInFlight(attempt) && (
            <p role="status" style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-xs)' }}>
              正在等待派发（每 2 秒刷新一次；到达终态后自动停止）。
            </p>
          )}
        </div>
      )}
    </section>
  )
}
