"""领域枚举：系统内所有受控取值的唯一来源。

集中定义的目的：规则配置按此校验（非法取值让规则加载失败，而不是带病运行）、
避免字符串散落、以及与 `db/schema.sql` 的取值域说明保持同步。

全部继承 `(str, Enum)`，可直接比较、写入数据库、JSON 序列化。
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """字符串枚举基类。"""

    def __str__(self) -> str:
        # 便于 f-string 与日志输出取值本身，而不是 "TaskStatus.PENDING"
        return self.value


# ============================================================
# 任务与回写状态
# ============================================================


class TaskStatus(StrEnum):
    """审批任务状态（对应需求 2.4.4）。

    异常时进入 `BLOCKED`，人工重试后回到 `PARSING` 或 `REVIEWING`。
    """

    PENDING = "pending"
    PARSING = "parsing"
    REVIEWING = "reviewing"
    BLOCKED = "blocked"
    DONE = "done"


class WriteStatus(StrEnum):
    """评论回写状态 —— 只描述"这次回写操作走到哪一步"，严格保持四个值**不扩展**。

    - `NOT_WRITTEN`：尚未回写。**门禁拒绝时也是这个值**（根本没发起过回写）；
    - `WRITING` / `SUCCESS` / `FAILED`：已发起 / 对方确认成功 / 对方报错。

    ⚠️ 不要为了"区分责任方"而加值（曾短暂加过 `rejected`）：那会让同一个字段
    同时表达"是否允许发起回写"与"回写是否成功"。原因由 `WritebackReasonCode` 表达。
    """

    NOT_WRITTEN = "not_written"
    WRITING = "writing"
    SUCCESS = "success"
    FAILED = "failed"


class WritebackReasonCode(StrEnum):
    """回写未成功的**稳定原因码**（成功时为 NULL）。

    与 `WriteStatus` 是两个正交维度：状态回答"走到哪一步"，原因回答"为什么没成功"。
    门禁拒绝时**没有发起回写**，因此 `write_status = not_written` +
    `reason_code = WRITEBACK_POLICY_DENIED`；外部调用失败才是 `failed` + `APPROVAL_API_ERROR`。
    """

    # ---------- 门禁拒绝（未发起回写 → write_status = not_written）----------
    POLICY_DENIED = "WRITEBACK_POLICY_DENIED"  # harness 门禁未通过
    TASK_NOT_DONE = "TASK_NOT_DONE"  # 任务尚未 done
    RESULT_MISSING = "RESULT_MISSING"  # 没有可回写的审查结果
    CONTEXT_NOT_VALID = "CONTEXT_NOT_VALID"  # 立场上下文不可信（缺失或冲突）
    MANUAL_CONFIRM_REQUIRED = "MANUAL_CONFIRM_REQUIRED"  # 高风险尚未人工确认
    ALREADY_WRITTEN = "ALREADY_WRITTEN"  # 已成功回写过（幂等拒绝）
    COMMENT_TEXT_MISSING = "COMMENT_TEXT_MISSING"  # 结果没有可回写的正文

    # ---------- 外部调用失败（已发起 → write_status = failed）----------
    APPROVAL_API_ERROR = "APPROVAL_API_ERROR"  # 审批系统返回错误
    APPROVAL_API_TIMEOUT = "APPROVAL_API_TIMEOUT"  # 调用超时
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"  # 同幂等键但内容不同


class RuleStatus(StrEnum):
    """规则启停用状态（`review_rules.rule_status` 的 CHECK 取值域）。

    ⚠️ 它**不是**"规则配置是否合法"：配置合法性由
    `app/rules/validation.py` 判定，与启停用无关 ——
    一条停用的规则照样必须能通过校验（见 `activate_validation` 的说明）。
    """

    ACTIVE = "active"
    INACTIVE = "inactive"


class RunStatus(StrEnum):
    """审查批次状态（`review_runs`）。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ReviewStatus(StrEnum):
    """结论完整性。

    与单条规则的 `EvaluationStatus` 是**两个层级**：这是"整份结论可不可信"，
    那是"某一条规则判不判得了"。
    """

    COMPLETE = "complete"
    NEEDS_REVIEW = "needs_review"


# ============================================================
# 后台作业（企业内部执行状态，与业务状态严格分离）
# ============================================================


class JobStatus(StrEnum):
    """作业状态（`workflow_jobs`）—— 表达 **Worker 执行情况**，与 `TaskStatus`
    是**两个层级**，不可混用。

    一个任务对应多个作业（拉取、解析、审查、回写各一个），
    某个作业失败不代表任务阻塞 —— 只有自动重试耗尽才是 `blocked`。
    """

    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobType(StrEnum):
    """作业类型，用于构造幂等键与检查点定位。"""

    PULL = "pull"
    DETAIL = "detail"
    DOWNLOAD = "download"
    PARSE = "parse"
    RULE = "rule"
    RESULT = "result"
    WRITEBACK = "writeback"


class DownloadStatus(StrEnum):
    """附件下载状态（取值域与 `approval_attachments.download_status` 的 CHECK 一致）。

    ⚠️ 与 `contract_parses.parse_status` 的拼写不同（这里 `SUCCESS`，那里 `succeeded`）——
    两者是不同表的既有约定，**不要为了"统一"而改其中一个**：
    那会同时破坏数据库 CHECK 与需求文档的字段约定。
    """

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class ParseStatus(StrEnum):
    """单份附件的解析记录状态（`contract_parses.parse_status`）。

    ⚠️ 与 `TaskStatus` **不是一回事**，但两者的取值里都有 `pending` / `parsing` /
    `blocked` —— 正是这种重合最容易让人把它们当成一个东西。区别在于粒度：

    | 枚举 | 粒度 |
    | --- | --- |
    | `TaskStatus` | **任务**级（业务审查进度） |
    | `ParseStatus` | **单份附件**级 |

    一份附件解析失败，任务可能仍在 `parsing`（别的附件还在跑）。
    混用会让"任务级 `blocked`"与"附件级 `failed`"互相掩盖：
    看到任务 `parsing` 就以为一切正常，而某份附件其实已经永久失败。

    ⚠️ 前三个取值参与**缓存占位闸门**（部分唯一索引 `WHERE parse_status IN (...)`），
    `FAILED` / `BLOCKED` **不参与** —— 那正是"失败后可以重新解析"能成立的原因。
    两者的取值集合必须与 `db/schema.sql` 的 CHECK 与部分索引**逐字一致**，
    由 `tests/test_schema_consistency.py` 两侧比对守住。
    """

    PENDING = "pending"
    PARSING = "parsing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"

    @classmethod
    def cache_gate_values(cls) -> frozenset[str]:
        """参与缓存占位闸门的取值（与部分唯一索引的 WHERE 逐字一致）。

        做成类方法而不是散落的字面量：索引的 WHERE 与这里的集合一旦不一致，
        表现是"并发下偶尔插进去两行"或"失败后无法重新解析" ——
        两者都不报错，只在数据里显形。
        """
        return frozenset({cls.PENDING, cls.PARSING, cls.SUCCEEDED})


class LogLevel(StrEnum):
    """日志级别（取值域与 `task_logs.log_level` 的 CHECK 一致）。"""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LogType(StrEnum):
    """日志类型（业务事件来源）。

    与作业类型基本对齐，另加 `SYSTEM` 表示不属于任何单一任务的系统级事件。
    受控取值让日志界面能稳定按来源过滤，而不是对自由文本做模糊匹配。
    """

    SYSTEM = "system"
    PULL = "pull"
    DETAIL = "detail"
    DOWNLOAD = "download"
    PARSE = "parse"
    RULE = "rule"
    RESULT = "result"
    WRITEBACK = "writeback"


# ============================================================
# Outbox 与审计（M6：回写的事务性意图与不可变审计账）
# ============================================================


class OutboxStatus(StrEnum):
    """Outbox 事件状态（`outbox_events.event_status`）。

    只有三个值：领取**不改状态**（仍是 `PENDING`），互斥靠租约字段 ——
    若引入 `dispatching` 之类的中间态，"已领取但进程死亡"就必须额外对账，
    状态机越宽，恰好一次越难证明。
    """

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class OutboxEventType(StrEnum):
    """Outbox 事件类型 —— 派发器只处理**已知**类型。

    未知类型必须拒绝并保留事件（而不是标记送达）：
    "跳过"会让事件静默丢失，且丢失方式在统计上看不出来。
    """

    WRITE_APPROVAL_COMMENT = "WRITE_APPROVAL_COMMENT"


class AuditAction(StrEnum):
    """审计动作（`audit_events.action`）。

    审计账要能按动作聚合统计（"本周确认了多少份结果"），
    因此取值受控；新增动作必须同步 `db/schema.sql` 的 CHECK。
    """

    #: 人工确认了某版本的审查结果（绑定当时的 content_digest）
    RESULT_CONFIRMED = "RESULT_CONFIRMED"
    #: 人工确认了**权威审查上下文**（我方 / 合同标签 / 业务角色 / 合同类型）。
    #: ⚠️ 与 `RESULT_CONFIRMED` 是**两个**动作，不是一个的两个阶段：
    #: 立场确认回答"我们代表谁"，结果确认回答"这份结论与正文我认可"。
    #: 合并成一个动作时，"我只想确认立场，结果还要再看看"就做不到了。
    CONTEXT_CONFIRMED = "CONTEXT_CONFIRMED"
    #: 发起回写（业务事务内写下 Outbox 意图）
    WRITEBACK_REQUESTED = "WRITEBACK_REQUESTED"
    #: 回写送达（派发器确认外部效果成立）
    WRITEBACK_DELIVERED = "WRITEBACK_DELIVERED"
    #: 人工重试一条 `blocked` 任务（M7）。
    #: ⚠️ 与 `CONTEXT_CONFIRMED` / `RESULT_CONFIRMED` 并列，而不是它们的子动作：
    #: 重试回答"这次干预是谁做的、从哪个检查点恢复"，
    #: 而"是否允许恢复"本身不是一个人工判断（`blocked` 才可重试）。
    #: 不记录它，事后就无法回答"这条任务为什么从 blocked 变回了 reviewing"。
    TASK_RETRIED = "TASK_RETRIED"
    #: 新建一条审查规则（M7）。
    RULE_CREATED = "RULE_CREATED"
    #: 修改一条审查规则 —— 内容、版本或启停用（M7）。
    #: ⚠️ 与 `RULE_CREATED` 分开：审计账要能回答"本周改了几条规则"，
    #: 合并后新建与修改会一起计数，而两者的风险完全不同
    #: （新建只影响之后的新批次，修改可能改变既有版本的语义）。
    RULE_UPDATED = "RULE_UPDATED"


class ErrorCode(StrEnum):
    """**稳定机器错误码** —— 任务阻塞与作业失败的判据。

    必须与可读文本分开：中文说明会改、会翻译、会因人而异，
    而统计、看板与自动化断言需要一个永不变形的键。

    ⚠️ 最容易分错的是存储类："超时 / 连不上"是**瞬时**（重试通常就好），
    "路径越界 / 权限不足 / 校验和不符"是**确定性**（重试一万次也一样）。
    把前者当确定性错误，会让一次网络抖动把任务打成永久失败。
    """

    # ---------- 瞬时错误（可退避重试）----------
    APPROVAL_API_ERROR = "APPROVAL_API_ERROR"  # 审批系统 5xx
    APPROVAL_API_TIMEOUT = "APPROVAL_API_TIMEOUT"  # 调用超时
    APPROVAL_UNREACHABLE = "APPROVAL_UNREACHABLE"  # 连不上
    # 限流（HTTP 429）是**瞬时**错误。
    # ⚠️ 它会被"其他 4xx 一律确定性"的兜底分支误伤 —— 4xx 并不都是"请求有问题"，
    #    429 恰恰是"请稍后再来"。判成永久失败会让任务在对方限流时直接 blocked。
    APPROVAL_RATE_LIMITED = "APPROVAL_RATE_LIMITED"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"  # 存储超时 / 连接失败 / 后端 5xx
    # 推理与渲染的**超时**是瞬时错误 —— 重试通常就好。
    # ⚠️ 这两个码必须同时进 RETRYABLE_ERROR_CODES：is_retryable() 对**未知码返回 False**，
    #    漏登记的表现是"一次推理超时把任务直接打死"，而日志里的错误码看起来完全正常。
    OCR_INFERENCE_TIMEOUT = "OCR_INFERENCE_TIMEOUT"
    PDF_RENDER_TIMEOUT = "PDF_RENDER_TIMEOUT"
    # ⚠️ 与 `OCR_INFERENCE_TIMEOUT` 必须分开：把解码失败、模型运行时错误
    #    都记成"超时"，会让看板上"超时"这个数字失去意义 ——
    #    而"本周超时多少次"正是判断 Worker 是否健康的直接信号。
    #    两者都按**瞬时**处理（重试有界、代价可控），但**码不能混**。
    OCR_INFERENCE_FAILED = "OCR_INFERENCE_FAILED"
    # 身份提供方（JWKS 端点 / 令牌内省服务）暂时不可达（M7）。
    # ⚠️ 这是**瞬时**错误，不是认证失败：拿不到公钥时我们**无法判断**令牌是否有效，
    #    而"无法判断"与"令牌无效"是完全不同的两件事。
    #    判成 401 会让调用方**丢弃一份可能完全有效的令牌**去重新登录（甚至清掉本地会话），
    #    而正确处置只是稍后重试 —— 这正是本项目反复强调的"错误分类错了，
    #    处置就一定错"。
    IDENTITY_PROVIDER_UNAVAILABLE = "IDENTITY_PROVIDER_UNAVAILABLE"

    # ---------- 确定性错误（立即 blocked，不重试）----------
    # ⚠️ AUTH_FAILED 是**出站**方向的：**审批系统**拒绝了我们的凭据
    #    （`PermanentGatewayError`，映射 502，"我们的配置有问题"）。
    #    不要拿它表示**入站**的 401/403 —— 见下面两个码。
    AUTH_FAILED = "AUTH_FAILED"  # 401 / 403
    # ---------- 身份与授权（入站方向，M7）----------
    # 与 AUTH_FAILED 分开的理由不只是"方向不同"，而是**调用方的处置不同**：
    #   AUTH_FAILED（502）→ 查我们的凭据配置，调用方做什么都没用；
    #   AUTHENTICATION_REQUIRED（401）→ 调用方**去拿一份新身份**再来；
    #   PERMISSION_DENIED（403）→ 身份没问题，换账号或找管理员加权限。
    # 合成一个码时，"我没登录"与"我登录了但没权限"在客户端看起来一样，
    # 于是客户端的处置只能靠猜 —— 有一半概率猜错。
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"  # 401：没有可信身份
    PERMISSION_DENIED = "PERMISSION_DENIED"  # 403：身份可信但缺权限
    INSTANCE_NOT_FOUND = "INSTANCE_NOT_FOUND"  # 外部审批系统中不存在该审批单
    # 本系统里没有这条任务（还没拉取 / 还没同步详情）。
    # 与 INSTANCE_NOT_FOUND 的区别很重要：那是"外部系统说没有"，这是"我们这边还没有"，
    # 处理动作完全不同（前者找业务方，后者先跑一次拉取）。
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    # ⚠️ 与 WritebackReasonCode.IDEMPOTENCY_CONFLICT 不是一回事：那个是**本系统侧**
    #    判断"已回写过，主动拒绝"，这个是**外部系统**返回的契约级拒绝（HTTP 409）。
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    ATTACHMENT_MISSING = "ATTACHMENT_MISSING"  # 附件已被删除
    ATTACHMENT_EMPTY = "ATTACHMENT_EMPTY"  # 空文件
    ATTACHMENT_TYPE_NOT_ALLOWED = "ATTACHMENT_TYPE_NOT_ALLOWED"  # 类型不在白名单
    ATTACHMENT_TOO_LARGE = "ATTACHMENT_TOO_LARGE"  # 超过大小上限
    STORAGE_PATH_INVALID = "STORAGE_PATH_INVALID"  # 非法路径 / 目录穿越
    STORAGE_WRITE_DENIED = "STORAGE_WRITE_DENIED"  # 权限 / 配额不足
    OBJECT_NOT_FOUND = "OBJECT_NOT_FOUND"  # 对象存储中不存在该对象
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"  # 下载后 SHA-256 与声明不符
    INVALID_GATEWAY_RESPONSE = "INVALID_GATEWAY_RESPONSE"  # 响应体结构不符
    # 非法状态转换是**确定性**错误：重试永远不会变合法。它通常意味着代码缺陷，
    # 需要立即暴露而不是被重试掩盖。
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    # 【租约已不属于本次执行】两种情形共用一个码，因为它们在同一次崩溃里是**同一件事**：
    #   ① Worker 完成时条件更新影响行数为 0（租约被回收或转给了别人）；
    #   ② 回收时发现作业还在 running 但租约已过期。
    # 确定性：重试不会让"我已经不是持有者"变成"我是"。
    # 必须能统计：本周有多少作业因租约丢失而被作废，是判断 Worker 是否健康的直接信号。
    LEASE_LOST = "LEASE_LOST"
    # 【处理器抛出了非业务异常】—— 按 `app/errors.py` 的分工，这类是**代码缺陷**
    # （TypeError / KeyError 之类），不属于"可预期业务错误"，因此**不包装**成
    # 带业务语义的异常，只用一个独立码把它记下来，便于统计与定位。
    # 归**确定性**：缺陷不会因为重试而消失；重试只会白耗预算并推迟它被看见的时间。
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"
    # ---------- 规则管理与人工重试（M7）----------
    # 规则被改错的影响面是**全局的**（所有合同的结论都变了），因此这几个码必须
    # 各自独立、可统计 —— 它们回答的是三个不同的问题：
    #   RULE_NOT_FOUND      → "你给的 rule_code 不存在"（处置：核对 code）
    #   RULE_CONFIG_INVALID → "这条配置写错了"（处置：改配置，重试无用）
    #   RULE_VERSION_IN_USE → "这个版本已经被用过了"（处置：提升 rule_version）
    # 合并成一个码时，排障的人会去改一个本来就对的东西。
    RULE_NOT_FOUND = "RULE_NOT_FOUND"
    RULE_CONFIG_INVALID = "RULE_CONFIG_INVALID"
    RULE_VERSION_IN_USE = "RULE_VERSION_IN_USE"
    # 【该失败位置没有可自动重跑的步骤】人工重试接口用。
    # ⚠️ 与 `INVALID_STATE_TRANSITION` **不是一回事**，不能合并：
    #   前者是"任务状态不对"（应等它跑完或先让它阻塞），
    #   后者是"这个任务确实阻塞了，但这一步的恢复入口不在本接口"
    #   （下载失败要重跑工具 3、拉取失败要重跑工具 1）。
    #   合并会让操作员反复重试一个注定拒绝的请求，而提示里
    #   没有任何线索告诉他"该去点哪个按钮"。
    RETRY_NOT_SUPPORTED = "RETRY_NOT_SUPPORTED"
    # 【引用型查询的目标不存在】`GET /api/jobs/{id}`、`GET /api/parses/{id}` 用。
    # ⚠️ 与 `TASK_NOT_FOUND` **不是一回事**，不能合并：
    #    `TASK_NOT_FOUND` 是**业务结论** —— "本系统里还没有这条任务（先跑一次拉取）"，
    #    处置动作是**去拉取**；
    #    本码是**引用错误** —— "你给的这个 id 不存在"，处置动作是**核对 id**。
    #    合并会让调用方拿着一个不存在的 job_id 去做一次无用的拉取。
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"

    # ---------- M4 解析（确定性：重试一万次结果相同）----------
    DOCUMENT_EMPTY = "DOCUMENT_EMPTY"  # 全部页面确认无文字（门禁用，非单页）
    OCR_UNRECOGNIZABLE = "OCR_UNRECOGNIZABLE"  # 扫描页无法可靠识别
    # ⚠️ 与 OCR_INFERENCE_TIMEOUT 相反：模型缺失是**配置错误**，
    #    登记成可重试会让它重试到预算耗尽，把"模型路径写错"掩盖成"服务不稳定"。
    OCR_MODEL_UNAVAILABLE = "OCR_MODEL_UNAVAILABLE"
    PDF_ENCRYPTED = "PDF_ENCRYPTED"  # 加密文档
    PDF_CORRUPT = "PDF_CORRUPT"  # 损坏文档
    PDF_TOO_MANY_PAGES = "PDF_TOO_MANY_PAGES"  # 超过页数上限
    PDF_TOO_LARGE_PIXELS = "PDF_TOO_LARGE_PIXELS"  # 渲染像素超限（内存保护）
    # ⚠️ 与 `PDF_CORRUPT` **不是一回事**：白名单里现在**不止 PDF**（§4.9 修-17），
    #    当一个文件既不是可解析的 PDF、也不是可识别的图片时，
    #    说"PDF 损坏"是**指错了对象** —— 而错误码要回答的正是"到底哪里不行"。
    DOCUMENT_FORMAT_UNRECOGNIZED = "DOCUMENT_FORMAT_UNRECOGNIZED"


#: 瞬时错误集合 —— **唯一**的重试判据来源，避免各处自行判断。
RETRYABLE_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.APPROVAL_API_ERROR,
        ErrorCode.APPROVAL_API_TIMEOUT,
        ErrorCode.APPROVAL_UNREACHABLE,
        ErrorCode.APPROVAL_RATE_LIMITED,
        ErrorCode.STORAGE_UNAVAILABLE,
        ErrorCode.OCR_INFERENCE_TIMEOUT,
        ErrorCode.PDF_RENDER_TIMEOUT,
        ErrorCode.OCR_INFERENCE_FAILED,
        # ⚠️ 必须登记：`is_retryable()` 对未知码返回 False，
        #    漏登记的表现是"身份提供方抖动一次就把调用方打成 401"。
        ErrorCode.IDENTITY_PROVIDER_UNAVAILABLE,
    }
)


def is_retryable(code: ErrorCode | str | None) -> bool:
    """错误码是否属于"值得退避重试"的瞬时错误（`None` 与未知取值都视为不可重试）。"""
    if code is None:
        return False
    try:
        return ErrorCode(code) in RETRYABLE_ERROR_CODES
    except ValueError:
        return False


# ============================================================
# 权威审查上下文（业务事实，由审批系统或人工确认提供）
# ============================================================


class ContextSource(StrEnum):
    """权威审查上下文的来源。"""

    APPROVAL_SYSTEM = "approval_system"
    MANUAL = "manual"


class ContextStatus(StrEnum):
    """权威审查上下文状态 —— **回写门禁的输入**。

    ⚠️ 两个 `CONFIRMED` 互相独立，不可互推：
    本枚举的表示"**审查立场**已人工确认"，
    `review_results.manual_confirmed` 表示"**审查结果与回写正文**已人工确认"。
    """

    COMPLETE = "complete"
    MISSING = "missing"
    CONFLICT = "conflict"
    CONFIRMED = "confirmed"


class ContractLabel(StrEnum):
    """我方在合同正文中的**形式标签**。

    ⚠️ 它**不携带业务语义**：销售合同里甲方通常是卖方，
    绝不能把"甲方"默认等同于"采购方"。
    """

    PARTY_A = "party_a"
    PARTY_B = "party_b"
    OTHER = "other"
    UNKNOWN = "unknown"


class BusinessRole(StrEnum):
    """我方在交易中的**实际身份** —— 规则方向判断的依据（"这对我是好是坏"）。

    付款类规则中我方是 `BUYER` 则预付款比例高是风险，是 `SELLER` 则不适用。
    """

    BUYER = "buyer"
    SELLER = "seller"
    CUSTOMER = "customer"
    SERVICE_PROVIDER = "service_provider"
    LICENSOR = "licensor"
    LICENSEE = "licensee"
    OTHER = "other"
    UNKNOWN = "unknown"


class ContractType(StrEnum):
    """合同业务分类 —— 判断规则**是否适用**的依据。

    例：知识产权类规则只对软件、研发、外包等涉及成果权属的合同适用；
    标准商品采购合同应为 `not_applicable`，而不是报"知识产权缺失"。
    """

    PROCUREMENT = "procurement"
    SALES = "sales"
    SOFTWARE_SERVICE = "software_service"
    DEVELOPMENT = "development"
    OUTSOURCING = "outsourcing"
    LEASE = "lease"
    OTHER = "other"
    UNKNOWN = "unknown"


# ============================================================
# 解析证据
# ============================================================


class Precision(StrEnum):
    """定位精度：诚实标注证据的可核验程度，不夸大。

    单一维度，服务于 M3 的 `rule_hits.evidence_position`。
    M4 的标准文档把"文本精度"与"几何精度"**拆成两个维度**
    （`TextPrecision` / `BboxPrecision`）—— 理由见后者的 docstring。
    """

    CHAR = "char"  # 文本层 PDF，可精确到字符
    LINE = "line"  # 扫描件 OCR，只能到行级
    NONE = "none"  # 未定位


class TextPrecision(StrEnum):
    """**文本**定位精度 —— 决定"证据文本是否可信"。"""

    CHAR = "char"  # 有字符区间，可精确定位到字符
    LINE = "line"  # 只有行级文本（OCR 通常如此）
    NONE = "none"


class BboxPrecision(StrEnum):
    """**几何**定位精度 —— 决定 M8 画字符框还是块框（并如实标注）。

    ⚠️ 与 `TextPrecision` **不能共用一个枚举**：两者取值域不同 ——
    `BLOCK` 只对几何有意义（一段文本的"块级文本精度"不是一种精度），
    `LINE` 只对文本有意义（块级 bbox 不存在"行级几何"这一档）。
    共用时每个使用点都要额外说明"哪些值在这里非法"，漏说明的地方就会写出矛盾。
    """

    CHAR = "char"  # 有逐字符 bbox
    LINE = "line"  # 只有行级 bbox
    BLOCK = "block"  # 只有块级 bbox
    NONE = "none"


class PageStatus(StrEnum):
    """**页级**处理结论 —— 四态，且必须按处理阶段区分。

    ⚠️ `BLANK` 与 `UNCERTAIN` 不是同义词，混用会丢掉内容或谎报故障：

    | 取值 | 事实 | 判错会怎样 |
    | --- | --- | --- |
    | `OK` | 确实读到了内容 | —— |
    | `BLANK` | **可靠识别后**确认无文字 | 把没读过的页判成空白 = **在读取之前宣布没内容** |
    | `UNCERTAIN` | 有输出但置信度/覆盖不足 | 归 `BLANK` 丢内容；归 `FAILED` 谎报故障 |
    | `FAILED` | OCR 或渲染抛错 | 与 `BLANK` 混淆会让"失败页"被当成"空页"处理 |

    "没有原生文本"**不是** `BLANK`：扫描件的每一页都没有原生文本，
    而它们恰恰是最需要 OCR 的页面。因此无文本层时**先走 OCR**，
    由 OCR 的结果决定是 `BLANK` 还是 `UNCERTAIN`。
    """

    OK = "ok"
    BLANK = "blank"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


class FieldStatus(StrEnum):
    """解析字段状态 —— 本项目最关键的一个区分。

    - `NOT_FOUND`：**完成规定范围检索后确实没找到**。只有这个状态才可能判定"条款缺失"；
    - `UNCERTAIN`：存在疑似内容但证据不足 → 必须 `needs_review`；
    - `FAILED`：提取过程失败 → 必须 `needs_review`，**不能报缺失**。

    把 `FAILED` / `UNCERTAIN` 当成 `NOT_FOUND`，是"缺失类规则"误报的根源。
    """

    EXTRACTED = "extracted"
    NOT_FOUND = "not_found"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


# ============================================================
# 规则与规则评价
# ============================================================


class RiskLevel(StrEnum):
    """风险等级。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MatchMode(StrEnum):
    """规则匹配模式。

    `EXPR` 为扩展模式，用于**数值/字段比较**类规则（如预付款比例 > 30%）。
    """

    KEYWORD = "keyword"
    REGEX = "regex"
    LLM = "llm"
    EXPR = "expr"


class EvaluationStatus(StrEnum):
    """规则评价四态，**语义互斥**（一条规则在一个批次中恰好处于其中一种）。

    数据库物理列名为 `hit_status`（需求文档 2.4.9 规定），
    Python 属性名映射到该列，因此不存在重复列。
    """

    HIT = "hit"
    NOT_HIT = "not_hit"
    NOT_APPLICABLE = "not_applicable"
    NEEDS_REVIEW = "needs_review"


class ReasonCode(StrEnum):
    """稳定机器原因码。中文解释放 `reason_text`，原因码用于统计、看板与自动化断言。"""

    APPLICABILITY_NOT_MET = "APPLICABILITY_NOT_MET"
    CONTEXT_MISSING = "CONTEXT_MISSING"
    CONTEXT_CONFLICT = "CONTEXT_CONFLICT"
    EVIDENCE_UNCERTAIN = "EVIDENCE_UNCERTAIN"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    CONDITION_MATCHED = "CONDITION_MATCHED"
    CONDITION_NOT_MATCHED = "CONDITION_NOT_MATCHED"

    # ---------- M5 增补（均为**确定性**，重试一万次结果相同）----------
    #: 字段币种与规则期望币种不一致 → 数值**不可比**。
    #: ⚠️ 不能退化成"直接比较数值"：`USD 200,000` 与阈值 `CNY 1,200,000` 比出的大小
    #: 是无意义的，而它会以"命中/未命中"的形式给出一个**看起来很确定的错误结论**。
    #: M4 §4.8 把币种拆成独立字段，为的就是让这件事**可判断**。
    CURRENCY_NOT_COMPARABLE = "CURRENCY_NOT_COMPARABLE"
    #: `expr` 规则缺 `value`（`is_null` / `not_null` 之外的操作符必须有阈值）。
    #: 这是**配置错误**，不是判断不足 —— 但结论仍记 `needs_review`，
    #: 因为"按 0 比较"会静默产出 not_hit，把配置错误伪装成"这条没问题"。
    THRESHOLD_NOT_CONFIGURED = "THRESHOLD_NOT_CONFIGURED"


# ============================================================
# 聚合口径（供 M5 规则评价引擎与结构校验共用）
# ============================================================

# 只有"命中"参与总风险等级聚合；其余三态一律不计入风险
RISK_CONTRIBUTING_STATUSES: frozenset[EvaluationStatus] = frozenset(
    {EvaluationStatus.HIT}
)

# 需要人工介入、禁止自动回写的状态
REQUIRES_HUMAN_STATUSES: frozenset[EvaluationStatus] = frozenset(
    {EvaluationStatus.NEEDS_REVIEW}
)
