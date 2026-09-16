import { createContext, useContext } from 'react'

import type { ApiError } from '../api/client'
import type { ActorIdentity, Permission } from '../api/contracts'
import type { DevIdentity } from '../api/identity'

/**
 * 身份上下文与消费入口（M8 Task 2）。
 *
 * 单独一个文件而不是和 `AuthProvider` 放一起：`react-refresh` 只在
 * "文件只导出组件"时能热更新，而 Provider 文件里一旦还导出 hook，
 * 改一行就整页刷新、丢掉界面状态（与 `api/identity.ts` 同一理由）。
 */

export type AuthStatus = 'unauthenticated' | 'loading' | 'ready' | 'error'

export interface AuthContextValue {
  readonly status: AuthStatus
  /** 服务端确认的身份与**已展开的权限**；未就绪时为 `null` */
  readonly identity: ActorIdentity | null
  /** 开发期本地身份（生产为 `null`） */
  readonly devIdentity: DevIdentity | null
  /** 取身份失败时的错误（401 **不算**错误，见 `AuthProvider`） */
  readonly error: ApiError | null
  /**
   * 是否**同时**具备所列权限；无参数表示"已认证即可"。
   *
   * ⚠️ 语义与后端 `Actor.has` 一致（**全部满足**，不是满足其一）——
   * 写成"其一"时，`can('rule:manage', 'ops:retry')` 会被读成"或"，
   * 而调用方想说的几乎总是"且"。
   *
   * ⚠️ 它只用来决定"要不要显示入口"，**不是安全边界**（材料 §5.3）：
   * 返回真不代表操作一定成功（状态可能已变），返回假也挡不住手敲 URL。
   */
  readonly can: (...permissions: Permission[]) => boolean
  readonly signIn: (identity: DevIdentity) => void
  readonly signOut: () => void
  readonly refresh: () => void
}

export const AuthContext = createContext<AuthContextValue | null>(null)

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (value === null) {
    // 抛错而不是给一份"空权限"的默认值：后者会让忘记包 Provider 的页面
    // 静默地按"什么都没有权限"渲染 —— 那看起来像权限配错了。
    throw new Error('useAuth 必须在 <AuthProvider> 内使用')
  }
  return value
}
