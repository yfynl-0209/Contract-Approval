import { Link } from 'react-router-dom'

import type { RunEvaluation } from '../../api/contracts'
import { evidenceDeepLink } from '../../app/routes'
import { humanizeFieldCodes, reasonCodeLabel, riskLevelLabel } from '../../domain/labels'
import {
  missingEvidenceNote,
  readCalculation,
  readRuleEvidence,
} from './ruleEvidence'

/**
 * 一条规则评价（M8 Task 6）。
 *
 * ## ⚠️ `needs_review` **不给风险徽章**
 *
 * 每行都带 `risk_level`，但那是**规则配置的等级**，不是"已经发生的风险"。
 * `needs_review` 的含义恰恰是"判不了"（证据不足 / 上下文缺失）——
 * 给它配一个红色的「高」会让人把**待判断**读成**已确认的高风险**，
 * 这正是设计 §4.4 第 5 条要防的事（原因决定下一步动作，而不只是程度）。
 *
 * 因此：
 *
 * | 状态 | 显示 |
 * | --- | --- |
 * | `hit` | 风险徽章（规则命中，等级生效） |
 * | `needs_review` | **原因**（`reason_code` + `reason_text`），无风险徽章 |
 * | `not_hit` / `not_applicable` | 只有原因（"为什么没报警"） |
 */
export function EvaluationCard({
  evaluation,
  taskId,
}: {
  readonly evaluation: RunEvaluation
  readonly taskId: number
}): JSX.Element {
  const evidence = readRuleEvidence(evaluation.evidence)
  const calculation = readCalculation(evaluation.hit_detail)
  const missingNote = missingEvidenceNote(evaluation, evidence.length)
  const isHit = evaluation.evaluation_status === 'hit'

  return (
    <li
      data-rule-code={evaluation.rule_code}
      data-status={evaluation.evaluation_status}
      className="rule-item"
      style={{ '--rc': isHit ? riskColor(evaluation) : 'var(--border-strong)' } as React.CSSProperties}
    >
      <div className="rule-item-head">
        {/* ⚠️ 给人看的是**规则名**（`预付款比例过高`），编码（`PAY_PREPAY_…`）
            是给机器与排障的 —— 收进悬停。名字缺失（规则被删）时回退显示编码 */}
        <span className="name" title={evaluation.rule_code}>
          {evaluation.rule_name ?? evaluation.rule_code}
        </span>

        {isHit && (
          <span className="badge b-neutral" style={{ color: riskColor(evaluation) }}>
            {riskLevelLabel(evaluation.risk_level)}
          </span>
        )}

        {evaluation.reason_code !== null && (
          <span className="rule-reason" style={{ margin: 0 }}>
            <code title={evaluation.reason_code}>{reasonCodeLabel(evaluation.reason_code)}</code>
          </span>
        )}
      </div>

      {evaluation.reason_text !== null && (
        <p className="rule-reason">
          {/* reason_text 由后端模板拼出，内嵌的字段码（prepay_ratio）
              在这里翻译成中文名 —— 文案归前端，字段字典也归前端 */}
          {humanizeFieldCodes(evaluation.reason_text)}
        </p>
      )}

      {calculation.expression !== null && (
        <p data-testid="calculation" style={{ margin: 'var(--space-2) 0 0' }}>
          <span className="rule-calc">
            计算：<code>{calculation.expression}</code>
          </span>
        </p>
      )}

      {calculation.facts.length > 0 && (
        <ul style={{ margin: 'var(--space-2) 0 0', paddingLeft: '1.2em', fontSize: 'var(--font-size-xs)' }}>
          {calculation.facts.map((fact) => (
            <li key={fact.label}>
              {fact.label}：{fact.value}
            </li>
          ))}
        </ul>
      )}

      {evidence.length > 0 && (
        <ul data-testid="rule-evidence" className="rule-evidence">
          {evidence.map((item, index) => (
            <li key={`${evaluation.rule_code}:${index}`}>
              {item.text !== '' && <q>「{truncate(item.text, 60)}」</q>}
              {item.span === null ? (
                // 没有坐标就**不说"查看原文"** —— 那个链接落不到具体位置，
                // 用户点进去只会看到第 1 页并按"定位坏了"来理解
                <span style={{ color: 'var(--text-3)' }}> 这条证据没有坐标，无法在 PDF 中定位</span>
              ) : (
                <>
                  {' '}
                  <Link
                    to={evidenceDeepLink(taskId, {
                      page: item.span.page,
                      blockId: item.span.block_id,
                    })}
                  >
                    查看原文 → 第 {item.span.page} 页
                  </Link>
                </>
              )}
            </li>
          ))}
        </ul>
      )}

      {missingNote !== null && (
        <p style={{ margin: 'var(--space-2) 0 0', fontSize: 'var(--font-size-xs)', color: 'var(--text-2)' }}>
          {missingNote}
        </p>
      )}
    </li>
  )
}

function riskColor(evaluation: RunEvaluation): string {
  switch (evaluation.risk_level) {
    case 'high':
      return 'var(--status-danger)'
    case 'medium':
      return 'var(--status-warn)'
    default:
      return 'var(--status-ok)'
  }
}

function truncate(text: string, limit: number): string {
  return text.length <= limit ? text : `${text.slice(0, limit)}…`
}
