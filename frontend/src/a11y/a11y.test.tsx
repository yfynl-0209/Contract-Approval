import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { screen, within } from '@testing-library/react'

// ⚠️ 读源文件的原因见 contrast.test.ts 顶部说明（`?raw` 会被 CSS 管线拦掉）
const indexHtml = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'index.html'),
  'utf8',
)
const globalCss = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '..', 'styles', 'global.css'),
  'utf8',
)
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { json, stubApi } from '../test/stubApi'
import { renderApp } from '../test/renderApp'

/**
 * 无障碍行为回归（M8 Task 9）—— jsdom 能**真实验证**的那部分。
 *
 * ## 诚实的能力边界
 *
 * | 能在 jsdom 里验证 | 不能（要真浏览器，见 `e2e/accessibility.spec.ts`） |
 * | --- | --- |
 * | 焦点顺序 = DOM 顺序（键盘 Tab 路径） | 焦点环**真的画出来**且足够醒目 |
 * | 每个可交互元素有**可访问名称** | 读屏软件的实际播报 |
 * | 状态有**文字标记**（颜色不是唯一载体） | 实际渲染的对比度（由 `contrast.test.ts` 按公式算） |
 * | `<html lang>`、`:focus-visible` 规则存在 | |
 *
 * "不能"的那部分如实留给 e2e —— 在 jsdom 里写一个"检查了样式字符串"的断言
 * 冒充浏览器验证，比不测更糟。
 */
function setup(permissions: readonly string[] = ['task:read', 'result:save', 'result:confirm', 'rule:manage', 'audit:read', 'ops:retry']): void {
  sessionStorage.setItem(
    'contract-approval.dev-identity',
    JSON.stringify({ actorId: 'r1', displayName: '', roles: ['legal_reviewer'] }),
  )
  stubApi({
    '/api/me': () =>
      json({
        actor_id: 'r1',
        display_name: 'R1',
        tenant_id: 'default',
        roles: ['legal_reviewer'],
        unknown_roles: [],
        permissions,
      }),
    '/api/tasks': () =>
      json({ items: [], total: 0, page: 1, page_size: 20, page_count: 0, has_next: false }),
    '/api/tasks/summary': () =>
      json({ total: 0, by_status: { pending: 0, parsing: 0, reviewing: 0, blocked: 0, done: 0 }, writeback_failed: 0 }),
  })
}

describe('键盘可达性', () => {
  it('⚠️ Tab 的第一站是**主导航的第一个链接**（顺序 = DOM 顺序，可预测）', async () => {
    const user = userEvent.setup()
    setup()

    renderApp('/tasks')

    await user.tab()
    const nav = screen.getByRole('navigation', { name: '主导航' })
    const firstLink = within(nav).getAllByRole('link')[0]
    expect(firstLink).toHaveFocus()
  })

  it('连续 Tab 依次到达主导航的三个入口（不跳、不陷入）', async () => {
    const user = userEvent.setup()
    setup()

    renderApp('/tasks')

    await user.tab()
    const nav = screen.getByRole('navigation', { name: '主导航' })
    const links = within(nav).getAllByRole('link')

    expect(document.activeElement).toBe(links[0])
    await user.tab()
    expect(document.activeElement).toBe(links[1])
    await user.tab()
    expect(document.activeElement).toBe(links[2])
  })
})

describe('可访问名称', () => {
  it('⚠️ 页面上**每个**按钮、链接、输入都有可访问名称（无"无名控件"）', async () => {
    setup()

    renderApp('/tasks')

    // 等列表请求落地后再查（加载中的骨架不算）
    await screen.findByRole('navigation', { name: '主导航' })

    for (const button of screen.getAllByRole('button')) {
      expect(button, `按钮缺可访问名称：${button.outerHTML.slice(0, 80)}`).toHaveAccessibleName()
    }
    for (const link of screen.getAllByRole('link')) {
      expect(link, '链接缺可访问名称').toHaveAccessibleName()
    }
    for (const box of [
      ...screen.queryAllByRole('textbox'),
      ...screen.queryAllByRole('searchbox'),
    ]) {
      expect(box, '输入框缺可访问名称').toHaveAccessibleName()
    }
  })
})

describe('文档级', () => {
  it('⚠️ `<html lang>` 必须声明（读屏据此选发音引擎；缺省时中文按英文读）', () => {
    expect(indexHtml).toMatch(/<html\s+lang="zh-CN"/)
  })

  it('焦点样式必须用 `:focus-visible`（鼠标点击不亮环，键盘导航才亮）', () => {
    // ⚠️ 用 `:focus` 会让鼠标点过的按钮一直亮环 —— "谁有焦点"在视觉上失去意义
    expect(globalCss).toContain(':focus-visible')
    expect(globalCss).toContain('focus-ring')
  })
})
