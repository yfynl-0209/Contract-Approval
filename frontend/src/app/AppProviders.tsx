import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'

import { createQueryClient } from './queryClient'

/**
 * 应用级 Provider（M8 Task 1）。
 *
 * 查询策略本身在 `queryClient.ts`（那里也解释了为什么值得单独一个文件）。
 * 本文件只负责"把 client 接到组件树上"，且**只导出组件** ——
 * 否则 `react-refresh` 无法热更新（改一行就整页刷新，界面状态全丢）。
 */

interface AppProvidersProps {
  readonly children: ReactNode
  /** 测试注入隔离实例；生产用默认工厂 */
  readonly client?: QueryClient
}

export function AppProviders({
  children,
  client,
}: AppProvidersProps): JSX.Element {
  // 惰性初始化：每次渲染都 `new QueryClient()` 会让所有缓存失效
  // （表现为"点了筛选又回到原来的数据"）
  const [fallback] = useState(createQueryClient)
  return (
    <QueryClientProvider client={client ?? fallback}>
      {children}
    </QueryClientProvider>
  )
}
