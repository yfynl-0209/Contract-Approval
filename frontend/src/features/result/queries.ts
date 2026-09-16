import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api, shouldRetryQuery } from '../../api/client'
import type {
  Page,
  ReviewResultRow,
  SavedResultResponse,
  WritebackAttempt,
} from '../../api/contracts'
import { POLL_INTERVAL_MS, queryKeys, STALE_TIME } from '../../api/queryKeys'
import { isWritebackInFlight } from './resultState'

/**
 * 模块 5 的数据获取（M8 Task 7）。
 *
 * ## 结果列表而不是"结果详情"
 *
 * `GET /api/results?task_id=N` 一次给出该任务的**全部历史版本**，
 * 且每行都带 `confirmation_valid`（后端算的）—— 因此不需要为每一行再发详情请求，
 * 也不需要"再取一次当前版本"（`is_current_version` 逐行标好了）。
 */
const RESULT_PAGE_SIZE = 50

export function useTaskResults(taskId: number) {
  return useQuery({
    queryKey: queryKeys.results(taskId, { page: 1, pageSize: RESULT_PAGE_SIZE }),
    queryFn: () =>
      api.get<Page<ReviewResultRow>>('/api/results', {
        query: { task_id: taskId, page: 1, page_size: RESULT_PAGE_SIZE },
      }),
    staleTime: STALE_TIME.detail,
    retry: shouldRetryQuery,
  })
}

/** 回写尝试（含投递进度）。**在飞行中时轮询**，终态立刻停。 */
export function useWriteback(attemptId: number | null) {
  return useQuery({
    queryKey: queryKeys.writeback(attemptId ?? 0),
    queryFn: () => api.get<WritebackAttempt>(`/api/writebacks/${attemptId ?? 0}`),
    enabled: attemptId !== null,
    staleTime: STALE_TIME.detail,
    retry: shouldRetryQuery,
    /*
     * ⚠️ 轮询的**开关来自数据本身**（`write_status` / 投递状态），
     * 而不是"这个页面开着就轮询"：
     * 终态（`success` / `failed` / 门禁拒绝）继续轮询只会每两秒重绘同样的内容，
     * 而"永远在转的进度条"会让人以为它还在动。
     */
    refetchInterval: (query) =>
      isWritebackInFlight(query.state.data ?? null) ? POLL_INTERVAL_MS : false,
  })
}

export interface EditCommentInput {
  readonly resultId: number
  readonly commentText: string
  /** 用于保存后失效列表缓存 —— 复用**当前查看的任务**，避免多造一个参数 */
  readonly taskId: number
}

/**
 * 修改回写正文 → **新版本**（`POST /api/results/{id}/comment`）。
 *
 * ⚠️ 成功之后要**重取结果列表**，而不是在前端把 `confirmation_valid` 改成 false：
 * 那条判断属于后端（`confirmation_valid` 含"仍是当前版本 + 摘要相符"），
 * 前端自己置位时，一旦口径变化（例如将来允许"确认后编辑仍有效"），
 * 界面会继续显示一个**它自己编的**结论。
 */
export function useEditComment() {
  const client = useQueryClient()

  return useMutation({
    mutationFn: (input: EditCommentInput) =>
      api.post<SavedResultResponse>(`/api/results/${input.resultId}/comment`, {
        comment_text: input.commentText,
      }),
    onSuccess: (_saved, input) => {
      void client.invalidateQueries({ queryKey: queryKeys.resultsList(input.taskId) })
      // 任务详情里的回写口径也可能跟着变（例如"没有可回写正文"这条拒绝原因消失）
      void client.invalidateQueries({ queryKey: queryKeys.task(input.taskId) })
    },
  })
}

/** 人工确认（`POST /api/results/{id}/confirm`）。 **不带请求体**：见接口说明。 */
export function useConfirmResult() {
  const client = useQueryClient()

  return useMutation({
    mutationFn: (input: { readonly resultId: number; readonly taskId: number }) =>
      // ⚠️ 刻意不传 body：后端**没有**传摘要的入口 ——
      // "确认了哪份正文"由服务端按当前正文摘要绑定，因此不是可以伪造的事实
      api.post<ReviewResultRow>(`/api/results/${input.resultId}/confirm`),
    onSuccess: (_row, input) => {
      void client.invalidateQueries({ queryKey: queryKeys.resultsList(input.taskId) })
      void client.invalidateQueries({ queryKey: queryKeys.task(input.taskId) })
    },
  })
}
