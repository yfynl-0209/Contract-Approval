import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Page, TaskSummary, TaskView } from '../../api/contracts'
import { formatLocalTime } from '../../domain/time'
import { createFastRetryQueryClient, renderApp } from '../../test/renderApp'
import { json, never, stubApi } from '../../test/stubApi'
import { parseListParams } from './listParams'

/**
 * 模块 1 待办列表测试（M8 Task 3）。
 *
 * ## 本文件守的四件事（按"错了会不会有人发现"排序）
 *
 * 1. **统计与页码来自后端**。前端只能数到当前这一页，而卡片说的是全量 ——
 *    数据超过一页时两者不等，**而数据少于一页时永远相等**，
 *    因此"前端自己数"的缺陷只会在生产量级上显形。用一组
 *    "总数 45、本页 1 行"的数据把它钉死。
 * 2. **状态与原因分两处**（§5.1）。只显示"未回写"时，
 *    "被门禁拒绝"看起来像"还没轮到"，而前者重试永远是白试。
 * 3. **没审过 ≠ 低风险**。`null` 必须显示 `—`。
 * 4. **业务数据不进 URL**。URL 里只允许 `status` / `page`，且取值必须过白名单。
 */

// ============================================================
// 假后端
// ============================================================

function taskRow(overrides: Partial<TaskView> = {}): TaskView {
  return {
    task_id: 1,
    instance_id: 'HT-2026-0001',
    approval_code: 'HT-2026-0001',
    approval_title: '办公设备采购合同',
    applicant_name: '张三',
    task_status: 'done',
    write_status: 'success',
    context_status: 'confirmed',
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
    overall_risk_level: 'low',
    writeback: {
      task_write_status: 'success',
      latest_attempt_id: 7,
      latest_attempt_no: 1,
      latest_attempt_status: 'success',
      latest_reason_code: null,
      latest_reason_text: null,
      latest_attempt_at: '2026-09-15T10:00:00',
      latest_attempt_rejected: false,
      status_url: '/api/writebacks/7',
    },
    attachment_count: 1,
    created_at: '2026-09-15T09:30:00',
    updated_at: '2026-09-15T10:00:00',
    status_url: '/api/tasks/1',
    ...overrides,
  }
}

function page(items: TaskView[], overrides: Partial<Page<TaskView>> = {}): Page<TaskView> {
  return {
    items,
    total: items.length,
    page: 1,
    page_size: 20,
    page_count: 1,
    has_next: false,
    ...overrides,
  }
}

function summaryBody(overrides: Partial<TaskSummary> = {}): TaskSummary {
  return {
    total: 62,
    by_status: { pending: 12, parsing: 2, reviewing: 3, blocked: 2, done: 43 },
    writeback_failed: 1,
    ...overrides,
  }
}

afterEach(() => {
  sessionStorage.clear()
  vi.unstubAllGlobals()
})

// ============================================================
// 1. 加载 / 空 / 错
// ============================================================

describe('加载 / 空 / 错', () => {
  it('加载中保留表头，避免布局跳动', async () => {
    stubApi({
      '/api/tasks': never,
      '/api/tasks/summary': () => json(summaryBody()),
    })

    renderApp('/tasks')

    expect(screen.getByRole('columnheader', { name: '编号' })).toBeInTheDocument()
    expect(screen.getByText('正在加载…')).toBeInTheDocument()
  })

  it('空列表说明"同步由谁触发"，且**不放假按钮**', async () => {
    stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () => json(page([])),
    })

    renderApp('/tasks')

    expect(await screen.findByText(/暂无待办/)).toBeInTheDocument()
    // 控制台只调 /api/*，"触发拉取"目前没有对应端点 ——
    // 放一个点了没反应的按钮比不放更糟
    expect(screen.queryByRole('button', { name: /拉取/ })).toBeNull()
  })

  it('空列表与"筛选后为空"分开说', async () => {
    stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () => json(page([])),
    })

    renderApp('/tasks?status=blocked')

    expect(
      await screen.findByText(/当前筛选条件下没有任务/),
    ).toBeInTheDocument()
  })

  it('5xx 按三段式呈现，并说明"稍后重试"', async () => {
    stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () =>
        json(
          {
            outcome: 'error',
            error_code: 'STORAGE_UNAVAILABLE',
            message: '存储抖动',
            retryable: true,
          },
          503,
        ),
    })

    renderApp('/tasks', { client: createFastRetryQueryClient() })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('服务暂时不可用')
    // 第三段（我该做什么）必须在 —— 只说"失败了"等于让用户去猜
    expect(alert).toHaveTextContent('稍后重试')
    expect(within(alert).getByRole('button', { name: '重新加载' })).toBeInTheDocument()
  })
})

// ============================================================
// 2. 统计与页码来自后端
// ============================================================

describe('统计来自后端', () => {
  it('⚠️ 卡片与总数用后端给的数字，不是"这一页的行数"', async () => {
    stubApi({
      '/api/tasks/summary': () =>
        json(summaryBody({ total: 45, by_status: { pending: 12, parsing: 0, reviewing: 3, blocked: 2, done: 28 }, writeback_failed: 1 })),
      // 本页只有 1 行，但全量是 45 —— 前端数行会得到 1
      '/api/tasks': () =>
        json(page([taskRow()], { total: 45, page: 1, page_count: 3, has_next: true })),
    })

    renderApp('/tasks')

    const cards = await screen.findByLabelText('任务汇总')
    expect(within(cards).getByText('12')).toBeInTheDocument() // 待处理
    expect(within(cards).getByText('3')).toBeInTheDocument() // 审查中（parsing+reviewing）
    expect(within(cards).getByText('28')).toBeInTheDocument() // 已完成
    // 总数来自后端（本页只有 1 行 —— 前端数行会得到 1）
    expect(within(cards).getByText('45')).toBeInTheDocument()

    expect(screen.getByText(/第 1 页 \/ 共 3 页 · 共 45 条/)).toBeInTheDocument()
  })

  it('翻页只换页号，并把页号写进 URL', async () => {
    const user = userEvent.setup()
    const stubbed = stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () =>
        json(page([taskRow()], { total: 45, page: 1, page_count: 3, has_next: true })),
    })

    renderApp('/tasks')

    await user.click(await screen.findByRole('button', { name: '下一页' }))

    await waitFor(() => {
      expect(stubbed.calls.some((url) => url.includes('page=2'))).toBe(true)
    })
  })

  it('第一页禁用"上一页"，最后一页禁用"下一页"', async () => {
    stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () => json(page([taskRow()], { total: 1 })),
    })

    renderApp('/tasks')

    expect(await screen.findByRole('button', { name: '上一页' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()
  })
})

// ============================================================
// 3. 行内容：状态、原因、风险、回写
// ============================================================

describe('行内容', () => {
  it('阻塞行**内联**给出三件套（卡在哪一步 / 错误码 / 原因）', async () => {
    stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () =>
        json(
          page([
            taskRow({
              task_status: 'blocked',
              blocked_stage: 'download',
              last_error_code: 'ATTACHMENT_MISSING',
              block_reason: '附件已被删除',
            }),
          ]),
        ),
    })

    renderApp('/tasks')

    const row = (await screen.findByText('HT-2026-0001')).closest('tr')
    expect(row).not.toBeNull()
    expect(row).toHaveTextContent('阻塞')
    expect(row).toHaveTextContent('阻塞于「下载附件」')
    expect(row).toHaveTextContent('ATTACHMENT_MISSING')
    expect(row).toHaveTextContent('附件已被删除')
  })

  it('未回写 + 被门禁拒绝：状态与**原因**分两处呈现', async () => {
    stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () =>
        json(
          page([
            taskRow({
              task_status: 'reviewing',
              write_status: 'not_written',
              overall_risk_level: 'high',
              writeback: {
                task_write_status: 'not_written',
                latest_attempt_id: 9,
                latest_attempt_no: 2,
                latest_attempt_status: 'not_written',
                latest_reason_code: 'MANUAL_CONFIRM_REQUIRED',
                latest_reason_text: '高风险结果尚未人工确认',
                latest_attempt_at: '2026-09-15T10:00:00',
                latest_attempt_rejected: true,
                status_url: '/api/writebacks/9',
              },
            }),
          ]),
        ),
    })

    renderApp('/tasks')

    const row = (await screen.findByText('HT-2026-0001')).closest('tr')
    // 状态（走到哪一步）
    expect(row).toHaveTextContent('未回写')
    // 原因（为什么没成功）—— 只给状态会让用户去重试一个注定被拒的请求
    expect(row).toHaveTextContent('第 2 次：高风险结果需人工确认后才能回写')
  })

  it('写失败 + 外部故障：原因同样呈现', async () => {
    stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () =>
        json(
          page([
            taskRow({
              task_status: 'blocked',
              write_status: 'failed',
              writeback: {
                task_write_status: 'failed',
                latest_attempt_id: 11,
                latest_attempt_no: 1,
                latest_attempt_status: 'failed',
                latest_reason_code: 'APPROVAL_API_TIMEOUT',
                latest_reason_text: '调用超时',
                latest_attempt_at: '2026-09-15T10:00:00',
                latest_attempt_rejected: false,
                status_url: '/api/writebacks/11',
              },
            }),
          ]),
        ),
    })

    renderApp('/tasks')

    const row = (await screen.findByText('HT-2026-0001')).closest('tr')
    expect(row).toHaveTextContent('写失败')
    expect(row).toHaveTextContent('调用审批系统超时')
  })

  it('⚠️ 没有审查结果时风险显示 `—`，不是"低"', async () => {
    stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () => json(page([taskRow({ overall_risk_level: null, task_status: 'reviewing', write_status: 'not_written' })])),
    })

    renderApp('/tasks')

    const row = (await screen.findByText('HT-2026-0001')).closest('tr')
    expect(row).not.toBeNull()
    // 无障碍名里说明是"尚未审查"，而不是让读屏用户听到一个没有含义的横线
    expect(within(row as HTMLElement).getByLabelText('总风险：尚未审查出结果')).toHaveTextContent('—')
  })

  it('日期按后端给的本地时间串渲染（不经过 Date 的时区解释）', async () => {
    stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () => json(page([taskRow({ created_at: '2026-09-15T09:30:45' })])),
    })

    renderApp('/tasks')

    const row = (await screen.findByText('HT-2026-0001')).closest('tr')
    expect(row).toHaveTextContent('2026-09-15 09:30')
  })
})

// ============================================================
// 4. 筛选与 URL
// ============================================================

describe('筛选与 URL', () => {
  it('选状态后请求带上白名单取值，并回到第 1 页', async () => {
    const user = userEvent.setup()
    const stubbed = stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () => json(page([taskRow()], { total: 45, page: 2, page_count: 3, has_next: false })),
    })

    renderApp('/tasks?page=2')

    await user.selectOptions(await screen.findByLabelText('状态'), '阻塞')

    await waitFor(() => {
      expect(
        stubbed.calls.some(
          (url) => url.includes('task_status=blocked') && url.includes('page=1'),
        ),
      ).toBe(true)
    })
  })

  it('⚠️ URL 里的非法取值被丢弃，且**业务数据不会被转发**', async () => {
    const stubbed = stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () => json(page([taskRow()])),
    })

    // 手工构造一个"被改过"的链接：非法状态、负页码、以及一段本不该出现的业务数据
    renderApp('/tasks?status=bogus&page=-3&form_data=SECRET-CONTRACT-TEXT')

    await waitFor(() => {
      expect(stubbed.calls.some((url) => url.startsWith('/api/tasks'))).toBe(true)
    })

    for (const url of stubbed.calls) {
      expect(url).not.toContain('SECRET-CONTRACT-TEXT')
      expect(url).not.toContain('bogus')
      expect(url).not.toMatch(/page=-\d/)
    }
  })

  it('`parseListParams` 只认白名单与正整数页码', () => {
    expect(parseListParams(new URLSearchParams('status=blocked&page=3'))).toEqual({
      page: 3,
      status: 'blocked',
    })
    // 非法取值一律回落到默认：链接可能来自旧版本或被手工改过，
    // 而用户想看的仍然是列表
    expect(parseListParams(new URLSearchParams('status=BLOCKED'))).toEqual({
      page: 1,
      status: null,
    })
    expect(parseListParams(new URLSearchParams('page=0'))).toEqual({ page: 1, status: null })
    expect(parseListParams(new URLSearchParams('page=abc'))).toEqual({ page: 1, status: null })
    expect(parseListParams(new URLSearchParams(''))).toEqual({ page: 1, status: null })
  })
})

// ============================================================
// 5. 键盘
// ============================================================

describe('键盘可达', () => {
  it('上下箭头在行之间移动焦点，到头停住', async () => {
    const user = userEvent.setup()
    stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () =>
        json(
          page([
            taskRow({ task_id: 1, approval_code: 'HT-1', instance_id: 'HT-1' }),
            taskRow({ task_id: 2, approval_code: 'HT-2', instance_id: 'HT-2' }),
          ]),
        ),
    })

    renderApp('/tasks')

    const first = await screen.findByRole('link', { name: 'HT-1' })
    const second = screen.getByRole('link', { name: 'HT-2' })

    first.focus()
    expect(first).toHaveFocus()

    await user.keyboard('{ArrowDown}')
    expect(second).toHaveFocus()

    // 到底就停住：跳到页面别处会让"在表里移动"失去可预测性
    await user.keyboard('{ArrowDown}')
    expect(second).toHaveFocus()

    await user.keyboard('{ArrowUp}')
    expect(first).toHaveFocus()
  })

  it('编号是链接，能直接 Tab 到', async () => {
    stubApi({
      '/api/tasks/summary': () => json(summaryBody()),
      '/api/tasks': () => json(page([taskRow()])),
    })

    renderApp('/tasks')

    expect(await screen.findByRole('link', { name: 'HT-2026-0001' })).toHaveAttribute(
      'href',
      '/tasks/1',
    )
  })
})

// ============================================================
// 时间格式化（纯函数）
// ============================================================

describe('时间格式化', () => {
  it('按字符串切分，不做时区转换', () => {
    // ⚠️ 后端给的是**不带时区**的本地时间；走 `new Date()` 会引入
    // 引擎相关的解释差异（有的按本地、有的按 UTC），
    // 于是同一份数据在 CI 与开发机上显示不同的时间
    expect(formatLocalTime('2026-09-15T09:30:45')).toBe('2026-09-15 09:30')
    expect(formatLocalTime('2026-09-15 09:30:00')).toBe('2026-09-15 09:30')
    expect(formatLocalTime(null)).toBe('—')
    expect(formatLocalTime('')).toBe('—')
  })
})
