/**
 * 领域文案（M8 Task 3）。
 *
 * ## 为什么集中在这里，而不是写在各个组件里
 *
 * 同一个取值会在**多个页面**出现（`blocked` 在列表与详情、`write_status`
 * 在列表与结果页）。各写一遍时，分叉方式是某天改了一处的说法 ——
 * 于是同一个状态在两个页面上叫两个名字，而审阅者会以为它们是两件事。
 *
 * ## 三条纪律
 *
 * 1. **每个取值都有文字标签**：颜色永远不是唯一的信息载体（`tokens.css` 首段）。
 * 2. **未知取值原样返回机器码，不显示"未知"**：机器码是可统计、可搜代码的判据，
 *    把它换成一个笼统的词会让排障的人失去唯一的线索。
 * 3. **不出现"失败"这种笼统说法**：`not_written` 与 `failed` 的处置方向相反，
 *    文案必须让用户能分辨"该去确认"还是"该去重试"。
 */

import type {
  JobStatus,
  JobType,
  ReviewStatus,
  RunStatus,
  BusinessRole,
  ContextStatus,
  ContractLabel,
  ContractType,
  EvaluationStatus,
  RiskLevel,
  TaskStatus,
  WritebackReasonCode,
  WriteStatus,
} from '../api/contracts'

export const TASK_STATUS_LABELS: Readonly<Record<TaskStatus, string>> = {
  pending: '待处理',
  parsing: '解析中',
  reviewing: '审查中',
  blocked: '阻塞',
  done: '已完成',
}

export const WRITE_STATUS_LABELS: Readonly<Record<WriteStatus, string>> = {
  not_written: '未回写',
  writing: '回写中',
  success: '写成功',
  failed: '写失败',
}

export const RISK_LEVEL_LABELS: Readonly<Record<RiskLevel, string>> = {
  low: '低',
  medium: '中',
  high: '高',
}

/** 规则评价四态（`app/enums.py::EvaluationStatus`）。 */
export const EVALUATION_STATUS_LABELS: Readonly<Record<EvaluationStatus, string>> = {
  hit: '命中',
  needs_review: '需人工判断',
  not_hit: '未命中',
  not_applicable: '不适用',
}

/**
 * 审查批次状态（`app/enums.py::RunStatus`）—— 模块 4 的批次头。
 *
 * ⚠️ `running` / `failed` 时那份聚合**只反映已落库的部分**：
 * 把半截批次当完整结论会得到一份"低风险"，因为**还没算完**。
 * 因此这两个取值在界面上要带警示，而不是只显示一个中性的词。
 */
export const RUN_STATUS_LABELS: Readonly<Record<RunStatus, string>> = {
  running: '进行中（结论尚未完整）',
  completed: '已完成',
  failed: '已失败',
}

/** 结论完整性（`app/enums.py::ReviewStatus`）—— 与单条规则的状态是**两个层级**。 */
export const REVIEW_STATUS_LABELS: Readonly<Record<ReviewStatus, string>> = {
  complete: '完整',
  needs_review: '需人工判断',
}

/** 作业状态（`app/enums.py::JobStatus`）。`retry_wait` = 退避中，**不是**失败。 */
export const JOB_STATUS_LABELS: Readonly<Record<JobStatus, string>> = {
  queued: '排队中',
  running: '执行中',
  retry_wait: '等待重试',
  succeeded: '已完成',
  failed: '已失败',
}

/** 作业类型（`app/enums.py::JobType`）。前三种由工具 1–3 同步完成，一般不建作业。 */
export const JOB_TYPE_LABELS: Readonly<Record<JobType, string>> = {
  pull: '拉取待办',
  detail: '拉取详情',
  download: '下载附件',
  parse: '解析文档',
  rule: '规则审查',
  result: '保存结果',
  writeback: '回写意见',
}

/**
 * 卡在哪一步（`blocked_stage`）。
 *
 * ⚠️ 它是**恢复入口的线索**：`download` 失败要去重跑工具 3，
 * 而人工重试接口对 `pull` / `detail` / `download` 返回 `RETRY_NOT_SUPPORTED`（409）。
 * 因此文案要说"卡在哪"，而不是"出了错"。
 */
export const BLOCKED_STAGE_LABELS: Readonly<Record<string, string>> = {
  pull: '拉取待办',
  detail: '拉取详情',
  download: '下载附件',
  parse: '解析文档',
  rule: '规则审查',
  result: '保存结果',
  writeback: '回写意见',
}

/**
 * 回写未成功的原因（`app/enums.py::WritebackReasonCode`）。
 *
 * 前七个是**门禁拒绝**（没发起回写），后三个是**外部调用失败**（已发起）。
 * 这两组对应完全相反的动作，因此文案里直接写出该做什么。
 */
export const WRITEBACK_REASON_LABELS: Readonly<Record<WritebackReasonCode, string>> = {
  // 门禁拒绝：正确处置不是重试
  WRITEBACK_POLICY_DENIED: '门禁未通过（未发起回写）',
  TASK_NOT_DONE: '任务尚未完成',
  RESULT_MISSING: '还没有审查结果',
  CONTEXT_NOT_VALID: '审查立场不可信（缺失或冲突）',
  MANUAL_CONFIRM_REQUIRED: '高风险结果需人工确认后才能回写',
  ALREADY_WRITTEN: '已回写过（幂等拒绝）',
  COMMENT_TEXT_MISSING: '结果没有可回写的正文',
  // 外部调用失败：稍后可重试
  APPROVAL_API_ERROR: '审批系统返回错误',
  APPROVAL_API_TIMEOUT: '调用审批系统超时',
  IDEMPOTENCY_CONFLICT: '同一幂等键的内容不一致',
}

// ============================================================
// 权威审查上下文（模块 2）
// ============================================================

/** 上下文状态（`app/enums.py::ContextStatus`）。 */
export const CONTEXT_STATUS_LABELS: Readonly<Record<ContextStatus, string>> = {
  complete: '来自审批系统',
  missing: '立场未知',
  conflict: '声明与实际不一致',
  confirmed: '已人工确认',
}

/**
 * 每种状态**必须同时说明的后果**（设计 §4.2 的表）。
 *
 * ⚠️ `missing` 的警告不是客套：M3 已确定"刚拉取完的任务 `context_status`
 * 就是 `missing`"，那是**正确行为**。静默显示成普通字段时，
 * 用户会以为系统已经知道立场了 —— 而方向敏感的规则此时判不了。
 */
export const CONTEXT_STATUS_NOTES: Readonly<Record<ContextStatus, string>> = {
  complete: '四个业务事实来自审批系统。可人工修正。',
  missing: '立场未知，方向敏感的规则无法可靠判断，需人工填写后才能确认。',
  conflict: '审批单声明与实际不一致，需人工裁定。',
  confirmed: '已人工确认，回写门禁据此认为立场可信。',
}

export const CONTRACT_LABEL_LABELS: Readonly<Record<ContractLabel, string>> = {
  party_a: '甲方',
  party_b: '乙方',
  other: '其他',
  unknown: '未知',
}

export const BUSINESS_ROLE_LABELS: Readonly<Record<BusinessRole, string>> = {
  buyer: '采购方',
  seller: '销售方',
  customer: '客户',
  service_provider: '服务提供方',
  licensor: '许可方',
  licensee: '被许可方',
  other: '其他',
  unknown: '未知',
}

export const CONTRACT_TYPE_LABELS: Readonly<Record<ContractType, string>> = {
  procurement: '采购',
  sales: '销售',
  software_service: '软件服务',
  development: '开发',
  outsourcing: '外包',
  lease: '租赁',
  other: '其他',
  unknown: '未知',
}

/** 附件下载状态（`app/enums.py::DownloadStatus`）。 */
export const DOWNLOAD_STATUS_LABELS: Readonly<Record<string, string>> = {
  pending: '待下载',
  success: '已就绪',
  failed: '下载失败',
}

// ============================================================
// 解析字段与条款的中文名（模块 3）
// ============================================================

/**
 * 字段码 → 中文名。**与后端逐字对应**：
 * `app/rules/fields.py`（基本信息 8 + 规则直接输入 6 + 派生 3）
 * 与 `app/rules/clauses.py`（条款 9）。
 *
 * ⚠️ 这张表**不追求完整**，只求不撒谎：遇到没登记的码时
 * `fieldLabel()` **原样返回机器码**（如 `prepay_ratios`）——
 * 那正是需要被看见的信号（后端加了字段而前端没跟上）。
 * 换成"未命名字段"会把一个可搜索的线索抹掉。
 */
export const FIELD_CODE_LABELS: Readonly<Record<string, string>> = {
  // 基本信息（需求规定的 8 项）
  contract_title: '合同标题',
  contract_number: '合同编号',
  party_a: '甲方名称',
  party_b: '乙方名称',
  amount: '合同总金额',
  currency: '结算币种',
  effective_date: '生效时间',
  expiry_date: '到期时间',
  // 规则直接输入
  credit_code: '统一社会信用代码',
  pay_days: '付款周期（天）',
  renew_term_months: '单次自动续约期（月）',
  renew_notice_days: '续约异议的提前通知期（天）',
  confidentiality_years: '保密期限（年）',
  acceptance_days: '验收期限（天）',
  // 派生字段（由其他字段算出，无独立原文片段）
  prepay_ratio: '预付款比例',
  liability_party_a_ratio: '甲方违约金比例',
  liability_party_b_ratio: '乙方违约金比例',
  // 条款类型
  payment: '付款',
  delivery: '交付',
  acceptance: '验收',
  liability: '违约',
  confidentiality: '保密',
  data_processing: '数据',
  intellectual_property: '知识产权',
  dispute_resolution: '争议解决（含管辖地）',
  auto_renewal: '自动续约',
}

/** 字段/条款的中文名；未登记时**原样返回机器码**（见上方说明）。 */
export function fieldLabel(fieldCode: string): string {
  return FIELD_CODE_LABELS[fieldCode] ?? fieldCode
}

// ============================================================
// 规则评价的中文呈现（模块 4）
// ============================================================

/**
 * 稳定原因码 → 中文（`app/enums.py::ReasonCode` 的取值域）。
 *
 * 与字段表同一纪律：**未登记的码原样返回** —— 后端加新原因码而前端没跟上时，
 * 屏幕上出现的是那个可搜索的英文码，而不是一个撒谎的通用文案。
 */
export const REASON_CODE_LABELS: Readonly<Record<string, string>> = {
  APPLICABILITY_NOT_MET: '不满足适用条件',
  CONTEXT_MISSING: '缺少立场上下文',
  CONTEXT_CONFLICT: '立场上下文冲突',
  EVIDENCE_UNCERTAIN: '证据不足，需人工判断',
  EXTRACTION_FAILED: '字段提取失败',
  MODEL_UNAVAILABLE: '智能判断暂不可用',
  CONDITION_MATCHED: '条件成立',
  CONDITION_NOT_MATCHED: '条件不成立',
  CURRENCY_NOT_COMPARABLE: '币种不一致，无法比较',
  THRESHOLD_NOT_CONFIGURED: '未配置比较阈值',
}

export function reasonCodeLabel(code: string): string {
  return REASON_CODE_LABELS[code] ?? code
}

/**
 * 把文案里裸露的**字段码**替换成中文名（`prepay_ratio 未在合同中找到` →
 * `预付款比例 未在合同中找到`）。
 *
 * 只替换**整词**（`\b` 边界保证 `pay_days` 不会误伤 `notice_pay_days`），
 * 且**长码优先**；没登记的码原样保留 —— 与 `fieldLabel` 同一条"不撒谎"纪律。
 */
export function humanizeFieldCodes(text: string): string {
  let result = text
  const codes = Object.keys(FIELD_CODE_LABELS).sort((a, b) => b.length - a.length)
  for (const code of codes) {
    const label = FIELD_CODE_LABELS[code]
    if (label !== undefined) {
      result = result.replace(new RegExp(`\\b${code}\\b`, 'g'), label)
    }
  }
  return result
}

/** 未知取值**原样返回**（见文件头第 2 条纪律）。 */
export function labelOf(
  map: Readonly<Record<string, string>>,
  value: string | null | undefined,
  fallback = '—',
): string {
  if (value === null || value === undefined || value === '') {
    return fallback
  }
  return map[value] ?? value
}

export function taskStatusLabel(status: TaskStatus): string {
  return TASK_STATUS_LABELS[status]
}

export function blockedStageLabel(stage: string | null): string {
  return labelOf(BLOCKED_STAGE_LABELS, stage)
}

export function writebackReasonLabel(code: string | null): string {
  return labelOf(WRITEBACK_REASON_LABELS, code)
}

/**
 * 风险等级文案。`null`（还没审查出结果）显示为 `—`。
 *
 * ⚠️ **不能回落成"低"**：那让一份从未被审查的合同看起来是安全的。
 */
export function riskLevelLabel(level: RiskLevel | null): string {
  return level === null ? '—' : RISK_LEVEL_LABELS[level]
}
