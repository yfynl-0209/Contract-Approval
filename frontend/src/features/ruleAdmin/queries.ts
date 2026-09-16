import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, shouldRetryQuery } from '../../api/client'
import type {
  Page,
  RuleRow,
  RulesetReport,
  RuleUpdateResponse,
} from '../../api/contracts'
import { queryKeys, STALE_TIME } from '../../api/queryKeys'

/**
 * 规则管理的数据获取（M8 Task 8）。
 *
 * ⚠️ **整个模块（含只读查询）都要求 `rule:manage`**（后端如此约定）：
 * 规则是"系统怎么判"的输入，改一条会改变所有合同的结论。
 * 因此 `read_only_auditor` 与 `legal_reviewer` 连**列表**都拿不到 ——
 * 界面要给的是"为什么看不到"，而不是一张空表。
 */
const RULE_PAGE_SIZE = 100

export interface RuleListFilters {
  readonly page: number
  readonly pageSize: number
  readonly ruleStatus: string | null
  readonly ruleCategory: string | null
  readonly matchMode: string | null
}

export function useRules(filters: RuleListFilters, enabled = true) {
  return useQuery({
    queryKey: queryKeys.rules(filters),
    queryFn: () =>
      api.get<Page<RuleRow>>('/api/rules', {
        query: {
          page: filters.page,
          page_size: RULE_PAGE_SIZE,
          // 后端按白名单校验：拼错 → 400（不是空列表）。因此只在真有值时才带
          ...(filters.ruleStatus === null ? {} : { rule_status: filters.ruleStatus }),
          ...(filters.ruleCategory === null ? {} : { rule_category: filters.ruleCategory }),
          ...(filters.matchMode === null ? {} : { match_mode: filters.matchMode }),
        },
      }),
    /*
     * ⚠️ `enabled` 由调用方给：无权限时**不发**这个注定 403 的请求。
     * 对必败接口的自动重试会把"权限问题"伪装成"服务不稳定"，
     * 而重试日志里会多出一串没人需要的 403。
     */
    enabled,
    // 规则列表是人工维护的配置（不是高频变化的数据），但改完要立刻看到
    staleTime: STALE_TIME.list,
    retry: shouldRetryQuery,
  })
}

/** 激活前校验（`POST /api/rules/reload`）—— **不是**重新加载，是闸门。 */
export function useActivateValidation() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () => api.post<RulesetReport>('/api/rules/reload'),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.rulesList() })
    },
  })
}

export function useCreateRule() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.post<RuleRow>('/api/rules', body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.rulesList() })
    },
  })
}

export interface UpdateRuleInput {
  readonly ruleCode: string
  /** **只带改过的字段**（后端按"显式提供"判：缺省=不动，显式 null=清空） */
  readonly body: Record<string, unknown>
}

export function useUpdateRule() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (input: UpdateRuleInput) =>
      api.patch<RuleUpdateResponse>(
        `/api/rules/${encodeURIComponent(input.ruleCode)}`,
        input.body,
      ),
    onSuccess: (row) => {
      void client.invalidateQueries({ queryKey: queryKeys.rulesList() })
      void client.invalidateQueries({ queryKey: queryKeys.rule(row.rule_code) })
    },
  })
}
