import { useState } from 'react'

import { asApiError } from '../../api/client'
import type { ApiError } from '../../api/client'
import type { ReviewResultRow } from '../../api/contracts'
import { formatLocalTime } from '../../domain/time'
import { describeConfirmFailure } from './failures'
import { useConfirmResult } from './queries'
import { confirmationExplanation, shortDigest } from './resultState'

/**
 * 人工确认（M8 Task 7，设计 §4.5）。
 *
 * ## 状态由后端判定，这里只说"为什么"
 *
 * `confirmation_valid` 由后端算（已确认 + 摘要相符 + 仍是当前版本）。
 * 前端唯一多做的一件事是**解释**它为什么不是有效 —— 而"该不该禁用按钮"
 * 是解释性的（点了也不会改变什么），不是授权：
 *
 * | 解释 | 确认按钮 | 为什么 |
 * | --- | --- | --- |
 * | 有效 | 不可点 | 已确认，重复确认是幂等的，没有意义 |
 * | 正文已变更 | 可点 | 需要针对新正文重新确认 |
 * | 已被接替 | 不可点 | 确认这一版不会让它变成当前版本（要确认先去当前版本） |
 * | 尚未确认 | 可点 | 正常路径 |
 *
 * 手工改 DOM 把按钮点亮也绕不过后端 —— 后端对每条写路由独立校验。
 */
export function ConfirmationPanel({
  result,
  taskId,
  canConfirm,
}: {
  readonly result: ReviewResultRow
  readonly taskId: number
  /**
   * 权限（`result:confirm`）：`true` / `false` / `null`（身份未就绪）。
   * ⚠️ 只用于说明；"还不知道"不等于"没有权限"。
   */
  readonly canConfirm: boolean | null
}): JSX.Element {
  const explanation = confirmationExplanation(result)
  const confirm = useConfirmResult()
  const [confirmedNotice, setConfirmedNotice] = useState(false)

  const run = (): void => {
    confirm.mutate(
      { resultId: result.result_id, taskId },
      { onSuccess: () => setConfirmedNotice(true) },
    )
  }

  return (
    <section aria-label="人工确认" style={{ marginBottom: 'var(--space-4)' }}>
      <h2 style={{ fontSize: 'var(--font-size-sm)' }}>人工确认</h2>

      <p data-testid="confirmation-state" style={{ margin: 'var(--space-1) 0' }}>
        <strong>{explanation.label}</strong>
        <span style={{ color: 'var(--text-2)' }}> —— {explanation.detail}</span>
      </p>

      <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--text-2)' }}>
        {result.manual_confirmed ? (
          <>
            确认人 {result.confirmed_by ?? '—'} ·{' '}
            {formatLocalTime(result.confirmed_at)} · 确认时正文摘要{' '}
            <code>{shortDigest(result.confirmed_digest)}</code>
            {result.confirmed_digest !== null &&
              result.confirmed_digest !== result.content_digest && (
                <>（与当前正文摘要 <code>{shortDigest(result.content_digest)}</code> <b>不同</b>）</>
              )}
          </>
        ) : (
          <>还没有确认记录。</>
        )}
      </div>

      {canConfirm === false && (
        <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
          你没有确认权限（`result:confirm`）。这不是安全边界 —— 后端独立校验。
        </p>
      )}

      {canConfirm === null && (
        <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
          正在获取身份，暂时不能确认。
        </p>
      )}

      <div style={{ marginTop: 'var(--space-2)' }}>
        <button
          type="button"
          onClick={run}
          disabled={canConfirm !== true || !explanation.worthConfirming || confirm.isPending}
          title={
            explanation.worthConfirming ? undefined : explanation.detail
          }
        >
          {confirm.isPending ? '确认中…' : '确认这一版正文'}
        </button>
      </div>

      {confirmedNotice && (
        <p role="status" style={{ color: 'var(--status-ok)', fontSize: 'var(--font-size-sm)' }}>
          已确认。确认绑定的是<strong>当前这一版正文</strong>的摘要；之后若正文再变，这次确认会自动失效。
        </p>
      )}

      {confirm.isError && <ConfirmFailure error={asApiError(confirm.error)} onRetry={run} />}
    </section>
  )
}

function ConfirmFailure({
  error,
  onRetry,
}: {
  readonly error: ApiError
  readonly onRetry: () => void
}): JSX.Element {
  const description = describeConfirmFailure(error)
  return (
    <div
      role="alert"
      style={{
        borderLeft: '4px solid var(--status-danger)',
        padding: 'var(--space-2)',
        marginTop: 'var(--space-2)',
      }}
    >
      <strong>{description.summary}</strong>
      <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
        {description.detail}——{description.action}
      </p>
      {error.retryable && (
        <button type="button" onClick={onRetry}>
          再试一次
        </button>
      )}
    </div>
  )
}
