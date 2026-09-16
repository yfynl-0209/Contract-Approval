import { render, type RenderResult } from '@testing-library/react'
import type { QueryClient } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import { AppProviders } from '../app/AppProviders'
import { AppRoutes } from '../app/AppRoutes'
import { AuthProvider } from '../app/AuthProvider'
import { ErrorBoundary } from '../app/ErrorBoundary'
import { createQueryClient } from '../app/queryClient'

/**
 * 造一个"与生产同样的默认值、但退避延迟为 0"的 client。
 *
 * ⚠️ 只覆盖 `retryDelay`，其余默认值**从 `createQueryClient()` 继承**：
 * 在测试里另写一份默认值（哪怕只是抄一遍），跑的就是与生产不同的策略 ——
 * 那样测出来的行为不算数。
 *
 * 用途：验证"失败后界面出现什么"时，生产的指数退避（1s 起）会让断言
 * 先等一次重试，于是测试要么超时、要么变慢。延迟为 0 只影响**等多久**，
 * 不影响"重试几次"与"最终渲染什么"。
 */
export function createFastRetryQueryClient(): QueryClient {
  const client = createQueryClient()
  client.setDefaultOptions({
    ...client.getDefaultOptions(),
    queries: { ...client.getDefaultOptions().queries, retryDelay: 0 },
  })
  return client
}

/**
 * 测试里渲染整个应用（M8 Task 1）。
 *
 * ## 为什么用**真实**的路由与 Provider，而不是给被测组件单搭一层
 *
 * 页面之间的大量约定藏在路由与 Provider 里：`?tab=` 的解析、
 * 重定向、`QueryClient` 的默认值。单独渲染一个页面时这些都测不到，
 * 而它们恰恰是最容易配错的部分（顺序、默认值、缺一层 Provider）。
 *
 * `MemoryRouter` 而不是 `BrowserRouter`：jsdom 的地址栏在用例之间会互相污染，
 * 而"上一个用例访问过的 URL 影响了这一个"是最难排查的一类顺序依赖。
 *
 * ⚠️ 每个用例一个**全新的 `QueryClient`**：共用一个时，上一个用例缓存下来的
 * 列表会出现在下一个用例里（理由见 `AppProviders.createQueryClient`）。
 */
export function renderApp(
  initialPath = '/',
  options: { readonly client?: QueryClient } = {},
): RenderResult {
  return render(
    // 与 `main.tsx` 同序：ErrorBoundary 在 Provider 之外
    <ErrorBoundary>
      <AppProviders client={options.client ?? createQueryClient()}>
        {/* AuthProvider 与 `main.tsx` 同序（在 Router 之外）——
            顺序不同时测试跑的是与生产不同的启动路径 */}
        <AuthProvider>
          {/* future 标志同理：两边不同时，
              测试跑的是与生产不同的路由行为 —— 那样测出来的结论不算数 */}
          <MemoryRouter
            initialEntries={[initialPath]}
            future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
          >
            <AppRoutes />
          </MemoryRouter>
        </AuthProvider>
      </AppProviders>
    </ErrorBoundary>,
  )
}
