/**
 * 安全回归（M8 Task 9 / 验收 13、15）—— **真浏览器**里抓网络流量与页面源码。
 *
 * ## ⚠️ 运行前置（本仓库当前**未安装**）
 *
 * 这三个 spec 需要 `@playwright/test` + 浏览器二进制
 * （`npm i -D @playwright/test && npx playwright install chromium`），
 * 以及一套跑起来的环境（后端 8000 + 前端 5173 或 playwright.config 的 webServer）。
 * 它们**已写好但未在本机运行** —— 原因与 `canvas` 的处理一致：
 * 装不上的浏览器二进制不该挡住其余门禁。
 * 在有浏览器的环境里运行：`npx playwright test e2e/security.spec.ts`。
 *
 * ## 为什么这些检查要放在 e2e 而不是单元测试
 *
 * 泄漏发生在**网络层与渲染层**：单元测试里 fetch 是替身，
 * "响应里有没有对象键"这个问题根本不存在。只有真请求打到真后端，
 * 抓到的响应体才是证据。
 */
import { expect, test } from '@playwright/test'

test.describe('响应与页面不泄漏内部实现', () => {
  test('所有 /api 响应里不出现对象键与服务器路径', async ({ page }) => {
    const bodies: string[] = []
    page.on('response', async (response) => {
      if (response.url().includes('/api/')) {
        try {
          bodies.push(await response.text())
        } catch {
          // 正文不可读（重定向/流）不算泄漏证据
        }
      }
    })

    await page.goto('/tasks')
    await expect(page.getByRole('navigation', { name: '主导航' })).toBeVisible()

    // 进入详情与解析页，覆盖附件与标准文档两类响应
    await page.goto('/tasks/1?tab=detail')
    await page.goto('/tasks/1?tab=parse')

    for (const body of bodies) {
      expect(body, '响应体里出现对象键（内部存储布局）').not.toContain('object_key')
      expect(body, '响应体里出现服务器路径').not.toMatch(/workspace\/|[A-Za-z]:\\/)
    }
  })

  test('页面渲染出来的文本里不出现服务器路径与写死的后端地址', async ({ page }) => {
    await page.goto('/tasks/1?tab=detail')
    const text = (await page.locator('body').innerText()).toString()

    expect(text).not.toMatch(/workspace\//)
    expect(text).not.toMatch(/[A-Za-z]:\\/)
    expect(text).not.toContain('127.0.0.1:8000')
    expect(text).not.toContain('localhost:8000')
  })

  test('附件内容经 /api 下发（Content-Disposition 用文件名，不含路径）', async ({ page }) => {
    const headers: Record<string, string> = {}
    page.on('response', (response) => {
      if (response.url().includes('/api/attachments/') && response.url().includes('/content')) {
        for (const [name, value] of Object.entries(response.headers())) {
          headers[name] = value
        }
      }
    })

    await page.goto('/tasks/1?tab=detail')

    expect(Object.keys(headers).length).toBeGreaterThan(0)
    const disposition = headers['content-disposition'] ?? ''
    expect(disposition).not.toMatch(/workspace\/|[A-Za-z]:\\/)
  })
})
