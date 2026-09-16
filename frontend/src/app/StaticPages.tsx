import { Link } from 'react-router-dom'

import { routes } from './routes'

/**
 * 两个静态页：404 与 403（M8 Task 1）。
 *
 * ## 为什么把 403 单独做一页，而不是弹一个"权限不足"
 *
 * §5.3：前端的禁用与隐藏**只是提示**，真正的判据在后端。
 * 因此"被后端拒绝"是一种**正常且预期会发生**的界面状态
 * （隐藏了按钮但用户直接敲 URL、或角色被改过而页面没刷新）。
 * 把它渲染成"未知错误"会让用户报障，而正确处理是告诉他**该找谁**。
 *
 * 两页都给出**下一步能做什么**（§5.5 的错误三段式）：
 * 只说"你没有权限"等于把问题丢回给用户。
 */

export function NotFoundPage(): JSX.Element {
  return (
    <section aria-labelledby="not-found-title">
      <h1 id="not-found-title">页面不存在</h1>
      <p style={{ color: 'var(--text-2)' }}>
        这个地址没有对应的页面。可能是链接过期，或任务编号被改正过。
      </p>
      <p>
        <Link to={routes.tasks}>返回待办列表</Link>
      </p>
    </section>
  )
}

export function ForbiddenPage(): JSX.Element {
  return (
    <section aria-labelledby="forbidden-title">
      <h1 id="forbidden-title">没有访问权限</h1>
      <p style={{ color: 'var(--text-2)' }}>
        当前身份不具备查看该页面所需的权限。请联系系统管理员为你的账号授予对应角色；
        这与"内容不存在"不同 —— 换一个账号登录后再试是有效的。
      </p>
      <p>
        <Link to={routes.tasks}>返回待办列表</Link>
      </p>
    </section>
  )
}
