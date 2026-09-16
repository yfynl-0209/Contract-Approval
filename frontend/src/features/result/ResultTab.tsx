import { useState } from 'react'

import { asApiError, describeApiError } from '../../api/client'
import { useAuth } from '../../app/authContext'
import { useTaskId } from '../../app/useTaskId'
import { formatLocalTime } from '../../domain/time'
import { REVIEW_STATUS_LABELS, riskLevelLabel } from '../../domain/labels'
import { useTaskDetail } from '../detail/queries'
import { CommentEditor } from './CommentEditor'
import { ConfirmationPanel } from './ConfirmationPanel'
import { WritebackTimeline } from './WritebackTimeline'
import { pickCurrentResult, shortDigest } from './resultState'
import { useTaskResults, useWriteback } from './queries'

/**
 * 模块 5：结果处理（M8 Task 7，设计 §4.5）。
 *
 * 目标：**确认后回写**，且明确知道"我确认的是哪一版正文"。
 *
 * ## 风险等级与完整性**全部来自后端**
 *
 * 页面上的"总风险：高"与"完整性：需人工判断"直接取结果行里的
 * `overall_risk_level` / `review_status`（后端在保存时按批次聚合算好的）。
 * 前端不按四态计数重算 —— 重算出来的值在数据稍有出入时会更"自洽"，
 * 而它与报告、与回写门禁的依据不是同一个东西。
 *
 * ## 版本切换
 *
 * `GET /api/results?task_id=` 一次给出全部历史版本（含 `is_current_version`
 * 与每行的 `confirmation_valid`）。切版本只是换显示哪一行 —— 因此
 * "我确认的是哪一版"在界面上永远与选中的那一行一致。
 */
export function ResultTab(): JSX.Element {
  const { taskId } = useTaskId()
  const { status: authStatus, can } = useAuth()
  const task = useTaskDetail(taskId)
  const results = useTaskResults(taskId)
  const [selectedResultId, setSelectedResultId] = useState<number | null>(null)

  // ⚠️ 回写尝试的 id 来自**任务详情的回写口径**（`latest_attempt_id`）——
  // 不从结果行推：一次回写尝试绑定的是"某个结果"，而"最近一次尝试"
  // 可能对应更早的版本（改版之后回写的是新版本，历史版本没有尝试）
  const attemptId = task.data?.writeback.latest_attempt_id ?? null
  const attempt = useWriteback(attemptId)

  /*
   * 权限三态：`true` / `false` / `null`（身份未就绪）。
   *
   * ⚠️ 未就绪时**不能**当成"没有权限"：那会让刚打开页面的一瞬显示
   * "你没有修改正文的权限" —— 一句假话，而且方向最危险
   * （用户会以为自己权限不够，而不是"再等一下"）。
   */
  const canSave: boolean | null =
    authStatus === 'ready' ? can('result:save') : null
  const canConfirm: boolean | null =
    authStatus === 'ready' ? can('result:confirm') : null

  if (task.isPending || results.isPending) {
    return <p aria-busy="true">正在加载审查结果…</p>
  }
  if (task.isError || results.isError) {
    const error = asApiError(task.isError ? task.error : results.error)
    const description = describeApiError(error)
    return (
      <div className="notice notice-danger" role="alert">
        <div>
          <strong>{description.summary}</strong>
          <p>
            {description.detail}——{description.action}
          </p>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => {
              void task.refetch()
              void results.refetch()
            }}
          >
            重新加载
          </button>
        </div>
      </div>
    )
  }

  const rows = results.data.items
  const current = pickCurrentResult(rows)
  const row = rows.find((item) => item.result_id === selectedResultId) ?? current

  if (row === null) {
    return (
      <div className="empty">
        <p className="big">这份合同还没有审查结果</p>
        <p>
          结果由工具 6（或后台作业）在批次完成后保存 —— 控制台不发起它。
          批次见「规则命中」页。
        </p>
      </div>
    )
  }

  const flaggedCurrent = rows.some((item) => item.is_current_version)
  const heroRisk =
    row.overall_risk_level === 'high'
      ? 'high'
      : row.overall_risk_level === 'medium'
        ? 'mid'
        : 'low'

  return (
    <div>
      <div className="result-hero">
        <div className="hero-box">
          <div className="lab">总风险</div>
          <div className={`hero-risk ${heroRisk}`} data-testid="overall-risk">
            {riskLevelLabel(row.overall_risk_level)}
          </div>
        </div>
        <div className="hero-box">
          <div className="lab">结论完整性</div>
          <div data-testid="review-status" style={{ fontWeight: 650 }}>
            {REVIEW_STATUS_LABELS[row.review_status]}
            {row.needs_review_count > 0 && (
              <span style={{ color: 'var(--text-3)', fontWeight: 400 }}>
                {' '}
                （{row.needs_review_count} 条待判断）
              </span>
            )}
          </div>
        </div>
        <div className="hero-box">
          <div className="lab">批次统计</div>
          <div style={{ fontSize: 'var(--font-size-sm)', color: 'var(--text-2)' }}>
            命中 {row.hit_count} · 不适用 {row.not_applicable_count} · 批次 #
            {row.run_id}
          </div>
          <div style={{ marginTop: 'var(--space-2)' }}>
            <select
              aria-label="结果版本"
              value={row.result_id}
              onChange={(event) => setSelectedResultId(Number.parseInt(event.target.value, 10))}
            >
              {rows.map((item) => (
                <option key={item.result_id} value={item.result_id}>
                  v{item.version_no}
                  {item.is_current_version ? '（当前）' : ''}
                  {item.confirmation_valid ? ' · 已确认' : ''}
                  {item.created_at === null ? '' : ` · ${formatLocalTime(item.created_at)}`}
                </option>
              ))}
            </select>
            {!row.is_current_version && (
              <span style={{ color: 'var(--status-warn)', fontSize: 'var(--font-size-xs)', marginLeft: 'var(--space-2)' }}>
                正在看历史版本
              </span>
            )}
          </div>
        </div>
      </div>

      {!flaggedCurrent && (
        // 一个都没有被标为"当前版本"是异常状态：列表可能不完整。
        // 静默按版本号回落后再显示，会让用户以为自己在看当前版本
        <p className="notice notice-warn" role="status" data-testid="no-current-version">
          没有任何版本被标为「当前版本」—— 上面的内容按版本号最大的那一版显示，
          它**不一定**是服务端认定的当前版本。请刷新或联系管理员核对。
        </p>
      )}

      <section aria-label="摘要" className="card card-pad" style={{ marginBottom: 'var(--space-4)' }}>
        <h2 className="card-title">摘要</h2>
        {row.summary_text === null || row.summary_text === '' ? (
          <p style={{ color: 'var(--text-3)', fontSize: 'var(--font-size-sm)' }}>
            这一版没有摘要文本 —— 这不等于「没有风险」，请以规则命中页的结论为准。
          </p>
        ) : (
          <p style={{ lineHeight: 1.6, margin: 0 }}>{row.summary_text}</p>
        )}
      </section>

      <section aria-label="关注点" className="card card-pad" style={{ marginBottom: 'var(--space-4)' }}>
        <h2 className="card-title">关注点</h2>
        {row.focus_points.length === 0 ? (
          <p style={{ color: 'var(--text-3)', fontSize: 'var(--font-size-sm)' }}>
            这一版没有关注点（聚合没有产出需要人看的项）。
          </p>
        ) : (
          <ol className="focus-list">
            {row.focus_points.map((point, index) => (
              <li key={`${index}:${point}`}>{point}</li>
            ))}
          </ol>
        )}
      </section>

      {/*
        ⚠️ `key={row.result_id}` 不是可选项。编辑器的草稿存在 `useState` 的**初值**里，
        而切版本只换 `result` 这个 prop —— React 不会重置 state。
        没有 key 时：切换版本后编辑器里仍是**上一版的正文**，
        而"保存"用的是新版本的 result_id → 把 v2 的正文写进 v1 的新版本。
        这类缺陷在界面上完全看不出来（框里有字、保存成功、版本号也涨了）。
      */}
      <CommentEditor key={row.result_id} result={row} taskId={taskId} canSave={canSave} />

      <ConfirmationPanel result={row} taskId={taskId} canConfirm={canConfirm} />

      <WritebackTimeline
        task={task.data}
        attempt={attempt.data ?? null}
        /*
         * ⚠️ `attemptId === null` 时**不能说"正在加载"**：
         * 被 `enabled: false` 关掉的 `useQuery` 在 v5 里**永远是 `isPending`** ——
         * 照搬 `attempt.isPending` 会让"还没有回写尝试"永久显示成加载中。
         * 这个缺陷在浏览器里表现为一个转不完的圈，而测试里是一条找不到的文案。
         */
        attemptLoading={attemptId !== null && attempt.isPending}
        attemptError={attempt.isError ? asApiError(attempt.error) : null}
      />

      <footer className="digest-line">
        <span>
          结果 #{row.result_id} · 保存人 {row.created_by ?? '—'} ·{' '}
          {formatLocalTime(row.created_at)}
        </span>
        <span className="mono" title="正文摘要（确认与回写依据的就是这一版）">
          {shortDigest(row.content_digest)}
        </span>
      </footer>
    </div>
  )
}
