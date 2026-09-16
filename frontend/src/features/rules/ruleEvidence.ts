/**
 * 规则评价的读取与整理（纯函数，M8 Task 6）。
 *
 * ## 两处形状差异是**真的**，不是这里写错
 *
 * 1. **证据的嵌套**：解析字段的证据是**平铺**的 `EvidenceSpan`
 *    （`{page, block_id, bbox, …, text}`），而规则评价的证据是
 *    `{text, position: {page, block_id, bbox, …}}`（`app/rules/evidence.py::_position_of`）。
 *    两者内容一样、形状不同，因此**不能复用** `fields.ts` 的读取函数 ——
 *    复用时 `span.page` 会是 `undefined`，而画框时表现为
 *    "证据定位没反应"，看不出是形状问题。这里把它**规整成同一个 `EvidenceSpan`**，
 *    于是下游（PDF 跳转、证据框）只有一套几何类型。
 * 2. **评价行的字段集**：见 `contracts.ts::RunEvaluation` 的说明。
 *
 * ## 排序与折叠（设计 §4.4）
 *
 * 顺序固定 `needs_review → hit → not_hit → not_applicable`，
 * **`needs_review` 排最前**：它不是"没结论"，而是"结论需要你来做"，
 * 属于**待处理**项。前两组默认展开，后两组默认折叠但**显示计数**
 * （折叠不等于隐藏：用户要能一眼确认"系统确实评估了 N 条规则"）。
 */

import type {
  EvaluationStatus,
  EvidenceSpan,
  JsonValue,
  RiskLevel,
  RunEvaluation,
} from '../../api/contracts'
import { EVALUATION_STATUS_LABELS } from '../../domain/labels'
import { readBbox } from '../pdf/coordinateTransform'

/** 一条规则证据：原文片段 + 几何（几何可能缺失）。 */
export interface RuleEvidence {
  readonly text: string
  readonly span: EvidenceSpan | null
}

/** 设计 §4.4 的固定顺序。**唯一**一份，界面与测试都从这里取。 */
export const EVALUATION_ORDER: readonly EvaluationStatus[] = [
  'needs_review',
  'hit',
  'not_hit',
  'not_applicable',
]

/** 默认展开的两态。 */
export const DEFAULT_OPEN_STATUSES: readonly EvaluationStatus[] = ['needs_review', 'hit']

/** 风险等级的可排序权重（高 → 低）。 */
const RISK_RANK: Readonly<Record<RiskLevel, number>> = { high: 3, medium: 2, low: 1 }

export interface EvaluationGroup {
  readonly status: EvaluationStatus
  readonly label: string
  readonly items: readonly RunEvaluation[]
  readonly count: number
  readonly defaultOpen: boolean
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function readStatus(value: unknown): EvaluationStatus | null {
  return typeof value === 'string' &&
    (EVALUATION_ORDER as readonly string[]).includes(value)
    ? (value as EvaluationStatus)
    : null
}

function readRisk(value: unknown): RiskLevel | null {
  return value === 'low' || value === 'medium' || value === 'high' ? value : null
}

/**
 * 规则证据的**嵌套**形状 → `RuleEvidence`。
 *
 * 读不出几何时 `span` 为 `null`，界面降级为"只给原文片段"——
 * 而不是拿四个 `NaN` 去画框（`readBbox` 会把它们挡掉）。
 */
export function readRuleEvidence(raw: JsonValue): readonly RuleEvidence[] {
  if (!Array.isArray(raw)) {
    return []
  }
  const items: RuleEvidence[] = []
  for (const entry of raw) {
    if (!isObject(entry)) {
      continue
    }
    const text = typeof entry['text'] === 'string' ? entry['text'] : ''
    const position = entry['position']
    const span = isObject(position) ? readPosition(position) : null
    if (text === '' && span === null) {
      // 既没有文本也没有位置：这条"证据"什么都指不了，丢掉它比列一行空白诚实
      continue
    }
    items.push({ text, span })
  }
  return items
}

function readPosition(position: Record<string, unknown>): EvidenceSpan | null {
  const page = position['page']
  const blockId = position['block_id']
  const bbox = readBbox(position['bbox'])
  if (typeof page !== 'number' || typeof blockId !== 'string' || bbox === null) {
    return null
  }
  return {
    page,
    block_id: blockId,
    text: '',
    bbox,
    char_start: typeof position['char_start'] === 'number' ? position['char_start'] : 0,
    char_end: typeof position['char_end'] === 'number' ? position['char_end'] : 0,
    text_precision: readTextPrecision(position['text_precision']),
    bbox_precision: readBboxPrecision(position['bbox_precision']),
  }
}

function readTextPrecision(value: unknown): EvidenceSpan['text_precision'] {
  return value === 'char' || value === 'line' || value === 'none' ? value : 'none'
}

function readBboxPrecision(value: unknown): EvidenceSpan['bbox_precision'] {
  return value === 'char' || value === 'line' || value === 'block' || value === 'none'
    ? value
    : 'none'
}

/** `GET /api/runs/{id}.evaluations[]` → 可渲染的评价列表（读完不出来的整条丢掉）。 */
export function readRunEvaluations(raw: JsonValue): readonly RunEvaluation[] {
  if (!Array.isArray(raw)) {
    return []
  }
  const items: RunEvaluation[] = []
  for (const entry of raw) {
    if (!isObject(entry)) {
      continue
    }
    const ruleCode = entry['rule_code']
    const status = readStatus(entry['evaluation_status'])
    const risk = readRisk(entry['risk_level'])
    if (typeof ruleCode !== 'string' || status === null || risk === null) {
      // 状态读不出来时**不能猜**：猜 `not_hit` 会让一条命中消失，
      // 猜 `hit` 会造出一条假风险
      continue
    }
    items.push({
      rule_code: ruleCode,
      rule_name: typeof entry['rule_name'] === 'string' ? entry['rule_name'] : null,
      evaluation_status: status,
      risk_level: risk,
      reason_code: typeof entry['reason_code'] === 'string' ? entry['reason_code'] : null,
      reason_text: typeof entry['reason_text'] === 'string' ? entry['reason_text'] : null,
      evidence: Array.isArray(entry['evidence']) ? entry['evidence'] : [],
      hit_detail: (entry['hit_detail'] ?? null) as JsonValue,
    })
  }
  return items
}

/**
 * 按四态分组（组内按 `risk_level` 降序、同级别按 `rule_code` 升序）。
 *
 * **四个组都给**（包括 `count` 为 0 的）：少一组时，"系统评估过 `not_hit` 吗"
 * 这件事在界面上无法确认。
 */
export function groupEvaluations(
  items: readonly RunEvaluation[],
): readonly EvaluationGroup[] {
  return EVALUATION_ORDER.map((status) => {
    const groupItems = items
      .filter((item) => item.evaluation_status === status)
      .slice()
      .sort((left, right) => {
        const byRisk = RISK_RANK[right.risk_level] - RISK_RANK[left.risk_level]
        return byRisk !== 0 ? byRisk : left.rule_code.localeCompare(right.rule_code)
      })
    return {
      status,
      label: EVALUATION_STATUS_LABELS[status],
      items: groupItems,
      count: groupItems.length,
      defaultOpen: DEFAULT_OPEN_STATUSES.includes(status),
    }
  })
}

export interface CalculationFact {
  readonly label: string
  readonly value: string
}

export interface Calculation {
  /** 形如 `0.6 > 0.3`；没有可展示的比较时为空 */
  readonly expression: string | null
  /** 其余键的逐项说明（**不丢键**：未知键按原样列出） */
  readonly facts: readonly CalculationFact[]
}

/** 操作符 → 数学符号（`app/schemas.py::ExprOp` 的取值域）。 */
const OP_SYMBOLS: Readonly<Record<string, string>> = {
  gt: '>',
  gte: '≥',
  lt: '<',
  lte: '≤',
  eq: '=',
  ne: '≠',
  contains: '包含',
  is_null: '为空',
  not_null: '非空',
}

/** `hit_detail` 里的已知键 → 中文标签。未知键**原样列出**（见 `readCalculation` 说明）。 */
const DETAIL_LABELS: Readonly<Record<string, string>> = {
  status: '字段状态',
  present: '是否存在',
  op: '比较方式',
  actual: '实际值',
  actual_currency: '实际币种',
  expected_currency: '运行币种',
  threshold: '阈值',
  raw_value: '未解析的阈值',
  evidence_truncated: '证据被截断',
  evidence_limit: '证据条目上限',
  // M11：llm 规则的判定过程（app/rules/llm_judge.py 写入 hit_detail）
  claimed_quote: '模型给出的引用',
  judged_by: '判定来源',
  prompt_version: '提示词版本',
  verdict_discarded: '结论作废原因',
}

/** `status` 键的取值 → 中文（关键字匹配的查找结论）。 */
const DETAIL_VALUE_LABELS: Readonly<Record<string, string>> = {
  not_found: '未找到',
  found: '已找到',
  ambiguous: '存在多个候选',
}

/** `judged_by` 的取值 → 中文（这条结论是谁判的）。 */
const JUDGED_BY_LABELS: Readonly<Record<string, string>> = {
  llm: '模型',
}

/** `verdict_discarded` 的取值 → 中文（模型的结论为什么被作废）。 */
const DISCARD_REASON_LABELS: Readonly<Record<string, string>> = {
  evidence_not_found: '给出的引用不在合同正文里',
  matched_without_evidence: '判了命中但没有给出依据',
}

/**
 * `hit_detail` → 可复核的计算过程（设计 §4.4 第 3 条）。
 *
 * 审查人要能复核"`60% > 30%`"这个判断本身，而不是只接受结论。
 * 三条纪律：
 *
 * 1. **数值用后端给的字符串**（`Decimal` 序列化成字符串，见 `matching._match_numeric`）——
 *    转成 `number` 再格式化会把这份"记账数据"拖回二进制误差；
 * 2. **不丢键**：没登记的键按原样列出（`evidence_truncated` 这类就得让人看见）；
 * 3. **没有比较就不编**：`expression` 为空时界面只列事实项，
 *    而不是拼一句"计算过程：无"。
 */
export function readCalculation(hitDetail: JsonValue): Calculation {
  if (!isObject(hitDetail)) {
    return { expression: null, facts: [] }
  }

  const actual = formatScalar(hitDetail['actual'])
  const threshold = formatScalar(hitDetail['threshold'])
  const op = typeof hitDetail['op'] === 'string' ? hitDetail['op'] : null
  const symbol = op === null ? null : (OP_SYMBOLS[op] ?? op)

  const expression =
    actual !== null && threshold !== null && symbol !== null
      ? `${actual} ${symbol} ${threshold}`
      : null

  const facts: CalculationFact[] = []
  for (const [key, value] of Object.entries(hitDetail)) {
    if (key === 'actual' || key === 'threshold') {
      // 已经进了 `expression`（有表达式时）；没有表达式时按事实列出
      if (expression !== null) {
        continue
      }
    }
    if (key === 'op' && expression !== null) {
      continue
    }
    const formatted = formatFactValue(key, value) ?? formatJson(value)
    if (formatted === null) {
      continue
    }
    facts.push({ label: DETAIL_LABELS[key] ?? key, value: formatted })
  }
  return { expression, facts }

  /**
   * 已知键的**值**也翻译成人话：`op: gte` → `比较方式：≥`、
   * `status: not_found` → `字段状态：未找到`、`present: false` → `是否存在：否`、
   * `judged_by: llm` → `判定来源：模型`、`verdict_discarded: evidence_not_found`
   * → `结论作废原因：给出的引用不在合同正文里`。
   * 未登记的键/值原样保留（与标签表同一条"不撒谎"纪律）。
   */
  function formatFactValue(key: string, value: unknown): string | null {
    if (key === 'op' && typeof value === 'string') {
      return OP_SYMBOLS[value] ?? value
    }
    if (key === 'status' && typeof value === 'string') {
      return DETAIL_VALUE_LABELS[value] ?? value
    }
    if (key === 'judged_by' && typeof value === 'string') {
      return JUDGED_BY_LABELS[value] ?? value
    }
    if (key === 'verdict_discarded' && typeof value === 'string') {
      return DISCARD_REASON_LABELS[value] ?? value
    }
    if (key === 'present' && typeof value === 'boolean') {
      return value ? '是' : '否'
    }
    return formatScalar(value)
  }
}

function formatScalar(value: unknown): string | null {
  if (typeof value === 'string') {
    return value
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  return null
}

function formatJson(value: unknown): string | null {
  try {
    return JSON.stringify(value)
  } catch {
    return null
  }
}

export interface RuleFiltersState {
  /** 只看需处理（`needs_review` + `hit`） */
  readonly onlyActionable: boolean
  /** 规则码子串（大小写不敏感） */
  readonly query: string
}

/**
 * 客户端筛选（作用于**已加载**的那一批）。
 *
 * ⚠️ 它**不改变**"共 N 条"这个数字：界面必须同时显示已加载数与总数
 * （`run.evaluations` 整批返回时两者相等，但界面的写法不能依赖这一点）。
 */
export function filterEvaluations(
  items: readonly RunEvaluation[],
  filters: RuleFiltersState,
): readonly RunEvaluation[] {
  const needle = filters.query.trim().toLowerCase()
  return items.filter((item) => {
    if (filters.onlyActionable && !DEFAULT_OPEN_STATUSES.includes(item.evaluation_status)) {
      return false
    }
    // 名字与编码都参与匹配：界面显示的是名字，搜"预付"必须能找到
    if (
      needle !== '' &&
      !item.rule_code.toLowerCase().includes(needle) &&
      !(item.rule_name ?? '').toLowerCase().includes(needle)
    ) {
      return false
    }
    return true
  })
}

/**
 * 没有原文片段时的说明。
 *
 * `absent=true` 的缺失类命中**天然没有**可指的片段（结论是"全文都没有"），
 * 一条"没有知识产权条款"的命中是合法且重要的。因此这里给出**原因**，
 * 而不是留一片空白让人以为证据丢了。
 */
export function missingEvidenceNote(evaluation: RunEvaluation, evidenceCount: number): string | null {
  if (evidenceCount > 0) {
    return null
  }
  if (evaluation.evaluation_status === 'hit') {
    return '这条命中没有原文片段：结论是「全文都没有」，依据见上方的计算明细。'
  }
  return null
}
