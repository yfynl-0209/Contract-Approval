import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'

import { ApiError } from '../../api/client'
import { apiError, json, stubApi, type StubResult } from '../../test/stubApi'
import { renderApp } from '../../test/renderApp'
import { describeRetryFailure } from './opsState'

/**
 * 运行管理页测试（M8 Task 8）。
 *
 * ## 这里守的三条
 *
 * 1. **重试的失败按"检查点"解释**：`pull` / `detail` / `download` 没有可重跑的作业
 *    （工具 1–3 同步完成）—— 409 `RETRY_NOT_SUPPORTED` 必须翻译成"去重跑哪个工具"，
 *    而不是"重试失败，请再试一次"（那会让人反复重试一个永远不被受理的请求）。
 * 2. **审计默认不含系统级事件**（fail-closed）：漏看一条只是少点信息，
 *    多看到别人的是泄漏。勾选后请求里必须带 `include_system=true`。
 * 3. **没有任务状态编辑入口**：状态只能由流程与检查点重试改变。
 */
function setup(
  options: {
    readonly permissions?: readonly string[]
    readonly jobs?: () => Response
    readonly logs?: () => Response
    readonly audit?: () => Response
    readonly retryResponse?: () => Response
  } = {},
): StubResult {
  sessionStorage.setItem(
    'contract-approval.dev-identity',
    JSON.stringify({ actorId: 'admin-1', displayName: '', roles: ['system_admin'] }),
  )

  return stubApi({
    '/api/me': () =>
      json({
        actor_id: 'admin-1',
        display_name: 'Admin',
        tenant_id: 'default',
        roles: ['system_admin'],
        unknown_roles: [],
        permissions: options.permissions ?? ['task:read', 'audit:read', 'ops:retry'],
      }),
    '/api/tasks/7/retry': () =>
      options.retryResponse?.() ??
      json({
        task_id: 7,
        blocked_stage: 'parse',
        resumed_status: 'parsing',
        retry_count: 1,
        reason: 'OCR 内存不足，已扩容',
        action: '重新排了解析作业',
        job_id: 55,
        job_type: 'parse',
        job_status: 'queued',
        attempt_id: null,
        outbox_event_id: null,
        status_url: '/api/tasks/7',
      }),
    '/api/jobs': () =>
      options.jobs?.() ??
      json({
        items: [
          {
            job_id: 55,
            task_id: 7,
            job_type: 'parse',
            job_status: 'retry_wait',
            attempt_no: 2,
            max_attempts: 5,
            next_retry_at: '2026-09-15T10:10:00',
            last_error_code: 'OCR_ENGINE_ERROR',
            last_error_text: '引擎崩溃',
            correlation_id: 'corr-9',
            status_url: '/api/jobs/55',
            result_ref: null,
          },
        ],
        total: 1,
        page: 1,
        page_size: 50,
        page_count: 1,
        has_next: false,
      }),
    '/api/logs/7': () =>
      options.logs?.() ??
      json({
        items: [
          {
            log_id: 1,
            task_id: 7,
            log_level: 'error',
            log_type: 'parse',
            log_content: 'OCR 第 3 页失败（已脱敏）',
            error_code: 'OCR_ENGINE_ERROR',
            correlation_id: 'corr-9',
            created_at: '2026-09-15T10:00:00',
          },
        ],
        total: 1,
        page: 1,
        page_size: 50,
        page_count: 1,
        has_next: false,
      }),
    '/api/audit': () =>
      options.audit?.() ??
      json({
        items: [
          {
            event_id: 1,
            task_id: 7,
            actor_id: 'reviewer-1',
            actor_name: 'Reviewer One',
            action: 'RESULT_CONFIRMED',
            target_type: 'review_result',
            target_id: '2',
            correlation_id: null,
            detail: { result_id: 2 },
            created_at: '2026-09-15T10:00:00',
          },
        ],
        total: 1,
        page: 1,
        page_size: 50,
        page_count: 1,
        has_next: false,
      }),
  })
}

afterEach(() => {
  sessionStorage.clear()
})

// ============================================================
// 1. 重试
// ============================================================

describe('检查点重试', () => {
  it('⚠️ 原因未填时按钮禁用（原因要进审计账）', async () => {
    const user = userEvent.setup()
    setup()

    renderApp('/ops')

    const panel = await screen.findByLabelText('人工重试', { selector: 'section' })
    // ⚠️ 区块在**身份就绪前**就已渲染（里面只有"正在获取身份"）——
    // 因此区块内的查询也要可等待，否则同步 getByRole 撞上加载态
    const button = await within(panel).findByRole('button', { name: '从检查点恢复' })
    expect(button).toBeDisabled()

    // 任务编号与原因**都**要填：只填原因时按钮仍然是禁用的
    // （任务编号是路径参数，没有它这个请求根本不该发出去）
    await user.type(await within(panel).findByLabelText(/任务编号/), '7')
    expect(within(panel).getByRole('button', { name: '从检查点恢复' })).toBeDisabled()
    await user.type(within(panel).getByLabelText(/重试原因/), '排查后确认可恢复')
    expect(within(panel).getByRole('button', { name: '从检查点恢复' })).toBeEnabled()
  })

  it('成功：显示后端给的 action 与作业号（不从 blocked_stage 反推）', async () => {
    const user = userEvent.setup()
    setup()

    renderApp('/ops')

    // 三个区块都有「任务编号」输入 —— 一律**先圈定区块**再取控件，
    // 否则 getByLabelText 会命中三个（而报错信息不会告诉你是哪三个）
    const panel = await screen.findByLabelText('人工重试', { selector: 'section' })
    await user.type(await within(panel).findByLabelText(/任务编号/), '7')
    await user.type(within(panel).getByLabelText(/重试原因/), 'OCR 内存不足，已扩容')
    await user.click(within(panel).getByRole('button', { name: '从检查点恢复' }))

    const outcome = await screen.findByTestId('retry-outcome')
    expect(outcome).toHaveTextContent('重新排了解析作业')
    expect(outcome).toHaveTextContent('已排作业 #55')
    expect(outcome).toHaveTextContent('失败位置：解析文档')
  })

  it('⚠️ `RETRY_NOT_SUPPORTED` → 解释"去重跑哪个工具"（不是"再试一次"）', async () => {
    const user = userEvent.setup()
    setup({
      retryResponse: () =>
        apiError('RETRY_NOT_SUPPORTED', 'pull 阶段没有可重跑对象', 409),
    })

    renderApp('/ops')

    const panel = await screen.findByLabelText('人工重试', { selector: 'section' })
    await user.type(await within(panel).findByLabelText(/任务编号/), '7')
    await user.type(within(panel).getByLabelText(/重试原因/), '想恢复')
    await user.click(within(panel).getByRole('button', { name: '从检查点恢复' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('这个失败位置不能在服务端重跑')
    expect(alert).toHaveTextContent('重跑对应的工具')
    expect(alert).toHaveTextContent('自动从检查点恢复')
  })

  it('纯函数：三类失败的处置各不相同，且都有第三段', () => {
    const make = (code: string): ApiError =>
      new ApiError({
        status: 409,
        errorCode: code,
        message: 'x',
        retryable: false,
        correlationId: null,
        method: 'POST',
        path: '/api/x',
      })

    expect(describeRetryFailure(make('RETRY_NOT_SUPPORTED')).action).toContain('工具')
    expect(describeRetryFailure(make('TASK_NOT_BLOCKED')).action).toContain('刷新任务状态')
    expect(describeRetryFailure(make('INVALID_ARGUMENT')).action).toContain('填写原因')
    expect(describeRetryFailure(make('SOMETHING_NEW')).action).not.toBe('')
  })

  it('⚠️ 无 `ops:retry` 时说明权限边界（但作业与日志仍可见）', async () => {
    setup({ permissions: ['task:read'] })

    renderApp('/ops')

    expect(await screen.findByText(/重试需要 `ops:retry` 权限/)).toBeInTheDocument()
    expect(screen.getByText(/后端独立校验/)).toBeInTheDocument()
    // 作业列表用 task:read —— 审核人要能看自己的合同卡在哪
    expect(await screen.findByText(/任务 7/)).toBeInTheDocument()
  })
})

// ============================================================
// 2. 作业 / 日志 / 审计
// ============================================================

describe('作业与日志', () => {
  it('作业显示尝试次数、退避时间与最近错误', async () => {
    setup()

    renderApp('/ops')

    const summary = await screen.findByText(/第 2\/5 次/)
    expect(summary).toHaveTextContent('等待重试')
    expect(summary).toHaveTextContent('下次重试')
    expect(summary).toHaveTextContent('OCR_ENGINE_ERROR')
  })

  it('作业状态筛选进请求（白名单校验在后端）', async () => {
    const user = userEvent.setup()
    const stub = setup()

    renderApp('/ops')

    await user.selectOptions(await screen.findByLabelText('状态'), 'failed')

    await waitFor(() => {
      expect(stub.calls.some((url) => url.includes('job_status=failed'))).toBe(true)
    })
  })

  it('日志必须给任务编号（按任务归属，不提供全表浏览）', async () => {
    setup()

    renderApp('/ops')

    expect(
      await screen.findByText(/输入任务编号以查看日志/),
    ).toBeInTheDocument()
  })

  it('⚠️ `correlation_id` 过滤真的会进请求（它是拼起两进程日志的唯一键）', async () => {
    const user = userEvent.setup()
    const stub = setup()

    renderApp('/ops')

    const logSection = screen.getByLabelText('运行日志', { selector: 'section' })
    await user.type(within(logSection).getByLabelText(/任务编号/), '7')
    await user.type(within(logSection).getByLabelText(/关联 ID/), 'corr-9')

    await waitFor(() => {
      expect(
        stub.calls.some((url) => url.includes('correlation_id=corr-9')),
      ).toBe(true)
    })
    expect(await screen.findByText(/OCR 第 3 页失败（已脱敏）/)).toBeInTheDocument()
  })
})

describe('审计', () => {
  it('⚠️ 默认**不带** `include_system`（fail-closed），勾选后才带', async () => {
    const user = userEvent.setup()
    const stub = setup()

    renderApp('/ops')

    await screen.findByText('RESULT_CONFIRMED')
    expect(stub.calls.some((url) => url.includes('include_system'))).toBe(false)

    await user.click(screen.getByRole('checkbox', { name: /包含系统级事件/ }))
    await waitFor(() => {
      expect(stub.calls.some((url) => url.includes('include_system=true'))).toBe(true)
    })
  })

  it('⚠️ 无 `audit:read` 时说明审计与日志的区别，而不是隐藏区块', async () => {
    setup({ permissions: ['task:read'] })

    renderApp('/ops')

    expect(await screen.findByText(/审计需要 `audit:read` 权限/)).toBeInTheDocument()
    expect(screen.getByText(/只追加.*追责凭据/)).toBeInTheDocument()
    // 日志仍可用（task:read）
    expect(screen.getByText(/输入任务编号以查看日志/)).toBeInTheDocument()
  })

  it('空结果说明"系统级事件默认不显示"（而不是"什么都没发生过"）', async () => {
    setup({
      audit: () =>
        json({ items: [], total: 0, page: 1, page_size: 50, page_count: 0, has_next: false }),
    })

    renderApp('/ops')

    expect(await screen.findByText(/系统级事件.*默认/)).toBeInTheDocument()
  })
})
