import { QueryClient } from '@tanstack/react-query'

/**
 * `QueryClient` 的构造（M8 Task 1）。
 *
 * ## 为什么单独一个文件，而不是和 `AppProviders` 放一起
 *
 * 两个原因，第二个才是决定性的：
 *
 * 1. `react-refresh` 只在"文件只导出组件"时能热更新。混着导出函数时，
 *    改一行 Provider 就会整页刷新，开发时每改一次都丢掉界面状态。
 * 2. **测试需要自己造一个 client**（每个用例一个，避免上个用例的缓存串进来）。
 *    从组件文件里 import 工厂会把整个 Provider（含 React 组件）拖进测试的
 *    依赖图里 —— 而这份默认值定义与组件无关，它只是查询策略。
 *
 * ## 默认值的理由（材料 §5.4）
 *
 * | 值 | 理由 |
 * | --- | --- |
 * | `staleTime: 10s` | 列表页的默认；详情类查询各自覆盖成 30s |
 * | `retry: 1` | 瞬时故障重试一次。**不重试 4xx**（M8 Task 2 的 `ApiError` 负责判断），否则"被门禁拒绝"会被重试三次，用户看到三次同样的失败 |
 * | `refetchOnWindowFocus: false` | 状态变化由**显式刷新与轮询**驱动。切回窗口就重新取数会在用户正读那段文字时把它换掉 —— 而"我刚看的那句话怎么没了"没有痕迹可查 |
 * | mutations `retry: 0` | 变更操作的成败由后端门禁决定；自动重发一次 = 一次用户没点过的确认/回写 |
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        retry: 1,
        refetchOnWindowFocus: false,
      },
      mutations: {
        retry: 0,
      },
    },
  })
}
