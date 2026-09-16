/**
 * 数值格式化（M8 Task 4）。
 *
 * 与 `domain/time.ts` 同一个理由单独成文件：这些是**纯函数**，
 * 可以直接单测，而且不放在组件文件里时 `react-refresh` 才能热更新。
 */

/**
 * 字节数 → 人类可读。
 *
 * ⚠️ `null` 给 `—`：**"未知"与"0 字节"是两件事**。
 * 后者是"这份文件是空的"（`ATTACHMENT_EMPTY` 那条业务结论），
 * 前者是"我们还没拿到它的大小"。合并成 `0 B` 会让两种情况看起来一样。
 */
export function formatSize(bytes: number | null): string {
  if (bytes === null) {
    return '—'
  }
  if (bytes < 1024) {
    return `${bytes} B`
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`
  }
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

/** 摘要只显示前 N 位（完整值留在 `title` 里，需要核对时可见）。 */
export function shortDigest(digest: string | null, length = 12): string {
  return digest === null ? '—' : digest.slice(0, length)
}
