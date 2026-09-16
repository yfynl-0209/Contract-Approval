# 合同审批审查系统：数据模型与规则语义修改方案

日期：2026-09-12  
状态：**已确认，进入实施**

## 0. 设计决议（2026-09-12 确认）

| # | 决议 | 说明 |
| --- | --- | --- |
| **D1** | **本次范围止于"地基"（方案甲）** | 本次只做：领域枚举、数据库结构、ORM 映射、数据库约束、规则配置 DTO 与加载校验、种子规则重写、结构一致性验证。<br>**规则评价引擎（四状态判定逻辑）归入 M5**，不在本次实现。M2 只依赖权威立场字段与 mock 接口，不依赖规则引擎。 |
| **D2** | `hit_status` 物理列名保留 | 数据库沿用需求文档规定的 `hit_status`；Python 属性命名为 `evaluation_status` 并映射到该列（`Column("hit_status", ...)`），**不新增重复列**；取值域扩展为四态。 |
| **D3** | 两种人工确认互相独立 | `approval_tasks.context_status = confirmed` 表示**审查立场**已人工确认；`review_results.manual_confirmed = 1` 表示**当前审查结果与回写正文**已人工确认。任何一方为真**不得**使另一方放行。 |
| **D4** | 一处规则的多处证据 | 新增 `evidence_json` 数组保存该规则的全部证据；`evidence_text` / `evidence_position` 继续保留为"主要证据"，**兼容需求文档规定的字段**。 |
| **D5** | `comment_logs.review_id` 语义 | 明确关联 `review_results.id`，再经该结果的 `run_id` 追溯审查批次。 |
| **D6** | M2 mock 回写幂等 | mock 评论接口接收并保存 `idempotency_key`；**同键重复调用返回第一次的结果，不重复生成评论**。 |

## 1. 修改目标

现有模型能保存审批任务、解析结果和规则命中，但没有可靠表达以下事实：

1. 系统代表哪一方审查合同；
2. 一条规则是否适用于当前合同；
3. “合同确实缺少条款”和“解析器没有识别出来”的区别；
4. 某次结论使用的是哪份合同、哪组业务事实和哪个规则版本；
5. 人工确认是否仍对应当前回写内容。

本次修改采用“权威业务事实 + 解析证据 + 审查批次 + 四状态规则评价”模型。目标是消除立场反转、缺失误报、新旧结果混用和旧确认复用问题，同时保持课程项目可实现、可演示、可测试。

## 2. 范围与非目标

### 2.1 本次范围

- 修订 `approval_tasks`、`contract_parses`、`review_rules`、`rule_hits`、`review_results`、`comment_logs`。
- 新增 `review_runs`，记录一次审查使用的输入快照。
- 修订 32 条种子规则的适用条件、方向语义和降级行为。
- 增加字段一致性核验、四状态评价、结果聚合和回写门禁的设计。
- 补充数据库约束与测试矩阵。

### 2.2 非目标

- 不建设通用规则编排平台或可视化规则语言。
- 不自动判断最终是否批准合同。
- 不允许大模型覆盖审批系统或人工确认的业务事实。
- 不在本次修改中实现 M2 mock 审批系统及后续业务模块（见 **D1**：本次范围止于"地基"）。
- **不在本次修改中实现规则评价引擎**（四状态判定逻辑属 M5）；本次只定义其数据结构与语义，并保证结构层面不阻碍 M5 实现。
- 不迁移生产数据；当前项目仍处于 M1，允许重建本地 SQLite 数据库。

## 3. 核心设计原则

### 3.1 三类信息不可混用

| 信息类型 | 权威来源 | 用途 | 是否允许解析器修改 |
| --- | --- | --- | --- |
| 业务事实 | 审批系统或人工确认 | 决定审查立场和适用规则 | 否 |
| 解析证据 | 合同文件解析/OCR | 核验业务事实、支持规则判断 | 仅新增证据 |
| 规则评价 | 规则引擎 | 给出四状态结论及原因 | 不适用 |

### 3.2 甲乙方标签不等于业务角色

`party_a`、`party_b` 仅表示合同正文中的形式标签。采购方、销售方、客户、服务提供方等属于业务角色。两者必须分别存储，避免把“甲方”错误等同于“采购方”。

### 3.3 所有规则先判断适用性

“规则库覆盖 11 类风险”不等于“每份合同必须执行全部 11 类”。规则只有在适用条件成立后，才进入风险条件判断。

### 3.4 不能判断不是未命中

信息不足、证据质量不足或模型不可用时必须返回 `needs_review`，不能当成 `not_hit`，也不能直接当成风险命中。

## 4. 数据模型修改

### 4.1 `approval_tasks`：权威审查上下文

新增字段：

| 字段 | 类型 | 允许空 | 含义 |
| --- | --- | --- | --- |
| `our_party_name` | TEXT | 是 | 我方企业全称 |
| `our_party_contract_label` | TEXT | 是 | `party_a / party_b / other / unknown` |
| `our_party_business_role` | TEXT | 是 | `buyer / seller / customer / service_provider / licensor / licensee / other / unknown` |
| `contract_type` | TEXT | 是 | `procurement / sales / software_service / development / outsourcing / lease / other / unknown` |
| `context_source` | TEXT | 否 | `approval_system / manual` |
| `context_status` | TEXT | 否 | `complete / missing / conflict / confirmed` |

约束：

- 拉取模块可以写入这些字段；解析模块只能读取。
- 人工修改时，将 `context_source` 设为 `manual`，`context_status` 设为 `confirmed`，并记录操作日志。
- 字段缺失或与解析证据冲突时，任务仍可完成解析，但立场相关规则返回 `needs_review`。

### 4.2 `contract_parses`：解析版本与质量

新增字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `source_checksum` | TEXT | 被解析附件的 SHA-256 |
| `parse_version` | INTEGER | 同一附件的解析版本，从 1 开始 |
| `text_coverage` | REAL | 可可靠读取的页面比例，范围 0–1 |
| `ocr_confidence` | REAL | OCR 总体置信度，非 OCR 文档可为空 |

`basic_info_json` 中的字段状态统一为：

- `extracted`：成功提取并有证据位置；
- `not_found`：完成规定范围检索后未找到；
- `uncertain`：存在疑似内容，但证据不足；
- `failed`：该字段提取过程失败。

保留键 `_party_consistency` 和 `_contract_type_consistency`，保存声明值、解析候选值、是否冲突和核验原因。它们只用于核验，不反向填充 `approval_tasks`。

约束：同一附件的 `source_checksum + parse_version` 唯一。

### 4.3 `review_rules`：适用条件、命中条件与降级条件分离

新增或调整字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `rule_version` | INTEGER | 规则版本，从 1 开始 |
| `applies_when_json` | TEXT | 适用条件；`NULL` 表示全局适用 |
| `match_text` | TEXT | 风险命中条件，保留现有字段名以兼容要求 |
| `fallback_match_json` | TEXT | LLM 不可用时的显式降级条件；无法降级则为空 |

`applies_when_json` 采用受控结构，不设计任意表达式语言：

```json
{
  "contract_types": ["software_service", "development"],
  "our_contract_labels": ["party_a", "party_b"],
  "our_business_roles": ["customer"],
  "requires_any_keyword": ["软件", "成果", "源代码"]
}
```

所有 JSON 配置必须在加载规则时由 Pydantic 模型校验。未知键、非法枚举或缺少必要参数应使规则加载失败并写入系统日志，不能带病运行。

方向敏感规则应拆成独立规则，而不是在一条规则里动态翻转结论。例如：

- `PAY_PREPAY_HIGH_FOR_BUYER`：我方为采购方且预付款比例超过 30% 时命中；
- 销售方不执行该规则，而不是把相同条件解释成相反风险；
- 违约责任、管辖地、知识产权归属等规则采用同样方式拆分。

### 4.4 `review_runs`：一次完整审查的输入快照

新增表：

| 字段 | 类型 | 约束/含义 |
| --- | --- | --- |
| `id` | INTEGER | 主键 |
| `task_id` | INTEGER | 外键，关联审批任务 |
| `parse_id` | INTEGER | 外键，关联本次使用的解析记录 |
| `context_snapshot_json` | TEXT | 本次使用的权威审查上下文快照 |
| `ruleset_version` | TEXT | 本次使用的规则集版本或摘要哈希 |
| `run_status` | TEXT | `running / completed / failed` |
| `started_at` | DATETIME | 开始时间 |
| `finished_at` | DATETIME | 结束时间 |

同一个任务可以有多次审查批次，但任何规则评价和审查结果都必须属于一个明确批次。

### 4.5 `rule_hits`：物理表保留，语义改为规则评价

课程要求中指定了 `rule_hits` 表名，因此保留物理表名；Python 模型及业务代码命名为 `RuleEvaluation`，避免把“未命中”和“不适用”称为命中。

新增或调整字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `run_id` | INTEGER | 所属审查批次 |
| `hit_status`（**物理列名，见 D2**） | TEXT | 四态 `hit / not_hit / not_applicable / needs_review`；Python 属性命名为 `evaluation_status` 并映射到本列，不新增重复列 |
| `reason_code` | TEXT | 稳定机器原因码 |
| `reason_text` | TEXT | 给审批人的中文解释 |
| `rule_version` | INTEGER | 本次执行的规则版本快照 |
| `evidence_json` | TEXT | **该规则的全部证据数组**（见 D4）；一处规则可能命中多个位置 |

保留 `evidence_text`、`evidence_position` 作为"主要证据"（兼容需求文档规定的字段名），以及 `hit_detail_json`。建立唯一约束：

```text
UNIQUE(run_id, rule_id)
```

每个审查批次的每条启用规则必须恰好产生一条评价记录，包括不适用规则。这使系统可以回答“为什么这条规则没有报警”。

推荐原因码：

- `APPLICABILITY_NOT_MET`
- `CONTEXT_MISSING`
- `CONTEXT_CONFLICT`
- `EVIDENCE_UNCERTAIN`
- `EXTRACTION_FAILED`
- `MODEL_UNAVAILABLE`
- `CONDITION_MATCHED`
- `CONDITION_NOT_MATCHED`

### 4.6 `review_results`：结果绑定审查批次和内容版本

新增字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `run_id` | INTEGER | 结果所属审查批次 |
| `review_status` | TEXT | `complete / needs_review` |
| `hit_count` | INTEGER | 命中数量 |
| `needs_review_count` | INTEGER | 待人工判断数量 |
| `not_applicable_count` | INTEGER | 不适用数量 |
| `content_digest` | TEXT | 回写正文的 SHA-256 |
| `confirmed_digest` | TEXT | 人工确认时对应的内容摘要 |
| `confirmed_at` | DATETIME | 人工确认时间 |

`manual_confirmed` 和 `confirmed_by` 继续保留。只有以下条件同时成立，确认才有效：

```text
manual_confirmed = 1
confirmed_digest = content_digest
```

保存后再次修改正文、重新解析或重新审查，必须生成新的内容摘要并使旧确认失效。

### 4.7 `comment_logs`：数据库级幂等

`review_id` 明确关联 `review_results.id`，再经该结果的 `run_id` 追溯审查批次（见 **D5**）。

新增 `idempotency_key TEXT NOT NULL UNIQUE`。推荐生成方式：

```text
SHA256(approval_code + review_id + content_digest)
```

调用外部审批系统时同步传递该键。仅依赖“先查询、后写入”的代码逻辑不足以抵抗并发请求，唯一约束必须作为最后防线。

**M2 mock 接口要求（见 D6）**：评论回写接口必须接收并保存 `idempotency_key`；**同键重复调用返回第一次的结果，不重复生成评论**。这样幂等由双方共同保证，而不是只靠审查系统一侧。

## 5. 规则评价语义

### 5.1 固定判定顺序

规则评价模块对外只暴露一个主要接口：

```text
evaluate(rule, review_context, parsed_evidence) -> RuleEvaluation
```

模块内部严格按以下顺序执行：

1. 整体解析失败：任务进入 `blocked`，不创建伪造评价；
2. 适用条件所需上下文缺失或冲突：`needs_review`；
3. 适用条件明确不满足：`not_applicable`；
4. 风险条件所需证据失败或不可靠：`needs_review`；
5. 风险条件成立：`hit`；
6. 风险条件不成立：`not_hit`。

这一接口是规则判断的唯一外部 seam。REST、MCP、页面和测试均调用同一模块，不在调用方重复判断逻辑。

### 5.2 四状态含义

| 状态 | 是否计入风险 | 是否展示 | 是否允许自动回写 |
| --- | --- | --- | --- |
| `hit` | 是 | 是 | 按风险等级和门禁决定 |
| `not_hit` | 否 | 可折叠展示 | 是 |
| `not_applicable` | 否 | 显示数量及原因 | 是 |
| `needs_review` | 否 | 必须突出展示 | 否，需人工确认 |

总风险等级只由 `hit` 聚合；`needs_review` 作为结论完整性单独表达。任务可进入 `done`，但 `review_results.review_status` 必须是 `needs_review`，回写门禁要求人工确认。

### 5.3 “缺失类”规则

缺失类规则不得简单地把字段值为空解释为风险命中：

- 规则不适用：`not_applicable`；
- 文档完整可读、规定范围已检索、仍未发现条款：`hit`；
- OCR 质量不足、字段提取失败或检索覆盖不足：`needs_review`；
- 成功发现有效条款：`not_hit`。

例如 `IP_MISSING` 只对软件、研发、外包等涉及成果权属的合同适用。标准商品采购合同没有知识产权条款时，应为 `not_applicable`，不能报高风险。

### 5.4 LLM 规则降级

- 有模型且输出通过结构校验：使用模型结论，但仍保存原文证据和原因；
- 无模型且存在 `fallback_match_json`：执行确定性降级规则；
- 无模型且不存在可靠降级规则：返回 `needs_review / MODEL_UNAVAILABLE`；
- 模型不得创建合同中不存在的证据，无法定位原文时不能返回 `hit`。

## 6. 结果聚合与回写门禁

结果聚合模块读取一个审查批次的全部规则评价：

- `hit` 参与总风险等级计算；
- `needs_review` 进入“待人工判断”列表；
- `not_applicable` 进入适用范围说明；
- `not_hit` 作为已检查未发现风险的证明。

回写必须同时满足：

1. 任务状态为 `done`；
2. 审查批次状态为 `completed`；
3. 存在审查结果；
4. 当前结果摘要与人工确认摘要一致；
5. 高风险或存在 `needs_review` 时已经人工确认；
6. `idempotency_key` 尚未成功写回；
7. 外部审批系统返回成功。

## 7. 种子规则修改策略

现有 11 类规则全部保留，但逐条标注适用范围：

| 类别 | 主要适用条件 | 是否方向敏感 |
| --- | --- | --- |
| 预付款比例 | 存在付款义务；按我方业务角色拆分 | 是 |
| 付款周期 | 存在应收或应付；按我方角色拆分 | 是 |
| 自动续约 | 持续性合同 | 是 |
| 违约责任 | 全局适用；不对等判断需明确我方 | 是 |
| 管辖地 | 全局适用；不利性判断需明确我方所在地 | 是 |
| 主体信息缺失 | 全局适用 | 否 |
| 金额缺失 | 有明确价款义务的合同 | 否 |
| 保密缺失 | 涉及保密信息或持续合作 | 部分 |
| 数据处理 | 涉及个人信息、业务数据或系统接入 | 部分 |
| 知识产权 | 软件、研发、设计、外包或授权合同 | 是 |
| 验收标准缺失 | 存在交付物或服务成果 | 否 |

修改种子规则时允许规则总数从 32 增加，因为方向相反的语义必须拆成不同 `rule_code`。验收以“11 类覆盖完整、每条规则语义单一”为准，不以固定规则数量为准。

## 8. 状态与异常处理

以下情况进入任务 `blocked`：

- 附件缺失；
- 文件无法打开或内容为空；
- OCR 完全失败；
- 审批系统接口调用失败；
- 规则配置整体无法加载。

以下情况不进入 `blocked`，而产生 `needs_review`：

- 我方信息缺失；
- 审批信息与合同正文冲突；
- 单个字段证据不足；
- 单条 LLM 规则无可用模型且无可靠降级规则。

这样区分“流程无法继续”和“流程已完成但部分结论需要人判断”。

## 9. 文件级修改清单

| 文件 | 修改内容 |
| --- | --- |
| `db/schema.sql` | 新增权威上下文字段、解析版本字段、规则配置字段、`review_runs`、四状态评价、结果摘要绑定和幂等唯一键 |
| `db/seed.sql` | 补充 `applies_when_json` 与显式 fallback；拆分方向敏感规则 |
| `app/models.py` | 同步表结构、关系、唯一约束和 Python 枚举映射 |
| `app/enums.py` | 新增上下文状态、合同标签、业务角色、合同类型、评价状态和审查结果状态 |
| `app/schemas.py` | 增加受控规则配置、审查上下文、解析证据和规则评价 DTO |
| `app/rules/evaluator.py` | 实现固定判定顺序和单一 `evaluate` 接口 |
| `app/rules/applicability.py` | 解释和校验 `applies_when_json` |
| `app/rules/aggregator.py` | 只聚合 `hit`，单独统计 `needs_review` 与 `not_applicable` |
| `app/services/writeback_service.py` | 增加上下文冲突、待人工判断和内容摘要一致性门禁（**落点勘误**：本稿原写作 `app/harness/policy.py`；M6 实施时并入回写服务，见 M6 计划 Task 4。仓库中不存在 `app/harness/` 包） |
| `app/db.py` | 为每个 SQLite 连接启用 `PRAGMA foreign_keys=ON` |
| `scripts/init_db.py` | 读取与应用统一配置；初始化后校验外键和规则配置 |
| `tests/` | 增加数据约束、四状态、方向规则、缺失规则、版本隔离和幂等测试 |

## 10. 测试与验收矩阵

至少覆盖以下场景：

| 场景 | 预期结果 |
| --- | --- |
| 同一条款，我方分别为采购方和销售方 | 得到不同适用范围或不同规则评价，不发生方向反转 |
| 标准商品采购合同无知识产权条款 | `not_applicable` |
| 软件开发合同无知识产权条款且全文可读 | `hit` |
| 软件开发扫描件 OCR 质量不足 | `needs_review` |
| 审批系统声明我方为乙方，正文匹配甲方 | `needs_review / CONTEXT_CONFLICT` |
| 金额字段未找到但文档完整可读 | 按合同类型和规则要求决定 `hit` 或 `not_applicable` |
| 金额字段因解析失败为空 | `needs_review`，不能报金额缺失 |
| LLM 不可用且规则有 fallback | 执行确定性降级 |
| LLM 不可用且规则无 fallback | `needs_review / MODEL_UNAVAILABLE` |
| 同一任务重新解析并重新审查 | 新旧审查批次完全隔离 |
| 确认后修改回写正文 | 旧确认失效 |
| 两个并发请求回写同一结果 | 数据库唯一约束保证最多一次成功 |
| ORM 删除任务 | SQLite 外键级联实际生效 |

验收输出至少展示：

```text
本次评价规则 38 条：命中 3、未命中 21、不适用 12、需人工判断 2
总风险等级：高
结论完整性：需人工判断
```

## 11. 实施顺序

本设计已确认。按 **D1（方案甲）**，本次实施顺序如下——**止于"地基"，规则评价引擎留待 M5**：

1. 修订领域枚举（`app/enums.py`）、数据库结构与 ORM 映射：
   - 新增 `review_runs`；
   - `approval_tasks` 增加权威审查上下文字段；
   - `contract_parses` 增加解析版本与质量字段；
   - `review_rules` 增加 `rule_version` / `applies_when_json` / `fallback_match_json`；
   - `rule_hits` 保留物理列名 `hit_status`（四态）并增加 `evidence_json` / `reason_code` / `reason_text` / `rule_version`；
   - `review_results` 增加批次绑定、计数与内容摘要绑定；
   - `comment_logs` 增加 `idempotency_key`；
   - **修复 `app/db.py`：为每个 SQLite 连接启用 `PRAGMA foreign_keys=ON`**。
2. 建立数据库约束及结构一致性测试（校验 `schema.sql` 与 `models.py` 一致、外键级联实际生效）。
3. 实现规则配置 DTO 与加载校验（`applies_when_json` / `fallback_match_json` 的受控校验），非法配置必须使规则加载失败并写日志。
4. 重写种子规则：补充 `applies_when_json` 与显式 fallback，按业务角色拆分方向敏感规则。
5. 重建本地数据库并完成回归验证。
6. 更新主项目计划中的 M1 完成条件。
7. 进入 **M2（mock 审批系统）**。

> M5 才实现：`app/rules/evaluator.py`（四状态判定）、`app/rules/aggregator.py`（风险聚合）、`app/services/writeback_service.py`（上下文冲突与内容摘要门禁；本稿原写作 `app/harness/policy.py`，M6 实施时并入回写服务）。本次仅定义其数据结构与语义。

## 12. 完成标准

### 12.1 本次（地基阶段）完成的判据

- 数据库结构与 ORM 映射一致（含 `hit_status` 物理列名与 `evaluation_status` 属性名的映射）；
- SQLite **每个连接**都启用外键约束，`ON DELETE CASCADE` 实际生效；
- `review_runs` 存在，且 `rule_hits` / `review_results` 均能追溯到明确批次；
- 规则配置（`applies_when_json` / `fallback_match_json`）通过受控 Pydantic 校验，未知键或非法枚举使规则加载失败并写入日志；
- 种子规则覆盖 11 类，方向敏感规则已按业务角色拆分为语义单一的独立 `rule_code`；
- 结构层面保证解析模块无法写入权威上下文字段；
- 所有新增自动化测试通过。

### 12.2 属于 M5（规则评价引擎）的判据（不在本次范围）

- 每个启用规则在每个已完成审查批次中恰好产生一条评价；
- 四种评价状态具有互斥、稳定的语义，且判定顺序固定；
- 缺失类规则不会把解析失败当成合同缺失；
- 方向敏感规则不会因甲乙方身份变化而反向误判；
- 新旧审查批次互不污染；
- 人工确认与当前回写内容严格绑定；
- 并发回写最多成功一次。
