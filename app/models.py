"""SQLAlchemy ORM 映射，与 `db/schema.sql` 一一对应。

**同步约定**：表结构的真相来源是 `db/schema.sql`，本文件只做 Python 侧映射。
改表结构必须**两边同时改**，否则会出现"ORM 能写、SQL 里没这列"这类难查的错误。

一致性由两组测试守住，**分工不同**：

- `tests/test_schema_consistency.py` —— 表集合、列集合、外键、唯一约束；
- `tests/test_data_integrity.py` —— **CHECK 表达式逐字比对**，以及部分唯一索引的判定条件。
  后者存在的理由：M9 会用 Alembic 从 **ORM 元数据**生成 PostgreSQL 迁移，
  ORM 少一条 CHECK 或一个索引，迁移出的库会**静默丢掉那条约束**。

**命名与物理列名有意不一致**：
    `RuleEvaluation` → `rule_hits`（该表已扩展为"规则评价"，不再只存命中）
    `RuleEvaluation.evaluation_status` → `rule_hits.hit_status`
    （需求 2.4.9 规定了列名，语义需要四态）

**两层默认值与级联**：`default=` 是 Python 侧、`server_default=` 是数据库侧，
两层都写是为了让"ORM 写入"与"直接执行 SQL"结果一致；级联同理 ——
`ondelete="CASCADE"` 由数据库保证（需 `app/db.py` 开启外键 PRAGMA），
`cascade="all, delete-orphan"` 由 ORM 保证。

**为什么用复合外键**：`review_runs` / `rule_hits` / `review_results` / `comment_logs`
都同时保存 `task_id` 与父级 id（需求 2.4.9 规定，不能删）。若只声明两个彼此独立的
单列外键，数据库就允许"任务 A + 任务 B 的解析结果"这类**跨任务拼接**，
且任何单列约束都发现不了。因此改用 `FOREIGN KEY (父级 id, task_id)` 复合外键，
父表提供 `UNIQUE (id, task_id)` 作为引用目标；代价是导航需逐级进行。
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import relationship

from app.db import Base

#: 缓存占位闸门的判定条件 —— 只有"进行中 / 已成功"的记录参与唯一约束。
#: 与 `db/schema.sql` 的同名索引**必须逐字一致**，由 `tests/test_data_integrity.py`
#: 的部分索引比对守住（与 CHECK 两侧比对的理由相同：ORM 是 M9 迁移的来源）。
_CACHE_GATE_PREDICATE = "parse_status IN ('pending', 'parsing', 'succeeded')"

# ============================================================
# 数据库层取值域约束（CHECK）
# ============================================================
# 为什么 ORM 侧也要写一遍（而不只在 schema.sql 里写）：
#
#   表结构的真相来源是 `db/schema.sql`，但 M9 会用 Alembic **从 ORM 元数据**
#   生成 PostgreSQL 迁移。若 ORM 不带 CHECK，迁移出来的表会**静默丢失全部
#   取值域约束** —— 建表看起来成功了，非法状态却重新变得可写入。
#
# 只在 Python 侧用枚举校验不够：脚本、手工 SQL、其他服务都能绕过 ORM。
# 两边逐字同步由 `tests/test_schema_consistency.py` 的 CHECK 文本比对守住。
_TABLE_CHECKS: dict[str, tuple[str, ...]] = {
    "approval_tasks": (
        "length(trim(provider)) > 0",
        "length(trim(tenant_id)) > 0",
        "length(trim(instance_id)) > 0",
        "length(trim(approval_code)) > 0",
        "task_status IN ('pending', 'parsing', 'reviewing', 'blocked', 'done')",
        "write_status IN ('not_written', 'writing', 'success', 'failed')",
        "context_source IN ('approval_system', 'manual')",
        "context_status IN ('complete', 'missing', 'conflict', 'confirmed')",
        "our_party_contract_label IS NULL OR our_party_contract_label IN "
        "('party_a', 'party_b', 'other', 'unknown')",
        "our_party_business_role IS NULL OR our_party_business_role IN "
        "('buyer', 'seller', 'customer', 'service_provider', 'licensor', "
        "'licensee', 'other', 'unknown')",
        "contract_type IS NULL OR contract_type IN "
        "('procurement', 'sales', 'software_service', 'development', "
        "'outsourcing', 'lease', 'other', 'unknown')",
        "retry_count >= 0",
        "blocked_stage IS NULL OR blocked_stage IN "
        "('pull', 'detail', 'download', 'parse', 'rule', 'result', 'writeback')",
    ),
    "approval_attachments": (
        "length(trim(attachment_id)) > 0",
        "length(trim(file_name)) > 0",
        "download_status IN ('pending', 'success', 'failed')",
        "file_size IS NULL OR file_size >= 0",
    ),
    "contract_parses": (
        "parse_status IN ('pending', 'parsing', 'succeeded', 'failed', 'blocked')",
        "parse_version >= 1",
        "text_coverage IS NULL OR (text_coverage >= 0 AND text_coverage <= 1)",
        "ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)",
        "ocr_pages IS NULL OR ocr_pages >= 0",
    ),
    "review_rules": (
        "length(trim(rule_code)) > 0",
        "risk_level IN ('low', 'medium', 'high')",
        "rule_status IN ('active', 'inactive')",
        "match_mode IN ('keyword', 'regex', 'llm', 'expr')",
        "priority >= 0",
        "rule_version >= 1",
    ),
    "review_runs": (
        "run_status IN ('running', 'completed', 'failed')",
        "version_no >= 1",
    ),
    "rule_hits": (
        "hit_status IN ('hit', 'not_hit', 'not_applicable', 'needs_review')",
        "risk_level IN ('low', 'medium', 'high')",
        "rule_version >= 1",
    ),
    "review_results": (
        "overall_risk_level IN ('low', 'medium', 'high')",
        "review_status IN ('complete', 'needs_review')",
        "manual_confirmed IN (0, 1)",
        "hit_count >= 0",
        "needs_review_count >= 0",
        "not_applicable_count >= 0",
        "confirmed_digest IS NULL OR content_digest IS NOT NULL",
        # M6 版本化：版本从 1 开始；空指纹让幂等判据失真
        "version_no >= 1",
        "length(trim(result_fingerprint)) > 0",
    ),
    "outbox_events": (
        "event_status IN ('pending', 'delivered', 'failed')",
        "attempt_no >= 0",
        "max_attempts >= 1",
        "length(trim(idempotency_key)) > 0",
        "length(trim(event_type)) > 0",
        "length(trim(aggregate_type)) > 0",
        "length(trim(payload_json)) > 0",
        "event_status <> 'delivered' OR delivered_at IS NOT NULL",
        "event_status = 'delivered' OR delivered_at IS NULL",
    ),
    "audit_events": (
        # ⚠️ 取值域必须与 `app/enums.py::AuditAction` 和 `db/schema.sql` 的 CHECK
        # **逐字一致**，由 `tests/test_m6_schema.py` 两侧比对守住。
        "action IN ('RESULT_CONFIRMED', 'CONTEXT_CONFIRMED', 'WRITEBACK_REQUESTED', "
        "'WRITEBACK_DELIVERED', 'TASK_RETRIED', 'RULE_CREATED', 'RULE_UPDATED')",
        "length(trim(actor_name)) > 0",
        "length(trim(target_type)) > 0",
        "target_id >= 1",
    ),
    "comment_logs": (
        "write_status IN ('not_written', 'writing', 'success', 'failed')",
        "attempt_no >= 1",
        "length(trim(idempotency_key)) > 0",
    ),
    "task_logs": (
        "log_level IN ('debug', 'info', 'warning', 'error')",
        "length(trim(log_type)) > 0",
    ),
    "workflow_jobs": (
        "job_type IN ('pull', 'detail', 'download', 'parse', "
        "'rule', 'result', 'writeback')",
        "job_status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')",
        "length(trim(idempotency_key)) > 0",
        # 输入与摘要都不得为空串：空串等同于"没有输入"，
        # 会让"这份结果基于什么输入"这个唯一用途失去意义
        "length(trim(input_json)) > 0",
        "length(trim(input_digest)) > 0",
        "attempt_no >= 0",
        "max_attempts >= 1",
    ),
    "parse_artifacts": (
        "kind IN ('standard_document', 'ocr_pages')",
        "size_bytes >= 0",
        "artifact_version >= 1",
    ),
}


def _checks(table: str) -> tuple[CheckConstraint, ...]:
    """取出某表的 CHECK 约束对象（供各类的 `__table_args__` 使用）。

    每次调用都新建实例：`CheckConstraint` 是有状态的可附加对象，
    复用一个实例同时挂到多张表上会出错。
    """
    return tuple(CheckConstraint(expr) for expr in _TABLE_CHECKS[table])


# ============================================================
# 1. 审批任务
# ============================================================


class ApprovalTask(Base):
    """审批任务主表（`approval_tasks`）—— 一个审批单一行，整条业务链路的根节点。

    ⚠️ **权威审查上下文字段只能由拉取模块或人工写入**，解析模块只能读取。
    这是"业务事实"与"解析证据"分离的结构保证。
    """

    __tablename__ = "approval_tasks"
    # 【对象级闸门】同一审批单只能有一条任务记录。
    # 与 workflow_jobs.idempotency_key（操作级闸门）职责不同，不可互相替代。
    __table_args__ = (
        UniqueConstraint("provider", "tenant_id", "instance_id"),
        *_checks("approval_tasks"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    # ---------- 来源与去重键 ----------
    provider = Column(String, nullable=False, default="mock")
    # v1 固定默认租户；字段先留，避免接入第二个企业时全库迁移
    tenant_id = Column(String, nullable=False, default="default")
    # ⚠️ 不给 default：空串会让上面的唯一约束形同虚设
    instance_id = Column(String, nullable=False)

    # ⚠️ 不做全局唯一：需求 2.4.4 只要求"按唯一业务标识去重"，
    # 并未要求跨企业、跨审批平台唯一；全局唯一会让第二个企业接入时必然迁移。
    approval_code = Column(String, nullable=False)
    approval_title = Column(String)
    applicant_name = Column(String)
    apply_time = Column(String)  # ISO 字符串，规避 SQLite 与 PG 的日期类型差异
    # 审批表单原样留存：外部系统不可用或实例被删时历史详情仍可查看。
    # ⚠️ 其中的人员姓名、证件号、联系方式等**禁止进入日志**
    form_data_json = Column(Text)

    task_status = Column(String, nullable=False, default="pending")
    # 严格四值（见 WriteStatus）；"为什么没写成功"用 comment_logs.reason_code
    write_status = Column(String, nullable=False, default="not_written")

    # ---------- 权威审查上下文（业务事实）----------
    our_party_name = Column(String)
    # 我方在正文中的形式标签。⚠️ 不携带业务语义——销售合同里甲方通常是卖方
    our_party_contract_label = Column(String)
    # 我方在交易中的实际身份。规则方向判断依赖它，而不是合同标签
    our_party_business_role = Column(String)
    # 规则是否适用依赖它
    contract_type = Column(String)
    context_source = Column(String, nullable=False, default="approval_system")
    # ⚠️ confirmed 表示"审查立场已人工确认"，
    # 与 review_results.manual_confirmed（结果与正文确认）**互相独立**
    context_status = Column(String, nullable=False, default="missing")
    # 【conflict 的两个来源对照】（M8）：{"declared": {...}, "confirmed": {...}}
    # ⚠️ 只在 `context_status='conflict'` 时非空；它不是"历史快照"，
    # 而是一份**待裁定的对照表** —— 裁定完成后必须清掉，
    # 否则下一次看这条任务的人会以为冲突还在。
    context_conflict_json = Column(Text)

    block_reason = Column(Text)
    # 失败**位置**，供人工重试从检查点恢复（§7.4）
    blocked_stage = Column(String)
    # 机器判据（统计/看板/断言）；给人看的中文说明走 block_reason
    last_error_code = Column(String)
    retry_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, server_default=func.current_timestamp())
    # ⚠️ 必须带 `onupdate`：SQLite 的 DEFAULT CURRENT_TIMESTAMP **只在插入时生效**，
    # 少了它 `updated_at` 会永远停在创建时间，而"重复拉取只刷新已有记录"要靠它体现。
    updated_at = Column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    # 级联链：approval_tasks → contract_parses → review_runs
    #        → {rule_hits, review_results} → comment_logs
    #        approval_tasks → contract_parses → parse_artifacts（单列外键，经 parses 导航）
    # 路径上每级都用**复合外键**，因此这里不声明 runs / results / comment_logs：
    # 它们与任务之间没有"单列外键"这种可推导关系，硬声明会形成双重路径。
    attachments = relationship(
        "ApprovalAttachment", back_populates="task", cascade="all, delete-orphan"
    )
    parses = relationship(
        "ContractParse", back_populates="task", cascade="all, delete-orphan"
    )
    logs = relationship(
        "TaskLog", back_populates="task", cascade="all, delete-orphan"
    )
    jobs = relationship(
        "WorkflowJob", back_populates="task", cascade="all, delete-orphan"
    )


# ============================================================
# 2. 合同附件
# ============================================================


class ApprovalAttachment(Base):
    """合同附件（`approval_attachments`）。

    `file_path` 只记录**相对** `storage_root` 的路径，便于整体搬迁目录。
    """

    __tablename__ = "approval_attachments"
    # 同一审批单下附件编号不可重复：防重复下载，也让"重复拉取"幂等
    __table_args__ = (
        UniqueConstraint("task_id", "attachment_id"),
        *_checks("approval_attachments"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer, ForeignKey("approval_tasks.id", ondelete="CASCADE"), nullable=False
    )
    attachment_id = Column(String, nullable=False)  # 审批系统侧的附件编号
    file_name = Column(String, nullable=False)
    file_type = Column(String)  # pdf / png / jpg
    file_path = Column(String)  # 相对 storage_root
    file_size = Column(Integer)
    # SHA-256：既用于完整性校验，也作为"内容是否变化"的快速判断
    file_checksum = Column(String)
    # 长期保存位置（内容寻址）。与 file_path 是两个不同性质的位置：
    # file_path 是受控临时物化路径（供解析工具用），object_key 是长期保存位置；
    # ⚠️ 两者都**不下发**到接口响应（见 app/api/tools.py 工具 3）
    object_key = Column(String)
    # 取自响应头，供 M4 的解析路由判断走文本抽取还是 OCR
    content_type = Column(String)
    download_status = Column(String, nullable=False, default="pending")
    error_message = Column(Text)
    created_at = Column(DateTime, server_default=func.current_timestamp())

    task = relationship("ApprovalTask", back_populates="attachments")
    parses = relationship(
        "ContractParse", back_populates="attachment", cascade="all, delete-orphan"
    )


# ============================================================
# 3. 合同解析结果（解析证据）
# ============================================================


class ContractParse(Base):
    """合同解析结果（`contract_parses`）。

    一个附件可被多次解析（重试、换解析器版本），每次一行，由
    `source_checksum + parse_version` 区分。字段结构与 `status` 语义见 `schema.sql` ——
    其中 `not_found`（确实没有）与 `failed`（没解析出来）的区分，
    是"缺失类规则"不误报的关键。
    """

    __tablename__ = "contract_parses"
    __table_args__ = (
        # ⚠️ 解析版本按**附件**区分，而不是按文件校验和：
        # 两个审批单完全可能上传同一份模板合同（校验和相同），
        # 用 source_checksum 做唯一键会让第二个任务无法记录解析结果。
        UniqueConstraint("attachment_id", "parse_version"),
        # 作为 review_runs 复合外键的引用目标
        UniqueConstraint("id", "task_id"),
        # 【缓存占位闸门】**部分**唯一索引 —— 只在"进行中 / 已成功"时占位。
        # ⚠️ 不能用全局 UniqueConstraint：那会与"失败后可以重新解析"互斥
        # （第二次解析会撞 IntegrityError，而错误信息与"失败要能重试"看不出关联）。
        # 必须挂在 ORM 上：M9 用 Alembic 从 ORM 元数据生成迁移，
        # 少了它，迁移出的库会**静默丢掉这条闸门**（与 CHECK 同理）。
        Index(
            "uq_parse_cache_key",
            "attachment_id",
            "cache_key",
            unique=True,
            sqlite_where=text(_CACHE_GATE_PREDICATE),
            postgresql_where=text(_CACHE_GATE_PREDICATE),
        ),
        *_checks("contract_parses"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer, ForeignKey("approval_tasks.id", ondelete="CASCADE"), nullable=False
    )
    # NOT NULL：解析必然针对某个附件；允许为空会让
    # UNIQUE(attachment_id, parse_version) 因 SQLite 视 NULL 互不相同而失效
    attachment_id = Column(
        Integer,
        ForeignKey("approval_attachments.id", ondelete="CASCADE"),
        nullable=False,
    )
    basic_info_json = Column(Text)  # 合同基本信息（标题/编号/主体/金额/日期…）
    clause_info_json = Column(Text)  # 条款信息（付款/交付/验收/违约/保密…）
    parse_status = Column(String, nullable=False, default="pending")
    # 失败原因。需求明确"解析失败时不允许只返回空结果"
    parse_error = Column(Text)
    # 机器判据（ErrorCode）。⚠️ 与人读的 parse_error 不可合并：
    # 作业表的 last_error_code 只描述**当下**状态，而
    # "历次解析分别因为什么失败"（第 3 次 PDF_ENCRYPTED、第 1 次 DOCUMENT_EMPTY）
    # 只有解析记录自己能回答。
    parse_error_code = Column(String)

    # ---------- M4 解析版本追溯与缓存 ----------
    # parser_version（**由哪个解析器产生**）与 parse_version（**第几次解析**）是两件事。
    # 混用会让解析器升级后的旧记录仍显示"版本 2"→ 缓存判定命中同一条 →
    # **升级后的解析器永远不会被真正执行**。
    parser_name = Column(String)
    parser_version = Column(String)
    config_digest = Column(String)  # 参与解析的配置摘要（DPI、阈值、白名单、规范化版本…）
    cache_key = Column(String)  # 上述三者与 source_checksum 的合成键

    # ---------- 解析版本与质量（needs_review 的判定依据）----------
    source_checksum = Column(String)
    parse_version = Column(Integer, nullable=False, default=1)
    text_coverage = Column(Float)  # 可靠读取的页面比例 0–1
    ocr_confidence = Column(Float)  # 非 OCR 文档为空而不是 0（空与 0 必须区分）
    ocr_pages = Column(Integer, default=0)

    created_at = Column(DateTime, server_default=func.current_timestamp())

    task = relationship("ApprovalTask", back_populates="parses")
    attachment = relationship("ApprovalAttachment", back_populates="parses")
    runs = relationship(
        "ReviewRun", back_populates="parse", cascade="all, delete-orphan"
    )
    artifacts = relationship(
        "ParseArtifact", back_populates="parse", cascade="all, delete-orphan"
    )


# ============================================================
# 4. 审查规则
# ============================================================


class ReviewRule(Base):
    """审查规则（`review_rules`）。

    三类条件**必须分离**：`applies_when_json`（该不该判）/ `match_text`（命中条件）/
    `fallback_match_json`（LLM 不可用时的显式降级条件）。

    配置加载时由 Pydantic 受控校验：未知键、非法枚举、缺参数都必须让规则加载失败
    并写日志，**不允许带病运行**（M5 实现）。
    """

    __tablename__ = "review_rules"
    __table_args__ = _checks("review_rules")

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_code = Column(String, nullable=False, unique=True)  # 稳定标识
    rule_name = Column(String, nullable=False)
    rule_category = Column(String)  # 归属 11 类之一
    risk_level = Column(String, nullable=False, default="medium")
    rule_status = Column(String, nullable=False, default="active")  # active / inactive
    priority = Column(Integer, nullable=False, default=100)  # 数值小者先执行
    rule_version = Column(Integer, nullable=False, default=1)

    match_mode = Column(String, nullable=False)  # keyword / regex / llm / expr

    # 适用条件；NULL 表示全局适用。受控结构，不设计任意表达式语言
    applies_when_json = Column(Text)
    match_text = Column(Text, nullable=False)  # 风险命中条件（沿用需求字段名）
    fallback_match_json = Column(Text)  # LLM 不可用时的显式降级条件

    # 否定词表（逗号分隔），仅 keyword 模式生效：防止
    # "甲方不承担保密义务"被误判为"存在保密条款"
    exclude_text = Column(Text)
    suggestion_text = Column(Text)
    # 同 approval_tasks.updated_at：SQLite 的 DEFAULT 只在插入时生效，必须显式 onupdate
    updated_at = Column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )

    evaluations = relationship("RuleEvaluation", back_populates="rule")


# ============================================================
# 5. 审查批次
# ============================================================


class ReviewRun(Base):
    """审查批次（`review_runs`）：针对**一份确定的解析结果 + 一组上下文快照 +
    一个规则集版本**执行的一次完整规则评价。

    同一任务会有多批次（重新解析、修正立场后重跑），任何评价与结论都必须隶属
    一个明确批次，否则无法回答"这条结论是用哪份解析、哪组业务事实、哪个规则版本得出的"。
    """

    __tablename__ = "review_runs"
    __table_args__ = (
        UniqueConstraint("task_id", "version_no"),
        # 作为子表（rule_hits / review_results）复合外键的引用目标
        UniqueConstraint("id", "task_id"),
        # 【复合外键】批次使用的解析记录必须属于**同一任务**，禁止跨任务拼接。
        # 因此 task_id 不再单独指向 approval_tasks；删除任务的级联路径为
        # approval_tasks → contract_parses → review_runs，依然完整。
        ForeignKeyConstraint(
            ["parse_id", "task_id"],
            ["contract_parses.id", "contract_parses.task_id"],
            ondelete="CASCADE",
        ),
        *_checks("review_runs"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # task_id / parse_id 参与复合外键，因此不单独声明 ForeignKey
    task_id = Column(Integer, nullable=False)
    # 批次与解析绑定，保证结论可复现
    parse_id = Column(Integer, nullable=False)
    version_no = Column(Integer, nullable=False)  # 任务内批次序号，从 1 开始
    # 上下文快照：即使之后 approval_tasks 的立场字段被改，历史批次仍可还原判断依据
    context_snapshot_json = Column(Text)
    ruleset_version = Column(String)  # 规则集版本或摘要哈希

    # 【M5 / P2 审计】当时的**规则集内容**（规范化 JSON，按 rule_code 排序）。
    # 只存哈希是不够的：规则可被原地修改，之后还原不出当时的配置 ——
    # 而"这条结论是按哪版规则判的"是审计里第一个会被问到的。
    # 版本号由本列内容派生，因此两者不会漂移。
    ruleset_snapshot_json = Column(Text)

    # 【M5】批次绑定的六项输入里，除 parse_id / context_snapshot_json / ruleset_version 外的三项。
    # 做成列而不是塞进 `context_snapshot_json`：**单一语义**（业务事实 vs 模型/提示词/配置）
    # + **可聚合**（M11 要按 model_version 分组统计，塞进 JSON 就得扫全表解 JSON）。
    #
    # ⚠️ 取值是**配置快照**，不含运行期抖动：单次调用失败记在该规则的 `reason_code` 上，
    # 不得改这三列 —— 否则同一份配置重跑两次会得出不同的幂等键，幂等失效。
    model_version = Column(String)
    prompt_version = Column(String)
    config_version = Column(String)
    run_status = Column(String, nullable=False, default="running")
    started_at = Column(DateTime, server_default=func.current_timestamp())
    finished_at = Column(DateTime)

    # 任务通过 parse 派生：run → parse → task（复合外键已保证 parse 属于同一任务）
    parse = relationship("ContractParse", back_populates="runs")
    evaluations = relationship(
        "RuleEvaluation", back_populates="run", cascade="all, delete-orphan"
    )
    results = relationship(
        "ReviewResult", back_populates="run", cascade="all, delete-orphan"
    )


# ============================================================
# 6. 规则评价（物理表名沿用 rule_hits）
# ============================================================


class RuleEvaluation(Base):
    """规则评价记录（物理表 `rule_hits`）。

    语义已从"命中记录"扩展为"**规则评价记录**"：每个批次的**每条启用规则恰好一条**，
    包括不适用的规则。这样才能回答"为什么这条规则没有报警"，
    也才能展示"本次评价 38 条：命中 3、未命中 21、不适用 12、需人工判断 2"。

    ⚠️ 物理列名 `hit_status` 是需求 2.4.9 规定的，Python 属性名是 `evaluation_status`，
    二者是同一列。
    """

    __tablename__ = "rule_hits"
    __table_args__ = (
        # 同一批次内每条规则恰好一条评价
        UniqueConstraint("run_id", "rule_id"),
        # 【复合外键】评价必须属于该任务自己的批次，禁止跨任务拼接
        ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["review_runs.id", "review_runs.task_id"],
            ondelete="CASCADE",
        ),
        *_checks("rule_hits"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # run_id / task_id 参与复合外键，因此不单独声明 ForeignKey；
    # task_id 是需求 2.4.9 规定的字段，保留
    run_id = Column(Integer, nullable=False)
    task_id = Column(Integer, nullable=False)
    rule_id = Column(Integer, ForeignKey("review_rules.id"), nullable=False)
    # 规则版本快照：规则日后被改，历史评价仍能还原当时的语义
    rule_version = Column(Integer, nullable=False, default=1)
    # 冗余一份风险等级：规则停用或改级后，历史结论仍可追溯
    risk_level = Column(String, nullable=False)

    # 【关键】四态：hit / not_hit / not_applicable / needs_review
    evaluation_status = Column(
        "hit_status", String, nullable=False, default="hit", index=False
    )

    reason_code = Column(String)  # CONTEXT_MISSING / EVIDENCE_UNCERTAIN / ...
    reason_text = Column(Text)  # 给审批人的中文解释

    # 主要证据（兼容需求字段名）：第一处、也是最重要的一处
    evidence_text = Column(Text)
    # JSON: {page, bbox, char_start, char_end, precision}
    evidence_position = Column(Text)
    # 全部证据：JSON [{text, position}, ...]（一条规则可能命中多个位置）
    evidence_json = Column(Text)
    # expr 模式的计算过程：{actual, op, threshold}
    hit_detail_json = Column(Text)

    created_at = Column(DateTime, server_default=func.current_timestamp())

    run = relationship("ReviewRun", back_populates="evaluations")
    rule = relationship("ReviewRule", back_populates="evaluations")


# ============================================================
# 7. 审查结果
# ============================================================


class ReviewResult(Base):
    """审查结果（`review_results`）。

    总风险等级只由 `hit` 聚合；`needs_review` 单独表达为"结论不完整"。
    """

    __tablename__ = "review_results"
    __table_args__ = (
        # 供子表 comment_logs 复合外键引用
        UniqueConstraint("id", "task_id"),
        # 【复合外键】结果必须属于该任务自己的批次，禁止跨任务拼接
        ForeignKeyConstraint(
            ["run_id", "task_id"],
            ["review_runs.id", "review_runs.task_id"],
            ondelete="CASCADE",
        ),
        # 【复合外键】新版本只能接替**本任务**的旧结果（M6）
        ForeignKeyConstraint(
            ["supersedes_result_id", "task_id"],
            ["review_results.id", "review_results.task_id"],
            ondelete="CASCADE",
        ),
        # M6 版本化：同任务同版本唯一；同批次同指纹唯一（重放复用判据）
        UniqueConstraint("task_id", "version_no"),
        UniqueConstraint("run_id", "result_fingerprint"),
        *_checks("review_results"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # task_id / run_id 参与复合外键，因此不单独声明 ForeignKey
    task_id = Column(Integer, nullable=False)
    run_id = Column(Integer, nullable=False)
    # 由 hit 聚合（high>=1 → high；medium>=3 → high；…）
    overall_risk_level = Column(String, nullable=False)
    summary_text = Column(Text)
    focus_points_json = Column(Text)
    comment_text = Column(Text)

    # 结论完整性（complete / needs_review）—— 与单条规则的四态是两个层级
    review_status = Column(String, nullable=False, default="complete")
    hit_count = Column(Integer, nullable=False, default=0)
    needs_review_count = Column(Integer, nullable=False, default=0)
    not_applicable_count = Column(Integer, nullable=False, default=0)

    # ---------- M6 版本化：同一任务的结果版本链 ----------
    version_no = Column(Integer, nullable=False, default=1)
    # 结果指纹：规范化(摘要, 关注点, 正文, 聚合口径, 规则输入) 的 SHA-256。
    # ⚠️ 与 content_digest 分工：后者是**回写正文**的摘要（人工确认绑定对象），
    #    前者是"内容与输入是否完全一致"的判据（重放复用）。合并会让
    #    "正文没变但规则输入变了"的新版本被误判为可复用旧版本。
    result_fingerprint = Column(String, nullable=False)
    # 本任务上一版结果的 id（v1 为 NULL）；复合外键禁止跨任务接替
    supersedes_result_id = Column(Integer)
    created_by = Column(String)  # 保存者留痕
    updated_at = Column(DateTime, onupdate=func.current_timestamp())

    # ---------- 人工确认与内容绑定 ----------
    # ⚠️ 这是"审查结果与回写正文"的确认，
    # 与 approval_tasks.context_status='confirmed'（立场确认）**互相独立**
    manual_confirmed = Column(Integer, nullable=False, default=0)
    confirmed_by = Column(String)
    content_digest = Column(String)  # 回写正文的 SHA-256
    # 确认时对应的摘要：必须与 content_digest 相等确认才有效；
    # 正文一旦变更，旧确认自动失效
    confirmed_digest = Column(String)
    confirmed_at = Column(DateTime)

    created_at = Column(DateTime, server_default=func.current_timestamp())

    run = relationship("ReviewRun", back_populates="results")
    comment_logs = relationship(
        "CommentLog", back_populates="review", cascade="all, delete-orphan"
    )


# ============================================================
# 8. 评论回写日志
# ============================================================


class CommentLog(Base):
    """评论回写日志（`comment_logs`）。

    幂等由 `idempotency_key` 的**数据库唯一约束**保证 —— "先查后写"在并发下会穿透，
    唯一约束才是最后防线。
    """

    __tablename__ = "comment_logs"
    __table_args__ = (
        # 【复合外键】回写日志必须指向本任务自己的审查结果
        ForeignKeyConstraint(
            ["review_id", "task_id"],
            ["review_results.id", "review_results.task_id"],
            ondelete="CASCADE",
        ),
        *_checks("comment_logs"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # task_id / review_id 参与复合外键，因此不单独声明 ForeignKey
    task_id = Column(Integer, nullable=False)
    review_id = Column(Integer, nullable=False)
    # ⚠️ 门禁拒绝时**没有发起回写**，状态就是 not_written，不要为此新增状态值
    write_status = Column(String, nullable=False, default="not_written")
    # 与 write_status 是两个正交维度：
    #   门禁拒绝 → WRITEBACK_POLICY_DENIED + not_written
    #   外部失败 → APPROVAL_API_ERROR + failed
    reason_code = Column(String)
    reason_text = Column(Text)  # 给人看；程序判据一律用 reason_code
    content_digest = Column(String)
    # SHA256(规范化(provider, tenant_id, instance_id, result_id, content_digest))：
    # 不含 approval_code（重新拉取后可能变化），含 digest（正文变了就是另一次回写）
    idempotency_key = Column(String, nullable=False, unique=True)
    # **外部系统**返回的原始文本；门禁拒绝时没有外部调用，故为空
    write_response_text = Column(Text)
    attempt_no = Column(Integer, nullable=False, default=1)
    operator_name = Column(String)  # 轻量身份留痕，非账号体系

    created_at = Column(DateTime, server_default=func.current_timestamp())

    review = relationship("ReviewResult", back_populates="comment_logs")


# ============================================================
# 9. 全链路日志
# ============================================================


class TaskLog(Base):
    """全链路日志（`task_logs`）—— 各模块关键动作写一条，供排障与验收过程回放。"""

    __tablename__ = "task_logs"
    __table_args__ = _checks("task_logs")

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 可为空：系统级日志（如规则热更新）不属于任何具体任务
    task_id = Column(Integer, ForeignKey("approval_tasks.id", ondelete="CASCADE"))
    log_level = Column(String, nullable=False, default="info")  # debug/info/warning/error
    log_type = Column(String, nullable=False)  # pull / parse / rule / writeback …
    log_content = Column(Text)  # 正文，前缀带 [operator] 体现操作人
    # 稳定错误码——**结构化**保存，不塞进 log_content：
    # 自由文本无法统计、告警或断言，否则"本周多少次超时"要退化成正则考古
    error_code = Column(String)
    # 【关联 ID】（§4.7）：请求入口绑定 → LogService 自动带上。
    # ⚠️ 必须落库：Worker 在**另一个进程**，contextvar 传不过去，只能读回来再注入。
    correlation_id = Column(String)
    created_at = Column(DateTime, server_default=func.current_timestamp())

    task = relationship("ApprovalTask", back_populates="logs")


# ============================================================
# 10. 后台作业（企业内部执行状态）
# ============================================================


class WorkflowJob(Base):
    """后台作业（`workflow_jobs`）。

    ⚠️ 表达的是 **Worker 执行情况**，与 `ApprovalTask.task_status`（业务审查进度）
    是**两个层级**，严禁混用：任务"进行中"而某个作业"失败"并不等于任务阻塞 ——
    只有自动重试耗尽才是 `blocked`。

    M3 只**写入**台账（工具 1~3 同步执行完成），不引入 Worker；
    M4 引入 Worker 后改为入队执行，**本表结构与写入路径都不需要变**。
    """

    __tablename__ = "workflow_jobs"
    __table_args__ = _checks("workflow_jobs")

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 可为空：拉取作业不属于任何单个任务
    task_id = Column(Integer, ForeignKey("approval_tasks.id", ondelete="CASCADE"))
    job_type = Column(String, nullable=False)  # JobType

    # 【操作级闸门】= {job_type}:{业务标识}:{输入版本}
    # ⚠️ 不得只含 instance_id：审批表单与附件都会变化，只认审批单号会让
    #    同一审批单的**第二次同步被永久拒绝**。对象级闸门由 ApprovalTask 的
    #    UNIQUE(provider, tenant_id, instance_id) 负责。
    #    版本取不到时退化为含请求指纹的一次性键：宁可多跑一次，也不能把对象卡死。
    idempotency_key = Column(String, nullable=False, unique=True)

    # ---------- M4：不可变输入、关联 ID 与租约 ----------
    # 与 checkpoint_json 分工：input_json **不可变**（重试沿用同一输入），
    # checkpoint_json **可变**（执行进度，每次重试都可能被改写）。
    # 混在一起，"这份结果基于什么输入"永远无法回答。
    input_json = Column(Text, nullable=False)
    input_digest = Column(String, nullable=False)  # input_json 的 SHA-256
    correlation_id = Column(String)  # Worker 在另一进程，关联 ID 只能靠落库传递
    lease_owner = Column(String)
    # 【fencing】每次领取都重新生成。⚠️ 只匹配 lease_owner 不是 fencing：
    # worker_id 稳定时（重启复用、进程卡顿后恢复），失去租约的旧执行
    # 仍会通过校验并覆盖新 Worker 的结果。判据必须绑定"这一次领取"。
    lease_token = Column(String)
    lease_expires_at = Column(DateTime)

    job_status = Column(String, nullable=False, default="queued")
    attempt_no = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    next_retry_at = Column(DateTime)  # retry_wait 的下次可执行时间（指数退避）
    checkpoint_json = Column(Text)  # 检查点：从失败位置恢复，而不是从头重做
    last_error_code = Column(String)  # ErrorCode
    last_error_text = Column(Text)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.current_timestamp())

    task = relationship("ApprovalTask", back_populates="jobs")


# ============================================================
# 11. 解析工件（M4）
# ============================================================


class ParseArtifact(Base):
    """解析工件（`parse_artifacts`）—— 标准文档与 OCR 原始结果的**长期存放位置**。

    完整标准文档（页 / 文本块 / 逐字符坐标）动辄数千条，全塞进 SQLite 会把
    "按任务查解析结果"变成全表扫描。因此内容入对象存储，库里只留对象键与摘要 ——
    与 M3 附件同一思路。

    与 `contract_parses` 的字段 JSON **不重复存储同一信息**：
    工件里不重复字段结论，库里不重复坐标。两边的关系只靠 `parse_id`。
    """

    __tablename__ = "parse_artifacts"
    __table_args__ = (
        # 同一解析记录下的同类工件，按版本区分（重解析 / 换管线各留一份）
        UniqueConstraint("parse_id", "kind", "artifact_version"),
        *_checks("parse_artifacts"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    parse_id = Column(
        Integer,
        ForeignKey("contract_parses.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind = Column(String, nullable=False)  # standard_document / ocr_pages
    object_key = Column(String, nullable=False)
    sha256 = Column(String, nullable=False)  # 取回后必须能核验，否则"证据可核验"是空话
    size_bytes = Column(Integer, nullable=False)
    content_type = Column(String, nullable=False, default="application/json")
    artifact_version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, server_default=func.current_timestamp())

    parse = relationship("ContractParse", back_populates="artifacts")


# ============================================================
# 12. Outbox 事件（M6：回写的事务性意图）
# ============================================================


class OutboxEvent(Base):
    """Outbox 事件（`outbox_events`）—— 回写的**事务性意图**。

    外部调用不能与本地事务原子提交：先调外部、崩溃在落库前 → 回写丢失；
    先落库"回写成功"、崩溃在调用前 → 谎报成功。本表让业务事务只写**意图**，
    送达由独立的派发器保证（至少一次 + 幂等键 = 恰好一次）。

    ⚠️ 本表是回写意图的唯一真相源：Redis 只做加速，不承担业务真相。
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("idx_outbox_claim", "event_status", "next_retry_at"),
        Index("idx_outbox_aggregate", "aggregate_type", "aggregate_id"),
        Index("idx_outbox_lease", "lease_expires_at"),
        *_checks("outbox_events"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 聚合定位：事件属于哪个业务对象（如 comment_log）。多态引用，无单表外键
    aggregate_type = Column(String, nullable=False)
    aggregate_id = Column(Integer, nullable=False)
    event_type = Column(String, nullable=False)  # OutboxEventType
    payload_json = Column(Text, nullable=False)  # 派发时要还原的输入，不可变
    idempotency_key = Column(String, nullable=False, unique=True)
    # 领取不改状态（仍是 pending），靠租约字段互斥：
    # 避免"dispatching 但进程已死"这种需要对账的中间态
    event_status = Column(String, nullable=False, default="pending")
    attempt_no = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=5)
    next_retry_at = Column(DateTime)
    lease_owner = Column(String)
    lease_expires_at = Column(DateTime)
    last_error_code = Column(String)
    last_error_text = Column(Text)
    correlation_id = Column(String)
    created_at = Column(DateTime, server_default=func.current_timestamp())
    delivered_at = Column(DateTime)


# ============================================================
# 13. 审计事件（M6：只追加，不可变）
# ============================================================


class AuditEvent(Base):
    """审计事件（`audit_events`）—— 谁在什么时候对什么做了关键动作。

    ⚠️ **只追加**：无 update / delete 服务接口。审计的价值在事后不可改；
    任何"修正审计"的需求都应通过新事件表达，而不是改写历史。
    `detail_json` 只放标识与摘要（result_id / content_digest 等），不放正文。

    ⚠️ **`task_id` 可为空**（M7）：需求 §12 要求把"规则修改"也记为不可变审计事件，
    而改一条规则影响的是**所有任务**，它不属于任何一条任务。为它随便挑一个
    `task_id` 会让审计账出现一条"看起来在说任务 7"的规则变更记录 ——
    排障的人会去查任务 7，而真正变的是全局配置。
    `target_type` / `target_id` 本就是多态目标，此处只是把同一件事做完整。
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("idx_audit_task", "task_id", "created_at"),
        Index("idx_audit_action", "action"),
        *_checks("audit_events"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer, ForeignKey("approval_tasks.id", ondelete="CASCADE"), nullable=True
    )
    actor_id = Column(String)
    actor_name = Column(String, nullable=False)  # 系统动作用 'system'
    action = Column(String, nullable=False)  # AuditAction
    target_type = Column(String, nullable=False)
    target_id = Column(Integer, nullable=False)
    correlation_id = Column(String)
    detail_json = Column(Text)
    created_at = Column(DateTime, server_default=func.current_timestamp())
