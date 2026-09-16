import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import jsxA11y from 'eslint-plugin-jsx-a11y'
import tseslint from 'typescript-eslint'

/**
 * ESLint 9 的扁平配置（M8 Task 1）。
 *
 * 刻意**不开 type-aware**（`projectService`）：
 * 它要求 `vite.config.ts` 等边缘文件也进 tsconfig，
 * 于是为了 lint 一个构建脚本要给它配一份 type 环境。
 * 这里用 `tseslint.configs.recommended` 的语法级规则，
 * 把类型正确性交给 `npm run typecheck`（一条命令、一个真相来源）。
 *
 * 真正的界面级约束（不得把 form_data 写进 URL/console）由
 * M8 Task 9 的 e2e 与代码搜索守着，而不是靠一条 lint 规则 ——
 * 后者只能看见字面量，看不见拼接出来的字符串。
 */
export default tseslint.config(
  {
    // ⚠️ `e2e` 引用 @playwright/test（浏览器二进制未随本仓库安装，见 e2e 文件头）——
    // 在它真正可运行之前不参与 lint/typecheck，避免"装不上的依赖"挡住全部门禁
    ignores: ['dist', 'node_modules', 'coverage', 'e2e'],
  },
  {
    files: ['**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.browser },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true },
      ],
      // 后端错误体里可能含细节；只允许显式处理后的 `error_code` / `message`
      // 进 console（M8 Task 2 的 `ApiError` 负责裁剪）
      'no-console': ['warn', { allow: ['warn', 'error'] }],
    },
  },
  {
    // ⚠️ 无障碍规则进 **lint 门禁**（Task 9）：`alt`/`aria-label`/交互元素这类
    // 问题在代码评审里最容易漏，而它们是静态可查的。
    // 动态部分（焦点路径、读屏播报）仍归 e2e 与人工验证 —— 两者不互替。
    files: ['**/*.tsx'],
    ...jsxA11y.flatConfigs.recommended,
  },
)
