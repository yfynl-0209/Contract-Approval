/// <reference types="vite/client" />

/**
 * Vite 环境变量的类型声明（M8 Task 1/2）。
 *
 * ⚠️ 只声明**非敏感**的变量。控制台里**没有任何**令牌、密钥或凭据
 * （材料 §Global Constraints：令牌不得进入前端代码与日志）：
 * 生产环境前端只从同源接口取数，身份由反向代理 / SSO 会话提供。
 * 下面这几个是**开发期**方便起本地环境用的调用身份，默认值本身就是公开信息。
 */
interface ImportMetaEnv {
  /** 开发期默认调用身份的 actor_id（仅 `AUTH_MODE=dev` 时有意义） */
  readonly VITE_DEV_ACTOR_ID?: string
  /** 开发期默认调用身份的显示名（**须为 ASCII**，见 `AuthProvider` 的说明） */
  readonly VITE_DEV_ACTOR_NAME?: string
  /** 开发期默认角色声明，逗号分隔：`legal_reviewer,system_admin` */
  readonly VITE_DEV_ACTOR_ROLES?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
