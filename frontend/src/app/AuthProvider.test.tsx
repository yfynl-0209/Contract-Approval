import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { buildDevIdentityHeaders } from '../api/identity'
import { createFastRetryQueryClient, renderApp } from '../test/renderApp'

/**
 * 身份与权限测试（M8 Task 2）。
 *
 * 本文件守的是**"权限从哪里来"**这件事：
 *
 * 1. 权限**只能来自服务端**（`GET /api/me`）。前端自己从 `roles` 推时，
 *    映射就有了第二份实现，漂移的表现是"入口在、点了 403" —— 两边都不报错。
 * 2. **401 不是错误**：它是"这份身份服务端不认"，处置是再填一次，
 *    而不是报障。渲染成红色故障会把人引向错误的处置。
 * 3. **导航隐藏只是提示**（材料 §5.3）：把入口藏起来不等于门禁，
 *    直达 URL 依然要能到达页面并得到后端解释。
 */

const STORAGE_KEY = 'contract-approval.dev-identity'

function identityResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Correlation-ID': 'corr-1' },
  })
}

/** 空的任务列表 / 汇总 —— 页面挂载时会自己发这两个请求。 */
const EMPTY_TASK_LIST = {
  items: [],
  total: 0,
  page: 1,
  page_size: 20,
  page_count: 1,
  has_next: false,
}
const EMPTY_SUMMARY = {
  total: 0,
  by_status: { pending: 0, parsing: 0, reviewing: 0, blocked: 0, done: 0 },
  writeback_failed: 0,
}

/**
 * 装一个**按路径分派**的 fetch 替身。
 *
 * ⚠️ 初版对所有 URL 都返回身份对象 —— 那在模块 1 落地之前是对的，
 * 之后就成了"列表拿到一份身份对象"（`items` 为 `undefined`），
 * 页面抛错、错误边界接管，于是**断言身份的用例全红**。
 * 一个"什么都能回"的替身会随被测系统的长大而悄悄变成错的。
 */
function stubApi(handlers: {
  readonly me: () => Response | Promise<Response>
  readonly tasks?: () => Response | Promise<Response>
  readonly summary?: () => Response | Promise<Response>
}): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.startsWith('/api/me')) {
      return Promise.resolve(handlers.me())
    }
    if (url.startsWith('/api/tasks/summary')) {
      return Promise.resolve(
        (handlers.summary ?? (() => identityResponse(EMPTY_SUMMARY)))(),
      )
    }
    if (url.startsWith('/api/tasks')) {
      return Promise.resolve(
        (handlers.tasks ?? (() => identityResponse(EMPTY_TASK_LIST)))(),
      )
    }
    return Promise.resolve(
      identityResponse(
        {
          outcome: 'error',
          error_code: 'RESOURCE_NOT_FOUND',
          message: '未搭桩',
          retryable: false,
        },
        404,
      ),
    )
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const AUDITOR_IDENTITY = {
  actor_id: 'auditor-1',
  display_name: 'Auditor One',
  tenant_id: 'default',
  roles: ['read_only_auditor'],
  unknown_roles: [],
  permissions: ['task:read', 'audit:read'],
}

/** 预置一份开发期身份（模拟用户上一次填过、或环境变量提供）。 */
function seedIdentity(actorId = 'li-hua', roles = 'legal_reviewer'): void {
  sessionStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({ actorId, displayName: '', roles: roles.split(',') }),
  )
}

afterEach(() => {
  sessionStorage.clear()
  vi.unstubAllGlobals()
})

// ============================================================
// 身份解析
// ============================================================

describe('身份解析', () => {
  it('没有本地身份时**不请求 /api/me**，界面提示去提供身份', async () => {
    const fetchMock = stubApi({ me: () => identityResponse(AUDITOR_IDENTITY) })

    renderApp('/tasks')

    expect(await screen.findByText('尚未提供身份')).toBeInTheDocument()
    // 没有身份时请求 /api/me 必然 401（`AUTH_MODE=dev`），
    // 那只会让界面在"未登录"与"错误"之间闪一下。
    // 列表自己的请求不受影响 —— "没有身份"不等于"不加载页面"。
    const meCalls = fetchMock.mock.calls
      .map((call) => String(call[0]))
      .filter((url) => url.startsWith('/api/me'))
    expect(meCalls).toEqual([])
  })

  it('有本地身份时请求 /api/me 并带上身份头', async () => {
    seedIdentity('li-hua', 'legal_reviewer')
    const seen: Array<{ url: string; headers: Record<string, string> }> = []
    stubApi({
      me: () => identityResponse(AUDITOR_IDENTITY),
    })
    // 再包一层以记录头（`stubApi` 只管分派，不记录请求头）
    const inner = globalThis.fetch as unknown as (
      input: RequestInfo | URL,
      init?: RequestInit,
    ) => Promise<Response>
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        seen.push({
          url: String(input),
          headers: (init?.headers ?? {}) as Record<string, string>,
        })
        return inner(input, init)
      }),
    )

    renderApp('/tasks')

    // 服务端给的 display_name 会出现在身份面板上（"你现在是谁"必须常驻可见）
    expect(await screen.findByText('Auditor One')).toBeInTheDocument()
    // ⚠️ Cookie 与请求头并行维护：扩展的 fetch 钩子丢头时，Cookie 兜底
    // （浏览器自动携带，不在 fetch 的 init 里，动不了）
    expect(document.cookie).toContain('dev_identity=')
    const meCall = seen.find((call) => call.url.startsWith('/api/me'))
    expect(meCall).toBeDefined()
    // ⚠️ 身份走标准 `Authorization` 头（浏览器扩展会剥 `X-Actor-*` 自定义头），
    // 载荷是 base64url 的 JSON —— 解开后断言字段，而不是断言整串编码。
    const authorization = meCall?.headers.Authorization
    expect(authorization).toMatch(/^Bearer dev\./)
    const payload = authorization!.slice('Bearer dev.'.length)
    const padded = payload + '='.repeat((4 - (payload.length % 4)) % 4)
    const claims = JSON.parse(
      atob(padded.replace(/-/g, '+').replace(/_/g, '/')),
    ) as { sub: string; roles: string[] }
    expect(claims.sub).toBe('li-hua')
    expect(claims.roles).toEqual(['legal_reviewer'])
  })

  it('401 走"未提供身份"而不是"错误"', async () => {
    seedIdentity()
    stubApi({
      me: () =>
        identityResponse(
          {
            outcome: 'error',
            error_code: 'AUTHENTICATION_REQUIRED',
            message: '缺少请求头 X-Actor-Id',
            retryable: false,
          },
          401,
        ),
    })

    renderApp('/tasks')

    // 处置是"再填一次身份"，所以给回表单
    expect(await screen.findByText('尚未提供身份')).toBeInTheDocument()
  })

  it('服务端故障时给出可操作提示（不是一句"失败了"）', async () => {
    seedIdentity()
    stubApi({
      me: () =>
        identityResponse(
          {
            outcome: 'error',
            error_code: 'STORAGE_UNAVAILABLE',
            message: '存储抖动',
            retryable: true,
          },
          503,
        ),
    })

    // 503 是可重试的：生产会先按指数退避重试一次再定论。
    // 这里只把**等待时间**压到 0（重试次数与最终呈现不变），
    // 否则断言要等一次 1s 退避，测试既慢又脆。
    renderApp('/tasks', { client: createFastRetryQueryClient() })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('服务暂时不可用')
    expect(within(alert).getByRole('button', { name: '重试' })).toBeInTheDocument()
  })
})

// ============================================================
// 权限只能来自服务端
// ============================================================

describe('权限判据', () => {
  it('⚠️ 前端**不**从 roles 推权限：服务端说没有就是没有', async () => {
    // 本地声明了 system_admin，但服务端给的 permissions 里没有 rule:manage。
    // 若前端自己按角色推，这里会错误地显示出"规则管理"入口。
    seedIdentity('li-hua', 'system_admin')
    stubApi({ me: () => identityResponse(AUDITOR_IDENTITY) })

    renderApp('/tasks')

    await screen.findByText('Auditor One')
    const nav = screen.getByRole('navigation', { name: '主导航' })
    expect(within(nav).queryByRole('link', { name: '规则管理' })).toBeNull()
    expect(within(nav).queryByRole('link', { name: '运行管理' })).toBeNull()
    // 只读审计能看的"待办"仍在
    expect(within(nav).getByRole('link', { name: '待办调用' })).toBeInTheDocument()
  })

  it('有权限时入口出现（管理员三个都在）', async () => {
    seedIdentity('admin-1', 'system_admin')
    stubApi({
      me: () =>
        identityResponse({
          ...AUDITOR_IDENTITY,
          actor_id: 'admin-1',
          display_name: 'Admin One',
          roles: ['system_admin'],
          permissions: ['task:read', 'rule:manage', 'ops:retry', 'audit:read'],
        }),
    })

    renderApp('/tasks')

    await screen.findByText('Admin One')
    const nav = screen.getByRole('navigation', { name: '主导航' })
    expect(within(nav).getByRole('link', { name: '规则管理' })).toBeInTheDocument()
    expect(within(nav).getByRole('link', { name: '运行管理' })).toBeInTheDocument()
  })

  it('⚠️ 隐藏入口**不是**门禁：直达 URL 依然渲染页面（由后端拒绝）', async () => {
    seedIdentity('li-hua', 'read_only_auditor')
    stubApi({ me: () => identityResponse(AUDITOR_IDENTITY) })

    // 导航里没有"规则管理"，但用户手敲 /rules 依然到达 ——
    // 页面渲染出来是**刻意的**：真正的拒绝发生在后端的 require_permissions，
    // 由 M8 Task 8 在该页面上按 403 渲染三段式说明（材料 §5.3）。
    renderApp('/rules')

    await screen.findByText('Auditor One')
    expect(screen.getByRole('heading', { name: '规则管理' })).toBeInTheDocument()
  })

  it('未识别角色会被明确提示（否则只是"入口变少了"）', async () => {
    seedIdentity('li-hua', 'future_role')
    stubApi({
      me: () =>
        identityResponse({
          ...AUDITOR_IDENTITY,
          roles: ['future_role'],
          unknown_roles: ['future_role'],
          permissions: [],
        }),
    })

    renderApp('/tasks')

    expect(await screen.findByText(/1 个角色未被识别/)).toBeInTheDocument()
  })
})

// ============================================================
// 身份头的形状（提交表单 → 请求头）
// ============================================================

describe('身份头', () => {
  /** 解开 bearer 载荷（与后端 `dev_header_identity` 的解码同一算法）。 */
  function decodeClaims(authorization: string): Record<string, unknown> {
    const payload = authorization.slice('Bearer dev.'.length)
    const padded = payload + '='.repeat((4 - (payload.length % 4)) % 4)
    const json = atob(padded.replace(/-/g, '+').replace(/_/g, '/'))
    return JSON.parse(json) as Record<string, unknown>
  }

  it('非 ASCII 显示名**不发**（载荷里只放能安全进 HTTP 头的值）', () => {
    const withoutName = buildDevIdentityHeaders({
      actorId: 'li-hua',
      displayName: '李华',
      roles: [],
    })
    expect(Object.keys(withoutName)).toEqual(['Authorization'])
    expect(decodeClaims(withoutName.Authorization!)).toEqual({
      sub: 'li-hua',
      roles: [],
    })

    const withName = buildDevIdentityHeaders({
      actorId: 'li-hua',
      displayName: 'Li Hua',
      roles: ['legal_reviewer'],
    })
    expect(decodeClaims(withName.Authorization!)).toEqual({
      sub: 'li-hua',
      name: 'Li Hua',
      roles: ['legal_reviewer'],
    })
  })

  it('没有身份时不给任何头', () => {
    expect(buildDevIdentityHeaders(null)).toEqual({})
  })

  it('填表后立即以该身份请求，并可退出（退出后回到表单）', async () => {
    const user = userEvent.setup()
    stubApi({ me: () => identityResponse(AUDITOR_IDENTITY) })

    renderApp('/tasks')

    await user.type(await screen.findByLabelText('actor_id'), 'auditor-1')
    await user.click(screen.getByRole('button', { name: '使用该身份' }))

    // 身份确认后显示的是**服务端**给的名字
    expect(await screen.findByText('Auditor One')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /退出/ }))

    expect(await screen.findByText('尚未提供身份')).toBeInTheDocument()
    // ⚠️ 退出必须把本地身份也清掉：共用机器上留着它，
    // 下一个人打开页面就会以**上一个人的名义**做决定
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull()
  })
})
