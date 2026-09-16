import { useQuery } from '@tanstack/react-query'

import { api, shouldRetryQuery } from '../../api/client'
import type { Page, TaskSummary, TaskView } from '../../api/contracts'
import { queryKeys, type TaskListFilters, STALE_TIME } from '../../api/queryKeys'

/**
 * 模块 1 的数据获取（M8 Task 3）。
 *
 * ## 两条刻意的"不做"
 *
 * 1. **不在前端合并分页**。每次只请求当前页，并**完全信任**后端给的
 *    `total` / `page_count` / `has_next`。把多页拼成一个数组"看起来更快"，
 *    代价是前端开始维护一份与后端不一致的数据（谁是真的一页就说不清了），
 *    而分页的权威口径必须只有一份。
 * 2. **不做乐观更新**。列表上的操作（重试、确认）成败由后端门禁决定，
 *    前端先假装成功再回滚会让用户看到"改了 → 又变回来"的闪烁，
 *    而中间那一瞬**他已经以为生效了**。
 */

/** 列表查询参数 —— 只有**白名单取值**能拼进 URL（见 `listParams.ts`）。 */
export type TaskQueryFilters = TaskListFilters

export function useTasks(filters: TaskQueryFilters) {
  return useQuery({
    queryKey: queryKeys.tasks(filters),
    queryFn: () =>
      api.get<Page<TaskView>>('/api/tasks', {
        query: {
          page: filters.page,
          page_size: filters.pageSize,
          // `null` 会被 `buildQuery` 丢掉 —— 这正是"不筛选"的表达
          task_status: filters.taskStatus,
        },
      }),
    staleTime: STALE_TIME.list,
    retry: shouldRetryQuery,
  })
}

/**
 * 汇总计数。
 *
 * ⚠️ 键**不含筛选条件**：卡片说的是"全量"，它不随列表筛选变化 ——
 * 把筛选放进键里会让每次改筛选都重新请求一次同一个数字。
 */
export function useTaskSummary() {
  return useQuery({
    queryKey: queryKeys.taskSummary(),
    queryFn: () => api.get<TaskSummary>('/api/tasks/summary'),
    staleTime: STALE_TIME.list,
    retry: shouldRetryQuery,
  })
}
