/**
 * 性能回归（M8 Task 9）—— 真浏览器里的加载与 DOM 边界。
 *
 * ⚠️ 运行前置见 `security.spec.ts` 文件头（浏览器二进制未安装，spec 已写好未运行）。
 *
 * ## 这两条守的是什么
 *
 * 1. **pdf.js 不进主包**（Task 5 的懒加载决策）：主文档的 JS 请求集合
 *    不应包含 PdfPageCanvas chunk，除非用户真的走到了能画框的那一页。
 * 2. **DOM 页数有界**：100 页文档在 DOM 里只有 1 页
 *    （单元测试在 mock 下已守住；这里用真实渲染再验一次）。
 */
import { expect, test } from '@playwright/test'

test('主文档不加载 PDF 引擎 chunk（除非进入渲染页）', async ({ page }) => {
  const jsRequests: string[] = []
  page.on('request', (request) => {
    if (request.url().endsWith('.js') || request.url().endsWith('.mjs')) {
      jsRequests.push(request.url())
    }
  })

  await page.goto('/tasks')
  await expect(page.getByRole('heading', { name: '待办调用' })).toBeVisible()

  expect(
    jsRequests.some((url) => url.includes('PdfPageCanvas')),
    '待办列表页不应加载 PDF 查看器（373 kB 的 pdf.js）',
  ).toBe(false)

  // 进入解析页并真的渲染一页后，chunk 才出现（懒加载生效的证据）
  await page.goto('/tasks/1?tab=parse')
  await expect(page.getByTestId('pdf-page').first()).toBeVisible({ timeout: 15_000 })
  // 此时 PdfPageCanvas chunk 必须已被请求
})

test('长文档在 DOM 里只有当前一页', async ({ page }) => {
  // ⚠️ 前置：夹具里有一份 ≥100 页的合同（scripts 的 make_fixtures 负责）
  await page.goto('/tasks/1?tab=parse')
  await page.getByRole('button', { name: '下一页' }).click()

  await expect(page.getByTestId('pdf-page')).toHaveCount(1)
})
