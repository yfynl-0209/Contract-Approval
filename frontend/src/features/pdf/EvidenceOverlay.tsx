import type { EvidenceBox } from './evidenceBoxes'

/**
 * 证据高亮层（M8 Task 5）。
 *
 * ## 为什么每个框是一个 `<button>` 而不是一个 `<div>`
 *
 * 设计 §4.3 要求**双向**联动：点 PDF 上的框 → 选中对应字段。
 * 用 `div + onClick` 时这件事只有鼠标能做 —— 而键盘用户恰恰最需要它：
 * 他看不到框，只能靠读屏，读屏只能读到可聚焦元素。
 * 一个 `<button>` 顺带把"这是什么"（`aria-label`：字段名 + 精度）也说清了。
 *
 * ## 这一层不接收几何计算
 *
 * 入参已经是**视口坐标的矩形**（`EvidenceBox`）。层里再算一次坐标，
 * 就会出现两份几何实现 —— 而它们的分叉表现为"框偏了几像素"，
 * 谁也不会去查。
 */
export function EvidenceOverlay({
  boxes,
  onSelect,
}: {
  readonly boxes: readonly EvidenceBox[]
  readonly onSelect: (fieldCode: string) => void
}): JSX.Element {
  return (
    <div
      data-testid="evidence-overlay"
      style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}
    >
      {boxes.map((box) => (
        <button
          key={box.key}
          type="button"
          data-field-code={box.fieldCode}
          data-selected={box.selected ? 'true' : 'false'}
          aria-label={`${box.label}（${box.precision}）`}
          aria-pressed={box.selected}
          title={box.text}
          onClick={() => onSelect(box.fieldCode)}
          style={{
            position: 'absolute',
            left: `${box.rect.x}px`,
            top: `${box.rect.y}px`,
            width: `${box.rect.width}px`,
            height: `${box.rect.height}px`,
            // 容器设为穿透，框自己必须收回来 —— 否则点不到任何框
            pointerEvents: 'auto',
            padding: 0,
            border: box.selected
              ? '2px solid var(--accent)'
              : '1px solid var(--status-warn)',
            background: box.selected
              ? 'rgba(37, 99, 235, 0.22)'
              : 'rgba(217, 119, 6, 0.16)',
            borderRadius: '2px',
            cursor: 'pointer',
          }}
        />
      ))}
    </div>
  )
}
