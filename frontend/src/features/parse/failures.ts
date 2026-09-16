/**
 * 模块 3 的失败处置：**按 `error_code` 分支，不按 HTTP 状态**（M8 Task 5）。
 *
 * ## 为什么这两件事值得单独一个文件
 *
 * 两个接口都会返回 **404**，而 404 的原因完全不同：
 *
 * | 接口 | `error_code` | 事实 | 用户该做什么 |
 * | --- | --- | --- | --- |
 * | 标准文档 | `RESOURCE_NOT_FOUND` | 解析记录不存在 / 不属于本租户 | **核对版本**（重试永远不会成功） |
 * | 标准文档 | `OBJECT_NOT_FOUND` | 记录在，工件还没有（解析未完成） | **重跑解析** |
 * | 标准文档 | `INVALID_GATEWAY_RESPONSE` | 工件存在但结构损坏 | 联系管理员（数据问题，不是网络问题） |
 * | 附件字节 | `RESOURCE_NOT_FOUND` | 附件记录不存在 / 不属于本租户 | **核对任务** |
 * | 附件字节 | `OBJECT_NOT_FOUND` | 记录在，**字节**没有（没下载过 / 丢了） | **重跑附件下载（工具 3）** |
 *
 * 只看状态码时这五种情况在界面上长得一模一样，于是只剩一个"重试"按钮 ——
 * 而前两种情况下它**永远不会成功**。把它做成**纯函数**还有一个好处：
 * 分支本身可以直接单测，不必渲染组件（渲染只能验证"渲染对了"，
 * 而写错分支时渲染出来的东西依然是"一个错误提示"，看起来是对的）。
 */

import type { ApiError } from '../../api/client'

export interface FailureDescription {
  readonly summary: string
  readonly detail: string
  /** ⚠️ 第三段（我现在能做什么）**必须**有：只说"失败了"等于让用户去猜 */
  readonly action: string
}

/** `GET /api/parses/{id}/document` 失败时的处置。 */
export function describeDocumentFailure(error: ApiError): FailureDescription {
  switch (error.errorCode) {
    case 'RESOURCE_NOT_FOUND':
      return {
        summary: '解析记录不存在或无权访问',
        detail: '这条解析记录可能已被删除，或它不属于当前租户。',
        action: '核对解析版本后刷新列表 —— 重试不会成功。',
      }
    case 'OBJECT_NOT_FOUND':
      return {
        summary: '标准文档工件还没有',
        detail: '解析记录存在，但它的标准文档工件尚未生成（解析可能还没跑完）。',
        action: '重跑解析，完成后再回来查看。',
      }
    case 'INVALID_GATEWAY_RESPONSE':
      return {
        summary: '标准文档工件损坏',
        detail: '工件存在但内容不是合法的标准文档 —— 这是数据问题，不是网络问题。',
        action: '联系管理员重跑解析。',
      }
    default:
      return {
        summary: '标准文档加载失败',
        detail: error.message,
        action: error.retryable ? '稍后自动重试，也可以手动重新加载。' : '请联系管理员。',
      }
  }
}

/** `GET /api/attachments/{id}/content` 失败时的处置。 */
export function describeContentFailure(error: ApiError): FailureDescription {
  switch (error.errorCode) {
    case 'RESOURCE_NOT_FOUND':
      return {
        summary: '附件记录不存在或无权访问',
        detail: '附件记录可能已被删除，或它不属于当前租户。',
        action: '核对任务与附件后重试 —— 重试不会成功。',
      }
    case 'OBJECT_NOT_FOUND':
      return {
        summary: '附件内容尚未入库',
        detail: '附件记录存在，但字节还没有下载过（或已丢失）。',
        // 控制台**没有**这个入口（硬约束：只调 `/api/*`），因此如实说明由谁触发
        action: '重跑附件下载（工具 3）后即可查看；控制台不提供该入口，它由外部调用方或定时任务触发。',
      }
    default:
      return {
        summary: '附件内容加载失败',
        detail: error.message,
        action: error.retryable ? '稍后自动重试，也可以手动重新加载。' : '请联系管理员。',
      }
  }
}
