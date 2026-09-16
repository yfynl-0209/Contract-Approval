# M9 PostgreSQL, Redis, MinIO, and Docker Compose Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the application against PostgreSQL 16, Redis, and MinIO with reversible Alembic migrations and a one-command Docker Compose development/prod-like stack.

**Architecture:** PostgreSQL is the durable source of truth for tasks, jobs, results, Outbox, and audit. Redis is an acceleration adapter for wake-ups, locks, and bounded caches; losing Redis must not erase business state. MinIO replaces local object storage behind the existing `ObjectStorage` interface. Containers use explicit health/readiness dependencies and private networks; only the frontend gateway is intended for later public exposure.

**Tech Stack:** PostgreSQL 16, Alembic, psycopg 3, Redis 7, MinIO, Docker Compose, Python 3.11, Node 20.

## Entry Gate and Dependency Boundary

- Database migration, Redis, MinIO, and Compose preparation may be designed independently, but a single Agent working in this checkout executes M6 → M7 → M8 → M9 serially to avoid overlapping edits.
- M9 end-to-end acceptance requires the M4 `PARSE`, M5 `RULE`, M6 `RESULT/WRITEBACK`, and M7 API paths to have passed their real queue-to-service integration tests.
- If an upstream gate is still open, M9 may report individual infrastructure checks only; it must not claim the whole application flow is complete.

## Global Constraints

- Preserve SQLite for fast tests and the requirement-mandated initialization path.
- `db/schema.sql`, SQLAlchemy models, and Alembic head must remain structurally equivalent where dialects permit.
- Redis is never the only copy of a job, result, lock owner, or delivery status.
- Buckets are private; browser access is authorized through M7, with short-lived presigned URLs only when policy allows.
- Database, Redis, MinIO, and internal API ports are not publicly exposed in production topology.
- No credentials or `.env` files are committed; Compose consumes environment variables and secret files.
- M9 changes infrastructure adapters, not M3–M8 business semantics.

---

## File Map

- Create `alembic.ini`, `alembic/env.py`, `alembic/versions/*`, `app/adapters/storage/minio_storage.py`, `app/adapters/queue/redis_notifier.py`.
- Create `Dockerfile.api`, `Dockerfile.worker`, `Dockerfile.frontend`, `docker-compose.yml`, `docker-compose.override.yml`.
- Create `scripts/wait_for_dependencies.py`, `scripts/verify_m9.py` and infrastructure tests.
- Modify `app/config.py`, `app/db.py`, `app/api/deps.py`, `scripts/run_worker.py`, `scripts/run_outbox_dispatcher.py`, `requirements.txt`, `.env.example`, `README.md`.

### Task 1: Database URL portability and PostgreSQL test fixture

**Files:**
- Modify: `app/config.py`, `app/db.py`, `requirements.txt`, `tests/conftest.py`
- Create: `tests/postgres/conftest.py`, `tests/postgres/test_postgres_smoke.py`

**Interfaces:**
- Produces one `DATABASE_URL` configuration and session factory for SQLite/PostgreSQL.

- [ ] Write a PostgreSQL smoke test for connection, transaction rollback, foreign keys, timestamps, and concurrent job claim.
- [ ] Add psycopg and configure connection health/pre-ping without SQLite-only arguments on PostgreSQL.
- [ ] Remove production reliance on PRAGMA and SQLite-specific SQL from runtime modules; keep dialect-specific behavior in adapters/tests.
- [ ] Run SQLite full tests and PostgreSQL smoke tests.

### Task 2: Alembic baseline and reversible migrations

**Files:**
- Create: `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/20260914_0001_baseline.py`
- Test: `tests/postgres/test_migrations.py`

**Interfaces:**
- Produces `alembic upgrade head`, `downgrade base`, and second `upgrade head`.

- [ ] Write a failing migration round-trip test against an empty temporary PostgreSQL database.
- [ ] Encode all current tables, checks, composite foreign keys, partial indexes, and server defaults explicitly; do not trust unchecked autogenerate output.
- [ ] Seed rules through a separate idempotent command, not migration side effects.
- [ ] Compare model metadata, SQLite schema, and PostgreSQL information schema for required columns/constraints/indexes.
- [ ] Run upgrade → inspect → downgrade → inspect empty → upgrade; expect success.

### Task 3: PostgreSQL concurrency semantics

**Files:**
- Modify: `app/worker.py`, `app/outbox.py`, relevant repositories
- Create: `tests/postgres/test_job_concurrency.py`, `test_outbox_concurrency.py`, `test_idempotency_concurrency.py`

**Interfaces:**
- Uses `SELECT ... FOR UPDATE SKIP LOCKED` on PostgreSQL while preserving a deterministic SQLite test path.

- [ ] Prove two workers cannot claim the same job or Outbox event.
- [ ] Prove concurrent result saves/writeback requests return one idempotent effect rather than leaking `IntegrityError`.
- [ ] Verify lease expiry uses database time consistently.
- [ ] Run concurrency tests repeatedly and retain counts/owner IDs in failure diagnostics.

### Task 4: MinIO ObjectStorage adapter

**Files:**
- Create: `app/adapters/storage/minio_storage.py`, `tests/contract/test_minio_storage.py`
- Modify: `app/api/deps.py`, `app/config.py`, `requirements.txt`

**Interfaces:**
- Implements existing `ObjectStorage` interface without changing callers.

- [ ] Reuse the storage contract suite for put/get/exists/delete, digest, content type, traversal resistance, and missing object semantics.
- [ ] Create/check a private bucket at startup with bounded retries; never make it public.
- [ ] Support streaming and optional short-lived presign behind authorized M7 delivery.
- [ ] **Extend the `ObjectStorage` port with ranged/streamed reads (`stat()` / `open_stream()` / `read_range()`) and switch `app/api/attachments.py::_read_object` to them.**
      **Why this is a required deliverable, not a nice-to-have:** M7 ships attachment
      delivery as *"read the whole object into memory, then slice by `Range`"* —
      the port only has `get(key) -> bytes`. That is acceptable in M7 because
      `attachment_max_bytes` (20 MB) bounds it, but **the bound is configuration,
      not architecture**: raising it silently raises per-request memory instead of
      failing. Without this item, migrating to MinIO only moves *where the bytes
      are stored* while the read path keeps downloading everything into the API
      process. The debt is recorded in `app/api/attachments.py`'s module docstring
      under "当前实现是'限制大小后整份读取并分片响应'".
      ⚠️ Do **not** describe the M7 implementation as "streaming from
      `ObjectStorage`" — it is not, and that wording is what lets this item get
      dropped.
- [ ] Run the same contract against local storage and MinIO.

### Task 5: Redis notifier, locks, and cache

**Files:**
- Create: `app/ports/job_notifier.py`, `app/adapters/queue/redis_notifier.py`
- Modify: job creation and worker polling composition
- Test: `tests/contract/test_job_notifier.py`, `tests/integration/test_redis_recovery.py`

**Interfaces:**
- Produces `notify(job_id)`, `wait(timeout)`, and bounded cache helpers; PostgreSQL remains authoritative.

- [ ] Test notification wakes a worker but the worker still claims/validates the PostgreSQL row.
- [ ] Stop Redis after a committed job insert and prove DB polling eventually processes the job.
- [ ] Delete Redis cache and prove results/history remain available.
- [ ] Namespace every key by environment/tenant and set TTL on locks/caches.
- [ ] Do not introduce Celery unless it replaces, rather than duplicates, the existing Worker semantics and passes all lease/idempotency tests.

### Task 6: Container images and Compose topology

**Files:**
- Create: `Dockerfile.api`, `Dockerfile.worker`, `Dockerfile.frontend`, `docker-compose.yml`, `docker-compose.override.yml`, `.dockerignore`

**Interfaces:**
- Produces services: `postgres`, `redis`, `minio`, `minio-init`, `mock-approval`, `api`, `worker`, `outbox-dispatcher`, `frontend`.

- [ ] Build non-root, pinned-runtime images with separate API/worker commands and no source `.env`, tests, caches, or virtualenv copied into runtime layers.
- [ ] Add named volumes for PostgreSQL/MinIO and health checks for every dependency.
- [ ] Use health/readiness conditions and bounded startup retry; do not rely on fixed sleeps.
- [ ] Put stateful/internal services on a private network; development port mappings live in the override file.
- [ ] Run `docker compose config` and image builds; expect exit 0.

### Task 7: Health, readiness, initialization, and graceful shutdown

**Files:**
- Modify: `app/main.py`, `scripts/run_worker.py`, `scripts/run_outbox_dispatcher.py`
- Create: `scripts/wait_for_dependencies.py`, `tests/integration/test_health_dependencies.py`

**Interfaces:**
- Produces `/health/live`, `/health/ready`, `/health/dependencies` with different semantics.

- [ ] Liveness must not fail merely because Redis/MinIO is briefly unavailable; readiness must fail when required durable dependencies are unusable.
- [ ] Dependency health reports sanitized status/latency without connection strings or credentials.
- [ ] Apply migrations and idempotent seed as explicit one-shot Compose jobs before API readiness.
- [ ] Verify SIGTERM stops claiming new work, finishes or releases current leases, and exits within the configured grace period.

### Task 8: M9 integration and migration acceptance

**Files:**
- Create: `scripts/verify_m9.py`, `tests/integration/test_compose_flow.py`
- Modify: `.env.example`, `README.md`, `合同审批审查系统-项目计划.md`

- [ ] Start the complete stack with one command and wait on readiness conditions.
- [ ] Run a real flow against PostgreSQL/MinIO/Redis: pull → download → parse fixture → rules → save/confirm → Outbox writeback.
- [ ] Restart API, worker, Redis, and MinIO individually; prove committed history survives and pending work resumes.
- [ ] Execute Alembic upgrade/downgrade/upgrade and structural parity tests.
- [ ] Inspect exposed ports and prove only documented development mappings exist; production profile exposes no database/Redis/MinIO port.
- [ ] Run `python scripts/verify_m9.py --verbose`; require fail-closed exit 0, then run all SQLite and PostgreSQL tests.
- [ ] Record exact image tags, schema revision, commands, and measured results before marking M9 complete.
