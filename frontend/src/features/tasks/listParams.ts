import type { TaskStatus } from '../../api/contracts'

/**
 * 列表的查询串解析（M8 Task 3）。
 *
 * 单独成文件而不是放在页面里：`react-refresh` 只在"文件只导出组件"时
 * 能热更新（与 `api/identity.ts` 同一理由），而这段逻辑本身是可以
 * **直接用单元测试钉住**的纯函数。
 *
 * ## 只认两个键，取值必须过白名单
 *
 * | 键 | 规则 | 非法时 |
 * | --- | --- | --- |
 * | `status` | 五个枚举值之一 | 当作"不筛选" |
 * | `page` | 纯数字且 ≥ 1 | 回到第 1 页 |
 *
 * ⚠️ 非法取值**回落到默认而不是报错**：链接可能来自旧版本、被手工改过、
 * 或粘贴时被截断。那时用户想看的仍然是列表，给他一个错误页是把
 * "链接问题"升级成"流程阻塞"。
 *
 * ⚠️ 更重要的是**只读这两个键**：URL 里出现的其它任何东西（表单内容、
 * 合同正文）都不参与解析、更不会被转发给后端。这是
 * "业务数据不进 URL"在读取侧的对应约束。
 */

/** 允许出现在查询串里的状态取值（与 `TaskStatus` 一致，写死以便运行时校验）。 */
export const STATUS_WHITELIST: readonly TaskStatus[] = [
  'pending',
  'parsing',
  'reviewing',
  'blocked',
  'done',
]

export interface ListParams {
  readonly page: number
  readonly status: TaskStatus | null
}

/**
 * 解析查询串。
 *
 * `page` 用正则而不是 `Number()`/`parseInt()`：
 * `Number('')` 是 `0`、`Number('3abc')` 是 `NaN`、`parseInt('3abc')` 是 `3` ——
 * 三种都对"这个链接被改过"给出了不同的判断。一个畸形的页码说明链接不可信，
 * 回到第一页比"猜一个"更可预测。
 */
export function parseListParams(searchParams: URLSearchParams): ListParams {
  const rawPage = searchParams.get('page') ?? ''
  const page = /^\d+$/.test(rawPage) ? Number.parseInt(rawPage, 10) : 1

  const rawStatus = searchParams.get('status')
  const status = STATUS_WHITELIST.find((item) => item === rawStatus) ?? null

  return { page: page >= 1 ? page : 1, status }
}
