import type { TaskView } from '../../api/contracts'
import {
  RISK_LEVEL_LABELS,
  TASK_STATUS_LABELS,
  WRITE_STATUS_LABELS,
  blockedStageLabel,
  riskLevelLabel,
  writebackReasonLabel,
} from '../../domain/labels'
import { Link } from 'react-router-dom'
import { routes } from '../../app/routes'

/**
 * 列表的三种单元格（M8 Task 3）。
 *
 * ## 为什么它们在一个文件里
 *
 * 三个单元格共用同一份输入（`TaskView`）与同一条纪律：**状态与原因分两处呈现**
 * （材料 §5.1）。拆成三个文件时，"只显示状态"这种退化会出现在其中一个里，
 * 而另外两个看起来是对的。
 *
 * ## ⚠️ 本文件最重要的两条约定
 *
 * 1. **`not_written` 不等于"还没轮到"**（`TaskWritebackCell`）：
 *    它可能是"门禁明确拒绝"，两者对用户意味着完全不同的动作。
 * 2. **没审过不等于低风险**（`TaskRiskCell`）：`null` 显示 `—`，
 *    回落成"低"会让一份从未被审查的合同看起来是安全的。
 */

/** 业务状态 + **阻塞原因内联**（不是藏在 tooltip 里）。 */
export function TaskStatusCell({ task }: { readonly task: TaskView }): JSX.Element {
  const blocked = task.task_status === 'blocked'

  return (
    <div>
      <span
        style={{
          color: blocked ? 'var(--status-danger)' : 'var(--text-1)',
          fontWeight: blocked ? 600 : 400,
        }}
      >
        {/* 颜色只是辅助：文字标签本身说明了状态（不许只靠颜色表达） */}
        {blocked ? '⚠ ' : ''}
        {TASK_STATUS_LABELS[task.task_status]}
      </span>

      {blocked && (
        // 三件套同屏：卡在哪一步 + 稳定错误码 + 人读原因。
        // 只看"阻塞"两个字，用户必须点进去才知道该找谁 —— 而很多人不会点。
        <div style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-xs)' }}>
          阻塞于「{blockedStageLabel(task.blocked_stage)}」
          {task.last_error_code !== null && (
            <>
              {' · '}
              <code>{task.last_error_code}</code>
            </>
          )}
          {task.block_reason !== null && <> · {task.block_reason}</>}
        </div>
      )}
    </div>
  )
}

/**
 * 总风险等级。
 *
 * `null`（还没审查出结果）显示 `—`，并在无障碍名里说明原因 ——
 * 只看到一个"横线"的人（尤其是读屏用户）需要知道它是"未审查"而不是"没有风险"。
 */
export function TaskRiskCell({ task }: { readonly task: TaskView }): JSX.Element {
  const level = task.overall_risk_level

  if (level === null) {
    return (
      <span
        title="尚未审查出结果"
        aria-label="总风险：尚未审查出结果"
        style={{ color: 'var(--text-3)' }}
      >
        —
      </span>
    )
  }

  return (
    <span aria-label={`总风险：${RISK_LEVEL_LABELS[level]}`}>
      {riskLevelLabel(level)}
    </span>
  )
}

/**
 * 回写列：**状态与原因分两段**（§5.1）。
 *
 * 只显示状态时，`写失败` 与 `未回写（被门禁拒绝）` 长得一样，
 * 而处置完全相反：
 *
 * | 情形 | 该做什么 |
 * | --- | --- |
 * | 外部故障（`failed` + `APPROVAL_API_ERROR`） | **等/重试** |
 * | 门禁拒绝（`not_written` + `MANUAL_CONFIRM_REQUIRED`） | 去**确认** —— 重试永远是白试 |
 */
export function TaskWritebackCell({ task }: { readonly task: TaskView }): JSX.Element {
  const { writeback } = task
  const reasonCode = writeback.latest_reason_code

  return (
    <div>
      <span>{WRITE_STATUS_LABELS[writeback.task_write_status]}</span>
      {reasonCode !== null && (
        <div
          style={{
            // 门禁拒绝是**中性提示**（不是错误）：它说的是"还差一个前置动作"，
            // 而不是"系统坏了"。渲染成红色会让用户去报障。
            color: writeback.latest_attempt_rejected
              ? 'var(--status-warn)'
              : 'var(--status-danger)',
            fontSize: 'var(--font-size-xs)',
          }}
        >
          {writeback.latest_attempt_no !== null && (
            <>第 {writeback.latest_attempt_no} 次：</>
          )}
          {writebackReasonLabel(reasonCode)}
        </div>
      )}
    </div>
  )
}

/** 编号格子：唯一可点进详情的入口（键盘可达的是它，不是整行）。 */
export function TaskCodeCell({
  task,
  onKeyDown,
}: {
  readonly task: TaskView
  /** 上下箭头在行链接之间移动焦点（监听器在这里 —— 表格不是交互元素）。 */
  readonly onKeyDown: (event: React.KeyboardEvent<HTMLAnchorElement>) => void
}): JSX.Element {
  return (
    <Link
      to={routes.task(task.task_id)}
      data-row-link={task.task_id}
      onKeyDown={onKeyDown}
      style={{ fontFamily: 'var(--font-mono)' }}
    >
      {task.approval_code}
    </Link>
  )
}
