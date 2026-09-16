/**
 * 规则编辑的判据与文案（纯函数，M8 Task 8）。
 *
 * ## 这里唯一"算"的东西：哪些字段的改动**必须同时提升版本**
 *
 * 后端的口径（`rule_admin_service`）：改了**判定语义**、版本又不变、而这个版本
 * **已经被审查引用过** → 409 `RULE_VERSION_IN_USE`。原因很硬：
 * 就地改内容会让"版本 N 的含义"被静默改写，而历史评价仍按版本 N 留痕。
 *
 * 界面因此要做的是**在保存前说清后果**，而不是替后端判定：
 * 提交时仍然由后端裁决（它才知道这个版本有没有被引用过 —— 前端查不到
 * `rule_hits`）。
 *
 * ⚠️ 字段清单**照抄** `rule_admin_service.CONTENT_FIELDS`
 * （= 可编辑字段减去 `rule_status` / `rule_version`）。
 * 抄漏一个的后果是：界面不提版本，保存得到 409，而用户不知道为什么。
 */

import type { ApiError } from '../../api/client'
import type { MatchMode, RiskLevel, RuleRow, RuleStatus } from '../../api/contracts'

/**
 * 改动会**改变判定语义**的字段（必须与后端 `CONTENT_FIELDS` 一致）。
 *
 * `rule_status`（启停用）与 `rule_version` **不在**其中：
 * 前者改变的是"这条规则参不参与本次评价"，而它本身就会让规则集版本变化。
 */
type ContentField =
  | 'rule_name'
  | 'rule_category'
  | 'risk_level'
  | 'priority'
  | 'match_mode'
  | 'match_text'
  | 'applies_when_json'
  | 'fallback_match_json'
  | 'exclude_text'
  | 'suggestion_text'

export const CONTENT_FIELDS: readonly ContentField[] = [
  'rule_name',
  'rule_category',
  'risk_level',
  'priority',
  'match_mode',
  'match_text',
  'applies_when_json',
  'fallback_match_json',
  'exclude_text',
  'suggestion_text',
]

/** 可编辑字段的中文名（界面与"改了哪些"提示共用一份）。 */
export const RULE_FIELD_LABELS: Readonly<Record<string, string>> = {
  rule_name: '规则名',
  rule_category: '类别',
  risk_level: '风险等级',
  priority: '优先级',
  rule_version: '规则版本',
  rule_status: '启停用',
  match_mode: '匹配模式',
  match_text: '匹配内容',
  applies_when_json: '适用条件',
  fallback_match_json: '降级条件',
  exclude_text: '排除词',
  suggestion_text: '修改建议',
}

/** 需求 2.4.6 的 11 类（与 `app/rules/validation.py::REQUIRED_CATEGORIES` 同源）。 */
export const REQUIRED_RULE_CATEGORIES: readonly string[] = [
  '预付款比例',
  '付款周期',
  '自动续约',
  '违约责任',
  '管辖地',
  '主体信息缺失',
  '金额缺失',
  '保密缺失',
  '数据处理',
  '知识产权',
  '验收标准缺失',
]

/**
 * 编辑表单的值。
 *
 * ⚠️ **显式列出**而不用映射类型推导：`CONTENT_FIELDS` 的元素类型是
 * `keyof RuleRow` 的并集，`Pick<RuleRow, …>` 推出来会把 `rule_id` / `rule_code`
 * 也带进草稿 —— 于是"草稿"里出现两个**不该被编辑**的标识，
 * 而 `draftFrom` 缺它们时在类型上反而成了错误。
 */
export interface RuleDraft {
  rule_name: string
  rule_category: string | null
  risk_level: RiskLevel
  priority: number
  match_mode: MatchMode
  match_text: string
  applies_when_json: string | null
  fallback_match_json: string | null
  exclude_text: string | null
  suggestion_text: string | null
  rule_version: number
  rule_status: RuleStatus
}

export function draftFrom(rule: RuleRow): RuleDraft {
  return {
    rule_name: rule.rule_name,
    rule_category: rule.rule_category,
    risk_level: rule.risk_level,
    priority: rule.priority,
    match_mode: rule.match_mode,
    match_text: rule.match_text,
    applies_when_json: rule.applies_when_json,
    fallback_match_json: rule.fallback_match_json,
    exclude_text: rule.exclude_text,
    suggestion_text: rule.suggestion_text,
    rule_version: rule.rule_version,
    rule_status: rule.rule_status,
  }
}

/** 值比较：`null` 与 `''` 视为不同（后端按"字段有没有出现"判，不按真假）。 */
function sameValue(left: unknown, right: unknown): boolean {
  return left === right
}

export interface DraftChange {
  /** 改了判定语义的字段（中文名） */
  readonly contentChanges: readonly string[]
  readonly statusChanged: boolean
  readonly versionChanged: boolean
  /** 改语义但**没有**提升版本 —— 保存可能被 409 拒（后端按是否被引用裁决） */
  readonly needsVersionBump: boolean
}

/**
 * 草稿相对原规则的改动。
 *
 * ⚠️ `needsVersionBump` 是**风险提示**，不是"一定会失败"：
 * 该版本从未被引用过时，后端允许就地改（首次使用前修正）。
 * 把它写成"必定失败"会让人以为必须先升版本，而升版本本身也是有代价的
 * （历史评价与新版本会分开）。
 */
export function diffDraft(rule: RuleRow, draft: RuleDraft): DraftChange {
  const contentChanges = CONTENT_FIELDS.filter(
    (field) => !sameValue(rule[field], draft[field]),
  ).map((field) => RULE_FIELD_LABELS[field] ?? field)

  const statusChanged = rule.rule_status !== draft.rule_status
  const versionChanged = rule.rule_version !== draft.rule_version

  return {
    contentChanges,
    statusChanged,
    versionChanged,
    needsVersionBump: contentChanges.length > 0 && !versionChanged,
  }
}

/** 提交用的请求体：**只带真正改过的字段**（后端按"显式提供"判，见 PATCH 说明）。 */
export function patchBodyFor(rule: RuleRow, draft: RuleDraft): Record<string, unknown> {
  const body: Record<string, unknown> = {}
  for (const field of CONTENT_FIELDS) {
    if (!sameValue(rule[field], draft[field])) {
      body[field] = draft[field]
    }
  }
  if (rule.rule_status !== draft.rule_status) {
    body['rule_status'] = draft.rule_status
  }
  if (rule.rule_version !== draft.rule_version) {
    body['rule_version'] = draft.rule_version
  }
  return body
}

export interface FailureText {
  readonly summary: string
  readonly detail: string
  readonly action: string
}

/**
 * 规则接口失败的分支（按 `error_code`）。
 *
 * ⚠️ `RULE_CONFIG_INVALID` 的 `message` 里是**逐条问题**（后端刻意一次给全，
 * 而不是第一个）—— 因此这里把原文**整体展示**，不截断、不改写。
 */
export function describeRuleFailure(error: ApiError): FailureText {
  switch (error.errorCode) {
    case 'RULE_NOT_FOUND':
      return {
        summary: '规则不存在',
        detail: '它可能已被删除，或 `rule_code` 拼错了。',
        action: '刷新列表后重试。重试不会成功。',
      }
    case 'RULE_VERSION_IN_USE':
      return {
        summary: '这个版本已被审查引用过，不能再就地修改',
        detail:
          '历史评价按"当时的版本"留痕；就地改内容会让那个版本的含义被静默改写。',
        action: '把「规则版本」加 1 再保存（新版本与历史记录分开）。',
      }
    case 'RULE_CONFIG_INVALID':
      return {
        summary: '规则配置未通过校验',
        // 原文保留：它逐条列出全部问题（含缺少的类别覆盖）
        detail: error.message,
        action: '按上面的问题逐条修正后重试。一个字段都不会被写入。',
      }
    case 'AUTHORIZATION_DENIED':
      return {
        summary: '你没有规则管理权限',
        detail: '规则是「系统怎么判」的输入，改一条会影响所有合同的结论。',
        action: '请系统管理员操作。',
      }
    default:
      return {
        summary: '规则操作失败',
        detail: error.message,
        action: error.retryable ? '稍后重试。' : '请联系管理员。',
      }
  }
}

/**
 * 11 类里**一条规则都没有**的类别。
 *
 * ⚠️ 只用于**显示**。判据的真正来源是后端的 `ruleset_problems`
 * （激活前校验会把缺失类别写进 400 的消息里）。两边都显示时，
 * 以激活前校验的结论为准 —— 这里是它的**提前提示**，不是替代。
 */
export function missingCategories(
  categories: Readonly<Record<string, number>>,
): readonly string[] {
  return REQUIRED_RULE_CATEGORIES.filter((name) => (categories[name] ?? 0) === 0)
}

/** 后端报告里出现过、但不在需求 11 类里的类别（如实显示，不隐藏）。 */
export function extraCategories(
  categories: Readonly<Record<string, number>>,
): readonly string[] {
  return Object.keys(categories)
    .filter((name) => !REQUIRED_RULE_CATEGORIES.includes(name))
    .sort()
}

export const MATCH_MODE_LABELS: Readonly<Record<MatchMode, string>> = {
  keyword: '关键词',
  regex: '正则',
  llm: '模型判定',
  expr: '数值比较',
}

export const RISK_OPTIONS: readonly RiskLevel[] = ['low', 'medium', 'high']

/** 规则状态的中文（启停用）。 */
export const RULE_STATUS_LABELS: Readonly<Record<RuleStatus, string>> = {
  active: '启用',
  inactive: '停用',
}
