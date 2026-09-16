import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'

import { ApiError } from '../../api/client'
import type {
  JsonValue,
  ReviewResultRow,
  TaskDetail,
  WritebackAttempt,
} from '../../api/contracts'
import { apiError, json, never, stubApi, type StubResult } from '../../test/stubApi'
import { renderApp } from '../../test/renderApp'
import { describeCommentSaveFailure, describeConfirmFailure } from './failures'
import {
  confirmationExplanation,
  isWritebackInFlight,
  writebackView,
} from './resultState'

/**
 * 模块 5（结果 / 确认 / 回写）测试（M8 Task 7）。
 *
 * ## 这里守的三条
 *
 * 1. **风险等级与完整性取自后端**：夹具里故意放一个"高风险 + 完整性 complete"
 *    这种自相矛盾的组合，界面必须**照实显示**（各显示各自的值）。
 *    前端若按四态计数重算，会把这个组合理顺成一个"看起来对"的结论 ——
 *    而那与报告、与回写门禁的依据不是同一个东西。
 * 2. **`confirmation_valid` 由后端判定**：四态文案（有效 / 正文已变更 /
 *    已被接替 / 尚未确认）只是**解释**，而"该不该禁用按钮"是解释性的；
 *    手工点亮按钮也绕不过后端。
 * 3. **保存正文只传 `comment_text`**，且确认接口**不带请求体** ——
 *    两条都断言到请求体一级（只看 URL 时"多传了一个摘要"照样通过）。
 */
function resultRow(overrides: Partial<ReviewResultRow> = {}): ReviewResultRow {
  return {
    result_id: 2,
    run_id: 21,
    task_id: 7,
    version_no: 2,
    is_current_version: true,
    confirmation_valid: false,
    overall_risk_level: 'high',
    review_status: 'needs_review',
    hit_count: 2,
    needs_review_count: 1,
    not_applicable_count: 1,
    summary_text: '本合同为我方作为采购方的设备采购合同，存在 2 处风险。',
    focus_points: ['预付款比例 60%，高于内部标准 30%', '未约定知识产权归属'],
    comment_text: '【风险审查意见】预付款比例偏高，建议调整至 30% 以内。',
    content_digest: 'aaaa1111bbbb2222',
    manual_confirmed: false,
    confirmed_by: null,
    confirmed_at: null,
    confirmed_digest: null,
    supersedes_result_id: 1,
    created_by: 'tool-six',
    created_at: '2026-09-15T10:00:00',
    updated_at: '2026-09-15T10:00:00',
    status_url: '/api/results/2',
    ...overrides,
  }
}

function taskDetail(overrides: Partial<TaskDetail> = {}): TaskDetail {
  /*
   * ⚠️ 夹具**刻意写成 `TaskDetail` 而不是宽松的 `Record<string, JsonValue>`**：
   * 类型化之后，字段名写错（例如 `is_business_blocked`）会在 `tsc` 阶段被拒
   * —— 本轮就靠它抓出三个我凭记忆写错的字段名。代价是末尾要一次断言：
   * `Partial` 展开后 TS 无法证明"每个键都还在"，而那与夹具的正确性无关。
   */
  const base = {
    task_id: 7,
    task_status: 'reviewing',
    context_status: 'complete',
    context_source: 'approval_system',
    our_party_name: '甲方公司',
    our_party_contract_label: 'party_a',
    our_party_business_role: 'buyer',
    contract_type: 'procurement',
    context_conflict: null,
    blocked_stage: null,
    last_error_code: null,
    last_error_is_business_fact: false,
    overall_risk_level: 'high',
    latest_parse_id: 11,
    latest_run_id: 21,
    current_result_id: 2,
    attachment_count: 1,
    correlation_id: null,
    writeback: {
      task_write_status: 'not_written',
      latest_attempt_id: 31,
      latest_attempt_no: 1,
      latest_attempt_status: 'not_written',
      latest_reason_code: 'MANUAL_CONFIRM_REQUIRED',
      latest_reason_text: '高风险结果需人工确认后才能回写',
      latest_attempt_at: '2026-09-15T10:05:00',
      latest_attempt_rejected: true,
      status_url: '/api/writebacks/31',
    },
    form_data: {},
    ...overrides,
  }

  return { ...base, ...overrides } as TaskDetail
}

function writebackBody(overrides: Record<string, JsonValue> = {}): Record<string, JsonValue> {
  return {
    attempt_id: 31,
    task_id: 7,
    result_id: 2,
    instance_id: 'HT-2026-0001',
    write_status: 'not_written',
    reason_code: 'MANUAL_CONFIRM_REQUIRED',
    reason_text: '高风险结果需人工确认后才能回写',
    content_digest: 'aaaa1111bbbb2222',
    attempt_no: 1,
    operator_name: 'reviewer-1',
    created_at: '2026-09-15T10:05:00',
    task_status: 'reviewing',
    task_write_status: 'not_written',
    delivery: null,
    status_url: '/api/writebacks/31',
    ...overrides,
  }
}

function setup(
  options: {
    readonly rows?: readonly ReviewResultRow[]
    readonly task?: TaskDetail
    readonly attempt?: Record<string, JsonValue>
    readonly permissions?: readonly string[]
    readonly me?: () => Response | Promise<Response>
    readonly commentResponse?: () => Response
    readonly confirmResponse?: () => Response
  } = {},
): StubResult {
  sessionStorage.setItem(
    'contract-approval.dev-identity',
    JSON.stringify({ actorId: 'reviewer-1', displayName: '', roles: ['legal_reviewer'] }),
  )
  const rows = options.rows ?? [resultRow()]

  return stubApi({
    '/api/me':
      options.me ??
      (() =>
        json({
          actor_id: 'reviewer-1',
          display_name: 'Reviewer One',
          tenant_id: 'default',
          roles: ['legal_reviewer'],
          unknown_roles: [],
          permissions: options.permissions ?? ['task:read', 'result:save', 'result:confirm'],
        })),
    '/api/tasks/7': () => json(options.task ?? taskDetail()),
    '/api/writebacks/31': () => json(options.attempt ?? writebackBody()),
    '/api/results/2/comment': () =>
      options.commentResponse?.() ??
      json({
        outcome: 'saved',
        result_id: 3,
        run_id: 21,
        task_id: 7,
        version_no: 3,
        overall_risk_level: 'high',
        content_digest: 'cccc3333dddd4444',
        result_url: '/api/results/3',
      }),
    '/api/results/2/confirm': () => options.confirmResponse?.() ?? json(resultRow({ confirmation_valid: true })),
    // ⚠️ 路径按长度倒序匹配：`/api/results/2/comment` 比 `/api/results` 长，因此先匹配
    '/api/results': () =>
      json({
        items: rows,
        total: rows.length,
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
// 1. 结论口径：全部来自后端
// ============================================================

describe('风险与完整性', () => {
  it('⚠️ 两个值**各自照实显示**，前端不重算、不互相理顺', async () => {
    // 故意自相矛盾的组合：高风险 + 完整性「完整」。
    // 前端按四态计数重算时会把它理顺成一个"看起来对"的结论 —— 那是错的
    setup({
      rows: [
        resultRow({
          overall_risk_level: 'high',
          review_status: 'complete',
          needs_review_count: 0,
        }),
      ],
    })

    renderApp('/tasks/7?tab=result')

    // 页头改用 hero 信息盒后："总风险"是盒子标签、"高"是数值元素；
    // "完整性"同理（"完整"+ 旁边统计）。
    expect(await screen.findByTestId('overall-risk')).toHaveTextContent('高')
    expect(screen.getByTestId('review-status')).toHaveTextContent('完整')
    expect(screen.getByTestId('review-status')).not.toHaveTextContent('待判断')
  })

  it('待判断条数出现在完整性旁边（它决定"还需不需要人看"）', async () => {
    setup()

    renderApp('/tasks/7?tab=result')

    expect(await screen.findByTestId('review-status')).toHaveTextContent('（1 条待判断）')
  })

  it('摘要与关注点都渲染；为空时**如实说明**而不是留空白', async () => {
    setup({
      rows: [resultRow({ summary_text: null, focus_points: [] })],
    })

    renderApp('/tasks/7?tab=result')

    expect(await screen.findByText(/这一版没有摘要文本/)).toBeInTheDocument()
    expect(screen.getByText(/这一版没有关注点/)).toBeInTheDocument()
  })
})

// ============================================================
// 2. 版本
// ============================================================

describe('结果版本', () => {
  it('默认显示**当前版本**，并可切到历史版本（切了要说明）', async () => {
    const user = userEvent.setup()
    const older = resultRow({
      result_id: 1,
      version_no: 1,
      is_current_version: false,
      comment_text: '第一版正文',
      supersedes_result_id: null,
    })
    setup({ rows: [resultRow({ comment_text: '第二版正文' }), older] })

    renderApp('/tasks/7?tab=result')

    expect(await screen.findByLabelText('回写正文')).toHaveValue('第二版正文')

    await user.selectOptions(screen.getByLabelText('结果版本'), '1')

    expect(screen.getByLabelText('回写正文')).toHaveValue('第一版正文')
    expect(screen.getByText(/正在看历史版本/)).toBeInTheDocument()
  })

  it('⚠️ 没有任何版本被标为当前时给出警示（按版本号回落不等于它是当前版本）', async () => {
    setup({ rows: [resultRow({ is_current_version: false })] })

    renderApp('/tasks/7?tab=result')

    expect(await screen.findByTestId('no-current-version')).toHaveTextContent(
      '不一定',
    )
  })
})

// ============================================================
// 3. 确认
// ============================================================

describe('人工确认', () => {
  it('⚠️ 四态文案：有效 / 正文已变更 / 已被接替 / 尚未确认', async () => {
    const cases: ReadonlyArray<{
      readonly row: Partial<ReviewResultRow>
      readonly expected: string
    }> = [
      { row: { confirmation_valid: true, manual_confirmed: true, confirmed_by: 'r' }, expected: '已确认（有效）' },
      {
        row: {
          confirmation_valid: false,
          is_current_version: true,
          manual_confirmed: true,
          confirmed_digest: 'old-old-old',
        },
        expected: '正文已变更，确认已失效',
      },
      {
        row: { confirmation_valid: false, is_current_version: false, manual_confirmed: true },
        expected: '这一版已被接替',
      },
      { row: { confirmation_valid: false, is_current_version: true }, expected: '尚未人工确认' },
    ]

    for (const item of cases) {
      setup({ rows: [resultRow(item.row)] })
      const { unmount } = renderApp('/tasks/7?tab=result')

      await waitFor(() => {
        expect(screen.getByTestId('confirmation-state')).toHaveTextContent(item.expected)
      })
      unmount()
    }
  })

  it('⚠️ 确认按钮的可点性只是**解释**：有效与已被接替都不可点', async () => {
    setup({ rows: [resultRow({ confirmation_valid: true, manual_confirmed: true })] })
    const { unmount } = renderApp('/tasks/7?tab=result')
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /确认这一版正文/ })).toBeDisabled()
    })
    unmount()

    setup({ rows: [resultRow({ is_current_version: false, manual_confirmed: true })] })
    renderApp('/tasks/7?tab=result')
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /确认这一版正文/ })).toBeDisabled()
    })
  })

  it('点确认 → POST 且**不带请求体**（后端没有传摘要的入口）', async () => {
    const user = userEvent.setup()
    const stub = setup()

    renderApp('/tasks/7?tab=result')

    await user.click(await screen.findByRole('button', { name: /确认这一版正文/ }))

    await waitFor(() => {
      const call = stub.requests.find((item) => item.url.includes('/confirm'))
      expect(call?.method).toBe('POST')
      expect(call?.body).toBeNull()
    })
  })

  it('确认成功后结果列表会**重新取回**（新的 confirmation_valid 由后端给）', async () => {
    const user = userEvent.setup()
    const stub = setup()

    renderApp('/tasks/7?tab=result')

    await user.click(await screen.findByRole('button', { name: /确认这一版正文/ }))

    await waitFor(() => {
      const listCalls = stub.calls.filter((url) => url.startsWith('/api/results'))
      expect(listCalls.length).toBeGreaterThan(1)
    })
  })
})

// ============================================================
// 4. 正文编辑
// ============================================================

describe('修改正文', () => {
  it('保存只传 `comment_text`，并说明会生成新版本', async () => {
    const user = userEvent.setup()
    const stub = setup()

    renderApp('/tasks/7?tab=result')

    const editor = await screen.findByLabelText('回写正文')
    await user.clear(editor)
    await user.type(editor, '改过的正文')
    await user.click(screen.getByRole('button', { name: '保存为新版本' }))

    await waitFor(() => {
      const call = stub.requests.find((item) => item.url.includes('/comment'))
      expect(call?.method).toBe('POST')
      expect(JSON.parse(call?.body ?? '{}')).toEqual({ comment_text: '改过的正文' })
    })

    expect(await screen.findByText(/已保存为 v3/)).toBeInTheDocument()
  })

  it('同一份正文被复用时不谎称"生成了新版本"', async () => {
    const user = userEvent.setup()
    setup({
      commentResponse: () =>
        json({
          outcome: 'reused',
          result_id: 2,
          run_id: 21,
          task_id: 7,
          version_no: 2,
          overall_risk_level: 'high',
          content_digest: 'aaaa1111bbbb2222',
          result_url: '/api/results/2',
        }),
    })

    renderApp('/tasks/7?tab=result')

    const editor = await screen.findByLabelText('回写正文')
    await user.type(editor, 'x')
    await user.click(screen.getByRole('button', { name: '保存为新版本' }))

    expect(await screen.findByText(/已复用 v2（没有多出版本）/)).toBeInTheDocument()
  })

  it('保存失败按 `error_code` 分支（口径不一致是**数据问题**，不是网络问题）', async () => {
    const user = userEvent.setup()
    setup({
      commentResponse: () => apiError('RESULT_INPUT_MISMATCH', '口径不一致', 400),
    })

    renderApp('/tasks/7?tab=result')

    const editor = await screen.findByLabelText('回写正文')
    await user.type(editor, 'x')
    await user.click(screen.getByRole('button', { name: '保存为新版本' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('结果口径与批次聚合不一致')
    expect(alert).toHaveTextContent('重试不会成功')
  })

  it('纯函数层面的分支表（不必渲染就能守住）', () => {
    const make = (code: string): ApiError =>
      new ApiError({
        status: 400,
        errorCode: code,
        message: 'x',
        retryable: false,
        correlationId: null,
        method: 'POST',
        path: '/api/x',
      })

    expect(describeCommentSaveFailure(make('RESULT_RUN_NOT_COMPLETED')).action).toContain('等批次完成')
    expect(describeCommentSaveFailure(make('RESULT_NOT_FOUND')).action).toContain('重试不会成功')
    expect(describeConfirmFailure(make('AUTHORIZATION_DENIED')).action).toContain('有权限的复核人')
    // 未列举的码也要有第三段
    expect(describeCommentSaveFailure(make('SOMETHING_NEW')).action).not.toBe('')
  })
})

// ============================================================
// 5. 权限三态
// ============================================================

describe('权限', () => {
  it('⚠️ 只读角色：编辑与确认都不可用，并说明"这不是安全边界"', async () => {
    setup({ permissions: ['task:read'] })

    renderApp('/tasks/7?tab=result')

    expect(await screen.findByLabelText('回写正文')).toHaveAttribute('readonly')
    expect(screen.getByText(/你没有修改正文的权限/)).toBeInTheDocument()
    expect(screen.getByText(/后端对写操作独立校验/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /确认这一版正文/ })).toBeDisabled()
  })

  it('⚠️ 身份**未就绪**时说的是"暂时只读"，而不是"你没有权限"', async () => {
    setup({ me: () => never() })

    renderApp('/tasks/7?tab=result')

    expect(await screen.findByText(/正在获取身份，暂时只读/)).toBeInTheDocument()
    expect(screen.queryByText(/你没有修改正文的权限/)).toBeNull()
  })
})

// ============================================================
// 6. 回写状态
// ============================================================

describe('回写状态', () => {
  it('任务级与尝试级**分开**呈现，并给出投递进度', async () => {
    setup({
      attempt: writebackBody({
        write_status: 'writing',
        reason_code: null,
        reason_text: null,
        delivery: {
          event_id: 5,
          event_type: 'comment.write',
          event_status: 'pending',
          attempt_no: 2,
          max_attempts: 5,
          next_retry_at: '2026-09-15T10:10:00',
          last_error_code: 'APPROVAL_API_TIMEOUT',
          last_error_text: '超时',
          correlation_id: 'corr-1',
          created_at: '2026-09-15T10:00:00',
          delivered_at: null,
        },
      }),
    })

    renderApp('/tasks/7?tab=result')

    expect(await screen.findByTestId('writeback-task-level')).toHaveTextContent('未回写')
    const attempt = screen.getByTestId('writeback-attempt-level')
    expect(attempt).toHaveTextContent('第 2 / 5 次投递')
    expect(attempt).toHaveTextContent('APPROVAL_API_TIMEOUT')
    expect(attempt).toHaveTextContent('正在等待派发')
  })

  it('⚠️ 门禁拒绝是**中性提示**，不是错误；且不显示"正在等待派发"', async () => {
    setup({ attempt: writebackBody() })

    renderApp('/tasks/7?tab=result')

    const attempt = await screen.findByTestId('writeback-attempt-level')
    expect(attempt).toHaveTextContent('门禁拒绝（未发起回写）')
    expect(attempt).toHaveTextContent('MANUAL_CONFIRM_REQUIRED')
    expect(attempt).toHaveTextContent('重试不会成功')
    // 没有 Outbox 事件 → 轮询必须停（否则进度提示会一直亮着）
    expect(attempt).not.toHaveTextContent('正在等待派发')
    // 拒绝不是故障：不占用 role="alert"（那是给真正的错误的）
    expect(screen.getByTestId('writeback-task-level').closest('[role="alert"]')).toBeNull()
  })

  it('投递耗尽时说明"恢复点是回写本身"', async () => {
    setup({
      attempt: writebackBody({
        write_status: 'failed',
        delivery: {
          event_id: 5,
          event_type: 'comment.write',
          event_status: 'failed',
          attempt_no: 5,
          max_attempts: 5,
          next_retry_at: null,
          last_error_code: 'APPROVAL_API_ERROR',
          last_error_text: '外部错误',
          correlation_id: null,
          created_at: '2026-09-15T10:00:00',
          delivered_at: null,
        },
      }),
    })

    renderApp('/tasks/7?tab=result')

    expect(await screen.findByText(/重试预算已耗尽/)).toBeInTheDocument()
    expect(screen.getByText(/恢复点是回写本身/)).toBeInTheDocument()
  })

  it('没有回写尝试时说明"控制台不发起回写"（而不是留空让人找按钮）', async () => {
    setup({ task: taskDetail({ writeback: { ...taskDetail().writeback, latest_attempt_id: null } }) })

    renderApp('/tasks/7?tab=result')

    expect(await screen.findByText(/控制台不发起回写/)).toBeInTheDocument()
  })

  it('纯函数：轮询开关只认"在飞行中"', () => {
    const attempt = (writeStatus: string, eventStatus: string | null): WritebackAttempt =>
      writebackBody({
        write_status: writeStatus,
        delivery:
          eventStatus === null
            ? null
            : {
                event_id: 1,
                event_type: 'comment.write',
                event_status: eventStatus,
                attempt_no: 1,
                max_attempts: 3,
                next_retry_at: null,
                last_error_code: null,
                last_error_text: null,
                correlation_id: null,
                created_at: null,
                delivered_at: null,
              },
      }) as unknown as WritebackAttempt

    // `writing` / 投递中 → 还在动；终态与门禁拒绝 → 停
    expect(isWritebackInFlight(attempt('writing', 'pending'))).toBe(true)
    expect(isWritebackInFlight(attempt('not_written', null))).toBe(false)
    expect(isWritebackInFlight(attempt('success', 'delivered'))).toBe(false)
    expect(isWritebackInFlight(null)).toBe(false)

    const rejected = writebackView(attempt('not_written', null))
    expect(rejected.rejected).toBe(true)
    expect(rejected.deliveryLabel).toContain('门禁拒绝')
    expect(writebackView(attempt('success', 'delivered')).rejected).toBe(false)
  })

  it('纯函数：`confirmationExplanation` 先看后端结论，再解释原因', () => {
    // 摘要一致但已被接替 → 仍必须是"已被接替"（顺序反了会说成"有效"）
    const explanation = confirmationExplanation(
      resultRow({
        confirmation_valid: false,
        is_current_version: false,
        manual_confirmed: true,
        confirmed_digest: 'aaaa1111bbbb2222',
      }),
    )
    expect(explanation.kind).toBe('superseded')
    expect(explanation.worthConfirming).toBe(false)
  })
})

// ============================================================
// 7. 空态
// ============================================================

describe('空态', () => {
  it('没有结果时说明"由工具 6 保存，控制台不发起它"', async () => {
    setup({ rows: [] })

    renderApp('/tasks/7?tab=result')

    expect(await screen.findByText(/还没有审查结果/)).toBeInTheDocument()
    expect(screen.getByText(/控制台不发起它/)).toBeInTheDocument()
  })

  it('结果加载失败时给三段式（含"我现在能做什么"）', async () => {
    sessionStorage.setItem(
      'contract-approval.dev-identity',
      JSON.stringify({ actorId: 'r', displayName: '', roles: ['legal_reviewer'] }),
    )
    stubApi({
      '/api/me': () => json({ actor_id: 'r', display_name: 'R', tenant_id: 'default', roles: ['legal_reviewer'], unknown_roles: [], permissions: ['task:read'] }),
      '/api/tasks/7': () => json(taskDetail()),
      '/api/results': () => apiError('RESOURCE_NOT_FOUND', '找不到', 404),
    })

    renderApp('/tasks/7?tab=result')

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByRole('button', { name: '重新加载' })).toBeInTheDocument()
  })
})
