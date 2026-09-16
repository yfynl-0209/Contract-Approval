/**
 * 构建产物的**泄漏检查**（M8 Task 9 / 验收 13、15）。
 *
 * 在 `dist/` 里搜这些字符串，命中即失败（退出码 1）：
 *
 * | 模式 | 为什么不该出现 |
 * | --- | --- |
 * | `object_key` | 内部存储布局（M9 换 MinIO 前的本地实现细节）。下发它等于把"实现替换"变成"破坏性变更" |
 * | `file_path` | 工具 3 给**外部调用方**的受控路径；前端若出现它，说明界面开始依赖"服务器本地有文件"这一 M3 实现事实 |
 * | `workspace/`、盘符路径 | 服务器真实路径 |
 * | `localhost:8000` / `127.0.0.1:8000` | 前端必须走相对 `/api`（代理），写死后端地址会让部署配置失效 |
 * | `super-secret` 等令牌样串 | 测试替身里的凭据 |
 * | `HT-2026-` | 演示夹具的实例号 —— 出现在产物里说明测试数据混进了业务代码 |
 *
 * ⚠️ 这条检查**只在打包后的产物上跑**：源码里这些词出现在**注释**与**测试**里是合法的，
 * 打包会剥掉前者、排除后者 —— 所以"源码 grep 干净"不等于"产物干净"。
 *
 * 挂在 `npm run build` 的末尾：产物刚生成就验，而不是等 Task 10 的验收再验一次。
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const distDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'dist')

const FORBIDDEN = [
  { pattern: /object_key/i, why: '对象键（内部存储布局）' },
  { pattern: /file_path/i, why: '服务器文件路径（外部调用方专用字段）' },
  { pattern: /workspace\//i, why: '服务器相对路径' },
  // ⚠️ 盘符路径要**带真实目录段**才算：裸的 `X:/` 会命中 "https:/" 这类
  // 协议串的尾部（实测 `s:/`、`p:/` 都是这么来的）
  {
    pattern: /[A-Za-z]:[\\\\/](?:Users|Windows|Program|home|workspace|tmp|opt|var)/i,
    why: '盘符路径',
  },
  { pattern: /\.venv/i, why: '服务器目录' },
  { pattern: /localhost:8000|127\.0\.0\.1:8000/i, why: '写死的后端地址（应走相对 /api）' },
  { pattern: /super-secret/i, why: '测试令牌' },
  { pattern: /HT-2026-\d{3,}/, why: '演示夹具实例号' },
]

function walk(dir) {
  const entries = []
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) {
      entries.push(...walk(full))
    } else {
      entries.push(full)
    }
  }
  return entries
}

let files
try {
  files = walk(distDir)
} catch {
  console.error('[check-assets] 找不到 dist/ —— 请先执行 `npm run build`')
  process.exit(1)
}

const problems = []
for (const file of files) {
  const text = readFileSync(file, 'utf8')
  for (const { pattern, why } of FORBIDDEN) {
    const match = text.match(pattern)
    if (match !== null) {
      problems.push(`${file}: 命中 ${pattern}（${why}）→ "${match[0]}"`)
    }
  }
}

if (problems.length > 0) {
  console.error(`[check-assets] 产物里有 ${problems.length} 处不该出现的内容：`)
  for (const problem of problems) {
    console.error(`  - ${problem}`)
  }
  process.exit(1)
}

console.log(`[check-assets] ${files.length} 个产物文件干净（无对象键 / 路径 / 令牌 / 夹具数据泄漏）`)
