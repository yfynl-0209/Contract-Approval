/**
 * 运行管理的判断与文案（纯函数，M8 Task 8）。
 *
 * ## 这一页的三条纪律
 *
 * 1. **重试的入口按"失败检查点"解释，而不是按按钮**：`pull` / `detail` / `download`
 *    三种失败位置**没有**可重跑的作业（它们由工具 1–3 在同步路径上完成）——
 *    把它们硬映射成一个 Worker 永远不会领取的作业，任务会回到 `parsing`
 *    然后**永远停住**。后端对此返回 409 `RETRY_NOT_SUPPORTED`，
 *    界面要把它翻译成"去重跑哪个工具"。
 * 2. **"原因必填"是业务规则**：它要进审计账。前端禁用按钮只是让"没填就点"
 *    变得不可能，真正的判据在服务层（空白串也 400）。
 * 3. **没有"任意编辑任务状态"的入口**：任务状态只能由流程（作业成功/失败）
 *    与检查点重试改变 —— 给一个下拉框改状态，等于把状态机从系统手里拿走。
 */

import type { ApiError } from '../../api/client'
import type { JobRecord, RetryOutcome, TaskStatus } from '../../api/contracts'
import { BLOCKED_STAGE_LABELS, JOB_STATUS_LABELS, JOB_TYPE_LABELS, labelOf } from '../../domain/labels'
import { formatLocalTime } from '../../domain/time'

export interface FailureText {
  readonly summary: string
  readonly detail: string
  readonly action: string
}

/** `POST /api/tasks/{id}/retry` 失败的分支（按 `error_code`）。 */
export function describeRetryFailure(error: ApiError): FailureText {
  switch (error.errorCode) {
    case 'RETRY_NOT_SUPPORTED':
      return {
        summary: '这个失败位置不能在服务端重跑',
        detail:
          '拉取 / 详情 / 下载由工具 1–3 在同步路径上完成 —— 它们没有可重跑的作业。' +
          '硬造一个作业会让任务回到运行中然后永远停住。',
        action: '重跑对应的工具（例如附件丢了就重跑工具 3），成功后任务会自动从检查点恢复。',
      }
    case 'TASK_NOT_BLOCKED':
      return {
        summary: '这条任务不在阻塞状态',
        detail: '只有 `blocked` 的任务有"从哪失败从哪恢复"的检查点。',
        action: '刷新任务状态；如果它确实卡住了，先看下面的运行日志再决定。',
      }
    case 'RESOURCE_NOT_FOUND':
      return {
        summary: '任务不存在或无权访问',
        detail: '任务可能已被删除，或它属于别的租户。',
        action: '核对任务编号。重试不会成功。',
      }
    case 'INVALID_ARGUMENT':
      return {
        summary: '重试原因不能为空',
        detail: '原因会进入审计账 —— 它回答"当时为什么要重试"。',
        action: '填写原因后再重试。',
      }
    default:
      return {
        summary: '重试失败',
        detail: error.message,
        action: error.retryable ? '稍后重试。' : '请联系管理员。',
      }
  }
}

export interface RetrySummary {
  readonly headline: string
  readonly lines: readonly string[]
}

/**
 * 重试结果的人读摘要。
 *
 * ⚠️ `action` 是后端给的"这次到底做了什么"（排了一个作业 / 重新武装了一次投递），
 * **不要**从 `blocked_stage` 自己推 —— 推一遍就是第二份会漂移的判据。
 */
export function retrySummary(outcome: RetryOutcome): RetrySummary {
  const lines = [
    `失败位置：${labelOf(BLOCKED_STAGE_LABELS, outcome.blocked_stage)}`,
    `任务回到：${statusLabel(outcome.resumed_status)}`,
    outcome.job_id !== null
      ? `已排作业 #${outcome.job_id}（${labelOf(JOB_TYPE_LABELS, outcome.job_type)}）`
      : null,
    outcome.reason !== null && outcome.reason !== '' ? `原因：${outcome.reason}` : null,
  ].filter((line): line is string => line !== null)

  return { headline: outcome.action, lines }
}

function statusLabel(status: TaskStatus): string {
  return labelOf(TASK_STATUS_TEXT, status)
}

const TASK_STATUS_TEXT: Readonly<Record<string, string>> = {
  pending: '待处理',
  parsing: '解析中',
  reviewing: '审查中',
  blocked: '阻塞',
  done: '已完成',
}

/** 作业行的一句话摘要（时间 + 尝试次数 + 最近错误）。 */
export function jobSummary(job: JobRecord): string {
  const parts = [
    labelOf(JOB_TYPE_LABELS, job.job_type),
    labelOf(JOB_STATUS_LABELS, job.job_status),
    `第 ${job.attempt_no}/${job.max_attempts} 次`,
  ]
  if (job.next_retry_at !== null) {
    parts.push(`下次重试 ${formatLocalTime(job.next_retry_at)}`)
  }
  if (job.last_error_code !== null) {
    parts.push(`上次错误 ${job.last_error_code}`)
  }
  return parts.join(' · ')
}

/** 作业是否还会自己动（决定要不要提示"等它跑"）。 */
export function isJobSettled(job: JobRecord): boolean {
  return job.job_status === 'succeeded' || job.job_status === 'failed'
}
