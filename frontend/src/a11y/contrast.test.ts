import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/*
 * ⚠️ 读**源文件**而不是经过 Vite 的模块：
 * - `?raw` 在 Vitest 里会被 CSS 管线拦成空模块（实测）；
 * - `import '../styles/tokens.css'` 拿到的是副作用导入，不是文本。
 * 测试文件运行在 Node 里，`node:fs` 是这里的正当工具（tsconfig 已带 node 类型）。
 */
const css = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), '..', 'styles', 'tokens.css'),
  'utf8',
)

/**
 * 对比度回归（M8 Task 9）—— **按 `tokens.css` 现算 WCAG 比值**。
 *
 * ## 为什么不用浏览器/截图工具算
 *
 * jsdom 不做样式计算，快照对比也只 tells you "像素没变"。
 * 而对比度是一条**可计算的公式**（WCAG 2.x 相对亮度 + 比值），
 * 源就是 `tokens.css` 里的色值 —— 在这里按公式算，改任何一个色
 * 若跌下阈值测试就红，且报出的数字能直接核对。
 *
 * ## 阈值的取舍（都要能说出理由）
 *
 * | 组合 | 阈值 | 理由 |
 * | --- | --- | --- |
 * | 文字色 × 各表面 | **4.5** | WCAG AA 正文（本项目正文 14px、辅助 12px，都不属于"大字"） |
 * | `--accent` × 表面 | **4.5** | 链接是**文字**，不是图形 |
 * | 状态/风险色 × 白底与对应 soft 底 | **4.5** | 它们被用作**文字**（"⚠ 不确定"），不只是徽章底色 |
 * | `--focus-ring` × 表面 | **3** | 焦点指示属于"需要辨认的 UI 边界"（WCAG 1.4.11） |
 * | 一般边框 | **不查** | 装饰性边界不承载辨认义务；输入框由 label + 背景共同标识 |
 */

// ⚠️ 读源文件的原因见文件上方 import 处的说明

function parseTokens(source: string): Record<string, string> {
  const tokens: Record<string, string> = {}
  for (const match of source.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)) {
    const name = match[1]
    const value = match[2]
    if (name === undefined || value === undefined) {
      continue
    }
    tokens[name] = value
  }
  return tokens
}

function channels(hex: string): readonly [number, number, number] {
  const value = (start: number): number => Number.parseInt(hex.slice(start, start + 2), 16)
  return [value(1), value(3), value(5)]
}

/** sRGB → 线性化 → WCAG 相对亮度。 */
function luminance(hex: string): number {
  const linear = (value: number): number => {
    const scaled = value / 255
    return scaled <= 0.03928 ? scaled / 12.92 : ((scaled + 0.055) / 1.055) ** 2.4
  }
  const [r, g, b] = channels(hex)
  return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
}

function contrast(foreground: string, background: string): number {
  const lighter = Math.max(luminance(foreground), luminance(background))
  const darker = Math.min(luminance(foreground), luminance(background))
  return (lighter + 0.05) / (darker + 0.05)
}

const tokens = parseTokens(css)

/** 取令牌值；不存在就**抛错**而不是 `undefined` 悄悄参与计算（防改名静默跳过）。 */
function token(name: string): string {
  const value = tokens[name]
  if (value === undefined) {
    throw new Error(`tokens.css 里缺少 --${name}（被改名了？）`)
  }
  return value
}

const TEXT_TOKENS = ['text-1', 'text-2', 'text-3']
const SURFACES = ['surface-0', 'surface-1', 'surface-2', 'surface-sunken']
/** 状态/风险色 → 它们配套的 soft 底（徽章里"色底 + 同色文字"的组合）。 */
const STATUS_PAIRS: Readonly<Record<string, string>> = {
  'status-ok': 'risk-low-soft',
  'status-warn': 'risk-medium-soft',
  'status-danger': 'risk-high-soft',
  'risk-high': 'risk-high-soft',
  'risk-medium': 'risk-medium-soft',
  'risk-low': 'risk-low-soft',
}

describe('设计令牌的对比度（WCAG AA）', () => {
  it('解析 sanity：断言里用到的令牌都存在（防改名静默跳过）', () => {
    for (const name of [...TEXT_TOKENS, ...SURFACES, 'accent', 'focus-ring', ...Object.keys(STATUS_PAIRS)]) {
      expect(tokens[name], `缺少令牌 --${name}`).toBeDefined()
    }
  })

  it('⚠️ 正文/辅助文字 × 四种表面 ≥ 4.5:1', () => {
    for (const text of TEXT_TOKENS) {
      for (const surface of SURFACES) {
        const ratio = contrast(token(text), token(surface))
        expect(
          ratio,
          `--${text} 对 --${surface} 只有 ${ratio.toFixed(2)}:1（要 4.5）`,
        ).toBeGreaterThanOrEqual(4.5)
      }
    }
  })

  it('链接色 × 表面 ≥ 4.5:1（链接是文字）', () => {
    for (const surface of SURFACES) {
      const ratio = contrast(token('accent'), token(surface))
      expect(ratio, `--accent 对 --${surface} 只有 ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('⚠️ 状态/风险色作**文字**时，对白底与配套 soft 底 ≥ 4.5:1', () => {
    for (const [color, soft] of Object.entries(STATUS_PAIRS)) {
      const onWhite = contrast(token(color), token('surface-0'))
      const onSoft = contrast(token(color), token(soft))
      expect(
        onWhite,
        `--${color} 对白底只有 ${onWhite.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(4.5)
      expect(
        onSoft,
        `--${color} 对 --${soft} 只有 ${onSoft.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('焦点指示对表面 ≥ 3:1（WCAG 1.4.11 非文本对比）', () => {
    for (const surface of SURFACES) {
      const ratio = contrast(token('focus-ring'), token(surface))
      expect(ratio, `焦点环对 --${surface} 只有 ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(3)
    }
  })

  it('层次仍可分辨：text-3 必须比 text-2 **更浅**（否则两级说明没有视觉层次）', () => {
    // 对比度回归不能把"全都调到最黑"当成通过 —— 那牺牲了信息层次
    expect(luminance(token('text-3'))).toBeGreaterThan(luminance(token('text-2')))
  })
})
