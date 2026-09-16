/**
 * 解析字段的读取与整理（纯函数，M8 Task 5）。
 *
 * ## 为什么要**运行时窄化**，而不是 `as FieldSet`
 *
 * `ParseRecord.basic_info` 的类型是 `JsonValue`（后端构造期有校验，
 * 但 TS 不校验运行时）。直接断言成 `FieldSet` 时，一旦结构不同
 * （旧 schema 版本、或后端改了形状），这里的 `fields.map` 会**静默**拿到
 * `undefined` —— 界面显示"没有字段"，而真相是"这份 JSON 不是我以为的形状"。
 *
 * 因此这里逐字段检查，认不出就返回 `null`，由界面如实说
 * "这份记录的结构不是当前版本能读的"。这同时是 `schema_version` 的用途。
 */

import type { ExtractedField, FieldSet, FieldStatus, JsonValue } from '../../api/contracts'
import { fieldLabel } from '../../domain/labels'
import { readBbox } from '../pdf/coordinateTransform'

export interface FieldRow {
  readonly fieldCode: string
  readonly label: string
  readonly field: ExtractedField
  /** 基本信息 / 条款 —— 两者在同一屏里分组显示 */
  readonly group: 'basic' | 'clause'
}

/** JSON → `FieldSet`；结构不符返回 `null`（**不抛错**：界面要能说明原因）。 */
export function readFieldSet(raw: JsonValue): FieldSet | null {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    return null
  }
  const fields = (raw as { readonly fields?: unknown }).fields
  if (!Array.isArray(fields)) {
    return null
  }

  const parsed: ExtractedField[] = []
  for (const item of fields) {
    const field = readField(item)
    if (field === null) {
      // 一个字段读不了就整体判为"读不了"：跳过它会让界面显示"字段缺了"，
      // 而那与"合同里没有这一项"在视觉上无法区分（正是本项目最忌讳的合并）
      return null
    }
    parsed.push(field)
  }

  const schemaVersion = (raw as { readonly schema_version?: unknown }).schema_version
  return {
    schema_version: typeof schemaVersion === 'number' ? schemaVersion : 1,
    fields: parsed,
  }
}

function readField(raw: unknown): ExtractedField | null {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
    return null
  }
  const item = raw as Record<string, unknown>
  const fieldCode = item['field_code']
  const status = item['status']
  if (typeof fieldCode !== 'string' || !isFieldStatus(status)) {
    return null
  }

  return {
    field_code: fieldCode,
    value_text: typeof item['value_text'] === 'string' ? item['value_text'] : '',
    value_decimal:
      typeof item['value_decimal'] === 'string' ? item['value_decimal'] : null,
    currency: typeof item['currency'] === 'string' ? item['currency'] : null,
    status,
    evidence: readEvidenceList(item['evidence']),
    reason_code: typeof item['reason_code'] === 'string' ? item['reason_code'] : null,
    reason_text: typeof item['reason_text'] === 'string' ? item['reason_text'] : null,
  }
}

const FIELD_STATUSES: readonly FieldStatus[] = [
  'extracted',
  'not_found',
  'uncertain',
  'failed',
]

function isFieldStatus(value: unknown): value is FieldStatus {
  return typeof value === 'string' && FIELD_STATUSES.includes(value as FieldStatus)
}

function readEvidenceList(raw: unknown): ExtractedField['evidence'] {
  if (!Array.isArray(raw)) {
    return []
  }
  const spans: ExtractedField['evidence'][number][] = []
  for (const item of raw) {
    if (item === null || typeof item !== 'object' || Array.isArray(item)) {
      continue
    }
    const span = item as Record<string, unknown>
    const page = span['page']
    const blockId = span['block_id']
    // 复用几何模块的窄化（**不在这里再写一遍四个数的检查**）：
    // 两处各写一遍时，一处接受倒序框、另一处拒绝，同一条证据会有两种命运
    const bbox = readBbox(span['bbox'])
    if (typeof page !== 'number' || typeof blockId !== 'string' || bbox === null) {
      // 证据读不了就**丢掉它**（而不是丢掉整个字段）：字段本身还有值，
      // 而界面会因为它"没有画得出的框"如实降级为文本定位
      continue
    }
    spans.push({
      page,
      block_id: blockId,
      text: typeof span['text'] === 'string' ? span['text'] : '',
      bbox,
      char_start: typeof span['char_start'] === 'number' ? span['char_start'] : 0,
      char_end: typeof span['char_end'] === 'number' ? span['char_end'] : 0,
      text_precision: span['text_precision'] === 'char' ? 'char' : span['text_precision'] === 'line' ? 'line' : 'none',
      bbox_precision:
        span['bbox_precision'] === 'char'
          ? 'char'
          : span['bbox_precision'] === 'line'
            ? 'line'
            : span['bbox_precision'] === 'block'
              ? 'block'
              : 'none',
    })
  }
  return spans
}

/**
 * 两份 `FieldSet` → 一屏可读的行。
 *
 * 顺序**保持不变**（后端按"需要人看的排前面"之类的语义排过），
 * 前端再排一次会让两处的排序口径开始竞争。
 */
export function toFieldRows(
  basic: FieldSet | null,
  clauses: FieldSet | null,
): readonly FieldRow[] {
  const rows: FieldRow[] = []
  for (const field of basic?.fields ?? []) {
    rows.push({ fieldCode: field.field_code, label: fieldLabel(field.field_code), field, group: 'basic' })
  }
  for (const field of clauses?.fields ?? []) {
    rows.push({ fieldCode: field.field_code, label: fieldLabel(field.field_code), field, group: 'clause' })
  }
  return rows
}

/** 值的显示形态：摘要/正文可能为空，此时说"没有值"而不是显示空白。 */
export function fieldDisplayValue(field: ExtractedField): string {
  if (field.value_text !== '') {
    return field.currency === null
      ? field.value_text
      : `${field.value_text} ${field.currency}`
  }
  if (field.value_decimal !== null) {
    return field.currency === null ? field.value_decimal : `${field.value_decimal} ${field.currency}`
  }
  return '—'
}
