/**
 * 无障碍回归（M8 Task 9）—— jsdom 测不到的部分在真浏览器里验证。
 *
 * ⚠️ 运行前置见 `security.spec.ts` 文件头（浏览器二进制未安装，spec 已写好未运行）。
 *
 * 单元层（`src/a11y/`）已覆盖：可访问名称、Tab 顺序、`lang`、`:focus-visible` 规则、
 * 对比度数值。这一层补的是**需要真实渲染**的：
 *
 * - 焦点环**真的可见**（2px、非 outline:none）；
 * - 状态信息不依赖颜色（高风险徽章同时有文字）；
 * - 颜色方案下的整体可读性（可加 @axe-core/playwright 做全页扫描）。
 */
import { expect, test } from '@playwright/test'

test.describe('键盘与焦点', () => {
  test('Tab 进入页面后，焦点环在主导航上可见', async ({ page }) => {
    await page.goto('/tasks')

    await page.keyboard.press('Tab')

    const focused = page.evaluate(() => document.activeElement?.tagName)
    expect(await focused).toBe('A')

    // 焦点元素的 outline/box-shadow 必须非 none（global.css 的 :focus-visible 规则）
    const hasRing = page.evaluate(() => {
      const element = document.activeElement
      if (element === null) {
        return false
      }
      const style = getComputedStyle(element)
      return style.outlineStyle !== 'none' || style.boxShadow !== 'none'
    })
    expect(await hasRing).toBe(true)
  })

  test('证据框可用键盘聚焦（它们是 button，不是只能鼠标点的 div）', async ({ page }) => {
    await page.goto('/tasks/1?tab=parse')

    // 定位到有证据的页后，Tab 应能到达证据框
    const boxes = page.locator('[data-testid="evidence-overlay"] button')
    const count = await boxes.count()
    test.skip(count === 0, '该夹具任务没有带坐标的证据 —— 定位用例见 parse 数据')

    await boxes.first().focus()
    await expect(boxes.first()).toBeFocused()
  })
})

test.describe('颜色不是唯一载体', () => {
  test('高风险标记同时有文字', async ({ page }) => {
    await page.goto('/tasks/1?tab=result')

    // 总风险为"高"时，页面必须出现"高"这个字 —— 灰度打印/色盲下信息仍在
    await expect(page.getByTestId('overall-risk')).toContainText('高')
  })

  test('解析字段的四态各自带文字标记（不是只有颜色条）', async ({ page }) => {
    await page.goto('/tasks/1?tab=parse')

    const fields = page.getByTestId('parse-fields')
    await expect(fields).toBeVisible()

    // 四态标记：✓ 已提取 / ∅ 未发现 / ⚠ 不确定 / ✗ 解析失败
    const marks = fields.locator('text=/^[✓∅⚠✗]/')
    expect(await marks.count()).toBeGreaterThan(0)
  })
})
