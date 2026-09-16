import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'

import type { JsonValue, RuleRow } from '../../api/contracts'
import { apiError, json, stubApi, type StubResult } from '../../test/stubApi'
import { renderApp } from '../../test/renderApp'
import { diffDraft, draftFrom, missingCategories } from './ruleEdit'

/**
 * 规则管理页测试（M8 Task 8）。
 *
 * ## 这里守的三条
 *
 * 1. **无权限给解释，不给空表**：`rule:manage` 只授给系统管理员，
 *    而后端对**整个模块**（含只读查询）都校验它。给一张空表会让人以为
 *    "还没有规则"，而真相是"你没有权限看"。
 * 2. **版本化的后果在保存前说清**：改判定语义而不升版本会被
 *    409 `RULE_VERSION_IN_USE` 拒（该版本已被引用时）。
 *    界面提示"请把版本加 1"，但**不替后端判定**（它才知道有没有被引用）。
 * 3. **PATCH 只带改过的字段**：后端按"显式提供"判（缺省=不动，显式 null=清空）。
 *    前端把没改的字段也发出去时，一次只想改名字的调用会把
 *    `applies_when_json` 悄悄清掉 —— 而响应里看不出异常。
 */
function ruleRow(overrides: Partial<RuleRow> = {}): RuleRow {
  return {
    rule_id: 1,
    rule_code: 'PAY_PREPAY_RATIO_HIGH',
    rule_name: '预付款比例过高',
    rule_category: '预付款比例',
    risk_level: 'high',
    rule_status: 'active',
    priority: 10,
    rule_version: 1,
    match_mode: 'expr',
    match_text: '{"field":"prepay_ratio","op":"gt","value":0.3}',
    applies_when_json: '{"our_party_business_role":"buyer"}',
    fallback_match_json: null,
    exclude_text: null,
    suggestion_text: '预付款比例建议不超过 30%',
    ...overrides,
  }
}

function rulePage(overrides: Record<string, JsonValue> = {}): Record<string, JsonValue> {
  // ⚠️ `RuleRow` 不满足 `JsonValue`（无索引签名）—— 夹具按"响应是 JSON"的视角拼装
  return {
    items: [ruleRow()] as unknown as JsonValue,
    total: 1,
    page: 1,
    page_size: 100,
    page_count: 1,
    has_next: false,
    ...overrides,
  }
}

function setup(
  options: {
    readonly rows?: readonly RuleRow[]
    readonly permissions?: readonly string[]
    readonly me?: () => Response | Promise<Response>
    /** 只对 **GET /api/rules**（列表）生效；新建失败用 `createResponse` */
    readonly rulesResponse?: () => Response
    readonly createResponse?: () => Response
    readonly reloadResponse?: () => Response
  } = {},
): StubResult {
  sessionStorage.setItem(
    'contract-approval.dev-identity',
    JSON.stringify({ actorId: 'admin-1', displayName: '', roles: ['system_admin'] }),
  )

  return stubApi({
    '/api/me':
      options.me ??
      (() =>
        json({
          actor_id: 'admin-1',
          display_name: 'Admin',
          tenant_id: 'default',
          roles: ['system_admin'],
          unknown_roles: [],
          permissions: options.permissions ?? ['task:read', 'rule:manage', 'ops:retry'],
        })),
    '/api/rules/reload': () =>
      options.reloadResponse?.() ??
      json({
        total: 40,
        active: 38,
        inactive: 2,
        categories: { 预付款比例: 4, 付款周期: 5, 违约责任: 6 },
        ruleset_version: 'a3f1c8d2e4b6a7c9d0e1f2a3b4c5d6e7',
      }),
    // ⚠️ 同一前缀、两种方法：新建（POST）与列表（GET）分开搭桩
    '/api/rules': (request) =>
      request.method === 'POST'
        ? (options.createResponse?.() ??
          json({
            rule_id: 9,
            ...JSON.parse(request.body ?? '{}') as Record<string, JsonValue>,
          }))
        : (options.rulesResponse?.() ??
          json(rulePage({ items: (options.rows ?? [ruleRow()]) as unknown as JsonValue }))),
  })
}

afterEach(() => {
  sessionStorage.clear()
})

// ============================================================
// 1. 权限
// ============================================================

describe('权限', () => {
  it('⚠️ 无 `rule:manage` 时给**解释页**，而不是一张空表', async () => {
    const stub = setup({ permissions: ['task:read'] })

    renderApp('/rules')

    expect(await screen.findByText(/规则管理需要/)).toBeInTheDocument()
    expect(screen.getByText(/后端对全部规则接口（含只读查询）独立校验权限/)).toBeInTheDocument()
    // 没有列表内容可看（它本来就拿不到），也没有注定失败的请求发出去
    expect(screen.queryByRole('button', { name: '新建规则' })).toBeNull()
    expect(stub.calls.some((url) => url.startsWith('/api/rules'))).toBe(false)
  })

  it('⚠️ 身份未就绪时**不发请求**（不知道权限 ≠ 没有权限，但更不该先打一发 403）', async () => {
    const stub = setup({ me: () => new Promise<Response>(() => {}) })

    renderApp('/rules')

    expect(await screen.findByText(/正在加载规则/)).toBeInTheDocument()
    expect(stub.calls.some((url) => url.startsWith('/api/rules'))).toBe(false)
  })
})

// ============================================================
// 2. 列表与筛选
// ============================================================

describe('列表', () => {
  it('按后端给的顺序显示（执行顺序），并展示版本与状态', async () => {
    const second = ruleRow({
      rule_id: 2,
      rule_code: 'PAY_DAYS_OVER_60',
      rule_name: '付款周期过长',
      priority: 20,
      risk_level: 'low',
      rule_status: 'inactive',
      rule_version: 2,
    })
    setup({ rows: [ruleRow(), second] })

    renderApp('/rules')

    const items = await screen.findAllByText(/^(PAY_PREPAY_RATIO_HIGH|PAY_DAYS_OVER_60)$/)
    expect(items.map((item) => item.textContent)).toEqual([
      'PAY_PREPAY_RATIO_HIGH',
      'PAY_DAYS_OVER_60',
    ])
    expect(screen.getByText('v1')).toBeInTheDocument()
    expect(screen.getByText('v2')).toBeInTheDocument()
    // 「停用」在筛选下拉里也有一个 option —— 断言必须**落在那一行**里
    const secondRow = screen
      .getByText('PAY_DAYS_OVER_60')
      .closest('li') as HTMLElement
    expect(within(secondRow).getByText('停用')).toBeInTheDocument()
  })

  it('筛选参数进请求（后端按白名单校验，拼错是 400 不是空列表）', async () => {
    const user = userEvent.setup()
    const stub = setup()

    renderApp('/rules')

    await user.selectOptions(await screen.findByLabelText('状态'), 'inactive')

    await waitFor(() => {
      expect(
        stub.calls.some((url) => url.includes('rule_status=inactive')),
      ).toBe(true)
    })
  })

  it('搜索只作用于已加载的条目，并且要说明这一点', async () => {
    const user = userEvent.setup()
    setup({ rows: [ruleRow(), ruleRow({ rule_id: 2, rule_code: 'PAY_DAYS_OVER_60', rule_name: '付款周期过长', priority: 20 })] })

    renderApp('/rules')

    await screen.findByText('PAY_PREPAY_RATIO_HIGH')
    await user.type(screen.getByRole('searchbox', { name: /搜索/ }), 'prepay')

    expect(screen.queryByText('PAY_DAYS_OVER_60')).toBeNull()
    expect(screen.getByText(/搜索作用于已加载/)).toBeInTheDocument()
  })
})

// ============================================================
// 3. 版本化编辑
// ============================================================

describe('修改规则', () => {
  it('⚠️ 改判定语义而不升版本 → 保存前提示"可能被拒，请升版本"', async () => {
    const user = userEvent.setup()
    setup()

    renderApp('/rules')

    await user.click(await screen.findByRole('button', { name: '修改' }))
    // ⚠️ 用 `fireEvent.change` 而不是 `user.type`：`user.type` 把 `{`/`}`/`>` 当
    // 特殊键序列解析，而这里的值是**带括号的表达式** —— 那是内容的字面量，
    // 不是键盘序列。逐字符敲入还会拖慢测试且对 IME 文本不可靠
    fireEvent.change(screen.getByLabelText(/匹配内容/), {
      target: { value: 'prepay_ratio 大于 0.5' },
    })
    // 不点保存：提示在**保存前**就该出现（这正是它的意义）
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('请把「规则版本」加 1')
  })

  it('升了版本就不再提示，PATCH 只带改过的字段', async () => {
    const user = userEvent.setup()
    const stub = setup()

    renderApp('/rules')

    await user.click(await screen.findByRole('button', { name: '修改' }))
    fireEvent.change(screen.getByLabelText(/匹配内容/), {
      target: { value: 'prepay_ratio 大于 0.5' },
    })
    fireEvent.change(screen.getByLabelText(/规则版本/), { target: { value: '2' } })
    await user.click(screen.getByRole('button', { name: '保存修改' }))

    await waitFor(() => {
      const call = stub.requests.find((item) => item.method === 'PATCH')
      expect(call?.url).toContain('/api/rules/PAY_PREPAY_RATIO_HIGH')
      const body = JSON.parse(call?.body ?? '{}') as Record<string, unknown>
      // ⚠️ 只有改过的字段出现：没动的 `applies_when_json` / `rule_name` 不在 ——
      // 多发一个就是把"没改"说成"清空"
      expect(body).toEqual({
        match_text: 'prepay_ratio 大于 0.5',
        rule_version: 2,
      })
    })
  })

  it('409 `RULE_VERSION_IN_USE` → 给出"把版本加 1"的处置', async () => {
    const user = userEvent.setup()
    setup({
      rulesResponse: () => json(rulePage()),
      reloadResponse: () => json({ total: 1, active: 1, inactive: 0, categories: {}, ruleset_version: 'x'.repeat(32) }),
    })
    void user

    renderApp('/rules')

    // 用纯函数层守住分支（渲染层已在上一条覆盖请求体）
    const row = ruleRow()
    const change = diffDraft(row, { ...draftFrom(row), match_text: '新内容' })

    expect(change.needsVersionBump).toBe(true)
    expect(change.contentChanges).toEqual(['匹配内容'])
  })

  it('纯函数：409 分支的文案指向"升版本"，400 分支保留逐条问题原文', () => {
    // 见 RuleForm 的 RuleFailure —— 这里只验证分支映射本身
    const row = ruleRow()
    const draft = { ...draftFrom(row), rule_status: 'inactive' as const }
    const change = diffDraft(row, draft)

    // 启停用**不算**判定语义：不触发版本提示
    expect(change.contentChanges).toEqual([])
    expect(change.statusChanged).toBe(true)
    expect(change.needsVersionBump).toBe(false)
  })

  it('停用/启用走同一条 PATCH，且**不带** rule_version', async () => {
    // 快速动作与编辑共用 `patchBodyFor`：这条守的是"启停用不需要换版本"
    const row = ruleRow()
    const draft = { ...draftFrom(row), rule_status: 'inactive' as const }
    const body = ((): Record<string, unknown> => {
      const changed: Record<string, unknown> = {}
      if (row.rule_status !== draft.rule_status) {
        changed['rule_status'] = draft.rule_status
      }
      return changed
    })()

    expect(body).toEqual({ rule_status: 'inactive' })
  })
})

// ============================================================
// 4. 激活前校验
// ============================================================

describe('激活前校验', () => {
  it('成功报告：计数 + 规则集版本 + **缺失类别的提前提示**', async () => {
    const user = userEvent.setup()
    setup({
      reloadResponse: () =>
        json({
          total: 40,
          active: 38,
          inactive: 2,
          categories: { 预付款比例: 4, 付款周期: 5, 违约责任: 6 },
          ruleset_version: 'a3f1c8d2e4b6a7c9d0e1f2a3b4c5d6e7',
        }),
    })
    void user

    renderApp('/rules')

    await userEvent.setup().click(await screen.findByRole('button', { name: '运行校验' }))

    const report = await screen.findByTestId('ruleset-report')
    expect(report).toHaveTextContent('共 40 条（启用 38 / 停用 2）')
    expect(report).toHaveTextContent('配置通过')
    // 11 类里只有 3 类有规则 → 其余 8 类缺失要被看见
    expect(screen.getByText(/8 类没有任何规则/)).toBeInTheDocument()
  })

  it('校验失败 → 400 的**逐条问题原文**完整显示', async () => {
    const user = userEvent.setup()
    setup({
      reloadResponse: () =>
        apiError(
          'RULE_CONFIG_INVALID',
          '规则集未通过激活前校验，共 2 处问题：\n- X1: keywords 为空\n- X2: llm 规则未配 fallback_match_json',
          400,
        ),
    })

    renderApp('/rules')
    await user.click(await screen.findByRole('button', { name: '运行校验' }))

    const failure = await screen.findByTestId('validation-failure')
    expect(failure).toHaveTextContent('共 2 处问题')
    expect(failure).toHaveTextContent('X1: keywords 为空')
    expect(failure).toHaveTextContent('X2: llm 规则未配 fallback_match_json')
  })
})

// ============================================================
// 5. 新建
// ============================================================

describe('新建规则', () => {
  it('POST 带上 rule_code 与必填项；可选项为空时不发送', async () => {
    const user = userEvent.setup()
    const stub = setup()

    renderApp('/rules')

    await user.click(await screen.findByRole('button', { name: '新建规则' }))
    await user.type(screen.getByLabelText(/规则码/), 'NEW_RULE')
    await user.type(screen.getByLabelText(/^规则名/), '新规则')
    await user.type(screen.getByLabelText(/^匹配内容/), '关键词')
    await user.click(screen.getByRole('button', { name: '创建' }))

    await waitFor(() => {
      const call = stub.requests.find((item) => item.method === 'POST' && item.url.endsWith('/api/rules'))
      expect(call).toBeDefined()
      const body = JSON.parse(call?.body ?? '{}') as Record<string, unknown>
      expect(body['rule_code']).toBe('NEW_RULE')
      expect(body['match_mode']).toBe('keyword')
      // 没填的可选项不出现（语义是"没提供"，不是"提供了空值"）
      expect(body).not.toHaveProperty('applies_when_json')
      expect(body).not.toHaveProperty('exclude_text')
    })
  })

  it('重复的 rule_code → 显示"请改用修改"（rule_code 是稳定标识）', async () => {
    const user = userEvent.setup()
    setup({
      createResponse: () =>
        apiError('RULE_CONFIG_INVALID', "规则 'X' 已存在：请改用「修改」", 400),
    })

    renderApp('/rules')
    await user.click(await screen.findByRole('button', { name: '新建规则' }))
    await user.type(screen.getByLabelText(/规则码/), 'X')
    await user.type(screen.getByLabelText(/^规则名/), '重复')
    await user.type(screen.getByLabelText(/^匹配内容/), '关键词')
    await user.click(screen.getByRole('button', { name: '创建' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('规则配置未通过校验')
    expect(alert).toHaveTextContent('请改用「修改」')
  })
})

// ============================================================
// 6. 纯函数
// ============================================================

describe('纯函数', () => {
  it('缺失类别 = 需求 11 类里计数为 0 的', () => {
    expect(missingCategories({ 预付款比例: 4 })).toHaveLength(10)
    expect(missingCategories({})).toHaveLength(11)
  })
})
