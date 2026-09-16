/**
 * 模块占位块（M8 Task 1 的脚手架）。
 *
 * ⚠️ **它不是"待办清单"，而是"路由已就位、内容未实现"的显式标记。**
 *
 * 为什么不做成空白页：空白页与"加载失败但没报错"在界面上完全一样，
 * 而 M8 的五个模块要逐个实现，中间态会被反复看到。
 * 占位块写明**这个模块叫什么、哪个 Task 会填它、它依赖哪个后端接口**，
 * 于是"这里为什么是空的"不需要去翻计划。
 *
 * 每个模块的实现会把对应文件里的 `Placeholder` **换掉**，
 * 而不是再加一层"如果实现了就渲染实现"的分支 ——
 * 那种分支永远删不干净，最后变成上线后还挂着的死代码。
 */

export interface PlaceholderProps {
  /** 模块名（与需求文档里的叫法一致） */
  readonly title: string
  /** 交付该模块的 Task，便于对照计划 */
  readonly task: string
  /** 该模块主要消费的后端接口 */
  readonly endpoints: readonly string[]
  /** 一句话说明这个模块要回答什么问题 */
  readonly purpose: string
}

export function Placeholder({
  title,
  task,
  endpoints,
  purpose,
}: PlaceholderProps): JSX.Element {
  return (
    <section
      aria-labelledby="placeholder-title"
      data-testid="module-placeholder"
      style={{
        border: '1px dashed var(--border-strong)',
        borderRadius: 'var(--radius)',
        padding: 'var(--space-4)',
        background: 'var(--surface-1)',
      }}
    >
      <h2 id="placeholder-title">{title}</h2>
      <p style={{ color: 'var(--text-2)' }}>{purpose}</p>
      <dl
        style={{
          display: 'grid',
          gridTemplateColumns: 'max-content 1fr',
          gap: 'var(--space-1) var(--space-3)',
          margin: 0,
          fontSize: 'var(--font-size-sm)',
          color: 'var(--text-2)',
        }}
      >
        <dt>计划任务</dt>
        <dd style={{ margin: 0 }}>{task}</dd>
        <dt>消费接口</dt>
        <dd style={{ margin: 0 }}>{endpoints.join('、')}</dd>
      </dl>
    </section>
  )
}
