/**
 * 查询键与缓存策略（M8 Task 2）。
 *
 * ## 为什么集中定义键
 *
 * 键写错时的症状是"失效没生效"：确认完结果，列表上的状态还是旧的，
 * 而**没有任何报错**。集中定义的收益是可以一眼看出"哪几个键是同一棵树"——
 * 失效 `['tasks']` 就覆盖了它下面所有带参数的变体。
 *
 * 因此键的**结构是约定**：`[领域, 视角, 参数…]`。
 * 第一段是失效的粒度，第二段区分"同一领域的不同视图"（列表 / 详情）。
 *
 * ## 缓存时间（材料 §5.4）
 *
 * | 数据类型 | staleTime | 理由 |
 * | --- | --- | --- |
 * | 列表 / 汇总 | 10s | 状态变化快，但切筛选不该每次都打一遍 |
 * | 详情 / 解析 / 评价 / 结果 | 30s | 这几页要**逐行读**，中途被换掉会让人丢掉正在看的位置 |
 * | 长作业轮询 | 2s | 只在该作业**活跃**时轮询，跑完立刻停 |
 *
 * ⚠️ **不做乐观更新**：这些操作的成败由后端门禁决定，
 * 前端先假装成功、失败再回滚，会让用户看到"确认了 → 又没确认"的闪烁，
 * 而中间那一瞬他**已经以为生效了**。
 */

import type { JobStatus } from './contracts'

/** 缓存新鲜度（毫秒）。 */
export const STALE_TIME = {
  /** 列表与汇总 */
  list: 10_000,
  /** 详情类（正文要逐行读，中途不换） */
  detail: 30_000,
} as const

/** 长作业轮询间隔（毫秒）。 */
export const POLL_INTERVAL_MS = 2_000

/**
 * 作业是否还在推进 —— **轮询的开关**。
 *
 * 只在这三个状态下轮询：`succeeded` / `failed` 都是终态，
 * 继续轮询只会让界面每隔两秒重绘一次同样的内容。
 */
export function isJobActive(status: JobStatus): boolean {
  return status === 'queued' || status === 'running' || status === 'retry_wait'
}

export interface TaskListFilters {
  readonly page: number
  readonly pageSize: number
  /** 白名单枚举；拼错时后端返回 400（不是空列表） */
  readonly taskStatus: string | null
}

export interface PageFilters {
  readonly page: number
  readonly pageSize: number
}

export interface EvaluationFilters extends PageFilters {
  readonly runId: number | null
  readonly evaluationStatus: string | null
}

export const queryKeys = {
  /** 当前身份（`GET /api/me`）—— 权限判据，不随筛选变化 */
  me: () => ['me'] as const,

  /**
   * `['tasks', 'list']` 前缀 —— 失效**列表的所有筛选变体**，但不碰附件与解析。
   *
   * ⚠️ 刻意**不用** `['tasks']`（那会把附件、解析版本一起失效）：
   * 一次立场确认只影响"这条任务的状态"与"列表上那一行"，
   * 而多失效的每个键都会产生一次**没人需要的请求** ——
   * 在列表页打开时它还是并发的。
   */
  tasksList: () => ['tasks', 'list'] as const,
  tasks: (filters: TaskListFilters) => ['tasks', 'list', filters] as const,
  /** 汇总与列表**分开**：改筛选条件不该让卡片重新请求（它不随筛选变化） */
  taskSummary: () => ['tasks', 'summary'] as const,
  task: (taskId: number) => ['tasks', 'detail', taskId] as const,
  attachments: (taskId: number, filters: PageFilters) =>
    ['tasks', 'attachments', taskId, filters] as const,
  /** 某任务的解析版本列表（模块 3 的版本切换用它） */
  taskParses: (taskId: number, filters: PageFilters) =>
    ['tasks', 'parses', taskId, filters] as const,

  /**
   * ⚠️ **筛选必须进键**：`jobStatus` 不在键里时，改筛选会命中同一个缓存
   * 而界面显示的还是上一个筛选的结果 —— 没有任何报错
   * （Task 8 的测试抓到过：`failed` 筛选发出去的请求仍是旧键）。
   */
  jobs: (filters: {
    readonly taskId: number
    readonly jobStatus: string | null
    readonly page: number
    readonly pageSize: number
  }) => ['jobs', 'list', filters] as const,
  job: (jobId: number) => ['jobs', 'detail', jobId] as const,

  parse: (parseId: number) => ['parses', 'detail', parseId] as const,
  parseDocument: (parseId: number) => ['parses', 'document', parseId] as const,

  evaluations: (taskId: number, filters: EvaluationFilters) =>
    ['evaluations', 'list', taskId, filters] as const,
  run: (runId: number) => ['runs', 'detail', runId] as const,

  results: (taskId: number, filters: PageFilters) =>
    ['results', 'list', taskId, filters] as const,
  /**
   * `['results', 'list', taskId]` 前缀 —— 失效该任务的**全部结果查询变体**。
   *
   * ⚠️ 保存新正文 / 人工确认之后用它：那两件事改变的是"这个任务的结果集合"，
   * 而失效时必须回到**同一个任务**的键（用 `['results']` 会顺带失效别的任务，
   * 那些页面会各发一次没人需要的请求）。
   */
  resultsList: (taskId: number) => ['results', 'list', taskId] as const,
  result: (resultId: number) => ['results', 'detail', resultId] as const,
  writeback: (attemptId: number) => ['writebacks', 'detail', attemptId] as const,

  logs: (
    taskId: number,
    filters: PageFilters & {
      correlationId: string | null
      logLevel?: string | null
      logType?: string | null
    },
  ) => ['logs', 'list', taskId, filters] as const,
  audit: (filters: {
    readonly page: number
    readonly pageSize: number
    readonly taskId: number | null
    readonly action: string | null
    readonly includeSystem: boolean
  }) => ['audit', 'list', filters] as const,

  rules: (filters: PageFilters & { ruleStatus: string | null }) =>
    ['rules', 'list', filters] as const,
  /** `['rules', 'list']` 前缀 —— 规则改动后失效全部列表变体 */
  rulesList: () => ['rules', 'list'] as const,
  rule: (ruleCode: string) => ['rules', 'detail', ruleCode] as const,
} as const
