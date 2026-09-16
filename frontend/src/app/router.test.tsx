import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'

import { json, stubApi } from '../test/stubApi'
import { renderApp } from '../test/renderApp'
import { ErrorBoundary } from './ErrorBoundary'
import {
  WORKBENCH_TABS,
  WORKBENCH_TAB_LABELS,
  evidenceDeepLink,
  parseWorkbenchTab,
  readEvidenceDeepLink,
  type WorkbenchTab,
} from './routes'

/**
 * 路由与外壳测试（M8 Task 1）。
 *
 * 本文件守的是**信息架构**这一层：五个模块能否各自到达、视角切换是否
 * 留在同一个路由里、未匹配地址与无权限页是否兜得住。
 *
 * ⚠️ 这些断言看起来"只是路由"，但材料 §3 的每条结构决定都有后果：
 * `?tab=` 而不是嵌套路由（后退行为）、非法 tab 回落而不是白屏
 * （链接过期时不该阻塞流程）、通配路由必须最后（放前面会吃掉所有路径
 * 且"照样能跑"）。
 */

/**
 * 每个视角应出现的模块标题。
 *
 * ⚠️ `detail` 是 `null`：模块 2 已实现，它的内容来自接口
 * （本文件不搭桩，因此不能断言内容）。路由测试在这里只断言
 * **"这个视角被到达了"** —— 那是本文件的职责；内容由
 * `features/detail/DetailTab.test.tsx` 覆盖。
 * 后续模块实现时同样把对应项改成 `null`。
 */
const TAB_MODULE_TITLES: Readonly<Record<WorkbenchTab, string | null>> = {
  detail: null,
  parse: null,
  rules: null,
  result: null,
}

/**
 * 证据深链（M8 Task 6）：模块 4/5 的「查看原文」→ 模块 3 的某一页某一处。
 *
 * ⚠️ URL 里**只放定位符**（页码、块号）——原文片段不进 URL（材料 §Global Constraints）。
 * 这一对函数（构造 / 解析）放在路由模块里，是为了让"链接会长什么样"
 * 只有一个答案：两处各写一遍时，反查那一侧迟早漏掉一个参数，
 * 而症状是"点了查看原文，跳过去了但停在第 1 页"。
 */
describe('证据深链', () => {
  it('构造：带上页码与块号', () => {
    expect(evidenceDeepLink(7, { page: 2, blockId: 'p2-b1' })).toBe(
      '/tasks/7?tab=parse&page=2&block=p2-b1',
    )
  })

  it('构造：没有块号时不带 `block`（而不是带一个空串）', () => {
    expect(evidenceDeepLink(7, { page: 3, blockId: null })).toBe('/tasks/7?tab=parse&page=3')
    expect(evidenceDeepLink(7, { page: 3, blockId: '' })).toBe('/tasks/7?tab=parse&page=3')
  })

  it('解析：往返一致', () => {
    const link = evidenceDeepLink(12, { page: 5, blockId: 'p5-b9' })
    const search = new URLSearchParams(link.slice(link.indexOf('?') + 1))

    expect(readEvidenceDeepLink(search)).toEqual({ page: 5, blockId: 'p5-b9' })
  })

  it('解析：非法值一律当**没有**（链接可能被改过或来自旧版本）', () => {
    const read = (query: string): ReturnType<typeof readEvidenceDeepLink> =>
      readEvidenceDeepLink(new URLSearchParams(query))

    expect(read('')).toEqual({ page: null, blockId: null })
    expect(read('page=0')).toEqual({ page: null, blockId: null })
    expect(read('page=-3')).toEqual({ page: null, blockId: null })
    expect(read('page=abc')).toEqual({ page: null, blockId: null })
    expect(read('page=2&block=')).toEqual({ page: 2, blockId: null })
  })
})

describe('应用外壳', () => {
  it('把根路径重定向到待办列表', () => {
    renderApp('/')

    // 标题取自设计 §4.1 的模块名（"待办调用"）—— 它守的是"重定向真的发生了"，
    // 因此这里断言的是**模块标题**而不是某个正在加载的列表内容
    expect(
      screen.getByRole('heading', { name: '待办调用' }),
    ).toBeInTheDocument()
  })

  it('渲染主导航，且每个入口都可点击', async () => {
    // ⚠️ 规则管理页必须**先知道身份**才能决定渲染什么（整个模块锁在
    // `rule:manage` 上）。本文件其余用例不搭接口，但这一条必须给身份 ——
    // 否则页面永远停在"正在加载"，而那不是导航的错
    sessionStorage.setItem(
      'contract-approval.dev-identity',
      JSON.stringify({ actorId: 'admin-1', displayName: '', roles: ['system_admin'] }),
    )
    stubApi({
      '/api/me': () =>
        json({
          actor_id: 'admin-1',
          display_name: 'Admin',
          tenant_id: 'default',
          roles: ['system_admin'],
          unknown_roles: [],
          permissions: ['task:read', 'rule:manage', 'audit:read', 'ops:retry'],
        }),
      // 权限就绪后规则页会拉列表；不搭桩的话它 404 → 页面渲染**错误态**（无标题）
      '/api/rules': () =>
        json({ items: [], total: 0, page: 1, page_size: 100, page_count: 1, has_next: false }),
    })

    const user = userEvent.setup()
    renderApp('/tasks')

    const nav = screen.getByRole('navigation', { name: '主导航' })
    await user.click(within(nav).getByRole('link', { name: '规则管理' }))

    // ⚠️ 用可等待断言：页面在权限就绪前先渲染加载态，
    // 同步断言会撞上那一瞬（而它不是页面缺标题）
    expect(
      await screen.findByRole('heading', { name: '规则管理' }),
    ).toBeInTheDocument()

    sessionStorage.clear()
  })
})

describe('任务工作台（模块 2–5）', () => {
  it.each(WORKBENCH_TABS)('%s 视角可以独立到达', (tab) => {
    renderApp(`/tasks/HT-2026-0001?tab=${tab}`)

    // 容器仍在（说明没有跳出去），且选中的正是该视角。
    // 页头不搭 API → 中性标题（页头不再展示内部编号"任务 #HT-2026-0001"）
    expect(screen.getByRole('heading', { name: '合同审查详情' })).toBeInTheDocument()
    const tabButton = screen.getByRole('tab', { name: WORKBENCH_TAB_LABELS[tab] })
    expect(tabButton).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel')).toBeInTheDocument()

    const title = TAB_MODULE_TITLES[tab]
    if (typeof title === 'string') {
      expect(screen.getByRole('heading', { name: title })).toBeInTheDocument()
    }
  })

  it('不带 tab 时默认展示详情视角', () => {
    renderApp('/tasks/HT-2026-0001')

    expect(
      screen.getByRole('tab', { name: WORKBENCH_TAB_LABELS.detail }),
    ).toHaveAttribute('aria-selected', 'true')
  })

  it('非法 tab 回落到详情视角，而不是白屏', () => {
    // 链接可能来自旧版本、被手工改过、或粘贴时被截断。
    // 报一个错误页会把"链接问题"升级成"流程阻塞"，而用户想看的仍是这份合同。
    renderApp('/tasks/HT-2026-0001?tab=whatever')

    expect(
      screen.getByRole('tab', { name: WORKBENCH_TAB_LABELS.detail }),
    ).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tabpanel')).toBeInTheDocument()
  })

  it('切换视角用查询串，不新增路径段（因此不产生新的历史记录）', async () => {
    const user = userEvent.setup()
    renderApp('/tasks/HT-2026-0001')

    const tablist = screen.getByRole('tablist', { name: '任务视角' })
    const rulesTab = within(tablist).getByRole('tab', { name: '规则命中' })

    // ① 目标地址是 `?tab=` 形态 —— 嵌套路由会让"后退"退到上一个视角
    expect(rulesTab).toHaveAttribute('href', '/tasks/HT-2026-0001?tab=rules')

    // ② 点完之后仍在工作台容器里（没有被路由带走），且**该视角被选中**
    await user.click(rulesTab)
    expect(screen.getByRole('heading', { name: '合同审查详情' })).toBeInTheDocument()
    // 模块 4 已实现（内容来自接口，本文件不搭桩）：
    // 因此这里断言"这个视角被选中了"，而不是它的内容
    expect(screen.getByRole('tab', { name: '规则命中' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
    expect(screen.getByRole('tabpanel')).toBeInTheDocument()
  })

  it('视角取值白名单只认四个', () => {
    expect(parseWorkbenchTab('rules')).toBe('rules')
    expect(parseWorkbenchTab('')).toBe('detail')
    expect(parseWorkbenchTab(null)).toBe('detail')
    expect(parseWorkbenchTab('DETAIL')).toBe('detail')
  })
})

describe('兜底页面', () => {
  it('无权限页给出"该找谁"，而不是只说没有权限', () => {
    renderApp('/forbidden')

    const page = screen.getByRole('heading', { name: '没有访问权限' }).closest('section')
    expect(page).not.toBeNull()
    expect(page).toHaveTextContent('系统管理员')
    // 与"内容不存在"必须区分：换账号登录是有效动作
    expect(page).toHaveTextContent('换一个账号')
  })

  it('未匹配的地址渲染 404 页，而不是空白', () => {
    renderApp('/nope/deep/link')

    expect(screen.getByRole('heading', { name: '页面不存在' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '返回待办列表' })).toBeInTheDocument()
  })
})

describe('错误边界', () => {
  it('兜住渲染期异常，且**不把异常正文写进 console**', () => {
    // 模拟"响应体被塞进 Error"——后端错误消息里会带业务上下文
    // （哪份合同、哪个附件），它不该出现在浏览器日志里。
    const confidential = 'CONFIDENTIAL-CONTRACT-BODY'
    function Boom(): JSX.Element {
      throw new Error(confidential)
    }

    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      render(
        <ErrorBoundary>
          <Boom />
        </ErrorBoundary>,
      )

      // 兜底界面必须出现（否则用户看到的是白屏）
      expect(
        screen.getByRole('heading', { name: '页面出现未预期的错误' }),
      ).toBeInTheDocument()

      // ⚠️ 只看**我们自己**输出的那几行：React 18 在开发模式也会打印原始异常
      // （生产构建不会），断言它等于在断言 React 的行为。
      const ours = consoleError.mock.calls
        .map((call) => String(call[0]))
        .filter((line) => line.startsWith('[ui]'))
      expect(ours.length).toBeGreaterThan(0)
      expect(ours.join('\n')).not.toContain(confidential)
    } finally {
      consoleError.mockRestore()
    }
  })
})
