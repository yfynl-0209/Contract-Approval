import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'

import type { JsonValue } from '../../api/contracts'
import { json, stubApi, type StubResult } from '../../test/stubApi'
import { renderApp } from '../../test/renderApp'

/**
 * 模块 4（规则命中）测试（M8 Task 6）。
 *
 * ## 这里守着的两条最要紧的事
 *
 * 1. **`needs_review` 不等于"风险"**：它带 `risk_level`（规则配置的等级），
 *    但含义是"判不了"。给它配一个红色「高」会把**待判断**读成**已确认的高风险**。
 *    测试因此逐卡片断言"这个框里没有风险徽章"。
 * 2. **计数来自后端聚合**：用一个"聚合说有 31 条不适用、而条目只给 1 条"的
 *    故意不一致的夹具，断言页头显示 **31** —— 前端自己数数组时这条会失败。
 *    （真实数据里两者相等，因此这个缺陷在数据量小时**永远看不出来**。）
 */
function position(
  page: number,
  blockId: string,
): Record<string, JsonValue> {
  return {
    page,
    block_id: blockId,
    bbox: [100, 200, 300, 250],
    char_start: 0,
    char_end: 13,
    text_precision: 'char',
    bbox_precision: 'char',
  }
}

function evaluation(
  ruleCode: string,
  status: string,
  risk: string,
  extra: Record<string, JsonValue> = {},
): Record<string, JsonValue> {
  return {
    rule_code: ruleCode,
    evaluation_status: status,
    risk_level: risk,
    reason_code: null,
    reason_text: null,
    evidence: [],
    hit_detail: null,
    ...extra,
  }
}

function runBody(overrides: Record<string, JsonValue> = {}): Record<string, JsonValue> {
  return {
    run_id: 21,
    task_id: 7,
    parse_id: 11,
    version_no: 3,
    run_status: 'completed',
    ruleset_version: 'rs-3f9a',
    model_version: 'm1',
    prompt_version: 'p1',
    config_version: 'c1',
    aggregate: {
      overall_risk_level: 'high',
      review_status: 'needs_review',
      counts: { hit: 2, not_hit: 1, not_applicable: 1, needs_review: 1 },
      focus_points: [],
      summary: '共评价 5 条规则：命中 2 条…',
    },
    evaluations: [
      evaluation('PAY_PREPAY_RATIO_HIGH_FOR_BUYER', 'needs_review', 'high', {
        reason_code: 'CONTEXT_MISSING',
        reason_text: '我方立场未知，方向敏感的规则判不了',
      }),
      evaluation('PREPAY_RATIO_OVER_30', 'hit', 'high', {
        reason_code: 'CONDITION_MATCHED',
        hit_detail: { actual: '0.6', op: 'gt', threshold: '0.3' },
        evidence: [
          { text: '合同签订后 60 日内支付预付款', position: position(2, 'p2-b1') },
          { text: '预付款比例为 60%', position: position(2, 'p2-b2') },
        ],
      }),
      evaluation('IP_CLAUSE_ABSENT', 'hit', 'medium', {
        reason_code: 'CONDITION_MATCHED',
        hit_detail: { status: 'not_found', op: 'is_null' },
      }),
      evaluation('PAY_DAYS_OVER_60', 'not_hit', 'low', {
        reason_code: 'CONDITION_NOT_MATCHED',
        hit_detail: { actual: '30', op: 'gt', threshold: '60' },
      }),
      evaluation('AUTO_RENEW_ABSENT', 'not_applicable', 'low', {
        reason_code: 'CONDITION_NOT_APPLICABLE',
      }),
    ],
    started_at: '2026-09-15T10:00:00',
    finished_at: '2026-09-15T10:00:05',
    ...overrides,
  }
}

function setup(options: { readonly run?: Record<string, JsonValue>; readonly latestRunId?: number | null } = {}): StubResult {
  sessionStorage.setItem(
    'contract-approval.dev-identity',
    JSON.stringify({ actorId: 'reviewer-1', displayName: '', roles: ['legal_reviewer'] }),
  )
  const latestRunId = options.latestRunId === undefined ? 21 : options.latestRunId

  return stubApi({
    '/api/me': () =>
      json({
        actor_id: 'reviewer-1',
        display_name: 'Reviewer One',
        tenant_id: 'default',
        roles: ['legal_reviewer'],
        unknown_roles: [],
        permissions: ['task:read', 'result:confirm'],
      }),
    '/api/tasks/7': () =>
      json({
        task_id: 7,
        task_status: 'reviewing',
        context_status: 'complete',
        context_source: 'approval_system',
        our_party_name: '甲方公司',
        our_party_contract_label: 'party_a',
        our_party_business_role: 'buyer',
        contract_type: 'procurement',
        context_correction: null,
        raw_context_facts: null,
        context_conflict: null,
        blocked_stage: null,
        blocked_reason_code: null,
        last_error_code: null,
        is_business_blocked: false,
        overall_risk_level: null,
        latest_parse_id: 11,
        latest_run_id: latestRunId,
        current_result_id: null,
        attachment_count: 1,
        correlation_id: null,
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
      }),
    '/api/runs/21': () => json(options.run ?? runBody()),
  })
}

afterEach(() => {
  sessionStorage.clear()
})

describe('四态分组与排序', () => {
  it('⚠️ 四组都在，且 `needs_review` **排在 `hit` 之前**', async () => {
    setup()
    renderApp('/tasks/7?tab=rules')

    await screen.findByTestId('run-counts')

    const groups = screen.getAllByRole('group')
    expect(groups.map((element) => element.getAttribute('data-status'))).toEqual([
      'needs_review',
      'hit',
      'not_hit',
      'not_applicable',
    ])
  })

  it('前两组默认展开，后两组默认折叠但**显示计数**', async () => {
    setup()
    renderApp('/tasks/7?tab=rules')

    await screen.findByTestId('run-counts')
    const groups = screen.getAllByRole('group')
    const byStatus = (status: string): HTMLDetailsElement =>
      groups.find((element) => element.getAttribute('data-status') === status) as HTMLDetailsElement

    expect(byStatus('needs_review').open).toBe(true)
    expect(byStatus('hit').open).toBe(true)
    // 折叠不等于隐藏：计数必须看得见（"系统确实评估过这一类"）
    expect(byStatus('not_hit').open).toBe(false)
    expect(byStatus('not_hit').textContent).toContain('未命中（1）')
    expect(byStatus('not_applicable').open).toBe(false)
    expect(byStatus('not_applicable').textContent).toContain('不适用（1）')
  })

  it('组内按风险等级降序（高 → 中）', async () => {
    setup()
    renderApp('/tasks/7?tab=rules')

    const hitGroup = (await screen.findAllByRole('group')).find(
      (element) => element.getAttribute('data-status') === 'hit',
    )
    // 只取**卡片**（`data-rule-code`）：证据条目也是 `<li>`，
    // 按 `listitem` 全会把嵌套的证据行算进来
    const codes = Array.from(
      (hitGroup as HTMLElement).querySelectorAll('[data-rule-code]'),
    ).map((item) => item.getAttribute('data-rule-code'))
    expect(codes).toEqual(['PREPAY_RATIO_OVER_30', 'IP_CLAUSE_ABSENT'])
  })
})

describe('⚠️ 计数来自后端聚合（不数数组）', () => {
  it('聚合与条目数不一致时，页头显示**聚合的数**并给出警示', async () => {
    const run = runBody()
    // 故意构造不一致：聚合说 31 条不适用，而条目只给 1 条
    run['aggregate'] = {
      overall_risk_level: 'high',
      review_status: 'needs_review',
      counts: { hit: 2, not_hit: 1, not_applicable: 31, needs_review: 1 },
      focus_points: [],
      summary: '…',
    }
    setup({ run })

    renderApp('/tasks/7?tab=rules')

    const counts = await screen.findByTestId('run-counts')
    // 前端自己数数组时这里会是 1
    expect(counts).toHaveTextContent('不适用 31')
    expect(counts).toHaveTextContent('共 35 条规则')
    // 不一致本身要被看见，而不是悄悄显示两套数字
    expect(screen.getByTestId('count-mismatch')).toBeInTheDocument()
  })

  it('一致时不显示那条警示（避免变成常驻噪音）', async () => {
    setup()
    renderApp('/tasks/7?tab=rules')

    await screen.findByTestId('run-counts')
    expect(screen.queryByTestId('count-mismatch')).toBeNull()
  })
})

describe('⚠️ `needs_review` 不显示风险徽章', () => {
  it('待判断项给出**原因**而不是风险等级', async () => {
    setup()
    renderApp('/tasks/7?tab=rules')

    const card = (await screen.findByText('PAY_PREPAY_RATIO_HIGH_FOR_BUYER')).closest('li')
    expect(card).not.toBeNull()

    // 规则配置的等级是 high，但它是"判不了"，不能显示成"高风险"
    expect(within(card as HTMLElement).queryByText('高')).toBeNull()
    expect(within(card as HTMLElement).getByText('我方立场未知，方向敏感的规则判不了')).toBeInTheDocument()
    // 原因码翻译成**人话**显示，稳定编码收进悬停（决定下一步动作：去补立场）
    expect(within(card as HTMLElement).getByText('缺少立场上下文')).toBeInTheDocument()
    expect(within(card as HTMLElement).getByTitle('CONTEXT_MISSING')).toBeInTheDocument()
  })

  it('`hit` 才给风险徽章', async () => {
    setup()
    renderApp('/tasks/7?tab=rules')

    const card = (await screen.findByText('PREPAY_RATIO_OVER_30')).closest('li')
    expect(within(card as HTMLElement).getByText('高')).toBeInTheDocument()
  })
})

describe('计算过程与证据', () => {
  it('给出可复核的计算过程（op 映射成数学符号）', async () => {
    setup()
    renderApp('/tasks/7?tab=rules')

    expect(await screen.findByText('0.6 > 0.3')).toBeInTheDocument()
    // 未命中项的 `30 > 60` 也要能看到（它是"为什么没报警"）
    expect(screen.getByText('30 > 60')).toBeInTheDocument()
  })

  it('多处证据全部列出，且链接指向**模块 3 的具体位置**并保留任务上下文', async () => {
    setup()
    renderApp('/tasks/7?tab=rules')

    const links = await screen.findAllByRole('link', { name: /查看原文/ })
    const hrefs = links.map((link) => link.getAttribute('href'))
    expect(hrefs).toContain('/tasks/7?tab=parse&page=2&block=p2-b1')
    expect(hrefs).toContain('/tasks/7?tab=parse&page=2&block=p2-b2')
  })

  it('缺失类命中（没有原文片段）如实说明，且**不给**"查看原文"', async () => {
    setup()
    renderApp('/tasks/7?tab=rules')

    const card = (await screen.findByText('IP_CLAUSE_ABSENT')).closest('li')
    expect(within(card as HTMLElement).getByText(/没有原文片段/)).toBeInTheDocument()
    expect(within(card as HTMLElement).queryByRole('link', { name: /查看原文/ })).toBeNull()
  })

  it('证据没有坐标时不谎称可定位', async () => {
    const run = runBody()
    run['evaluations'] = [
      evaluation('KEYWORD_HIT', 'hit', 'medium', {
        evidence: [{ text: '乙方应在 30 日内交付', position: {} }],
      }),
    ]
    run['aggregate'] = {
      overall_risk_level: 'medium',
      review_status: 'complete',
      counts: { hit: 1, not_hit: 0, not_applicable: 0, needs_review: 0 },
      focus_points: [],
      summary: '…',
    }
    setup({ run })

    renderApp('/tasks/7?tab=rules')

    expect(await screen.findByText(/没有坐标，无法在 PDF 中定位/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /查看原文/ })).toBeNull()
  })
})

describe('批次状态', () => {
  it('⚠️ 批次未完成时警示"结论尚未完整"', async () => {
    setup({ run: runBody({ run_status: 'running' }) })

    renderApp('/tasks/7?tab=rules')

    expect(await screen.findByText(/还没跑完/)).toBeInTheDocument()
    expect(screen.getByText(/进行中（结论尚未完整）/)).toBeInTheDocument()
  })

  it('已完成时不显示那条警示', async () => {
    setup()
    renderApp('/tasks/7?tab=rules')

    await screen.findByTestId('run-counts')
    expect(screen.queryByText(/还没跑完/)).toBeNull()
  })
})

describe('筛选与空态', () => {
  it('「只看需处理」隐藏未命中与不适用', async () => {
    const user = userEvent.setup()
    setup()
    renderApp('/tasks/7?tab=rules')

    await screen.findByText('PAY_DAYS_OVER_60')
    await user.click(screen.getByRole('checkbox', { name: /只看需处理/ }))

    expect(screen.queryByText('PAY_DAYS_OVER_60')).toBeNull()
    expect(screen.queryByText('AUTO_RENEW_ABSENT')).toBeNull()
    expect(screen.getByText('PREPAY_RATIO_OVER_30')).toBeInTheDocument()
  })

  it('按规则码搜索', async () => {
    const user = userEvent.setup()
    setup()
    renderApp('/tasks/7?tab=rules')

    await screen.findByText('PAY_DAYS_OVER_60')
    await user.type(screen.getByRole('searchbox', { name: /规则码/ }), 'prepay')

    expect(screen.getByText('PREPAY_RATIO_OVER_30')).toBeInTheDocument()
    expect(screen.getByText('PAY_PREPAY_RATIO_HIGH_FOR_BUYER')).toBeInTheDocument()
    expect(screen.queryByText('PAY_DAYS_OVER_60')).toBeNull()
  })

  it('筛选后为空的组说"数据在，只是没显示"（而不是"不适用"）', async () => {
    const user = userEvent.setup()
    setup()
    renderApp('/tasks/7?tab=rules')

    await screen.findByText('PAY_DAYS_OVER_60')
    await user.click(screen.getByRole('checkbox', { name: /只看需处理/ }))

    // 未命中与不适用**各**有一个空组，两处都要说"数据在，只是没显示"
    expect(screen.getAllByText(/清掉筛选就能看到/)).toHaveLength(2)
  })

  it('没有任何批次时说明"控制台不触发批次"', async () => {
    setup({ latestRunId: null })

    renderApp('/tasks/7?tab=rules')

    expect(await screen.findByText(/还没有审查批次/)).toBeInTheDocument()
    expect(screen.getByText(/控制台不触发它/)).toBeInTheDocument()
  })

  it('批次没有任何评价时说"这不等于没有风险"', async () => {
    const run = runBody()
    run['evaluations'] = []
    run['aggregate'] = {
      overall_risk_level: 'low',
      review_status: 'complete',
      counts: { hit: 0, not_hit: 0, not_applicable: 0, needs_review: 0 },
      focus_points: [],
      summary: '本次没有任何规则被评价…',
    }
    setup({ run })

    renderApp('/tasks/7?tab=rules')

    // 只说**一遍**（四个空组各说一遍时，同一个问题会被读成四个）
    expect(await screen.findByTestId('no-evaluations')).toHaveTextContent('不等于「没有风险」')
    expect(screen.getAllByText(/不等于「没有风险」/)).toHaveLength(1)
  })
})
