-- ============================================================
-- 合同审批审查系统 —— 数据库建表脚本
-- 数据库：SQLite
-- 版本：v2（数据模型与规则语义修订，2026-09-12）
--
-- 设计依据：docs/superpowers/specs/2026-09-12-data-model-rule-semantics-design.md
--
-- 四条核心原则：
--   1. 三类信息不可混用：业务事实 / 解析证据 / 规则评价；
--   2. 甲乙方标签（contract_label）≠ 业务角色（business_role）；
--   3. 所有规则先判断适用性（applies_when_json），"不适用"不等于"缺失"；
--   4. 不能判断不是未命中（四态：hit / not_hit / not_applicable / needs_review）。
--
-- ⚠️ 外键说明：
--   `PRAGMA foreign_keys` 是**连接级**设置，不是数据库级。
--   下面这行只对"执行本脚本的连接"生效；应用运行时由 app/db.py 在
--   每个新建连接上通过 connect 事件显式开启，否则 ON DELETE CASCADE 不会触发。
-- ============================================================

PRAGMA foreign_keys = ON;


-- ============================================================
-- 1. 审批任务主表（业务事实的载体）
-- ============================================================
CREATE TABLE IF NOT EXISTS approval_tasks (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,

    -- ---------- 来源与去重键（企业化设计 §10.2 / §10.3）----------
    provider                  TEXT NOT NULL DEFAULT 'mock',
    -- 轻量租户：v1 固定默认租户，但字段先留，避免接入第二个企业时全库迁移
    tenant_id                 TEXT NOT NULL DEFAULT 'default',
    -- 去重键组成。⚠️ 不给 DEFAULT：默认空串会让下面的唯一约束形同虚设
    instance_id               TEXT NOT NULL,

    -- 业务展示与查询字段。
    -- 需求 2.4.4 要求"按唯一业务标识去重"，但**并未要求 approval_code
    -- 跨企业、跨审批平台全局唯一**。因此这里只建普通索引，
    -- 唯一性完全由 (provider, tenant_id, instance_id) 承担——
    -- 若保留全局唯一，第二个企业接入时必然要迁移，与轻量租户目标直接冲突。
    approval_code             TEXT NOT NULL,
    approval_title            TEXT,
    applicant_name            TEXT,
    apply_time                TEXT,
    -- 审批表单原样留存：外部审批系统不可用或实例被删时，历史详情仍可查看。
    -- ⚠️ 其中的人员姓名、证件号、联系方式等**禁止进入日志**（见日志模块脱敏规则）
    form_data_json            TEXT,

    -- 任务状态：pending / parsing / reviewing / blocked / done
    task_status               TEXT NOT NULL DEFAULT 'pending',
    -- 回写状态：not_written / writing / success / failed
    -- 「为什么没写成功」不在这里表达，见 comment_logs.reason_code
    write_status              TEXT NOT NULL DEFAULT 'not_written',

    -- ---------- 权威审查上下文（业务事实，仅拉取模块或人工可写）----------
    -- 解析模块只能读取这些字段，绝不能写入。
    our_party_name            TEXT,
    -- 我方在合同正文中的形式标签：party_a / party_b / other / unknown
    our_party_contract_label  TEXT,
    -- 我方在交易中的实际身份：buyer / seller / customer /
    --   service_provider / licensor / licensee / other / unknown
    our_party_business_role   TEXT,
    -- 合同业务分类：procurement / sales / software_service /
    --   development / outsourcing / lease / other / unknown
    contract_type             TEXT,
    -- 上下文来源：approval_system / manual
    context_source            TEXT NOT NULL DEFAULT 'approval_system',
    -- 上下文状态：complete / missing / conflict / confirmed
    --   ⚠️ confirmed 表示"审查立场已人工确认"，
    --      与 review_results.manual_confirmed（结果与正文确认）互相独立
    context_status            TEXT NOT NULL DEFAULT 'missing',
    -- 【conflict 的两个来源对照】（M8 补，JSON）
    --   形如 {"declared": {…审批系统声明的四项…}, "confirmed": {…人工背书的四项…}}
    --   仅当 context_status='conflict' 时非空，其余情况一律 NULL（清除）。
    --
    --   为什么必须存下来：`conflict` 说的是"两个来源不一致，需人工裁定"，
    --   而"不一致"必须能指出**是哪两个值**——只说"冲突了"时，
    --   人的下一步只能是猜（或去翻审计）。四个源字段仍保存**人工背书**的那一组，
    --   因为裁定结果以人的判断为准。
    context_conflict_json     TEXT,

    -- ---------- 阻塞与重试（需求 2.4.4）----------
    block_reason              TEXT,
    -- 失败**位置**，供人工重试从检查点恢复（§7.4）：
    --   pull / detail / download / parse / rule / result / writeback
    blocked_stage             TEXT,
    -- 稳定错误码（ErrorCode）。与 block_reason 分工：
    --   last_error_code = 机器判据（统计、看板、自动化断言）
    --   block_reason    = 给人看的中文说明
    last_error_code           TEXT,
    retry_count               INTEGER NOT NULL DEFAULT 0,

    created_at                DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at                DATETIME DEFAULT CURRENT_TIMESTAMP,

    -- 【去重唯一键】对象级闸门：同一审批单只能有一条任务记录。
    -- 与 workflow_jobs.idempotency_key（操作级闸门）职责不同，不可互相替代：
    -- 前者防"同一对象重复建记录"，后者防"同一输入版本的操作重复入队"。
    UNIQUE (provider, tenant_id, instance_id),

    -- ---------- 状态与取值域约束（**数据库层**，不只是 Python 枚举）----------
    -- ⚠️ 只在 Python 侧校验枚举是不够的：
    --    脚本、手工 SQL、未来的其他服务都能绕过 ORM 写入非法值，而数据库毫无察觉。
    --    状态字段一旦被写坏，"为什么这条任务卡住不动了"会变得极难排查。
    --    注意：删除 Python 枚举里的 rejected 时，数据库若不设约束，
    --    旧值仍可被写入 —— 这就是"枚举删了、库还接受"的漏洞。
    CHECK (length(trim(provider)) > 0),
    CHECK (length(trim(tenant_id)) > 0),
    -- 去重键组成，空串会让 UNIQUE 约束形同虚设
    CHECK (length(trim(instance_id)) > 0),
    CHECK (length(trim(approval_code)) > 0),
    CHECK (task_status IN ('pending', 'parsing', 'reviewing', 'blocked', 'done')),
    CHECK (write_status IN ('not_written', 'writing', 'success', 'failed')),
    CHECK (context_source IN ('approval_system', 'manual')),
    CHECK (context_status IN ('complete', 'missing', 'conflict', 'confirmed')),
    CHECK (our_party_contract_label IS NULL
           OR our_party_contract_label IN ('party_a', 'party_b', 'other', 'unknown')),
    CHECK (our_party_business_role IS NULL
           OR our_party_business_role IN ('buyer', 'seller', 'customer',
                                          'service_provider', 'licensor', 'licensee',
                                          'other', 'unknown')),
    CHECK (contract_type IS NULL
           OR contract_type IN ('procurement', 'sales', 'software_service',
                                'development', 'outsourcing', 'lease', 'other', 'unknown')),
    -- 计数不得为负：负数会让"重试了几次"这类判断彻底失去意义
    CHECK (retry_count >= 0),
    CHECK (blocked_stage IS NULL
           OR blocked_stage IN ('pull', 'detail', 'download',
                                'parse', 'rule', 'result', 'writeback'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON approval_tasks(task_status);
CREATE INDEX IF NOT EXISTS idx_tasks_context ON approval_tasks(context_status);
-- approval_code 只做查询与展示，**不再承担唯一性**（理由见上方建表注释）
CREATE INDEX IF NOT EXISTS idx_tasks_approval_code ON approval_tasks(approval_code);


-- ============================================================
-- 2. 合同附件
-- ============================================================
CREATE TABLE IF NOT EXISTS approval_attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         INTEGER NOT NULL REFERENCES approval_tasks(id) ON DELETE CASCADE,
    attachment_id   TEXT NOT NULL,                 -- 审批系统侧的附件编号
    file_name       TEXT NOT NULL,
    file_type       TEXT,                          -- pdf / png / jpg
    file_path       TEXT,                          -- 相对 storage_root
    file_size       INTEGER,
    file_checksum   TEXT,                          -- SHA-256
    -- 长期保存位置（对象存储键，内容寻址）。与 file_path 是两个不同性质的位置：
    --   file_path  = 受控临时物化路径，只供后续解析工具使用
    --   object_key = 长期保存位置；调用端不得获得可绕过鉴权的永久公开地址
    object_key      TEXT,
    -- 从响应头落地，供 M4 的解析路由判断该走文本抽取还是 OCR
    content_type    TEXT,
    download_status TEXT NOT NULL DEFAULT 'pending',
    error_message   TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (task_id, attachment_id),
    CHECK (length(trim(attachment_id)) > 0),
    CHECK (length(trim(file_name)) > 0),
    CHECK (download_status IN ('pending', 'success', 'failed')),
    CHECK (file_size IS NULL OR file_size >= 0)
);
CREATE INDEX IF NOT EXISTS idx_attach_task ON approval_attachments(task_id);


-- ============================================================
-- 3. 合同解析结果（解析证据）
-- ============================================================
-- basic_info_json / clause_info_json 内每个字段的结构：
--   {"value": "...", "snippet": "...", "page": 1,
--    "bbox": [x0,y0,x1,y1], "char_start": 0, "char_end": 10,
--    "precision": "char|line|none",
--    "status": "extracted|not_found|uncertain|failed"}
--
--   status 语义（关键）：
--     extracted 成功提取且有证据位置
--     not_found 完成规定范围检索后确实未找到 → 才可能判定"条款缺失"
--     uncertain 存在疑似内容但证据不足       → must needs_review
--     failed    该字段提取过程失败           → must needs_review，不能报缺失
--
-- 保留键（下划线前缀 = 元数据，非合同字段）：
--   _party_consistency、_contract_type_consistency
--   保存声明值 / 解析候选值 / 是否冲突 / 核验原因；仅用于核验，
--   不得反向填充 approval_tasks。
CREATE TABLE IF NOT EXISTS contract_parses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          INTEGER NOT NULL REFERENCES approval_tasks(id) ON DELETE CASCADE,
    -- 解析记录必然针对某个附件，因此 NOT NULL。
    -- 若允许为空，UNIQUE(attachment_id, parse_version) 会因 SQLite 视 NULL 互不相同而失效。
    attachment_id    INTEGER NOT NULL REFERENCES approval_attachments(id) ON DELETE CASCADE,
    basic_info_json  TEXT,
    clause_info_json TEXT,
    -- pending / parsing / succeeded / failed / blocked
    parse_status     TEXT NOT NULL DEFAULT 'pending',
    -- 失败原因：parse_error 给人看（中文说明），parse_error_code 给机器用（稳定判据）。
    -- ⚠️ 两者不可合并：自由文本无法统计、无法告警、无法做自动化断言，
    --    而"第 3 次解析因为 PDF_ENCRYPTED 失败、第 1 次是 DOCUMENT_EMPTY"
    --    这类历史问题只有解析记录自己能回答（作业表的 last_error_code 描述的是当下状态）。
    parse_error      TEXT,
    parse_error_code TEXT,

    -- ---------- M4 解析版本追溯与缓存（设计文档 §3.2）----------
    -- 「由哪个解析器版本产生」与 parse_version（「同一附件的第几次解析」）是**两件事**。
    -- 混用会产生一个具体错误：解析器升级后旧记录仍显示"版本 2"，缓存判定命中同一条，
    -- 于是**升级后的解析器永远不会被真正执行**。
    parser_name      TEXT,                          -- 解析器标识，如 "pymupdf"
    parser_version   TEXT,                          -- 解析器版本，如 "1.25.1+pipe-v1"
    config_digest    TEXT,                          -- 参与解析的配置摘要（DPI、阈值、白名单、规范化版本…）
    cache_key        TEXT,                          -- 上述三者与 source_checksum 的合成键

    -- ---------- 解析版本与质量 ----------
    source_checksum  TEXT,                          -- 被解析附件的 SHA-256
    parse_version    INTEGER NOT NULL DEFAULT 1,    -- 同一附件的解析版本，从 1 开始
    text_coverage    REAL,                          -- 可靠读取的页面比例 0–1
    ocr_confidence   REAL,                          -- OCR 总体置信度，非 OCR 文档为空
    ocr_pages        INTEGER DEFAULT 0,

    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,

    -- 取值域约束：解析状态、版本与质量指标必须落在合法范围。
    -- text_coverage / ocr_confidence 是 0–1 的比例，越界会让 needs_review 判定失真。
    CHECK (parse_status IN ('pending', 'parsing', 'succeeded', 'failed', 'blocked')),
    CHECK (parse_version >= 1),
    CHECK (text_coverage IS NULL OR (text_coverage >= 0 AND text_coverage <= 1)),
    CHECK (ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)),
    CHECK (ocr_pages IS NULL OR ocr_pages >= 0),

    -- 同一附件的同一解析版本唯一。
    -- ⚠️ 这里**不能**用 source_checksum 作唯一键：两个不同审批单完全可能上传同一份
    --    模板合同，内容相同 → 校验和相同 → 会与"解析版本"发生错误冲突，
    --    导致第二个任务根本无法记录解析结果。
    --    source_checksum 只做普通索引，用于"内容是否变化"的快速判断。
    UNIQUE (attachment_id, parse_version),
    -- 供子表 review_runs 用复合外键引用，保证"解析记录必须属于同一任务"
    UNIQUE (id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_parse_task ON contract_parses(task_id);
CREATE INDEX IF NOT EXISTS idx_parse_checksum ON contract_parses(source_checksum);
CREATE INDEX IF NOT EXISTS idx_parse_attachment ON contract_parses(attachment_id);

-- 【缓存占位闸门】必须是**部分**唯一索引，不能是全局唯一（设计文档 §3.2 修-31）。
--
-- 全局唯一会让两条要求**互相矛盾**：
--   「同一文件并发解析只跑一次 OCR」——需要唯一约束拦住第二个占位；
--   「失败后可以重新解析」——需要允许再插入一条同键记录。
-- 加上 WHERE 之后两者才自洽：
--   进行中/已成功的记录占位（并发只允许一个）；
--   failed / blocked 的记录不占位，历史留痕且允许重来。
--
-- ⚠️ 写成全局唯一的表现是：修复一次失败解析时抛 IntegrityError —
--    运行期才暴露，且错误信息只谈约束冲突，与"失败后要能重试"这个意图看不出关联。
CREATE UNIQUE INDEX IF NOT EXISTS uq_parse_cache_key
    ON contract_parses(attachment_id, cache_key)
    WHERE parse_status IN ('pending', 'parsing', 'succeeded');


-- ============================================================
-- 4. 审查规则
-- ============================================================
-- match_mode 取值：keyword | regex | llm | expr
--
-- 三类条件必须分离：
--   applies_when_json   适用条件（NULL = 全局适用）—— 先判"该不该判"
--   match_text          风险命中条件（沿用需求文档字段名）
--   fallback_match_json LLM 不可用时的显式降级条件（无法降级则为空）
--
-- applies_when_json 受控结构（不设计任意表达式语言）：
--   {"contract_types": [...], "our_contract_labels": [...],
--    "our_business_roles": [...], "requires_any_keyword": [...]}
-- 未知键 / 非法枚举 / 缺少必要参数 → 加载即失败，不允许带病运行。
CREATE TABLE IF NOT EXISTS review_rules (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_code           TEXT NOT NULL UNIQUE,
    rule_name           TEXT NOT NULL,
    rule_category       TEXT,                       -- 11 类之一
    risk_level          TEXT NOT NULL DEFAULT 'medium',
    rule_status         TEXT NOT NULL DEFAULT 'active',   -- active / inactive
    priority            INTEGER NOT NULL DEFAULT 100,      -- 数值小者先执行
    rule_version        INTEGER NOT NULL DEFAULT 1,
    match_mode          TEXT NOT NULL,
    applies_when_json   TEXT,                       -- NULL = 全局适用
    match_text          TEXT NOT NULL,
    fallback_match_json TEXT,
    exclude_text        TEXT,                       -- 否定词表，仅 keyword 生效
    suggestion_text     TEXT,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP,

    CHECK (length(trim(rule_code)) > 0),
    CHECK (risk_level IN ('low', 'medium', 'high')),
    CHECK (rule_status IN ('active', 'inactive')),
    CHECK (match_mode IN ('keyword', 'regex', 'llm', 'expr')),
    CHECK (priority >= 0),
    CHECK (rule_version >= 1)
);
CREATE INDEX IF NOT EXISTS idx_rules_status ON review_rules(rule_status, priority);
CREATE INDEX IF NOT EXISTS idx_rules_category ON review_rules(rule_category);


-- ============================================================
-- 5. 审查批次（review_runs）—— 一次完整审查的输入快照
-- ============================================================
-- 同一任务可有多批次（重新解析、修正立场后重跑）。
-- 任何规则评价与审查结果都必须隶属一个明确批次，
-- 否则无法回答"这条结论是用哪份解析、哪组业务事实、哪个规则版本得出的"。
CREATE TABLE IF NOT EXISTS review_runs (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id               INTEGER NOT NULL,
    parse_id              INTEGER NOT NULL,
    version_no            INTEGER NOT NULL,         -- 任务内批次序号，从 1 开始
    context_snapshot_json TEXT,                     -- 本次使用的权威审查上下文快照
    ruleset_version       TEXT,                     -- 规则集版本或摘要哈希

    -- 【M5 / P2 审计】当时的**规则集内容**（规范化 JSON，按 rule_code 排序）。
    -- ⚠️ 只存哈希是不够的：规则记录可以被**原地修改**，之后仅凭哈希与"当前规则表"
    --    还原不出当时的配置 —— 于是"这条结论是按哪版规则判的"变成一个无法回答的问题，
    --    而它在审计场景里恰恰是第一个会被问到的。
    --    哈希用于**比较**（幂等判据），快照用于**还原**（审计），两者都要，不是重复。
    -- ⚠️ 版本号由本列内容**派生**（`app/services/rule_service.ruleset_version_of`），
    --    因此"版本没变、内容却变了"不可能发生。
    ruleset_snapshot_json TEXT,

    -- 【M5】批次绑定的六项输入里，除 parse_id / context_snapshot_json / ruleset_version 外的三项。
    -- ⚠️ 做成**列**而不是塞进 context_snapshot_json，有两个理由：
    --    ① **单一语义**：那个字段回答"当时的业务事实是什么"，这三项回答
    --       "当时的模型/提示词/配置是什么"。混在一个 JSON 里，读取方必须知道
    --       该去找哪几个键 —— 而**漏找一个键不会报错**，只会读到 None。
    --    ② **可聚合**：M11 的模型质量统计要按 model_version 分组，
    --       塞进 JSON 就只能扫全表解 JSON —— 信息在库里但用不了。
    -- ⚠️ 取值是**配置快照**，不是运行结果：单次调用失败记在**该规则**的 reason_code 上，
    --    不得改这三列 —— 否则同一份配置重跑两次会得出**不同的幂等键**，幂等直接失效。
    model_version         TEXT,                     -- 接入的模型标识（无模型时如 'none:fallback'）
    prompt_version        TEXT,                     -- 提示词版本
    config_version        TEXT,                     -- 引擎配置版本（阈值/开关等）

    run_status            TEXT NOT NULL DEFAULT 'running',  -- running / completed / failed
    started_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
    finished_at           DATETIME,

    CHECK (run_status IN ('running', 'completed', 'failed')),
    CHECK (version_no >= 1),

    UNIQUE (task_id, version_no),
    -- 供子表（rule_hits / review_results）用复合外键引用
    UNIQUE (id, task_id),

    -- 【复合外键】本次批次使用的解析记录必须属于**同一任务**。
    -- 若只声明两个独立的单列外键，数据库会允许保存"任务 A + 任务 B 的解析结果"，
    -- 也就是跨任务拼接审查数据，而任何单列约束都发现不了。
    -- 注意：task_id 因此不再单独指向 approval_tasks；删除任务的级联路径为
    -- approval_tasks → contract_parses → review_runs，依然完整。
    FOREIGN KEY (parse_id, task_id)
        REFERENCES contract_parses (id, task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON review_runs(task_id, version_no);


-- ============================================================
-- 6. 规则评价（物理表名沿用需求文档的 rule_hits）
-- ============================================================
-- ⚠️ 物理表名与物理列名均沿用需求文档 2.4.9（rule_hits / hit_status），
--    Python 属性名使用 RuleEvaluation / evaluation_status 以准确表达语义。
--    本表语义已扩展为"规则评价记录"：每个批次的每条启用规则
--    **恰好产生一条记录**，包括不适用规则——这样才能回答"为什么这条规则没报警"。
--
-- hit_status 四态：hit / not_hit / not_applicable / needs_review
CREATE TABLE IF NOT EXISTS rule_hits (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER NOT NULL,
    -- task_id 是需求文档 2.4.9 规定的字段，因此保留；
    -- 但它与 run_id 一起参与复合外键，保证两者必然属于同一任务
    task_id           INTEGER NOT NULL,
    rule_id           INTEGER NOT NULL REFERENCES review_rules(id),
    rule_version      INTEGER NOT NULL DEFAULT 1,   -- 本次执行的规则版本快照
    risk_level        TEXT NOT NULL,
    hit_status        TEXT NOT NULL DEFAULT 'hit',  -- 四态，Python 侧名为 evaluation_status
    reason_code       TEXT,                          -- 稳定机器原因码
    reason_text       TEXT,                          -- 给审批人的中文解释

    -- 主要证据（兼容需求文档规定的字段名）：保存第一处、也是最重要的一处
    evidence_text     TEXT,
    evidence_position TEXT,     -- JSON: {page,bbox,char_start,char_end,precision}
    -- 该规则的全部证据数组（一处规则可能命中多个位置）
    evidence_json     TEXT,     -- JSON: [{text, position}, ...]
    -- expr 模式的计算过程：{actual, op, threshold}
    hit_detail_json   TEXT,

    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,

    -- 四态是规则评价的核心语义，绝不允许出现第五个值
    CHECK (hit_status IN ('hit', 'not_hit', 'not_applicable', 'needs_review')),
    CHECK (risk_level IN ('low', 'medium', 'high')),
    CHECK (rule_version >= 1),

    -- 同一批次内每条规则恰好一条评价
    UNIQUE (run_id, rule_id),
    -- 【复合外键】评价必须属于该任务自己的批次，禁止跨任务拼接
    FOREIGN KEY (run_id, task_id)
        REFERENCES review_runs (id, task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_hits_run ON rule_hits(run_id);
CREATE INDEX IF NOT EXISTS idx_hits_task ON rule_hits(task_id);
CREATE INDEX IF NOT EXISTS idx_hits_status ON rule_hits(hit_status);
CREATE INDEX IF NOT EXISTS idx_hits_rule ON rule_hits(rule_id);


-- ============================================================
-- 7. 审查结果
-- ============================================================
CREATE TABLE IF NOT EXISTS review_results (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            INTEGER NOT NULL,
    run_id             INTEGER NOT NULL,
    overall_risk_level TEXT NOT NULL,
    summary_text       TEXT,
    focus_points_json  TEXT,
    comment_text       TEXT,

    -- 结论完整性：complete / needs_review（与单条规则四态是两个层级）
    review_status      TEXT NOT NULL DEFAULT 'complete',
    hit_count              INTEGER NOT NULL DEFAULT 0,
    needs_review_count     INTEGER NOT NULL DEFAULT 0,
    not_applicable_count   INTEGER NOT NULL DEFAULT 0,

    -- ---------- M6 版本化：同一任务的结果版本链 ----------
    -- 同一任务重跑 / 修正后产生新版本；"当前版本" = MAX(version_no)。
    version_no         INTEGER NOT NULL DEFAULT 1,
    -- 结果指纹：规范化(摘要, 关注点, 回写正文, 聚合口径, 规则输入) 的 SHA-256。
    -- ⚠️ 与 content_digest 分工：后者只是**回写正文**的 SHA-256（人工确认绑定的对象），
    --    前者回答"这份结果的内容与输入是否与另一份完全一致"（重放 / 复用判据）。
    --    合并会让"正文没变但规则输入变了"的新版本被误判为可复用旧版本。
    result_fingerprint TEXT NOT NULL,
    -- 本任务上一版结果的 id（v1 为 NULL）。
    -- 复合外键保证接替关系**不跨任务**：与 review_runs 等表的拼接防护同一理由。
    supersedes_result_id INTEGER,
    created_by         TEXT,                          -- 保存者（操作人留痕）
    updated_at         DATETIME,                      -- 版本更新时间（onupdate 由 ORM 侧维护）

    -- ---------- 人工确认与内容绑定 ----------
    -- ⚠️ 这是"结果与回写正文"的确认，
    --    与 approval_tasks.context_status='confirmed'（立场确认）互相独立
    manual_confirmed   INTEGER NOT NULL DEFAULT 0,
    confirmed_by       TEXT,
    -- 回写正文的 SHA-256；正文一旦变更，旧确认立即失效
    content_digest     TEXT,
    -- 人工确认时对应的内容摘要：必须与 content_digest 相等，确认才有效
    confirmed_digest   TEXT,
    confirmed_at       DATETIME,

    created_at         DATETIME DEFAULT CURRENT_TIMESTAMP,

    CHECK (overall_risk_level IN ('low', 'medium', 'high')),
    CHECK (review_status IN ('complete', 'needs_review')),
    CHECK (manual_confirmed IN (0, 1)),
    CHECK (hit_count >= 0),
    CHECK (needs_review_count >= 0),
    CHECK (not_applicable_count >= 0),
    -- 确认摘要有存在的前提：没有正文摘要就无法"确认某一份正文"
    CHECK (confirmed_digest IS NULL OR content_digest IS NOT NULL),
    -- M6：版本号从 1 开始；空指纹会让幂等判据失真
    CHECK (version_no >= 1),
    CHECK (length(trim(result_fingerprint)) > 0),

    -- 供子表 comment_logs 用复合外键引用
    UNIQUE (id, task_id),
    -- M6：同一任务同一版本只有一条（"当前版本"才有答案）；
    --     同一批次同指纹只有一条（指纹相同 = 内容与输入完全一致，重放必须复用）
    UNIQUE (task_id, version_no),
    UNIQUE (run_id, result_fingerprint),
    -- 【复合外键】结果必须属于该任务自己的批次，禁止跨任务拼接
    FOREIGN KEY (run_id, task_id)
        REFERENCES review_runs (id, task_id) ON DELETE CASCADE,
    -- 【复合外键】新版本只能接替**本任务**的旧结果
    FOREIGN KEY (supersedes_result_id, task_id)
        REFERENCES review_results (id, task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_results_task ON review_results(task_id);
CREATE INDEX IF NOT EXISTS idx_results_run ON review_results(run_id);


-- ============================================================
-- 8. 评论回写日志（数据库级幂等）
-- ============================================================
CREATE TABLE IF NOT EXISTS comment_logs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    -- task_id 是需求文档 2.4.9 规定的字段，保留；与 review_id 一起参与复合外键
    task_id             INTEGER NOT NULL,
    -- 关联 review_results.id，再经该结果的 run_id 追溯审查批次
    review_id           INTEGER NOT NULL,
    -- 回写状态：not_written / writing / success / failed
    -- ⚠️ 严格四值，不扩展。门禁拒绝时**没有发起回写**，状态就是 not_written
    write_status        TEXT NOT NULL DEFAULT 'not_written',
    -- 「为什么没写成功」的稳定原因码（成功时为 NULL）
    --   门禁拒绝 → WRITEBACK_POLICY_DENIED 等（配合 write_status = not_written）
    --   外部失败 → APPROVAL_API_ERROR 等    （配合 write_status = failed）
    -- 与 write_status 是两个正交维度，合并会让语义自相矛盾
    reason_code         TEXT,
    -- 原因的中文说明，给人看；程序判据一律用 reason_code
    reason_text         TEXT,
    -- 回写内容哈希
    content_digest      TEXT,
    -- 幂等键：SHA256(规范化(provider, tenant_id, instance_id, result_id, content_digest))
    -- 不含 approval_code（重新拉取后可能变化），含 digest（正文变了就是另一次回写）
    -- "先查后写"在并发下会穿透，唯一约束才是最后防线
    idempotency_key     TEXT NOT NULL UNIQUE,
    -- **外部系统**返回的原始文本；门禁拒绝时没有外部调用，故为空
    write_response_text TEXT,
    attempt_no          INTEGER NOT NULL DEFAULT 1,
    operator_name       TEXT,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,

    -- 回写状态严格四值：数据库层同样拒绝 rejected 之类被删除的历史取值
    CHECK (write_status IN ('not_written', 'writing', 'success', 'failed')),
    CHECK (attempt_no >= 1),
    CHECK (length(trim(idempotency_key)) > 0),

    -- 【复合外键】回写日志必须指向本任务自己的审查结果
    FOREIGN KEY (review_id, task_id)
        REFERENCES review_results (id, task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_comment_task ON comment_logs(task_id, write_status);


-- ============================================================
-- 9. 全链路日志
-- ============================================================
CREATE TABLE IF NOT EXISTS task_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER REFERENCES approval_tasks(id) ON DELETE CASCADE,
    log_level   TEXT NOT NULL DEFAULT 'info',
    log_type    TEXT NOT NULL,
    log_content TEXT,
    -- 稳定错误码（ErrorCode）——**结构化**保存，不塞进 log_content。
    -- 为什么必须独立成列：自由文本无法统计、无法告警、无法做自动化断言。
    -- 否则"本周有多少次 APPROVAL_API_TIMEOUT"会退化成对日志做正则考古，
    -- 而日志正文本身是随时可能被改写、脱敏或截断的。
    error_code  TEXT,
    -- 【关联 ID】（设计文档 §4.7 / 决策④）：请求入口绑定 → LogService **自动**带上。
    -- ⚠️ 必须**落库**，不能只留在进程日志里：Worker 在**另一个进程**，
    --    contextvar 传不过去，只能从库里读回来再注入。
    correlation_id TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,

    CHECK (log_level IN ('debug', 'info', 'warning', 'error')),
    CHECK (length(trim(log_type)) > 0)
);
CREATE INDEX IF NOT EXISTS idx_logs_task ON task_logs(task_id);
-- 排障的第一动作是"按关联 ID 把一次请求的全链路捞出来"，没有索引就是全表扫
CREATE INDEX IF NOT EXISTS idx_logs_correlation ON task_logs(correlation_id);


-- ============================================================
-- 10. 后台作业（企业内部执行状态）
-- ============================================================
-- ⚠️ 本表表达的是 **Worker 执行情况**，与 approval_tasks.task_status
--    （业务审查进度）是**两个层级**，严禁混用：
--      任务可以"进行中"，而其中某个作业"失败"——这不等于任务阻塞。
--
-- 为什么 M3 就先建它（而不是"提前建空表"）：
--   idempotency_key 在 M3 就有当下用途——它是工具 1~3 重放请求的幂等台账；
--   M4 引入 Worker 时只把"同步执行"换成"入队执行"，
--   **表结构与已写入的作业路径都不需要改**。
CREATE TABLE IF NOT EXISTS workflow_jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 可为空：拉取作业不属于任何单个任务
    task_id           INTEGER REFERENCES approval_tasks(id) ON DELETE CASCADE,
    -- pull / detail / download / parse / rule / result / writeback
    job_type          TEXT NOT NULL,

    -- 【操作级闸门】= {job_type}:{业务标识}:{输入版本}
    --
    -- ⚠️ 不得只含 instance_id！
    --    审批表单与附件都可能发生变化，若幂等键只认审批单号，
    --    同一审批单的**第二次同步会被唯一约束永久拒绝**——
    --    这是"把操作级闸门误当对象级闸门"导致的死锁。
    --    对象级（同一审批单只有一条记录）由
    --    approval_tasks 的 UNIQUE(provider, tenant_id, instance_id) 负责。
    --
    -- 版本取不到时退化为含请求指纹的一次性键：宁可多跑一次，也不能把对象卡死。
    idempotency_key   TEXT NOT NULL UNIQUE,

    -- ---------- M4：不可变输入、关联 ID 与租约（设计文档 §3.3）----------
    -- 不可变输入（结构见设计文档 §4.5）。与 checkpoint_json 分工：
    --   input_json      = **不可变**，回答"这份结果基于什么输入"；重试沿用同一输入
    --   checkpoint_json = **可变**，记录执行进度，每次重试都可能被改写
    -- 混在一起，"这份结果基于什么输入"永远无法回答。
    -- ⚠️ 也不得从 idempotency_key 反解析业务参数：键是**摘要**，为去重而设计。
    --    一旦有人依赖从键里读出参数，改键格式就成了破坏性变更，而且不报错。
    input_json        TEXT NOT NULL,
    input_digest      TEXT NOT NULL,                -- input_json 的 SHA-256，用于追溯
    correlation_id    TEXT,                         -- 关联 ID（Worker 在另一进程，必须落库）
    lease_owner       TEXT,                         -- 持有租约的 Worker 标识
    -- 【fencing】每次领取都重新生成的 UUID。
    -- ⚠️ 只匹配 lease_owner **不是** fencing：worker_id 稳定时（重启复用同一标识、
    --    同一进程卡顿后恢复），失去租约的旧执行仍会通过校验、覆盖新 Worker 的结果。
    --    判据必须绑定"这一次领取"，而不是"哪个 Worker"。
    lease_token       TEXT,
    lease_expires_at  DATETIME,                     -- 到期即视为失联，可被回收

    -- queued / running / retry_wait / succeeded / failed
    job_status        TEXT NOT NULL DEFAULT 'queued',
    attempt_no        INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 3,
    -- retry_wait 的下次可执行时间（指数退避）
    next_retry_at     DATETIME,
    -- 检查点：供 §7.4 从失败位置恢复，而不是从头重做
    checkpoint_json   TEXT,
    -- 稳定错误码（ErrorCode），瞬时错误重试耗尽后才转 blocked
    last_error_code   TEXT,
    last_error_text   TEXT,
    started_at        DATETIME,
    finished_at       DATETIME,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,

    -- 取值域约束：作业状态、类型与计数都必须合法。
    -- 尤其 max_attempts >= 1：为 0 意味着任何瞬时错误都无法重试，
    -- 却会让任务**静默地直接 blocked** —— 属于"配置写错却看不出来"的典型。
    CHECK (job_type IN ('pull', 'detail', 'download', 'parse',
                        'rule', 'result', 'writeback')),
    CHECK (job_status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')),
    CHECK (length(trim(idempotency_key)) > 0),
    -- 输入与摘要都不得为空串：空串等同于"没有输入"，会让"这份结果基于什么输入"无解
    CHECK (length(trim(input_json)) > 0),
    CHECK (length(trim(input_digest)) > 0),
    CHECK (attempt_no >= 0),
    CHECK (max_attempts >= 1)
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON workflow_jobs(job_status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_jobs_task ON workflow_jobs(task_id);
-- 租约回收扫描：按状态 + 到期时间找"失联的 running 作业"
CREATE INDEX IF NOT EXISTS idx_jobs_lease ON workflow_jobs(job_status, lease_expires_at);
-- "这次请求触发了哪些作业" —— 与 idx_logs_correlation 配对使用（§4.7 要求两处都建索引）
CREATE INDEX IF NOT EXISTS idx_jobs_correlation ON workflow_jobs(correlation_id);


-- ============================================================
-- 11. 解析工件（M4 新增，设计文档 §3.1）
-- ============================================================
-- 完整标准文档（页 / 文本块 / 逐字符坐标）与 OCR 原始结果体积大，
-- 全部塞进 SQLite 会把"按任务查解析结果"变成全表扫描。
-- 因此数据库只留对象键与摘要，与 M3 附件的做法一致。
--
-- 唯一真相来源（设计文档 §3.4）：
--   字段与条款结论 → contract_parses.basic_info_json / clause_info_json（紧凑 JSON）
--   页面 / 文本块 / 坐标 → 本表 kind='standard_document' 指向的对象
--   两者不重复存储同一信息：文件里不重复字段结论，库里不重复坐标。
CREATE TABLE IF NOT EXISTS parse_artifacts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    parse_id         INTEGER NOT NULL REFERENCES contract_parses(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,                 -- standard_document / ocr_pages
    object_key       TEXT NOT NULL,                 -- 内容寻址对象键
    sha256           TEXT NOT NULL,                 -- 工件内容摘要，用于核验
    size_bytes       INTEGER NOT NULL,
    content_type     TEXT NOT NULL DEFAULT 'application/json',
    -- 同一解析记录可有多个版本的同一类工件（重解析、换管线），由它区分
    artifact_version INTEGER NOT NULL DEFAULT 1,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (parse_id, kind, artifact_version),
    CHECK (kind IN ('standard_document', 'ocr_pages')),
    CHECK (size_bytes >= 0),
    CHECK (artifact_version >= 1)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_parse ON parse_artifacts(parse_id);


-- ============================================================
-- 12. Outbox 事件（M6 新增：回写的事务性意图）
-- ============================================================
-- 回写的两难：外部调用不可与本地事务原子提交。若"先调外部、成功后再落库"，
-- 外部成功而本地崩溃 → 回写丢失且无人知晓；若"先落库再调用"崩溃在中间 →
-- 意图还在但没有结果。Outbox 的解法：**业务事务里**只写"意图"（本表一行），
-- 派发器独立轮询本表完成外部调用并回填结果 —— 意图与业务状态同生共死，
-- 送达由派发器保证至少一次，配合幂等键达到**恰好一次**。
--
-- ⚠️ 本表是回写意图的**唯一真相源**：Redis 只做加速，不得承担业务真相。
CREATE TABLE IF NOT EXISTS outbox_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 聚合定位：事件属于哪个业务对象（如 comment_log）。多态引用，无单表外键
    aggregate_type   TEXT NOT NULL,
    aggregate_id     INTEGER NOT NULL,
    -- 事件类型（OutboxEventType）。派发器只处理已知类型，未知类型必须**拒绝**
    -- 而不是标记送达 —— "跳过"会让事件静默丢失
    event_type       TEXT NOT NULL,
    -- 派发时要还原的输入（如 instance_id / comment 正文引用），不可变
    payload_json     TEXT NOT NULL,
    -- 幂等键：外部效果的唯一性判据，数据库唯一约束兜底
    idempotency_key  TEXT NOT NULL UNIQUE,
    -- pending / delivered / failed。
    -- 领取不改状态（仍是 pending），靠租约字段互斥 —— 避免状态机里出现
    -- "dispatching 但进程已死"这种需要对账的中间态
    event_status     TEXT NOT NULL DEFAULT 'pending',
    attempt_no       INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 5,
    -- 失败退避后的下次可执行时间
    next_retry_at    DATETIME,
    -- 租约：领取者标识与到期时间（到期即视为失联，可被其他派发器接管）
    lease_owner      TEXT,
    lease_expires_at DATETIME,
    last_error_code  TEXT,
    last_error_text  TEXT,
    -- 关联 ID：与 task_logs / workflow_logs 同一语义，跨进程排障的纽带
    correlation_id   TEXT,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    delivered_at     DATETIME,

    CHECK (event_status IN ('pending', 'delivered', 'failed')),
    CHECK (attempt_no >= 0),
    CHECK (max_attempts >= 1),
    CHECK (length(trim(idempotency_key)) > 0),
    CHECK (length(trim(event_type)) > 0),
    CHECK (length(trim(aggregate_type)) > 0),
    -- 空 payload = "要写什么"无据可查
    CHECK (length(trim(payload_json)) > 0),
    -- 送达时间只属于 delivered 状态，双向都不许错位
    CHECK (event_status <> 'delivered' OR delivered_at IS NOT NULL),
    CHECK (event_status = 'delivered' OR delivered_at IS NULL)
);
-- 派发器常驻轮询的领取扫描：没有索引时每次空扫都是全表扫，
-- Outbox 积压越多轮询越慢，最终表现成"回写延迟无故变大"
CREATE INDEX IF NOT EXISTS idx_outbox_claim ON outbox_events(event_status, next_retry_at);
-- "这个业务对象有哪些待发事件"
CREATE INDEX IF NOT EXISTS idx_outbox_aggregate ON outbox_events(aggregate_type, aggregate_id);
-- 租约回收扫描：找"失联的领取"
CREATE INDEX IF NOT EXISTS idx_outbox_lease ON outbox_events(lease_expires_at);


-- ============================================================
-- 13. 审计事件（M6 新增：只追加，不可变）
-- ============================================================
-- 谁在什么时候对什么做了关键动作。⚠️ **无 update / delete 服务接口**：
-- 审计的价值在事后不可改；任何"修正审计"的需求都应通过新事件表达，
-- 而不是改写历史。detail_json 只放标识与摘要，不放正文 / 指针类敏感字段。
CREATE TABLE IF NOT EXISTS audit_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- ⚠️ M7 起**可为空**：需求 §12 要求"规则修改"也进不可变审计账，
    --    而改一条规则影响的是所有任务，它不属于任何一条任务。
    --    为它随便挑一个 task_id 会让审计里出现一条"看起来在说某个任务"的
    --    规则变更记录 —— 排障的人会去查那条任务，而真正变的是全局配置。
    --    （关联到具体任务的审计事件仍应填写它，级联语义不变。）
    task_id         INTEGER REFERENCES approval_tasks(id) ON DELETE CASCADE,
    -- 操作者（轻量身份留痕，非账号体系；系统动作用 'system'）
    actor_id        TEXT,
    actor_name      TEXT NOT NULL,
    -- AuditAction：受控取值，审计账要能按动作聚合统计
    action          TEXT NOT NULL,
    target_type     TEXT NOT NULL,
    target_id       INTEGER NOT NULL,
    correlation_id  TEXT,
    -- 只放标识与摘要（如 result_id / content_digest），不放正文
    detail_json     TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,

    CHECK (action IN ('RESULT_CONFIRMED', 'CONTEXT_CONFIRMED', 'WRITEBACK_REQUESTED',
                      'WRITEBACK_DELIVERED', 'TASK_RETRIED', 'RULE_CREATED', 'RULE_UPDATED')),
    CHECK (length(trim(actor_name)) > 0),
    CHECK (length(trim(target_type)) > 0),
    CHECK (target_id >= 1)
);
CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action);


-- ============================================================
-- 扩展清单（相对需求文档 2.4.9）
-- ============================================================
-- A 类：文档别处明确要求，但 2.4.9 漏列（属"补全"，非扩展）
--   approval_tasks.apply_time                 ← 2.4.3 待办列表要求"申请时间"
--   approval_attachments.attachment_id        ← 2.4.10 download_contract_attachment
--   rule_hits.risk_level                      ← 2.4.6 "命中结果至少包含…风险等级"
--   comment_logs.review_id                    ← 2.4.10 write_approval_comment(review_id)
--
-- B 类：为设计闭环自行扩展（每条均有明确理由）
--   approval_tasks.our_party_name / our_party_contract_label /
--     our_party_business_role / contract_type / context_source / context_status
--                                             ← 权威审查上下文，方向敏感规则的语义基础
--   approval_tasks.block_reason / retry_count  ← 2.4.4 阻塞原因与人工重试
--   approval_attachments.file_size / file_checksum / error_message
--                                             ← 附件校验与失败原因
--   contract_parses.attachment_id / ocr_pages  ← 多附件定位与 OCR 统计
--   contract_parses.source_checksum / parse_version / text_coverage / ocr_confidence
--                                             ← 解析版本与质量，needs_review 的判定依据
--   review_rules.rule_category / priority / exclude_text
--                                             ← 11 类分类、执行顺序、否定词处理
--   review_rules.rule_version / applies_when_json / fallback_match_json
--                                             ← 适用性判断与显式降级
--   review_runs（整表新增，第 9 张表）          ← 审查批次与输入快照，保证结论可追溯可复现
--   rule_hits.run_id / rule_version / reason_code / reason_text / evidence_json
--                                             ← 批次归属、规则版本快照、原因码、多处证据
--   review_results.run_id / review_status / hit_count / needs_review_count /
--     not_applicable_count / content_digest / confirmed_digest / confirmed_at
--                                             ← 批次绑定、结论完整性、确认与正文绑定
--   review_results.manual_confirmed / confirmed_by
--                                             ← harness 高风险人工确认
--   comment_logs.content_digest / idempotency_key / attempt_no / operator_name
--                                             ← 数据库级幂等、重试次数、操作人
--   comment_logs.reason_code / reason_text     ← 「为什么没写成功」的独立表达。
--                                               write_status 只回答"走到哪一步"（严格四值），
--                                               门禁拒绝时根本没有发起回写，状态就是 not_written；
--                                               把拒绝塞进 write_status 会让一个字段同时
--                                               表达"是否允许回写"与"回写是否成功"，语义自相矛盾
--
--   approval_tasks.provider / tenant_id / instance_id
--                                             ← 企业化设计 §10.2 的去重唯一键。
--                                               v1 固定默认租户，但字段先留，避免接入第二个企业时全库迁移
--   approval_tasks.form_data_json             ← 外部审批系统不可用或实例被删时，
--                                               历史审批表单仍可查看（调用端模块 2 要求）。
--                                               ⚠️ 其中敏感字段禁止进入日志
--   approval_tasks.blocked_stage / last_error_code
--                                             ← §7.4 检查点恢复（失败位置）与 §7.3 稳定错误码
--   approval_attachments.object_key           ← §5.2 长期保存位置。
--                                               与 file_path（受控临时物化路径）性质不同，不可合并
--   approval_attachments.content_type         ← M4 解析路由（文本层 / 扫描件）的判断依据
--   workflow_jobs（整表新增，第 10 张表）       ← 后台作业、内部状态与检查点。
--                                               与 task_status 分层，严禁混用
--
--   ---- M4（标准文档、分层解析管线与 Worker）----
--   contract_parses.parse_error_code          ← 「这次解析为什么失败」的稳定判据（M4 §3.2）。
--                                               与 parse_error（中文说明）分工：作业表的
--                                               last_error_code 描述的是当下状态，
--                                               "历次解析分别因为什么失败"只有解析表能回答
--   contract_parses.parser_name / parser_version / config_digest / cache_key
--                                             ← 解析版本追溯与缓存（M4 §3.2）。
--                                               parser_version（由哪个解析器产生）与
--                                               parse_version（同一附件的第几次解析）是两件事，
--                                               混用会让升级后的解析器**永远不被执行**
--   workflow_jobs.input_json / input_digest   ← 不可变输入（M4 §4.5）。
--                                               与 checkpoint_json（可变进度）分离，
--                                               否则"这份结果基于什么输入"永远无法回答
--   workflow_jobs.correlation_id              ← 请求 → 作业 → Worker → 日志 的关联 ID。
--                                               Worker 在另一进程，只能靠落库传递
--   workflow_jobs.lease_owner / lease_token / lease_expires_at
--                                             ← Worker 租约与 fencing（M4 §4.4.2）。
--                                               lease_token 不可省：只匹配 lease_owner 时，
--                                               worker_id 稳定（重启复用、进程卡顿恢复）
--                                               会让旧执行通过校验并覆盖新结果
--   parse_artifacts（整表新增，第 11 张表）     ← 标准文档与 OCR 原始工件入对象存储，
--                                               数据库只留对象键与摘要（体积原因）
--
--   ---- M6（结果版本化、Outbox 回写与审计账）----
--   review_results.version_no / result_fingerprint / supersedes_result_id /
--     created_by / updated_at
--                                             ← 结果版本链：同任务同版本唯一、
--                                               同批次同指纹唯一（重放复用）、
--                                               接替关系不跨任务（复合外键）
--   outbox_events（整表新增，第 12 张表）       ← 回写的事务性意图。
--                                               业务事务里只写意图，派发器独立送达；
--                                               幂等键唯一约束 + 租约 + 退避重试
--   audit_events（整表新增，第 13 张表）        ← 只追加审计账，无 update/delete 接口
--
-- C 类：结构级约束（不新增列，但决定数据能否被写坏）
--   approval_tasks 取消 approval_code 全局唯一，改为 UNIQUE(provider, tenant_id, instance_id)
--                                             ← 需求 2.4.4 只要求"按唯一业务标识去重"，
--                                               并未要求 approval_code 跨企业、跨审批平台全局唯一。
--                                               旧约束比需求更强，且强在了错误的地方：
--                                               保留它会让第二个企业接入时必然迁移，
--                                               与 §10.3 轻量租户目标直接冲突。
--                                               这不是弱化需求，而是把约束对齐到真实去重键
--   workflow_jobs.idempotency_key 必含"输入版本或请求指纹"，不得只含 instance_id
--                                             ← 详情与附件会变化；只认审批单号会让
--                                               同一审批单的第二次同步被永久拒绝
--   contract_parses  UNIQUE(attachment_id, parse_version)
--                                             ← 解析版本按"附件"区分，而不是按文件校验和。
--                                               两个审批单上传同一份模板合同会导致校验和相同，
--                                               用 source_checksum 做唯一键会让第二个任务无法记录解析结果
--   contract_parses  uq_parse_cache_key 必须是**部分**唯一索引（M4 §3.2）
--                                             ← 全局唯一会让两条要求互相矛盾：
--                                               「并发只跑一次 OCR」需要约束拦住第二个占位；
--                                               「失败后可以重新解析」需要允许再插入同键记录。
--                                               加上 WHERE parse_status IN
--                                               ('pending','parsing','succeeded') 才自洽：
--                                               进行中的记录占位，failed/blocked 不占位。
--                                               写成全局唯一的表现是——修复一次失败解析时
--                                               抛 IntegrityError，而错误信息与"失败要能重试"
--                                               这个意图看不出任何关联
--   contract_parses  / review_runs / review_results  各加 UNIQUE(id, task_id)
--                                             ← 作为复合外键的引用目标
--   review_runs / rule_hits / review_results / comment_logs 使用复合外键
--                                             ← 保证"批次→解析""评价→批次""结果→批次""回写→结果"
--                                               两端必属同一任务，使跨任务拼接审查数据在结构上不可能
-- ============================================================
