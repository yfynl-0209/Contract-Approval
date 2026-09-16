/**
 * 路由级加载态（M8 Task 1）。
 *
 * 为什么要有它，而不是"加载中什么都不显示"：
 * 空白页面与"这一页坏了"在视觉上无法区分，而用户的第一反应是刷新 ——
 * 刷新会把正在进行的请求全丢掉，于是慢接口永远等症状不消失。
 *
 * `aria-busy` + 可读文字：屏幕阅读器用户需要知道"正在等"，否则他听到的是
 * 一个没有内容的页面。故意**不做**旋转图标动画作为唯一线索
 * （`prefers-reduced-motion` 下它会被关掉，那时就什么都不剩了）。
 */
export function RouteLoading(): JSX.Element {
  return (
    <div aria-busy="true" aria-live="polite" style={{ padding: 'var(--space-5)' }}>
      <p style={{ color: 'var(--text-2)' }}>正在加载…</p>
    </div>
  )
}
