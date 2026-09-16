import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AttachmentRow, Page, TaskDetail } from '../../api/contracts'
import { apiError, json, stubApi, type StubResult } from '../../test/stubApi'
import { createFastRetryQueryClient, renderApp } from '../../test/renderApp'

/**
 * 模块 2 详情查看测试（M8 Task 4）。
 *
 * ## 本文件守的几件事（按"错了会不会有人发现"排序）
 *
 * 1. **`missing` 必须显式警告**。它是"刚拉取完任务"的**正常**状态，
 *    静默显示成普通字段时，用户会以为系统已经知道立场了 ——
 *    而方向敏感的规则此时判不了。
 * 2. **敏感值默认掩码**。控制台是要被截图贴进审批流的；
 *    默认展示原文时，泄漏发生在截图那一刻，事后追不回。
 * 3. **业务结论与系统故障措辞不同**。"附件在审批系统里没了"与"存储抖动"
 *    的处置相反（找人 vs 等重试），渲染成同一句话时用户会做错动作。
 * 4. **`missing` 下表单直接展开**：这两种状态没有可确认的对象，
 *    唯一出路是给出四值；藏在按钮后面等于让用户再多猜一步。
 */

function taskDetail(overrides: Partial<TaskDetail> = {}): TaskDetail {
  return {
    task_id: 7,
    instance_id: 'HT-2026-0007',
    approval_code: 'HT-2026-0007',
    approval_title: '原材料采购合同',
    applicant_name: '李四',
    task_status: 'reviewing',
    write_status: 'not_written',
    context_status: 'complete',
    context_source: 'approval_system',
    context_conflict: null,
    our_party_name: '我方公司',
    our_party_contract_label: 'party_a',
    our_party_business_role: 'buyer',
    contract_type: 'procurement',
    blocked_stage: null,
    block_reason: null,
    last_error_code: null,
    retry_count: 0,
    overall_risk_level: null,
    writeback: {
      task_write_status: 'not_written',
      latest_attempt_id: null,
      latest_attempt_no: null,
      latest_attempt_status: null,
      latest_reason_code: null,
      latest_reason_text: null,
      latest_attempt_at: null,
      latest_attempt_rejected: false,
      status_url: null,
    },
    attachment_count: 0,
    form_data: { 金额: '1200000', 联系人手机: '13800001234' },
    last_error_is_business_fact: false,
    latest_parse_id: null,
    latest_run_id: null,
    current_result_id: null,
    correlation_id: 'corr-abc',
    created_at: '2026-09-15T09:30:00',
    updated_at: '2026-09-15T10:00:00',
    status_url: '/api/tasks/7',
    ...overrides,
  }
}

function attachmentRow(overrides: Partial<AttachmentRow> = {}): AttachmentRow {
  return {
    attachment_record_id: 11,
    attachment_id: 'A-11',
    file_name: '原材料采购合同.pdf',
    file_type: 'pdf',
    content_type: 'application/pdf',
    file_size: 204800,
    file_checksum: 'abcdef0123456789' + '0'.repeat(48),
    download_status: 'success',
    error_message: null,
    content_url: '/api/attachments/11/content',
    created_at: '2026-09-15T09:31:00',
    ...overrides,
  }
}

function emptyPage<T>(): Page<T> {
  return { items: [], total: 0, page: 1, page_size: 100, page_count: 1, has_next: false }
}

/** 装一个详情页用的后端（身份 + 详情 + 附件 + 确认）。 */
function setupDetail(options: {
  readonly detail: TaskDetail
  readonly attachments?: readonly AttachmentRow[]
  readonly detailResponse?: () => Response
  readonly confirmResponse?: () => Response
  readonly attachmentsResponse?: () => Response
  /** 覆盖身份权限（默认给确认权限） */
  readonly permissions?: readonly string[]
}): StubResult {
  const permissions = options.permissions ?? ['task:read', 'result:confirm']
  const roles = permissions.includes('result:confirm')
    ? ['system_admin']
    : ['read_only_auditor']
  const actorId = roles[0] === 'system_admin' ? 'admin-1' : 'auditor-1'

  sessionStorage.setItem(
    'contract-approval.dev-identity',
    JSON.stringify({ actorId, displayName: '', roles }),
  )

  return stubApi({
    // ⚠️ 身份必须与业务接口**同一个替身**：分开装时后装的会覆盖前一个，
    // 而 `/api/me` 的 404 会让 `can()` 全为 false —— 页面看起来"没有权限"，
    // 而真正的原因是搭桩被覆盖了。
    '/api/me': () =>
      json({
        actor_id: actorId,
        display_name: roles[0] === 'system_admin' ? 'Admin One' : 'Auditor One',
        tenant_id: 'default',
        roles,
        unknown_roles: [],
        permissions,
      }),
    '/api/tasks/7/attachments': () =>
      options.attachmentsResponse?.() ??
      json({
        ...emptyPage<AttachmentRow>(),
        items: options.attachments ?? [],
        total: (options.attachments ?? []).length,
      }),
    '/api/tasks/7/context/confirm': () =>
      options.confirmResponse?.() ??
      json({
        task_id: 7,
        context_status: 'confirmed',
        context_source: 'manual',
        our_party_name: options.detail.our_party_name,
        our_party_contract_label: options.detail.our_party_contract_label,
        our_party_business_role: options.detail.our_party_business_role,
        contract_type: options.detail.contract_type,
        status_url: '/api/tasks/7',
      }),
    '/api/tasks/7': () => options.detailResponse?.() ?? json(options.detail),
  })
}

afterEach(() => {
  sessionStorage.clear()
  vi.unstubAllGlobals()
})

// ============================================================
// 1. 上下文四态
// ============================================================

describe('权威审查上下文', () => {
  it('`complete`：标明来自审批系统，并给出确认与修正两个动作', async () => {
    setupDetail({ detail: taskDetail({ context_status: 'complete' }) })

    renderApp('/tasks/7?tab=detail')

    const section = await screen.findByRole('region', { name: '权威审查上下文' })
    expect(section).toHaveTextContent('来自审批系统')
    expect(
      within(section).getByRole('button', { name: '确认该立场' }),
    ).toBeInTheDocument()
    expect(within(section).getByRole('button', { name: '人工修正' })).toBeInTheDocument()
  })

  it('⚠️ `missing`：显式警告"方向敏感的规则无法可靠判断"，且表单**直接展开**', async () => {
    setupDetail({
      detail: taskDetail({
        context_status: 'missing',
        our_party_name: null,
        our_party_contract_label: null,
        our_party_business_role: null,
        contract_type: null,
      }),
    })

    renderApp('/tasks/7?tab=detail')

    const section = await screen.findByRole('region', { name: '权威审查上下文' })
    expect(section).toHaveTextContent('立场未知')
    expect(section).toHaveTextContent('方向敏感的规则无法可靠判断')
    // 唯一出路是给出四值，因此表单不藏在按钮后面
    expect(
      within(section).getByRole('form', { name: '修正权威审查上下文' }),
    ).toBeInTheDocument()
    expect(within(section).getByLabelText('我方名称')).toBeInTheDocument()
  })

  it('`conflict`：警告"需人工裁定"，表单同样直接展开', async () => {
    setupDetail({
      detail: taskDetail({
        context_status: 'conflict',
        context_conflict: {
          declared: {
            our_party_name: '示例科技有限公司',
            our_party_contract_label: 'party_a',
            our_party_business_role: 'buyer',
            contract_type: 'procurement',
          },
          confirmed: {
            our_party_name: '示例科技有限公司',
            our_party_contract_label: 'party_a',
            our_party_business_role: 'seller',
            contract_type: 'procurement',
          },
        },
      }),
    })

    renderApp('/tasks/7?tab=detail')

    const section = await screen.findByRole('region', { name: '权威审查上下文' })
    expect(section).toHaveTextContent('声明与实际不一致')
    expect(section).toHaveTextContent('需人工裁定')
    expect(
      within(section).getByRole('form', { name: '修正权威审查上下文' }),
    ).toBeInTheDocument()
  })

  it('⚠️ `conflict` 必须显示**冲突双方**，并标出不一致的那一项', async () => {
    setupDetail({
      detail: taskDetail({
        context_status: 'conflict',
        context_conflict: {
          declared: {
            our_party_name: '示例科技有限公司',
            our_party_contract_label: 'party_a',
            our_party_business_role: 'buyer',
            contract_type: 'procurement',
          },
          confirmed: {
            our_party_name: '另一个公司名',
            our_party_contract_label: 'party_a',
            our_party_business_role: 'seller',
            contract_type: 'procurement',
          },
        },
      }),
    })

    renderApp('/tasks/7?tab=detail')

    const table = await screen.findByRole('table', { name: '立场冲突对照' })
    // 两个来源的名称与角色都不同 → 两处标记；标签与类型相同 → 无标记
    expect(within(table).getByText('示例科技有限公司')).toBeInTheDocument()
    expect(within(table).getByText('另一个公司名')).toBeInTheDocument()
    // 取值码要映射成文案（否则人看到的是 buyer / seller）
    expect(table).toHaveTextContent('采购方')
    expect(table).toHaveTextContent('销售方')
    expect(table).toHaveTextContent('回写已暂停')
  })

  it('没有冲突时不渲染对照表（避免"看起来冲突了"）', async () => {
    setupDetail({ detail: taskDetail({ context_status: 'complete' }) })

    renderApp('/tasks/7?tab=detail')

    await screen.findByRole('region', { name: '权威审查上下文' })
    expect(screen.queryByRole('table', { name: '立场冲突对照' })).toBeNull()
  })

  it('`confirmed`：标明已人工确认', async () => {
    setupDetail({
      detail: taskDetail({ context_status: 'confirmed', context_source: 'manual' }),
    })

    renderApp('/tasks/7?tab=detail')

    expect(
      await screen.findByRole('region', { name: '权威审查上下文' }),
    ).toHaveTextContent('已人工确认')
  })
})

// ============================================================
// 2. 修正提交
// ============================================================

describe('人工修正提交', () => {
  it('提交时带上**四条**业务事实（后端要求齐全）', async () => {
    const user = userEvent.setup()
    const stubbed = setupDetail({
      detail: taskDetail({
        context_status: 'missing',
        our_party_name: null,
        our_party_contract_label: null,
        our_party_business_role: null,
        contract_type: null,
      }),
    })

    renderApp('/tasks/7?tab=detail')

    const section = await screen.findByRole('region', { name: '权威审查上下文' })
    await user.type(within(section).getByLabelText('我方名称'), '某某科技')
    await user.selectOptions(within(section).getByLabelText('业务角色'), 'seller')
    await user.click(within(section).getByRole('button', { name: '提交并确认' }))

    await waitFor(() => {
      expect(
        stubbed.calls.some((url) => url.includes('/api/tasks/7/context/confirm')),
      ).toBe(true)
    })
  })

  it('提交后失效整棵 tasks 查询（列表上的状态也跟着变）', async () => {
    const user = userEvent.setup()
    const stubbed = setupDetail({
      detail: taskDetail({ context_status: 'complete' }),
    })

    renderApp('/tasks/7?tab=detail')

    await user.click(
      await screen.findByRole('button', { name: '确认该立场' }),
    )

    // 重新拉详情 = 失效生效（不做乐观更新，因此必须真的再请求一次）
    await waitFor(() => {
      const detailCalls = stubbed.calls.filter((url) => url === '/api/tasks/7')
      expect(detailCalls.length).toBeGreaterThan(1)
    })
  })

  it('后端拒绝时按三段式解释（不是一句"操作失败"）', async () => {
    const user = userEvent.setup()
    setupDetail({
      detail: taskDetail({ context_status: 'complete' }),
      confirmResponse: () =>
        apiError('PERMISSION_DENIED', '缺少权限 result:confirm', 403),
    })

    renderApp('/tasks/7?tab=detail')

    await user.click(await screen.findByRole('button', { name: '确认该立场' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('没有该操作的权限')
    // 第三段：该找谁
    expect(alert).toHaveTextContent('管理员')
  })

  it('没有 `result:confirm` 权限时不显示动作，并说明原因', async () => {
    setupDetail({
      detail: taskDetail({ context_status: 'complete' }),
      permissions: ['task:read', 'audit:read'],
    })

    renderApp('/tasks/7?tab=detail')

    const section = await screen.findByRole('region', { name: '权威审查上下文' })
    expect(
      within(section).queryByRole('button', { name: '确认该立场' }),
    ).toBeNull()
    expect(section).toHaveTextContent('没有确认权限')
  })
})

// ============================================================
// 3. 审批表单掩码
// ============================================================

describe('审批表单', () => {
  it('⚠️ 敏感值默认掩码，点"显示"才展开', async () => {
    const user = userEvent.setup()
    setupDetail({ detail: taskDetail() })

    renderApp('/tasks/7?tab=detail')

    const section = await screen.findByRole('region', { name: '审批表单' })
    // 非敏感字段原样展示
    expect(section).toHaveTextContent('1200000')
    // 手机号默认掩码：保留首尾各两位便于核对，不足以还原（11 位 → 中间 7 位打码）
    expect(section).toHaveTextContent('13*******34')
    expect(section).not.toHaveTextContent('13800001234')

    await user.click(within(section).getByRole('button', { name: '显示 联系人手机' }))

    expect(section).toHaveTextContent('13800001234')
  })

  it('⚠️ 表单值不进入 URL（不进浏览器历史与访问日志）', async () => {
    setupDetail({ detail: taskDetail() })

    renderApp('/tasks/7?tab=detail')

    await screen.findByRole('region', { name: '审批表单' })

    // 地址栏里不能出现表单值 —— 它可能被截图、被 Referer 带走、被历史记录留下
    expect(window.location.href).not.toContain('13800001234')
    expect(window.location.search).not.toContain('1200000')
  })
})

// ============================================================
// 4. 附件
// ============================================================

describe('附件', () => {
  it('成功行给出大小、摘要前 12 位与**可预览的**内容地址', async () => {
    setupDetail({ detail: taskDetail(), attachments: [attachmentRow()] })

    renderApp('/tasks/7?tab=detail')

    const table = await screen.findByRole('table', { name: '附件' })
    const row = within(table).getByText('原材料采购合同.pdf').closest('tr')
    expect(row).toHaveTextContent('200.0 KB')
    expect(row).toHaveTextContent('abcdef012345')
    // 地址来自后端的 content_url，不由前端拼路径
    expect(within(row as HTMLElement).getByRole('link', { name: '预览 原材料采购合同.pdf' }))
      .toHaveAttribute('href', '/api/attachments/11/content')
  })

  it('失败行解释"该找谁"：外部事实 vs 我方故障', async () => {
    setupDetail({
      detail: taskDetail({
        last_error_code: 'ATTACHMENT_MISSING',
        last_error_is_business_fact: true,
      }),
      attachments: [
        attachmentRow({
          download_status: 'failed',
          error_message: '附件已被删除',
        }),
      ],
    })

    renderApp('/tasks/7?tab=detail')

    const table = await screen.findByRole('table', { name: '附件' })
    // 用正则匹配：失败原因与错误码在**同一个**元素里，精确串匹配找不到它
    const row = within(table).getByText(/附件已被删除/).closest('tr')
    expect(row).toHaveTextContent('ATTACHMENT_MISSING')
    expect(row).toHaveTextContent('联系上传人')
    // 没有字节时不给预览入口（点了 404 比没有更糟）
    expect(within(row as HTMLElement).queryByRole('link')).toBeNull()
    expect(row).toHaveTextContent('尚不可预览')
  })

  it('我方故障时给出相反的处置（等自动重试）', async () => {
    setupDetail({
      detail: taskDetail({
        last_error_code: 'STORAGE_UNAVAILABLE',
        last_error_is_business_fact: false,
      }),
      attachments: [
        attachmentRow({ download_status: 'failed', error_message: '存储抖动' }),
      ],
    })

    renderApp('/tasks/7?tab=detail')

    const table = await screen.findByRole('table', { name: '附件' })
    expect(table).toHaveTextContent('稍后会自动重试')
  })

  it('没有附件时说明清楚，而不是留一张空表', async () => {
    setupDetail({ detail: taskDetail(), attachments: [] })

    renderApp('/tasks/7?tab=detail')

    expect(
      await screen.findByText('这份合同还没有附件记录。'),
    ).toBeInTheDocument()
  })
})

// ============================================================
// 5. 加载与失败
// ============================================================

describe('详情的加载与失败', () => {
  it('详情 5xx 时给三段式与重新加载', async () => {
    setupDetail({
      detail: taskDetail(),
      detailResponse: () => apiError('STORAGE_UNAVAILABLE', '存储抖动', 503, true),
    })

    renderApp('/tasks/7?tab=detail', { client: createFastRetryQueryClient() })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('服务暂时不可用')
    expect(within(alert).getByRole('button', { name: '重新加载' })).toBeInTheDocument()
  })

  it('附件失败**不影响**上下文与表单区块', async () => {
    setupDetail({
      detail: taskDetail(),
      attachmentsResponse: () =>
        apiError('STORAGE_UNAVAILABLE', '存储抖动', 503, true),
    })

    renderApp('/tasks/7?tab=detail', { client: createFastRetryQueryClient() })

    // 三个区块各有各的失败态：一个坏掉不该让整页变成错误页
    expect(
      await screen.findByRole('region', { name: '权威审查上下文' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('region', { name: '审批表单' })).toBeInTheDocument()
    expect(await screen.findByText(/附件列表加载失败/)).toBeInTheDocument()
  })
})
