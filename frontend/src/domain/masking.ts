/**
 * 敏感值掩码（M8 Task 4，材料 §4.2）。
 *
 * ## 为什么默认掩码是"必须"而不是"最好有"
 *
 * 审批表单里带着证件号与联系方式，而控制台是要**被截图贴进审批流**的
 * （验收 14 明确检查"导出/截图材料中不含证件号、联系方式等敏感值"）。
 * 默认展示原文时，泄漏发生在**截图那一刻**，而事后无法追回。
 *
 * ## 掩码只发生在**呈现**层
 *
 * 服务端把原文给它授权的调用方（判据是 `task:read`），掩码是本页面的显示决定。
 * 因此：
 * - **不得**把掩码结果写回任何请求（那不是脱敏，是改数据）；
 * - **不得**把原文放进 URL / console（那是另一条泄漏路径，见 `client.ts`）。
 *
 * ## 为什么保留首尾各两位
 *
 * 全掩成 `••••` 时，用户无法回答"这是不是我以为的那一条" ——
 * 于是他只能点开，而点开就失去了掩码的意义。
 * 保留少量字符是通行做法（如 `3302**********1234`）：
 * 足以核对、不足以还原。
 */

/** 判定"这个键名是不是敏感"。中文键名与英文键名都要覆盖。 */
const SENSITIVE_KEY_PATTERN =
  /(身份证|证件|证号|统一社会信用|纳税人|税号|银行|账号|账户|手机|电话|联系方式|邮箱|邮件|id_?card|passport|mobile|phone|email|account|iban|tax_?no|credit)/i

/** 邮箱：只保留域名 —— 域名通常不敏感，而它决定了"这是公司邮箱还是私人邮箱"。 */
const EMAIL_PATTERN = /^([^@]{1,2})[^@]*(@.*)$/

export function isSensitiveKey(key: string): boolean {
  return SENSITIVE_KEY_PATTERN.test(key)
}

/**
 * 掩码一个值。
 *
 * ⚠️ 非字符串（数字、嵌套对象）**不递归**：把数字掩成字符串会让"金额"这类
 * 字段失去可读性，而递归进对象则可能把嵌套结构里的值暴露成同一层级的文本。
 * 遇到非字符串时统一返回一个不含原文的占位符。
 */
export function maskValue(value: unknown): string {
  if (typeof value !== 'string') {
    // 数字/布尔/对象：给一个**不含原文**的提示，而不是把它们序列化出来 ——
    // 序列化会把值（可能含证件号）重新变成一个可读字符串
    return '（已掩码）'
  }
  const text = value.trim()
  if (text === '') {
    return '（空）'
  }

  const email = EMAIL_PATTERN.exec(text)
  if (email !== null) {
    const [, head, domain] = email
    return `${head}***${domain}`
  }

  if (text.length <= 4) {
    // 太短时保留任何一位都可能等于泄漏全部
    return '*'.repeat(text.length)
  }
  const head = text.slice(0, 2)
  const tail = text.slice(-2)
  return `${head}${'*'.repeat(text.length - 4)}${tail}`
}

/** 值的展示形态：字符串原样，其余 JSON 化（**仅在已决定展示时调用**）。 */
export function displayValue(value: unknown): string {
  if (value === null || value === undefined) {
    return '—'
  }
  if (typeof value === 'string') {
    return value
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  return JSON.stringify(value)
}
