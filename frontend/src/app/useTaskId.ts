import { useParams } from 'react-router-dom'

/**
 * 从路由取 `taskId`（数字）。
 *
 * ⚠️ **必须走 `useParams`，不能用 `window.location`**：读全局地址栏的代码在
 * 测试里拿到的是 jsdom 的空白地址（`MemoryRouter` 不写地址栏），
 * 于是它只在浏览器里"能跑" —— 而测试根本测不到它。
 *
 * 单独成文件（而不是放在某个 Tab 里）是因为模块 2/3/4/5 都要用它：
 * 各写一遍时，"非法 id 怎么处理"会有两个答案（`0` / `NaN` / 抛错），
 * 而它们都只在链接被手工改过时才显形。
 */
export function useTaskId(): { readonly taskId: number } {
  const { taskId } = useParams<{ taskId: string }>()
  return { taskId: taskId === undefined ? 0 : Number.parseInt(taskId, 10) }
}
