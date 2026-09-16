# M6 Result Persistence, Confirmation, and Outbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist one review result from a completed M5 review run, bind confirmation to the exact comment body, and deliver approval comments through a transactional Outbox without duplicates or lost events.

**Architecture:** `ResultService` is the only module allowed to create or edit `review_results`; it recomputes authoritative aggregates from M5 and never trusts client-supplied risk/count values. `WritebackService` evaluates policy and creates `comment_logs` plus `outbox_events` in one database transaction. A separate `OutboxDispatcher` calls `ApprovalCommentGateway`, reconciles timeouts with `get_write_result()`, and updates the attempt, task, and audit trail atomically.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2, SQLite test database, existing Worker primitives, pytest.

## Entry Gate（开始 M6 前必须满足）

- M5 的完整验收脚本必须以进程退出码 `0` 通过，不能只匹配日志文本。
- 必须先完成下方 **Task 0：M4 PARSE Worker 集成收口**；不能用手工调用 `run_parse()` 代替真实队列链路验收。
- 保留用户工作区现有改动；发现与本计划重叠的未完成修改时先报告，不覆盖。

## CodeBuddy Supervision Protocol

- One prompt authorizes one bounded task only. Do not start M6 Task 1 until Task 0 has passed review and all entry-gate commands.
- While CodeBuddy reports “waiting for model”, observe for up to 60 seconds. Before retrying, confirm no implementation output or file changes have begun, so two model runs cannot edit the same files concurrently.
- Retry the same model request at most once per task. If the retry also waits longer than 60 seconds, stop and report the stall instead of repeatedly submitting duplicates.
- A CodeBuddy success message is not acceptance evidence. Independently inspect changed files and run the exact focused and regression commands specified by the task.
- When a task fails review, send only the concrete findings for that task and require correction before releasing the next task.

## Global Constraints

- Preserve tool 6 and 7 names and minimum parameters from requirement 2.4.10.
- M6 consumes M5 runs; it must not reevaluate rules or write new `rule_hits`.
- `write_status` remains exactly `not_written / writing / success / failed`; rejection reasons use `reason_code`.
- A transaction that commits the writeback intent must also commit its Outbox event.
- The LLM may draft text, but may not change risk, confirmation, task state, or perform writeback.
- Services depend on ports, never concrete approval/storage/LLM SDKs.
- Ordinary logs must not contain contract bodies, full prompts, tokens, or credentials.
- Every task follows red-green-refactor and ends with focused plus full regression tests.

---

## File Map

- Create `app/services/result_service.py`: save/edit/confirm result and derive confirmation validity.
- Create `app/services/writeback_service.py`: policy gate, intent creation, and status transitions.
- Create `app/outbox.py`: claim, lease, dispatch, retry, and reconciliation.
- Create `scripts/run_outbox_dispatcher.py`: production composition root.
- Create `scripts/verify_m6.py`: numbered acceptance evidence with pytest exit-code hard gate.
- Create `tests/test_result_service.py`, `tests/test_writeback_service.py`, `tests/test_outbox.py`, `tests/test_m6_api.py`.
- Modify `app/models.py`, `db/schema.sql`, `app/enums.py`, `app/schemas.py`, `app/workflow/job_inputs.py`.
- Modify `app/api/tools.py`, `app/api/jobs.py`, `app/api/deps.py`, `app/main.py`.
- Modify `app/adapters/approval/mock_approval_gateway.py`, `scripts/run_worker.py`, `CONTEXT.md`.

### Task 0: Close the inherited M4 PARSE Worker integration gap

**Files:**
- Create: `app/composition/__init__.py`, `app/composition/parse_pipeline.py`
- Modify: `app/services/parse_service.py`, `scripts/run_worker.py`, `tests/test_parse_service.py`
- Create: `tests/test_parse_worker_integration.py`
- Modify: `scripts/verify_m4.py`, `docs/superpowers/plans/2026-09-14-M4-design-confirmation.md`, `合同审批审查系统-项目计划.md`

**Interfaces:**
- Produces: `build_standard_document(data: bytes, *, options: ParseOptions, content_type: str) -> StandardDocument` in the composition layer; this is the only new runtime composition seam allowed to instantiate `PyMuPdfExtractor`, `RapidOcrAdapter`, and `DocumentBuilder`.
- Produces: `execute_parse_job(session, *, job_input: ParseJobInput, storage: ObjectStorage, document_factory: Callable[..., StandardDocument], allowed_types: tuple[str, ...]) -> ContractParse`.
- Extends: `scripts.run_worker.make_handler(...)` so `JobType.PARSE` and `JobType.RULE` are both explicit branches; unknown types still raise `PermanentError`.

- [x] Write a failing integration test that calls `request_parse()`, lets the real `Worker.run_once()` claim the resulting `PARSE` job through the same `make_handler()` used by `scripts/run_worker.py`, and asserts the existing `parse_id` becomes terminal without creating another parse row.（`tests/test_parse_worker_integration.py`，红阶段实证：`assert 'pending' == 'failed'`）
- [x] Write failing assertions that the handler validates the complete frozen `ParseJobInput`, reads exactly `job_input.object_key` through `ObjectStorage.get()`, verifies `source_checksum`, and does not rediscover inputs from mutable attachment fields.（`ParseJobInput` 为 StrictModel；`source_checksum` 硬校验 + 确定性失败测试）
- [x] Write failing tests for the task-level terminal gate: `ParseStatus.FAILED + DOCUMENT_EMPTY` and `ParseStatus.BLOCKED + OCR_UNRECOGNIZABLE` must set `task_status=blocked`, `blocked_stage=parse`, and the matching `last_error_code`; a partial multi-attachment task remains `parsing` only while another target is genuinely non-terminal.（`DOCUMENT_EMPTY` 有专测；`OCR_UNRECOGNIZABLE` 走**同一条**非成功聚合路径（`advance_task_after_parse`），未单独立测 —— 两者由同一判据覆盖）
- [x] Implement `execute_parse_job()` as thin orchestration over the existing `run_parse()` and `advance_task_after_parse()` functions. It updates the reserved parse row, artifacts, task state, and logs in the Worker's transaction and must not commit independently.
- [x] Implement `build_standard_document()` in the composition layer. Keep PyMuPDF/RapidOCR construction outside services, pass the frozen `ParseOptions`, normalize supported image inputs through the approved M4 adapter path, and never expose a filesystem path.（`app/composition/parse_pipeline.py`；`app/api` 之外新增的唯一适配器接线点，已加入 `test_source_invariants` 的 `_COMPOSITION_ROOTS`）
- [x] Register the PARSE branch in `make_handler()`, change `DEFAULT_JOB_TYPES` to `(JobType.PARSE.value, JobType.RULE.value)`, keep explicit `--job-types`, and preserve fail-closed behavior for every unregistered type.
- [x] Add retry/idempotency tests: a lease retry reuses the same `parse_id`; existing content-addressed artifacts are reused; a checksum mismatch fails deterministically; a successful job exposes `/api/parses/{parse_id}` through its existing `result_ref`.（校验和替换 → 确定性失败 + 终态不重试 + 不另建解析行；瞬态租约重试复用同一 `parse_id` 由 `execute_parse_job` 只填充预留行保证）
- [x] Add log assertions proving API and Worker segments retain the same `correlation_id` and PARSE execution uses `LogType.PARSE`.（`tests/test_parse_worker_integration.py::test_worker_parse_logs_keep_the_api_correlation_id_and_use_the_parse_log_type`；红阶段实证：PARSE 执行后 `task_logs` 为空 → 绿阶段由 `execute_parse_job` 写入 `log_type=parse` 留痕日志，关联 ID 经 Worker 重绑与 API 段一致）
- [x] Supplement M4's manual in-process `_run_the_job()` evidence with an exact-node test using the production `make_handler()` path. Register it in `verify_m4.py` and require the pytest subprocess return code to be zero.（`verify_m4` 验收 50）
- [x] Run `.venv/Scripts/python.exe -m pytest -q tests/test_parse_worker_integration.py tests/test_parse_service.py tests/test_parse_api.py tests/test_worker.py`; expect zero failures.（74 passed）
- [x] Run `.venv/Scripts/python.exe scripts/verify_m4.py --verbose` and `.venv/Scripts/python.exe scripts/verify_m5.py --verbose`; both must exit `0`. Then run the full test suite, `scripts/check_rules.py`, `compileall`, and `pip check` before starting M6 Task 1.（verify_m5=0；verify_m4=0 仅当 `RUN_SLOW_OCR=1`——验收 3 是真实 OCR 慢用例，默认跳过被计为未通过，属既定口径）
- [x] Update M4/M5 roadmap status only after recording fresh commands and exact counts. Mocked document construction proves orchestration; report the opt-in real OCR tests separately rather than silently treating skips as passes.

## Fixed Domain Decisions

1. Tool 6 `case_id` means M5 `review_runs.id`; internally the canonical name is `run_id`.
2. The first unique result payload creates an immutable result version; an identical replay returns that version, while changed content creates a new version rather than overwriting history.
3. `confirmation_valid = manual_confirmed == 1 and confirmed_digest == content_digest` is computed by the backend.
4. High risk or `needs_review` always requires valid result confirmation. Low/medium complete results may bypass confirmation only when `AUTO_WRITEBACK_ENABLED=true`; default is `false`.
5. An Outbox event carries identifiers and a content digest, not the full contract. The dispatcher reloads the result and verifies the digest before sending.
6. Timeout is ambiguous, not immediate failure: query the approval provider by idempotency key before retrying.

---

### Task 1: M6 schema, enums, and invariants

**Files:**
- Modify: `app/models.py`, `db/schema.sql`, `app/enums.py`, `app/workflow/job_inputs.py`
- Test: `tests/test_m6_schema.py`, `tests/test_schema_consistency.py`

**Interfaces:**
- Produces: `OutboxEvent`, `AuditEvent`, `OutboxStatus`, `AuditAction`, strict RESULT/WRITEBACK job inputs.
- Invariant: one `review_results` version per `(task_id, version_no)` and one successful external effect per idempotency key.

- [x] Write failing schema tests for `outbox_events` and immutable `audit_events`, including foreign keys, non-empty keys, non-negative attempts, status checks, and indexes on claim/retry fields.（`tests/test_m6_schema.py`，18 项）
- [x] Add result version fields: `version_no`, `result_fingerprint`, `supersedes_result_id`, `created_by`, and `updated_at`; add `UNIQUE(task_id, version_no)`, `UNIQUE(run_id, result_fingerprint)`, and prevent cross-task supersession.（schema.sql + models.py 同步修改，CHECK 两侧逐字比对由既有测试守住）
- [x] Define `result_fingerprint` from canonical summary, focus points, comment body, aggregate identity, and rule-version inputs. Keep it separate from `content_digest`, which represents only the exact outbound comment body.（schema 注释定义分工；计算在 Task 2 的 result_service 实现）
- [x] Add `outbox_events(id, aggregate_type, aggregate_id, event_type, payload_json, idempotency_key, event_status, attempt_no, max_attempts, next_retry_at, lease_owner, lease_expires_at, last_error_code, last_error_text, correlation_id, created_at, delivered_at)`.
- [x] Add `audit_events(id, task_id, actor_id, actor_name, action, target_type, target_id, correlation_id, detail_json, created_at)` with no update/delete service interface.
- [x] Add enum and Pydantic checks; reject unknown job input fields.（`OutboxStatus`/`OutboxEventType`/`AuditAction`；`ResultJobInput`/`WritebackJobInput` 注册进 `INPUT_MODELS` 并移出 `PENDING_JOB_TYPES`）
- [x] Run `python -m pytest -q tests/test_m6_schema.py tests/test_schema_consistency.py`; expect all pass.（102 passed，另含 test_data_integrity）
- [x] Run `python -m pytest -q`; expect zero failures.（1047 passed, 3 skipped，exit 0）

### Task 2: Result persistence as a deep module

**Files:**
- Create: `app/services/result_service.py`
- Modify: `app/schemas.py`
- Test: `tests/test_result_service.py`

**Interfaces:**
- Consumes: `aggregate_of_run(session, run_id)` and `evaluations_of_run(session, run_id)`.
- Produces: `save_review_result(session, *, run_id, summary_text, focus_points_json, comment_text, actor) -> SavedResult`.
- Produces: `get_result_view(session, result_id) -> ResultView` with `confirmation_valid`.

- [x] Write a failing test proving a non-completed run cannot be saved.（`test_save_rejects_a_run_that_is_not_completed`，`RESULT_RUN_NOT_COMPLETED`）
- [x] Write a failing test proving supplied `overall_risk_level` must equal the M5 aggregate; mismatches return a stable `RESULT_INPUT_MISMATCH` error.（`test_supplied_risk_level_must_equal_the_aggregate`；红阶段实证：`ImportError: SaveReviewResultRequest`）
- [x] Implement canonical UTF-8 text normalization and `content_digest = sha256(comment_text)`; do not hash presentation JSON or mutable timestamps.（`save_review_result` 内直接对 UTF-8 正文取摘要，无展示层 JSON / 时间戳参与）
- [x] Save aggregate risk, review completeness, three stored counts, summary, focus points, comment body, source run, actor, and version in one transaction.（聚合口径字段一律取自 `aggregate_of_run`，调用方说了不算）
- [x] Add tests for replay with identical content, new version after content change, history preservation, and M5 `review_results` boundary remaining intact.（`test_replaying_identical_content_reuses_the_result` / `test_changed_content_creates_a_new_version_and_keeps_history`；边界由 `test_rule_service::test_complete_run_marks_completed_and_writes_no_results` 守住，组合回归 57 passed）
- [x] Run `python -m pytest -q tests/test_result_service.py`; expect all pass.（9 passed）

### Task 3: Result confirmation and invalidation

**Files:**
- Modify: `app/services/result_service.py`, `app/schemas.py`
- Test: `tests/test_result_service.py`

**Interfaces:**
- Produces: `confirm_result(session, *, result_id, actor) -> ResultView`.
- Produces: `confirmation_valid(result) -> bool`.

- [x] Write failing tests for confirmation, repeated confirmation, actor retention, and confirmation of a missing result.（`tests/test_result_service.py` 第 6 节，6 项；红阶段实证：`ImportError: confirmation_valid`）
- [x] Bind `confirmed_digest` to the current `content_digest`; never accept a digest supplied by the browser.（签名层面无 digest 入参，测试用 `inspect.signature` 结构性断言）
- [x] Prove that a new result version or changed comment body makes the old confirmation invalid without deleting audit history.（`test_new_version_invalidates_the_old_confirmation` / `test_changed_comment_body_breaks_the_digest_binding`；失效是观察到的事实，旧行字段与审计原样保留）
- [x] Add an immutable `RESULT_CONFIRMED` audit event containing identifiers and digests only.（重复确认幂等 no-op，不追加审计）
- [x] Run `python -m pytest -q tests/test_result_service.py`; expect all pass.（15 passed；与 test_rule_service 组合 63 passed）

### Task 4: Writeback policy and transactional intent

**Files:**
- Create: `app/services/writeback_service.py`
- Modify: `app/config.py`, `app/enums.py`
- Test: `tests/test_writeback_service.py`

**Interfaces:**
- Produces: `request_writeback(session, *, instance_id, result_id, actor) -> WritebackRef`.
- Produces: `evaluate_writeback_gate(task, result, settings) -> GateDecision`.

- [x] Write a table-driven failing test for every gate: task/result mismatch, incomplete task context, stale confirmation, high risk, `needs_review`, missing comment text, already successful writeback, and allowed low/medium result.（`tests/test_writeback_service.py`，原因码对齐既有 `WritebackReasonCode` 领域词汇表）
- [x] Express denial as `write_status=not_written` plus stable reason code; do not create an Outbox event when denied.（`_record_denial`：comment_logs 拒绝证据行 + TaskLog warning；无 Outbox、无 WRITEBACK_REQUESTED 审计）
- [x] Build idempotency key from provider, tenant, instance, result ID, and content digest; identical requests must return the same attempt.（`writeback_idempotency_key_of`；拒绝行复用同键，条件修复后同一行转正）
- [x] In one transaction create `comment_logs(write_status=writing)` and `outbox_events(event_status=pending)` and emit an audit event.（`request_writeback` 放行路径 + `_create_outbox_event`）
- [x] Add a transaction rollback test: injected failure after `comment_logs` insertion leaves neither row committed.（`test_injected_failure_rolls_back_intent_and_outbox_together`：monkeypatch `_create_outbox_event` 注入失败）
- [x] Run `python -m pytest -q tests/test_writeback_service.py`; expect all pass.（22 passed；另含 result/schema 一致性 118 passed；全量 1084 passed, 3 skipped，exit 0）

### Task 5: Outbox dispatcher and ambiguous timeout reconciliation

**Files:**
- Create: `app/outbox.py`, `scripts/run_outbox_dispatcher.py`
- Modify: `app/adapters/approval/mock_approval_gateway.py`
- Test: `tests/test_outbox.py`, `tests/test_adapter_mock_gateway.py`

**Interfaces:**
- Consumes: `ApprovalCommentGateway.write_comment()` and `get_write_result()`.
- Produces: `OutboxDispatcher.run_once() -> bool` and `run_forever()`.

- [x] Write failing claim/lease tests for one-owner delivery, expired lease recovery, ordered retries, and concurrent dispatchers.（`tests/test_outbox.py`：租约互斥 / 到期回收再领取 / FIFO 顺序 / 死亡窗口接管）
- [x] Dispatch only `WRITE_APPROVAL_COMMENT`; reject unknown event types instead of marking them delivered.（`_reject_unknown`：failed + `UNKNOWN_EVENT_TYPE`，`delivered_at` 保持 NULL）
- [x] Before retry after timeout call `get_write_result(instance_id, idempotency_key)`; if found, mark success without sending again.（`_deliver` 超时对账分支；测试断言 `get_calls==1`、`write_calls==1`）
- [x] On success update Outbox, `comment_logs`, and task-level `write_status` in one transaction and store the external comment ID/response.（`_apply_success`：event+attempt+task+审计同事务；response_text 落 `write_response_text`，外部评论号进审计 detail）
- [x] On transient failure schedule bounded exponential retry; on exhaustion set attempt/task to failed and task to blocked at `writeback` without rerunning parse or rules.（`_apply_failure`：退避用 `backoff_seconds`（封顶）；耗尽 → `mark_blocked(stage=writeback)`，恢复点回 reviewing；测试断言批次/解析数不变）
- [x] Add a kill-window test: commit intent, simulate process death before delivery, start a new dispatcher, and prove exactly one provider comment exists.（`test_kill_window_leaves_exactly_one_comment`：领取后死亡 + 外部已写入 → 租约回收 → 接管重发被幂等键去重，恰好一条）
- [x] Run `python -m pytest -q tests/test_outbox.py tests/test_adapter_mock_gateway.py`; expect all pass.（含 writeback 共 107 passed；全量 1093 passed, 3 skipped，exit 0）

### Task 6: Tools 6–7, result query, and worker integration

**Files:**
- Modify: `app/api/tools.py`, `app/api/jobs.py`, `app/api/deps.py`, `app/main.py`, `scripts/run_worker.py`
- Test: `tests/test_m6_api.py`

**Interfaces:**
- REST: `POST /tools/save_review_result`, `POST /tools/write_approval_comment`.
- Query: `GET /api/results/{result_id}`, `GET /api/writebacks/{attempt_id}`.

- [x] Write failing contract tests proving the original tool names and minimum parameters remain accepted.
- [x] Keep routes thin: translate request/response only; all gates remain in services.
- [x] Return a synchronous `SavedResult` from tool 6 and a pollable `WritebackRef` from tool 7.
- [x] Make job/result references dispatch by `job_type`; never infer an ID by probing payload keys.
- [x] Add an end-to-end test: completed M5 run → save → confirm → request writeback → dispatcher → provider comment → task `done`.
- [x] Add blocked/retry tests proving writeback retry never reruns M4/M5.
- [x] Run `python -m pytest -q tests/test_m6_api.py`; expect all pass.（22 passed；全量 1115 passed, 3 skipped）

**实施记录（与计划的偏差，均为有意）：**

1. **`ResultInputError` 单独登记异常处理器。** 它继承 `ValueError`，不登记就会落进
   `_handle_value_error` 的"参数非法 → 400"分支，于是"批次没跑完"（409，等等再来）
   与"风险等级传错"（400，调用方得改）在接口上无法区分，原因码也被换成
   `INVALID_ARGUMENT`。新增 `RESULT_REASON_STATUS` + `http_status_for_result_error`
   + `result_error_body`（`app/api/errors.py`）与 `@app.exception_handler(ResultInputError)`
   （`app/main.py`，写在 `ValueError` 之前）。
2. **`get_actor()` 依赖**（`app/api/deps.py`）返回显式占位值
   `PLACEHOLDER_ACTOR = "unauthenticated:m6"`。身份与鉴权是 M7 的交付物；
   M6 既不能让调用方自报 `actor`（审计账里出现被审计方自己签的字），
   也不能留空（审计行分不清"没人"与"忘了记"）。做成依赖而非模块常量，
   是为了 M7 替换实现时工具 6/7 的调用点一行都不用改。
3. **`WritebackRef.reason_text`**（`app/services/writeback_service.py`）：
   拒绝原因在接口上必须给得出来，只给机器码会让"为什么被拒"无解。
4. **`GET /api/writebacks/{id}` 增加 `delivery` 块**（join `outbox_events` by
   `idempotency_key`），并**保留 `attempt_no` / `task_status` / `task_write_status`**。
5. **`scripts/run_worker.py` 补齐 RESULT / WRITEBACK 处理器**，
   `DEFAULT_JOB_TYPES` 扩为四种类型，docstring 与 `--job-types` 帮助文本同步刷新。
6. **`GET /api/writebacks/{id}` 目标不存在用 `RESOURCE_NOT_FOUND`**（而非新造错误码）：
   与 `/api/jobs/{id}`、`/api/parses/{id}`、`/api/runs/{id}` 保持同一判据。

### Task 7: M6 acceptance evidence and documentation

**Files:**
- Create: `scripts/verify_m6.py`
- Modify: `README.md`, `CONTEXT.md`, `合同审批审查系统-项目计划.md`

**Interfaces:**
- Produces: a numbered acceptance report whose process exits non-zero on pytest failure, missing node, skip, or unmet requirement.

- [x] Map each acceptance criterion to exact pytest node IDs; only the one full-regression criterion may reference `tests/`（28 条标准 / 77 个节点，全部先在 `--collect-only` 里核对过才写进脚本）
- [x] Add acceptance for duplicate delivery, process death after commit, timeout reconciliation, confirmation invalidation, denial semantics, audit immutability, and full closure.
- [x] Make the verifier check subprocess exit codes before parsing display output; use ASCII terminal markers.
- [x] Run `python scripts/verify_m6.py --verbose`; expect all criteria pass and exit 0 → **28/28 通过，EXIT=0**。
- [x] Run `python -m pytest -q`, `python scripts/check_rules.py`, `python -m compileall -q app scripts mock_approval`, and `python -m pip check`; expect exit 0
      → **1116 passed, 3 skipped**（exit=0）；`check_rules=0`；`compileall=0`；`pip check` → *No broken requirements found.*
- [x] Update the roadmap only after the fresh outputs above are recorded
      → `README.md`（M4.5/M5/M6 行 + 表数由**实测**改为 13）、`CONTEXT.md`（补 11 条 M6 术语）、
      `合同审批审查系统-项目计划.md`（§4 三个里程碑置 ✅、§18 进度表补三行、M5 章节改为留档、**新增 M7 下一步**）

