/**
 * 后端响应契约（M8 Task 2）。
 *
 * ## 这些类型是**手写**的，而且这不是偷懒
 *
 * M7 的查询接口响应体是**手搓 dict**（`app/api/views.py::page_json`、
 * `app/api/jobs.py::_job_json`、`app/api/results.py::result_row_json` …），
 * 没有 Pydantic `response_model` —— 因此 **OpenAPI 里查不到响应字段**
 * （生成器只会给出空 schema）。契约只能照那些构体函数逐字段抄。
 *
 * 代价必须说清楚：**它不会自己发现漂移**。后端加字段时这里不会报错，
 * 只会"少显示一个东西"。因此配套做了两件事：
 *
 * 1. 每个类型上方写明它对应**哪个后端文件**，改后端时能直接找到这里；
 * 2. M8 Task 10 的验收里有一条**契约漂移检查**：把这里的字段名集合
 *    与实际响应逐项比对（拿真实响应跑，而不是读代码）。
 *
 * ## 三条贯穿性约定（都来自材料 §5）
 *
 * | 约定 | 在类型上的体现 |
 * | --- | --- |
 * | 状态与原因**分两处** | `TaskView.write_status`（走到哪一步）与 `WritebackSummary.latest_reason_code`（为什么没成）是**两个**字段，类型上不可互推 |
 * | 业务结论 ≠ 系统故障 | `outcome: 'error'` 只用于**接口级**失败；业务结论走 200 + 数据里的 `blocked` / `not_written` |
 * | 时间一律是**无时区**的本地 ISO 串 | `IsoTime = string`（`app/api/views.py::iso` 返回 `datetime.isoformat()`，**不带 Z 或偏移**），因此前端必须按**本地时间**解析，不能 `new Date()` 后当 UTC 处理 |
 *
 * ## 为什么时间类型不写成 `Date`
 *
 * 后端下发的是字符串。若在这里标成 `Date`，使用方会以为可以直接算差 ——
 * 而它其实还是字符串（TS 不校验运行时）。标成 `string` 才能让"先解析再算"
 * 变成不得不做的事，解析处也能统一处理"无时区"这个坑。
 */

// ============================================================
// 通用形状（`app/api/views.py`）
// ============================================================

/** 后端下发的时间：**不带时区**的本地 ISO 串。`null` 表示"没有这个时间"。 */
export type IsoTime = string

/**
 * 分页信封（`views.page_json`）。
 *
 * ⚠️ `total` / `page_count` / `has_next` **一律取自后端**：
 * 前端自己数当前页长度时，只有一页数据的情况下两者永远相等，
 * 于是这个缺陷会一直活到数据量上来 —— 表现是"永远只有一页"。
 */
export interface Page<T> {
  readonly items: readonly T[]
  readonly total: number
  readonly page: number
  readonly page_size: number
  readonly page_count: number
  readonly has_next: boolean
}

/**
 * 错误响应体（`app/api/errors.py::error_body`）。
 *
 * `error_code` 是**稳定机器判据**，`message` 只给人看 ——
 * 前端按 `error_code` 分流处置（去换身份 / 找管理员 / 等重试），
 * 而**不得**对 `message` 做字符串匹配：中文说明会改。
 */
export interface ApiErrorBody {
  readonly outcome: 'error'
  readonly error_code: string
  readonly message: string
  readonly retryable: boolean
}

/**
 * 自由形态 JSON（`evidence_json` / `hit_detail_json` / 审计 `detail`）。
 *
 * ⚠️ 用 `unknown`-风格的联合而不是 `any`：`any` 会让"忘了做运行时校验"
 * 这件事完全静默（例如把 `evidence[0].page` 直接当数字用）。
 * 消费这些值的地方必须**先窄化**（Task 6 会给出 `EvidenceSpan` 的校验函数）。
 */
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | readonly JsonValue[]
  | { readonly [key: string]: JsonValue }

// ============================================================
// 身份（`app/api/identity.py` + `app/auth.py`）
// ============================================================

/** 权限取值域（`app/auth.py::Permission`）。**后端是唯一判据**，这里只为类型安全。 */
export type Permission =
  | 'task:read'
  | 'review:execute'
  | 'result:save'
  | 'result:confirm'
  | 'writeback:execute'
  | 'rule:manage'
  | 'ops:retry'
  | 'audit:read'

/** 角色取值域（`app/auth.py::Role`）。 */
export type KnownRole = 'legal_reviewer' | 'system_admin' | 'read_only_auditor'

/**
 * 当前身份（`GET /api/me`）。
 *
 * `permissions` 是**服务端算好的**：前端不得从 `roles` 自己推 ——
 * 那样角色→权限映射就有了第二份实现，两份漂移时表现为
 * "入口在、点了 403"（或反过来"有权限却看不到入口"），而两边都不报错。
 *
 * `unknown_roles` 是"IdP 上了新角色而本系统还没映射"的唯一线索：
 * 未识别角色**不带来任何权限**（fail-closed），但必须可见，否则
 * 用户只会看到"莫名其妙少了一堆入口"。
 */
export interface ActorIdentity {
  readonly actor_id: string
  readonly display_name: string
  readonly tenant_id: string
  /** 令牌里**原样**的角色声明（含未识别的），排障时回答"当初声明了什么" */
  readonly roles: readonly string[]
  readonly unknown_roles: readonly string[]
  readonly permissions: readonly Permission[]
}

// ============================================================
// 任务（`app/services/query_service.py::task_view` + `app/api/tasks.py`）
// ============================================================

/** 任务状态（`app/enums.py::TaskStatus`）。 */
export type TaskStatus = 'pending' | 'parsing' | 'reviewing' | 'blocked' | 'done'

/**
 * 回写状态（`app/enums.py::WriteStatus`）。
 *
 * ⚠️ 严格四个值，**门禁拒绝时也是 `not_written`**（根本没发起过回写）。
 * 因此"未回写"绝不能单独渲染成"还没轮到" —— 必须同时看原因码。
 */
export type WriteStatus = 'not_written' | 'writing' | 'success' | 'failed'

/** 回写未成功的原因码（`app/enums.py::WritebackReasonCode`）。成功时为 `null`。 */
export type WritebackReasonCode =
  // 门禁拒绝（未发起回写 → write_status = not_written）
  | 'WRITEBACK_POLICY_DENIED'
  | 'TASK_NOT_DONE'
  | 'RESULT_MISSING'
  | 'CONTEXT_NOT_VALID'
  | 'MANUAL_CONFIRM_REQUIRED'
  | 'ALREADY_WRITTEN'
  | 'COMMENT_TEXT_MISSING'
  // 外部调用失败（已发起 → write_status = failed）
  | 'APPROVAL_API_ERROR'
  | 'APPROVAL_API_TIMEOUT'
  | 'IDEMPOTENCY_CONFLICT'

/** 权威审查上下文状态（`app/enums.py::ContextStatus`）。 */
export type ContextStatus = 'complete' | 'missing' | 'conflict' | 'confirmed'

/** 上下文来源（`app/enums.py::ContextSource`）。 */
export type ContextSource = 'approval_system' | 'manual'

/** 我方在合同正文中的形式标签（`app/enums.py::ContractLabel`）。无业务语义。 */
export type ContractLabel = 'party_a' | 'party_b' | 'other' | 'unknown'

/** 我方的实际业务身份（`app/enums.py::BusinessRole`）—— 方向敏感规则的判据。 */
export type BusinessRole =
  | 'buyer'
  | 'seller'
  | 'customer'
  | 'service_provider'
  | 'licensor'
  | 'licensee'
  | 'other'
  | 'unknown'

/** 合同业务分类（`app/enums.py::ContractType`）—— 规则是否适用的判据。 */
export type ContractType =
  | 'procurement'
  | 'sales'
  | 'software_service'
  | 'development'
  | 'outsourcing'
  | 'lease'
  | 'other'
  | 'unknown'

/**
 * 回写的**两个层级**（`app/api/tasks.py::_writeback_json`）。
 *
 * | 层级 | 字段 | 回答 |
 * | --- | --- | --- |
 * | 任务级 | `task_write_status` | 这张单子写成了没有 |
 * | 尝试级 | `latest_*` | **最近这一次**为什么没成 |
 *
 * 两者不可互相替代：任务级 `failed` 只知道"没写成"，
 * 而 `not_written` + `MANUAL_CONFIRM_REQUIRED` 才说明"它在等一次人工确认" ——
 * **重试一个被拒的请求永远是白试**。
 *
 * ⚠️ 列表行与详情**给的是同一个对象**（同名同结构）：只给列表任务级状态时，
 * "写失败"与"被门禁拒绝"在列表上长得一样，而两者的处置相反。
 */
export interface WritebackSummary {
  readonly task_write_status: WriteStatus
  readonly latest_attempt_id: number | null
  readonly latest_attempt_no: number | null
  readonly latest_attempt_status: WriteStatus | null
  readonly latest_reason_code: WritebackReasonCode | null
  readonly latest_reason_text: string | null
  readonly latest_attempt_at: IsoTime | null
  /**
   * 最近一次尝试是否属于**门禁拒绝**。
   *
   * 单独一个布尔量，因为它决定的处置方向与"失败"**相反**：
   * 拒绝要人去确认，失败要人去重试。让每个调用方自己从 `reason_code` 推，
   * 等于要求它知道哪些原因属于拒绝类。
   *
   * ⚠️ **可为 `null`**：这张单子还没有过任何回写尝试（`latest_attempt_*`
   * 全为空）—— 此时"最近一次是否被拒"无从谈起。契约形状检查
   * （apiShape.test.ts vs verify_m8.py 钉住的后端形状）抓到的漂移。
   */
  readonly latest_attempt_rejected: boolean | null
  readonly status_url: string | null
}

/**
 * 任务的**列表行**形状（`query_service.task_view` + `app/api/tasks.py::_task_row_serializer`）。
 *
 * 详情与列表共用它 —— 两套时"列表里的状态"与"详情里的状态"迟早漂移。
 * 详情在此基础上多挂链路指针（`TaskDetail`）。
 */
export interface TaskView {
  readonly task_id: number
  readonly instance_id: string
  readonly approval_code: string
  readonly approval_title: string | null
  readonly applicant_name: string | null
  readonly task_status: TaskStatus
  /** **任务级**回写状态（与尝试级原因分两处，见 `WritebackSummary`） */
  readonly write_status: WriteStatus
  readonly context_status: ContextStatus
  readonly context_source: ContextSource | null
  /**
   * `conflict` 时给出**两个来源的对照**，其余情况为 `null`。
   *
   * ⚠️ `null` 而不是空对象：空对象读起来像"冲突了，但没有细节"，
   * 而真相是"没有冲突" —— 界面据此渲染的提示**完全相反**。
   */
  readonly context_conflict: ContextConflict | null
  readonly our_party_name: string | null
  readonly our_party_contract_label: ContractLabel | null
  readonly our_party_business_role: BusinessRole | null
  readonly contract_type: ContractType | null
  /** 卡在哪一步（`pull` / `detail` / `download` / `parse` / `rule` / `result` / `writeback`） */
  readonly blocked_stage: string | null
  readonly block_reason: string | null
  /** 稳定机器码（`app/enums.py::ErrorCode`）；只能用于分流，不能当文案 */
  readonly last_error_code: string | null
  readonly retry_count: number
  /**
   * **当前版本**结果的总风险等级；`null` = 还没审查出结果。
   *
   * ⚠️ `null` **不是** `low`：把"没审过"显示成"低风险"是最危险的默认值 ——
   * 它让一份从未被审查的合同看起来是安全的。
   */
  readonly overall_risk_level: RiskLevel | null
  readonly writeback: WritebackSummary
  /** 附件数（列表与详情**同名列**，列表页的"附件"列用它） */
  readonly attachment_count: number
  readonly created_at: IsoTime | null
  readonly updated_at: IsoTime | null
  readonly status_url: string
}

/**
 * 任务汇总计数（`GET /api/tasks/summary`）。
 *
 * ⚠️ **服务端算的**：卡片说的是全量，而前端只能数到当前这一页 ——
 * 数据少于一页时两者永远相等，因此"前端自己数"的缺陷会一直活到上线之后。
 *
 * `by_status` 的键**恒定齐全**（五个状态即使为 0 也在），因此可以安全地
 * 直接取用而不必到处写 `?? 0`。
 */
export interface TaskSummary {
  readonly total: number
  readonly by_status: Readonly<Record<TaskStatus, number>>
  readonly writeback_failed: number
}

/** 任务详情（`GET /api/tasks/{id}`）= 列表行 + 链路指针 + 两个回写层级。 */
export interface TaskDetail extends TaskView {
  readonly latest_parse_id: number | null
  readonly latest_run_id: number | null
  readonly current_result_id: number | null
  /**
   * 关联 ID：取自最近一次**作业**。
   *
   * 它是唯一能把 API 与 Worker **两个进程**的日志拼成一条链的键 ——
   * 排障时把它复制给运维，比"大概几点、哪份合同"有用得多。
   */
  readonly correlation_id: string | null
  /**
   * 审批表单（原样键值对）。
   *
   * ⚠️ **只用于渲染**，而且默认要掩码敏感值（§4.2）。它**不得**进入 URL、
   * console 或错误上报（材料 §Global Constraints）。
   */
  readonly form_data: JsonValue
  /**
   * `last_error_code` 属于**业务结论**还是**系统故障**（§5.2）。
   *
   * 判据在服务端（`app/errors.py::BUSINESS_FACT_CODES`）：
   * 前端自己列一张码表时会与它漂移，而漂移的后果是
   * "附件被删了"被渲染成"系统故障，稍后重试" —— 处置完全相反。
   */
  readonly last_error_is_business_fact: boolean
}

/**
 * 附件元数据（`GET /api/tasks/{id}/attachments`）。
 *
 * ⚠️ 响应里**没有** `object_key`、也**没有** `file_path`：
 * 前者是内部存储布局，后者是一条绕过鉴权的读取通道。
 * 取字节走 `content_url`（唯一的地址来源，UI 不拼路径）。
 */
export interface AttachmentRow {
  /** 本系统主键（`approval_attachments.id`）—— 取内容用的是它 */
  readonly attachment_record_id: number
  /** 外部系统的附件编号（TEXT）。与上一行**同名不同义**，因此两个都发、名字不同 */
  readonly attachment_id: string
  readonly file_name: string
  readonly file_type: string | null
  readonly content_type: string | null
  readonly file_size: number | null
  /** SHA-256（界面显示前 12 位，用于"我拿到的就是那一份"的核对） */
  readonly file_checksum: string | null
  readonly download_status: 'pending' | 'success' | 'failed'
  readonly error_message: string | null
  readonly content_url: string
  readonly created_at: IsoTime | null
}

/**
 * 人工**修正**权威审查上下文（`ConfirmContextRequest`）—— 四条必须齐全。
 *
 * ⚠️ 只提交其中两条时剩下两条还是旧值，于是"我方是谁"与
 * "这对我是好是坏"可能自相矛盾，而这类不一致不会被任何校验发现。
 */
export interface ContextCorrection {
  readonly our_party_name: string
  readonly our_party_contract_label: ContractLabel
  readonly our_party_business_role: BusinessRole
  readonly contract_type: ContractType
}

/** 四项业务事实的原始取值（未映射成文案；enum 字段这里是取值码）。 */
export interface RawContextFacts {
  readonly our_party_name: string | null
  readonly our_party_contract_label: string | null
  readonly our_party_business_role: string | null
  readonly contract_type: string | null
}

/**
 * 立场冲突的**两个来源**（`approval_tasks.context_conflict_json`）。
 *
 * `declared` = 审批系统本次声明；`confirmed` = 人工背书的那一组
 * （后者的值保存在任务自身的四个字段里，裁定以它为准）。
 */
export interface ContextConflict {
  readonly declared: RawContextFacts
  readonly confirmed: RawContextFacts
}

/** 立场确认的响应（`POST /api/tasks/{id}/context/confirm`）。 */
export interface ContextConfirmation {
  readonly task_id: number
  readonly context_status: ContextStatus
  readonly context_source: ContextSource | null
  readonly our_party_name: string | null
  readonly our_party_contract_label: ContractLabel | null
  readonly our_party_business_role: BusinessRole | null
  readonly contract_type: ContractType | null
  readonly status_url: string
}

// ============================================================
// 作业（`app/api/jobs.py::_job_json`）
// ============================================================

/** 作业状态（`app/enums.py::JobStatus`）—— **Worker 执行情况**，与任务状态两个层级。 */
export type JobStatus = 'queued' | 'running' | 'retry_wait' | 'succeeded' | 'failed'

/** 作业类型（`app/enums.py::JobType`）。 */
export type JobType =
  | 'pull'
  | 'detail'
  | 'download'
  | 'parse'
  | 'rule'
  | 'result'
  | 'writeback'

/**
 * 作业成功时的结果引用（`jobs._result_ref`）。
 *
 * ⚠️ 判据是**作业级** `job_status == 'succeeded'`，不是解析成功：
 * 作业跑完了但解析被质量门禁判 `failed` 是完全可能的。
 * 因此拿到非空 `result_ref` 后仍**必须**看 `parse_status` / `run_status`。
 *
 * ⚠️ `result` / `writeback` 两类作业**恒为 `null`**：它们的产物 id
 * （结果号 / 尝试号）不在冻结输入里，而是执行时才产生的。
 */
export interface JobResultRef {
  readonly parse_id?: number
  readonly run_id?: number
  readonly result_url: string
}

/** 作业记录（`jobs._job_json`）。 */
export interface JobRecord {
  readonly job_id: number
  readonly task_id: number
  readonly job_type: JobType
  readonly job_status: JobStatus
  readonly attempt_no: number
  readonly max_attempts: number
  readonly next_retry_at: IsoTime | null
  readonly last_error_code: string | null
  readonly last_error_text: string | null
  readonly correlation_id: string | null
  readonly status_url: string
  readonly result_ref: JobResultRef | null
}

// ============================================================
// 解析（`app/api/jobs.py::get_parse`）
// ============================================================

/** 解析状态（`app/enums.py::ParseStatus`）—— **单份附件级**，与任务状态不是一回事。 */
export type ParseStatus = 'pending' | 'parsing' | 'succeeded' | 'failed' | 'blocked'

/** 解析质量指标（`get_parse` 的 `quality`）。 */
export interface ParseQuality {
  readonly text_coverage: number | null
  readonly ocr_pages: number | null
  readonly ocr_confidence: number | null
}

/** 解析记录（`GET /api/parses/{id}`）。字段由后端**内联**返回，不必去对象存储取。 */
export interface ParseRecord {
  readonly parse_id: number
  readonly task_id: number
  /**
   * ⚠️ 叫 `attachment_record_id` 而不是 `attachment_id`：
   * 库里 `contract_parses.attachment_id`（INTEGER，本系统主键）与
   * `approval_attachments.attachment_id`（TEXT，外部编号）**同名不同义**。
   */
  readonly attachment_record_id: number
  readonly parse_status: ParseStatus
  readonly parse_version: number
  readonly parser_name: string
  readonly parser_version: string
  readonly cache_key: string | null
  /** 稳定机器码（`app/enums.py::ErrorCode`）；与 `parse_error`（人读文本）分开 */
  readonly parse_error_code: string | null
  readonly parse_error: string | null
  /** 结构化字段（内容由 M4 决定，前端使用前须窄化） */
  readonly basic_info: JsonValue
  readonly clause_info: JsonValue
  readonly quality: ParseQuality
}

// ============================================================
// 解析字段与证据（`app/ports/field_contract.py`）
// ============================================================

/**
 * 字段四态（`app/enums.py::FieldStatus`）—— **本项目最关键的一个区分**。
 *
 * | 取值 | 事实 | 判错会怎样 |
 * | --- | --- | --- |
 * | `extracted` | 有值、有证据 | —— |
 * | `not_found` | **完成规定范围检索后确实没找到** | —— |
 * | `uncertain` | 有疑似内容但证据不足 | 当成 `not_found` → 缺失类规则**误报** |
 * | `failed` | 提取过程失败 | 当成 `not_found` → 把系统故障说成"合同没约定" |
 *
 * ⚠️ 界面**绝不可**把后两者合并成"无数据"。
 */
export type FieldStatus = 'extracted' | 'not_found' | 'uncertain' | 'failed'

/** **文本**定位精度（`TextPrecision`）：证据文本是否可信。 */
export type TextPrecision = 'char' | 'line' | 'none'

/** **几何**定位精度（`BboxPrecision`）：画字符框还是块框。与上一行**不是**同一个枚举。 */
export type BboxPrecision = 'char' | 'line' | 'block' | 'none'

/** 归一化包围盒 `[x0, y0, x1, y1]`（`pdf-point-top-left` 空间，单位 PDF 点）。 */
export type BBox = readonly [number, number, number, number]

/**
 * 一处证据（`EvidenceSpan`）。
 *
 * `char_start` / `char_end` 是**页内**半开区间，与 `DocumentBlock` 同一坐标系 ——
 * 共用坐标系时"从证据反推块"是查表而不是换算。
 */
export interface EvidenceSpan {
  readonly page: number
  readonly block_id: string
  readonly text: string
  readonly bbox: BBox
  readonly char_start: number
  readonly char_end: number
  readonly text_precision: TextPrecision
  readonly bbox_precision: BboxPrecision
}

/**
 * 一个字段（或一类条款）的结论（`ExtractedField`）。
 *
 * ⚠️ 四态各自的不变量由**后端构造期**保证，到前端时已经成立：
 * `extracted` 必有 `evidence`、`not_found` 必无、`uncertain`/`failed` 必有 `reason_code`。
 * 前端据此渲染即可，**不需要**再防御一遍（那会让"后端契约破了"这件事悄无声息）。
 */
export interface ExtractedField {
  readonly field_code: string
  /** 原文片段（人看的）。证据不足以给值时为空串 */
  readonly value_text: string
  /** 十进制**字符串**，仅数值类字段有值（**不是** number：金额绝不用浮点） */
  readonly value_decimal: string | null
  readonly currency: string | null
  readonly status: FieldStatus
  readonly evidence: readonly EvidenceSpan[]
  readonly reason_code: string | null
  readonly reason_text: string | null
}

/** `basic_info` / `clause_info` 的根结构（`_FieldSet`）。两份 JSON 同一形状。 */
export interface FieldSet {
  readonly schema_version: number
  readonly fields: readonly ExtractedField[]
}

// ============================================================
// 标准文档几何（`app/ports/parse_document.py`）
// ============================================================

/** `[raw_start, raw_end)`：规范化后一个字符对应的**原始**区间（NFC 可多对一）。 */
export interface SourceSpan {
  readonly raw_start: number
  readonly raw_end: number
}

export interface DocumentChar {
  readonly text: string
  readonly bbox: BBox
  readonly char_start: number
  readonly char_end: number
}

export interface DocumentBlock {
  readonly block_id: string
  readonly text: string
  readonly bbox: BBox
  readonly char_start: number
  readonly char_end: number
  readonly text_precision: TextPrecision
  readonly bbox_precision: BboxPrecision
  /** 逐字符几何；`bbox_precision !== 'char'` 时为空 */
  readonly chars: readonly DocumentChar[]
}

/** 页级四态（`PageStatus`）：`blank` 是"**可靠识别后**确认无文字"，与 `failed` 不同。 */
export type PageStatus = 'ok' | 'blank' | 'uncertain' | 'failed'

/** 页来源：原生文本层还是 OCR —— 决定"这条结论是读的还是猜的"。 */
export type PageSource = 'text' | 'ocr'

/**
 * 一页的标准表示（`DocumentPage`）。
 *
 * ⚠️ **`width` / `height` 对应旋转后可见页面，`bbox` 一律已换算到该空间**。
 * 因此前端画框时**不得再应用一次 `rotation`**（见 `coordinateTransform.ts`）。
 */
export interface DocumentPage {
  readonly page: number
  readonly width: number
  readonly height: number
  readonly bbox_space: 'pdf-point-top-left'
  readonly rotation: 0 | 90 | 180 | 270
  readonly source: PageSource
  readonly page_status: PageStatus
  /** 权威明文；`blocks[].text` 是它的切片（后端构造期强制） */
  readonly text: string
  /** 走 OCR 的判据；`source === 'text'` 时为 null */
  readonly route_reason: string | null
  /** `page_status === 'failed'` 时的稳定错误码 */
  readonly error_code: string | null
  readonly char_map: readonly SourceSpan[]
  readonly blocks: readonly DocumentBlock[]
}

export interface StandardDocument {
  readonly schema_version: number
  readonly pages: readonly DocumentPage[]
}

/**
 * `GET /api/parses/{id}/document` 的响应（`app/api/attachments.py::get_parse_document`）。
 *
 * `sha256` 是**工件自身**的摘要：消费方要核验"我拿到的就是那份证据"时有依据。
 */
export interface ParseDocumentResponse {
  readonly parse_id: number
  readonly task_id: number
  readonly artifact_id: number
  readonly artifact_version: number
  readonly sha256: string
  readonly size_bytes: number
  readonly schema_version: number
  readonly page_count: number
  readonly document: StandardDocument
  readonly created_at: IsoTime | null
}

// ============================================================
// 规则评价（`app/api/views.py::evaluation_json` + `app/api/results.py`）
// ============================================================

/** 规则评价四态（`app/enums.py::EvaluationStatus`），**语义互斥**。 */
export type EvaluationStatus = 'hit' | 'not_hit' | 'not_applicable' | 'needs_review'

/** 风险等级（`app/enums.py::RiskLevel`）。 */
export type RiskLevel = 'low' | 'medium' | 'high'

/**
 * 一条规则评价（`GET /api/evaluations` 的 `items[]`）。
 *
 * ⚠️ **四态都给**，包括 `not_hit` 与 `not_applicable`：
 * 只显示 `hit` 时，"这条规则为什么没报警"永远答不出来 ——
 * 而那正是四态记录存在的理由（界面必须给折叠区并**显示计数**）。
 */
export interface EvaluationItem {
  readonly evaluation_id: number
  readonly run_id: number
  readonly task_id: number
  readonly rule_code: string
  /** 规则中文名（`ReviewRule.rule_name`）；规则被删等查不到时为 `null` */
  readonly rule_name: string | null
  readonly rule_version: number
  readonly evaluation_status: EvaluationStatus
  readonly risk_level: RiskLevel
  /** 稳定原因码（`app/enums.py::ReasonCode`） */
  readonly reason_code: string | null
  readonly reason_text: string | null
  /** 证据数组；逐条形如 `{page, bbox, char_start, char_end, precision}`，使用前须窄化 */
  readonly evidence: readonly JsonValue[]
  readonly hit_detail: JsonValue
  readonly created_at: IsoTime | null
}

// ============================================================
// 审查批次（`app/api/jobs.py::get_run`）—— 模块 4 的数据源
// ============================================================

/** 批次状态（`app/enums.py::RunStatus`）。⚠️ 只有 `completed` 时聚合才是完整的。 */
export type RunStatus = 'running' | 'completed' | 'failed'

/**
 * `GET /api/runs/{id}` 的一条评价（`jobs._evaluation_json`）。
 *
 * ⚠️ **它比 `/api/evaluations` 的行少几个字段**：同一份"一条评价"，
 * 两个接口给的不是同一个形状 ——
 *
 * | 字段 | `/api/evaluations` | `/api/runs/{id}.evaluations[]` |
 * | --- | --- | --- |
 * | 上面这 7 个 | ✓ | ✓ |
 * | `evaluation_id` / `run_id` / `task_id` / `rule_version` / `created_at` | ✓ | **没有** |
 *
 * 这不是前端能修的，只能**照实建模**：合成一个类型再到处断言 `!`，
 * 会在模块 4 里造出四个恒为 `undefined` 的字段，而界面显示"版本 undefined"
 * 时没人知道是接口没给还是解析错了。Task 10 的契约漂移检查会把它列为已知差异。
 */
export interface RunEvaluation {
  readonly rule_code: string
  /** 规则中文名；查不到时为 `null` —— 界面回退显示 `rule_code` */
  readonly rule_name: string | null
  readonly evaluation_status: EvaluationStatus
  readonly risk_level: RiskLevel
  readonly reason_code: string | null
  readonly reason_text: string | null
  /** 形如 `[{text, position:{page, block_id, bbox, char_start, char_end, text_precision, bbox_precision}}]`，使用前须窄化 */
  readonly evidence: readonly JsonValue[]
  /** `expr` 规则的计算过程（`actual` / `op` / `threshold`）；其它规则是别的键 */
  readonly hit_detail: JsonValue
}

/** 聚合里的一个关注点（`aggregator.FocusPoint.to_json`）——**不带证据几何**，只有证据文本。 */
export interface AggregateFocusPoint {
  readonly rule_code: string
  readonly status: EvaluationStatus
  readonly risk_level: RiskLevel
  readonly reason_code: string | null
  readonly reason_text: string | null
  readonly evidence_text: string | null
}

/**
 * 批次聚合（`aggregator.Aggregate.to_json`）——**现算的，不是从结果表读的**。
 *
 * ⚠️ `counts` 的四个键**恒定齐全**（后端用穷举的 `_COUNTED` 初始化）：
 * 因此"不适用 31"这样的数字可以直接相信；而"0"与"没有这个键"在这里不会分不开。
 */
export interface RunAggregate {
  readonly overall_risk_level: RiskLevel
  readonly review_status: ReviewStatus
  readonly counts: {
    readonly hit: number
    readonly not_hit: number
    readonly not_applicable: number
    readonly needs_review: number
  }
  readonly focus_points: readonly AggregateFocusPoint[]
  readonly summary: string
}

/**
 * 一次审查批次（`GET /api/runs/{id}`）。
 *
 * ⚠️ `evaluations` **整批返回、不分页**（后端一次给全），而
 * `/api/evaluations` 是分页的。模块 4 用这个接口正是为了这一点：
 * 分批取评价时，"四态计数"与"列出来的条目"会来自两次不同的请求，
 * 数字与列表**迟早对不上**（而这正是验收 4 要防的）。
 *
 * ⚠️ `run_status !== 'completed'` 时聚合只反映**已落库的那部分**评价：
 * 把半截批次当完整结论会得到一份"低风险"，因为**还没算完**。
 */
export interface RunDetail {
  readonly run_id: number
  readonly task_id: number
  readonly parse_id: number | null
  readonly version_no: number
  readonly run_status: RunStatus
  /** 规则集快照的版本标识（批次自带当时的规则内容，规则日后被改也能还原依据） */
  readonly ruleset_version: string | null
  readonly model_version: string | null
  readonly prompt_version: string | null
  readonly config_version: string | null
  readonly aggregate: RunAggregate
  /** ⚠️ 声明为 `JsonValue`：运行时形状必须经 `ruleEvidence.readRunEvaluations` 窄化 */
  readonly evaluations: readonly JsonValue[]
  readonly started_at: IsoTime | null
  readonly finished_at: IsoTime | null
}

// ============================================================
// 审查结果（`app/api/results.py::result_row_json`）
// ============================================================

/** 结论完整性（`app/enums.py::ReviewStatus`）—— "整份结论可不可信"。 */
export type ReviewStatus = 'complete' | 'needs_review'

/**
 * 一份审查结果（列表行与详情**形状完全一致**）。
 *
 * `confirmation_valid` **由后端判定**（已确认 + 摘要相符 + 仍是当前版本），
 * 三个条件缺一不可 —— 前端自己比对摘要时，改版后算错的方向恰恰是
 * 把**失效的确认显示成有效**，而它不会有任何报错。
 *
 * `content_digest` 是正文**摘要**而非正文：它是人工确认绑定的对象，
 * 据此能回答"我确认的是不是这一份"，且不构成机密。
 */
export interface ReviewResultRow {
  readonly result_id: number
  readonly run_id: number
  readonly task_id: number
  readonly version_no: number
  readonly is_current_version: boolean
  readonly confirmation_valid: boolean
  readonly overall_risk_level: RiskLevel
  readonly review_status: ReviewStatus
  readonly hit_count: number
  readonly needs_review_count: number
  readonly not_applicable_count: number
  readonly summary_text: string | null
  readonly focus_points: readonly string[]
  readonly comment_text: string | null
  readonly content_digest: string | null
  readonly manual_confirmed: boolean
  readonly confirmed_by: string | null
  readonly confirmed_at: IsoTime | null
  readonly confirmed_digest: string | null
  readonly supersedes_result_id: number | null
  readonly created_by: string | null
  readonly created_at: IsoTime | null
  readonly updated_at: IsoTime | null
  readonly status_url: string
}

// ============================================================
// 保存结果 / 修改正文（工具 6 与 M8 的薄出口，**同一个形状**）
// ============================================================

/**
 * `POST /tools/save_review_result` 与 `POST /api/results/{id}/comment`
 * 的返回 —— **两个入口逐字相同**（后者复用前者的服务函数与门面）。
 *
 * ⚠️ 不含 `confirmation_valid`：**新版本必然未确认**（确认绑定的是当时那份正文），
 * 要拿新的确认状态就再读一次 `result_url`。这里回显一个调用方能自行推断的字段，
 * 等于让两个形状开始漂移 —— 而它们现在是逐字相同的。
 */
export interface SavedResultResponse {
  /** `saved` = 新建了一版；`reused` = 同批次同指纹，复用了既有版本 */
  readonly outcome: 'saved' | 'reused'
  readonly result_id: number
  readonly run_id: number
  readonly task_id: number
  readonly version_no: number
  readonly overall_risk_level: RiskLevel
  readonly content_digest: string
  readonly result_url: string
}

// ============================================================
// 回写尝试（`app/api/jobs.py::get_writeback`）
// ============================================================

/** Outbox 事件状态（`app/enums.py::OutboxStatus`）—— 只有三个值。 */
export type OutboxStatus = 'pending' | 'delivered' | 'failed'

/**
 * **投递进度**（Outbox 层）。
 *
 * ⚠️ 与"尝试状态"是两个层级（`get_writeback` 的 docstring）：
 * 尝试层回答"这次回写走到哪一步"，投递层回答"派发器试了几次、下次什么时候、
 * 上次为什么失败"。只给尝试层时，一次卡在重试中的回写只能看到 `writing` ——
 * 而调用方最需要知道的"还要等多久 / 是不是快耗尽了"恰好在投递层。
 */
export interface OutboxDelivery {
  readonly event_id: number
  readonly event_type: string
  readonly event_status: OutboxStatus
  readonly attempt_no: number
  readonly max_attempts: number
  readonly next_retry_at: IsoTime | null
  readonly last_error_code: string | null
  readonly last_error_text: string | null
  readonly correlation_id: string | null
  readonly created_at: IsoTime | null
  readonly delivered_at: IsoTime | null
}

/**
 * 一次回写尝试（`comment_logs` 行）。
 *
 * ⚠️ `write_status = not_written` + `reason_code` = **门禁拒绝**：
 * 没有发起过回写，`delivery` 为 `null`（拒绝不产生 Outbox 事件）。
 * 用零值对象填充时，"被拒绝"与"排队中"在接口上长得一样 ——
 * 而两者的正确处置完全相反（一个要人去确认，一个只需等待）。
 */
export interface WritebackAttempt {
  readonly attempt_id: number
  readonly task_id: number
  /** 对应 `review_results.id`；`null` 表示这次尝试没有绑定结果 */
  readonly result_id: number | null
  readonly instance_id: string | null
  readonly write_status: WriteStatus
  readonly reason_code: string | null
  readonly reason_text: string | null
  readonly content_digest: string | null
  readonly attempt_no: number
  readonly operator_name: string | null
  readonly created_at: IsoTime | null
  readonly task_status: TaskStatus | null
  readonly task_write_status: WriteStatus | null
  readonly delivery: OutboxDelivery | null
  readonly status_url: string
}

// ============================================================
// 规则管理（`app/api/rules.py` + `rule_admin_service.rule_view`）
// ============================================================

/** 规则启停用（`app/enums.py::RuleStatus`）。⚠️ 它不是"配置是否合法"。 */
export type RuleStatus = 'active' | 'inactive'

/** 匹配模式（`app/enums.py::MatchMode`）。`expr` 是数值/字段比较。 */
export type MatchMode = 'keyword' | 'regex' | 'llm' | 'expr'

/**
 * 一条规则（列表与详情**形状完全一致**）。
 *
 * ⚠️ `rule_code` 是**稳定标识**（进历史评价、批次快照、界面），
 * 因此没有改名入口；`rule_version` 的升级规则见 `ruleEdit.ts`。
 */
export interface RuleRow {
  readonly rule_id: number
  readonly rule_code: string
  readonly rule_name: string
  /** 需求 2.4.6 的 11 类之一（后端存字符串，界面按需给下拉） */
  readonly rule_category: string | null
  readonly risk_level: RiskLevel
  readonly rule_status: RuleStatus
  readonly priority: number
  readonly rule_version: number
  readonly match_mode: MatchMode
  readonly match_text: string
  /** 适用条件 JSON（原文）；`null` = 全局适用 */
  readonly applies_when_json: string | null
  /** `llm` 规则的必备降级条件（无模型时的确定性判据） */
  readonly fallback_match_json: string | null
  readonly exclude_text: string | null
  readonly suggestion_text: string | null
}

/** `PATCH /api/rules/{code}` 的返回：规则本身 + **这次到底改没改**。 */
export interface RuleUpdateResponse extends RuleRow {
  readonly changed: boolean
}

/**
 * 激活前校验报告（`POST /api/rules/reload`）。
 *
 * ⚠️ 它**不是**"重新加载"：规则没有缓存，这个接口是**闸门**
 * （整批配置合法吗、覆盖率如何）。校验**包含停用的规则**。
 */
export interface RulesetReport {
  readonly total: number
  readonly active: number
  readonly inactive: number
  readonly categories: Readonly<Record<string, number>>
  /** 当前**启用**规则的规则集版本（进批次快照） */
  readonly ruleset_version: string
}

// ============================================================
// 日志与审计（`app/api/admin.py`）
// ============================================================

/** 日志级别（`app/enums.py::LogLevel`）。 */
export type LogLevel = 'debug' | 'info' | 'warning' | 'error'

/**
 * 一条运行日志（`admin._log_json`）。
 *
 * `log_content` **已由 `LogService` 脱敏**（键名黑名单 / 长文本摘要 /
 * 身份证手机邮箱模式）—— 合同正文与令牌不会出现在这里。
 * 即便如此，前端也**不得**把它写进 URL 或 console（材料 §Global Constraints）。
 */
export interface TaskLogRow {
  readonly log_id: number
  readonly task_id: number | null
  readonly log_level: LogLevel
  readonly log_type: string
  readonly log_content: string
  readonly error_code: string | null
  readonly correlation_id: string | null
  readonly created_at: IsoTime | null
}

/** 一条审计事件（`admin._audit_json`）。`task_id` 为 `null` 表示系统级事件。 */
export interface AuditEventRow {
  readonly event_id: number
  readonly task_id: number | null
  readonly actor_id: string | null
  readonly actor_name: string | null
  readonly action: string
  readonly target_type: string | null
  readonly target_id: string | null
  readonly correlation_id: string | null
  /** 只含标识与摘要（`result_id` / `content_digest` / `changed_fields`），**不含正文** */
  readonly detail: JsonValue
  readonly created_at: IsoTime | null
}

// ============================================================
// 人工重试（`app/api/admin.py::retry_task_route`）
// ============================================================

/**
 * 重试结果。
 *
 * `action` 区分**这次重试做了什么** —— "排了一个作业"与"重新武装了一次投递"
 * 是两种不同的干预（回写失败不新建作业：意图早已在库，失败的是送达）。
 * 从 `blocked_stage` 再推一遍会多出一份会漂移的判据。
 */
export interface RetryOutcome {
  readonly task_id: number
  readonly blocked_stage: string | null
  readonly resumed_status: TaskStatus
  readonly retry_count: number
  readonly reason: string
  readonly action: string
  readonly job_id: number | null
  readonly job_type: JobType | null
  readonly job_status: JobStatus | null
  readonly attempt_id: number | null
  readonly outbox_event_id: number | null
  readonly status_url: string
}
