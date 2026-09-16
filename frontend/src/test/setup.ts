import '@testing-library/jest-dom/vitest'

/**
 * 测试环境准备（M8 Task 1）。
 *
 * 只做一件事：装 `jest-dom` 的匹配器（`toBeVisible` / `toHaveAccessibleName` …）。
 * 刻意**不在这里塞全局 mock**（比如假的 fetch）：一个"到处都生效"的 mock
 * 会让某个测试忘了搭桩时也照样通过 —— 而它验证的其实是那份 mock。
 * 需要替身的测试各自显式搭桩（M8 Task 2 起用 `msw` 风格的显式 handler）。
 */
