import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { Suspense } from 'react'

import { routes } from './routes'
import { RouteLoading } from './RouteLoading'
import type { Permission } from '../api/contracts'
import { IdentityPanel } from './IdentityPanel'
import { useAuth } from './authContext'

/**
 * 应用外壳 —— **前端重设计**（对齐 `frontend-redesign/` 原型）：
 * 左侧深色导航（品牌 / 分组导航 / 待办角标 / 身份卡）+ 右侧亮色主区（顶栏 + 内容）。
 *
 * ## 导航为什么只是"提示"
 *
 * §5.3：导航里隐藏某一项**不是权限控制**。真正的判据在后端
 * （`require_permissions`），而这一层只决定"用户看到哪些入口"。
 * 因此这里不做"没权限就不渲染链接"的强制逻辑——用户手敲 URL 依然能到达页面，
 * 页面必须能正确处理 403。把"看不到入口"当成"访问不了"，正是任务 9 证伪过的假设。
 */

const NAV_ITEMS: ReadonlyArray<{
  readonly to: string
  readonly label: string
  /** 显示该入口所需的权限（**只用于显示**） */
  readonly permission: Permission
  readonly icon: JSX.Element
  readonly group: '工作台' | '系统'
}> = [
  {
    to: routes.tasks,
    label: '待办调用',
    permission: 'task:read',
    group: '工作台',
    icon: (
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M4 6h16M4 12h16M4 18h10" />
      </svg>
    ),
  },
  {
    to: routes.rules,
    label: '规则管理',
    permission: 'rule:manage',
    group: '系统',
    icon: (
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7l8-4z" />
      </svg>
    ),
  },
  {
    to: routes.ops,
    label: '运行管理',
    permission: 'ops:retry',
    group: '系统',
    icon: (
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
      </svg>
    ),
  },
]

/** 顶栏面包屑：当前路径对应的页面名（导航之外的第二处位置提示）。 */
function crumbFor(pathname: string): string {
  if (pathname.startsWith(routes.tasks) || pathname === '/') {
    return '审查工作台'
  }
  if (pathname.startsWith(routes.rules)) {
    return '系统'
  }
  if (pathname.startsWith(routes.ops)) {
    return '系统'
  }
  return '合同审批审查系统'
}

export function AppLayout(): JSX.Element {
  const { status, can } = useAuth()
  const location = useLocation()

  /**
   * 身份**未就绪时不过滤**入口：'还不知道'与'确定没有权限'是两件事。
   * 未就绪时把入口都留着，点进去由页面自己解释 401/403 ——
   * 那条路径同时是"前端无法绕过门禁"的证据：页面的可见性不构成授权。
   */
  const visibleItems =
    status === 'ready' ? NAV_ITEMS.filter((item) => can(item.permission)) : NAV_ITEMS

  const isWorkbench = /\/tasks\/\d+/.test(location.pathname)

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            审
          </div>
          <div>
            <div className="brand-name">合同审批审查系统</div>
            <div className="brand-sub">Contract Review Copilot</div>
          </div>
        </div>

        <nav className="nav" aria-label="主导航">
          <div className="nav-section">审查工作台</div>
          {visibleItems
            .filter((item) => item.group === '工作台')
            .map((item) => (
              <NavLink key={item.to} to={item.to} className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`} end={item.to === routes.tasks}>
                {item.icon}
                {item.label}
              </NavLink>
            ))}
          <div className="nav-section">系统</div>
          {visibleItems
            .filter((item) => item.group === '系统')
            .map((item) => (
              <NavLink key={item.to} to={item.to} className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>
                {item.icon}
                {item.label}
              </NavLink>
            ))}
        </nav>

        <div className="actor-card">
          <IdentityPanel />
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <span className="topbar-crumb">
            {crumbFor(location.pathname)}
            {isWorkbench ? <b> · 任务工作台</b> : null}
          </span>
          <div className="topbar-right">
            {/* ⚠️ 这句不是客套：本系统只生成审查意见，不代替人工作出审批决定 */}
            <span style={{ color: 'var(--text-3)', fontSize: 'var(--font-size-xs)' }}>
              仅生成审查意见，审批结论由人作出
            </span>
            <span className="badge b-violet">
              <span className="dot" aria-hidden="true" />
              local · Mock 审批系统
            </span>
          </div>
        </header>

        <main className="content">
          {/* 路由级加载态：懒加载页面首次进入时才有意义，
              同时兜住"某个页面忘了处理 loading"的情况 */}
          <Suspense fallback={<RouteLoading />}>
            <Outlet />
          </Suspense>
          <div className="footer-note">
            仅生成审查意见 · 所有统计口径以后端聚合为准 · 合同正文与敏感值不写入 URL / console
          </div>
        </main>
      </div>
    </div>
  )
}
