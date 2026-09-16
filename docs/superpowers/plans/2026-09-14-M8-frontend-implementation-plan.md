# M8 React Review Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the five required contract-review modules as a secure React application, including PDF evidence navigation, human confirmation, and writeback status.

**Architecture:** A task workbench contains detail, parse, rule, and result views so one contract retains a single navigation context. A typed client is the only network seam; TanStack Query owns server state, while the browser never recomputes risk, confirmation validity, permissions, totals, or workflow transitions. PDF rendering consumes M4 coordinates and M7 authorized content endpoints.

**Tech Stack:** Node 20, React 18, TypeScript 5, Vite, TanStack Query, React Router, PDF.js/react-pdf, Vitest, Testing Library, Playwright.

## Entry Gate（开始 M8 前必须满足）

- M7 的 OpenAPI 契约、认证/RBAC、租户隔离、附件 Range 接口及七工具 facade 必须先冻结并通过验收。
- TypeScript 类型从已验收的后端响应模型生成或逐项对照；前端不得用临时字段猜测后端契约。
- 若 M7 契约发生变化，先更新契约测试和本计划映射，再修改页面；不得在组件里加入兼容分支掩盖漂移。

### Entry Gate 实测（Task 2 期间逐条核对，2026-09-15）

**核对方式**：读 `app/api/**` 的路由装饰器与**实际构体函数**逐条对照设计文档 §6.2。

> ⚠️ **先说一个影响全部类型工作的事实**：M7 的查询接口响应体是**手搓 dict**
> （`views.page_json` / `jobs._job_json` / `results.result_row_json` / `admin._log_json` …），
> **没有 Pydantic `response_model`** —— 因此 **OpenAPI 的响应 schema 是空的**，
> "从已验收的响应模型生成 TypeScript 类型"这条路**走不通**（生成器只能生成 `{}`）。
> 类型只能照构体函数逐字段抄（`frontend/src/api/contracts.ts`，每个类型都标注了对应的后端文件），
> 而"它不会自己发现漂移"这个代价，由 Task 10 的**契约漂移检查**（拿真实响应逐项比对）兜住。

**结论：设计 §6.2 列的接口有 4 处没有落地。** 这不是"M7 实现偷懒"，而是
**M7 的 43 条验收清单与设计文档的接口清单不是同一份清单**：前者验收的是
"七个工具 + 查询/确认/重试 + RBAC"，后者是**按页面推导**出来的需求。
两份清单之间的差额，只有把 §6.2 逐条对照代码才会显形。

| # | 设计 §6.2 要求 | 现状 | 影响 | 处置 |
| --- | --- | --- | --- | --- |
| G-M8-1 | `GET /api/tasks/summary`（服务端聚合计数） | **缺** | 模块 1 的汇总卡片。§9 硬约束禁止前端自己 count，因此**不能绕过** | Task 3 前补（后端 + 测试） |
| G-M8-2 | 当前身份（角色 / 权限） | **缺** | Task 2 的"按权限渲染导航"没有数据来源；前端只能自己解令牌 → 角色→权限映射出现**第二份实现** | ✅ **本次已补**：`GET /api/me` + `tests/test_identity_api.py`（9 条） |
| G-M8-3 | 任务列表带**最新批次 `overall_risk_level`** | **缺** | 列表页无法显示或筛选风险等级。前端**不能**自己取批次再聚合 —— 那是"在浏览器里算业务结论" | Task 3 前补 |
| G-M8-4 | `GET /api/tasks/{id}/parses`（版本列表）+ `GET /api/parses/{id}/fields`（字段四态 + 证据） | **部分**：只有 `GET /api/parses/{id}`，字段内联在 `basic_info` / `clause_info` 里，**没有四态也没有证据数组** | 验收 5（四态分别呈现、`not_found` 与 `failed` 不合并）与验收 12（版本切换不混用证据）**无法成立** | Task 5 前补 |
| G-M8-5 | 设计 §4.1 空态的「拉取待办」按钮 | **缺**：触发拉取只有 `/tools/list_pending_contract_approvals`（给**外部系统**的），前端按硬约束只调 `/api/*` | 空列表页无法给出这个入口 | Task 3 的处置：**不放按钮**，改为说明"同步由定时任务或外部调用方触发"；要真正提供需新增 `POST /api/tasks/pull`（属于新决定） |
| G-M8-6 | 模块 2 要的 `form_data` 与**附件列表** | **缺**：详情只给了 `attachment_count`，没有 `form_data`、也没有附件行 | 模块 2 的两块内容都没有数据源 | ✅ **Task 4 已补**：详情加 `form_data` 与 `last_error_is_business_fact`；新增 `GET /api/tasks/{id}/attachments` |
| G-M8-7 | **立场的"人工修正"** | **缺**：只有"确认"，而确认**只允许 `complete`** | ⚠️ **死锁**：`missing`（刚拉取完任务的**正常**状态）无法确认、也无法填入 → 任务永久停住，而回写门禁要求可信立场 | ✅ **Task 4 已补**：`POST /api/tasks/{id}/context/confirm` 接受**可选请求体**（四条齐全 = 修正，同时确认） |
| G-M8-8 | `conflict` 的"**冲突双方**"与 `confirmed` 的"**by …**" | **缺**：接口只给当前值，没有两个来源的对照，也没有确认人与时间 | 界面说不出"哪两处不一致"、也说不出"谁确认的" | ✅ **冲突双方已补**（新列 `context_conflict_json` → `GET /api/tasks/{id}` 的 `context_conflict` → 界面对照表）；确认人/时间**仍缺** —— 要补需在详情里加 `confirmed_by` / `confirmed_at` |
| G-M8-9 | 附件失败的「**重新下载**」 | **缺**：重跑下载只有工具 3（外部路径），而 `/api/tasks/{id}/retry` 对 `download` 阶段返回 **409 `RETRY_NOT_SUPPORTED`** | 界面无法提供这个动作 | Task 4 的处置：说明"该由外部调用方重跑工具 3"，并说明"我方故障会自动重试"。要补需新增 `POST /api/attachments/{id}/redownload` |
| G-M8-10 | `conflict` 的**第二条产生路径**：解析结果与审批声明交叉核验 | **缺**：规则引擎读 `_party_consistency` / `_contract_type_consistency` 两个保留键，但**没有任何写入方**（M4 遗留） | 交叉核验实际上没有发生；缺它时适用性判 `needs_review`（方向保守，可接受） | 记录在案，**不假装已有**。`conflict` 目前由"人工背书 vs 后续声明不一致"产生（见评审修正 ②）—— 那是一条真实路径，但不等价于交叉核验 |

**"已有但形态与设计文档不同"的三处**（不是缺口，但页面必须按**实际**形态写）：

| 设计 §6.2 的写法 | 实际形态 | 说明 |
| --- | --- | --- |
| `GET /api/tasks/{id}/jobs`、`/results`、`/evaluations` | `GET /api/jobs?task_id=`、`GET /api/results?task_id=`、`GET /api/evaluations?run_id=` | 扁平集合 + 过滤参数。**不要为它加任务专属路径**：两套路径会让"哪一套是权威"变成一个真问题 |
| `POST /api/results/{id}/confirm` "必须携带 `content_digest`；不一致 → 409" | 请求体**为空**，摘要由服务端自己算 | ⚠️ 这是**更安全**的形态：让调用方传摘要，等于让"我确认的是哪份正文"成为一个**可以伪造**的事实。**设计文档该条应改为现状**，而不是要求后端加上传参入口 |
| 模块 1 的 `write_status` | 任务级 `approval_tasks.write_status` + 尝试级 `latest_*`（含 `latest_attempt_rejected`） | 见缺口 G-4 的口径定义。列表取任务级，失败原因取最近一次尝试 |

## Global Constraints

- Follow `2026-09-14-M8-frontend-design.md`; this file converts that approved design into executable tasks.
- The five original modules are mandatory; rule/operations administration is secondary.
- Frontend calls `/api/*`, not `/tools/*`.
- No object key, server path, contract body, form data, or token may enter URLs, console logs, analytics, or error telemetry.
- Backend remains authoritative for permissions, transitions, totals, risk, write status, and confirmation validity.
- Evidence highlights must use page width/height, origin/unit, and rotation; missing geometry degrades visibly to text navigation.
- Keyboard access, focus management, color contrast, loading/empty/error states, and Chinese copy are acceptance requirements.

---

## File Map

- Create `frontend/` Vite application with `src/app`, `src/api`, `src/features`, `src/components`, `src/styles`, and `e2e`.
- Create `frontend/src/api/client.ts`, `contracts.ts`, and per-resource query modules.
- Create routes `/tasks`, `/tasks/:taskId`, `/rules`, `/ops`, `/forbidden`.
- Create `scripts/verify_m8.ps1` and frontend CI commands.
- Modify root `README.md`, `.env.example`, and later M9 Compose files only through environment variables.

### Task 1: Frontend foundation and design system

**Files:**
- Create: `frontend/package.json`, `vite.config.ts`, `tsconfig.json`, `src/main.tsx`, `src/app/router.tsx`, `src/styles/tokens.css`, `src/styles/global.css`
- Test: `frontend/src/app/router.test.tsx`

**Interfaces:**
- Produces route shell, error boundary, authenticated layout, and reusable visual tokens.

- [x] Write a failing router test for the five-module task workflow and forbidden route.
      → `frontend/src/app/router.test.tsx`，**13 passed**。覆盖：`/` 重定向到待办、
      五个视角各自可独立到达（参数化四个 tab）、不带 `?tab=` 默认详情、
      **非法 tab 回落详情**（链接过期不该阻塞流程）、切换视角走查询串而非新增路径段、
      `/rules` 与 `/ops` 可达、`/forbidden` 给出"该找谁"、未知路径渲染 404 而不是白屏。
- [x] Scaffold Vite with strict TypeScript, eslint, formatter, Vitest/jsdom, and deterministic test timezone.
      → `tsconfig.json` 开了 `strict` + `noUncheckedIndexedAccess` + `verbatimModuleSyntax`；
      eslint 9 扁平配置（语法级，不开 type-aware —— 类型正确性只有 `tsc --noEmit` 一个真相来源）；
      prettier；vitest + jsdom；`test.env.TZ='Asia/Shanghai'`
      （后端返回**不带时区**的本地时间串，不固定时区时同一条断言会在 CI 与开发机上给出不同结果）。
- [x] Define a restrained legal-workbench visual language: dense readable typography, neutral surfaces, explicit risk/status tokens, focus rings, and no color-only meaning.
      → `src/styles/tokens.css` + `global.css`：14px 基准 + 1.6 行高（法律文本要逐行读）、
      中性表面三级、风险（high/medium/low）与业务状态各一套语义色、
      `:focus-visible` 焦点环（用 `:focus-visible` 而不是 `:focus`：后者会让鼠标点过的按钮一直亮着，
      "谁有焦点"在视觉上失去意义）。**"不得只靠颜色表达"写进了 tokens 的首段注释**：
      每个语义色都要求配一个同时出现的文字/图标标记，配对由组件保证。
- [x] Add application error boundary and route-level loading states without printing response bodies to console.
      → `src/app/ErrorBoundary.tsx` 与 `RouteLoading.tsx`。
      ⚠️ 边界**只记录错误类型与组件栈首行**，不 `console.error(error)` ——
      后端错误消息里带业务上下文（哪份合同、哪个附件），而它进了浏览器日志就可能
      被截图/上报/第三方脚本读走（材料 §Global Constraints）。
      该条有专属测试：注入一个 `Error(message='CONFIDENTIAL-CONTRACT-BODY')` 的子组件，
      断言我们自己输出的行里**不含**该字符串（只过滤 `[ui]` 开头的行 ——
      React 18 开发模式自己也会打印原始异常，断言它等于在断言 React 的行为）。
- [x] Run `npm run typecheck`, `npm test -- --run`, and `npm run build`; expect exit 0.
      → 三条门禁全绿：`lint` 0 problems、`typecheck` 干净、`test` 13 passed、`build` 91 modules / 205 kB（gzip 66.7 kB）。

**本任务的两处实施记录（都会影响下一个人）：**

1. **`npm install` 必须加 `--ignore-scripts`**（README §2 已写明）：
   `pdfjs-dist` / `react-pdf` 把 `canvas` 列为**可选依赖**，其安装脚本要下载 GitHub
   预编译二进制、失败后回退 `node-gyp` 本地编译（Windows 通常无 C++ 工具链）——
   表现为 `npm install` 挂住数分钟后失败，而控制台在浏览器里**完全不需要 canvas**
   （它只在 Node 侧渲染 PDF 时用）。
2. **`createQueryClient` 单独成文件**（`src/app/queryClient.ts`）：
   与 `AppProviders` 放一起时 `react-refresh/only-export-components` 报 warning，
   而那条规则指向的是真实代价 —— 混着导出函数时改一行 Provider 就整页刷新，
   开发中每改一次丢掉界面状态。测试也要单独造 client（避免上个用例的缓存串进来），
   从组件文件 import 工厂会把整个 Provider 拖进测试依赖图。

### Task 2: Typed HTTP client, identity, and server-state conventions

**Files:**
- Create: `frontend/src/api/client.ts`, `frontend/src/api/contracts.ts`, `frontend/src/api/queryKeys.ts`, `frontend/src/app/AuthProvider.tsx`
- Test: `frontend/src/api/client.test.ts`, `frontend/src/app/AuthProvider.test.tsx`

**Interfaces:**
- Produces `api.get/post/patch<T>()`, `ApiError`, `Actor`, and centralized query keys.

- [x] Generate or hand-maintain exact TypeScript contracts from M7 response schemas; prohibit `any` in API types.
      → **只能手写**：响应体是手搓 dict，**没有 `response_model`**，OpenAPI 里查不到响应字段
      （见上方 Entry Gate 实测的说明）。`frontend/src/api/contracts.ts` 为每个类型标注了
      对应的后端文件；自由形态 JSON 用 `JsonValue`（不是 `any` —— `any` 会让"忘了运行时窄化"
      完全静默，例如把 `evidence[0].page` 直接当数字用）。
      覆盖：分页信封、错误体、身份、任务（列表行/详情/两个回写层级）、作业（含 `result_ref` 的
      分类型语义）、解析、评价四态、结果、日志、审计、重试。
      ⚠️ **规则与标准文档的形状留给 Task 5 / Task 8**：那两个形状（`StandardDocument` 的页/块/字符几何、
      规则配置 JSON）我还没有逐字段核对过，先写等于凭印象定契约 —— 那正是本任务要避免的事。
- [x] Add correlation-ID display/copy support while excluding credentials and bodies from errors.
      → 客户端捕获响应头 `X-Correlation-ID` 并有 `setCorrelationIdListener` 供界面展示/复制；
      `ApiError` 只带 `errorCode` / `message` / `correlationId` / 路径（**不含查询串**）。
      两条专属测试：后端**多返回一个含正文的 debug 字段**时，断言该字段不出现在
      `error.message` 与 `JSON.stringify(error)` 里；网关 HTML 502 页不会被当成我们的错误体。
- [x] Configure TanStack Query: list stale time 10s, detail 30s, job polling 2s while active, no client-side merge of authoritative aggregates.
      → `api/queryKeys.ts`：`STALE_TIME.list/detail`、`POLL_INTERVAL_MS`、`isJobActive()`（**终态不轮询**）
      与全量查询键（`[领域, 视角, 参数…]` 结构决定失效粒度）。默认值仍在 `queryClient.ts` 一处定义，
      测试只覆盖 `retryDelay`（继承其余默认值，不另抄一份）。
- [x] Render role-aware navigation as guidance only; tests must prove direct forbidden requests are still rejected by M7.
      → `AppLayout` 按 `can(permission)` 过滤入口，**但身份未就绪时不过滤**（"还不知道"≠"确定没有权限"，隐藏入口只会让人猜）；
      `AuthProvider.test.tsx` 有一条专门守它：只读审计看不到"规则管理"入口，**手敲 `/rules` 依然渲染页面** ——
      可见性不构成授权，拒绝发生在后端的 `require_permissions`（`test_auth_rbac` / `test_task_queries` 已覆盖逐路由 403）。
- [x] Run focused tests and typecheck.
      → 前端 `lint` 0 problems、`typecheck` 干净、**38 tests passed**（client 14 + router 13 + AuthProvider 11）；
      后端全量 **收集 1348 / 1345 passed / 3 skipped**（基线 1336 + 新增 9）。

**本任务的两处实施记录：**

1. **dev 头身份发不了中文显示名**（实测：httpx 直接抛 `UnicodeEncodeError`）。
   HTTP 头是 latin-1 字节串，非 ASCII 值过去就是 mojibake —— 而审计里的人名坏了是撤不回的。
   处置：非 ASCII 时**不发** `X-Actor-Name`，服务端回落到 `actor_id`；
   宁可界面显示 `li-hua`，也不要往审计账里写一个坏名字（生产用 JWT，姓名在 JSON 声明里没有这个问题）。
2. **`react-refresh/only-export-components` 报的不是噪音**：它指出"文件同时导出组件与函数时热更新失效"。
   按它拆了三个文件：`api/identity.ts`（纯逻辑，可单测）、`app/authContext.ts`（上下文 + `useAuth`）、
   `app/AuthProvider.tsx`（只剩组件）。这比加一条 eslint-disable 更有价值 ——
   拆开之后"非 ASCII 名不发"这条规则可以直接用单元测试钉住，不必渲染组件。

### Task 3: Module 1 — pending task list

**Files:**
- Create: `frontend/src/features/tasks/TaskListPage.tsx`, `TaskFilters.tsx`, `TaskStatusCell.tsx`, `queries.ts`
- Test: `frontend/src/features/tasks/TaskListPage.test.tsx`

**Interfaces:**
- Consumes `GET /api/tasks` server pagination and totals.

- [x] Test loading, empty, error, blocked, done, pagination, filtering, and keyboard row navigation.
      → `TaskListPage.test.tsx` **18 条**（前端总数 38 → **56**）：加载（保留表头）/ 空 / 筛选后空 /
      5xx 三段式 / 阻塞三件套 / 未回写被拒 / 写失败外部故障 / 风险 `—` / 时间格式 /
      翻页 / 首页末页禁用 / 筛选回第 1 页 / URL 白名单 / 上下箭头行导航 / 编号链接可 Tab。
- [x] Display approval code/title/applicant/time/attachment count, business status, task-level write status, and stable blocked reason.
      → 表格 8 列全在；阻塞行**内联**给出"卡在哪一步 + 错误码 + 原因"（不藏 tooltip ——
      只看"阻塞"两个字的人不会点进去，而他不点就不知道该找谁）。
- [x] Do not calculate totals or merge pages in the browser.
      → 卡片与页码**只显示后端给的数字**。有一条用例专门用"总数 45 / 本页 1 行"把它钉死 ——
      前端数行会得到 1，而这种缺陷在数据少于一页时**永远不显形**。
- [x] Preserve filters in safe query parameters only; never serialize form data or contract content.
      → 查询串只认 `status`（五值白名单）与 `page`（纯数字 ≥ 1），非法取值回落默认；
      有一条用例直接构造 `?status=bogus&page=-3&form_data=SECRET-CONTRACT-TEXT`，
      断言转发给后端的 URL 里**不出现**这三个东西。
- [x] Run focused tests and a production build.
      → `lint` 0 problems、`typecheck` 干净、**56 tests**、`build` 102 modules / 233 kB（gzip 75.7 kB）；
      后端全量 **1385 passed / 3 skipped**。

**本任务为满足设计而补的后端接口（第 3 节起，全部是"前端算不了"的东西）：**

| 缺口 | 补的内容 | 关键决定 |
| --- | --- | --- |
| G-M8-1 | `GET /api/tasks/summary` | 两条 SQL（`GROUP BY` + 一次计数），不是每个状态一次；`by_status` 五个键**恒定齐全**（缺键时前端渲染 `undefined`，而"0"与"没有键"在界面上分不开） |
| G-M8-3 | 列表与详情加 `overall_risk_level` | 批量 `row_number()` 一次取回整页；`null` 表示"还没审过"，**不是** `low` |
| （新） | 列表行加 `writeback`（与详情**同结构**） | 设计 §5.1 要求"状态与原因分两处"，而列表是用户第一眼看到的地方 —— 只给状态会让人去重试一个注定被拒的请求 |
| （新） | 列表行加 `attachment_count` | 设计 §4.1 的"附件"列 |
| （新） | `approval_tasks.context_conflict_json` 列 | 存**冲突的两个来源**（`declared` / `confirmed`）—— 只说"冲突了"时人只能猜；这条列同时让设计 §4.2 的"显示冲突双方"可实现 |
| （修） | `pull_service._apply_context` 重写 | 人工背书过的立场与本次声明冲突时**不再覆盖**，改判 `conflict`（详见下方评审修正 ③） |

⚠️ **这四项都做成了"一处判据"**：新增
`current_results` / `latest_writeback_attempts` / `attachment_counts` 三个批量函数，
并让 `task_chain` 与 `writeback_summary` **改走它们**。
各写一遍时，分叉方式是某天只改了列表那一侧 —— 于是同一条任务在列表与详情上
显示不同的风险等级 / 不同的失败原因 / 不同的附件数，而两处都"看起来对"。
`test_list_and_detail_writeback_shapes_match` 与
`test_attachment_count_agrees_between_list_and_detail` 就是守这个的。

**⚠️ 一处设计文档与实际接口的冲突（不阻塞，但必须记下来）**

设计 §4.1 的空态写"可点「拉取待办」从审批系统同步"。**没有这个 `/api` 端点**：
触发一次拉取目前只有 `/tools/list_pending_contract_approvals`，而它是给**外部系统**的
（本项目硬约束第一条就是"前端只调 `/api/*`"）。放一个点了没反应的按钮比不放更糟，
因此空态改为说明"同步由定时任务或外部调用方触发"，并有一条测试断言
**页面上没有"拉取"按钮**。要真正提供这个入口，需要新增一个
`POST /api/tasks/pull`（会写审计、消耗外部调用配额），属于 M8 之外的决定。

**本任务抓到的一个测试自身的缺陷（值得记住）**

`AuthProvider.test.tsx` 里的 fetch 替身原本**对所有 URL 都返回身份对象**。
模块 1 落地后，`/tasks` 页面开始自己请求 `/api/tasks` —— 于是列表拿到了一份身份对象
（`items` 是 `undefined`），页面抛错、错误边界接管，**7 条身份用例一起变红**。

这条缺陷的性质不是"写错了桩"，而是：**"什么都能回"的替身会随着被测系统长大而悄悄变成错的**，
而且它红得很有误导性（看起来像身份模块坏了）。处置是把替身改成**按路径分派**，
并且分派时**最长路径优先**（`/api/tasks/summary` 也以 `/api/tasks` 开头，
顺序反了只会表现为"卡片数字不对"）。

> 与 `react-refresh` 那条同一个模式：**工具给的警告指向的都是真实代价**。
> 这一轮按它拆了三个文件（`domain/time.ts`、`features/tasks/listParams.ts`、
> 以及 Task 2 的 `api/identity.ts`），好处是这些纯函数可以**直接单测**，
> 不必渲染组件。

### Task 4: Module 2 — task detail and authoritative context

**Files:**
- Create: `frontend/src/features/workbench/TaskWorkbench.tsx`, `frontend/src/features/detail/DetailTab.tsx`, `ContextConfirmationForm.tsx`, `AttachmentList.tsx`
- Test: `frontend/src/features/detail/DetailTab.test.tsx`

**Interfaces:**
- Consumes task detail, attachment metadata, and context-confirm endpoints.

- [x] Test four context states separately: complete, missing, conflict, confirmed.
      → `DetailTab.test.tsx` **18 条**（前端总数 56 → **74**）：四态各自的文案与动作、
      **冲突对照表**（两侧 + 不一致标记 + 无冲突时不渲染）、
      `missing` / `conflict` **表单直接展开**（它们没有可确认的对象，唯一出路是给四值）、
      提交带上四条、无权限时不显示动作、后端 403 的三段式、掩码与显示、URL 不含表单值、
      附件成功/失败行、区块级失败、详情 5xx。
- [x] Render form fields with masking rules and attachment actions without exposing storage identifiers.
      → `domain/masking.ts`：按**键名**判定敏感（中文/英文键名都覆盖），默认掩码、
      点「显示」展开；保留首尾各两位（**全掩成 `••••` 时用户无法回答"这是不是我以为的那一条"，
      于是只能点开 —— 点开就失去了掩码的意义**）。
      附件行只显示白名单字段 + **内容地址来自后端的 `content_url`**（前端不拼路径）；
      后端一条测试断言响应里既没有 `object_key` 也没有 `file_path`、且响应全文不含它们的值。
- [ ] **Branch attachment-content failures on the backend `error_code`, never on HTTP status alone.**
      `GET /api/attachments/{id}/content` returns **404 for two different situations**,
      and their recovery actions differ:

      | `error_code` | Meaning | UI must say | User action |
      | --- | --- | --- | --- |
      | `RESOURCE_NOT_FOUND` | row absent *or* not visible to this tenant | "资源不存在或无授权访问" | none — do not offer a retry |
      | `OBJECT_NOT_FOUND` | row exists, bytes never stored or lost | "附件内容尚未入库或已丢失" | **重新执行附件下载**（工具 3） |

      Deciding on `404` alone makes "id is wrong" and "not downloaded yet" look identical,
      so the only available affordance is a retry button — which for the first case can
      never succeed. The backend already emits distinct machine codes
      (`app/enums.py::ErrorCode`); the console must read them.
      ⚠️ Never surface `object_key`, server filesystem paths, or MinIO internal
      addresses in the UI, in error text, or in logs.
- [x] Require explicit user confirmation before context mutation; show backend reason on 409.
      → 变更是**显式提交**（`提交并确认` / `确认该立场`），没有任何隐式写操作；
      后端 4xx/409 一律走 `describeApiError` 的三段式（409 → "当前状态不允许该操作" +
      "按提示先完成前置动作再试"），而不是一句"操作失败"。
- [x] Invalidate only the affected task queries after success.
      → 只失效 `['tasks','detail',id]` 与 `['tasks','list']`（列表行上有 `context_status`）。
      ⚠️ **刻意不用** `['tasks']` 整棵：那会把附件与解析版本一起失效，
      而它们与立场确认无关 —— 多失效的每个键都是一次没人需要的请求。
      （`queryKeys.ts` 里写明了这条，避免后来者"顺手改成整棵更保险"。）
- [x] Run focused tests and accessibility checks.
      → `lint` 0 problems、`typecheck` 干净、**74 tests**、`build` 249.2 kB（gzip 79.9 kB）。
      可访问性用**语义断言**覆盖：区块是 `region` + 可访问名、表单是 `form` + 名字、
      掩码按钮的 `aria-label` 是「显示 联系人手机」、风险为空时的 `aria-label` 说明原因。

---

## 评审修正（2026-09-15）：三处问题与处置

外部评审（并行会话）针对 Task 4 的"立场修正"提了三条，**三条都成立**，
其中第三条是**行为缺陷**而不是文档问题。

### ③ 人工修正会被下一次同步静默覆盖，而门禁仍然放行（已修，缺陷级）

实测证据链（修复前）：

| 步骤 | `context_status` | `context_source` | 四字段 | 回写门禁 |
| --- | --- | --- | --- | --- |
| 同步后 | `complete` | `approval_system` | 系统值 | 放行 |
| 人工修正后 | `confirmed` | `manual` | 人工值 | 放行 |
| **再次同步后** | **`complete`** | **`approval_system`** | **被覆盖回系统值** | **仍然放行** |

成因在 `pull_service._apply_context`：它**无条件**写入本次声明，
只在"取值未变"时保留 `confirmed`。旧口径在**人工只能确认、不能改值**时是安全的
（`confirmed` 只可能是系统取值的镜像）；有了"人工修正"之后，
"人工改过 → 取值必变 → 重置为 `complete`"成了必然路径 ——
而 `complete` **也在** `_TRUSTED_CONTEXT` 里，于是回写照常，
用的是**被否决的立场**。

危害不是"状态显示不对"，而是**规则方向反转**：`our_party_business_role` 从
`seller` 被改回 `buyer` 后，方向敏感的规则该判的不判、不该判的误命中，
而报告上看不出任何异常。

**修法**（`_apply_context` 重写 + 新列 `approval_tasks.context_conflict_json`）：

| 库里状态 | 本次声明 | 结果 |
| --- | --- | --- |
| 未人工确认 | 四项齐全 / 缺项 | `complete` / `missing`（原行为） |
| `confirmed` | 与库里**相同** | 保持 `confirmed`（清掉冲突记录） |
| `confirmed` | 有值但**不同** | **`conflict`**：保留人工值、记下声明值、**不覆盖** |
| `confirmed` | 本次没给全 | 保持 `confirmed`（"没拿到" ≠ "拿到了别的"） |

三点附带收益：

1. `conflict` **不在** `_TRUSTED_CONTEXT` 里 → 回写被拒（`CONTEXT_NOT_VALID`），
   直到有人再看一眼。这是"停下来等人裁定"的机器表达。
2. 两个来源的值都存进 `context_conflict_json`，并经
   `GET /api/tasks/{id}` 的 `context_conflict` 下发 → 设计 §4.2 的
   "**显示冲突双方**"从"无数据可显示"变成可实现（前端已落地对照表）。
3. "人工背书过"的判据只用 `context_status`，**不用 `context_source`**：
   旧实现在"保留 `confirmed`"**之前**就把它改成了 `approval_system`，
   按 source 判断会让历史数据再被覆盖一次。

测试：`tests/test_pull_service.py` 里那条**断言旧行为**的用例
（`test_changed_context_invalidates_confirmation`，断言"变了 → `complete`"）
已改写为四条新用例（冲突、两侧对照、重新一致后清空、声明不全不影响）；
`test_writeback_service.py` 的参数化里原本就有 `conflict` → `CONTEXT_NOT_VALID`。

### ② `conflict` 分支不可达（已随 ③ 修复）

评审的核查是对的：全仓库只有测试手工 seed 过 `conflict`，生产代码里没有任何写出方
（`_apply_context` 的注释还写着"M4 才有"）。

修 ③ 的同时给出了**一条真实的产生路径**：人工背书过的立场与后续同步声明不一致。
另一条（**解析结果**与审批声明交叉核验）**仍然不通**：规则引擎侧读的
`_party_consistency` / `_contract_type_consistency` 保留键**有读无写** ——
这是 M4 遗留的口子，允许保留（缺它时适用性判 `needs_review`，方向偏保守），
但不该被当成"已经有交叉核验"。

### ① 两处文档自相矛盾（已修）

`query_service.confirm_context` 的 docstring 与 `app/api/tasks.py` 的路由描述尾部
仍写着"只有 `complete` 状态可确认"，与新增的"带请求体即可修正"并列时自相矛盾；
第二处**会进 OpenAPI**，前端照着它写就会错。

处置：两处合并成同一张表（两条路 + 各自允许的状态），并补上
"`conflict` 是怎么产生的"；`_apply_context` 的 docstring 同步重写。

---

**⚠️ 一条**未**在本任务完成的要求（有意留给 Task 5）**

本条要求的"**按 `error_code` 分支附件内容失败**"（`RESOURCE_NOT_FOUND` = 核对 id、
`OBJECT_NOT_FOUND` = 去重跑工具 3）在本任务**没有落地**：本任务的预览是一个
指向 `content_url` 的链接，失败发生在**浏览器新标签页**里，页面拿不到那个错误码。
真正能分支的地方是 Task 5 的**应用内查看器**（它自己发请求、自己拿错误码），
因此这条并到 Task 5 一起做 —— 在那里"两个 404"能各自给出正确的下一步。

### Task 5: Module 3 — parsing and PDF evidence navigation

**Files:**
- Create: `frontend/src/features/parse/ParseTab.tsx`, `frontend/src/features/pdf/PdfViewer.tsx`, `EvidenceOverlay.tsx`, `coordinateTransform.ts`
- Test: `frontend/src/features/pdf/coordinateTransform.test.ts`, `frontend/src/features/parse/ParseTab.test.tsx`

**Interfaces:**
- Consumes authorized attachment bytes with Range and standard-document geometry.
- Produces `toViewportRect(evidence, pageMeta, viewport) -> Rect` as the single coordinate transform.

- [x] Write numeric tests for top-left origin, scale, 0/90/180/270 rotation, clipping, char precision, and line precision.
      → `coordinateTransform.test.ts` **18 条**，全部手算数字（页 600×800、视口 600×800 / 1200×1600）。
      ⚠️ **"0/90/180/270 旋转"这条测试的形态与计划设想的不同**，见下方实施记录 ①：
      后端已经把 `bbox` 换算到旋转后可见页的空间，**前端不得再旋转一次** ——
      因此那四个角度的用例断言的是"**同一个 bbox 在四种 rotation 下得到完全相同的结果**"，
      并附一个"再多转一次会得到什么"的反例。
- [x] Render field/clause status, evidence excerpt, page, and precision; selecting a field scrolls and highlights the PDF, selecting a highlight selects the field.
      → `ParseTab.test.tsx` **20 条**。四态各自的标记 + 文案（且断言**挂在正确的字段行上**，
      不是"页面上出现过这四个词"）；证据给出页码 + 文本精度 + 几何精度 + 原文片段；
      点字段 → 选中并高亮（`aria-pressed` + 框的 `data-selected`）、点「定位」→ 翻到证据页；
      点 PDF 上的框 → 选中字段（**双向**，且框是 `<button>`，键盘与读屏同样可用）。
- [x] Degrade a page without coordinates to text-level navigation and label the loss of precision; never fabricate a box.
      → 没有块 → 自动降级为文本定位并明确标注"**画不出证据框**"，
      原文里的引用片段用 `<mark>` 标出；`evidenceRects` 在四种情形下都返回空
      （无坐标 / 块不在本文档 / 完全出界 / 精度为 `none`），界面**如实说**而不是就近画一个。
- [x] Virtualize page rendering and release PDF resources when leaving the route.
      → **一次只渲染一页**（上一页/下一页），DOM 里的页数恒为 1 ——
      这满足了"有界 DOM"，但**不是**"虚拟化长列表"：证据定位是"跳到某一页"，
      而不是"滚动浏览 100 页"，因此不需要窗口化。字节持有在组件状态里，卸载即释放；
      pdf.js 的文档对象由 `react-pdf` 在 `<Document>` 卸载时销毁。
- [ ] Add Playwright screenshots for normal, rotated, and OCR-line evidence.
      → **未做，移到 Task 9**：Playwright 与浏览器二进制在 Task 9（e2e 套件）里才会引入，
      现在装它等于为一个截图用例拖进整套浏览器依赖。Task 9 的清单里已有
      `frontend/e2e/*.spec.ts` 三项，届时一并补这三种证据的截图。

**本任务的实施记录（四条，都会影响下一个人）：**

**① 前端**不做**旋转（计划里那条"旋转测试"差点把我引到坑里）**

`app/ports/parse_document.py` 写明：`DocumentPage.width/height` 对应**旋转后可见页面**，
且 `bbox` 一律**已换算到该空间**。因此 `toViewportRect` 只做缩放。
旋转**只发生在一个地方** —— 构造 PDF 渲染视口时用**文档给的** `rotation`
（`pageRotationForRender`），而不是 PDF 自己的 `/Rotate`。

这个错误的可怕之处是它**几乎总是看不出来**：`rotation = 0` 的文档（绝大多数）结果完全相同，
只有扫描件（90°/270°）会整体错位与宽高互换，而页面看起来正常。
测试里因此同时放了正例（四种角度结果相同）与反例（重复旋转会得到什么）。

**② 字符级精度按"行"合并，而不是逐字画框**

初版按字符逐个画框（`charRectsForRange` 直接返回），结果是**一排相邻小框** ——
视觉上是虚线，而它想表达的只是"这一段"。现在 `evidenceRects` 先按**纵向重叠**分行，
**每行一个并集框**：同一行的证据是一个框，跨行证据是每行一个
（单框并集会盖住两行之间的空白与无关文字，看起来像"证据是这一整块"）。

**③ 一个被测试抓到的**假话**：加载中被渲染成"字段结构读不出来"**

`readable = parse.data !== undefined && …` 在请求还没回来时是 `false`，
于是左栏先渲染"这份解析记录的字段结构不是当前版本能读的（可能来自旧 schema）" ——
**一句假话**，而且它给出的处置（重跑解析）与真实原因（等一下）无关。
已改成四段顺序：**加载中 → 失败 → 结构不可读 → 正常**。
（这是"用 `undefined` 兼任'没有'与'还没到'"的典型代价。）

**④ pdf.js 挪出主包（`AppRoutes` 里预告过的唯一例外）**

静态 import 之后主包从 **249 kB 涨到 643 kB**（gzip 199 kB），而
`react-pdf` / `pdfjs-dist` **只有这一页用得上** —— 每个打开待办列表的人都在下载 PDF 引擎。
改成 `lazy(() => import('../pdf/PdfPageCanvas'))` 之后实测：

| 产物 | 体积 |
| --- | --- |
| 主包 `index-*.js` | 269.8 kB（gzip 86.7 kB）—— 回到原量级 |
| 查看器 `PdfPageCanvas-*.js` | 373.1 kB（gzip 111.1 kB）—— **只在能画框时才取** |
| `pdf.worker.min.mjs` | 1.38 MB（由 pdf.js 按需加载） |

**⑤ `PdfPageCanvas` 没有单元测试 —— 这是如实的"没测到"**

它把字节交给 pdf.js 在 `<canvas>` 上渲染，而 jsdom **没有 canvas 实现**
（`canvas` 包在安装时被 `--ignore-scripts` 跳过，浏览器端也不需要它）。
与其写一个"渲染了个空 canvas 也算通过"的假测试，不如把**能测的部分**
（几何、证据框、四态、降级、失败分支）全部抽成纯函数测到位
（`coordinateTransform` 18 条 + `ParseTab` 20 条），
把这条**测不到**的边界写进文件头。Task 9 的 Playwright 截图是它真正的覆盖手段。

**⑥ Task 4 欠下的那条要求在这里落地了**

"按 `error_code` 分支附件内容失败"（`RESOURCE_NOT_FOUND` vs `OBJECT_NOT_FOUND`）
在 Task 4 做不到（预览是浏览器新标签页里的链接，页面拿不到错误码）。
本任务的查看器**自己取字节**（`api.getBytes` —— 与 JSON 路径**共用同一套错误映射**，
否则同一条后端错误在两条路径上会得到不同的 `ApiError`），
于是两个 404 各自给出正确的下一步，并且分支逻辑抽成纯函数
（`failures.ts`）**可直接单测**。

### Task 6: Module 4 — rule evaluations

**Files:**
- Create: `frontend/src/features/rules/RuleTab.tsx`, `EvaluationCard.tsx`, `RuleFilters.tsx`
- Test: `frontend/src/features/rules/RuleTab.test.tsx`

**Interfaces:**
- Consumes M7 evaluation views and evidence arrays.

- [x] Test all four states and stable reason codes.
      → `RuleTab.test.tsx` **18 条**：四组齐全且顺序固定、折叠态计数可见、
      稳定原因码（`CONTEXT_MISSING` 这类）逐条断言。
- [x] Show `hit` and `needs_review` expanded; keep `not_hit` and `not_applicable` available but collapsed.
      → `<details open>` 四态分组，前两组默认展开、后两组折叠但**组头带计数**；
      空组的说明分三种（筛选掉了 / 真的没有这一类 / 批次根本没评价），
      因为它们的下一步完全不同。
- [x] Display rule/version/risk/reason/suggestion/calculation/evidence without converting `needs_review` into risk.
      → **`needs_review` 不给风险徽章**（测试逐卡片断言"这个框里没有风险等级"）：
      每行都带 `risk_level`，但那是**规则配置的等级**，`needs_review` 的含义恰恰是"判不了"——
      配一个红色的「高」会让人把**待判断**读成**已确认的高风险**。
      计算过程来自 `hit_detail`（`actual` / `op` / `threshold` → `0.6 > 0.3`），
      未登记的键**原样列出**（不丢键），没有比较时不编一句"计算过程：无"。
- [x] Link each evidence item to the PDF viewer and preserve task context.
      → `routes.evidenceDeepLink(taskId, {page, blockId})` → `/tasks/{id}?tab=parse&page=2&block=p2-b1`；
      模块 3 侧 `readEvidenceDeepLink` 落在那一页，并**能反查到引用该块的字段**（`ParseTab` 的深链 effect）。
      块不属于任何字段时只停在那一页 —— 不硬塞一个"莫名其妙选中的字段"。
      URL 里**只有定位符**，原文片段与结论文字一律不进（材料 §Global Constraints）。
- [x] Run focused tests and accessibility checks.
      → 四条门禁全绿（lint 0 / typecheck 干净 / **141 tests** / build 281.2 kB）；
      "查看更多原文" 是 `<Link>`、分组是原生 `<details>`，键盘与读屏可直接用。

**本任务的实施记录（四条）：**

**① 数据源用 `/api/runs/{id}`，不用 `/api/evaluations`（这一条决定了页面会不会撒谎）**

| 维度 | `/api/runs/{id}` | `/api/evaluations` |
| --- | --- | --- |
| 计数 | **后端现算聚合**（四态齐全、与列表同源） | 无 |
| 评价 | **整批返回** | 分页 |
| 风险/完整性 | 有 | 无 |

用分页接口渲染"四态 + 计数"时，数字来自**已加载的那一页**，而界面写着"不适用 31" ——
数据少于一页时两者**永远相等**，于是这个缺陷会一直活到数据量上来（M8 验收 4）。
另加一条一致性检查：聚合与条目数不等时给出显式警示（真实数据里两者相等，
**一旦不等就该被看见**，而不是悄悄显示两套数字）。

**② 同一份"一条评价"有两个形状（后端现状，前端只能照实建模）**

| 字段 | `/api/evaluations` | `/api/runs/{id}.evaluations[]` |
| --- | --- | --- |
| 7 个基础字段 | ✓ | ✓ |
| `evaluation_id` / `run_id` / `task_id` / `rule_version` / `created_at` | ✓ | **没有** |

合成一个类型再到处断言 `!`，会在模块 4 里造出四个恒为 `undefined` 的字段
（界面显示"版本 undefined"时没有人知道是接口没给还是解析错了）。
另外**规则中文名拿不到**：`/api/rules` 需要 `rule:manage`，审查人没有这个权限 ——
因此界面显示 `rule_code`。把它记在这里，免得下一个人"顺手加个请求去取规则名"。

**③ 证据形状不同：解析字段是**平铺**，规则评价是**嵌套****

`{text, position: {page, block_id, bbox, …}}` vs `{page, block_id, bbox, …, text}`。
因此**不能复用** `fields.ts` 的读取函数（复用时 `span.page` 为 `undefined`，
表现为"证据定位没反应"，看不出是形状问题）。`ruleEvidence.readRuleEvidence`
把它规整成同一个 `EvidenceSpan`，下游只有一套几何类型。

**④ 空组说明去重**

初版四个空组各说一遍"这不等于没有风险"，测试立刻暴露出**同一句话出现四次** ——
它会被读成四个各自独立的问题，而事实是同一个。现在这条只在页面顶部说一次。

### Task 7: Module 5 — result, confirmation, and writeback

**Files:**
- Create: `frontend/src/features/result/ResultTab.tsx`, `CommentEditor.tsx`, `ConfirmationPanel.tsx`, `WritebackTimeline.tsx`
- Test: `frontend/src/features/result/ResultTab.test.tsx`

**Interfaces:**
- Consumes backend aggregate/result/confirmation/writeback views and mutation endpoints.

- [x] Test low/medium/high and complete/needs-review combinations without recomputing policy.
      → `ResultTab.test.tsx` **23 条**。夹具里故意放"**高风险 + 完整性「完整」**"这种
      自相矛盾的组合，界面必须**照实显示各自的值** —— 前端按四态计数重算时
      会把它理顺成一个"看起来对"的结论，而那与报告、与回写门禁的依据不是同一个东西。
- [x] Save edited comment as a new backend result version; after edit, display backend `confirmation_valid=false` immediately.
      → **薄出口已落地**：`POST /api/results/{id}/comment`（见"决策 ①"），
      `tests/test_result_comment_api.py` **7 passed**（2 条服务层 + 5 条路由级）。
      前端保存后**重取结果列表**拿后端算出的 `confirmation_valid`，
      而不是本地置一个"已失效"标记。
- [x] Require explicit confirmation UI where backend policy requires it; button disabling is explanatory, not authorization.
      → `ConfirmationPanel` 的四态解释（有效 / 正文已变更 / 已被接替 / 尚未确认）；
      按钮可点性**只解释**（"点了也不会改变什么"），后端独立判定 ——
      测试里连"确认按钮在有效与已被接替时都不可点"都断言了。
- [x] Poll the returned job URL and render attempt number, task-level status, attempt-level reason, and final provider result.
      → `GET /api/writebacks/{id}`：任务级 / 尝试级 / **投递级**三层分开呈现
      （`第 2 / 5 次投递 · 等待派发`）；轮询判据取自数据本身
      （`isWritebackInFlight`），终态与门禁拒绝都**立刻停**。
- [x] Prove a hand-crafted forbidden writeback request is rejected by the backend even if the button is enabled in devtools.
      → 本轮**没有**在控制台里放"发起回写"的按钮：回写是工具 7（`/tools/*`），
      控制台只调 `/api/*`。因此这条的形态变成**界面不给按钮**（并写明由谁触发），
      而"手工构造请求会被后端拒绝"由 M6 既有的用例守着
      （`test_m6_api.py` 的三类门禁拒绝 + `writeback_service` 的幂等）。
      这比"放一个注定被拒的按钮再证明它被拒"更诚实地反映了权限边界。

**Task 7 实施记录（六条）：**

**① 薄出口落地（`POST /api/results/{id}/comment`）**

按"决策 ①"实现：读当前结果 → 取它自己的 `run_id` / `overall_risk_level` /
`summary_text` / `focus_points` → 调**工具 6 用的同一个门面函数**。
请求体只有 `comment_text`（`RuleAdminRequest` 基类：`extra=forbid` +
`str_strip_whitespace`，两者配合才拦得住 `"   "`）。

5 条路由级用例里有一条是这条决策的**守门人**：
`test_comment_endpoint_shape_is_identical_to_tool_six` 断言两个入口的返回
**键集合相同** —— 只断言"两条都能用"时，第二份实现照样能通过。

**② `POST /api/results/{id}/confirm` 确认**不带请求体**（照实现做，已记入计划）**

后端刻意不给"传摘要的入口"（确认因此不可伪造）。前端不传体、
**也不做 409 分支**（那个场景不存在），只渲染后端给的 `confirmation_valid`。

**③ 版本切换抓到的一个**真缺陷**：编辑器的草稿不随版本切换**

`CommentEditor` 把草稿存在 `useState` 的**初值**里，而切版本只换 `result` 这个 prop
—— React 不重置 state。后果不是"显示不对"，而是**把上一版的正文保存进另一版**：
框里有字、保存成功、版本号也涨了，因此**在界面上完全看不出来**。
修法是 `key={row.result_id}`（强制重挂载）。抓到它的是"切版本后编辑器里应当是
v1 的正文"这条断言 —— 没有它，这个缺陷会一直活到有人拿两个版本对照。

**④ 被禁用的查询**永远是 `isPending`**（v5）**

`useWriteback(null)`（没有回写尝试）时 `isPending` 恒为 `true`，
照搬它会让"还没有回写尝试"永久显示成"正在加载回写尝试…"（转不完的圈）。
判据因此是 `attemptId !== null && attempt.isPending`。

**⑤ 类型化夹具抓出三个凭记忆写错的字段名**

`taskDetail()` 刻意标注为 `TaskDetail`（而不是宽松的 `Record<string, JsonValue>`），
于是 `context_correction` / `blocked_reason_code` / `is_business_blocked`
在 `tsc` 阶段就被拒。顺手核了后端：`last_error_is_business_fact` 才是真字段名
—— **契约本身是对的**，错的是我凭记忆写的夹具。
（代价是末尾一次断言：`Partial` 展开后 TS 无法证明"每个键都还在"。）

**⑥ 轮询判据两处共用一个函数**

`refetchInterval` 与界面上的"正在等待派发"都取 `isWritebackInFlight`。
各写一份时会出现"提示一直亮着而请求早已停止"（或反过来）——
而两者都是**看不出来**的那类不一致。

**Task 7 的前置修正（实施前核出来的两处，都会改变做法）：**

**① "保存新版正文"缺的不是能力，是 `/api/` 出口 —— 决策：加**薄出口**，不重写**

`POST /api/results/{result_id}/comment`，请求体 `{ comment_text }`：

```text
读当前结果 → 取它自己的 run_id / overall_risk_level / summary_text / focus_points
          → result_service.save_review_result(...)   ← 与工具 6 **同一个函数**
返回:       与工具 6 同形（result_id / version_no / content_digest / result_url）
```

初版把这条写成"**新增接口：生成新版本 `version_no+1`、`supersedes_result_id`、
重算 `content_digest`、写审计事件**"。那个写法把已有能力描述成新接口要做的事，
最可能的产出是**第二份实现** —— 而"REST 与 MCP 必须共用同一套应用服务"
是这个仓库的架构底线，两份实现的分叉方式是"某天只改了其中一处"，
没有一处会报错。

**代价已用探针验证**（`tests/test_result_comment_api.py`，**2 passed**）：

| 断言 | 结果 |
| --- | --- |
| 改正文 → `version_no == 2`、`supersedes_result_id == 旧 id`、`reused is False` | ✓ |
| 摘要 / 关注点 / 风险等级**照抄当前结果**（编辑正文不顺带改结论） | ✓ |
| **旧版本 `confirmation_valid` 自动变 `False`**，且 `confirmed_by` 痕迹仍在 | ✓ |
| 同一份正文存两次 → 复用同一版本（双击安全） | ✓ |

探针刻意打在**服务层**（照出口将要传的参数），原因有二：出口要落在
`app/api/results.py`，而**另一会话正在同一批文件上工作**（直接动它会丢掉那边的编辑）；
且真正影响决策的问题只是"复用够不够"，而它由**行为**回答，不由签名像不像回答。
出口落地后要补的路由级用例只有三行，**断言一个字都不用改**（形态写在探针文件末尾）。

**四条硬约束**（不写下来就会在别处丢）：

1. **只允许改 `comment_text`**。风险等级 / 摘要 / 关注点不由这个出口改 ——
   工具 6 要求 `overall_risk_level` 与批次聚合一致，让它可传就等于重开一个
   被 `RESULT_INPUT_MISMATCH` 关掉的口子；
2. **必须复用 `save_review_result`**（判据就是探针通过）；
3. **等级与 `run_id` 取自当前结果**：它们在该结果保存时已被校验过（不是猜的）；
   重新聚合反而会让"编辑正文"顺带改了风险等级；
4. **确认失效不需要写任何新代码**：`confirmation_valid` 已含"仍是当前版本"这一条
   （`result_service.get_result_view`），新版本一落库旧版本的确认自动失效 ——
   验收 9 的"**由后端判定**"因此天然成立，不需要前端联动。

**为什么不选"控制台正文只读"**：设计 §4.5 明写"回写正文（**可编辑**，编辑后需重新确认）"，
验收 9 的前半句就是"正文编辑后确认立即失效且由后端判定"。选只读等于**作废一条已验收的设计要求** ——
而那必须去把设计文档里那一段**改掉**，不能"以后再说"：留着它就是一条永远红的验收项，
下一轮还会有人重新提出这个岔路，然后再把今天核过的这些事实重核一遍。
"不撒谎"这个理由其实站在薄出口这一边：只读在界面上是诚实的，但它让文档继续声称一个不存在的能力。

**② `POST /api/results/{id}/confirm` **不接受请求体**（设计 §6.2 的"必须携带 `content_digest`；不一致 → 409"未实现）**

核后端：该路由**没有** body 参数，docstring 写明"调用方**没有传摘要的入口**：
'确认了哪份正文'因此不是可以伪造的事实"。

这个做法**更好**，照它做（前端确认按钮不传体）。因此计划里"不一致 → 409"的场景
**不存在**，不要为它写界面分支 —— 但"编辑后确认失效"仍然要显示，
依据是后端给的 `confirmation_valid`（而不是前端自己比摘要）。

### Task 8: Rule and operations administration

**Files:**
- Create: `frontend/src/features/ruleAdmin/RuleAdminPage.tsx`, `frontend/src/features/ops/OpsPage.tsx`
- Test: corresponding `*.test.tsx`

**Interfaces:**
- Consumes M7 versioned rule, retry, job, log, and audit endpoints.

- [x] Implement searchable rule list, validated editor, version/publish confirmation, and impact display only when the backend provides it.
      → `RuleAdminPage.test.tsx` **15 条**。列表按**执行顺序**（后端 `priority ASC`）显示；
      筛选参数进请求（后端白名单校验）；搜索作用于已加载条目并**说明**这一点。
      "影响显示"只显示后端给的东西：激活前校验报告（total/active/inactive/规则集版本）
      + 11 类覆盖的**提前提示**（"缺哪几类"的最终结论仍是激活前校验的 400 消息，
      界面不维护第二份判据）。
- [x] Implement job/log/audit views and checkpoint-aware retry with required operator reason.
      → `OpsPage.test.tsx` **12 条**。作业/日志/审计三层分开（三种权限）；
      重试面板列出**可恢复的检查点**（4 个）并明确"拉取/详情/下载没有可重跑的作业"；
      `RETRY_NOT_SUPPORTED` 翻译成"去重跑哪个工具"，而不是"请再试一次"。
- [x] Hide mutations for read-only roles and prove the backend still returns 403 on direct calls.
      → 权限三态（`true`/`false`/`null`）：无权限给**解释**（"这不是安全边界，后端独立校验"）；
      未就绪时**不发**注定 403 的请求（否则权限问题被伪装成服务不稳定）；
      "后端仍拒"由既有用例守着（`test_auth_rbac.py` 的逐权限断言 + `test_rule_admin_api`/`test_retry_api`）。
- [x] Do not allow arbitrary task-state editing.
      → 页面上**没有**这个入口，并写明理由：状态只能由流程与检查点重试改变 ——
      "把 blocked 手改成 reviewing"看起来像恢复了，实际什么都没跑。

**Task 8 实施记录（四条）：**

**① 测试抓到两个**查询键漏筛**（都会静默显示错误数据）**

`jobStatus` 与 `includeSystem` 不在查询键里时，改筛选/勾选命中**同一个缓存**：
界面显示的是上一个筛选的结果，没有任何报错。
`queryKeys.jobs/logs/audit` 因此把全部筛选收进键 —— 这正是 Task 2 写的
"键写错时的症状是'失效没生效'：没有任何报错"的另一种形态（连失效都不需要）。

**② 同一前缀、两种方法（`POST /api/rules` 新建 vs `GET /api/rules` 列表）**

`stubApi` 原来只按前缀分派：给"新建失败"搭的桩把列表请求一起打挂，
症状是"页面打不开"，完全不像搭桩问题。处理器现在收到本次请求
（url/method/body），按方法区分 —— 顺带让测试能断言**请求体**
（PATCH 只带改过的字段、确认不带体，都断言到了这一级）。

**③ 版本化的后果在保存前说清（但不替后端判定）**

改判定语义而不升版本 → 可能被 409 `RULE_VERSION_IN_USE` 拒
（该版本已被审查引用时）。界面在保存前提示"请把版本加 1"，
但**不判定会不会被拒** —— `rule_hits` 里有没有同版本评价只有后端知道；
把它写成"必定失败"会逼人白白升版本（而升版本本身有代价）。
`CONTENT_FIELDS` 照抄后端（= 可编辑字段 − `rule_status`/`rule_version`），
抄漏一个的表现是"界面不提版本 → 保存 409 → 不知道为什么"。

**④ 权限页面的加载态会"吃掉"同步断言**

区块在**身份就绪前**就已渲染（里面只有"正在获取身份"）——
区块内的查询必须可等待（`within(panel).findBy…`），同步 `getBy` 会撞加载态。
这条同样是给下一个人的提醒：**权限驱动的页面**有三个渲染分支
（加载 / 无权限 / 正常），测试选分支要显式。

### Task 9: Security, accessibility, and performance regression

**Files:**
- Create: `frontend/e2e/security.spec.ts`, `accessibility.spec.ts`, `performance.spec.ts`
- Modify: frontend lint configuration

- [x] Search built assets for `object_key`, known server paths, fixture contract bodies, and tokens; fail if found outside test fixtures.
      → `scripts/check-built-assets.mjs` **挂在 `npm run build` 末尾**（产物刚生成就验）。
      **首轮就抓到真泄漏**：`describeApiError` 的网络失败文案写死了
      "（默认 127.0.0.1:8000）"——它进了产物，而且在生产环境里指错方向。已改成
      "确认后端服务已启动、且本页面的反向代理已指向它"。另修两个正则误报
      （裸盘符 `X:/` 会命中 "https:/" 的尾部，改成带真实目录段才算）。
      ⚠️ 这条检查只对**打包产物**有意义：源码里这些词出现在注释与测试里是合法的。
- [x] Test focus order, keyboard tabs/dialogs, screen-reader labels, non-color status cues, and contrast.
      → 三层：(1) **`jsx-a11y` 进 lint 门禁**——首轮就抓到"键盘行导航挂在 `<table>` 上"
      （非交互元素不该有键盘监听），已移到行链接（语义也更对）；
      (2) `src/a11y/a11y.test.tsx`：Tab 首站与顺序、每个按钮/链接/输入都有可访问名称、
      `<html lang="zh-CN">`、`:focus-visible` 规则存在；
      (3) **`contrast.test.ts` 按 WCAG 公式对 `tokens.css` 现算比值**——
      首轮抓到 `--text-3 #828a99` 对白底只有 ~3.5:1（辅助文字恰恰是 12px 小字），
      已改成 `#5d6878`（白底 5.6 / 卡片 5.3 / 表头 4.9），并加一条
      "text-3 必须仍比 text-2 浅"防"全调到最黑"式的通过。
- [x] Test a 100-page fixture for lazy rendering and bounded DOM page count.
      → `boundedPages.test.tsx` **3 条**：100 页文档下 DOM 里的页数恒为 **1**
      （策略是"一次只渲染一页"，不是窗口化长列表——证据定位的交互是"跳到某一页"）；
      证据框只属于当前页；文本降级也只渲染当前页。
- [x] Test session expiry, 401 redirect, 403 explanation, network retry, and stale job polling cleanup.
      → `session.test.tsx`：**401 不是故障**（切到登录表单，不是红色错误页——
      它的含义是"这份身份服务端不认"，处置是再填一次）；`/forbidden` 直接可达并说明"该找谁"；
      **轮询清理用 fake timers 验证**：卸载后推进 20 秒，`/api/writebacks` 的请求数
      停止增长——清理失效的表现是"没人看的页面每两秒打一次后端"，而本地看起来什么都没发生。
- [~] Run unit, accessibility, and Playwright suites.
      → 单元 + jsx-a11y lint 全绿（**208 tests**）。
      **Playwright 三个 spec 已写好但未运行**（`e2e/security|accessibility|performance.spec.ts`）：
      需要 `@playwright/test` + 浏览器二进制，本机装不了（理由与 `canvas` 相同——
      装不上的依赖不该挡住其余门禁）。它们已从 lint/typecheck 中**显式排除**（不是被遗忘），
      文件头写明运行前置；在有浏览器的环境：`npx playwright test e2e/`。

**Task 9 实施记录（三条）：**

**① 泄漏检查的价值在第一轮就兑现了**

`127.0.0.1:8000` 是**我自己**在 Task 2 写进错误文案的——"本地能跑"的地址
进了产物，而且在生产环境里指错方向。这正是"源码 grep 干净 ≠ 产物干净"的活例：
它躲在一条用户会看到的三段式文案里，代码评审根本不会看它一眼。

**② 对比度必须按公式算，而不是靠眼睛**

12px 的辅助文字 + 3.5:1 是双重惩罚（字号小、对比低）。修色后的层次约束
（text-3 仍比 text-2 浅）也进了测试——否则"把所有文字调成最黑"也能通过对比度回归。

**③ `?raw` 在 Vitest 里会被 CSS 管线拦成空串**

想读 `tokens.css` 的文本时，`import css from '...css?raw'` 拿到的是空模块
（CSS 处理发生在 raw 之前）。读静态文本用 `node:fs`（测试运行在 Node 里，
tsconfig 已带 `@types/node`）—— 这类"工具在测试环境里的真实行为"只有跑了才知道。

### Task 10: M8 acceptance and demo

**Files:**
- Create: `scripts/verify_m8.ps1`, `docs/demo/m8-demo-script.md`
- Modify: `README.md`, `合同审批审查系统-项目计划.md`

- [x] Encode all 15 approved M8 design acceptances plus five-module continuous demonstration as executable checks.
      → `scripts/verify_m8.py`（**本仓库第一个直接 import `app` 的脚本**——以脚本运行时必须
      显式把项目根加进 `sys.path`）。10 条：五模块走查 6（列表 → 详情 → 解析 → 规则 →
      保存/确认 → 回写链，每步用**前端真正消费的接口**取数）+ 薄出口 2（版本化/确认失效/
      幂等 + 与工具 6 的键集合一致）+ **契约漂移检查** 1（真实响应键集合 vs
      `frontend/src/api/apiShapes.json`；前端 `apiShape.test.ts` 用**同一份文件**核自己的
      类型化样本——两端都被钉在同一份文件上，谁漂了都在各自的门禁里红）+
      真浏览器回归 1（unmet，如实标注）。
- [x] Start mock approval, API, RULE/Outbox workers, and frontend; execute pending → detail → parse → rules → save/confirm/writeback.
      → TestClient + **与生产相同的 Worker 入口**（`make_handler`）。两处替身都有说明：
      附件字节直连对象存储（下载的 HTTP 编排不属控制台五模块，M3/M4 已覆盖）、
      `_FakeCommentGateway`（M6 同款）。⚠️ 走查**真的抓到了两个缺陷**，见 ①②。
- [ ] Capture screenshots of five modules, exact PDF highlight, permission denial, invalidated confirmation, and successful writeback.
      → **未拍，unmet**：浏览器二进制未安装。三个 e2e spec 已就绪
      （`frontend/e2e/security|accessibility|performance.spec.ts`），
      有浏览器的环境跑 `npx playwright test e2e/` 补齐；演示点击路径在
      `docs/demo/m8-demo-script.md`。
- [x] Run `npm run lint`, `npm run typecheck`, `npm test -- --run`, `npm run build`, Playwright, backend pytest, and `scripts/verify_m8.ps1`; expect exit 0.
      → `scripts/verify_m8.ps1` 编排六步（后端 pytest / 走查 / 前端四条门禁），
      任何一步失败退出码非 0。前端 **223 tests**（+15 契约形状）、lint/typecheck/build 全绿；
      后端全量回归见 ①③。Playwright 除外（unmet，同上）。
- [x] Mark M8 complete only after all five original modules work; admin pages cannot substitute for them.
      → 五模块由走查第 1–6 条连续证明（**管理页不在主线上**，只作为扩展入口演示）。

**本任务的实施记录（走查的真正价值：抓到了四个"各自看都正常"的问题）：**

**① 缺陷 A：空 `allowed_types` 把解析成功的任务判死（真缺陷，已修 + 回归测试）**

`make_handler` 默认 `allowed_types=()`，生产入口 `run_worker.main()` 也一直用默认值。
而 `execute_parse_job` 的契约是"空元组表示**不在此复核**"（647 行 `if allowed_types and …`），
**同一函数**调用的 `advance_task_after_parse` 却把空集当成"没有任何允许的类型"——
于是每次解析成功后任务被 `blocked/ATTACHMENT_TYPE_NOT_ALLOWED`：
**解析行是绿的，任务却是死的**，单看任何一行都"正常"。
只有走到回写（`done` 不可从 `blocked` 进入）才暴露——正是五模块连续走查的位置。
修复：聚合侧与门禁侧对"空"取同一语义（空 = 用下载时的权威校验）；
`main()` 显式传配置（行为只取决于 `.env`，不取决于两层函数的默契）；
`test_worker_with_default_allowed_types_does_not_block_the_task` 钉住病灶。

**② 缺陷 B：门禁失败的任务永远停在"正在解析"（被缺陷 A 掩盖，已修）**

旧单元测试 `test_failed_parse_does_not_let_the_task_through` 断言失败后任务停在 `PARSING`——
而 worker 集成测试 `test_gate_failure_moves_the_task_to_blocked_not_stuck_in_parsing` 断言 `BLOCKED`。
两个测试此前**同时通过**：缺陷 A 的空集判死恰好把任务打成 blocked（错误码还是错的），
把聚合函数缺失的"全部终局但有失败"分支掩盖了。修复后单测改为断言
`BLOCKED` + 错误码透传（`DOCUMENT_EMPTY`），两个测试终于说到同一件事。

**③ 85 个 teardown 假错误：IDE 的 safe-delete 守卫（环境，非产品）**

全量后端测试出现 85 个 `ERROR at teardown`，根因是 CodeBuddy IDE 的 sitecustomize
对"单次删除 ≥500 个文件"的 `rmtree` 抛 `SystemExit` 要求人工确认——测试临时目录
（含解析工件）恰好超阈值。conftest 对自家临时目录的清理加了防护；
**这与产品代码无关**，但不知道它的话，验收会被 85 个假错误拖进错误的排查方向。

**④ 契约形状检查第一轮就抓到真漂移：`latest_attempt_rejected`**

后端在没有回写尝试时返回 `null`，前端契约声明为 `boolean`。
`apiShape.test.ts`（15 条）用钉住的真实形状核类型化样本，编译期即红。
已改 `boolean | null` 并注明出处。

**⑤ 走查脚本的三个"症状与病因错位"（都写进了脚本注释）**

fitz 基座字体（helv）写不进中文 → 解析端报 `DOCUMENT_EMPTY`（PDF 看着正常）；
同字节同引擎命中解析缓存 → 本段预留的解析行永远 `pending`（症状是"没跑"）；
工件端点从真实存储读字节 → `OBJECT_NOT_FOUND`（真因是读错了存储）。
`run_once() is True` 只说明**领到了作业**，不代表成功——验收现场必须逐步核终态。
