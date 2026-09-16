import { useState, type FormEvent } from 'react'

import { describeApiError } from '../api/client'
import { useAuth } from './authContext'

/**
 * 身份面板（M8 Task 2）。
 *
 * ## 为什么"当前身份"必须一直在界面上
 *
 * 一份合同的审查结论要进审批流，而"谁确认的"是审计账里的关键字段。
 * 开发期身份来自请求头、生产来自 SSO 会话 —— 两者都可能**不是你**：
 * 共用测试机上前一个人留下的身份、代理注入的默认身份、会话过期后的匿名状态。
 * 界面不显示"你现在是谁"，用户就会在自己以为是自己的身份下做决定。
 *
 * ## 开发期表单只出现在 `import.meta.env.DEV`
 *
 * 生产构建里这段表单会被摇掉，绝不存在"线上也能手填一个管理员身份"的路径 ——
 * 那正是 `AUTH_MODE=dev` 被 `assert_auth_configuration` 拦住的那件事，
 * 前端不该给它开第二个入口。
 */
export function IdentityPanel(): JSX.Element {
  const { status, identity, devIdentity, error, signIn, signOut, refresh } = useAuth()

  if (status === 'unauthenticated') {
    return <SignInForm onSubmit={signIn} />
  }

  if (status === 'loading') {
    return <span className="actor-card-muted">正在确认身份…</span>
  }

  if (status === 'error') {
    // `status === 'error'` 时 `error` 必非空（见 `AuthProvider` 的推导），
    // 这里仍显式判一次：拿不到具体错误时也要说清"该做什么"，
    // 而不是渲染一个没有下文的红色字。
    const description =
      error === null
        ? { summary: '身份查询失败', action: '稍后重试。' }
        : describeApiError(error)
    return (
      <div role="alert" className="actor-stack">
        <span style={alertStyle}>{description.summary}</span>
        <span className="actor-card-muted">{description.action}</span>
        <button type="button" onClick={refresh}>
          重试
        </button>
        {/*
          ⚠️ 必须有回到表单的出口：身份存的是 sessionStorage（按源隔离），
          换一个 origin（localhost ↔ 127.0.0.1）旧身份就不在了——
          只给"重试"会让用户卡在一个重复同一失败的循环里。
        */}
        <button type="button" onClick={signOut}>
          重新填写身份
        </button>
      </div>
    )
  }

  const unknown = identity?.unknown_roles ?? []

  return (
    /*
     * 竖排（用户反馈）：侧边栏窄，横排会把名字、权限、退出挤成一行看不清。
     * 名字独占一行（白字最大），actor_id / 权限数各占一行（次级色），退出按钮在最下。
     */
    <div className="actor-stack">
      <div>
        <div className="actor-name" title={identity?.actor_id}>
          {identity?.display_name ?? identity?.actor_id}
        </div>
        <div className="actor-role actor-card-muted" title={identity?.actor_id}>
          {identity?.actor_id}
        </div>
      </div>
      <div className="actor-row">
        <span className="actor-card-muted">
          权限 {identity?.permissions.length ?? 0} 项
        </span>
        {/*
          ⚠️ 未识别角色**必须显示**：IdP 先上了新角色、本系统还没发布映射时，
          该角色不带来任何权限（fail-closed），用户只会看到"莫名其妙少了一堆入口"。
          这一行是唯一的线索。
        */}
        {unknown.length > 0 && (
          <span role="status" style={alertStyle} title={unknown.join('、')}>
            {unknown.length} 个角色未被识别
          </span>
        )}
      </div>
      {import.meta.env.DEV && (
        <button type="button" onClick={signOut}>
          退出（换一个身份）
        </button>
      )}
      {devIdentity === null && (
        <span className="actor-card-muted">（身份来自登录会话）</span>
      )}
    </div>
  )
}

interface SignInFormProps {
  readonly onSubmit: (identity: {
    actorId: string
    displayName: string
    roles: readonly string[]
  }) => void
}

/**
 * 开发期身份表单。
 *
 * `roles` 是**逗号分隔的角色声明**，与 `X-Actor-Roles` 的格式一致 ——
 * 让人能本地模拟"只读审计看到什么"，而不用去改配置或重启服务。
 * 角色→权限的展开仍在服务端（前端不推），所以这里填什么角色都不构成提权：
 * 服务端只认识三个角色，其余一律不授予权限。
 */
function SignInForm({ onSubmit }: SignInFormProps): JSX.Element {
  const [actorId, setActorId] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [roles, setRoles] = useState('legal_reviewer')

  const handleSubmit = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault()
    if (actorId.trim() === '') {
      return
    }
    onSubmit({
      actorId: actorId.trim(),
      displayName: displayName.trim(),
      roles: roles
        .split(',')
        .map((role) => role.trim())
        .filter((role) => role !== ''),
    })
  }

  return (
    /*
     * 竖排表单：label 在上、输入框独占整行（宽 232px 的侧边栏里横排
     * 会把输入框压到看不清自己打了什么）。
     */
    <form onSubmit={handleSubmit} aria-label="开发期身份">
      <span style={alertStyle}>尚未提供身份</span>
      <label>
        actor_id
        <input
          value={actorId}
          onChange={(event) => setActorId(event.target.value)}
          placeholder="li-hua"
          required
        />
      </label>
      <label>
        显示名（ASCII）
        <input
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
          placeholder="Li Hua"
        />
      </label>
      <label>
        角色（逗号分隔）
        <input
          value={roles}
          onChange={(event) => setRoles(event.target.value)}
          placeholder="legal_reviewer,system_admin"
        />
      </label>
      <button type="submit">使用该身份</button>
    </form>
  )
}

const alertStyle = { color: '#f5dcb8', fontSize: 'var(--font-size-xs)' } as const
