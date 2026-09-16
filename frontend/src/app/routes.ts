/**
 * 路由与 URL 的**唯一定义处**（M8 Task 1）。
 *
 * 为什么把路径拼接收进来：字符串散落各处时，"任务详情的地址变了一下"
 * 会变成十几个文件里各改一处，而漏掉的那一处只在**某个入口**才失效
 * （比如只有从列表点进去才带对了参数）。
 *
 * ⚠️ 这里**只允许出现路径与 tab 名**：任何业务数据（合同标题、实例号、
 * 表单内容）都不许进 URL —— 材料 §Global Constraints 明确禁止，
 * 而"顺手把标题拼进查询串方便分享"正是最常见的违规方式。
 * 任务 id 是唯一允许出现在 URL 里的业务标识（它本身就是路由参数）。
 */

export const routes = {
  /** 总览：目前直接落到待办列表（模块 1） */
  home: '/',
  /** 模块 1：待办调用 */
  tasks: '/tasks',
  /** 任务工作台（模块 2–5 的容器） */
  task: (taskId: string | number): string => `/tasks/${String(taskId)}`,
  /** 扩展：规则管理 */
  rules: '/rules',
  /** 扩展：运行管理 */
  ops: '/ops',
  /** 无权限说明页（由 403 处理逻辑跳转，也可直接访问） */
  forbidden: '/forbidden',
} as const

/**
 * 工作台的四个视角。
 *
 * ⚠️ 用 `?tab=` 而**不是**嵌套路由（材料 §3）：切换视角不应产生新的历史记录，
 * 否则"后退"会退到上一个 tab，与用户对"返回列表"的预期冲突。
 * 因此这里没有任何 `routes.taskXxx()` 形态的函数 —— 它只返回查询串。
 */
export const WORKBENCH_TABS = ['detail', 'parse', 'rules', 'result'] as const

export type WorkbenchTab = (typeof WORKBENCH_TABS)[number]

/** 默认视角：先看到"这份合同是什么、卡在哪" */
export const DEFAULT_WORKBENCH_TAB: WorkbenchTab = 'detail'

export const WORKBENCH_TAB_LABELS: Readonly<Record<WorkbenchTab, string>> = {
  detail: '详情',
  parse: '解析结果',
  rules: '规则命中',
  result: '结果处理',
}

/**
 * 判断查询串里的 tab 是否合法。
 *
 * ⚠️ 非法取值**回落到默认视角**，而不是白屏或报错：
 * 链接可能来自旧版本、被手工编辑过、或从别处粘贴时截断。
 * 那时用户想看的仍然是"这份合同"，给他一个错误页是把链接问题升级成流程阻塞。
 */
export function parseWorkbenchTab(raw: string | null): WorkbenchTab {
  const found = WORKBENCH_TABS.find((tab) => tab === raw)
  return found ?? DEFAULT_WORKBENCH_TAB
}

/** 工作台地址（供 tab 链接与"切到某个视角"的跳转共用） */
export function workbenchPath(
  taskId: string | number,
  tab: WorkbenchTab = DEFAULT_WORKBENCH_TAB,
): string {
  return `${routes.task(taskId)}?tab=${tab}`
}

/**
 * 证据深链：从别的视角跳到**模块 3 的某一页某一处**（模块 4/5 的"查看原文"用它）。
 *
 * ⚠️ URL 里只放**定位符**（页码、块号），原文片段与结论文字一律不进 ——
 * 材料 §Global Constraints 禁止把正文写进 URL，而"顺手带上片段方便分享"
 * 正是最常见的违规方式。块号形如 `p3-b12`，是文档内的位置标识，不构成内容。
 */
export function evidenceDeepLink(
  taskId: string | number,
  evidence: { readonly page: number; readonly blockId: string | null },
): string {
  const params = new URLSearchParams({ tab: 'parse', page: String(evidence.page) })
  if (evidence.blockId !== null && evidence.blockId !== '') {
    params.set('block', evidence.blockId)
  }
  return `${routes.task(taskId)}?${params.toString()}`
}

/**
 * 读取证据深链的参数（`?page=2&block=p3-b12`）。
 *
 * 非法值一律当**没有**：链接可能被手工改过、或来自旧版本 ——
 * 那时给出"页码范围外"的错误页是把链接问题升级成流程阻塞，
 * 而落回第 1 页仍然让用户看到了这份合同的证据。
 */
export function readEvidenceDeepLink(search: URLSearchParams): {
  readonly page: number | null
  readonly blockId: string | null
} {
  const rawPage = search.get('page')
  const parsed = rawPage === null ? Number.NaN : Number.parseInt(rawPage, 10)
  const blockId = search.get('block')
  return {
    page: Number.isInteger(parsed) && parsed >= 1 ? parsed : null,
    blockId: blockId === null || blockId === '' ? null : blockId,
  }
}
