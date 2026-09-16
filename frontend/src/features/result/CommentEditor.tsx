import { useState } from 'react'

import { asApiError } from '../../api/client'
import type { ApiError } from '../../api/client'
import type { ReviewResultRow } from '../../api/contracts'
import { describeCommentSaveFailure } from './failures'
import { useEditComment } from './queries'
import { shortDigest } from './resultState'

/**
 * 回写正文的编辑与保存（M8 Task 7）。
 *
 * ## 保存会**生成新版本**，并让当前确认失效
 *
 * 这是设计的核心口径（§4.5）：确认绑定的是**具体那一版正文**。
 * 因此界面上编辑框一动就要说清后果 —— 而不是先让人改、再在保存后
 * 冒出一句"确认已失效"。
 *
 * ## 前端**不**自己判定确认失效
 *
 * 这里的 `dirty` 只用来提示"有未保存的修改"。
 * 保存成功后由 `useEditComment` 失效结果列表缓存、重新取回后端算出的
 * `confirmation_valid` —— 前端置一个本地标记时，一旦后端口径变化
 * （例如将来允许"确认后编辑仍有效"），界面会继续显示**它自己编的**结论。
 */
export function CommentEditor({
  result,
  taskId,
  canSave,
}: {
  readonly result: ReviewResultRow
  readonly taskId: number
  /**
   * 权限（`result:save`）：`true` / `false` / `null`（身份未就绪）。
   *
   * ⚠️ 三态而不是两态：把"还不知道"当成"没有权限"时，刚打开页面的一瞬
   * 会显示"你没有修改正文的权限" —— 一句假话，且方向最危险。
   * ⚠️ 它只用于**说明**，授权由后端判定。
   */
  readonly canSave: boolean | null
}): JSX.Element {
  const original = result.comment_text ?? ''
  const [draft, setDraft] = useState(original)
  const edit = useEditComment()

  const editable = canSave === true
  const dirty = draft !== original
  const trimmedEmpty = draft.trim() === ''

  const save = (): void => {
    edit.mutate({ resultId: result.result_id, commentText: draft, taskId })
  }

  return (
    // ⚠️ 区域标签与输入框标签**必须不同**：两者同名时
    // `getByLabelText('回写正文')` 会命中两个元素（区域 + 输入框），
    // 而这类"测试选择器撞车"最常见的处置是把断言改松 —— 那就白丢了精度
    <section aria-label="回写正文编辑区" className="card card-pad" style={{ marginBottom: 'var(--space-4)' }}>
      <h2 className="card-title">回写正文</h2>

      <textarea
        aria-label="回写正文"
        className="comment-editor"
        value={draft}
        rows={10}
        readOnly={!editable}
        onChange={(event) => setDraft(event.target.value)}
        style={{ background: editable ? 'var(--surface-0)' : 'var(--surface-2)' }}
      />

      <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--text-2)', marginTop: 'var(--space-1)' }}>
        正文摘要 <code>{shortDigest(result.content_digest)}</code>
        {' · '}
        确认绑定的是<b>这一版</b>正文，改动会生成新版本
        {result.supersedes_result_id !== null && `（本版接替 #${result.supersedes_result_id}）`}
      </div>

      {canSave === false && (
        <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
          你没有修改正文的权限（`result:save`），因此这里只读。
          这不是安全边界 —— 后端对写操作独立校验，界面只是不给你一个点了会失败的动作。
        </p>
      )}

      {canSave === null && (
        <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
          正在获取身份，暂时只读 —— 「还不知道权限」与「没有权限」是两件事。
        </p>
      )}

      {dirty && editable && (
        <p role="status" style={{ color: 'var(--status-warn)', fontSize: 'var(--font-size-sm)' }}>
          有未保存的修改：保存会<strong>生成新版本</strong>，当前版本的确认随之失效，需要重新确认。
        </p>
      )}

      <div className="result-actions" style={{ marginTop: 'var(--space-3)', paddingTop: 'var(--space-3)' }}>
        <button
          type="button"
          className="btn btn-primary"
          onClick={save}
          disabled={!editable || !dirty || trimmedEmpty || edit.isPending}
          title={
            trimmedEmpty && dirty ? '正文不能为空' : dirty ? undefined : '没有修改'
          }
        >
          {edit.isPending ? '保存中…' : '保存为新版本'}
        </button>
        {dirty && (
          <button type="button" className="btn" onClick={() => setDraft(original)} disabled={edit.isPending}>
            放弃修改
          </button>
        )}
      </div>

      {edit.data !== undefined && !edit.isPending && (
        <p role="status" style={{ color: 'var(--status-ok)', fontSize: 'var(--font-size-sm)' }}>
          {edit.data.outcome === 'reused'
            ? `正文与既有版本完全相同，已复用 v${edit.data.version_no}（没有多出版本）。`
            : `已保存为 v${edit.data.version_no}。该版本尚未确认 —— 请重新确认后再回写。`}
        </p>
      )}

      {edit.isError && <EditFailure error={asApiError(edit.error)} onRetry={save} />}
    </section>
  )
}

/** 保存失败：三段式（是什么 / 为什么 / 我现在能做什么）。 */
function EditFailure({
  error,
  onRetry,
}: {
  readonly error: ApiError
  readonly onRetry: () => void
}): JSX.Element {
  const description = describeCommentSaveFailure(error)
  return (
    <div className="notice notice-danger" role="alert" style={{ marginTop: 'var(--space-2)' }}>
      <div>
        <strong>{description.summary}</strong>
        <p>
          {description.detail}——{description.action}
        </p>
        {error.retryable && (
          <button type="button" className="btn btn-sm" onClick={onRetry}>
            再存一次
          </button>
        )}
      </div>
    </div>
  )
}
