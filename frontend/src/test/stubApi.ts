import { vi } from 'vitest'

/**
 * 测试用的 fetch 替身（M8 Task 3/4 起共用）。
 *
 * ## 为什么必须**按路径分派**，而不是"什么都能回"
 *
 * 初版在 `AuthProvider.test.tsx` 里对所有 URL 都返回身份对象。模块 1 落地后
 * 页面开始请求 `/api/tasks`，于是列表拿到一份身份对象（`items` 是 `undefined`）、
 * 页面抛错、错误边界接管 —— **7 条身份用例一起变红**，而且红得很有误导性。
 *
 * 教训：一个"什么都能回"的替身会随被测系统长大而**悄悄地变成错的**。
 * 因此这里要求调用方逐条写清"哪个前缀回什么"，**没有兜底响应**
 * （未搭桩的请求返回 404 并在 `calls` 里留下痕迹，便于定位）。
 *
 * ## ⚠️ 路由按**路径长度倒序**匹配
 *
 * `/api/tasks/summary` 也以 `/api/tasks` 开头。顺序反了会让汇总请求拿到列表的
 * 响应 —— 而那看起来只是"卡片数字不对"，不像是搭桩的问题。
 */

export interface StubRoutes {
  /**
   * 处理器收到**本次请求**（url / method / body）。
   *
   * 同一前缀下 POST 与 GET 是两回事（`POST /api/rules` 是新建、
   * `GET /api/rules` 是列表）—— 只按前缀分派时，给"新建失败"搭的桩
   * 会把列表请求一起打挂，而症状是"页面打不开"，完全不像搭桩问题。
   */
  readonly [prefix: string]: (request: StubRequest) => Response | Promise<Response>
}

export interface StubRequest {
  readonly url: string
  readonly method: string
  /** 请求体原文（非字符串体为 `null`）。**断言"传了什么"用它** */
  readonly body: string | null
}

export interface StubResult {
  /** 每次请求的 URL（**含查询串**）—— 断言"什么被发出去了"用它 */
  readonly calls: readonly string[]
  /**
   * 同上，但带**方法与请求体**。
   *
   * 写操作的验收点常常是"传了什么"（`comment_text` 对不对、
   * 确认接口**有没有**带体），只看 URL 时两者都成功发送 ——
   * 而"确认时多带了一个前端自己算的摘要"正是 M8 设计要避免的事。
   */
  readonly requests: readonly StubRequest[]
  readonly fetchMock: ReturnType<typeof vi.fn>
}

/** 一个 JSON 响应。默认带上 `X-Correlation-ID`，因为界面上会展示它。 */
export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Correlation-ID': 'corr-test' },
  })
}

/** 后端错误体（`app/api/errors.py::error_body` 的形状）。 */
export function apiError(
  errorCode: string,
  message: string,
  status: number,
  retryable = false,
): Response {
  return json({ outcome: 'error', error_code: errorCode, message, retryable }, status)
}

/** 永不落地的响应：用来稳定地停在"加载中"这一态。 */
export function never(): Promise<Response> {
  return new Promise<Response>(() => {})
}

export function stubApi(routes: StubRoutes): StubResult {
  const calls: string[] = []
  const requests: StubRequest[] = []
  const prefixes = Object.keys(routes).sort((a, b) => b.length - a.length)

  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    calls.push(url)
    const request: StubRequest = {
      url,
      method: init?.method ?? 'GET',
      body: typeof init?.body === 'string' ? init.body : null,
    }
    requests.push(request)
    for (const prefix of prefixes) {
      if (url.startsWith(prefix)) {
        const handler = routes[prefix]
        if (handler !== undefined) {
          return Promise.resolve(handler(request))
        }
      }
    }
    // ⚠️ **没有兜底成功响应**：未搭桩的请求必须显形（见文件头）
    return Promise.resolve(
      apiError('RESOURCE_NOT_FOUND', `测试未搭桩：${url}`, 404),
    )
  })

  vi.stubGlobal('fetch', fetchMock)
  return { calls, requests, fetchMock }
}
