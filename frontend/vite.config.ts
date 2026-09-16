import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

/**
 * 前端构建与测试配置（M8 Task 1）。
 *
 * ## 为什么用 `vitest/config` 的 `defineConfig`
 *
 * `test` 段落在 vite 自己的 `defineConfig` 里不是合法字段，类型也对不上。
 * 用 vitest 的版本可以让"测试配置"和"构建配置"**同源**：
 * 两边若各写一份（比如测试用另一套 resolve/alias），
 * 就会出现"测试里能 import、构建后 404"这种只在生产暴露的差异。
 *
 * ## 代理：前端只调 `/api/*`
 *
 * 计划 §Global Constraints 明确：**前端调 `/api/*`，不调 `/tools/*`**。
 * 七个工具是给**外部系统**的（需求 2.4.10），控制台用的是查询/确认/重试那些端点。
 * 因此这里只代理 `/api`。
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // ⚠️ 同时监听 IPv4 与 IPv6：Node 17+ 默认只绑 `::1`（IPv6 的 localhost），
    // 而部分浏览器把 `localhost` 解析成 `127.0.0.1`（IPv4）——
    // 结果是"服务明明在跑，浏览器却连接被拒"。host: true 绑全部地址（0.0.0.0 + ::），
    // 仅开发服务器，生产由反向代理提供同源。
    host: true,
    // 开发期同源代理：控制台与 API 之间不引入 CORS（CORS 一开就是另一条配置面，
    // 而它错了以后表现为"线上某个浏览器版本才失败"）
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    // ⚠️ 测试必须从"没有默认身份"的确定性状态出发：`.env.local` 里的
    // VITE_DEV_ACTOR_* 会被 vitest 一并加载，让"没有身份时应显示表单"
    // 之类的断言全部失真（真实教训：加默认身份的当晚全量测试红了 4 条）。
    // 显式置空以覆盖 env 文件；开发服务器不受影响。
    env: {
      VITE_DEV_ACTOR_ID: '',
      VITE_DEV_ACTOR_NAME: '',
      VITE_DEV_ACTOR_ROLES: '',
    },
    css: false,
    // 时区写死：后端返回的时间是**不带时区**的本地时间字符串
    // （`app/api/views.py::iso`），界面上按浏览器时区渲染时，
    // 同一条断言在 CI 与开发机上会得到不同结果 —— 那是**会飘的测试**，
    // 比没有测试更糟：它会消耗排查时间却不指向真正的缺陷。
    env: { TZ: 'Asia/Shanghai' },
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
