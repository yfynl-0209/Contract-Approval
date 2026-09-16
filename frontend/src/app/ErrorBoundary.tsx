import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * 应用级错误边界（M8 Task 1）。
 *
 * ## ⚠️ 它**不打印响应体**，而且这不是"顺手省略"
 *
 * 材料 §Global Constraints 明确："No object key, server path, contract body, form data,
 * or token may enter URLs, console logs, analytics, or error telemetry."
 *
 * 后端错误体（`app/api/errors.py`）在 `message` 里会带**业务上下文**
 * （哪份合同、哪个附件、哪个字段）—— 那是给人看的排障信息，不是给浏览器日志的。
 * 一份合同标题进了控制台，就可能被截图、被上报、被第三方脚本读走。
 *
 * 因此这里**只记错误类型与位置**（`name` / `componentStack` 的顶层组件名），
 * 正文一律不进 console。需要细节时去服务端日志按 `correlation_id` 查 ——
 * 那才是它该在的地方（M8 Task 2 会把 correlation id 显示给用户，便于工单对齐）。
 *
 * ## 为什么错误边界只兜"渲染期缺陷"
 *
 * 接口失败是**业务事实**（§5.2），由 `ApiError` 作为**返回值**处理并渲染成
 * 三段式提示。它会一路冒到这里，说明有人把"可预期的失败"抛成了异常 ——
 * 那时页面结构已经不完整，展示一句笼统的话并给出重新加载才是正确处置。
 */

interface ErrorBoundaryProps {
  readonly children: ReactNode
}

interface ErrorBoundaryState {
  readonly error: Error | null
}

export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // ⚠️ 只留类型与组件栈首行。**不要**改写成 `console.error(error)` ——
    // 那是把整棵异常对象（含可能被塞进 Error 的响应体）写进浏览器日志。
    const component = info.componentStack?.split('\n')[1]?.trim() ?? '未知组件'
    console.error(`[ui] 未预期的渲染错误：${error.name} @ ${component}`)
  }

  private readonly handleReload = (): void => {
    // 整页重新加载而不是"清空错误状态再渲染"：出错时组件树的内部状态
    // 可能已经自相矛盾（比如列表拿到一半数据），原地重试很可能再崩一次，
    // 而用户会以为是"点了一次没用"。
    window.location.reload()
  }

  render(): ReactNode {
    const { error } = this.state
    if (error === null) {
      return this.props.children
    }

    return (
      <section
        role="alert"
        aria-labelledby="fatal-title"
        style={{ padding: 'var(--space-5)' }}
      >
        <h1 id="fatal-title">页面出现未预期的错误</h1>
        <p style={{ color: 'var(--text-2)' }}>
          这不是你的操作问题，页面状态可能已经不一致。请重新加载；
          如果反复出现，请把下方错误类型告知开发人员。
        </p>
        <p style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-sm)' }}>
          错误类型：{error.name}
        </p>
        <button type="button" onClick={this.handleReload}>
          重新加载页面
        </button>
      </section>
    )
  }
}
