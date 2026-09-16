/**
 * 模块 5 的失败处置（M8 Task 7）—— **按 `error_code` 分支**。
 *
 * 三个接口的失败在界面上长得很像（都是"保存没成功"），而处置相反：
 *
 * | 接口 | `error_code` | 事实 | 该做什么 |
 * | --- | --- | --- | --- |
 * | 修改正文 | `RESULT_NOT_FOUND` | 结果不存在 / 不属于本租户 | 核对任务（重试不会成功） |
 * | 修改正文 | `RESULT_INPUT_MISMATCH` | 与批次聚合口径不一致 | 联系管理员（**数据问题**，不是网络问题） |
 * | 修改正文 | `RESULT_RUN_NOT_COMPLETED` | 批次还没跑完 | 等批次完成再来 |
 * | 人工确认 | `RESULT_NOT_FOUND` | 同上 | 同上 |
 * | 回写查询 | `RESOURCE_NOT_FOUND` | 尝试不存在 | 核对（回写记录可能已被清理） |
 *
 * 与模块 3 的 `failures.ts` 同理：分支抽成纯函数**可以直接单测** ——
 * 渲染只能验证"渲染对了"，而写错分支时渲染出来的仍然是一个"错误提示"。
 */

import type { ApiError } from '../../api/client'

export interface FailureDescription {
  readonly summary: string
  readonly detail: string
  /** ⚠️ 第三段（我现在能做什么）必须总有 */
  readonly action: string
}

/** `POST /api/results/{id}/comment` 失败时的处置。 */
export function describeCommentSaveFailure(error: ApiError): FailureDescription {
  switch (error.errorCode) {
    case 'RESULT_NOT_FOUND':
      return {
        summary: '这份结果不存在或无权访问',
        detail: '结果可能已被删除，或它属于别的租户。',
        action: '回到待办列表重新进入，或核对解析与批次是否还在。重试不会成功。',
      }
    case 'RESULT_INPUT_MISMATCH':
      return {
        summary: '结果口径与批次聚合不一致',
        detail:
          '保存时后端会校验风险等级是否等于该批次聚合。不一致说明结果与批次已经不是同一份数据。',
        action: '联系管理员核对批次与结果（这是数据问题，重试不会成功）。',
      }
    case 'RESULT_RUN_NOT_COMPLETED':
      return {
        summary: '这一批还没跑完',
        detail: '批次未完成时聚合口径不完整，后端不接受基于它的正式结果。',
        action: '等批次完成后重新进入这一页。',
      }
    default:
      return {
        summary: '保存失败',
        detail: error.message,
        action: error.retryable ? '稍后自动重试，也可以手动再保存一次。' : '请联系管理员。',
      }
  }
}

/** `POST /api/results/{id}/confirm` 失败时的处置。 */
export function describeConfirmFailure(error: ApiError): FailureDescription {
  switch (error.errorCode) {
    case 'RESULT_NOT_FOUND':
      return {
        summary: '这份结果不存在或无权访问',
        detail: '结果可能已被删除，或它属于别的租户。',
        action: '回到待办列表重新进入。重试不会成功。',
      }
    case 'AUTHORIZATION_DENIED':
      return {
        summary: '你没有确认权限',
        detail: '确认需要 result:confirm 权限。',
        action: '请有权限的复核人操作，或让管理员调整角色。',
      }
    default:
      return {
        summary: '确认失败',
        detail: error.message,
        action: error.retryable ? '稍后重试。' : '请联系管理员。',
      }
  }
}
