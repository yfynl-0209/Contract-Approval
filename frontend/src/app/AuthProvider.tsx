import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'

import { ApiError, api, setIdentityHeaderProvider, shouldRetryQuery } from '../api/client'
import type { ActorIdentity, Permission } from '../api/contracts'
import {
  buildDevIdentityHeaders,
  envDefaultIdentity,
  readStoredIdentity,
  writeDevIdentityCookie,
  writeStoredIdentity,
  type DevIdentity,
} from '../api/identity'
import { queryKeys, STALE_TIME } from '../api/queryKeys'
import { AuthContext, type AuthContextValue, type AuthStatus } from './authContext'

/**
 * 身份与权限（M8 Task 2）。
 *
 * ## 权限只有一个来源：服务端的 `GET /api/me`
 *
 * 前端**不解析令牌、不维护角色→权限映射**。理由不是"省事"，而是：
 * 那样映射就有两份实现，两份漂移时的表现是"入口在、点了 403"
 * （或反过来"有权限却看不到入口"），而**两边都不报错**。
 *
 * `can()` 因此只是在服务端给我的权限列表里查一下，不是判断。
 *
 * ## 开发期身份
 *
 * `AUTH_MODE=dev` 下身份来自请求头，因此本地需要有个地方填；
 * 存储与头的形状在 `api/identity.ts`（那里也是"显示名只发 ASCII"的理由所在）。
 */

interface AuthProviderProps {
  readonly children: ReactNode
}

export function AuthProvider({ children }: AuthProviderProps): JSX.Element {
  const [devIdentity, setDevIdentity] = useState<DevIdentity | null>(
    () => readStoredIdentity() ?? envDefaultIdentity(),
  )

  /*
   * ⚠️ 身份头必须在**渲染期**注册，而不是等 useEffect：
   * 子组件的查询与这里的 effect 在同一次提交里，而 React 的 effect 顺序是
   * "子先父后"——一旦某个页面查询赶在注册之前发出，它就会**不带身份头**，
   * 得到 401"没有可信身份"，而且这个错误结果会被缓存住。
   * （M8 演示现场的真实故障：换了一个 origin 后身份消失，页面永远卡在
   * "无法连接服务 / 没有可信身份"两个错误之间。）
   * 卸载时的清理仍在 effect 里做。
   */
  useMemo(() => {
    setIdentityHeaderProvider(() => buildDevIdentityHeaders(devIdentity))
    return null
  }, [devIdentity])
  useEffect(() => {
    return () => {
      setIdentityHeaderProvider(() => ({}))
    }
  }, [])

  /*
   * ⚠️ Cookie 与请求头**并行维护**，且跟随身份状态（含挂载时的环境默认身份）：
   * 扩展的 fetch 钩子会丢 init.headers（M8 现场），Cookie 由浏览器自动携带、
   * 动不了——两条通道互为备份。退出时写 null 即清除。
   */
  useEffect(() => {
    writeDevIdentityCookie(devIdentity)
  }, [devIdentity])

  const query = useQuery({
    queryKey: queryKeys.me(),
    queryFn: () => api.get<ActorIdentity>('/api/me'),
    // 没有本地身份时不发请求：`AUTH_MODE=dev` 下它必然 401，
    // 发出去只会让界面在"未登录"与"错误"之间闪一下
    enabled: devIdentity !== null,
    retry: shouldRetryQuery,
    staleTime: STALE_TIME.detail,
  })

  const signIn = useCallback((identity: DevIdentity) => {
    writeStoredIdentity(identity)
    setDevIdentity(identity)
  }, [])

  const signOut = useCallback(() => {
    writeStoredIdentity(null)
    setDevIdentity(null)
  }, [])

  const refresh = useCallback(() => {
    void query.refetch()
  }, [query])

  /*
   * 身份变化后**全部重取**：旧身份下的失败（401 / 网络错误）与新身份无关，
   * 留着它们等于让用户看到"换对了身份界面却还是红的"。
   * ⚠️ 两个门槛：`devIdentity === null` 时跳过（`invalidateQueries` 会绕过
   * `enabled` 强制重取，会把本该禁用的 `/api/me` 也拉出去，401 后把
   * "去填身份"的表单顶成错误卡）；`--update` 见 mount 时也会跑一次——
   * 已有身份的首次加载重取一遍无害，反而修正了"查询先于身份头注册"的旧竞态。
   */
  const queryClient = useQueryClient()
  useEffect(() => {
    if (devIdentity === null) {
      return
    }
    void queryClient.invalidateQueries()
  }, [devIdentity, queryClient])

  const error = query.error instanceof ApiError ? query.error : null

  /**
   * 401 **不算错误**。
   *
   * 它的含义是"你给的这份身份服务端不认"，处置是**再填一次**，
   * 而不是"系统坏了"。渲染成红色故障会让用户去报障，
   * 而实际上只需要他把 actor_id 填对。
   */
  const unauthenticated = devIdentity === null || error?.isUnauthenticated === true

  const status: AuthStatus = unauthenticated
    ? 'unauthenticated'
    : query.isPending
      ? 'loading'
      : query.isError
        ? 'error'
        : 'ready'

  const identity = status === 'ready' ? (query.data ?? null) : null

  const value = useMemo<AuthContextValue>(() => {
    const granted = new Set<Permission>(identity?.permissions ?? [])
    return {
      status,
      identity,
      devIdentity,
      error,
      can: (...permissions: Permission[]): boolean =>
        permissions.length === 0
          ? identity !== null
          : permissions.every((permission) => granted.has(permission)),
      signIn,
      signOut,
      refresh,
    }
  }, [status, identity, devIdentity, error, signIn, signOut, refresh])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
