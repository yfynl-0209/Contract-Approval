import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, shouldRetryQuery } from '../../api/client'
import type {
  AuditEventRow,
  JobRecord,
  Page,
  RetryOutcome,
  TaskLogRow,
} from '../../api/contracts'
import { queryKeys, STALE_TIME } from '../../api/queryKeys'

/**
 * 运行管理的数据获取（M8 Task 8）。
 *
 * 权限分三层（与后端一致）：
 *
 * | 查询 | 权限 | 谁能用 |
 * | --- | --- | --- |
 * | 作业 / 日志 | `task:read` | 审核人也要看自己的合同卡在哪 |
 * | 审计 | `audit:read` | 仅管理员（只追加的追责账） |
 * | 重试 | `ops:retry` | 仅管理员（重新排作业、消耗预算、改状态） |
 */

const OPS_PAGE_SIZE = 50

export interface JobListFilters {
  readonly taskId: number | null
  readonly jobStatus: string | null
}

export function useJobs(filters: JobListFilters, enabled = true) {
  return useQuery({
    queryKey: queryKeys.jobs({
      taskId: filters.taskId ?? 0,
      jobStatus: filters.jobStatus,
      page: 1,
      pageSize: OPS_PAGE_SIZE,
    }),
    queryFn: () =>
      api.get<Page<JobRecord>>('/api/jobs', {
        query: {
          page: 1,
          page_size: OPS_PAGE_SIZE,
          ...(filters.taskId === null ? {} : { task_id: filters.taskId }),
          ...(filters.jobStatus === null ? {} : { job_status: filters.jobStatus }),
        },
      }),
    enabled,
    staleTime: STALE_TIME.list,
    retry: shouldRetryQuery,
  })
}

export interface LogFilters {
  readonly logLevel: string | null
  readonly logType: string | null
  /** ⚠️ 这是本接口存在的关键：API 与 Worker 两个进程的日志靠它拼成一条链 */
  readonly correlationId: string | null
}

export function useLogs(taskId: number, filters: LogFilters, enabled = true) {
  return useQuery({
    queryKey: queryKeys.logs(taskId, {
      page: 1,
      pageSize: OPS_PAGE_SIZE,
      correlationId: filters.correlationId,
      logLevel: filters.logLevel,
      logType: filters.logType,
    }),
    queryFn: () =>
      api.get<Page<TaskLogRow>>(`/api/logs/${taskId}`, {
        query: {
          page: 1,
          page_size: OPS_PAGE_SIZE,
          ...(filters.logLevel === null ? {} : { log_level: filters.logLevel }),
          ...(filters.logType === null ? {} : { log_type: filters.logType }),
          ...(filters.correlationId === null || filters.correlationId === ''
            ? {}
            : { correlation_id: filters.correlationId }),
        },
      }),
    enabled,
    staleTime: STALE_TIME.list,
    retry: shouldRetryQuery,
  })
}

export interface AuditFilters {
  readonly taskId: number | null
  readonly action: string | null
  /** 默认 false（fail-closed）：漏看一条只是少点信息，多看到别人的是泄漏 */
  readonly includeSystem: boolean
}

export function useAudit(filters: AuditFilters, enabled = true) {
  return useQuery({
    queryKey: queryKeys.audit({
      page: 1,
      pageSize: OPS_PAGE_SIZE,
      taskId: filters.taskId,
      action: filters.action,
      includeSystem: filters.includeSystem,
    }),
    queryFn: () =>
      api.get<Page<AuditEventRow>>('/api/audit', {
        query: {
          page: 1,
          page_size: OPS_PAGE_SIZE,
          ...(filters.taskId === null ? {} : { task_id: filters.taskId }),
          ...(filters.action === null ? {} : { action: filters.action }),
          ...(filters.includeSystem ? { include_system: true } : {}),
        },
      }),
    enabled,
    staleTime: STALE_TIME.list,
    retry: shouldRetryQuery,
  })
}

export function useRetry() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (input: { readonly taskId: number; readonly reason: string }) =>
      api.post<RetryOutcome>(`/api/tasks/${input.taskId}/retry`, {
        reason: input.reason,
      }),
    onSuccess: (_outcome, input) => {
      // 重试改变任务状态、作业台账与日志 —— 这三处都要回到服务端真相
      void client.invalidateQueries({ queryKey: queryKeys.task(input.taskId) })
      void client.invalidateQueries({ queryKey: ['jobs'] })
      void client.invalidateQueries({ queryKey: ['logs'] })
      void client.invalidateQueries({ queryKey: ['audit'] })
    },
  })
}
