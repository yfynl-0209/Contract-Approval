import type { AttachmentRow } from '../../api/contracts'
import { formatSize, shortDigest } from '../../domain/format'
import { DOWNLOAD_STATUS_LABELS, labelOf } from '../../domain/labels'

/**
 * 附件列表（M8 Task 4，设计 §4.2 第 3 块）。
 *
 * ## 每行给出什么
 *
 * 文件名、大小、类型、`download_status`、**SHA-256 前 12 位**、预览链接。
 * 摘要不是装饰：它是"我拿到的字节就是那一份"的唯一可核对凭据 ——
 * 附件被替换过时，只有摘要会变。
 *
 * ## 为什么"预览"是一个链接而不是按钮
 *
 * 内容由 `GET /api/attachments/{id}/content` 下发（支持 Range），
 * 用浏览器原生的打开方式即可。自己写一个 iframe 预览等于把 PDF 渲染
 * 也搬进这个页面，而模块 3 已经有专门的查看器 —— 两处渲染同一份文件时，
 * 证据高亮的坐标系只需要在其中一处对齐。
 *
 * ⚠️ **地址来自 `content_url`，不由前端拼路径**：存储实现换了（M9 的 MinIO）
 * 之后路径形态会变，而拼路径的代码不会跟着变。
 *
 * ## 失败原因的呈现
 *
 * 附件行只带一句人读 `error_message`，**原因码在任务级**
 * （`last_error_code`）。因此这里区分两点：
 * - 「外部事实」（附件在审批系统里就没了）与「我方故障」（存储抖动）；
 * - 前者该去找上传人，后者该等自动重试 —— 渲染成同一句话时，
 *   用户会对同一个词采取错误动作（§5.2）。
 *
 * ⚠️ "重新下载"按钮**不放**：控制台只调 `/api/*`，而重跑下载只有工具 3
 * （给外部系统的）。放一个点了没反应的按钮比不放更糟 —— 这一点在
 * 文本里说明，用户自己去走正确路径。
 */
export function AttachmentList({
  attachments,
  total,
  lastErrorCode,
  lastErrorIsBusinessFact,
}: {
  readonly attachments: readonly AttachmentRow[]
  readonly total: number
  readonly lastErrorCode: string | null
  readonly lastErrorIsBusinessFact: boolean
}): JSX.Element {
  if (attachments.length === 0) {
    return <p style={{ color: 'var(--text-2)' }}>这份合同还没有附件记录。</p>
  }

  return (
    <div>
      <table aria-label="附件" style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr style={{ background: 'var(--surface-2)', textAlign: 'left' }}>
            <th scope="col">文件名</th>
            <th scope="col">大小</th>
            <th scope="col">类型</th>
            <th scope="col">状态</th>
            <th scope="col">摘要（前 12 位）</th>
            <th scope="col">预览</th>
          </tr>
        </thead>
        <tbody>
          {attachments.map((row) => (
            <tr
              key={row.attachment_record_id}
              style={{ borderBottom: '1px solid var(--border)' }}
            >
              <td>
                {row.file_name}
                {row.download_status === 'failed' && (
                  <div
                    style={{
                      color: 'var(--text-2)',
                      fontSize: 'var(--font-size-xs)',
                    }}
                  >
                    {row.error_message ?? '下载失败'}
                    {lastErrorCode !== null && (
                      <>
                        {' · '}
                        <code>{lastErrorCode}</code>
                        {' · '}
                        {lastErrorIsBusinessFact
                          ? '审批系统侧的事实，请联系上传人处理'
                          : '我方下载故障，稍后会自动重试'}
                      </>
                    )}
                  </div>
                )}
              </td>
              <td>{formatSize(row.file_size)}</td>
              <td>{row.content_type ?? row.file_type ?? '—'}</td>
              <td>{labelOf(DOWNLOAD_STATUS_LABELS, row.download_status)}</td>
              <td>
                <code title={row.file_checksum ?? ''}>
                  {shortDigest(row.file_checksum)}
                </code>
              </td>
              <td>
                {row.download_status === 'success' ? (
                  <a
                    href={row.content_url}
                    target="_blank"
                    rel="noreferrer"
                    // 下载/预览都由后端鉴权；这里只是一个入口
                    aria-label={`预览 ${row.file_name}`}
                  >
                    预览
                  </a>
                ) : (
                  // 没有字节时不给链接：一个点了 404 的入口比没有更糟
                  <span style={{ color: 'var(--text-3)' }}>尚不可预览</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {total > attachments.length && (
        <p style={{ color: 'var(--text-2)', fontSize: 'var(--font-size-xs)' }}>
          共 {total} 个附件，当前显示前 {attachments.length} 个。
        </p>
      )}
    </div>
  )
}


