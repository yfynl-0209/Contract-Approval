/**
 * 契约形状测试：前端类型化样本 vs 钉住的后端真实形状。
 *
 * ## 这条链是怎么闭上的
 *
 * `scripts/verify_m8.py` 用 TestClient 取**真实响应**，把每类对象的
 * **键集合**钉进本目录的 `apiShapes.json`（`--update` 重新生成）。
 * 本测试拿**同一份文件**去核这里的手写样本：
 *
 * - 样本用 `satisfies` 标注前端契约类型 → 前端类型**少字段/改字段名** → 这里编译失败；
 * - 样本的键集合必须与钉住形状**逐键一致** → 后端**加/删字段**而前端没跟
 *   （或没人重跑 `--update`）→ 这里运行时失败。
 *
 * 于是"契约漂移"被钉在两道日常门禁（typecheck / vitest）上，
 * 而不是靠某次联调时的人眼。
 *
 * ⚠️ 只比**键的结构**，不比值：值会随数据变，键才会随契约变。
 */

import { describe, expect, it } from 'vitest'

import shapes from './apiShapes.json'
import type {
  ActorIdentity,
  AggregateFocusPoint,
  AuditEventRow,
  DocumentPage,
  JobRecord,
  ParseRecord,
  ReviewResultRow,
  RuleRow,
  RunDetail,
  RunEvaluation,
  TaskDetail,
  TaskLogRow,
  TaskView,
  WritebackAttempt,
} from './contracts'

/** 递归取"对象形状"：叶子 → null；数组 → [首元素形状]；对象 → 逐键。与 verify_m8.py 同一算法。 */
function shapeOf(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.length > 0 ? [shapeOf(value[0])] : []
  }
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>).map(
      ([key, item]) => [key, shapeOf(item)] as const,
    )
    return Object.fromEntries(entries)
  }
  return null
}

// ============================================================
// 样本（值是任意的；**键**必须与后端逐字一致）
// ============================================================

const identity = {
  actor_id: 'r1',
  display_name: '审查员一',
  tenant_id: 'default',
  roles: ['system_admin'],
  unknown_roles: [],
  permissions: ['task:read'],
} satisfies ActorIdentity

const writeback = {
  task_write_status: 'not_written',
  latest_attempt_id: null,
  latest_attempt_no: null,
  latest_attempt_status: null,
  latest_attempt_at: null,
  // ⚠️ 后端在没有尝试时返回 **null**（曾按 boolean 建模，形状检查抓到的漂移）
  latest_attempt_rejected: null,
  latest_reason_code: null,
  latest_reason_text: null,
  status_url: '/api/writebacks/1',
} satisfies TaskView['writeback']

const taskRow = {
  task_id: 1,
  instance_id: 'HT-2026-0001',
  approval_code: 'HT-2026-0001',
  approval_title: '设备采购合同',
  applicant_name: '示例科技有限公司',
  task_status: 'reviewing',
  write_status: 'not_written',
  context_status: 'confirmed',
  context_source: 'approval_system',
  context_conflict: null,
  our_party_name: '示例科技有限公司',
  our_party_contract_label: 'party_a',
  our_party_business_role: 'buyer',
  contract_type: 'procurement',
  blocked_stage: null,
  block_reason: null,
  last_error_code: null,
  retry_count: 0,
  created_at: '2026-09-15T00:00:00Z',
  updated_at: '2026-09-15T00:00:00Z',
  status_url: '/api/tasks/1',
  overall_risk_level: null,
  attachment_count: 1,
  writeback,
} satisfies TaskView

const taskDetail = {
  ...taskRow,
  form_data: null,
  last_error_is_business_fact: false,
  overall_risk_level: 'low',
  latest_parse_id: 1,
  latest_run_id: 1,
  current_result_id: 1,
  correlation_id: 'corr-1',
  writeback: {
    ...writeback,
    latest_attempt_id: 1,
    latest_attempt_no: 1,
    latest_attempt_status: 'success',
    latest_attempt_at: '2026-09-15T00:00:03Z',
    latest_attempt_rejected: false,
    latest_reason_code: null,
    latest_reason_text: null,
  },
} satisfies TaskDetail

const documentPage = {
  page: 1,
  width: 400,
  height: 200,
  bbox_space: 'pdf-point-top-left',
  rotation: 0,
  source: 'text',
  page_status: 'ok',
  text: 'Procurement Contract',
  route_reason: null,
  error_code: null,
  char_map: [{ raw_start: 0, raw_end: 1 }],
  blocks: [
    {
      block_id: 'p1-b1',
      text: 'Procurement Contract',
      bbox: [20, 60, 200, 72],
      char_start: 0,
      char_end: 1,
      text_precision: 'char',
      bbox_precision: 'char',
      chars: [
        { text: 'P', bbox: [20, 60, 30, 72], char_start: 0, char_end: 1 },
      ],
    },
  ],
} satisfies DocumentPage

const parseRecord = {
  parse_id: 1,
  task_id: 1,
  attachment_record_id: 1,
  parse_status: 'succeeded',
  parse_version: 1,
  parser_name: 'pymupdf',
  parser_version: 'pymupdf-1.25.1',
  cache_key: 'k',
  parse_error_code: null,
  parse_error: null,
  basic_info: {
    schema_version: 1,
    fields: [
      {
        field_code: 'amount',
        value_text: '1,200,000.00',
        value_decimal: '1200000.00',
        currency: 'CNY',
        status: 'extracted',
        evidence: [
          {
            page: 1,
            block_id: 'p1-b1',
            text: 'Procurement',
            bbox: [20, 60, 200, 72],
            char_start: 0,
            char_end: 1,
            text_precision: 'char',
            bbox_precision: 'char',
          },
        ],
        reason_code: null,
        reason_text: null,
      },
    ],
  },
  clause_info: {
    schema_version: 1,
    fields: [
      {
        field_code: 'payment',
        value_text: '',
        value_decimal: null,
        currency: null,
        status: 'not_found',
        evidence: [],
        reason_code: null,
        reason_text: null,
      },
    ],
  },
  quality: { text_coverage: 0.9, ocr_pages: 0, ocr_confidence: null },
} satisfies ParseRecord

const runEvaluation = {
  rule_code: 'M8_HT-2026-0001',
  // ⚠️ 后端补的规则中文名（界面显示名字、悬停看编码）；查不到时为 null
  rule_name: '验收走查占位规则',
  evaluation_status: 'hit',
  risk_level: 'low',
  reason_code: null,
  reason_text: '命中关键词',
  evidence: [
    {
      text: 'Procurement Contract A',
      position: {
        page: 1,
        block_id: 'p1-b1',
        bbox: [20, 60, 200, 72],
        char_start: 0,
        char_end: 1,
        text_precision: 'char',
        bbox_precision: 'char',
      },
    },
  ],
  hit_detail: { found: ['Procurement'] },
} satisfies RunEvaluation

/** 聚合里至少要有一个关注点元素——空数组对漂移检查没有约束力。 */
const focusPoint = {
  rule_code: 'M8_HT-2026-0001',
  status: 'hit',
  risk_level: 'low',
  reason_code: null,
  reason_text: '命中关键词',
  evidence_text: 'Procurement Contract A',
} satisfies AggregateFocusPoint

const runDetail = {
  run_id: 1,
  task_id: 1,
  parse_id: 1,
  version_no: 1,
  run_status: 'completed',
  ruleset_version: 'v1',
  model_version: 'm1',
  prompt_version: 'p1',
  config_version: 'c1',
  aggregate: {
    overall_risk_level: 'low',
    review_status: 'complete',
    counts: { hit: 1, not_hit: 0, not_applicable: 0, needs_review: 0 },
    focus_points: [focusPoint],
    summary: '…',
  },
  evaluations: [runEvaluation],
  started_at: '2026-09-15T00:00:00Z',
  finished_at: '2026-09-15T00:00:01Z',
} satisfies RunDetail

const resultRow = {
  result_id: 1,
  task_id: 1,
  run_id: 1,
  version_no: 1,
  is_current_version: true,
  supersedes_result_id: null,
  overall_risk_level: 'low',
  review_status: 'complete',
  hit_count: 1,
  needs_review_count: 0,
  not_applicable_count: 0,
  focus_points: [],
  summary_text: '…',
  comment_text: '…',
  manual_confirmed: true,
  confirmation_valid: true,
  confirmed_by: 'r1',
  confirmed_at: '2026-09-15T00:00:02Z',
  confirmed_digest: 'd1',
  content_digest: 'd0',
  created_by: 'r1',
  created_at: '2026-09-15T00:00:01Z',
  updated_at: '2026-09-15T00:00:02Z',
  status_url: '/api/results/1',
} satisfies ReviewResultRow

const writebackAttempt = {
  attempt_id: 1,
  task_id: 1,
  result_id: 1,
  instance_id: 'HT-2026-0001',
  operator_name: null,
  write_status: 'success',
  reason_code: null,
  reason_text: null,
  content_digest: 'd0',
  attempt_no: 1,
  task_write_status: 'success',
  task_status: 'done',
  status_url: '/api/writebacks/1',
  created_at: '2026-09-15T00:00:03Z',
  delivery: {
    event_id: 1,
    event_type: 'writeback',
    event_status: 'delivered',
    attempt_no: 1,
    max_attempts: 3,
    next_retry_at: null,
    delivered_at: '2026-09-15T00:00:04Z',
    last_error_code: null,
    last_error_text: null,
    correlation_id: 'corr-1',
    created_at: '2026-09-15T00:00:03Z',
  },
} satisfies WritebackAttempt

const jobRecord = {
  job_id: 1,
  task_id: 1,
  job_type: 'rule',
  job_status: 'succeeded',
  attempt_no: 1,
  max_attempts: 3,
  next_retry_at: null,
  result_ref: { run_id: 1, result_url: '/api/runs/1' },
  status_url: '/api/jobs/1',
  correlation_id: 'corr-1',
  last_error_code: null,
  last_error_text: null,
} satisfies JobRecord

const ruleRow = {
  rule_id: 1,
  rule_code: 'M8_HT-2026-0001',
  rule_name: '验收走查占位规则',
  rule_category: '其他',
  risk_level: 'low',
  rule_status: 'active',
  priority: 999,
  rule_version: 1,
  match_mode: 'keyword',
  match_text: '{"keywords": ["Procurement"]}',
  applies_when_json: null,
  fallback_match_json: null,
  exclude_text: null,
  suggestion_text: null,
} satisfies RuleRow

const logRow = {
  log_id: 1,
  task_id: 1,
  log_level: 'info',
  log_type: 'parse',
  log_content: '…',
  error_code: null,
  correlation_id: 'corr-1',
  created_at: '2026-09-15T00:00:00Z',
} satisfies TaskLogRow

const auditRow = {
  event_id: 1,
  task_id: 1,
  actor_id: 'r1',
  actor_name: '审查员一',
  action: 'RESULT_CONFIRMED',
  target_type: 'review_result',
  target_id: '1',
  correlation_id: 'corr-1',
  // ⚠️ 回写送达事件的 detail 就是这四个键（verify_m8 走查钉住的真实形状）；
  // 前端把它当 JsonValue 读，但样本的键必须与真实响应一致才有约束力
  detail: { attempt_id: 1, external_comment_id: 'c-1', replayed: false, result_id: 1 },
  created_at: '2026-09-15T00:00:00Z',
} satisfies AuditEventRow

// ============================================================
// 断言：每个样本的键集合 == 钉住的后端形状
// ============================================================

describe('契约形状（apiShapes.json 由 scripts/verify_m8.py 钉住）', () => {
  const cases: ReadonlyArray<readonly [string, object]> = [
    ['me', identity],
    ['taskRow', taskRow],
    ['taskDetail', taskDetail],
    ['parseRecord', parseRecord],
    ['documentPage', documentPage],
    ['runDetail', runDetail],
    ['runAggregate', runDetail.aggregate],
    ['runEvaluation', runEvaluation],
    ['resultRow', resultRow],
    ['writebackAttempt', writebackAttempt],
    ['jobRecord', jobRecord],
    ['ruleRow', ruleRow],
    ['logRow', logRow],
    ['auditRow', auditRow],
  ]

  it.each(cases)('%s 的键集合与后端逐字一致', (name, sample) => {
    const pinned = shapes[name as keyof typeof shapes]
    expect(pinned, `apiShapes.json 里没有 ${name}（重跑 verify_m8.py --update）`).toBeDefined()
    expect(shapeOf(sample)).toEqual(pinned)
  })

  it('apiShapes.json 里没有未覆盖的形状（新增端点必须补样本）', () => {
    expect(Object.keys(shapes).sort()).toEqual(
      cases.map(([name]) => name).sort(),
    )
  })
})
