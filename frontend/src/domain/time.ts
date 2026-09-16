/**
 * 时间格式化（M8 Task 3）。
 *
 * ## ⚠️ 为什么不走 `Date`
 *
 * 后端下发的是 `datetime.isoformat()`，**不带 `Z` 也没有时区偏移** ——
 * 它是数据库里的本地时间。而 `new Date('2026-09-15T09:30:45')` 在
 * ECMAScript 里被规定为**按本地时区**解析，历史上多数浏览器如此，
 * 但**日期时间串不带时区**这一档在不同引擎/版本上的处理并不完全一致
 * （`2026-09-15` 这种纯日期串更是明确按 UTC 解析）。
 *
 * 结果就是：同一份数据在 CI 与开发机上显示不同的小时数 ——
 * 而这种"看起来只是差几小时"的偏差，会被当成时区配置问题排查很久。
 *
 * 因此这里**按字符串切**：它与后端表达的是同一件事，没有任何解释空间。
 */

/** 把后端的时间串渲染成 `YYYY-MM-DD HH:mm`；空值给 `—`。 */
export function formatLocalTime(value: string | null): string {
  if (value === null || value === '') {
    return '—'
  }
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(value)
  if (match === null) {
    // 认不出的形态**原样显示**：把无法解析的值换成"无效时间"会掩盖
    // "后端换了格式"这件事，而那正是需要被看见的
    return value
  }
  const [, year, month, day, hour, minute] = match
  return `${year}-${month}-${day} ${hour}:${minute}`
}

/** 只要日期部分（列表里想更省空间时用）。 */
export function formatLocalDate(value: string | null): string {
  if (value === null || value === '') {
    return '—'
  }
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value)
  return match === null ? value : `${match[1]}-${match[2]}-${match[3]}`
}
