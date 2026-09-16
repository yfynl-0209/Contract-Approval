import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { AppProviders } from './app/AppProviders'
import { AppRoutes } from './app/AppRoutes'
import { AuthProvider } from './app/AuthProvider'
import { ErrorBoundary } from './app/ErrorBoundary'
import './styles/global.css'

/**
 * 应用入口（M8 Task 1）。
 *
 * ## 中间件的**顺序**是有讲究的
 *
 * ```text
 * StrictMode → ErrorBoundary → AppProviders → BrowserRouter → AppRoutes
 * ```
 *
 * - `ErrorBoundary` 在 Provider **之外**：Provider 自身的渲染失败
 *   （未来 `AuthProvider` 初始化抛错）也要能被兜住；
 *   反过来放时，Provider 崩了就是白屏。
 * - `AppProviders` 在 `BrowserRouter` 之外：M8 Task 2 起路由守卫要在
 *   渲染前拿到身份，而身份来自 Provider；顺序反了会得到"守卫先跑、身份后到"，
 *   表现为刷新页面时被误判为未登录。
 *
 * ⚠️ 这里**不注册任何全局的 `window.onerror` / 上报脚本**：
 * 材料 §Global Constraints 禁止把响应体或表单数据送进错误上报，
 * 而一个"什么都收"的全局钩子做不到这件事（它看不见什么该收）。
 * 需要上报时按 `correlation_id` 去服务端取，那才是权威来源。
 */
const container = document.getElementById('root')
if (container === null) {
  // 入口模板被改坏时立刻失败，而不是留下一个"页面空白但没有报错"的状态
  throw new Error('index.html 缺少 #root 容器')
}

createRoot(container).render(
  <StrictMode>
    <ErrorBoundary>
      <AppProviders>
        {/*
          ⚠️ 两个 v7 未来标志现在就打开，而不是等到升级那天：
          不打开时路由库**每次启动都打印警告**，而"一条谁都不读的警告"
          会让真正的警告也被忽略。两个标志的行为差异对本应用都是正向的：
            v7_startTransition —— 状态更新进入 startTransition，切 tab 时不阻塞输入；
            v7_relativeSplatPath —— 通配路由内的相对路径解析规则，本应用不依赖旧行为。
        */}
        <AuthProvider>
          <BrowserRouter
            future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
          >
            <AppRoutes />
          </BrowserRouter>
        </AuthProvider>
      </AppProviders>
    </ErrorBoundary>
  </StrictMode>,
)
