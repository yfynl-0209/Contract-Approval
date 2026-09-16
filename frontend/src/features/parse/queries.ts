import { useCallback, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { ApiError, api, asApiError, shouldRetryQuery } from '../../api/client'
import type {
  Page,
  ParseDocumentResponse,
  ParseRecord,
} from '../../api/contracts'
import { queryKeys, STALE_TIME } from '../../api/queryKeys'

/**
 * 模块 3 的数据获取（M8 Task 5）。
 *
 * 三个请求各有各的用途，**不合并**：
 *
 * | 请求 | 给什么 | 为什么分开 |
 * | --- | --- | --- |
 * | 解析版本列表 | 有哪些版本 + 各自的质量 | 版本切换的入口；它与"选了哪一版"无关 |
 * | 解析记录 | 字段（四态 + 证据）内联 | 切版本时它整体换掉 |
 * | 标准文档 | 页 / 块 / 逐字符几何 | 只有它带**坐标**；字段里的证据靠 `block_id` 找回来 |
 *
 * ⚠️ 后两个都**以 `parseId` 为键**：版本切换时两者同时重新取 ——
 * 这正是设计 §4.3 第 3 条（切换版本时字段与 PDF 必须**同时**切换）的落地方式。
 * 分别缓存、各自失效时会出现"字段是 v2、PDF 是 v1"，而界面看起来完全正常。
 */
const VERSION_PAGE_SIZE = 50

export function useTaskParses(taskId: number) {
  return useQuery({
    queryKey: queryKeys.taskParses(taskId, { page: 1, pageSize: VERSION_PAGE_SIZE }),
    queryFn: () =>
      api.get<Page<ParseRecord>>(`/api/tasks/${taskId}/parses`, {
        query: { page: 1, page_size: VERSION_PAGE_SIZE },
      }),
    staleTime: STALE_TIME.detail,
    retry: shouldRetryQuery,
  })
}

export function useParse(parseId: number | null) {
  return useQuery({
    queryKey: queryKeys.parse(parseId ?? 0),
    queryFn: () => api.get<ParseRecord>(`/api/parses/${parseId ?? 0}`),
    enabled: parseId !== null,
    staleTime: STALE_TIME.detail,
    retry: shouldRetryQuery,
  })
}

export function useParseDocument(parseId: number | null) {
  return useQuery({
    queryKey: queryKeys.parseDocument(parseId ?? 0),
    queryFn: () =>
      api.get<ParseDocumentResponse>(`/api/parses/${parseId ?? 0}/document`),
    enabled: parseId !== null,
    staleTime: STALE_TIME.detail,
    retry: shouldRetryQuery,
  })
}

/**
 * 附件原件的字节（交给 PDF 渲染器）。
 *
 * ⚠️ **不用 `useQuery` 的缓存**去存 `ArrayBuffer`：一份 20MB 的 PDF
 * 缓存下来会跟着 `staleTime` 一直占着内存，而它**不会**被重新渲染受益
 * （同一份字节渲染两次与一次没有区别）。这里只在加载时取一次，
 * 由组件自己持有。
 */
export async function fetchAttachmentBytes(contentUrl: string): Promise<ArrayBuffer> {
  return await api.getBytes(contentUrl)
}

export interface BytesState {
  readonly data: ArrayBuffer | null
  readonly error: ApiError | null
  readonly loading: boolean
  /** 再取一次。失败态上的「重新加载」按钮用它 —— 没有它按钮就是个摆设。 */
  readonly reload: () => void
}

/**
 * 取附件字节并在组件里持有。
 *
 * ⚠️ `cancelled` 标志不是可选项：用户切版本时 URL 会变，两个请求同时在路上，
 * **后到的那个未必是新的** —— 旧响应覆盖新数据时，界面显示的是上一版的 PDF
 * 配着这一版的字段（"证据框位置有点偏"，而实际上是两份不同的合同）。
 * 请求本身无法取消（`AbortController` 能，但这里收益不足以引入它），
 * 因此至少要保证**过期的响应不写进状态**。
 */
export function useAttachmentBytes(contentUrl: string | null): BytesState {
  const [data, setData] = useState<ArrayBuffer | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(contentUrl !== null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (contentUrl === null) {
      setData(null)
      setError(null)
      setLoading(false)
      return
    }

    let cancelled = false
    setData(null)
    setError(null)
    setLoading(true)

    fetchAttachmentBytes(contentUrl)
      .then((buffer) => {
        if (!cancelled) {
          setData(buffer)
          setError(null)
          setLoading(false)
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setData(null)
          setError(asApiError(cause))
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [contentUrl, attempt])

  const reload = useCallback(() => {
    setAttempt((current) => current + 1)
  }, [])

  return { data, error, loading, reload }
}
