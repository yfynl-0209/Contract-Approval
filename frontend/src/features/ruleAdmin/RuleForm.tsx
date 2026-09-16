import { useState } from 'react'

import { asApiError } from '../../api/client'
import type { ApiError } from '../../api/client'
import type { MatchMode, RiskLevel, RuleRow } from '../../api/contracts'
import {
  describeRuleFailure,
  diffDraft,
  draftFrom,
  MATCH_MODE_LABELS,
  patchBodyFor,
  REQUIRED_RULE_CATEGORIES,
  RISK_OPTIONS,
  RULE_STATUS_LABELS,
  type RuleDraft,
} from './ruleEdit'
import { useCreateRule, useUpdateRule } from './queries'
import { riskLevelLabel } from '../../domain/labels'

/**
 * 规则的新建 / 编辑表单（M8 Task 8）。
 *
 * ## 版本化的口径由后端裁决，界面只**说清后果**
 *
 * 改判定语义而不升版本，可能被 409 `RULE_VERSION_IN_USE` 拒
 * （该版本已被审查引用过时）；也可能被允许（从未被引用）。
 * 后者只有后端知道（`rule_hits` 里有没有同版本的评价行，前端查不到）。
 * 因此界面在**保存前**提示"这个改动需要升版本"，提交后按后端的裁决呈现。
 *
 * ## JSON 字段不做前端校验
 *
 * `applies_when_json` / `fallback_match_json` 的合法性判据在
 * `app/rules/validation.py`（与命令行 `check_rules.py` 同一份实现）。
 * 前端再写一遍会得到第二份会漂移的判据 —— 而漂移方向几乎必然是
 * "前端放过了，后端 400"或"前端拦住了合法配置"。
 * 这里只提供原文编辑，非法时把后端**逐条**的问题原样显示。
 */
export function RuleForm({
  mode,
  rule,
  onClose,
}: {
  readonly mode: 'create' | 'edit'
  /** `edit` 模式必填（要对照出"改了什么"） */
  readonly rule?: RuleRow
  readonly onClose: () => void
}): JSX.Element {
  const [draft, setDraft] = useState<RuleDraft>(() =>
    rule === undefined
      ? {
          rule_name: '',
          rule_category: null,
          risk_level: 'medium',
          priority: 100,
          match_mode: 'keyword',
          match_text: '',
          applies_when_json: null,
          fallback_match_json: null,
          exclude_text: null,
          suggestion_text: null,
          rule_version: 1,
          rule_status: 'active',
        }
      : draftFrom(rule),
  )
  const [ruleCode, setRuleCode] = useState('')

  const create = useCreateRule()
  const update = useUpdateRule()
  const pending = create.isPending || update.isPending
  const failure: ApiError | null =
    create.isError ? asApiError(create.error) : update.isError ? asApiError(update.error) : null

  const change =
    mode === 'edit' && rule !== undefined ? diffDraft(rule, draft) : null

  const submit = (): void => {
    if (mode === 'create') {
      create.mutate(
        {
          rule_code: ruleCode,
          rule_name: draft.rule_name,
          match_mode: draft.match_mode,
          match_text: draft.match_text,
          risk_level: draft.risk_level,
          priority: draft.priority,
          rule_version: draft.rule_version,
          rule_status: draft.rule_status,
          // 可选字段：空串不发送（让它们保持默认 null）——
          // 传 `""` 会被后端当"提供了空值"，而语义是"没提供"
          ...(draft.rule_category === null || draft.rule_category === ''
            ? {}
            : { rule_category: draft.rule_category }),
          ...(draft.applies_when_json === null || draft.applies_when_json === ''
            ? {}
            : { applies_when_json: draft.applies_when_json }),
          ...(draft.fallback_match_json === null || draft.fallback_match_json === ''
            ? {}
            : { fallback_match_json: draft.fallback_match_json }),
          ...(draft.exclude_text === null || draft.exclude_text === ''
            ? {}
            : { exclude_text: draft.exclude_text }),
          ...(draft.suggestion_text === null || draft.suggestion_text === ''
            ? {}
            : { suggestion_text: draft.suggestion_text }),
        },
        { onSuccess: onClose },
      )
      return
    }
    if (rule === undefined) {
      return
    }
    update.mutate(
      { ruleCode: rule.rule_code, body: patchBodyFor(rule, draft) },
      { onSuccess: onClose },
    )
  }

  const inputStyle = { width: '100%' } as const

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        submit()
      }}
      style={{
        border: '1px solid var(--border)',
        padding: 'var(--space-3)',
        marginBottom: 'var(--space-3)',
        background: 'var(--surface-1)',
      }}
    >
      <h3 style={{ fontSize: 'var(--font-size-sm)', marginTop: 0 }}>
        {mode === 'create' ? '新建规则' : `修改 ${rule?.rule_code}`}
      </h3>

      {mode === 'create' && (
        <label style={{ display: 'block', marginBottom: 'var(--space-2)' }}>
          规则码（rule_code，建好之后**不能改名**）
          <input
            required
            value={ruleCode}
            onChange={(event) => setRuleCode(event.target.value)}
            style={inputStyle}
          />
        </label>
      )}

      <label style={{ display: 'block', marginBottom: 'var(--space-2)' }}>
        规则名
        <input
          required
          value={draft.rule_name}
          onChange={(event) => setDraft({ ...draft, rule_name: event.target.value })}
          style={inputStyle}
        />
      </label>

      <div style={{ display: 'flex', gap: 'var(--space-2)', flexWrap: 'wrap', marginBottom: 'var(--space-2)' }}>
        <label>
          匹配模式
          <select
            value={draft.match_mode}
            onChange={(event) =>
              setDraft({ ...draft, match_mode: event.target.value as MatchMode })
            }
          >
            {(Object.keys(MATCH_MODE_LABELS) as MatchMode[]).map((mode_) => (
              <option key={mode_} value={mode_}>
                {MATCH_MODE_LABELS[mode_]}
              </option>
            ))}
          </select>
        </label>

        <label>
          风险等级
          <select
            value={draft.risk_level}
            onChange={(event) =>
              setDraft({ ...draft, risk_level: event.target.value as RiskLevel })
            }
          >
            {RISK_OPTIONS.map((level) => (
              <option key={level} value={level}>
                {riskLevelLabel(level)}
              </option>
            ))}
          </select>
        </label>

        <label>
          类别
          <select
            value={draft.rule_category ?? ''}
            onChange={(event) =>
              setDraft({ ...draft, rule_category: event.target.value === '' ? null : event.target.value })
            }
          >
            <option value="">（全局 / 未分类）</option>
            {REQUIRED_RULE_CATEGORIES.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>

        <label>
          优先级（数字小的先执行）
          <input
            type="number"
            min={0}
            value={draft.priority}
            onChange={(event) =>
              setDraft({ ...draft, priority: Number.parseInt(event.target.value, 10) || 0 })
            }
          />
        </label>

        <label>
          状态
          <select
            value={draft.rule_status}
            onChange={(event) =>
              setDraft({ ...draft, rule_status: event.target.value as RuleRow['rule_status'] })
            }
          >
            {(Object.keys(RULE_STATUS_LABELS) as RuleRow['rule_status'][]).map((status) => (
              <option key={status} value={status}>
                {RULE_STATUS_LABELS[status]}
              </option>
            ))}
          </select>
        </label>

        {mode === 'edit' && (
          <label>
            规则版本
            <input
              type="number"
              min={1}
              value={draft.rule_version}
              onChange={(event) =>
                setDraft({
                  ...draft,
                  rule_version: Number.parseInt(event.target.value, 10) || 1,
                })
              }
            />
          </label>
        )}
      </div>

      <label style={{ display: 'block', marginBottom: 'var(--space-2)' }}>
        匹配内容（{draft.match_mode === 'expr' ? '字段比较表达式 JSON' : '关键词 / 正则'}）
        <textarea
          required
          rows={3}
          value={draft.match_text}
          onChange={(event) => setDraft({ ...draft, match_text: event.target.value })}
          style={inputStyle}
        />
      </label>

      <label style={{ display: 'block', marginBottom: 'var(--space-2)' }}>
        适用条件 JSON（留空 = 全局适用）
        <textarea
          rows={2}
          value={draft.applies_when_json ?? ''}
          onChange={(event) =>
            setDraft({
              ...draft,
              applies_when_json: event.target.value === '' ? null : event.target.value,
            })
          }
          style={inputStyle}
        />
      </label>

      {draft.match_mode === 'llm' && (
        // 后端要求 llm 规则必须给显式降级条件（无模型时返回 needs_review 是不够的）
        <label style={{ display: 'block', marginBottom: 'var(--space-2)' }}>
          降级条件 JSON（llm 规则**必填**：无模型时的确定性判据）
          <textarea
            rows={2}
            value={draft.fallback_match_json ?? ''}
            onChange={(event) =>
              setDraft({
                ...draft,
                fallback_match_json: event.target.value === '' ? null : event.target.value,
              })
            }
            style={inputStyle}
          />
        </label>
      )}

      <label style={{ display: 'block', marginBottom: 'var(--space-2)' }}>
        排除词（否定词表；缺失类规则**不能**配置它）
        <input
          value={draft.exclude_text ?? ''}
          onChange={(event) =>
            setDraft({ ...draft, exclude_text: event.target.value === '' ? null : event.target.value })
          }
          style={inputStyle}
        />
      </label>

      <label style={{ display: 'block', marginBottom: 'var(--space-2)' }}>
        修改建议（命中时给人看的下一步）
        <input
          value={draft.suggestion_text ?? ''}
          onChange={(event) =>
            setDraft({
              ...draft,
              suggestion_text: event.target.value === '' ? null : event.target.value,
            })
          }
          style={inputStyle}
        />
      </label>

      {change !== null && change.contentChanges.length > 0 && (
        <p
          role="status"
          style={{
            fontSize: 'var(--font-size-xs)',
            color: change.needsVersionBump ? 'var(--status-warn)' : 'var(--text-2)',
          }}
        >
          将修改：{change.contentChanges.join('、')}
          {change.needsVersionBump
            ? ' —— ⚠️ 这些是判定语义；若当前版本已被审查引用过，保存会被拒。请把「规则版本」加 1（未被引用过时允许就地改）。'
            : ''}
        </p>
      )}

      {failure !== null && <RuleFailure error={failure} />}

      <div style={{ display: 'flex', gap: 'var(--space-2)', marginTop: 'var(--space-2)' }}>
        <button type="submit" disabled={pending}>
          {pending ? '保存中…' : mode === 'create' ? '创建' : '保存修改'}
        </button>
        <button type="button" onClick={onClose} disabled={pending}>
          取消
        </button>
      </div>
    </form>
  )
}

/** 规则接口失败的三段式。`RULE_CONFIG_INVALID` 的**逐条问题原文**必须完整显示。 */
function RuleFailure({ error }: { readonly error: ApiError }): JSX.Element {
  const description = describeRuleFailure(error)
  return (
    <div
      role="alert"
      style={{
        borderLeft: '4px solid var(--status-danger)',
        padding: 'var(--space-2)',
        marginBottom: 'var(--space-2)',
      }}
    >
      <strong>{description.summary}</strong>
      <p style={{ whiteSpace: 'pre-wrap', color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>
        {description.detail}
      </p>
      <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-sm)' }}>{description.action}</p>
    </div>
  )
}
