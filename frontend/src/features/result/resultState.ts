/**
 * 模块 5 的判断与文案（纯函数，M8 Task 7）。
 *
 * ## 这个文件里最重要的一条纪律：**解释，不判定**
 *
 * `confirmation_valid`、风险等级、完整性、回写状态**全部由后端给出**，
 * 界面只负责解释"为什么"。因此这里的每个函数都只读后端字段、不做业务推断：
 *
 * | 反面例子（都不要写） | 为什么 |
 * | --- | --- |
 * | 前端比 `content_digest == confirmed_digest` 来判"确认是否有效" | 漏掉"仍是当前版本"这一条，错的方向恰恰是把**失效的确认显示成有效** |
 * | 前端按四态计数重算风险等级 | 与后端口径迟早分叉，而分叉时界面看起来"更合理" |
 * | 前端把 `not_written` + `reason_code` 读成"失败" | 那是**门禁拒绝**：重试永远不会成功，动作是去确认 |
 *
 * 唯一"多出来"的东西是 `worthConfirming`：它是**解释性**的按钮禁用
 * （"这一版已被接替，确认没有意义"），不是授权判断。后端仍然独立判定，
 * 因此手工构造请求也绕不过去。
 */

import type {
  ReviewResultRow,
  TaskStatus,
  WriteStatus,
  WritebackAttempt,
  WritebackSummary,
} from '../../api/contracts'
import { formatLocalTime } from '../../domain/time'
import { WRITEBACK_REASON_LABELS, WRITE_STATUS_LABELS, labelOf } from '../../domain/labels'

/**
 * 从结果列表里挑出**当前版本**。
 *
 * 判据是后端的 `is_current_version`（服务端算的），不是"version_no 最大的那个"：
 * 版本号最大与"是当前版本"在正常情况下一致，但一旦不一致（数据修复、
 * 并发保存），按版本号挑会挑出一个**后端认为已被接替**的版本，
 * 而界面会把它当作当前版本渲染。
 *
 * 一个都没有（`is_current_version` 全为 false）时回落到版本号最大者：
 * 那时列表为空场景更常见，而回落至少让用户看到内容 —— 但这种情况
 * 本身值得怀疑，界面另有一处提示（见 `ResultTab` 的 `noCurrentVersion`）。
 */
export function pickCurrentResult(
  rows: readonly ReviewResultRow[],
): ReviewResultRow | null {
  const flagged = rows.find((row) => row.is_current_version)
  if (flagged !== undefined) {
    return flagged
  }
  return rows.reduce<ReviewResultRow | null>(
    (best, row) => (best === null || row.version_no > best.version_no ? row : best),
    null,
  )
}

export interface ConfirmationExplanation {
  readonly kind: 'valid' | 'stale' | 'superseded' | 'unconfirmed'
  readonly label: string
  readonly detail: string
  /**
   * 界面上"确认"按钮**是否有意义**。
   *
   * ⚠️ 这是解释，不是授权：前端禁用它只说明"点了也不会改变什么"，
   * 后端仍然独立校验。手工发请求能绕到端点，但绕不过后端。
   */
  readonly worthConfirming: boolean
}

/**
 * 确认状态的四态解释（设计 §4.5 第 1 条）。
 *
 * ⚠️ 顺序不可换：**先看后端的 `confirmation_valid`**，再解释"为什么不是有效"。
 * 反过来（先比摘要）时，一份"摘要相同但已被新版本接替"的结果会被讲成"有效"。
 */
export function confirmationExplanation(
  row: ReviewResultRow,
): ConfirmationExplanation {
  if (row.confirmation_valid) {
    return {
      kind: 'valid',
      label: '已确认（有效）',
      detail: '人工确认的正是当前这一版正文，且它仍是本任务的当前版本。',
      worthConfirming: false,
    }
  }

  if (!row.is_current_version) {
    return {
      kind: 'superseded',
      label: '这一版已被接替',
      detail:
        '确认绑定的是当时那份正文，而当前版本已经不是它 —— 因此这一版的确认不再有效。' +
        '要确认请切到当前版本。',
      worthConfirming: false,
    }
  }

  if (row.manual_confirmed && row.confirmed_digest !== row.content_digest) {
    return {
      kind: 'stale',
      label: '正文已变更，确认已失效',
      detail:
        '有人改过正文（摘要与确认时绑定的不一致），因此需要针对**新的正文**重新确认。',
      worthConfirming: true,
    }
  }

  return {
    kind: 'unconfirmed',
    label: '尚未人工确认',
    detail:
      row.review_status === 'needs_review'
        ? '结论完整性为「需人工判断」：确认后才能回写。'
        : '确认后即可回写；确认会记录确认人与时间，并写入不可变审计。',
    worthConfirming: true,
  }
}

/**
 * 回写是否**还在动**（决定要不要轮询）。
 *
 * `not_written` + `reason_code`（门禁拒绝）**不在飞行中**：它没有 Outbox 事件，
 * 轮询永远不会得到新结果 —— 而一直转的进度条会让人以为它还在推进。
 */
export function isWritebackInFlight(attempt: WritebackAttempt | null): boolean {
  if (attempt === null) {
    return false
  }
  if (attempt.write_status === 'writing') {
    return true
  }
  return attempt.delivery?.event_status === 'pending'
}

export interface WritebackView {
  /** 尝试层：这次回写走到哪一步 */
  readonly attemptLabel: string | null
  /** 投递层：派发器试了几次、下次什么时候 */
  readonly deliveryLabel: string | null
  /** 原因（稳定码 + 人读文本） */
  readonly reasonCode: string | null
  readonly reasonText: string | null
  /** 门禁拒绝（**不是失败**）：没有发起过回写 */
  readonly rejected: boolean
  /** 重试预算已耗尽（投递层） */
  readonly exhausted: boolean
  readonly nextRetryAtText: string | null
}

/**
 * 回写状态的两级视图（设计 §5.1、验收 10）。
 *
 * ⚠️ "状态"与"原因"**分两处**呈现：只显示"回写失败"会让人去重试，
 * 而"被门禁拒绝"的重试**永远是白试**（它等的那个确认不会因为重试而出现）。
 */
export function writebackView(attempt: WritebackAttempt | null): WritebackView {
  if (attempt === null) {
    return {
      attemptLabel: null,
      deliveryLabel: null,
      reasonCode: null,
      reasonText: null,
      rejected: false,
      exhausted: false,
      nextRetryAtText: null,
    }
  }

  const delivery = attempt.delivery
  const rejected = attempt.write_status === 'not_written' && attempt.reason_code !== null

  return {
    attemptLabel: labelOf(WRITE_STATUS_LABELS, attempt.write_status),
    deliveryLabel:
      delivery === null
        ? rejected
          ? '没有投递记录 —— 门禁拒绝不会产生投递事件'
          : null
        : `第 ${delivery.attempt_no} / ${delivery.max_attempts} 次投递 · ${labelOf(
            DELIVERY_LABELS,
            delivery.event_status,
          )}`,
    reasonCode: attempt.reason_code,
    reasonText: attempt.reason_text,
    rejected,
    exhausted:
      delivery !== null &&
      delivery.event_status === 'failed' &&
      delivery.attempt_no >= delivery.max_attempts,
    nextRetryAtText:
      delivery?.next_retry_at == null ? null : formatLocalTime(delivery.next_retry_at),
  }
}

/** 投递事件状态的中文（`OutboxStatus`）。 */
const DELIVERY_LABELS: Readonly<Record<string, string>> = {
  pending: '等待派发',
  delivered: '已送达',
  failed: '投递失败',
}

/**
 * 任务级回写口径（`task.writeback`）—— 与尝试级分开的**入口提示**。
 *
 * 列表页第一眼看到的是这一层，因此它必须能回答"我该不该点重试"。
 */
export function taskWritebackLabel(
  summary: WritebackSummary,
): { readonly label: string; readonly hint: string } {
  const status = labelOf(WRITE_STATUS_LABELS, summary.task_write_status)
  if (summary.latest_attempt_rejected) {
    return {
      label: status,
      hint: `门禁拒绝（${labelOf(WRITEBACK_REASON_LABELS, summary.latest_reason_code)}）—— 重试不会成功，先处理拒绝原因`,
    }
  }
  if (summary.task_write_status === 'failed') {
    return { label: status, hint: '外部调用失败 —— 可从「回写」检查点重试' }
  }
  return { label: status, hint: '' }
}

/** 任务状态提示（`blocked` 是**业务结论**，不是系统故障）。 */
export function taskStatusHint(status: TaskStatus): string {
  return status === 'blocked'
    ? '任务被阻塞：这是业务结论（等人处理），不是系统故障'
    : ''
}

/** 摘要显示成短码（长度够区分即可，不显示全串）。 */
export function shortDigest(digest: string | null): string {
  if (digest === null || digest === '') {
    return '—'
  }
  return digest.slice(0, 12)
}

/** 中文的写状态标签（供别处复用，避免各处自己 `labelOf`）。 */
export function writeStatusLabel(status: WriteStatus | null): string {
  return labelOf(WRITE_STATUS_LABELS, status)
}
