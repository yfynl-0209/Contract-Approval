import { useQuery } from '@tanstack/react-query'

import { api, shouldRetryQuery } from '../../api/client'
import type { RunDetail } from '../../api/contracts'
import { queryKeys, STALE_TIME } from '../../api/queryKeys'

/**
 * 模块 4/5 的数据获取（M8 Task 6）。
 *
 * ## 为什么用 `/api/runs/{id}` 而不是 `/api/evaluations`
 *
 * `/api/evaluations` 是**分页**的，且**返回的行是同一批次的切片**。
 * 用它渲染"四态分组 + 计数"时，计数来自**已加载的那一页**，
 * 而界面写的是"不适用 31" —— 数字与事实不一致，且**看起来完全正常**
 * （数据少于一页时两者永远相等）。`/api/runs/{id}` 一次给全：
 * 现算的聚合（四态计数 / 总风险 / 完整性 / 关注点）+ 整批评价。
 *
 * 这也是"统计与风险等级全部来自后端"（M8 验收 4）的落地方式：
 * 计数**不数数组**，直接取 `aggregate.counts`。
 */
export function useRun(runId: number | null) {
  return useQuery({
    queryKey: queryKeys.run(runId ?? 0),
    queryFn: () => api.get<RunDetail>(`/api/runs/${runId ?? 0}`),
    enabled: runId !== null,
    staleTime: STALE_TIME.detail,
    retry: shouldRetryQuery,
  })
}
