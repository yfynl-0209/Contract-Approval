/**
 * 开发期身份的**纯逻辑**（M8 Task 2）：类型、存储、请求头形状。
 *
 * 从 `AuthProvider.tsx` 拆出来，有两个具体理由：
 *
 * 1. **这些是可以用单元测试钉住的东西**（"非 ASCII 显示名不发"这条规则
 *    只有被测试钉住，才不会在某次"顺手改成全发"时静默回归），
 *    而它们与 React 无关，拆开后测试不必渲染组件。
 * 2. `react-refresh` 只在"文件只导出组件"时能热更新；混着导出函数时，
 *    改一行 Provider 就整页刷新、丢掉界面状态。
 *
 * ## ⚠️ 显示名只发 ASCII
 *
 * HTTP 头是 latin-1 字节串：中文显示名**发不出去**（浏览器编码成 mojibake，
 * 于是审计里的人名是乱码，而这条记录是撤不回的）。因此非 ASCII 时
 * **干脆不发** `X-Actor-Name`，让服务端回落到 `actor_id` ——
 * 宁可界面显示 `li-hua`，也不要往审计账里写一个坏名字。
 *
 * 生产用 JWT：姓名在 JSON 声明里，Unicode 完全正常，没有这个问题。
 */

/** 开发期本地填写的调用身份。 */
export interface DevIdentity {
  readonly actorId: string
  readonly displayName: string
  readonly roles: readonly string[]
}

const STORAGE_KEY = 'contract-approval.dev-identity'

/** 可打印 ASCII —— 能安全放进 HTTP 头的取值。 */
const ASCII_ONLY = /^[\x20-\x7E]*$/

/**
 * 读取本地身份。
 *
 * 存的是 **`sessionStorage`** 而不是 `localStorage`：它不是凭据，
 * 但能让任何打开这个浏览器的人以该身份操作，而共用机器（测试机、演示机）
 * 关掉标签页就该回到"没有身份"。
 */
export function readStoredIdentity(): DevIdentity | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY)
    if (raw === null) {
      return null
    }
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) {
      return null
    }
    const { actorId, displayName, roles } = parsed as Record<string, unknown>
    if (typeof actorId !== 'string' || actorId.trim() === '') {
      return null
    }
    return {
      actorId: actorId.trim(),
      displayName: typeof displayName === 'string' ? displayName.trim() : '',
      roles: Array.isArray(roles)
        ? roles.filter((role): role is string => typeof role === 'string')
        : [],
    }
  } catch {
    // 存储被禁用（隐私模式）或内容损坏 —— 一律当作"没有身份"，
    // 而不是崩在启动路径上：这种情况下用户仍应看到页面并填一次身份。
    return null
  }
}

export function writeStoredIdentity(identity: DevIdentity | null): void {
  try {
    if (identity === null) {
      sessionStorage.removeItem(STORAGE_KEY)
      return
    }
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(identity))
  } catch {
    // 存不下不影响本次会话：状态在内存里，只是刷新后要重填
  }
}

/** 环境变量提供的默认身份（起本地环境时不必每次手填）。 */
export function envDefaultIdentity(): DevIdentity | null {
  const actorId = import.meta.env.VITE_DEV_ACTOR_ID?.trim()
  if (actorId === undefined || actorId === '') {
    return null
  }
  const roles = (import.meta.env.VITE_DEV_ACTOR_ROLES ?? '')
    .split(',')
    .map((role) => role.trim())
    .filter((role) => role !== '')
  return {
    actorId,
    displayName: import.meta.env.VITE_DEV_ACTOR_NAME?.trim() ?? '',
    roles,
  }
}

/** 开发期身份 Cookie 名（与后端 `dev_header_identity.COOKIE_DEV_IDENTITY` 互为镜像）。 */
export const DEV_IDENTITY_COOKIE = 'dev_identity'

/**
 * 身份载荷 → base64url（`{sub, name?, roles?}`，与后端三种载体共用同一形状）。
 */
function encodeDevIdentity(identity: DevIdentity): string {
  const claims: Record<string, unknown> = {
    sub: identity.actorId,
    roles: identity.roles,
  }
  const name = identity.displayName.trim()
  if (name !== '' && ASCII_ONLY.test(name)) {
    claims.name = name
  }
  // btoa 只接受 latin-1；显示名已被 ASCII_ONLY 拦下，这里再做一次
  // UTF-8 安全编码以防 claims 里混入非 ASCII（比如未来的字段）。
  return btoa(unescape(encodeURIComponent(JSON.stringify(claims))))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
}

/**
 * 开发期身份 → 请求头（`Authorization`；后端另有 Cookie 载体兜底）。
 *
 * ## 为什么是 `Authorization` 而不是 `X-Actor-*`
 *
 * 浏览器的翻译/隐私类扩展会改写页面请求并**剥掉非标准自定义头**——
 * `X-Actor-Id` 被剥掉后，后端只能 401"缺少身份"，而同页面的其它请求又正常，
 * 症状极度误导（M8 演示现场实际发生，沉浸式翻译扩展实锤）。
 * `Authorization` 是标准凭据头，扩展不会碰它。
 */
export function buildDevIdentityHeaders(
  identity: DevIdentity | null,
): Record<string, string> {
  if (identity === null) {
    return {}
  }
  return { Authorization: `Bearer dev.${encodeDevIdentity(identity)}` }
}

/**
 * 登录/退出时维护 `dev_identity` Cookie。
 *
 * ## 为什么有了 Authorization 还要写 Cookie
 *
 * 沉浸式翻译类扩展会挂钩页面的 `fetch` 并重发请求且**丢掉 init.headers**——
 * 于是同一个页面里"有的请求带头、有的不带头"，间歇出现且无法预测
 * （`Authorization` 也会被这样丢掉，M8 演示现场第二次实锤）。
 * Cookie 由**浏览器自动随请求携带**，不在 fetch 的 init 里，任何 fetch
 * 钩子都动不了它——这是能想到的最强免疫。
 *
 * 会话 Cookie（不带 Expires）：与 `sessionStorage` 的语义一致——
 * 关掉浏览器即回到"没有身份"。
 */
export function writeDevIdentityCookie(identity: DevIdentity | null): void {
  if (identity === null) {
    document.cookie = `${DEV_IDENTITY_COOKIE}=; path=/; max-age=0; SameSite=Lax`
    return
  }
  document.cookie = `${DEV_IDENTITY_COOKIE}=${encodeDevIdentity(identity)}; path=/; SameSite=Lax`
}
