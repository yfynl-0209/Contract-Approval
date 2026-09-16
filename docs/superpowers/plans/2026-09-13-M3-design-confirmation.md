# M3 设计确认与实施计划：ApprovalGateway、工具 1–3、去重与附件保存

日期：2026-09-13
状态：**已确认（含 4 项决策 + 3 处修正）**
上游依据：`docs/superpowers/specs/2026-09-13-enterprise-optimization-design.md` §5.1 / §5.2 / §6 / §7.3 / §7.4 / §10.1 / §10.2 / §15.1 / §16
里程碑定义：《合同审批审查系统-项目计划》§4 的 M3 行

---

## 0. 复核修正记录（第二稿）

初稿经复核后有 **4 项决策变更** 与 **3 处设计修正**。逐条记录在此，便于追溯：

| # | 初稿 | 修正后 | 性质 |
| --- | --- | --- | --- |
| ① | 保留 `UNIQUE(approval_code)`，复合键叠加 | **取消全局唯一**，改普通索引；去重只用 `UNIQUE(provider, tenant_id, instance_id)` | 决策翻转 |
| ② | `form_data_json` 落库 | 维持落库，**补充**：日志需脱敏；批次快照复用已有 `review_runs.context_snapshot_json` | 决策确认 + 补充 |
| ③ | M3 不建 REST 端点 | **同步提供工具 1–3 的薄 REST 入口** | 决策翻转 |
| ④ | 单一 `ApprovalGateway`（5 方法） | **拆分为 `ApprovalReadGateway` + `ApprovalCommentGateway`** | 接口拆分 |
| 修-1 | 作业幂等键可只含 `instance_id` | **必须含输入版本或请求指纹** —— 否则同一审批单**永远无法再次同步** | **真 bug 修正** |
| 修-2 | `STORAGE_WRITE_FAILED` 归确定性错误 | **按失败原因拆分**：超时/连接失败可重试，非法路径/校验失败不可重试 | 分类修正 |
| 修-3 | 文档一处说 Worker 归 M6，一处说 M4 引入 | **统一为 M4**（见 §2.3 里程碑归属表） | 一致性修正 |

### 0.1 修-1 的实质：作业闸门 ≠ 对象闸门

初稿把 `workflow_jobs.idempotency_key` 当成"同一审批单只能处理一次"的闸门。这是错的：
**详情审批表单可能发生变化**，若幂等键只含 `instance_id`，第二次同步会被唯一约束永久拒绝。

两个闸门必须分开：

| 闸门 | 落点 | 回答的问题 |
| --- | --- | --- |
| **对象级** | `approval_tasks.UNIQUE(provider, tenant_id, instance_id)` | 同一审批单**只能有一条任务记录** |
| **操作级** | `workflow_jobs.UNIQUE(idempotency_key)` | 同一**输入版本**的同一操作**不得重复入队** |

作业幂等键的构成：

```text
idempotency_key = {job_type}:{业务标识}:{输入版本}
```

| `job_type` | 业务标识 | 输入版本 | 语义 |
| --- | --- | --- | --- |
| `pull` | `{provider}:{tenant_id}` | 触发时间窗口（分钟级） | 同一分钟重复点击 → 合流为一个作业；下一分钟 → 新作业 |
| `detail` | `{instance_id}` | 外部版本（`updated_at` / 表单版本）；缺失时回退**请求指纹** | 详情变化 → 新键 → **允许重新同步** |
| `download` | `{instance_id}:{attachment_id}` | 外部 `ETag` 或 `Last-Modified`+`Content-Length`；缺失时回退请求指纹 | 附件更新 → 新键 |

**兜底取向**：拿不到外部版本时，幂等键退化为含请求指纹的**一次性键**——
只阻止"同一次调用的重复入队"，**不阻止后续同步**。
宁可多跑一次，也不能把对象永久卡死。

### 0.3 第二轮复核修正（2026-09-14）

| # | 问题 | 修正 |
| --- | --- | --- |
| 修-4 | **HTTP 429 限流**被归入"其他 4xx 一律确定性"，判为永久失败 | 新增瞬时错误码 `APPROVAL_RATE_LIMITED`；429 → `TransientGatewayError`，并在消息中带上 `Retry-After` |
| 修-5 | `int(value)` **静默截断浮点**（`1.9 → 1`） | 带小数的浮点报 `INVALID_GATEWAY_RESPONSE`；整值浮点（`1.0`）仍接受 |
| 修-6 | `bool(value)` 使**字符串布尔值语义颠倒**（`"false" → True`） | 新增 `_boolean()` 严格解析；无法识别的取值**报错而不是猜** |
| 修-7 | 已存在对象的 `stat()` 未进统一错误体系，**裸 `PermissionError` 泄漏** | `stat()` 与读取均纳入映射：权限 → `STORAGE_WRITE_DENIED`（确定性），其他 IO → `STORAGE_UNAVAILABLE`（瞬时） |
| 修-8 | `finally` 中清理临时文件失败会**顶掉真正的异常** | 清理改为尽力而为并吞掉自身异常 |
| 修-9 | 非 `TransportError` 的 HTTP 层异常（如 `DecodingError`）**裸逃到业务层** | 增加 `except httpx.HTTPError` 兜底 → 瞬时错误 |

**修-4 的根因值得记下来**：429 看起来"有处理"——它确实落在了兜底分支里，
只是分类是错的。这类"有分支、分类错"的漏洞靠抽查代表性状态码发现不了。
因此新增两条**不变量测试**：

- `test_every_error_status_raises_a_port_exception_with_code` —— 穷举 HTTP 400–511，
  断言每个状态码都产出带 `code` 的端口异常；
- `test_no_raw_httpx_exception_escapes_the_adapter` —— 保证业务层永远拿到可分类的异常。

**背景**：修-7 之所以是 P2 而非 P3，是因为它直接卡住 T4 ——
调度器只能靠 `error.code` 判断"重试还是立即阻塞"，
而裸异常没有 `code`，只能退化成 `except Exception`，等于把分类责任丢回给调用方。

### 0.4 第三轮：验收驱动的修正（2026-09-14，T10）

T10 逐条核对 19 条验收标准时发现**实现与冻结标准不一致**的三处。
标准是**与干系人的约定**，实现是我方决定；两者冲突时不能静默保留偏差。

| # | 问题 | 修正 |
| --- | --- | --- |
| 验收-1 | **详情同步不写作业台账**，与标准 6「第二次同步产生新作业」、标准 13「三个工具各写一条 `workflow_jobs`」冲突 | 新增 `_open_detail_job` / `_abort_detail_job`；版本取**已存**上下文摘要，与下载作业同一套语义 |
| 验收-2 | 详情作业首次创建时任务尚不存在，`task_id` 恒为 `NULL` | 同步成功后**回填** `job.task_id`；否则按任务查作业历史看不到它，`ON DELETE CASCADE` 也带不走它 |
| 验收-3 | `DownloadStatus.FAILED` **从未被写入过**：确定性下载失败后附件记录永远停在 `pending` | 确定性失败时把附件标记为 `failed`（**仅在记录已存在时**，不凭空造占位行） |

**验收-1 的根因值得记下来**：原设计给出的理由是"详情是单条短读取，同步完成，不需要入队"——
这条理由本身成立，但它**回答了另一个问题**。写作业台账 ≠ 入队异步执行：
详情仍是同步执行、当场完成，作业创建后立即置为 `succeeded`，
M4 的 Worker 依然不会消费它。把"不需要入队"当成"不需要台账"，
结果就是**详情同步失败在库里不留任何痕迹**——拉取失败有记录、下载失败有记录，
唯独详情失败查不到，而"这个单子为什么一直没同步上"恰恰最需要线索。

**验收-3 的判据不是"标准这么写了"**，而是：`pending` 的语义是"排队中，稍后会做"。
一份确定性失败的附件保持 `pending`，控制台会显示"待下载"，
而它实际上在等人处理 —— **状态与事实不符**，看板显示正常、任务却永远不动。

三条修正都补了反向用例（如"瞬时失败必须留在 `pending`"、
"记录不存在时不得凭空新建"），确保修的是问题而不是把行为改成另一个极端。

---

### 0.5 第四轮：外部复审修正（2026-09-14，T11 之后）

| # | 问题 | 修正 |
| --- | --- | --- |
| 修-10 | **非法阻塞留下矛盾数据**：`mark_blocked` 先写三个阻塞字段、再 `transition`；转换抛错时字段已改，而业务失败路径**同样会提交**，库里留下 `task_status=done` 却 `blocked_stage=download` | 调整顺序：**先验证转换合法，再一次性写入**，中间不留可能抛错的操作 |
| 修-11 | **下载作业幂等键缺租户维度**，与拉取/详情作业不一致：两个租户下相同的实例号 + 附件号会命中同一条作业，`job.task_id` 仍指向第一个租户的任务 | identity 补 `provider:tenant_id`，与另外两类作业一致 |
| 修-12 | **从幂等键反解析附件编号**：编号 `ATT:1` 含冒号，反解析得到 `1`；任务被正确阻塞而附件记录永留 `pending`，日志里的编号也是错的 | 附件编号由调用方**显式传入**失败处理，不再反解析 |
| 修-13 | **裸 `PermissionError` 泄漏**：`exists` / `put` / `presign_get` 三处 | 新增 `_is_file()`，把文件系统异常统一映射为带 `code` 的端口异常 |

**修-10 的根因值得记下来**：`mark_blocked` 的"先写后验"最初**不会**造成持久化污染 ——
因为当时 `get_db` 在异常路径上根本不提交。T8 为了"失败也要留痕"引入
"业务失败同样提交"之后，这个潜伏缺陷才变成**真正的数据不一致**。

> 这说明一件事：**放宽事务边界会放大所有"部分写入"**。
> 那次改动本身是对的，但它把一处原本无害的写法变成了缺陷 ——
> 因此改事务语义时，必须回头检查所有"可能中途抛错的多字段写入"。

**修-13 的根因更值得记**：源码里当时写着一段注释，声称
"`Path.is_file()` 内部会吞掉 `OSError` 并返回 False"。
那是**错的**（CPython 只吞 `ENOENT` / `ENOTDIR` / `EBADF` / `ELOOP`，
**权限错误原样抛出**），而这段错误注释恰好掩盖了三处真实泄漏 ——
写它的人以为自己验证过。

> **错误注释比没有注释更危险，因为它会让人停止怀疑。**
> 因此本次同时补了一条**不变量测试**（穷举每个公开方法），
> 而不是只修三处调用点：漏一个的形状，抽查发现不了。

新增 7 条测试，其中两条是不变量式的：

- `test_no_raw_filesystem_exception_escapes_the_adapter` —— 穷举每个公开方法，
  断言任何异常都是带 `code` 的 `AppError`；
- `test_illegal_block_writes_nothing` —— 断言四个字段**一个都不能变**
  （只查 `task_status` 会放过这个缺陷，因为转换失败恰好不改状态）。

修-10 与修-11 做过**对抗验证**：把代码临时改回缺陷版本，
两条测试如期变红（`assert 'download' is None`、`两个租户必须各有一条下载作业`），
恢复后转绿 —— 确认这两条测试真的在守它们声称守的东西。

---

> **修订（2026-09-14，M4/T2）**：验收 8 的**取证方式**改过一次，需求没变。
>
> 原证据靠"A-1001 与 A-1002 的内容**恰好**相同"—— 因为 M2 的占位 PDF 是运行时
> 按固定模板生成的，两份附件字节全同。那是**偶然条件，不是需求本身**。
>
> M4/T2 把夹具换成各自真实的中文合成合同之后，这个碰撞自然消失了
> （两份合同的正文本来就不一样），于是这条从 19/19 掉到 18/19。
> **改的是取证方式，不是判据强度**：现在同时验两件事 ——
> ① 对象键**等于**由内容摘要推导的键（结构性保证：同一内容不可能占两个对象）；
> ② 同一内容再下一次，对象数不增（行为性保证）。
> 比"碰巧两份文件一样"更强，也不会因为夹具换了内容而失效。

### 0.2 修-3 的里程碑归属（统一口径）

| 里程碑 | 引入的机制 | 为什么在这里 |
| --- | --- | --- |
| **M3** | `workflow_jobs` 表 + **同步执行**（只写作业台账，不入队） | 工具 1–3 是短操作，同步完成即可 |
| **M4** | **Worker 进程**（轮询 `workflow_jobs` 中 `queued`，**DB 即队列**） | 工具 4（解析 + OCR）是**第一个长任务**，必须异步 |
| **M6** | **Outbox + Dispatcher**（回写的外部可靠投递） | 回写是不可撤销副作用，需与业务状态同事务 |
| **M9** | Redis 成为队列通道；MinIO 成为存储实现 | **`workflow_jobs` 始终是作业的唯一真相来源**，Redis 只加速、不改变语义 |

> 这样从 M3 到 M9 不需要重建作业模型：M4 把"同步执行"换成"入队执行"，
> M9 把"轮询 DB"换成"Redis 通道"，`workflow_jobs` 表结构全程不变。

---

## 1. 事实核对（动手前先核对代码，不是照抄文档）

> ### ⚠️ 本节是**规划时快照**，不是当前状态
>
> 下表记录的是 **M3 动工之前**（T1 开工前后）的事实。
> 保留它的价值在于可追溯"当时的决策建立在什么事实上"，
> **但不能用它判断现在的代码**。
>
> **当前状态**以文末「T11 文档收口」与项目计划 §18 为准（**10 张表 · 40 条规则 · 477 个测试**）。
>
> 早期版本没有区分"快照"与"现状"，本节写着"325 个测试、`app/services/` 尚未建立"，
> 而文末已记录 T10/T11 完成 —— 同一文档自相矛盾，
> 读者无法判断哪一处才是真的。**状态类信息必须标明它的时间点。**

| 项 | 规划时的实际情况（**非现状**） |
| --- | --- |
| 当时的进度 | M0 / M1 / M1.5 / M1.6 / M2，以及 M3 的 **T1 数据地基**、**T2 端口与异常**、**T3 适配器**、**T4 工作流最小件** |
| 当时的实测 | **10 张表 · 40 条规则 · 325 个测试**（数字会随任务推进变化，以 `pytest` 输出为准） |
| `app/workflow/` | ✅ 已建立（T4） |
| `app/ports/`、`app/adapters/` | ✅ 已建立（T2 端口与异常、T3 两个适配器） |
| `app/services/`、`app/api/` | **当时尚未建立**，属 T5–T8 |
| `mock_approval` | 已完成且可用：待办 / 详情 / 下载 / 回写 / 故障注入，Bearer 鉴权 |
| `app/rules/` | 已有 `fields` / `applicability` / `scoping`（M5 的前置，M3 不动） |
| `approval_tasks` | 当时无 `provider` / `tenant_id` / `instance_id`；去重只靠 `approval_code UNIQUE` |
| `approval_attachments` | 当时有 `file_path` / `file_checksum` / `download_status`；**无对象存储键** |
| `workflow_jobs` | 当时**不存在**（计划 §5.1 归 M3） |
| `review_runs.context_snapshot_json` | **已存在** ✅ —— 批次上下文快照的落点就绪，M5 写入即可 |

**这张快照直接决定了 M3 的范围**：`approval_tasks` 缺去重键、附件缺对象键、
`workflow_jobs` 不存在 —— 三件事都在 M3 补齐（见 §3 数据变更）。

### 1.1 一个决定 M3 形态的接口事实

mock 的两个接口返回的信息量**不同**：

| 接口 | 返回 | 关键含义 |
| --- | --- | --- |
| `GET /api/instances/pending` | `approval_code` / `approval_title` / `applicant_name` / `apply_time` / `attachment_count` | **不含**权威上下文，**不含**附件清单 |
| `GET /api/instances/{id}` | 上述 + `our_party_name` / `our_party_contract_label` / `our_party_business_role` / `contract_type` / `form_data` / `attachments[]` | 权威业务事实与附件清单**只在详情里** |

由此推出 M3 的三个工具职责**不可混**：

```
工具 1 拉取  → 建/更新 approval_tasks；context_status = missing（拿不到业务事实）
工具 2 详情  → 同步权威上下文 + 附件元数据；context_status 由 missing 变为 complete
工具 3 下载  → 字节 → 校验 → SHA-256 → 对象存储 → 更新附件记录
```

> 这也解释了一个容易被误判为 bug 的现象：**刚拉取完的任务 `context_status` 就是 `missing`**，
> 这是正确行为，不是缺陷。

### 1.2 受影响的既有测试（必须同步修改）

| 测试 | 现状 | 处理 |
| --- | --- | --- |
| `test_duplicate_approval_code_rejected` | 断言 `approval_code` 全局唯一 | **改写**为：同租户同 `instance_id` 重复 → 拒绝；**跨租户同 `approval_code` → 允许** |
| `test_schema_consistency.py` 各列断言 | 不含新增列 | 补充新增列与 `workflow_jobs` 的表结构断言 |

---

## 2. 范围边界

### 2.1 M3 交付

- 端口层：`ApprovalReadGateway`、`ApprovalCommentGateway`、`ObjectStorage`；
- 适配器层：`MockApprovalGateway`（同时实现读写两个接口）、`LocalFileStorage`（内容寻址）；
- 接入模块：待办拉取、按唯一键去重、详情同步、权威上下文标准化与状态判定；
- 附件模块：下载、类型/大小校验、SHA-256、对象存储落盘、受控临时物化路径、失败转 `blocked`；
- 工作流最小件：`workflow_jobs` 表 + 作业台账 + 任务状态转换唯一入口（**只写不消费**）；
- 日志模块：稳定错误码 + 脱敏；
- **工具 1–3 的薄 REST 入口**（`POST /tools/...`，只做协议转换）；
- Adapter 合约测试 + M3 验收测试。

### 2.2 M3 明确不做

| 不做 | 归属 |
| --- | --- |
| 工具 4–7 的 REST 入口、完整 MCP 注册 | M7（M3 只做工具 1–3 的入口） |
| Worker / Scheduler 进程 | **M4**（见 §0.2） |
| Outbox Dispatcher | M6 |
| Redis / Celery | M9 |
| PostgreSQL / Alembic | M9 |
| MinIO | M9（本阶段用 `LocalFileStorage`，**同一端口**） |
| PDF 解析与 OCR | M4 |
| 四状态规则评价 | M5 |
| 实际执行评论回写 | M6（但端口定义并实现读写两个接口，见 §4.1） |

> **为什么不做 MinIO 也不做 Worker**：端口已经把两者隔离。M3 用本地实现跑通语义，
> M9 换实现只改适配器装配，不碰业务代码——这正是"端口/适配器"要买到的东西。

---

## 3. 数据变更

### 3.1 `approval_tasks`（+6 列，唯一约束调整）

| 列 | 类型 | 理由 |
| --- | --- | --- |
| `provider` | `TEXT NOT NULL DEFAULT 'mock'` | 去重键组成部分（§10.2） |
| `tenant_id` | `TEXT NOT NULL DEFAULT 'default'` | 轻量租户（§10.3）：**v1 固定默认租户，但字段先留**，避免接入第二个企业时全库迁移 |
| `instance_id` | `TEXT NOT NULL` | 去重键组成部分。**不给 DEFAULT**：默认空串会让唯一约束形同虚设 |
| `form_data_json` | `TEXT` | 调用端模块 2 必须展示审批表单（§11） |
| `blocked_stage` | `TEXT` | §7.4 记录失败位置，供人工重试从检查点恢复 |
| `last_error_code` | `TEXT` | §7.3 稳定错误码；`block_reason` 继续承担**可读原因** |

**唯一约束调整（决策 ①）**：

```sql
-- 取消：approval_code 全局唯一（去掉 UNIQUE，改普通索引，仅供查询与展示）
CREATE INDEX IF NOT EXISTS idx_tasks_approval_code ON approval_tasks(approval_code);

-- 新增：真正的去重键
UNIQUE (provider, tenant_id, instance_id)
```

**理由**：需求 2.4.4 只要求「按唯一业务标识去重」，**并未要求 `approval_code` 跨企业、跨审批平台全局唯一**。
而企业化设计 §10.2 已明确去重唯一键为 `provider + tenant_id + instance_id`。
保留全局唯一会让**第二个企业接入时必然迁移**，与 §10.3 的租户目标直接冲突。

> 因此这不是"弱化需求"，而是**把约束对齐到需求与实际去重键**：
> 旧约束比需求更强，且强在了错误的地方。

### 3.2 `approval_attachments`（+2 列）

| 列 | 类型 | 理由 |
| --- | --- | --- |
| `object_key` | `TEXT` | 长期保存位置（§5.2：长期位置由对象存储键表示） |
| `content_type` | `TEXT` | 从响应头落地，供 M4 解析路由判断 |

**复用已有列，不新增冗余字段**：

| 已有列 | 在 M3 中的语义 |
| --- | --- |
| `file_path` | **受控临时物化路径**（相对 `storage_root`），只供后续解析工具使用（§5.2） |
| `file_checksum` | SHA-256 |
| `file_size` | 字节数 |
| `download_status` | `pending / succeeded / failed` |

> `file_path`（临时物化）与 `object_key`（长期保存）是**两个不同性质的位置**。
> 调用端不得获得可绕过鉴权的永久地址。

### 3.3 新表 `workflow_jobs`（第 10 张表）

```sql
CREATE TABLE IF NOT EXISTS workflow_jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 可为空：拉取作业不属于任何单个任务
    task_id           INTEGER REFERENCES approval_tasks(id) ON DELETE CASCADE,
    job_type          TEXT NOT NULL,      -- pull / detail / download / parse / rule / result / writeback
    -- 操作级闸门：{job_type}:{业务标识}:{输入版本}，见 §0.1
    -- ⚠️ 不得只含 instance_id —— 那会永久阻断同一审批单的后续同步
    idempotency_key   TEXT NOT NULL UNIQUE,
    -- queued / running / retry_wait / succeeded / failed  （§7.2，与业务状态严格分离）
    job_status        TEXT NOT NULL DEFAULT 'queued',
    attempt_no        INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 3,
    next_retry_at     DATETIME,
    checkpoint_json   TEXT,               -- 检查点，供 §7.4 从失败位置恢复
    last_error_code   TEXT,
    last_error_text   TEXT,
    started_at        DATETIME,
    finished_at       DATETIME,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**为什么 M3 就建它**（不是"提前建空表"）：它的 `idempotency_key` 在 M3 就有**当下用途**——
是三个工具重放请求的幂等台账；M4 引入 Worker 时，**表结构与已写入的作业路径都不需要改**。

### 3.4 受控取值：`ErrorCode`（含修-2）

`app/enums.py` 新增 `ErrorCode`，**按可重试性分为两类**：

```text
瞬时错误（可退避重试）：
  APPROVAL_API_ERROR           审批系统 5xx
  APPROVAL_API_TIMEOUT         调用超时
  APPROVAL_UNREACHABLE         连不上
  STORAGE_UNAVAILABLE          存储超时 / 连接失败 / 后端 5xx     ← 修-2：不再一律归确定性

确定性错误（直接 blocked，不重试）：
  AUTH_FAILED                  401 / 403
  INSTANCE_NOT_FOUND           审批单不存在
  ATTACHMENT_MISSING           附件已被删除
  ATTACHMENT_EMPTY             空文件
  ATTACHMENT_TYPE_NOT_ALLOWED  类型不在白名单
  ATTACHMENT_TOO_LARGE         超过大小上限
  STORAGE_PATH_INVALID         非法路径 / 目录穿越               ← 修-2
  STORAGE_WRITE_DENIED         权限 / 配额不足                   ← 修-2
  CHECKSUM_MISMATCH            下载后 SHA-256 与声明不符          ← 修-2
  INVALID_GATEWAY_RESPONSE     响应体结构不符
```

> **修-2 的理由**：初稿把 `STORAGE_WRITE_FAILED` 一律归为确定性错误。
> 但"存储超时/连接失败"是**环境临时问题**，重试即可恢复；
> 只有"非法路径、权限不足、内容校验失败"才是**重试也不会变好**的确定性错误。
> 混为一谈会导致临时故障被误判为永久失败，任务被无谓地打成 `blocked`。

配套新增异常层级（`app/errors.py`）：

```python
class GatewayError(Exception): ...
class TransientGatewayError(GatewayError):   # 可退避重试
class PermanentGatewayError(GatewayError):   # 直接 blocked，不浪费重试
class StorageError(Exception): ...
class TransientStorageError(StorageError): ...
class PermanentStorageError(StorageError): ...
```

### 3.5 配置新增（`app/config.py` + `.env.example`）

```ini
APPROVAL_PROVIDER=mock
TENANT_ID=default
GATEWAY_TIMEOUT_SECONDS=10
ATTACHMENT_MAX_BYTES=20971520          # 20 MB
ATTACHMENT_ALLOWED_TYPES=application/pdf
STORAGE_BACKEND=local                  # local（M9 增 minio）
PULL_WINDOW_MINUTES=1                  # 拉取作业幂等键的时间窗口（§0.1）
```

### 3.6 文档同步

`db/schema.sql` 的**扩展清单**逐列登记本节全部新增列，标注「文档依据」或「设计理由」，
并**明确记录 `approval_code` 唯一约束的取消**及其理由（这是对初版 2.4.9 落地的调整，必须显式说明）。

---

## 4. 接口设计

### 4.1 端口拆分（决策 ④）：读接口与评论接口分离

```python
# app/ports/approval_gateway.py
class ApprovalReadGateway(Protocol):
    """读取能力。真实审批系统可能**只授予读取权限**，
    因此这一层必须能独立实现与被验收。"""
    def list_pending(self, limit: int) -> list[PendingApprovalDTO]: ...
    def get_detail(self, instance_id: str) -> ApprovalDetailDTO: ...
    def download_attachment(self, instance_id: str, attachment_id: str) -> DownloadedAttachmentDTO: ...

class ApprovalCommentGateway(Protocol):
    """评论能力。只有具备回写权限的实现才需要满足它。"""
    def write_comment(self, instance_id: str, content: str, *,
                      idempotency_key: str, operator_name: str | None = None) -> WriteCommentResultDTO: ...
    def get_write_result(self, instance_id: str, idempotency_key: str) -> WriteCommentResultDTO | None: ...

class ApprovalGateway(ApprovalReadGateway, ApprovalCommentGateway, Protocol):
    """同时具备两种能力的实现（如 Mock 与自建审批系统）。"""
```

**依赖倒置的关键**：**消费者依赖它实际用到的那一个窄接口**——

| 消费者 | 依赖 | 里程碑 |
| --- | --- | --- |
| `ApprovalInboundService` | `ApprovalReadGateway` | M3 |
| `AttachmentService` | `ApprovalReadGateway` | M3 |
| `WritebackService` | `ApprovalCommentGateway` | M6 |

这样"只支持读取的真实审批系统"**不必被迫伪造评论查询能力**，
而 M3 的使用与验收只覆盖读接口，M6 才启用评论接口。

### 4.2 端口 DTO

```python
@dataclass(frozen=True)
class PendingApprovalDTO:
    provider: str; tenant_id: str; instance_id: str
    approval_code: str; approval_title: str
    applicant_name: str; apply_time: str; attachment_count: int

@dataclass(frozen=True)
class AuthoritativeContextDTO:            # 权威业务事实，非解析推断
    our_party_name: str | None
    our_party_contract_label: str | None
    our_party_business_role: str | None
    contract_type: str | None

@dataclass(frozen=True)
class AttachmentDTO:
    attachment_id: str; file_name: str; file_type: str; available: bool

@dataclass(frozen=True)
class ApprovalDetailDTO:
    instance_id: str; approval_code: str; approval_title: str
    applicant_name: str; apply_time: str
    context: AuthoritativeContextDTO
    form_data: dict[str, Any]            # 落库到 approval_tasks.form_data_json（决策 ②）
    attachments: tuple[AttachmentDTO, ...]

@dataclass(frozen=True)
class DownloadedAttachmentDTO:
    content: bytes; file_name: str; content_type: str

@dataclass(frozen=True)
class WriteCommentResultDTO:
    write_status: WriteStatus; external_comment_id: str | None
    replayed: bool; response_text: str | None
```

**关键约束**：DTO 是**内部标准化结构**，厂商原始字段不外泄（§5.1）。
适配器负责错误码转换——业务层只看到 `Transient*Error` / `Permanent*Error`。

### 4.3 端口：`app/ports/object_storage.py`

```python
@dataclass(frozen=True)
class ObjectRef:
    key: str; size: int; sha256: str; content_type: str

class ObjectStorage(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str) -> ObjectRef: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def presign_get(self, key: str, *, expires_in: int) -> str: ...

def content_addressed_key(sha256: str, *, suffix: str) -> str:
    """内容寻址键：sha256/ab/cd/<sha256>.<ext>

    同一份文件被多个审批单上传时只保存一份；
    同时与 §8.4 的解析缓存键（文件 SHA-256 + 解析器版本 + 配置版本）对齐。
    """
```

### 4.4 应用服务：方法名与 7 个工具**逐字对齐**

这是本阶段最重要的约定——**M7 的门面将是零逻辑的协议适配器**：

| 服务方法 | 对应工具 |
| --- | --- |
| `ApprovalInboundService.list_pending_contract_approvals(limit=20)` | 工具 1 |
| `ApprovalInboundService.get_contract_approval(instance_id)` | 工具 2 |
| `AttachmentService.download_contract_attachment(instance_id, attachment_id, file_name=None)` | 工具 3 |

```python
# app/services/pull_service.py
class ApprovalInboundService:
    """接入模块 —— **唯一**允许写 `approval_tasks` 权威上下文字段的模块。

    解析模块（M4）只能读这些字段，绝不写入：这是 §3.1 三类信息不可混用的执行点。
    """

    def __init__(self, gateway: ApprovalReadGateway, session: Session, log: LogService) -> None: ...

    def list_pending_contract_approvals(self, limit: int = 20) -> PendingListResult:
        """拉取待办并按 (provider, tenant_id, instance_id) 去重入库。

        去重语义（需求 2.4.4）：已存在则**只更新**审批单本身的字段与 updated_at，
        **不重建**任务、不清空已有解析结果、不重置 task_status。
        """

    def get_contract_approval(self, instance_id: str) -> ApprovalDetailResult:
        """同步详情：权威上下文 + form_data_json + 附件元数据（download_status 仍为 pending）。"""

    def _upsert_task(self, dto: PendingApprovalDTO) -> tuple[ApprovalTask, bool]: ...
    def _apply_context(self, task: ApprovalTask, ctx: AuthoritativeContextDTO) -> ContextStatus:
        """4 个字段齐全且取值合法 → complete；任一缺失 → missing。

        `conflict` 本阶段**不可能**产生：它需要解析结果做交叉核验（M4）。
        """

# app/services/attachment_service.py
class AttachmentService:
    def download_contract_attachment(
        self, instance_id: str, attachment_id: str, file_name: str | None = None
    ) -> DownloadResult:
        """下载 → 校验（类型/大小/非空）→ SHA-256 → 对象存储 → 物化 → 更新记录。

        确定性错误 → 直接 blocked，不重试；
        瞬时错误（超时/5xx/连不上/存储不可用）→ 交给作业重试，耗尽后 blocked。
        """

    def _safe_materialize_path(self, *parts: str) -> Path:
        """防目录穿越：最终路径必须落在 storage_root 内。非法则 STORAGE_PATH_INVALID。"""

# app/services/log_service.py
class LogService:
    def log(self, task_id: int | None, level: str, log_type: str,
            content: str, *, error_code: ErrorCode | None = None,
            operator: str | None = None) -> None:
        """写 task_logs。**必须脱敏**（§14）：

        - 合同正文 / OCR 全文 / 模型完整输入 → 不落日志；
        - `form_data_json` 中的**个人信息与证件号类字段** → 不落日志（决策 ② 补充）；
        - 令牌、密钥 → 不落日志，且异常信息中的 Authorization 头需过滤。
        """

# app/workflow/state_machine.py —— 任务状态转换的唯一入口
def transition(session, task, to_status: TaskStatus, *, reason: str | None = None) -> None: ...
def mark_blocked(session, task, *, stage: str, error_code: ErrorCode, message: str) -> None: ...

# app/workflow/jobs.py —— 作业台账（M3 只写，不消费）
def build_idempotency_key(job_type: str, identity: str, version: str | None) -> str:
    """{job_type}:{identity}:{version}；version 为空时回退为请求指纹（见 §0.1）。"""
def create_job(session, *, job_type: str, idempotency_key: str,
               task_id: int | None = None, max_attempts: int = 3) -> tuple[WorkflowJob, bool]: ...
def mark_running(session, job) -> None: ...
def mark_succeeded(session, job, *, checkpoint: dict | None = None) -> None: ...
def mark_failed(session, job, *, error: Exception | None = None,
                error_code: ErrorCode | None = None, message: str = "") -> None:
    """瞬时错误 → retry_wait + next_retry_at（指数退避）；确定性错误 → failed。"""
```

### 4.5 适配器

```python
# app/adapters/approval/mock_approval_gateway.py
class MockApprovalGateway:
    """同时实现 ApprovalReadGateway 与 ApprovalCommentGateway。

    对接**独立进程**的 mock 审批系统（端口 8001）：走真实 HTTP、真实鉴权、
    能真实触发 500 / 504 / 404 故障，不是同进程替身。
    """

# app/adapters/storage/local_file_storage.py
class LocalFileStorage:
    """对象存储的本地实现。根目录 storage_root/objects/。

    与未来 MinIO 实现**同一语义**（内容寻址、不可变、按 key 取回），
    使 M9 换实现不触碰业务代码。
    """
```

### 4.6 错误映射表（适配器内完成，业务层只见异常类型）

| 外部返回 | 映射 | 分类 |
| --- | --- | --- |
| `401` / `403` | `PermanentGatewayError(AUTH_FAILED)` | 确定性 |
| `404` | `PermanentGatewayError(INSTANCE_NOT_FOUND / ATTACHMENT_MISSING)` | 确定性 |
| `409` | `PermanentGatewayError(IDEMPOTENCY_CONFLICT)` | 确定性 |
| **`429`** | `TransientGatewayError(APPROVAL_RATE_LIMITED)` + 带 `Retry-After` | **瞬时（修-4）** |
| `5xx` | `TransientGatewayError(APPROVAL_API_ERROR)` | 瞬时 |
| `408` / `504` / `httpx.TimeoutException` | `TransientGatewayError(APPROVAL_API_TIMEOUT)` | 瞬时 |
| `httpx.ConnectError` / 其他 `TransportError` | `TransientGatewayError(APPROVAL_UNREACHABLE)` | 瞬时 |
| 其他 `httpx.HTTPError`（如 `DecodingError`） | `TransientGatewayError(APPROVAL_API_ERROR)` | **瞬时（修-9，兜底）** |
| 响应体结构不符 | `PermanentGatewayError(INVALID_GATEWAY_RESPONSE)` | 确定性 |
| 存储超时 / 连接失败 | `TransientStorageError(STORAGE_UNAVAILABLE)` | **瞬时（修-2）** |
| 路径越界 / 权限不足 / 校验不符 | `PermanentStorageError(...)` | **确定性（修-2）** |

> **429 是 4xx 里唯一的例外**：其余 4xx 表示"你的请求有问题"（重试无意义），
> 而 429 表示"请稍后再来"（设计文档 §7.3 明确列为瞬时错误）。
>
> **不变量**：适配器**不得**把裸的 `httpx` 异常抛给业务层 ——
> 那样的异常没有 `.code`，调度器无法判断"重试还是立即阻塞"。

### 4.7 薄 REST 入口（决策 ③）

M3 提供工具 1–3 的 REST 路由，**路径与计划 §8.1 逐字一致**：

| 方法 | 路径 |
| --- | --- |
| `POST` | `/tools/list_pending_contract_approvals` |
| `POST` | `/tools/get_contract_approval` |
| `POST` | `/tools/download_contract_attachment` |

**协议层的三条硬约束**：

1. **不写业务判断**——只做 DTO ↔ Pydantic 模型转换、调用服务、异常映射；
2. 复用同一应用服务，M7 补上工具 4–7 与 MCP 注册时**不重复业务逻辑**；
3. 异常 → HTTP 映射：

| 异常 | HTTP | 说明 |
| --- | --- | --- |
| `PermanentGatewayError` | `502` | 外部系统契约问题；资源不存在时用 `404` |
| `TransientGatewayError` | `503` + `Retry-After` | 可重试 |
| 业务结果是 `blocked` | **`200`** | 工具调用**成功**，业务状态是"阻塞"，不是 HTTP 错误 |

> 第 3 条容易被写错：附件缺失导致的 `blocked` 是**正常返回的业务结果**，
> 应返回 200 并在响应体里带 `task_status` / `last_error_code`，
> 而不是抛 5xx。否则调用端无法区分"系统坏了"与"这份合同缺附件"。

---

## 5. 验收标准（可验证，逐条给证据）

| # | 验收项 | 验证方式 | 对应设计 |
| --- | --- | --- | --- |
| 1 | 连续拉取 3 次，`approval_tasks` **仍只有 6 行** | pytest 断言行数与内容未被重复覆盖 | M3 完成标志 |
| 2 | **同 `approval_code`、不同 `tenant_id`** 可同时存在（决策 ① 反例） | pytest：插入两条不报 IntegrityError | §0 决策 ① |
| 3 | 同租户同 `instance_id` 重复插入被拒绝 | pytest `IntegrityError` | §10.2 |
| 4 | 拉取后 6 条任务 `context_status = 'missing'` | pytest | §1.1 |
| 5 | 详情同步后 `context_status = 'complete'`，4 字段与 mock 一致，`form_data_json` 已落库 | pytest | §10.1 / §3.1 |
| 6 | **详情变化后可再次同步**（作业幂等键含版本，不被永久阻断） | pytest：改 mock 数据 → 第二次同步产生新作业并成功 | **修-1** |
| 7 | 附件下载：SHA-256 一致、`object_key` 可用、物化路径可读 | pytest + `storage/objects/` 检查 | §5.2 |
| 8 | **同一内容只占一个对象**（对象键由内容摘要推导，内容寻址） | 活体：键 == 由摘要推导的键；同内容再下一次对象数不增 | §8.4 / **M4-T2 修-2** |
| 9 | 附件缺失（`HT-2026-0005` / `A-5002`）→ 附件 `failed`、任务 `blocked`、`blocked_stage='download'`、`last_error_code='ATTACHMENT_MISSING'` | pytest + 故障注入 | §7.3 / §7.4 |
| 10 | 注入 `500` → 瞬时错误**重试**；注入 `404` → **不重试**直接 blocked | pytest 断言 `attempt_no` | §7.3 |
| 11 | **存储不可用 → 可重试**（`TransientStorageError`）；路径越界 → 不可重试 | pytest 用假存储注入 | **修-2** |
| 12 | 三个工具可**通过 REST 实际调用**（决策 ③）：`blocked` 场景返回 **200** 而非 5xx | `TestClient` 调用三个 `/tools/*` | §4.7 |
| 13 | 三个工具各写一条 `workflow_jobs`；同输入版本重复调用**不新建作业** | pytest | §7.2 |
| 14 | 合约测试：`ApprovalReadGateway`/`ApprovalCommentGateway`（Mock）与 `ObjectStorage`（Local）满足端口语义 | `tests/contract/` | §15.1 第 3 层 |
| 15 | 日志中**不含**合同正文与 `form_data` 敏感字段 | pytest 扫描 `task_logs.log_content` | §14 / 决策 ② |
| 16 | **既有测试全绿**（T3 后为 229 个，含改写后的唯一约束测试） | `pytest` | 回归 |
| 17 | 数据库层拒绝非法状态与非法计数（`write_status='rejected'`、`instance_id=''`、`retry_count=-1`、`job_status='whatever'`、`max_attempts=0`） | `tests/test_data_integrity.py` | **T1 复核修正** |
| 18 | `updated_at` 随 ORM 更新前进（不是只写创建时间） | 同上 | **T1 复核修正** |
| 19 | `db/schema.sql` 与 `models.py` 的 CHECK 约束**双向一致** | 同上 | **T1 复核修正** |

---

## 6. 实施计划（任务分解）

| # | 任务 | 产出 | 依赖 | 预估 |
| --- | --- | --- | --- | --- |
| **T1** | 数据地基 | `enums.ErrorCode`/`JobStatus`/`JobType`；`schema.sql` + `models.py` 同步（+6/+2 列、唯一约束调整、新表 `workflow_jobs`）；扩展清单登记；**改写唯一约束测试 + 新增跨租户用例** | — | 0.75 天 |
| **T2** | 端口与异常 | `app/ports/approval_gateway.py`（读/评论/组合三个 Protocol + DTO）、`app/ports/object_storage.py`、`app/errors.py` | T1 | 0.5 天 |
| **T3** | 适配器 | `MockApprovalGateway`（httpx + Bearer + 错误映射）、`LocalFileStorage`（内容寻址） | T2 | 1 天 |
| **T4** | 工作流最小件 | `app/workflow/{state_machine,jobs}.py`，含 `build_idempotency_key` | T1 | 0.5 天 |
| **T5** | 日志服务 | `app/services/log_service.py`（脱敏 + `error_code`） | T4 | 0.25 天 |
| **T6** | 接入服务 | `app/services/pull_service.py`（工具 1 / 工具 2 业务实现） | T3 T4 T5 | 1 天 |
| **T7** | 附件服务 | `app/services/attachment_service.py`（工具 3 + 校验 + 对象存储 + `blocked`） | T3 T4 T5 | 1 天 |
| **T8** | 薄 REST 入口 | `app/api/tools.py`（3 路由）+ `app/main.py` 挂载 + 异常映射 | T6 T7 | 0.5 天 |
| **T9** | 合约测试 | `tests/contract/test_approval_gateway.py`、`tests/contract/test_object_storage.py` | T3 | 0.5 天 |
| **T10** | 验收测试 | `tests/test_m3_pull.py`、`tests/test_m3_attachment.py`、`tests/test_m3_faults.py`、`tests/test_m3_api.py` | T8 T9 | 0.75 天 |
| **T11** | 文档与验收 | 计划文档 M3 勾选、README（含 `approval_code` 约束变更说明）、验收证据输出 | T10 | 0.25 天 |

**进度**：**T10 ✅ —— 19 条验收标准全部通过**（477 个测试）。

验收证据由 `scripts/verify_m3.py` 一键生成（`python scripts/verify_m3.py`），
逐条给出**实测值**与来源，退出码可直接用作门禁：

```text
[ 1] PASS  连续拉取 3 次，approval_tasks 仍只有 6 行
      实测 : 三次 created=[6, 0, 0] / updated=[0, 6, 6]；approval_tasks 行数=6
[ 5] PASS  详情同步后 complete，4 字段与外部一致，form_data 已落库
      实测 : context_status=complete；4 个字段与外部台账逐字段一致=True
[ 8] PASS  同一内容只占一个对象（对象键由内容摘要推导，内容寻址）
      实测 : 落库对象键=sha256/a1/fc/a1fce63c7d32837…；
             由内容摘要推导=sha256/a1/fc/a1fce63c7d32837…；两者相同=True；
             同内容再下一次：对象数 2 → 2（未新增=True）
[ 9] PASS  附件缺失 → 附件 failed、任务 blocked、错误码 ATTACHMENT_MISSING
      实测 : HTTP 200 / outcome=blocked；(task_status, blocked_stage,
             last_error_code)=('blocked','download','ATTACHMENT_MISSING')；
             附件记录 download_status=failed
[12] PASS  三个工具可经 REST 实际调用；blocked 场景返回 200 而非 5xx
      实测 : 工具1 (200,'pulled') / 工具2 (200,'synced')
             / 工具3 (200,'downloaded') / 工具3(blocked) (200,'blocked')
[13] PASS  三个工具各写一条 workflow_jobs，同输入版本不新建
      实测 : 首轮作业类型=['detail','download','pull']；
             作业数 首轮 3 → 版本稳定后 5 → 再调一轮 5（不再增长）
[16] PASS  既有测试全绿
      实测 : 477 passed
结论：19/19 通过
```

> 活体部分**刻意不复用测试的夹具与假适配器**：独立验证的意义就在于
> 不依赖被验证者的自我描述，否则"测试本身写错"它同样发现不了。
> 因此一切从外部观察——真 HTTP、真 SQLite 文件、真对象目录。

**T11 ✅ 文档收口**：

- 计划文档 §3 目录结构、§4 里程碑状态、§5.1 表清单、§18 进度跟踪已同步；
- 新建 `README.md`：定位、快速开始、三个工具用法与状态码语义、
  关键设计约定，以及 **v1 已知限制六条**（含 `approval_code` 约束变更的
  影响与正确查询写法）；
- M3 完成标志四条逐条勾选（见计划文档 §18）。

**M3 全部完成（T1–T11）**。下一步：按 §4.1 进入 **M4 设计确认 + 实施计划**。

**T7 已实测达成第二条完成标志**（真适配器 + 真 mock 服务，完整链路）：

```text
拉取: 6 条 | 详情: complete | 附件数 1
下载: contract_01_clean.pdf | 697 bytes | application/pdf
object_key: sha256/43/20/4320ee10797704009f25f2cab1dbb89b2da9dd9e4283fce92f77a1086f7493d9.pdf
file_path : workspace/HT-2026-0001/contract_01_clean.pdf
SHA-256 与物化文件一致: True
对象存储回读与物化文件一致: True
附件记录: A-1001 success | object_key 已写: True
```

**T6 已实测达成第一条完成标志**（真适配器 + 真 mock 服务）：

```text
第1次拉取: fetched=6 created=6 updated=0
第2次拉取: fetched=6 created=0 updated=6
第3次拉取: fetched=6 created=0 updated=6
approval_tasks 行数: 6          ← 完成标志达成
workflow_jobs 行数: 1           ← 同一时间窗口复用一条作业记录
详情: HT-2026-0001 | 上下文 complete | 业务角色 buyer | 附件 1
```

**合计约 7 个工作日**（含测试与 REST 入口）。

关键路径：`T1 → T2 → T3 → T6 → T7 → T8 → T10`。`T4/T5` 可与 `T3` 并行，`T9` 只依赖 `T3`。

> **T9 的范围说明**：T4–T8 已按"每个工作包同步增加测试"的原则，
> 把适配器的语义与错误映射测试写在 `tests/test_adapter_*.py`。
> T9 的职责因此收窄为：把这些用例**整理归入 `tests/contract/`**，
> 并补齐"两个适配器实现同一端口时行为一致"的对照测试
> （例如未来 `MinioStorage` 与 `LocalFileStorage` 跑同一套合约）。
> 不重复编写已经存在的断言。

---

## 7. 风险与对策

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 取消 `approval_code` 唯一约束后，去重失效 | 重复建任务 | 复合唯一键 + 专项测试（验收 2/3 一正一反两条） |
| 作业幂等键设计不当 | **对象被永久卡死**（修-1 的原始风险） | 键必含输入版本；无法获取版本时退化为一次性键；验收 6 专项覆盖"详情变化可再同步" |
| 存储错误分类错误 | 临时故障被判永久失败 | 按 §3.4 拆分；验收 11 用假存储双向验证 |
| 薄 REST 入口夹带业务逻辑 | M7 重复实现 | code review 硬检查：`app/api/` 不得 import `app/models.py` |
| 对象存储写入失败留下半截状态 | 附件记录与对象不一致 | 先写对象后更新记录；失败写 `download_status='failed'` + 错误码，对象成为孤儿但**不产生错误引用** |
| 提前引入作业概念导致过度设计 | 拖慢 M3 | 作业层**只写不消费**，不引入 Worker、不引入队列 |
| `file_path` 与 `object_key` 语义混淆 | 调用端拿到永久路径 → 安全边界失守 | 二者在 §3.2 已明确区分；`presign_get` 在 M8 前不对外暴露 |

---

## 8. 已确认的决策记录

| # | 决策 | 结论 | 理由（简述） |
| --- | --- | --- | --- |
| ① | `approval_code` 唯一约束 | **取消全局唯一**，改普通索引；去重只用复合键 | 需求只要求"按唯一业务标识去重"，未要求跨企业全局唯一；保留会让第二租户接入时必然迁移，违背已确认的租户设计 |
| ② | `form_data` 是否落库 | **落库**（`form_data_json`） | 审批平台不可用时历史详情仍可查看；配套要求：敏感字段不落日志、批次快照复用已有 `review_runs.context_snapshot_json` |
| ③ | M3 是否暴露 REST | **提供工具 1–3 的薄 REST 入口** | 名义上交付"工具 1–3"，只有服务类与 pytest 不能证明工具**可被调用**；REST 只做协议转换，M7 再补 4–7 与 MCP |
| ④ | 端口是否拆分 | **拆为 `ApprovalReadGateway` + `ApprovalCommentGateway`**，Mock 同时实现 | 不让"只支持读取的真实审批系统"被迫伪造评论查询能力；M3 只验收读接口，M6 启用评论接口 |
| 修-1 | 作业幂等键 | **必含输入版本或请求指纹** | 只含 `instance_id` 会永久阻断同一审批单的后续同步 |
| 修-2 | 存储错误分类 | **按失败原因拆分瞬时/确定性** | 超时与连接失败可重试；非法路径与校验失败不可重试 |
| 修-3 | Worker 归属 | **统一为 M4**（第一个长任务）；Outbox Dispatcher 归 M6 | 消除文档自相矛盾；`workflow_jobs` 全程为作业真相来源 |

---

## 9. 下一步

按 §6 的 **T1 → T11** 顺序开发；每个任务完成后立即跑
`pytest` 与 `python scripts/check_rules.py`，保持回归全绿。
