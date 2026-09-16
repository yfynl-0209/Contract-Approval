import { Navigate, Route, Routes } from 'react-router-dom'

import { OpsPage } from '../features/ops/OpsPage'
import { RuleAdminPage } from '../features/ruleAdmin/RuleAdminPage'
import { TaskListPage } from '../features/tasks/TaskListPage'
import { TaskWorkbench } from '../features/workbench/TaskWorkbench'
import { AppLayout } from './AppLayout'
import { routes } from './routes'
import { ForbiddenPage, NotFoundPage } from './StaticPages'

/**
 * 路由表（M8 Task 1）。
 *
 * ## 三条刻意如此的结构决定
 *
 * 1. **模块 2–5 只占一个路由**（`/tasks/:taskId`），视角由 `?tab=` 决定，
 *    选择发生在 `TaskWorkbench` 内部（它读 `useSearchParams`）。
 *    这里**看不到**任何 tab 相关的路由 —— 那正是"切换视角不产生历史记录"
 *    这条要求的具体形态（材料 §3）。
 * 2. **页面直接 import，不做 `React.lazy`**：五个模块是同一份合同的几种看法，
 *    审查人会在它们之间反复切；懒加载只会让每次切换多一次网络往返与一个加载态闪烁。
 *    （M8 Task 5 的 PDF 查看器是唯一例外 —— 它要拖进 PDF.js，届时单独处理。）
 * 3. **`/` 重定向到 `/tasks`**，而不是做一个"总览页"：需求里的第一个模块就是
 *    待办列表，凭空多一个空壳首页只会让每次进入都多一次点击。
 */
export function AppRoutes(): JSX.Element {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<Navigate to={routes.tasks} replace />} />
        <Route path="tasks" element={<TaskListPage />} />
        {/* 模块 2–5 的容器（视角由 ?tab= 决定） */}
        <Route path="tasks/:taskId" element={<TaskWorkbench />} />
        <Route path="rules" element={<RuleAdminPage />} />
        <Route path="ops" element={<OpsPage />} />
        <Route path="forbidden" element={<ForbiddenPage />} />
        {/* ⚠️ 通配放在最后：放前面会吃掉所有路径，而它"照样能跑" */}
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}
