/**
 * 类型化 HTTP 客户端（M8 Task 2）。
 *
 * ## 这个文件的三条纪律
 *
 * ### 1. 响应体**只以字段形式**向外暴露，绝不以文本形式进入错误或日志
 *
 * 材料 §Global Constraints 明令：合同正文、`form_data`、对象键、令牌
 * 不得进入 URL、console、分析与错误上报。
 *
 * 因此 `ApiError` 只带三类信息：
 *
 * | 字段 | 来源 | 为什么可以带 |
 * | --- | --- | --- |
 * | `errorCode` | 后端错误体的 `error_code` | 稳定机器判据，不含业务内容 |
 * | `message` | 后端错误体的 `message` | 是**服务端**决定要说的话（它自己会脱敏），不是我们把响应体抄过来 |
 * | `correlationId` | 响应头 `X-Correlation-ID` | 随机标识，用来让运维去服务端日志里查细节 |
 *
 * ⚠️ 关键区别：`message` 是**读取错误体的那个字段**，不是"把响应体塞进去"。
 * 一个 HTML 502 页面、一段栈、一条 SQL —— 都不会成为 `message`。
 * 出问题时把 `correlationId` 给运维，让他去**服务端**看上下文；
 * 那才是细节该在的地方。
 *
 * ### 2. 查询串只允许**原始值**
 *
 * `buildQuery` 对对象值**抛错**而不是 `String(value)`。
 * 后者会把 `form_data` / 合同正文序列化成 `[object Object]` 或一长串 JSON 塞进 URL ——
 * 于是它进了浏览器历史、进了服务端访问日志、进了 Referer。
 * 这条约束在代码里是显式的（抛错），而不是靠"大家记得别这么写"。
 *
 * ### 3. 4xx **不重试**
 *
 * `retryable` 不只看后端给的字段，还按状态码兜底：
 * 401/403/404/409/400 说的是"先改变点什么再回来"，
 * 原样重发只会让用户把同样的失败多看两次。
 */

import type { ApiErrorBody } from './contracts'

/** 关联 ID 的响应头（`app/context.py::CORRELATION_ID_HEADER`）。 */
const CORRELATION_ID_HEADER = 'X-Correlation-ID'

/** 同源：开发期由 Vite 代理到 `127.0.0.1:8000`，生产由反向代理同源提供。 */
const API_BASE = ''

// ============================================================
// 身份头（由 `AuthProvider` 注入）
// ============================================================

/**
 * 身份头的**唯一来源**。
 *
 * 之所以做成回调而不是模块级变量：身份会变（开发期切换调用身份），
 * 而"改一次全局变量、忘记同步"的典型症状是"改了身份但请求还带着旧的"，
 * 表现为莫名其妙的 403 —— 回调让每次请求都拿当前值。
 */
type IdentityHeaderProvider = () => Readonly<Record<string, string>>

let identityHeaderProvider: IdentityHeaderProvider = () => ({})

export function setIdentityHeaderProvider(provider: IdentityHeaderProvider): void {
  identityHeaderProvider = provider
}

// ============================================================
// 关联 ID 监听（"复制关联 ID"按钮的数据来源）
// ============================================================

type CorrelationIdListener = (correlationId: string) => void

let correlationIdListener: CorrelationIdListener | null = null

/**
 * 订阅**最近一次**响应的关联 ID。
 *
 * 界面在"这次操作"的提示里展示并允许复制它（材料要求 correlation-ID
 * display/copy）：用户报障时把它给运维，运维能在服务端日志里
 * 按它拉出**两个进程**（API 与 Worker）的完整链路。
 * 不订阅时它只出现在失败的 `ApiError` 上 —— 而"操作成功了但结果不对"
 * 同样需要它。
 */
export function setCorrelationIdListener(listener: CorrelationIdListener | null): void {
  correlationIdListener = listener
}

// ============================================================
// 错误
// ============================================================

/** 网络层失败（没有响应）时的状态码占位值。 */
export const NO_RESPONSE_STATUS = 0

/** 客户端自己判定的错误码（不是后端发的）。 */
export const CLIENT_ERROR_CODES = {
  /** 请求根本没到服务端（断网、服务未启动、CORS 拦截） */
  NETWORK: 'NETWORK_ERROR',
  /** 有响应但不是我们约定的 JSON（网关 HTML 页、代理错误页） */
  MALFORMED: 'MALFORMED_RESPONSE',
} as const

export interface ApiErrorInit {
  readonly status: number
  readonly errorCode: string
  readonly message: string
  readonly retryable: boolean
  readonly correlationId: string | null
  readonly method: string
  /** ⚠️ **不含查询串**：查询串里可能有过滤值，而错误对象会被记日志 */
  readonly path: string
  /**
   * 底层原因（网络失败时的原始异常）。
   *
   * ⚠️ 挂成 `Error.cause` 而不是拼进 `message`：`cause` 不可枚举、
   * 不进日志序列化，只对调试器可见 —— 原始异常的文本里可能带 URL。
   */
  readonly cause?: unknown
}

export class ApiError extends Error {
  readonly status: number
  readonly errorCode: string
  readonly retryable: boolean
  readonly correlationId: string | null
  readonly method: string
  readonly path: string

  constructor(init: ApiErrorInit) {
    super(init.message, init.cause === undefined ? undefined : { cause: init.cause })
    this.name = 'ApiError'
    this.status = init.status
    this.errorCode = init.errorCode
    this.retryable = init.retryable
    this.correlationId = init.correlationId
    this.method = init.method
    this.path = init.path
  }

  /** 没有可信身份 → **去拿一份身份**再来（不是"去改参数"）。 */
  get isUnauthenticated(): boolean {
    return this.status === 401
  }

  /** 身份可信但缺权限 → 换账号或找管理员（**带一份新身份也没用**）。 */
  get isForbidden(): boolean {
    return this.status === 403
  }

  /** 目标不存在（含跨租户 —— 两者刻意给同一个 404，不给枚举线索）。 */
  get isNotFound(): boolean {
    return this.status === 404
  }

  /** 与当前状态冲突（等一会儿或先改状态，而不是重试同一个请求）。 */
  get isConflict(): boolean {
    return this.status === 409
  }

  /** 服务端故障（不是调用方的问题）。 */
  get isServerFault(): boolean {
    return this.status >= 500
  }
}

// ============================================================
// 三段式错误说明（材料 §5.5）
// ============================================================

export interface ErrorDescription {
  /** 是什么 */
  readonly summary: string
  /** 为什么（后端的话 + 关联 ID） */
  readonly detail: string
  /** 我现在能做什么 */
  readonly action: string
}

/**
 * 把错误翻译成"是什么 / 为什么 / 我现在能做什么"（§5.5）。
 *
 * ⚠️ 集中在**一处**而不是每个页面自己写：三段式里最容易被省掉的是第三段，
 * 而少了它，用户就只能猜 —— 猜错方向的代价是"该找管理员的人去反复重试"。
 *
 * 分派**只看 `errorCode` 与状态码**，不看 `message` 文本：
 * 中文说明会改、会翻译，用文本匹配做分支的代码会在某次改文案后静默失效。
 */
export function describeApiError(error: ApiError): ErrorDescription {
  const why = error.message
  const withId = (text: string): string =>
    error.correlationId === null
      ? text
      : `${text}（关联 ID：${error.correlationId}）`

  if (error.errorCode === CLIENT_ERROR_CODES.NETWORK) {
    return {
      summary: '无法连接服务',
      detail: withId(why),
      // ⚠️ 不写具体地址（如 127.0.0.1:8000）：后端地址随部署变，
      // 写死一个既会进产物（泄漏检查会拦），也会在生产环境里指错方向
      action: '确认后端服务已启动、且本页面的反向代理已指向它，然后重试。',
    }
  }

  if (error.errorCode === CLIENT_ERROR_CODES.MALFORMED) {
    return {
      summary: '服务返回了无法解析的响应',
      detail: withId(why),
      action:
        error.status >= 500
          ? '这通常是网关或服务端故障，稍后重试；若持续出现请把关联 ID 给运维。'
          : '请把关联 ID 给运维，服务端日志里有完整上下文。',
    }
  }

  if (error.isUnauthenticated) {
    return {
      summary: '没有可信身份',
      detail: withId(why),
      action: '提供一份有效身份（开发期填写调用身份；生产重新登录）后重试。',
    }
  }

  if (error.isForbidden) {
    return {
      summary: '没有该操作的权限',
      detail: withId(why),
      action: '换个有权限的账号，或请管理员为你的角色授予对应权限。',
    }
  }

  if (error.isNotFound) {
    return {
      summary: '目标不存在',
      detail: withId(why),
      action: '核对编号是否正确；该对象可能已被删除，或不属于你所在的租户。',
    }
  }

  if (error.isConflict) {
    return {
      summary: '当前状态不允许该操作',
      detail: withId(why),
      action: '刷新页面看最新状态，按提示先完成前置动作（如人工确认）再试。',
    }
  }

  if (error.status === 400) {
    return {
      summary: '请求参数不可接受',
      detail: withId(why),
      action: '按提示修正后重试；原样重发不会成功。',
    }
  }

  if (error.retryable || error.isServerFault) {
    return {
      summary: '服务暂时不可用',
      detail: withId(why),
      action: '稍后重试；后台会按退避策略自动重试，不需要频繁点击。',
    }
  }

  return {
    summary: '操作未完成',
    detail: withId(why),
    action: '把关联 ID 提供给开发人员以便定位。',
  }
}

// ============================================================
// 查询串
// ============================================================

/** 允许进查询串的取值。**不支持对象/数组**（见 `buildQuery` 的说明）。 */
export type QueryValue = string | number | boolean | null | undefined

/**
 * 拼查询串。
 *
 * ⚠️ 非原始值**抛错**而不是 `String(value)`：
 * 把对象序列化进 URL 是"业务数据进 URL"最常见的一条路径，
 * 而它的后果（浏览器历史、服务端访问日志、Referer 外泄）是不可撤回的 ——
 * 数据已经出去了。所以宁可在开发期就炸。
 */
export function buildQuery(params: Readonly<Record<string, QueryValue>>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined) {
      continue
    }
    if (typeof value === 'object') {
      throw new TypeError(
        `查询参数 ${key} 必须是原始值：业务数据（表单/正文）不得进入 URL`,
      )
    }
    search.append(key, String(value))
  }
  const query = search.toString()
  return query === '' ? '' : `?${query}`
}

// ============================================================
// 请求
// ============================================================

export interface RequestOptions {
  readonly query?: Readonly<Record<string, QueryValue>>
  readonly body?: unknown
  readonly signal?: AbortSignal
  /**
   * 附加请求头（如附件下载的 `Range`）。
   *
   * ⚠️ 不要用它塞身份头 —— 那条路径由 `setIdentityHeaderProvider` 统一负责，
   * 否则"某个页面忘了带头"会表现为 401，而它看起来像"身份过期了"。
   */
  readonly headers?: Readonly<Record<string, string>>
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 从错误响应体里**只取**我们认识的四个字段；其余一概丢弃。 */
function readErrorBody(raw: unknown): ApiErrorBody | null {
  if (!isRecord(raw)) {
    return null
  }
  const { error_code: errorCode, message, retryable } = raw
  if (typeof errorCode !== 'string' || typeof message !== 'string') {
    return null
  }
  return {
    outcome: 'error',
    error_code: errorCode,
    message,
    retryable: typeof retryable === 'boolean' ? retryable : false,
  }
}

/**
 * 按状态码兜底判断"重试是否有意义"。
 *
 * ⚠️ 不能只信后端的 `retryable`：网关/代理在服务不可达时返回的
 * 500/502/503 页**不是我们的错误体**，那时没有这个字段 ——
 * 若默认成"不可重试"，一次网关抖动就会被渲染成永久失败，
 * 用户去报障而我们什么也看不到。
 */
function fallbackRetryable(status: number): boolean {
  return status >= 500
}

async function readBodyText(response: Response): Promise<string> {
  try {
    return await response.text()
  } catch {
    return ''
  }
}

/**
 * 发请求 + **把非 2xx 统一映射成 `ApiError`**（含后端的 `error_code`）。
 *
 * ⚠️ 这一层是**唯一**的错误映射处。JSON 接口与字节接口（`getBytes`）
 * 都走它：分成两份时，同一份后端错误在两条路径上会得到不同的 `ApiError`
 * （一处丢了 `error_code`），而"按 `error_code` 分流"的界面就会对其中一条
 * 失灵 —— 那种失灵表现为"错误提示笼统了一点"，没人会去查。
 */
async function send(
  method: string,
  path: string,
  options: RequestOptions & { readonly accept?: string } = {},
): Promise<{ readonly response: Response; readonly correlationId: string | null }> {
  const query = buildQuery(options.query ?? {})
  const headers: Record<string, string> = {
    Accept: options.accept ?? 'application/json',
    ...identityHeaderProvider(),
    ...(options.headers ?? {}),
  }
  const hasBody = options.body !== undefined
  if (hasBody) {
    headers['Content-Type'] = 'application/json'
  }

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}${query}`, {
      method,
      headers,
      body: hasBody ? JSON.stringify(options.body) : undefined,
      signal: options.signal,
      // 同源凭据即可：控制台不跨站取数，`include` 会把 cookie 送给任何被
      // 误配的第三方地址
      credentials: 'same-origin',
    })
  } catch (cause) {
    // ⚠️ 网络失败时**不拼接 `cause` 的细节**（可能含 URL，而 URL 里有查询串）。
    // `cause` 只作为 `Error.cause` 保留给调试器，不进 message。
    throw new ApiError({
      status: NO_RESPONSE_STATUS,
      errorCode: CLIENT_ERROR_CODES.NETWORK,
      message: `请求 ${method} ${path} 未能到达服务端`,
      retryable: true,
      correlationId: null,
      method,
      path,
      cause,
    })
  }

  const correlationId = response.headers.get(CORRELATION_ID_HEADER)
  if (correlationId !== null && correlationId !== '') {
    correlationIdListener?.(correlationId)
  }

  if (!response.ok) {
    const text = await readBodyText(response)
    let parsed: unknown = null
    try {
      parsed = text === '' ? null : JSON.parse(text)
    } catch {
      parsed = null
    }

    const body = readErrorBody(parsed)
    if (body === null) {
      // 内容**不进** message：这里只说"我收到的东西不是我认识的形状"，
      // 细节交给关联 ID 去服务端查。
      throw new ApiError({
        status: response.status,
        errorCode: CLIENT_ERROR_CODES.MALFORMED,
        message: `服务端返回了非约定的错误响应（HTTP ${response.status}）`,
        retryable: fallbackRetryable(response.status),
        correlationId,
        method,
        path,
      })
    }

    throw new ApiError({
      status: response.status,
      errorCode: body.error_code,
      message: body.message,
      retryable: body.retryable || fallbackRetryable(response.status),
      correlationId,
      method,
      path,
    })
  }

  return { response, correlationId }
}

async function request<T>(
  method: string,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { response, correlationId } = await send(method, path, options)

  if (response.status === 204) {
    return undefined as T
  }

  const text = await readBodyText(response)
  try {
    return JSON.parse(text) as T
  } catch {
    throw new ApiError({
      status: response.status,
      errorCode: CLIENT_ERROR_CODES.MALFORMED,
      message: `服务端返回了无法解析的响应（HTTP ${response.status}）`,
      retryable: false,
      correlationId,
      method,
      path,
    })
  }
}

/**
 * 取**字节**（附件原件、标准文档工件）。
 *
 * ## 为什么不由渲染库（react-pdf）自己去取
 *
 * 它内部用自己的 fetch：**拿不到**我们的身份头，也**拿不到后端的 `error_code`**。
 * 而附件取不到时有两种原因，处置相反：
 *
 * | `error_code` | 含义 | 用户该做什么 |
 * | --- | --- | --- |
 * | `RESOURCE_NOT_FOUND` | 记录不存在，或不属于本租户 | **核对 id / 换任务** —— 重试永远不会成功 |
 * | `OBJECT_NOT_FOUND` | 记录在，字节没有（没下载过 / 丢了） | 去**重跑附件下载**（工具 3） |
 *
 * 只看 HTTP 404 时两者一模一样，界面只能给一个"重试"按钮 ——
 * 而第一种情况下它**永远不会成功**。因此字节由我们自己取，再交给渲染库。
 *
 * `Accept` 明说 PDF / 字节流：默认的 `application/json` 会让某些网关
 * 直接返回 406，而那看起来像"接口不支持"。
 */
async function requestBytes(
  path: string,
  options: RequestOptions = {},
): Promise<ArrayBuffer> {
  const { response } = await send('GET', path, {
    ...options,
    accept: 'application/pdf, application/octet-stream, */*',
  })
  return await response.arrayBuffer()
}

/**
 * 任意异常 → `ApiError`。
 *
 * 用于"捕获到的可能不是 `ApiError`"的地方（`useEffect` 里的 promise、
 * 渲染期间抛出的非 `ApiError`）。**不要**在各处自己 `instanceof` 判断后
 * 拼一个临时对象：那样同一个异常在不同页面上会得到不同的三段式文案，
 * 而其中一份迟早漏掉"该做什么"（第三段）。
 */
export function asApiError(cause: unknown): ApiError {
  if (cause instanceof ApiError) {
    return cause
  }
  return new ApiError({
    status: NO_RESPONSE_STATUS,
    errorCode: 'UNEXPECTED_UI_ERROR',
    message: '页面遇到了未预期的错误',
    retryable: true,
    correlationId: null,
    method: 'GET',
    path: '',
    cause,
  })
}

export const api = {
  get: <T>(path: string, options?: RequestOptions): Promise<T> =>
    request<T>('GET', path, options),
  /** 取字节（附件原件 / 标准文档工件）。错误与 JSON 路径**同一套映射**。 */
  getBytes: (path: string, options?: RequestOptions): Promise<ArrayBuffer> =>
    requestBytes(path, options),
  post: <T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> =>
    request<T>('POST', path, { ...options, body }),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> =>
    request<T>('PATCH', path, { ...options, body }),
}

// ============================================================
// 与 TanStack Query 的约定
// ============================================================

/**
 * 查询失败后是否重试（`QueryClient` 的 `retry` 用它）。
 *
 * 只重试**瞬时**失败，最多两次：
 * - 4xx（除 429）说的是"先改变点什么再回来"，原样重发永远不会成功；
 * - 重试两次以上时，用户已经在盯着一个不动的界面等了好几秒 ——
 *   而他已经可以从错误提示里读出"该做什么"。
 */
export function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (!(error instanceof ApiError)) {
    return failureCount < 2
  }
  if (!error.retryable) {
    return false
  }
  return failureCount < 2
}
