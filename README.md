# 合同审批审查系统

面向企业合同审批场景的自动审查工具服务。

> **快速启动**：见 [startguide.md](startguide.md)（详细步骤）；
> 一键启停：`.\scripts\start_all.ps1` / `.\scripts\stop_all.ps1`。

> **定位：AI 出证据，人做决定。**
> 本系统不代替人工审批，而是生成**带证据定位**的风险审查意见，
> 并写回审批系统的评论区；每一处结论都能被追问"依据在哪"。

---

## 1. 当前状态

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M0 | 环境、目录、FastAPI 骨架 | ✅ |
| M1 / M1.5 / M1.6 | 数据模型、规则语义、复审修正 | ✅ |
| M2 | mock 审批系统（外部对接方） | ✅ |
| **M3** | **审批接入：工具 1–3 + 去重 + 附件保存** | ✅ **19/19 验收通过** |
| **M4** | **分层解析管线 + Worker 进程** | **已完成**（T1–T12，见设计文档 §0.7–§0.16）；验收 `scripts/verify_m4.py` **56/56，exit=0** |
| **M4.5** | **PARSE Worker 集成收口（M6 前置门禁）** | ✅ **已完成**（M6 实施计划 Task 0）；`tests/test_parse_worker_integration.py` 用**生产** `make_handler` 走完"入队 → 领取 → 解析 → 工件 → 原 `parse_id` → `result_ref`" |
| **M5** | **四状态规则评价引擎 + 受控 LLM + 风险聚合** | ✅ **31/31 验收通过**（`scripts/verify_m5.py`，exit=0） |
| **M6** | **结果保存 + 确认摘要 + Outbox + 幂等回写与重试** | ✅ **28/28 验收通过**（`scripts/verify_m6.py`，exit=0） |
| **M7** | **七工具门面 + 任务/确认/重试/规则管理 + RBAC + 附件下发 + MCP 形态** | ✅ **43/43 验收通过**（`scripts/verify_m7.py`，exit=0）；全量基线 **收集 1339（1336 passed / 3 skipped）** |
| **M8** | **React 五模块调用端** + 规则管理 / 运行管理入口 | ✅ **完成**：五模块 + `/rules` + `/ops` 可用；验收走查 `scripts/verify_m8.py`（9/9，浏览器回归如实 unmet）；**走查修复 2 个真缺陷**（空 `allowed_types` 判死已解析任务、门禁失败任务卡"正在解析"）；契约形状双向钉住（`apiShapes.json` + `apiShape.test.ts`）；前端 **223 测试**、后端 **1393 passed**；演示脚本 `docs/demo/m8-demo-script.md` |
| **M9** | **基础设施迁移：PostgreSQL / Alembic / Redis / MinIO / Docker Compose** | ✅ **完成**：验收 `scripts/verify_m9.py` **8/8，exit=0**。SQLite 仍是默认（零配置可跑）；PG/Redis/MinIO 经端口与适配器接入，**业务代码零改动**；Alembic 基线（13 表 / CHECK / 复合外键 / 部分唯一索引）+ 往返与结构比对测试；领取 `SKIP LOCKED`、幂等 SAVEPOINT；`docker compose config` 通过、三镜像构建通过；compose 拓扑（内网 + healthcheck + 一次性 migrate 作业 + SIGTERM 优雅停机） |

**实际规模**：**13 张表** · 40 条规则 · **7 个工具已全部落地**
（**REST 与 MCP 两种形态，共用同一套门面**） · **测试数以 `pytest` 实际输出为准**。
M7 新增的后端接口：任务 / 作业 / 评价 / 结果查询、立场与结果确认、
附件内容与标准文档下发、人工重试、规则管理（版本化）、日志与审计查询。

> 这里**刻意不写测试数**：它会随里程碑持续增长，写死就一定会漂移，
> 而"漂移了没人发现"比"没有这个数字"更糟 —— 本项目已经栽过两次。
> 需要具体数字时跑 `pytest -q`，它的最后一行就是。
>
> **13 是实测的**，两条独立口径互相印证：
> `grep -c "^CREATE TABLE" db/schema.sql` → 13；
> `grep -c "__tablename__" app/models.py` → 13。
> （表名：`approval_attachments`、`approval_tasks`、`audit_events`、`comment_logs`、
> `contract_parses`、`outbox_events`、`parse_artifacts`、`review_results`、`review_rules`、
> `review_runs`、`rule_hits`、`task_logs`、`workflow_jobs`。）
>
> 这个数字**改过**，而且不是均匀地改：M3 收口时是 10 表，M4 加了 `parse_artifacts` 到 11，
> **M5 一张表都没加**，M6 又加了 `outbox_events` 与 `audit_events` 到 13。
> 它在这里曾经同时出现过两种错法 —— 一是**估**了一个 13（恰好与今天的实测值相同），
> 二是值已变而文档没变。两种错法看起来完全一样，这正是它每次都要重新数的原因。
（数字以 `pytest` 与 `/health` 的实际输出为准，不以文档为准。）

---

## 2. 快速开始

```powershell
# 1) 依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2) 配置（默认值即可本地跑通）
Copy-Item .env.example .env

# 3) 建库：含外键自检与规则自检，任何一项不过都会报错退出
.\.venv\Scripts\python.exe scripts\init_db.py --reset

# 4) 起 mock 审批系统（外部对接方，端口 8001）
.\scripts\run_mock.ps1

# 5) 起工具服务（端口 8000，另开一个终端）
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload

# 5b) 前端控制台（M8 起；端口 5173，已代理 /api → 8000）
cd frontend
npm install --ignore-scripts     # ⚠️ 见下方说明
npm run dev
cd ..

# 6) 验收证据：逐条打印实测值，退出码可直接作门禁
.\.venv\Scripts\python.exe scripts\verify_m3.py     # M3：19 条
.\.venv\Scripts\python.exe scripts\verify_m4.py     # M4：56 条
.\.venv\Scripts\python.exe scripts\verify_m5.py     # M5：31 条
.\.venv\Scripts\python.exe scripts\verify_m6.py     # M6：28 条
.\.venv\Scripts\python.exe scripts\verify_m7.py     # M7：43 条

# 7) 全部测试
.\.venv\Scripts\python.exe -m pytest -q
```

### 2b. 接入本地模型 API（M11：9 条 `llm` 规则真正调用模型）

任何 **OpenAI 兼容端点**都能接（vLLM / Ollama / LM Studio / 云 API 均可），
在 `.env` 填三项即可，**留空则自动退回纯规则模式**（行为与 M11 前逐字一致）：

```env
# 例：Ollama 本地跑 qwen2.5（Ollama 的兼容端点带 /v1）
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:7b
# 例：vLLM（LLM_MODEL 必须逐字等于 --served-model-name，它参与批次幂等）
# LLM_BASE_URL=http://127.0.0.1:8000/v1
# LLM_API_KEY=EMPTY
# LLM_MODEL=qwen3-8b
```

```powershell
# 模型合格性实测（有退出码）：逐字摘录能力 / 可判定比例 / 假阴性
.\.venv\Scripts\python.exe scripts\check_llm_qualification.py
```

判据（全满足 → 退出码 0）：`MODEL_UNAVAILABLE` = 0；证据引用被作废 ≤ 1；
明确结论比例 ≥ 85%；样本里**确实存在**的风险条款必须判出。
报告里的"合计耗时"= 一份合同 9 条 llm 规则的最坏批处理时间。

### 2c. 容器化（M9：PostgreSQL / Redis / MinIO / Compose）

```powershell
# 前置：Docker Desktop 在跑；compose 会读项目根 .env 里的凭证变量
#   POSTGRES_PASSWORD / MINIO_ROOT_USER / MINIO_ROOT_PASSWORD / MOCK_APPROVAL_TOKEN

# 一键起全套（内网拓扑 + 一次性 migrate 作业 + healthcheck 依赖）
docker compose up -d --build
# 应用库就绪后打开 http://localhost:8080（Nginx → api:8000）

# 本地开发容器（等价于 compose 的依赖三件套，端口见 startguide）
docker run -d --name m9-pg   -e POSTGRES_USER=m9 -e POSTGRES_PASSWORD=m9 -e POSTGRES_DB=m9 -p 55432:5432 postgres:16-alpine
docker run -d --name m9-redis -p 56379:6379 redis:7-alpine
docker run -d --name m9-minio -e MINIO_ROOT_USER=m9admin -e MINIO_ROOT_PASSWORD=m9admin-secret -p 59000:9000 -p 59001:9001 quay.io/minio/minio:latest server /data

# M9 验收（fail-closed，8 条）
.\.venv\Scripts\python.exe scripts\verify_m9.py
```

> 切换姿势：`DB_URL` 改成 `postgresql+psycopg://…`（Alembic 管结构）、
> `REDIS_URL` 配上即启用唤醒加速、`STORAGE_BACKEND=minio` 切对象存储。
> **业务代码零改动** —— 全部经端口与适配器（组合根 `app/adapters/storage.py`
> 与 `composition/job_queue.py` 唯一装配）。

> ⚠️ **`PYTHONPATH` 里有 IDE 注进的 `sitecustomize.py` 时，请先清掉它再跑全量测试**：
> 那个钩子会在解释器退出时对"批量删除"抛 `SystemExit(1)`，表现为若干条
> **teardown ERROR**（测试本体是绿的）。`scripts/verify_m*.py` 因此在子进程里
> 主动剥掉 `PYTHONPATH`（见 `_subprocess_env`）。命令：
> `$env:PYTHONPATH=$null; .\.venv\Scripts\python.exe -m pytest -q`

接口文档：<http://127.0.0.1:8000/docs>　　健康检查：<http://127.0.0.1:8000/health>

> **前端安装为什么要 `--ignore-scripts`**：`pdfjs-dist` / `react-pdf` 把 `canvas`
> 列为**可选依赖**，而它的安装脚本要下载 GitHub 上的预编译二进制、失败后回退到
> `node-gyp` 本地编译（Windows 上通常没有 C++ 工具链）。
> 结果是 `npm install` **挂住几分钟然后失败**，而控制台本身在浏览器里
> **完全不需要 canvas**（它只在 Node 侧渲染 PDF 时用）。
> 关掉安装脚本即可绕开；本项目的构建与测试都不依赖任何原生编译。
>
> 若默认源太慢，可临时指定镜像（不改全局配置）：
> `npm install --ignore-scripts --registry https://registry.npmmirror.com`

前端三条门禁（与后端同样"退出码即结论"）：

```powershell
cd frontend
npm run lint        # eslint（0 problems 才算干净）
npm run typecheck   # tsc --noEmit
npm test -- --run   # vitest（jsdom，时区固定 Asia/Shanghai）
npm run build       # tsc --noEmit && vite build
```

---

## 3. 已交付的工具（7 / 7）

七个工具名与需求 2.4.10 **逐字一致**，由 `tests/test_m6_api.py` 的
`test_seven_required_tool_names_are_exposed_verbatim` 守着 —— 改名会让它直接失败。

**M7 起七个工具有两种形态，且调用同一套应用服务**（`app/tool_facade.py`）：

```text
app/api/tools.py   （REST） ─┐
                             ├─→ app/tool_facade.py → app/services/*
app/mcp_server.py  （MCP）  ─┘
```

两边的**机器错误码也一致**（`RESULT_NOT_FOUND` 等原样送达，不被泛化）——
`tests/test_m7_contracts.py` 与 `tests/test_mcp_tools.py` 各有同源比对。

| 工具 | 端点 | 作用 | 形态 |
| --- | --- | --- | --- |
| 工具 1 | `POST /tools/list_pending_contract_approvals` | 拉取待审批合同并按去重键入库 | 同步 |
| 工具 2 | `POST /tools/get_contract_approval` | 查询详情，同步权威上下文 / 表单 / 附件元数据 | 同步 |
| 工具 3 | `POST /tools/download_contract_attachment` | 下载附件：校验 → SHA-256 → 对象存储 → 受控物化 | 同步 |
| 工具 4 | `POST /tools/parse_contract_document` | 入队解析，返回 `task_ref` | **异步** |
| 工具 5 | `POST /tools/run_contract_rules` | 入队规则评价，返回 `task_ref` | **异步** |
| 工具 6 | `POST /tools/save_review_result` | 保存审查结果，**同步**返回 `result_id` | 同步 |
| 工具 7 | `POST /tools/write_approval_comment` | 落库回写意图并派发 Outbox，同步返回回写引用 | 同步返回 + 后台投递 |

> **为什么工具 6 是同步的**：它是**人的动作**（把结论与回写正文提交上来），
> 调用端立刻需要 `result_id` 才能继续确认与回写。返回 `task_ref` 会把
> "我提交的结论到底存下来没有"变成一个要轮询的问题——而这一步没有耗时的外部依赖，
> 异步化只会把不确定性搬给调用端。
>
> **为什么工具 7 同步返回、却由后台投递**：回写意图必须与业务状态**同事务**落库
> （见 §4.5），所以"接受了吗"是同步的；真正调用审批系统由 Outbox 派发器负责，
> 所以"送到了吗"是异步可查的。这两件事被刻意分开——
> 合成一件，就得到"接口返回 200 但数据库里没有这条意图"。

状态查询：`GET /api/jobs/{job_id}`（工具 4/5 的进度）、
`GET /api/results/{result_id}`（含**后端计算**的 `confirmation_valid`）、
`GET /api/writebacks/{attempt_id}`（回写尝试 + Outbox 投递状态）。

M7 新增的控制台接口（**全部要求身份**，权限见 §4.8）：

| 接口 | 作用 | 权限 |
| --- | --- | --- |
| `GET /api/tasks` · `/api/tasks/{id}` | 任务列表 / 详情（含阶段、错误码、回写两个层级） | `task:read` |
| `GET /api/jobs` · `/api/evaluations` · `/api/results` | 作业 / 四态评价 / 结果（分页与**总数由后端给**） | `task:read` |
| `POST /api/tasks/{id}/context/confirm` | 确认**审查立场**（只有 `complete` 可确认，幂等） | `result:confirm` |
| `POST /api/results/{id}/confirm` | 确认**结果与回写正文**（绑定后端算的摘要） | `result:confirm` |
| `GET /api/attachments/{id}/content` | 附件**原始字节**（支持 Range，不暴露对象键） | `task:read` |
| `GET /api/parses/{id}/document` | 标准文档（页尺寸 / 坐标系 / rotation / 逐字符几何） | `task:read` |
| `POST /api/tasks/{id}/retry` | 人工重试（从失败检查点恢复，**必须填原因**） | `ops:retry` |
| `GET /api/logs/{task_id}` | 运行日志（可按 `correlation_id` 追一次请求的全链路） | `task:read` |
| `GET /api/audit` | 审计事件（系统级事件默认**不可见**，需 `include_system=true`） | `audit:read` |
| `GET/POST/PATCH /api/rules` · `POST /api/rules/reload` | 规则管理与**激活前校验**（版本化修改） | `rule:manage` |

```powershell
# 工具 1：反复调用是安全的，已存在的任务只刷新字段
Invoke-RestMethod -Method Post http://127.0.0.1:8000/tools/list_pending_contract_approvals `
  -ContentType 'application/json' -Body '{"limit": 20}'

# 工具 2：详情是权威上下文与附件清单的唯一来源，会写库
Invoke-RestMethod -Method Post http://127.0.0.1:8000/tools/get_contract_approval `
  -ContentType 'application/json' -Body '{"instance_id": "HT-2026-0001"}'

# 工具 3：返回长期保存位置与受控临时物化路径
Invoke-RestMethod -Method Post http://127.0.0.1:8000/tools/download_contract_attachment `
  -ContentType 'application/json' -Body '{"instance_id": "HT-2026-0001", "attachment_id": "A-1001"}'
```

### 状态码语义：问的是"调用完成了吗"，不是"业务顺利吗"

| 情况 | 状态码 | 为什么 |
| --- | --- | --- |
| **业务事实**（附件不存在 / 空 / 超限 / 类型不符） | **200** + `outcome="blocked"` | 调用**成功确认了一个业务事实**，任务已阻塞等人工处理 |
| 目标不存在 | 404 | 没找到目标 |
| 参数非法 | 400 | 调用方写错了 |
| 瞬时故障（超时 / 5xx / 存储抖动） | **503** + `Retry-After` | 稍后重试**确实**有意义 |
| 上游拒了我们的凭据 | 502 | 重试无意义 |
| 没带可信身份 / 身份不可信 | **401** + `WWW-Authenticate: Bearer` | 该去换一份身份 |
| 身份可信但缺权限 | **403** | 换身份也没用，得换账号或补授权 |

> "这份合同的附件在审批系统里已经没了"是一个**业务结论**，不是系统故障。
> 用 5xx 会让调用端把它当成服务抖动而反复重试，任务永远等不到人处理。

⚠️ **401 与 403 绝不能合并**（见 §4.8）。两者都是"被拒"，但客户端的正确处置
截然不同：401 → 取新令牌后重试，403 → 重试多少次都一样。
合并成一个码时，客户端只能对两种处置二选一，必然有一半是错的。

---

## 4. 关键设计约定

### 4.1 依赖方向（硬约束）

```text
api/ · mcp_server.py      只做协议转换，不写业务判断
        ↓
services/                 业务逻辑（REST 与 MCP 共用）
        ↓
ports/                    仅 Protocol 与 DTO
        ↑
adapters/                 唯一允许 import 外部 SDK 的地方
```

接口层不得出现业务分支。一旦这里出现第一个 `if`，M7 的 MCP 形态要么复制它、
要么依赖它，两套协议的错误语义迟早分叉。

### 4.2 两级闸门必须分开

| 闸门 | 落点 | 回答的问题 |
| --- | --- | --- |
| **对象级** | `approval_tasks.UNIQUE(provider, tenant_id, instance_id)` | 同一审批单只能有一条任务记录 |
| **操作级** | `workflow_jobs.UNIQUE(idempotency_key)`，键含 **租户维度** 与**输入版本** | 同一租户、同一输入版本的同一操作不得重复入队 |

操作级闸门有两条**必须同时满足**的要求：

1. **必须含输入版本**：审批表单与附件都会变化，只认审批单号会让同一审批单的
   **第二次同步被唯一约束永久拒绝**（对象被永久卡死）；
2. **必须含租户维度**：`provider:tenant:instance:attachment`。
   少了它，两个租户下相同的实例号 + 附件号会算出**同一个键**，
   于是第二个租户复用第一个租户的作业、其失败还会改写对方的记录。

> 幂等键是**不透明标识符**，任何代码都**不得**从中反解析业务字段
> （附件编号允许含 `:`，反解析会得到被截断的片段）。
> 需要业务字段时由调用方显式传递。

### 4.3 作业台账 ≠ 入队

`workflow_jobs` 记录"这次调用发生过"与失败原因，**不代表异步执行**。
工具 1–3 在 M3 全部同步执行完毕；M4 引入 Worker 后，
表结构与写入路径都不需要改。

作业状态选择 `retry_wait` 还是 `failed` 的**唯一判据**是错误码的可重试性
（见 `app/enums.py` 的 `RETRYABLE_ERROR_CODES` 与 `is_retryable()`），
不允许调用方自行判断——各处自行判断迟早会出现"同一类问题在不同路径被分成两类"。

### 4.4 日志不写合同正文与表单敏感值

`form_data` 里可能含人员姓名、证件号、联系方式，而姓名这类普通文本
不会被任何脱敏规则命中。因此保障**来自"根本不传"，而不是"传了再脱敏"**；
脱敏是第二层防线。

### 4.5 回写意图与业务状态同生共死（Transactional Outbox）

一次回写要同时做成三件事：写 `comment_logs`（意图）、写 `outbox_events`（待投递）、
写审计。**这三行在同一个事务里**，要么全有、要么全无。

> 如果先提交业务状态、再单独发送外部调用，那么"进程在提交之后、发送之前被强杀"
> 就会留下一条**永远不会被投递**的意图，而且它在库里看起来完全正常。
> 这正是本项目反复针对的缺陷形态：**让"没做"看起来像"做完了"。**

投递由独立的 `OutboxDispatcher` 负责，它有两条不能省的规矩：

1. **超时是"结果未知"，不是"失败"**：外部调用超时后**必须先按幂等键去查**
   （`get_write_result()`）再决定是否重发，不能直接重试——直接重试就是重复评论；
2. **重试有界**：耗尽后把任务停在 `blocked` 等人工，而不是无限重试或静默放弃。

### 4.6 拒绝 ≠ 失败，状态 ≠ 原因

`write_status` 严格四个值，`reason_code` 另有一套稳定原因码，二者**正交**：

| 情况 | `write_status` | `reason_code` | 重试有意义吗 |
| --- | --- | --- | --- |
| 门禁判定不该写（未确认 / 立场不可信 / 无正文 / 已写过） | `not_written` | `WRITEBACK_POLICY_DENIED` 等 | **没有**（没有发起调用） |
| 调用超时 | `failed` | `APPROVAL_API_TIMEOUT` | 有，但**先查后重发** |
| 上游返回错误 | `failed` | `APPROVAL_API_ERROR` | 看错误码 |

把"被拒"记成 `failed` 会让它看起来像一次可以重试的故障，
于是排障的人会去查一个并不存在的网络问题。

### 4.7 结果版本与确认有效性：由后端判定

同一批次可以产出多份结果，只有**最新版本**才有资格回写。
`confirmation_valid` 由后端计算，三个条件同时成立才算有效：

```text
人工已确认  AND  确认时的摘要 == 当前正文摘要  AND  该版本仍是最新版本
```

> 前端**不得**自己比较两个摘要来推断"确认还有效吗"——
> 那是把一条业务口径复制到浏览器里。复制出去的口径迟早会分叉，
> 而分叉的表现是"页面说有效、后端拒绝回写"。M7 会把 `confirmation_valid`
> 放进结果接口（缺口 G-3）。

### 4.8 身份与授权：路由声明**权限**，不声明角色

**v1 不建用户表**（路线图第 30 行）。角色来自身份提供方的令牌声明，
本系统只做"角色 → 权限"的映射：

| 角色 | 权限 |
| --- | --- |
| `legal_reviewer` | `task:read`、`review:execute`、`result:save`、`result:confirm`、`writeback:execute` |
| `system_admin` | 上列全部 + `rule:manage`、`ops:retry`、`audit:read` |
| `read_only_auditor` | `task:read`、`audit:read` |

7 个工具端点全部要求身份（**这是一处破坏性变更**：M6 之前它们不需要）。

配置项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ENV` | `development` | `production` / `prod` 触发下面的启动校验 |
| `AUTH_MODE` | `dev` | `dev` = 请求头身份（**仅开发期**）；`jwt` = 校验 JWT |
| `JWT_PUBLIC_KEY` / `JWT_JWKS_URL` | 空 | 二选一，至少一个；前者为静态公钥，后者按 `kid` 取 |
| `JWT_ISSUER` / `JWT_AUDIENCE` | 空 | 生产环境**必填** |
| `JWT_ALGORITHMS` | `RS256` | 算法来自**配置**，不取令牌的 `alg` 头（防算法混淆） |
| `JWT_SUBJECT_CLAIM` 等 4 项 | `sub` / `name` / `roles` / `tenant_id` | 企业 IdP 的声明名可改 |
| `JWT_LEEWAY_SECONDS` | `0` | 允许的时钟偏移 |

**开发期**：`AUTH_MODE=dev`，身份从请求头读（`X-Actor-Id` 必填，
`X-Actor-Roles` 逗号或空格分隔，`X-Tenant-Id` 与配置不符则拒绝）：

```bash
curl -H 'X-Actor-Id: reviewer-1' -H 'X-Actor-Roles: legal_reviewer' ...
```

**三道 fail-closed 防线**（每一道都有测试）：

1. `lifespan` 在服务对外之前校验配置 —— 生产环境选了 `dev`、或 JWT 配置不全，
   **进程起不来**。放在启动期而不是"第一次请求时"，是因为后者会让一个
   没有任何身份体系的服务先跑起来并对外服务。
2. 未知的 `AUTH_MODE` **直接抛错**，不兜底。兜底成 `dev` 时，一个尾随空格
   （`AUTH_MODE=jwt `）就足以让整个身份体系消失，而现象是"一切正常"。
3. `DevHeaderIdentity` 自己在生产环境拒绝被构造 —— 防的是"以后有人写了
   另一个组合根"。

**两道容易写反的映射**：

- `AUTH_FAILED` 是**出站**码（"审批系统拒绝了我们的凭据"，502），
  与入站的 401 是两件事。复用会让"调用方没带令牌"变成"上游凭据问题"。
- 缺密钥是**运维配错**，不是"来者身份不可信"：抛 `AuthConfigurationError`
  让进程以 500 暴露，而不是 401。抛 401 会让每个调用方都去重新登录，
  而该做的是改配置 —— 那件事任何调用方都做不到。

**未识别的角色 → 零权限，但不拒绝服务**：IdP 先上、本系统后跟时，
报错会让所有人无法使用，而少给权限只让那一个人看到 403。
角色原话保留在 `Actor.roles` 里并写进 403 消息，否则"我明明有那个角色"就没有线索。

**不记录 bearer 令牌**：错误消息只带校验器的描述，不带令牌原文与载荷内容
（有测试从四种拒绝路径逐一断言）。`request.state.actor` 只放解析后的
`Actor`，**不放请求头** —— 挂上 `Authorization` 原文等于给未来任何一处
日志或序列化留了一条泄漏路径。


---

## 5. v1 已知限制

> 这些是**有意为之的取舍**，不是待修的缺陷。列出它们是为了让使用方
> 能在依赖某项行为之前知道边界。

### 5.1 `approval_code` 不再是全局唯一（重要）

**变更**：`approval_tasks.approval_code` 从 `UNIQUE` 降为**普通索引**；
唯一性改由 `UNIQUE(provider, tenant_id, instance_id)` 承担。

**为什么改**：需求 2.4.4 只要求"按唯一业务标识去重"，
**并未要求审批单编号跨企业、跨审批平台全局唯一**。
保留全局唯一会让**第二个企业接入时必然要迁移数据**，与租户目标直接冲突。
因此这不是弱化需求，而是把约束对齐到真实的去重键。

**对使用方的影响**：

- 去重行为不变：同一审批单不会被重复创建；
- 但**不同租户下可以存在相同的 `approval_code`**。
  按编号查询时必须带上租户维度，不能假设编号全局唯一：

```sql
-- ❌ 会在第二个租户接入后返回多行
SELECT * FROM approval_tasks WHERE approval_code = 'HT-2026-0001';

-- ✅ 带上租户维度
SELECT * FROM approval_tasks
WHERE provider = 'mock' AND tenant_id = 'default' AND approval_code = 'HT-2026-0001';
```

> M9 引入 PostgreSQL 时，这一条会体现在 Alembic 迁移里，
> `upgrade` / `downgrade` 都需可往返。

### 5.2 单租户

`tenant_id` 字段已就位，但 v1 固定为 `default`（由 `TENANT_ID` 配置）。
多租户的数据模型已具备，尚未提供租户管理与隔离策略。

### 5.3 对象存储为本地文件实现

`STORAGE_BACKEND=local`：对象落在 `<STORAGE_ROOT>/objects/`（内容寻址）。

- `presign_get()` 返回的是**受控相对路径**，没有真实的签名与过期能力，
  **不得下发给普通调用端**；M8 起由 MinIO 实现真正的短期授权。
- 对象键（`object_key`，形如 `sha256/ab/cd/<摘要>.pdf`）**只在本系统内部使用，
  不出现在任何接口响应里**（含工具 3）。它仍完整保存在
  `approval_attachments.object_key` 供内部使用。
- 工具 3 **返回 `file_path`**：这是需求 2.4.4 要求的「本地文件路径」，
  形式为**受控相对路径**（`workspace/…`），不含服务器绝对目录。
  ⚠️ 它与对象键是**两个不同性质的位置**，不可混用；
  它**不得**被当成可绕过鉴权的永久公开地址下发。

### 5.4 作业台账只写不消费（M3 期的状态，**已不再成立**）

M3 阶段没有任何进程消费 `workflow_jobs`；M4 引入 Worker、M6 引入 Outbox 派发器后，
这一条**已经关闭**：解析与规则作业由 `scripts/run_worker.py` 领取并重试，
回写由 `OutboxDispatcher` 投递。

仍未提供的是**人工重试接口**（把 `blocked` 的任务重新推起来）——那在 **M7** 交付。
在此之前，`blocked` 任务需要人工改库或重新发起调用。

### 5.5 详情同步的作业版本取自"已存"上下文

幂等键必须在发起调用**之前**算出来，而新内容只有调用之后才知道，
因此版本取库里**已存**的上下文摘要（与下载作业取已存 `file_checksum` 同一套语义）。

后果：数据变化后的**第一次**同步一定会拿到新作业（不会被吞掉），
但作业记录的版本号比内容落后一轮，且"首次写入上下文"会多产生一条作业。
这是刻意取舍——**宁可多一条记录，也不能把对象永久卡死**。

### 5.6 大模型是可选的，默认不启用

M5 已引入**受控** LLM 调用（白名单 + Schema 校验 + 证据反向核验）。
**未配置模型时自动降级为纯规则模式**，功能仍完整可用：
需要模型的规则按各自携带的 `fallback_match_json` 走降级结论
（实测 9 条 llm 规则**全部配了** fallback）。

因此"没接模型"不是缺陷状态，而是**默认状态**。反过来说，
线上**是否真的在用模型**要看配置，不能从"这条规则出了结论"推出来。

---

## 6. 测试与验收

### 六层测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q                 # 全部测试（数量以输出为准）
.\.venv\Scripts\python.exe -m pytest tests\contract -q  # 跨实现合约
```

`tests/contract/` 存放"同一端口的不同实现都必须通过"的测试。
`ObjectStorage` 的合约已就位——M9 换成 MinIO 时只需新增一个子类，
不必重写断言。该目录另有守卫测试，确保合约**不依赖任何具体实现**
（否则它会静默退化成"本地文件系统的测试"）。

### M3 验收证据

```powershell
.\.venv\Scripts\python.exe scripts\verify_m3.py     # 19 条标准，退出码 0/1
.\.venv\Scripts\python.exe scripts\verify_m3.py --keep   # 保留临时现场
```

### M4 验收证据

```powershell
.\.venv\Scripts\python.exe scripts\verify_m4.py     # 56 条标准，逐条实测值，退出码 0/1

# 其中两条走**真实 OCR**（慢），默认跳过：
#   验收 3（同一份内容的文本件与扫描件字段一致）
#   验收 14d 的端到端部分（图片附件真的读出字）
# 跳过在本脚本里算**未通过** —— 跳过却记成通过，报告里就出现一个未经检验的绿点。
$env:RUN_SLOW_OCR=1; .\.venv\Scripts\python.exe scripts\verify_m4.py
```

### M7 验收证据

```powershell
.\.venv\Scripts\python.exe scripts\verify_m7.py --verbose   # 43 条标准（实测 43/43，exit=0）
```

M7 的 43 条覆盖：七个工具名称与**位置参数顺序**逐字一致、
门面在副作用之前判定权限、**REST↔门面**与 **MCP↔门面**两组同源比对、
分页与总数由后端给、排序稳定、回写两个层级（任务级 / 尝试级）、
`confirmation_valid` 由后端计算、四态评价不丢、
**租户门（含嵌套 id 与整段 id 区间枚举）**、
附件 Range 矩阵（含后缀区间与 416）、**响应与响应头里永不出现对象键**、
两个 404 机器码不同、标准文档原样下发、区间请求也核验整份对象、
**重试矩阵**、回写重试只重新武装投递且归还预算、操作原因必填并留痕、
重试与审计权限、日志按关联 ID 可过滤、系统级审计事件**默认不可见**、
规则激活前校验（含停用规则）、**规则版本化**（使用中的版本不得就地改写）、
规则写入前校验、规则变更审计不属于任何任务、规则管理仅管理员、
MCP 七个工具与必填参数、MCP 身份 fail-closed、MCP 长任务返回 `task_ref`、
MCP 错误载荷保留机器码、生产环境拒绝启动、401/403 可区分、
审计动作取值域、路由不重复注册、以及全量回归。

### MCP 形态怎么起

```powershell
# stdio（给本机 MCP 客户端）：身份来自**启动配置里的环境变量**
$env:MCP_ACTOR_ID = "legal-zhang"; $env:MCP_ACTOR_ROLES = "legal_reviewer"
.\.venv\Scripts\python.exe scripts\run_mcp.py --transport stdio

# streamable-http：身份**逐个请求**从请求头解析（越权防线见下）
.\.venv\Scripts\python.exe scripts\run_mcp.py --transport streamable-http --port 8765
```

| 传输 | 身份来源 | 为什么不同 |
| --- | --- | --- |
| `stdio` | 进程环境变量 → 组成请求头 → `IdentityProvider.resolve()` | 没有 HTTP 请求可读；客户端配置里本来就在传环境变量 |
| `streamable-http` | **每个请求**的头 | 端点被多个调用方共用，"启动时解一次"= 所有人共用第一个人的身份 |

两条路径都走**同一个** `IdentityProvider` 端口，且都**拒绝匿名**：
没有身份来源时 `build_mcp_server` **构造即失败**。

> ⚠️ `requirements.txt` 的 `mcp` 是 **1.9.4**（原计划写 1.2.0）——
> 1.2.0 没有 `streamable-http` 传输，只有 stdio / sse / websocket。

### M5 / M6 验收证据

```powershell
.\.venv\Scripts\python.exe scripts\verify_m5.py --verbose   # 31/31，exit=0
.\.venv\Scripts\python.exe scripts\verify_m6.py --verbose   # 28/28，exit=0
```

M6 的 28 条覆盖：结果持久化与版本链、指纹与版本的库级约束、
确认有效性由后端计算、正文变更使旧确认失效、审计不可篡改、
门禁拒绝 / 放行的完整矩阵、**被拒时不留 Outbox 事件**、
意图 + Outbox + 审计的**同事务原子性（注入失败后回滚）**、
幂等键绑定、Outbox 表约束、严格作业输入、重复投递、
**提交后强杀 → 租约回收**、超时对账、有界重试、未知事件类型、
先进先出、七个工具名逐字一致、工具 6 同步、工具 7 门禁拒绝作为**业务结论**、
端到端闭环到达 `done`、**门禁被拒后修复重试仍是同一次尝试**、
回写失败**不重跑解析与规则**（$0 重算）、确定性失败不耗重试次数、
`job_type` 分派、以及全量回归。

三条约定：

- **`SKIPPED` 算未通过**（同上）；
- **参数化用例名必须能匹配**：`pytest -v` 给的是 `…::test_y[abc 123]`。
  初版只做精确匹配时，这类用例**永远采集不到结果** —— 报告说"未通过"，
  而它其实是全绿的。**报告说错话比报告缺一条更糟**，它会让人去查一个不存在的问题；
- **`UNMET`（需求未满足）同样计入退出码 1** —— 否则"不得以『OCR 已跑通』
  为理由宣告完成"就只是一句口号。

报告逐条给出**实测值**与来源。其中活体部分**刻意不复用测试的夹具与假适配器**：

> 独立验证的意义就在于不依赖被验证者的自我描述。
> 如果脚本复用测试的假实现，那么"测试本身写错"这种情况它同样发现不了——
> 那它就只是一份更啰嗦的 `pytest` 报告。

因此活体部分一切从外部观察：真 HTTP、真 SQLite 文件、真对象目录。
故障演练（注入 500 / 404 / 超时）会**真起 mock 进程**——
`timeout` 只在真实套接字上生效，不跨进程的超时演练等于没测。

---

## 7. 运行时目录

| 路径 | 用途 | 是否入库 |
| --- | --- | --- |
| `data/app.db` | SQLite 数据库 | 否 |
| `data/contracts/` | 真实合同文件（若存在则 mock 优先返回，供 M12 黄金合同集使用） | 否 |
| `storage/objects/` | 对象存储（内容寻址，不可变） | 否 |
| `storage/workspace/` | 受控临时物化目录，供后续解析读取 | 否 |
| `.pytest_tmp/` | 测试临时目录 | 否 |

> **"删库即清空数据"** 是本项目的预期行为：对象键与物化路径都在
> `data/` 与 `storage/` 下，删除这两个目录即可回到干净状态。
