import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, shouldRetryQuery } from '../../api/client'
import type {
  AttachmentRow,
  ContextConfirmation,
  ContextCorrection,
  Page,
  TaskDetail,
} from '../../api/contracts'
import { queryKeys, STALE_TIME } from '../../api/queryKeys'

/**
 * 模块 2 的数据获取（M8 Task 4）。
 *
 * 附件列表一次取一页（`page_size=100`）：一份合同的附件数量是**个位数到几十**
 * 的量级，做成分页控件只会让用户多点两次。**但总数仍取自后端** ——
 * 超过一页时界面会明确说"还有更多"，而不是假装只有 100 个。
 */
const ATTACHMENT_PAGE_SIZE = 100

export function useTaskDetail(taskId: number) {
  return useQuery({
    queryKey: queryKeys.task(taskId),
    queryFn: () => api.get<TaskDetail>(`/api/tasks/${taskId}`),
    staleTime: STALE_TIME.detail,
    retry: shouldRetryQuery,
  })
}

export function useAttachments(taskId: number) {
  return useQuery({
    queryKey: queryKeys.attachments(taskId, {
      page: 1,
      pageSize: ATTACHMENT_PAGE_SIZE,
    }),
    queryFn: () =>
      api.get<Page<AttachmentRow>>(`/api/tasks/${taskId}/attachments`, {
        query: { page: 1, page_size: ATTACHMENT_PAGE_SIZE },
      }),
    staleTime: STALE_TIME.detail,
    retry: shouldRetryQuery,
  })
}

/**
 * 立场确认 / 修正。
 *
 * ⚠️ **不做乐观更新**：这个动作的成败由后端门禁决定（只有 `complete` 能确认，
 * `missing` / `conflict` 必须带修正）。前端先假装成功再回滚会让用户看到
 * "确认了 → 又没确认"的闪烁，而中间那一瞬**他已经以为生效了**。
 * 成功之后失效整棵 `['tasks']`：列表上的状态也跟着变了。
 */
export function useConfirmContext(taskId: number) {
  const client = useQueryClient()

  return useMutation({
    mutationFn: (correction?: ContextCorrection) =>
      api.post<ContextConfirmation>(
        `/api/tasks/${taskId}/context/confirm`,
        correction,
      ),
    onSuccess: async () => {
      // 只失效**受影响**的两个：这条任务的详情（状态变了）与列表
      // （列表行上有 `context_status`）。附件与解析版本不受影响 ——
      // 多失效它们只会产生没人需要的请求。
      await Promise.all([
        client.invalidateQueries({ queryKey: queryKeys.task(taskId) }),
        client.invalidateQueries({ queryKey: queryKeys.tasksList() }),
      ])
    },
  })
}
