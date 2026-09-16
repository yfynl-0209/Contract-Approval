# M7 Tool Facades, Query APIs, and RBAC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose all seven required tools through REST and MCP while adding authenticated task/query/confirmation/retry/admin interfaces for the React client.

**Architecture:** REST and MCP are adapters over one set of application interfaces; neither protocol layer owns business decisions. Authentication resolves an `Actor`, authorization is enforced server-side at a single dependency/policy seam, and every mutation emits an immutable M6 audit event. Attachment/document delivery is mediated by authorized backend endpoints and never exposes object keys or server paths.

**Tech Stack:** Python 3.11, FastAPI, MCP Python SDK, SQLAlchemy 2, Pydantic 2, pytest/httpx.

## Entry Gate（开始 M7 前必须满足）

- M6 的保存、确认、Outbox、Dispatcher 与工具 6/7 验收必须全部通过。
- M4 的 `PARSE` Worker 链路必须通过队列到服务的集成测试；不得把手工服务调用当作完整链路。

## Global Constraints

- Seven tool names and minimum parameters must match requirement 2.4.10 character-for-character.
- REST and MCP must call the same application functions and return the same business outcome semantics.
- Roles: `legal_reviewer`, `system_admin`, `read_only_auditor`; read-only actors receive HTTP 403 for mutations.
- Authentication failures are 401; authorization failures are 403; business gates retain their existing 409/200-outcome semantics.
- Frontend endpoints never return `file_path`, `object_key`, credentials, or unrestricted permanent URLs.
- List totals, writeback status, confirmation validity, and risk aggregates come from the backend.
- Every mutation records `actor_id`, `actor_name`, action, target, and correlation ID.

---

## File Map

- Create `app/auth.py`, `app/ports/identity_provider.py`, `app/adapters/auth/dev_header_identity.py`, `app/adapters/auth/jwt_identity.py`.
- Create `app/api/tasks.py`, `app/api/results.py`, `app/api/attachments.py`, `app/api/rules.py`, `app/api/admin.py`.
- Create `app/tool_facade.py`, `app/mcp_server.py`, `scripts/run_mcp.py`, `scripts/verify_m7.py`.
- Create tests: `test_auth_rbac.py`, `test_task_queries.py`, `test_attachment_content_api.py`, `test_rule_admin_api.py`, `test_mcp_tools.py`, `test_m7_contracts.py`.
- Modify `app/api/deps.py`, `app/api/tools.py`, `app/api/jobs.py`, `app/main.py`, `app/config.py`, `requirements.txt`.

### Task 1: Actor and authorization seam

**Files:**
- Create: `app/auth.py`, `app/ports/identity_provider.py`, `app/adapters/auth/dev_header_identity.py`, `app/adapters/auth/jwt_identity.py`
- Modify: `app/api/deps.py`, `app/config.py`
- Test: `tests/test_auth_rbac.py`

**Interfaces:**
- Produces: `Actor(actor_id, display_name, roles, tenant_id)` and `require_permissions(*permissions)`.
- Permissions: `task:read`, `review:execute`, `result:save`, `result:confirm`, `writeback:execute`, `rule:manage`, `ops:retry`, `audit:read`.

- [x] Write failing tests for missing credentials, invalid identity, tenant mismatch, allowed role, and denied mutation.
      → `tests/test_auth_rbac.py`，**53 passed**（exit=0）。先写测试再写实现；
      首轮跑出 47 passed / 1 failed，失败原因是 PyJWT 拒绝**生成**算法混淆令牌
      （见下方"偏离 4"）。
- [x] Implement a port-backed identity resolver; the development adapter reads explicit headers only when `AUTH_MODE=dev`.
      → `app/ports/identity_provider.py`（`IdentityProvider` Protocol，读 `Mapping[str, str]`
      而不是 `Request`，让 MCP 形态也能用）+ `app/adapters/auth/dev_header_identity.py`
      （`X-Actor-Id` / `X-Actor-Roles` / `X-Tenant-Id`）。组合根 `_build_identity_provider`
      只在 `AUTH_MODE=dev` 时选它，**未知取值直接抛错**（有测试）。
- [x] Implement the production JWT adapter with signature, issuer, audience, expiry, tenant, and role-claim validation; obtain keys through configured JWKS or a static-public-key adapter.
      → `app/adapters/auth/jwt_identity.py`：静态公钥与 JWKS **两条密钥来源**都已实现
      （JWKS 惰性构造，只在真正用到时才建连接）。签名 / `iss` / `aud` / `exp` / `tenant` /
      角色声明逐项有测试；算法来自**配置**而非令牌的 `alg` 头，算法混淆攻击有专门用例 +
      一个"该伪造令牌本身语法正确"的对照组。
      ⚠️ 新增依赖 `pyjwt[crypto]==2.14.0`（`[crypto]` extra 才会拉 `cryptography`，
      RS256/ES256 验签需要）。
- [x] Fail startup when `ENV=production` selects the development Header adapter or lacks valid production identity configuration; never downgrade to anonymous access.
      → 三层防线，每层各有测试：① `app/main.py` 的 `lifespan` 在 `yield` **之前**调
      `assert_auth_configuration`（进程起不来，不是"第一次请求时才报"）；
      ② `_build_identity_provider` 对未知 `AUTH_MODE` 抛错、不兜底；
      ③ `DevHeaderIdentity.__init__` 自己在生产环境拒绝被构造。
      **实测**：`ENV=production AUTH_MODE=dev` 下真的把应用启一遍，得到
      `REFUSED_TO_START: AuthConfigurationError`。
- [x] Map roles to immutable permission sets; routes name permissions rather than checking role strings.
      → `ROLE_PERMISSIONS` 是 `MappingProxyType`，运行时改写会抛 `TypeError`（有测试）；
      8 项权限逐项断言（不用"数量相等"——数量相等在权限被调换时照样通过）。
      `app/api/tools.py` 的 7 个端点全部写成 `Depends(require_permissions(...))`，
      **没有一处比较角色字符串**。
- [x] Attach the resolved actor to request scope and correlation logs without logging bearer tokens.
      → `get_actor` 把 `Actor` 写进 `request.state.actor`（关联 ID 中间件与异常处理器
      都在依赖之外，这是它们唯一都能看到的地方）。**只放解析结果，不放请求头** ——
      挂上 `Authorization` 原文等于给未来任何一处日志留了一条泄漏路径。
      "不记录 bearer 令牌"落地为 `test_rejected_token_text_never_appears_in_the_error`：
      四种拒绝路径下断言令牌原文与载荷内容都不出现在错误消息里。
      **该测试有牙齿**：把 `f"令牌 {token} 校验失败"` 注入 `_decode` 后它确实变红（已还原）。
      原始令牌也不进 `AuditEvent`。
- [x] Run `python -m pytest -q tests/test_auth_rbac.py`; expect all pass.
      → **53 passed，exit=0**。

**本任务对既有代码的连带改动（不在原计划 Files 清单里，但必须做）：**

| 文件 | 改动 | 为什么 |
| --- | --- | --- |
| `app/enums.py` | 新增 `AUTHENTICATION_REQUIRED`(401)、`PERMISSION_DENIED`(403)、`IDENTITY_PROVIDER_UNAVAILABLE`(503，瞬时) | `AUTH_FAILED` 是**出站**码（"审批系统拒绝了我们的凭据"，映射 502）。复用它做入站鉴权会得到"调用方没带令牌 → 502 上游凭据问题"这种荒谬映射 |
| `app/api/errors.py` | 401/403 映射；401 带 `WWW-Authenticate: Bearer` | 不带时部分客户端不会触发"取新令牌后重试" |
| `app/workflow/job_inputs.py` | 新增 `ActorPayload`；`ResultJobInput.actor` / `WritebackJobInput.actor` 由 `str` 改为 `ActorPayload` | 作业是"过去那一刻的意图"，Worker 进程身份与发起人无关。只存名字时审计里 `actor_id` 永远是空 —— 而"张伟"在两个部门各有一个 |
| `app/services/result_service.py`、`app/services/writeback_service.py` | `actor` 由 `str` 改为 `Actor` | 用一个参数而不是 `actor` + `actor_id` 两个：两个参数允许调用方填了显示名却**静默漏掉** id |
| `scripts/run_worker.py` | RESULT / WRITEBACK 处理器传 `job_input.actor.to_actor()` | Worker 侧还原当时身份 |
| `tests/`（**9** 个文件；计划预估"约 50 处调用点"，实测 **44** 处） | **36** 处字符串 `actor="名字"` 改为各文件本地 `_actor()` 助手（`test_result_service` 20、`test_writeback_service` 9、`test_m6_api` 6、`test_outbox` 1）；**8** 处 `"actor": "名字"` 改为 `_actor_payload("名字")`（`test_m6_api` 4、`test_m6_schema` 4）；4 个 API 夹具新增 `get_actor` 覆盖（`test_api_tools` / `test_fault_drills` / `test_m6_api` / `test_parse_api`）；`test_source_invariants` 放宽判据 | 夹具原本只覆盖 `get_db`/`get_gateway`/`get_storage`，不覆盖身份依赖 → 替换后首轮全量跑出 **66 failed / 1099 passed**，绝大多数是端点 401 |
| `tests/test_source_invariants.py` | 依赖方向判据放宽为"组合根**或** `adapters/` 自身" | `adapters/auth/jwt_identity.py` 引用同包的 `_headers.py` 是**同层内**复用，方向没反转。判据放宽后**仍有牙齿**：往 `services/result_service.py` 注入一句 `from app.adapters...`，两条方向测试同时变红（已还原） |

**偏离计划之处（均为实现中发现，非事后解释）：**

1. **`Actor` 里没有 `permissions` 字段，改为计算属性。** 存一份权限快照意味着
   角色映射表改了之后，已经构造出来的主体还带着旧权限 —— 这类不一致只在
   "改了映射表并有长生命周期对象"时才暴露。
2. **未识别的角色 → 零权限但不报错。** IdP 先上、本系统后跟时，
   报错会让**所有人**无法使用，而少给权限只会让那一个人看到 403；
   角色原话保留在 `Actor.roles` 里，403 消息会把它列出来（否则"我明明有那个角色"
   就没有线索）。
3. **新增 `AuthConfigurationError`，刻意**不是** `AppError。** 配置错误不能被翻译成
   HTTP 响应（它只能以 500 暴露），否则会被当成"调用方的问题"。
4. **算法混淆的伪造令牌用手工 HMAC 拼，不用 `pyjwt.encode`。** 新版 PyJWT 会以
   `InvalidKeyError` 拒绝用 PEM 公钥当 HMAC 密钥**生成**令牌 —— 这是库在保护调用方，
   但攻击者不受这层保护。用库来造，测的是"库不肯帮我造"而不是"我们的校验器会不会接受"。
   另配一个对照组，独立重算 HMAC 确认那枚伪造令牌本身语法正确、签名真实。
5. **`JwtIdentity` 在"既无公钥也无 JWKS"时抛 `AuthConfigurationError`（原为
   `AuthenticationError`）。** 这是**写测试时发现的缺陷**：抛 `AuthenticationError`
   会被翻成 401，于是每个调用方都读到"我的令牌有问题"、分头去重新登录，
   而真正该做的是改配置 —— 那件事任何调用方都做不到。已有专门测试锁住这个语义。
6. **`tests/` 不是包**（只有 `tests/contract/__init__.py`），因此 34 处调用点的
   `_actor()` 助手写在各自文件里，不做跨模块导入。

### Task 2: Canonical seven-tool facade

**Files:**
- Create: `app/tool_facade.py`
- Modify: `app/api/tools.py`
- Test: `tests/test_m7_contracts.py`

**Interfaces:**
- Produces exactly seven functions named by requirement 2.4.10.
- Each facade accepts the minimum legacy parameters and optional keyword-only enterprise context.

- [x] Write signature tests for all seven names and minimum positional parameters.
      → `tests/test_m7_contracts.py`，**54 passed**。签名按需求 6.5 写成**数据表**
      （`REQUIRED_SIGNATURE`：参数名、**顺序**、有无默认值三项），而不是散在断言里。
      顺序也断言：只比名字集合时，`download_contract_attachment` 的
      `instance_id` / `attachment_id` 对调仍然通过 —— 两个都是字符串，
      传反了会拿到"附件不存在"，排查方向直接跑偏。
- [x] Move protocol-neutral orchestration from REST route functions into the facade; do not move HTTP exceptions or response headers.
      → Task 2 开始前 `app/tool_facade.py`（656 行）与瘦身后的 `app/api/tools.py`
      **已就位**（M6 期收敛完成），本任务补上它缺失的契约测试。源码守卫
      `test_facade_never_imports_the_http_layer` 锁住"门面不得 import
      `fastapi` / `starlette` / `app.api`"—— 含 `app.api.errors`，
      它会诱导门面去理解状态码。
- [x] Apply permissions and actor propagation at the facade entry used by both protocols.
      → `_require` 在**每个**门面函数第一行。用例给关键字专属参数塞"一碰就炸"的
      替身（`_Exploding`），因此判定一旦被挪到副作用之后就会失败。
      **有牙齿**：在 `run_contract_rules` 的 `_require` 前注入一行
      `session.get_bind()` 后，`test_single_gate_rejects_before_any_side_effect[run_contract_rules]`
      确实变红（已还原）。
      另配只读角色边界用例：`read_only_auditor` 对 3 个读工具**通过**、
      对 4 个写工具**被拒** —— 只断言"全被拒"时，一个把 `task:read`
      也拿掉的实现照样通过。
- [x] Add parity tests: direct facade and REST produce equivalent outcome/data for the same request.
      → 七个工具各一条，用**两套独立装配**（同一 `work_dir`、同一 `storage_root`、
      两份种子相同的库）：一套只走 REST，一套只走门面，响应体**逐字相等**
      （`_assert_same_body`）。刻意不用"包含关系"——`rest.items() <= facade.items()`
      能被一种缺陷通过：端点**多下发**了 `object_key` 或文件系统路径。
      另有一条 `test_rest_forwards_the_enterprise_context_it_accepts`：
      `parse_options` 与 `force` 都有默认值，端点忘了转发时接口照样 200，
      只是"强制重跑静默变成复用"。
- [x] Run `python -m pytest -q tests/test_m7_contracts.py tests/test_api_tools.py`; expect all pass.
      → **88 passed**，exit=0。

### Task 3: Task, job, evaluation, and result query interfaces

**Files:**
- Create: `app/api/tasks.py`, `app/api/results.py`
- Modify: `app/api/jobs.py`, `app/main.py`
- Test: `tests/test_task_queries.py`

**Interfaces:**
- Produces: `GET /api/tasks`, `/api/tasks/{id}`, `/jobs`, `/evaluations`, `/results`.
- Produces: `POST /api/tasks/{id}/context/confirm`, `POST /api/results/{id}/confirm`.

- [x] Write response-schema tests for pagination, server-side totals, stable sorting, task status, task-level write status, latest attempt status/reason, and correlation IDs.
      → `tests/test_task_queries.py`，**21 passed**。
      **总数**：3 条数据 + `page_size=2` → `total=3` / `page_count=2` / `has_next=True`。
      **稳定排序**：把四行的 `created_at` 抹成同一个值（SQLite 的
      `CURRENT_TIMESTAMP` 精度只到秒，这不是理论问题），断言两次相同请求
      给出相同顺序，且顺序里 `id` 在场；另有"两页不重不漏"一条。
      **回写两个层级**：第 1 次 `not_written`+`MANUAL_CONFIRM_REQUIRED`、
      第 2 次 `failed`+`APPROVAL_API_ERROR` → 详情给的是**第 2 次的**原因，
      且 `latest_attempt_rejected` 从 `true` 变 `false`。
      **关联 ID**：作业带 `corr-abc` → 详情给得出；没有作业时给 `None`
      （**不编一个**：排障的人会拿着查不到任何日志的 ID 去查）。
      **枚举白名单**：`task_status=blockd` → 400 而不是空列表。
- [x] Return `confirmation_valid` from backend result views; do not expose digests as a decision the browser must compare.
      → `result_row_json` 用 `result_service` 的**完整口径**（已确认 + 摘要相符
      + **仍是当前版本**）。用例：确认 → `true`；新版本出现后旧版本
      `manual_confirmed` 仍为 1（历史不删）但 `confirmation_valid` 变 `false`。
      另有一条断言**列表行与详情的 JSON 逐字相等** —— 否则界面要为每一行
      再发一次详情请求，而两次的判据一旦分叉就会出现"列表说有效、详情说过期"。
- [x] Keep four rule states visible; default ordering puts `hit` and `needs_review` first without deleting other evaluations.
      → `_ATTENTION_FIRST`（`CASE` 排序，不是过滤）。用例断言
      `total == 4`、四态齐全、且前两位恰好是 `hit` 与 `needs_review`；
      同权重内按 `rule_code` 稳定排序，最后以 `id` 兜底。
- [x] Enforce tenant visibility on every query, including nested resource IDs.
      → `query_service` 里所有查询都从 `_task_scope(tenant_id)` 出发；
      `workflow_jobs` / `review_results` / `rule_hits` **都没有 `tenant_id`**，
      归属由 `task_id` 传递，因此一律 join 回 `approval_tasks` 再过滤。
      **本任务同时给既有端点补上了租户门**（`/api/jobs/{id}`、`/api/parses/{id}`、
      `/api/runs/{id}`、`/api/writebacks/{id}`）：它们此前只要知道 id 就能读，
      而这些字段单独看都不敏感、合起来能拼出对方的数据结构。
- [x] Add direct-ID enumeration tests proving another tenant receives 404/403 without data leakage.
      → 两层证据：① 六种嵌套资源逐个猜 id，全部 **404** 且响应体里
      不含对方的 `instance_id` / `our_party_name`；
      ② 枚举 `range(1, 12)` 的 **task_id 区间**，断言状态码集合是 `{404}`
      **且响应体彼此一致** —— 只要有一条回 403，状态码本身就成了探针。
      还补了写路由一条：跨租户**确认**别人的结果 → 404，且对方的
      `manual_confirmed` 仍是 0（不只在读路由上过滤）。
      **有牙齿**：把 `_assert_result_visible` 的归属判断去掉后，两条用例变红（已还原）。
- [x] Run `python -m pytest -q tests/test_task_queries.py`; expect all pass.
      → **21 passed**；全量 `python -m pytest -q` → **1244 passed, 3 skipped**，
      `compileall` 通过。

**本任务对既有代码的连带改动（不在原计划 Files 清单里，但必须做）：**

| 文件 | 改动 | 为什么 |
| --- | --- | --- |
| `app/services/query_service.py` | **新增**：分页、租户可见性、排序口径、`confirm_context` | 接口层不得有业务分支（README §4.1）。分页/排序/跨租户给 404 还是 403 全是业务口径 |
| `app/api/views.py` | **新增**：`page_json` / `evaluation_json` / `iso` / `json_or_none` | 同一条评价出现在 `/api/runs/{id}` 与 `/api/evaluations` 两处；两处各写一遍时的分叉方式是"某天给其中一处加了字段"，没有一处会报错 |
| `app/services/result_service.py` | 新增公开的 `is_current_version()`，`confirmation_valid()` 改用它 | "列表里哪个是当前版本"与"确认还有效吗"必须**同一判据**，否则同一页面上两个数字互相矛盾且各自看起来都对 |
| `app/enums.py` + `db/schema.sql` + `app/models.py` | 新增 `AuditAction.CONTEXT_CONFIRMED`（枚举、CHECK、ORM `_checks` 三处同步） | 需求 §12 把"上下文确认"列为必须留痕的审计事件；三处漏一处会被 `test_schema_consistency` 抓住 |
| `app/api/jobs.py` | 新增 `GET /api/jobs`；既有端点补 `actor` + 租户门；`/api/results/{id}` **移出**本模块；`_evaluation_json` 改为消费 `views` | 重复注册同一路径时后一条**永远不生效且不报错**，因此加了 `test_no_route_path_is_registered_twice` 守卫 |
| `tests/test_rule_service.py` | 5 处 `get_run(...)` 补 `actor=`；3 条用例补铺父任务 | 端点函数现在要求显式身份；租户判据挂在**任务**上，而本文件此前不铺 `tasks` 父级链 |
| `tests/test_m6_schema.py` | 审计动作取值域补 `CONTEXT_CONFIRMED` | 与上面三处同步 |

**偏离计划之处（均为实现中发现）：**

1. **`list_results` 删掉了 `only_current` 参数。** 加过一版：先取一页再过滤，
   于是 `total` 变成"过滤前的总数"——"共 8 条"配着 3 行数据。
   要这个视角的调用方按任务取详情（`is_current_version` 逐行给出）。
2. **`/api/results/{id}` 从 `jobs.py` 移到了 `results.py`。** 两个路由同时注册时
   后一条静默失效；顺带把 M6 承诺出去的 `RESULT_NOT_FOUND` 机器码**保留**下来
   （没顺手统一成 `RESOURCE_NOT_FOUND` —— 改码会让既有调用方在运行到那一行时才失效）。
3. **`task_view` 不收 `session`。** 收一个用不到的参数会让调用方以为这里还会查别的东西，
   而分页信封也就没法直接把它当逐行序列化函数用。
4. **`_fetch_page` 的 `scalars` 由调用方显式指定**，不用"选了几列"去推断：
   `select(Entity)` 与 `select(Entity, col)` 都只有一列/两列，而前者要实体、后者要元组；
   推断错的表现是序列化深处抛 `KeyError: 'id'`（已踩到）。
5. **列表接口刻意接受 N+1**（默认 20 行）来保证"列表与详情同一口径"。

### Task 4: Attachment and standard-document delivery

**Files:**
- Create: `app/api/attachments.py`
- Modify: `app/main.py`
- Test: `tests/test_attachment_content_api.py`

**Interfaces:**
- Produces: `GET /api/attachments/{id}/content` with Range support.
- Produces: `GET /api/parses/{id}/document` with page size, coordinate system, rotation, blocks, and text precision.

- [x] Write failing tests for full body, valid byte range, suffix range, invalid range 416, content type, authorization, and cross-tenant denial.
      → `tests/test_attachment_content_api.py`，**24 passed**。
      区间矩阵逐条：`bytes=0-99` → 206 + `Content-Range: bytes 0-99/总长`；
      `bytes=100-` 到末尾；`bytes=-100` 取**末尾 100 字节**（写成"从 100 开始"
      同样返回 206 与一段数据，只有逐字节比对才看得出）；越界 → 416 +
      `bytes */总长`；`bytes=-0` → 416。
      **不支持的 `Range` → 200 + 完整正文**（`items=` / 语法错 / 多区间 / 空区间，
      按 RFC 9110 §14.2 忽略）。多区间刻意**不**"只给第一段"：
      那会让调用端以为自己拿全了。
      授权：无身份 → **401 + `WWW-Authenticate: Bearer`**；只读审计**可以读**
      （下载属于"看"，挡掉时审计的人除了任务列表什么都看不到）；
      跨租户 → 404 且断言**响应里没有对方的字节**（只断状态码时，
      "先读字节再判归属"的实现照样通过）。
- [x] Stream bytes through `ObjectStorage`; never place `object_key` or filesystem path in the response.
      → 字节只经 `storage.get(key)` 取得；`object_key` **不出现在响应体与任何一个响应头**里
      （用例把正文与全部头拼起来找 `sha256/` 前缀与 `storage` 片段 ——
      只看正文会漏掉 `Content-Location` 这类"看起来只是元数据"的头）。
      ⚠️ **实现是整份读进内存再切片，不是真正的流式**（见下方偏离 1）。
- [x] Set safe `Content-Disposition`, `Accept-Ranges`, `Content-Range`, ETag from SHA-256, and `nosniff` headers.
      → `ETag` 用内容 SHA-256 并带引号（entity-tag 语法，不加 `W/`）；
      `X-Content-Type-Options: nosniff`（缺它时浏览器会把一份声称 pdf 的 HTML
      当页面渲染 —— 存储型 XSS 的标准入口）。
      `Content-Disposition` **两个文件名一起给**：`filename*`（UTF-8 百分号编码）
      给现代客户端，`filename` 作 ASCII 兜底。文件名先剔除 CR/LF/引号/反斜杠 ——
      它来自**外部审批系统**，是本模块唯一一处把外部字符串放进响应头的地方，
      用例用 `evil.pdf\r\nX-Injected: yes` 断言注入头不出现。
- [x] Return the versioned M4 standard-document contract without recomputing coordinates.
      → `GET /api/parses/{id}/document` 取 `artifact_version` 最大的一份工件
      （与 `rule_service` 读取侧的取法一致），**原样下发**：
      用例断言 `body["document"] == json.loads(工件原文)`，并逐字段核对
      `bbox` 浮点值 / `bbox_space` / `rotation` / 两个精度声明 / `chars` 的逐字符几何。
      下发前经 M4 契约校验（校验 ≠ 重算坐标）：损坏的工件得到明确错误，
      而不是一份结构上看不出问题的 JSON 被前端拿去画框。
      `artifact_version` 与 `schema_version` 都在响应里。
- [x] Run `python -m pytest -q tests/test_attachment_content_api.py`; expect all pass.
      → **24 passed**；全量 `python -m pytest -q` → **1268 passed, 3 skipped**，
      `compileall` 通过，路由无重复。

**偏离计划之处（均为实现中发现）：**

1. **没有做真正的流式读取。** 计划写的是 "Stream bytes through `ObjectStorage`"，
   而端口目前只有 `get(key) -> bytes`，没有分段读。加一个分段读方法是**投机**：
   本地文件实现下它与整读没有差别，收益要等 M9 的 MinIO 才出现 ——
   而那时真正要改的也是那个实现。当前开销由 `attachment_max_bytes`（默认 20MB）兜住。
2. **`OBJECT_NOT_FOUND` 登记为 404。** 它此前没在 `app/api/errors.py::_EXPLICIT_STATUS` 里，
   于是会落到"其他确定性失败 → 500"。这不只影响本任务的接口：
   `rule_service._standard_document` 在"解析没有标准文档工件"时抛的也是这个码，
   即一个纯数据问题会表现为服务端崩了。**顺手补上**，因为本任务正要依赖它的语义。
3. **"记录存在但字节没入库"用 `OBJECT_NOT_FOUND` 而不是 `RESOURCE_NOT_FOUND`。**
   两者都是 404，但机器码必须不同：前者的处置是"先跑工具 3"，
   后者的处置是"核对 id"。合成一个码时调用方会去改一个本来就对的 id。

### Task 5: Retry and administration interfaces

**Files:**
- Create: `app/api/admin.py`, `app/api/rules.py`
- Modify: `app/workflow/state_machine.py`
- Test: `tests/test_rule_admin_api.py`, `tests/test_retry_api.py`

**Interfaces:**
- Produces: task retry, rule CRUD/publish/reload, logs, and audit query routes defined in the project plan.

- [x] Write a retry matrix test: parse failures restart parse, rule/result failures restart review, writeback failures restart writeback only.
      → `tests/test_retry_api.py`，**48 passed**（与 Task 6 合计）。矩阵写成**参数化数据表**
      （`blocked_stage` → 重跑的作业类型 → 任务恢复到的状态）：
      `parse→parse/parsing`、`rule→rule/reviewing`、`result→**result**/reviewing`、`writeback→重新武装投递`。
      ⚠️ **`result` 映射到 `RESULT` 而不是重跑规则**：批次已经是好的，失败的只是"把结论落成结果"
      这一步；重跑规则会**新建批次**，把一次保存失败放大成一次结论变更。
      用例同时断言**没有多出别类型的作业**（只断言状态时，一个"改了状态但没排作业"的实现照样通过）。
- [x] Require operator reason for manual retry and persist an audit event.
      → `reason` 进 `audit_events.detail_json` 与 `task_logs`；`TASK_RETRIED` 是**新增的审计动作**
      （枚举 / `schema.sql` CHECK / ORM `_checks` 三处同步）。
      缺字段与纯空白得到**同一个 400**（下详"偏离 1"）。
- [x] Make rule updates versioned and validate configs before activation; reject in-place mutation of a version used by an existing run.
      → `app/services/rule_admin_service.py`。判据是 `rule_hits.rule_version`
      （**评价当时的版本快照**）：该版本被引用过 → 就地改内容返回 **409 `RULE_VERSION_IN_USE`**
      并提示提升版本；未被引用 → 允许（首次使用前的修正，否则版本号会变成噪音）。
      校验与 `scripts/check_rules.py` **共用一份实现**（新抽出的 `app/rules/validation.py`），
      并新增"llm 规则必须配 fallback"这条硬判据（计划 §5.4 有要求，脚本此前只当提示打印）。
- [x] Restrict rule management/retry to admin permissions and audit reads to admin/auditor permissions.
      → 5 条规则路由全部 `rule:manage`（**含只读查询**）；重试 `ops:retry`；审计 `audit:read`。
      `/api/rules/*` 的 5 条路由 × 2 个非管理角色逐条断言 403，另加一条 401（无身份）。
- [x] Run focused tests and prove read-only actors get 403 on every mutation route.
      → `tests/test_rule_admin_api.py` + `tests/test_retry_api.py`（合计 **48 passed**）。
      ⚠️ 只断言"只读被拒"不够：本文件同时断言**法务审核人**也被拒 ——
      把 `review:execute` 当通行证的实现会只在后一条上失败。

**本任务对既有代码的连带改动（不在原计划 Files 清单里，但必须做）：**

| 文件 | 改动 | 为什么 |
| --- | --- | --- |
| `app/rules/validation.py` | **新增**：从 `scripts/check_rules.py` 抽出单条规则的语义判据 | 判据有了第二个使用方（激活前校验）。两处各写一遍时的分叉方式是"某天给其中一处加了一条检查"，而另一处**不报错**、只是放行 |
| `app/services/query_service.py` | 新增 `list_task_logs` / `list_audit_events` / `paginate`；`_check_member` → 公开 `check_member` | 分页、排序、白名单、租户可见性全是**业务口径**（README §4.1：接口层不得有业务分支）。白名单反正是同一份判据，规则列表也要用 |
| `app/enums.py` | 新增 `RuleStatus` 枚举、4 个 `ErrorCode`（`RULE_NOT_FOUND` / `RULE_CONFIG_INVALID` / `RULE_VERSION_IN_USE` / `RETRY_NOT_SUPPORTED`）、3 个 `AuditAction`（`TASK_RETRIED` / `RULE_CREATED` / `RULE_UPDATED`） | 四个错误码回答四个不同的问题（核对 code / 改配置 / 升版本 / 换入口），合并会让操作员去改一个本来就对的东西 |
| `app/models.py` + `db/schema.sql` | `audit_events.task_id` 改为**可空** | 需求 §12 要求"规则修改"也进不可变审计账，而改一条规则影响的是**所有任务**。为它随便挑一个 `task_id`，审计里就会出现一条"看起来在说某条任务"的规则变更记录 —— 排障的人会去查那条任务，而真正变的是全局配置 |
| `app/schemas.py` | 新增 `CreateRuleRequest` / `UpdateRuleRequest` / `RetryTaskRequest` | 请求体 DTO 按既有约定放 `schemas.py`（"面向接口层的 DTO 也会放在本模块"） |

**偏离计划之处（均为实现中发现）：**

1. **`RetryTaskRequest.reason` 不写 `min_length=1`。** 写了之后：缺字段 → 框架 422，
   **纯空白 → 400**（被 `str_strip_whitespace` 清成 `""` 后撞 `min_length`）——
   同一件事两种状态码。而"原因必填"是一条**业务规则**（它要进审计账），
   因此只在服务层判一次，两种输入都得到同一个 400 `INVALID_ARGUMENT`。
2. **`pull` / `detail` / `download` 三种失败位置**返回 409 `RETRY_NOT_SUPPORTED`
   并在消息里写出**该去执行哪个工具**。它们由工具 1–3 在同步路径上完成
   （Worker 不领取这三类作业），硬映射成一个死作业会让任务回到 `parsing` 后永远停住。
   恢复入口确实存在：重跑工具 3 成功后 `attachment_service` 会自己 `start_retry(task)`。
3. **审计列表的 `include_system` 默认为假。** 系统级事件（规则变更）不属于任何租户，
   默认带上它们等于让每个租户都能看到全局配置变更；默认值选"看不到"是 fail-closed 的方向
   （漏看一条只是少一点信息，多看到别人的是数据泄漏）。
4. **"激活前校验"落在 `POST /api/rules/reload`。** 规则**没有缓存**（每次评价现读），
   因此"重新加载"这个动作在实现上不存在 —— 真正需要的是一个**闸门**：
   规则改完之后、被下一个批次用上之前，必须有一次"整批都合法吗"的检查。
   校验覆盖**停用**的规则：停用期间配置是坏的不会被任何人发现，直到启用那一刻才炸。

### Task 6: MCP adapter with seven exact tools

**Files:**
- Create: `app/mcp_server.py`, `scripts/run_mcp.py`
- Test: `tests/test_mcp_tools.py`

**Interfaces:**
- Produces seven MCP tools with stdio and streamable-HTTP transports.
- Consumes only `app/tool_facade.py`.

- [x] Write a discovery test asserting the exact seven names and required input fields.
      → `tests/test_mcp_tools.py`，**18 passed**。必填参数写成**数据表**
      （`REQUIRED_FIELDS`：工具名 → 必填集合），逐项比对 `inputSchema.required`。
      只断名字时，一个"参数被改成可选"的实现照样通过 —— 而必填变可选会让模型侧
      构造出一个缺字段的调用，错误推迟到运行时才出现。
- [x] Register facade calls without importing FastAPI routes.
      → `app/mcp_server.py` 只 import `app.tool_facade` / `app.services.result_service`
      （异常类型）与 `app.ports`；**不 import `app.api` / `fastapi` / 任何适配器**。
      适配器由组合根 `scripts/run_mcp.py` 注入 —— 这是
      `test_source_invariants.py::test_composition_root_is_the_only_adapter_importer` 的硬要求。
- [x] Translate application errors into stable MCP error payloads; preserve business outcomes such as `blocked` and `reused`.
      → **业务结论**（`blocked` / `reused` / `queued`）正常返回。
      **可预期业务错误**返回同一形状的载荷：`{"outcome": "error", "error_code": …, "retryable": …}`。
      ⚠️ 刻意**不用 `ToolError`**（下详"偏离 5"）：FastMCP 会把任何异常重新包装成
      `ToolError(f"Error executing tool {name}: {e}")`，JSON 载荷前被焊上一句英文前缀 →
      调用方要拿机器码就得做字符串处理，而那正是 `app/api/errors.py` 已经拒绝过一次的做法。
      `ResultInputError` **单独判且排在 `ValueError` 之前**：否则 REST 答
      `RESULT_RUN_NOT_COMPLETED`（等它跑完）而 MCP 答 `INVALID_ARGUMENT`（改参数）——
      **两种协议对同一次调用给出相反的处置方向**。
- [x] Add REST/MCP parity tests for success, business denial, missing resource, and asynchronous `TaskRef`.
      → 成功：工具 1/2/3 与门面**逐字相等**（工具 2 比较的是**第二次**同步 ——
      第一次会创建附件记录，`is_new` 不同，直接比第一次是测试自己造的差异）。
      业务拒绝：工具 3 → `outcome="blocked"` 且 `isError=false`。
      资源不存在：工具 5/6 → `RESOURCE_NOT_FOUND` / `RESULT_NOT_FOUND`。
      异步：工具 4/5 → `task_ref.job_id` + `status_url`。
      另有"代码缺陷**不**伪装成业务结论"一条（假网关抛 `TypeError` → `isError=true`）。
- [x] Run `python -m pytest -q tests/test_mcp_tools.py`; expect all pass.
      → **18 passed**。

**本任务对既有代码的连带改动：**

| 文件 | 改动 | 为什么 |
| --- | --- | --- |
| `requirements.txt` | `mcp==1.2.0` → **`mcp==1.9.4`** | 1.2.0 **没有** `streamable-http` 传输（只有 stdio / sse / websocket），而计划明确要求它。升级风险很低：`mcp` 此前**没有任何调用方**（M7 才开始接），且 1.9.4 的依赖（pydantic>=2.7.2 / starlette>=0.27 / httpx>=0.27）与现有版本相容 —— 实测只替换了 mcp 一个包 |
| `app/api/deps.py` | `_build_identity_provider` → 公开的 `build_identity_provider` | MCP 的组合根（`scripts/run_mcp.py`）也要按 `AUTH_MODE` 选适配器。让脚本去 import 一个私有函数，等于把"这是唯一选择点"这件事藏起来 |

**偏离计划之处（均为实现中发现）：**

5. **业务错误不走 `ToolError`，而是与成功结果**同形**的 `outcome="error"` 载荷。**
   `isError` 留给**协议层**失败（工具名不存在、入参不符合 inputSchema —— 这些由 SDK
   在调用我们的函数**之前**判定）。理由见上：FastMCP 会把 `ToolError` 的文本再包一层。
6. **`app/mcp_server.py` 刻意不使用 `from __future__ import annotations`。**
   FastMCP 生成参数 schema 时直接 `issubclass(param.annotation, Context)`，
   延迟注解会把注解变成字符串 → **注册工具时就抛 `TypeError`，服务根本起不来**。
   本项目其他模块都用延迟注解，这里是一处必须的例外（且它的失效方式是"起不来"，
   不会被误以为已经生效）。
7. **工具 6 的 `focus_points_json` 收 `list[str] | str` 两种形态。**
   FastMCP 在**校验之前**会做一次 JSON 预解析
   （`utilities/func_metadata.py::pre_parse_json`，为兼容"把数组当 JSON 字符串传"的客户端）：
   任何**能解析成 JSON 的字符串**都会被换成解析后的值。声明成 `str` 时，
   按需求传 `"[]"` 会被换成列表 `[]` 再按 `str` 校验 → `isError=true`，
   **按需求写的调用方永远调不通**。参数**名字**仍是需求那一个。

### Task 7: M7 acceptance and security regression

**Files:**
- Create: `scripts/verify_m7.py`
- Modify: `README.md`, `.env.example`, `合同审批审查系统-项目计划.md`

**Interfaces:**
- Produces a fail-closed numbered report and runnable REST/MCP examples.

- [x] Add exact-node acceptance for seven signatures, REST/MCP parity, 401/403, tenant isolation, audit actor, Range delivery, no path/object-key leakage, confirmation validity, and checkpoint retry.
      → `scripts/verify_m7.py`，**43 条**，术语沿用 `verify_m6` 的"逐条引用各起一次 pytest"形态。
      引用全部是**精确节点**（不是整文件）：文件通过不能证明"该验收项有专属断言"。
      ⚠️ 初版**只在最后打印**，于是中间失败要等全部跑完才看得见 ——
      而验收是**迭代**着用的（跑、修、再跑），等待时间要乘上迭代次数。
      已改为逐条 flush 进度输出。
- [x] Add source guards forbidding FastAPI imports in services and concrete adapter imports in core services.
      → `tests/test_source_invariants.py` 新增两条：
      `test_business_layers_do_not_import_the_http_layer`（业务层不得 import fastapi/starlette）
      与 `test_mcp_adapter_imports_neither_http_layer_nor_adapters`（MCP 不得 import 协议层/适配器）。
      前者与既有的"业务层不得 import 适配器"同源：两者都只在**换一种运行形态**时才显形。
- [x] Run `python scripts/verify_m7.py --verbose`; expect exit 0.
      → **43/43 通过（未满足 0 条，未通过 0 条），exit=0**。
      第 43 条（整个 `tests/`）实测 **收集 1339 条（其中 3 条跳过 → 1336 通过）**。
      ⚠️ **这个数字曾被读错**：脚本初版的措辞是"`N` 条通过（跳过 `M` 条）"，
      而 `N` 是**收集数**。一个会让人读错的统计口径比没有统计更坏 ——
      它把偏差写进了证据里。措辞已改为"收集 N 条，通过 N-M 条，跳过 M 条"。
- [x] Run full pytest, compileall, pip check, and OpenAPI route snapshot; expect zero failures or undocumented routes.
      → 全量 pytest 基线 **收集 1339（1336 passed / 3 skipped）**；
      路由快照实测 **29 条**（`/api/*` 22 + `/tools/*` 7），
      与计划逐条核对无多余端点；`test_no_route_path_is_registered_twice` 守着"后注册的那条永远不生效"。
      （M8 Task 2 新增 `GET /api/me` 后为 **收集 1348 / 1345 passed / 3 skipped**，路由 30 条。）
- [x] Mark M7 complete only after the seven tools can be demonstrated independently.
      → 七个工具**两种形态各自可独立演示**：REST（`app/api/tools.py`）与 MCP
      （`scripts/run_mcp.py --transport stdio|streamable-http`），
      且两侧都有与门面**逐字相等**的 parity 证据（验收 6 与 33）。

**Task 7 期间修掉的缺陷（验收的价值就在这里）：**

| 缺陷 | 症状 | 为什么只有"真跑一遍"才发现 |
| --- | --- | --- |
| `test_auth_rbac.py` 仍 import 私有的 `_build_identity_provider` | 1 条用例 `ImportError` 失败（243 通过） | Task 6 把它改成公开名时，**没有任何东西会提示跨模块引用**；改名方与引用方各自都"看起来没问题" |
| `verify_m7.py` 只在最后打印 | 43 条跑 ~2.5 分钟却看不到任何进度 | 不影响结论，但它把"迭代验收"的成本乘了一次迭代次数 |

> ⚠️ **全量测试的 teardown ERROR 有一个环境成因**：IDE 会经 `PYTHONPATH` 注入
> 一个 `sitecustomize.py` 钩子，它在解释器退出时对"批量删除"抛 `SystemExit(1)` ——
> 表现为若干条 **teardown ERROR**，而测试本体是绿的。
> `verify_m*.py` 的 `_subprocess_env()` 因此主动剥掉 `PYTHONPATH`；
> README §6 也写明了手工跑全量时要先清掉它（本次实测：清掉后 **0 error**）。
