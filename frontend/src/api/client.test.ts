import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  ApiError,
  CLIENT_ERROR_CODES,
  api,
  buildQuery,
  describeApiError,
  setCorrelationIdListener,
  setIdentityHeaderProvider,
  shouldRetryQuery,
} from './client'

/**
 * HTTP 客户端测试（M8 Task 2）。
 *
 * 本文件守的是**错误处理的三条边界**，它们错了都不会有人立刻发现：
 *
 * 1. **响应体泄进错误对象**：合同正文、`form_data`、对象键一旦进了
 *    `Error.message`，就会被日志/上报/截图带出去。而"能跑通"这件事
 *    与它完全无关。
 * 2. **4xx 被当成可重试**：用户会对着同一个注定失败的请求点三次，
 *    每次都看到同样的提示 —— 而正确的处置可能是"去找管理员加权限"。
 * 3. **401 与 403 混在一起**：两者的处置相反（拿一份新身份 vs 换账号），
 *    合并后调用方只能靠猜。
 *
 * 另外守一条"工程上很容易破"的：**查询串只允许原始值**。
 */

const CORRELATION_HEADER = 'X-Correlation-ID'

function jsonResponse(
  body: unknown,
  init: { status?: number; correlationId?: string | null } = {},
): Response {
  const headers = new Headers({ 'Content-Type': 'application/json' })
  if (init.correlationId !== undefined && init.correlationId !== null) {
    headers.set(CORRELATION_HEADER, init.correlationId)
  }
  return new Response(JSON.stringify(body), { status: init.status ?? 200, headers })
}

function textResponse(text: string, status: number): Response {
  return new Response(text, {
    status,
    headers: { 'Content-Type': 'text/html' },
  })
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response): void {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : String(input)
      return Promise.resolve(handler(url, init))
    }),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
  setIdentityHeaderProvider(() => ({}))
  setCorrelationIdListener(null)
})

// ============================================================
// 成功路径
// ============================================================

describe('成功响应', () => {
  it('解析 JSON 并返回数据', async () => {
    stubFetch(() => jsonResponse({ items: [], total: 0 }))

    await expect(api.get<{ total: number }>('/api/tasks')).resolves.toEqual({
      items: [],
      total: 0,
    })
  })

  it('把响应的关联 ID 交给监听者（"复制关联 ID"按钮的数据来源）', async () => {
    const seen: string[] = []
    setCorrelationIdListener((id) => seen.push(id))
    stubFetch(() => jsonResponse({ ok: true }, { correlationId: 'corr-123' }))

    await api.get('/api/tasks')

    expect(seen).toEqual(['corr-123'])
  })

  it('204 不解析 JSON（无内容响应）', async () => {
    stubFetch(() => new Response(null, { status: 204 }))

    await expect(api.post('/api/rules/reload')).resolves.toBeUndefined()
  })

  it('身份头由注入的提供方给出，且每次请求都取当前值', async () => {
    const calls: Array<Record<string, string>> = []
    stubFetch((_url, init) => {
      calls.push((init?.headers ?? {}) as Record<string, string>)
      return jsonResponse({ ok: true })
    })

    setIdentityHeaderProvider(() => ({ 'X-Actor-Id': 'a' }))
    await api.get('/api/tasks')
    setIdentityHeaderProvider(() => ({ 'X-Actor-Id': 'b' }))
    await api.get('/api/tasks')

    expect(calls[0]?.['X-Actor-Id']).toBe('a')
    expect(calls[1]?.['X-Actor-Id']).toBe('b')
  })

  it('POST 会带上 JSON 体与 Content-Type', async () => {
    let seen: RequestInit | undefined
    stubFetch((_url, init) => {
      seen = init
      return jsonResponse({ ok: true })
    })

    await api.post('/api/tasks/1/retry', { reason: '网络恢复' })

    expect(seen?.method).toBe('POST')
    expect(seen?.body).toBe(JSON.stringify({ reason: '网络恢复' }))
    expect((seen?.headers as Record<string, string>)['Content-Type']).toBe(
      'application/json',
    )
  })
})

// ============================================================
// 错误体 → ApiError
// ============================================================

describe('错误响应', () => {
  it('只取错误体的三个字段，**不把响应体整体带上**', async () => {
    const confidential = 'CONFIDENTIAL-CONTRACT-BODY'
    stubFetch(() =>
      jsonResponse(
        {
          outcome: 'error',
          error_code: 'RESULT_INPUT_MISMATCH',
          message: '总体风险等级与批次聚合不一致',
          retryable: false,
          // 后端**不会**发这些，这里模拟"某天有人顺手加了个 debug 字段"
          debug_payload: confidential,
          contract_body: confidential,
        },
        { status: 400, correlationId: 'corr-9' },
      ),
    )

    const error = await api.get('/api/results').catch((e: unknown) => e)

    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.errorCode).toBe('RESULT_INPUT_MISMATCH')
    expect(apiError.status).toBe(400)
    expect(apiError.retryable).toBe(false)
    expect(apiError.correlationId).toBe('corr-9')

    // 关键断言：响应体没有被整体塞进错误对象
    expect(JSON.stringify(apiError.message)).not.toContain(confidential)
    expect(JSON.stringify(apiError)).not.toContain(confidential)
  })

  it('401 与 403 可区分（处置方向相反）', async () => {
    stubFetch(() =>
      jsonResponse(
        {
          outcome: 'error',
          error_code: 'AUTHENTICATION_REQUIRED',
          message: '缺少凭据',
          retryable: false,
        },
        { status: 401 },
      ),
    )
    const unauthorized = (await api.get('/api/me').catch((e: unknown) => e)) as ApiError

    stubFetch(() =>
      jsonResponse(
        {
          outcome: 'error',
          error_code: 'PERMISSION_DENIED',
          message: '缺少权限 rule:manage',
          retryable: false,
        },
        { status: 403 },
      ),
    )
    const forbidden = (await api.get('/api/rules').catch((e: unknown) => e)) as ApiError

    expect(unauthorized.isUnauthenticated).toBe(true)
    expect(unauthorized.isForbidden).toBe(false)
    expect(forbidden.isForbidden).toBe(true)
    expect(forbidden.isUnauthenticated).toBe(false)

    // 三段式的"我现在能做什么"必须不同 —— 这是 401/403 不合并的全部意义
    expect(describeApiError(unauthorized).action).toContain('身份')
    expect(describeApiError(forbidden).action).toContain('权限')
  })

  it('5xx 视为可重试（即使错误体漏写了 retryable）', async () => {
    stubFetch(() =>
      jsonResponse(
        {
          outcome: 'error',
          error_code: 'STORAGE_UNAVAILABLE',
          message: '存储抖动',
          retryable: false,
        },
        { status: 503 },
      ),
    )

    const error = (await api.get('/api/tasks').catch((e: unknown) => e)) as ApiError

    expect(error.isServerFault).toBe(true)
    expect(error.retryable).toBe(true)
  })

  it('网关的 HTML 错误页不会被当成我们的错误体', async () => {
    const html = '<html><body>502 Bad Gateway: upstream connect error</body></html>'
    stubFetch(() => textResponse(html, 502))

    const error = (await api.get('/api/tasks').catch((e: unknown) => e)) as ApiError

    expect(error.errorCode).toBe(CLIENT_ERROR_CODES.MALFORMED)
    expect(error.status).toBe(502)
    // ⚠️ 页面内容不进 message，也不进错误对象
    expect(error.message).not.toContain('upstream connect error')
    expect(error.message).not.toContain('<html>')
  })

  it('请求发不出去（服务未启动）→ NETWORK_ERROR 且可重试', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))),
    )

    const error = (await api.get('/api/tasks').catch((e: unknown) => e)) as ApiError

    expect(error.errorCode).toBe(CLIENT_ERROR_CODES.NETWORK)
    expect(error.status).toBe(0)
    expect(error.retryable).toBe(true)
    // 底层原因（可能含 URL）不进 message
    expect(error.message).not.toContain('Failed to fetch')
  })

  it('错误对象里的路径**不含查询串**', async () => {
    stubFetch(() =>
      jsonResponse(
        { outcome: 'error', error_code: 'RESOURCE_NOT_FOUND', message: '无', retryable: false },
        { status: 404 },
      ),
    )

    const error = (await api
      .get('/api/tasks', { query: { page: 2, task_status: 'blocked' } })
      .catch((e: unknown) => e)) as ApiError

    expect(error.path).toBe('/api/tasks')
    expect(JSON.stringify(error)).not.toContain('blocked')
  })
})

// ============================================================
// 重试判据
// ============================================================

describe('重试判据', () => {
  it('4xx 不重试，5xx 最多重试两次', async () => {
    const forbidden = new ApiError({
      status: 403,
      errorCode: 'PERMISSION_DENIED',
      message: '无权限',
      retryable: false,
      correlationId: null,
      method: 'GET',
      path: '/api/rules',
    })
    const unavailable = new ApiError({
      status: 503,
      errorCode: 'STORAGE_UNAVAILABLE',
      message: '存储抖动',
      retryable: true,
      correlationId: null,
      method: 'GET',
      path: '/api/tasks',
    })

    expect(shouldRetryQuery(0, forbidden)).toBe(false)
    expect(shouldRetryQuery(0, unavailable)).toBe(true)
    expect(shouldRetryQuery(2, unavailable)).toBe(false)
  })
})

// ============================================================
// 查询串
// ============================================================

describe('查询串', () => {
  it('丢弃 null / undefined，保留原始值', () => {
    expect(buildQuery({ page: 1, task_status: 'blocked', x: null, y: undefined })).toBe(
      '?page=1&task_status=blocked',
    )
    expect(buildQuery({})).toBe('')
  })

  it('⚠️ 对象值抛错 —— 业务数据不得进入 URL', () => {
    // 这是"把表单/正文顺手拼进查询串"的唯一一道闸门：
    // 一旦进了 URL，它会进浏览器历史、服务端访问日志与 Referer，撤不回来。
    expect(() => buildQuery({ form_data: { amount: 1 } as never })).toThrow(TypeError)
    expect(() => buildQuery({ ids: [1, 2] as never })).toThrow(TypeError)
  })
})
