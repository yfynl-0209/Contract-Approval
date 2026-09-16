import { screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { apiError, json, stubApi } from '../test/stubApi'
import { renderApp } from '../test/renderApp'

/**
 * 会话与轮询回归（M8 Task 9）。
 *
 * ## 轮询清理是这一组里最容易烂掉的
 *
 * `useWriteback` 在回写进行中每 2 秒轮询一次。清理逻辑若失效：
 * 用户离开页面后请求**继续打后端**，在服务端日志里表现为
 * "没人看的一个 attempt 每两秒被查一次" —— 而本地的表现为"什么都没发生"。
 * fake timers 让它可测：卸载后推进时间，请求数必须**停止增长**。
 */
function identity(): void {
  sessionStorage.setItem(
    'contract-approval.dev-identity',
    JSON.stringify({ actorId: 'r1', displayName: '', roles: ['legal_reviewer'] }),
  )
}

afterEach(() => {
  sessionStorage.clear()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('会话过期（401）', () => {
  it('⚠️ 401 不是故障：界面切到**登录表单**，而不是红色错误页', async () => {
    identity()
    stubApi({
      // 401 的语义是"这份身份服务端不认" —— 处置是再填一次
      '/api/me': () => apiError('AUTHENTICATION_REQUIRED', '身份无效', 401),
    })

    renderApp('/tasks')

    // 出现登录表单（actor_id / 显示名等输入），而不是错误堆栈
    const boxes = await screen.findAllByRole('textbox')
    expect(boxes.length).toBeGreaterThanOrEqual(2)
  })

  it('403 的解释页可直接到达，并说明"该找谁"', async () => {
    identity()
    stubApi({
      '/api/me': () =>
        json({
          actor_id: 'r1',
          display_name: 'R1',
          tenant_id: 'default',
          roles: ['legal_reviewer'],
          unknown_roles: [],
          permissions: ['task:read'],
        }),
    })

    renderApp('/forbidden')

    expect(await screen.findByText(/没有访问权限/)).toBeInTheDocument()
    expect(screen.getByText(/系统管理员/)).toBeInTheDocument()
  })
})

describe('回写轮询的清理', () => {
  it('⚠️ 卸载后轮询**停止**（离开页面不再打后端）', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    identity()
    let attempt = 0
    const stub = stubApi({
      '/api/me': () =>
        json({
          actor_id: 'r1',
          display_name: 'R1',
          tenant_id: 'default',
          roles: ['legal_reviewer'],
          unknown_roles: [],
          permissions: ['task:read'],
        }),
      '/api/tasks/7': () =>
        json({
          task_id: 7,
          task_status: 'reviewing',
          write_status: 'not_written',
          context_status: 'complete',
          context_source: 'approval_system',
          our_party_contract_label: 'party_a',
          our_party_business_role: 'buyer',
          contract_type: 'procurement',
          context_conflict: null,
          our_party_name: '甲',
          blocked_stage: null,
          block_reason: null,
          last_error_code: null,
          last_error_is_business_fact: false,
          retry_count: 0,
          created_at: null,
          updated_at: null,
          status_url: '/api/tasks/7',
          form_data: {},
          overall_risk_level: 'high',
          latest_parse_id: null,
          latest_run_id: 21,
          current_result_id: 2,
          attachment_count: 0,
          correlation_id: null,
          writeback: {
            task_write_status: 'writing',
            latest_attempt_id: 31,
            latest_attempt_no: 1,
            latest_attempt_status: 'writing',
            latest_reason_code: null,
            latest_reason_text: null,
            latest_attempt_at: null,
            latest_attempt_rejected: false,
            status_url: '/api/writebacks/31',
          },
        }),
      '/api/writebacks/31': () => {
        attempt += 1
        return json({
          attempt_id: 31,
          task_id: 7,
          result_id: 2,
          instance_id: 'X',
          write_status: 'writing',
          reason_code: null,
          reason_text: null,
          content_digest: null,
          attempt_no: 1,
          operator_name: null,
          created_at: null,
          task_status: 'reviewing',
          task_write_status: 'writing',
          delivery: {
            event_id: 1,
            event_type: 'comment.write',
            event_status: 'pending',
            attempt_no: 1,
            max_attempts: 5,
            next_retry_at: null,
            last_error_code: null,
            last_error_text: null,
            correlation_id: null,
            created_at: null,
            delivered_at: null,
          },
          status_url: '/api/writebacks/31',
        })
      },
      '/api/results': () =>
        json({ items: [], total: 0, page: 1, page_size: 50, page_count: 0, has_next: false }),
    })

    const { unmount } = renderApp('/tasks/7?tab=result')

    // 轮询开始：等第一次查询落地，然后推进两个轮询周期
    await vi.waitFor(() => {
      expect(stub.calls.some((url) => url.startsWith('/api/writebacks/31'))).toBe(true)
    })
    const during = stub.calls.filter((url) => url.startsWith('/api/writebacks/31')).length
    await vi.advanceTimersByTimeAsync(4500)
    const polling = stub.calls.filter((url) => url.startsWith('/api/writebacks/31')).length
    expect(polling).toBeGreaterThan(during)

    // ⚠️ 卸载之后推进 20 秒：一次轮询都不该再发生
    unmount()
    const afterUnmount = stub.calls.filter((url) => url.startsWith('/api/writebacks/31')).length
    await vi.advanceTimersByTimeAsync(20_000)
    expect(stub.calls.filter((url) => url.startsWith('/api/writebacks/31')).length).toBe(
      afterUnmount,
    )
    expect(attempt).toBeGreaterThan(0)
  })
})
