"""故障演练（M3 / T9）：用真实 mock 的故障注入，端到端打穿四层。

## 为什么必须用**真实故障**

一次注入的故障要穿过四层才算真被处理：

```text
mock 注入 HTTP 500 / 404 / 超时
  → 适配器映射成带稳定错误码的端口异常        ← 分类
  → 服务据此决定 retry_wait 还是 blocked      ← 决策
  → 作业与任务状态落库                         ← 留痕
  → REST 返回 503 / 200+blocked               ← 告知
```

任何一层偏掉，最终表现都是"任务莫名卡住"——最难排查的那种。
单测用**假异常**只能证明后三层；第一层的映射
（HTTP 状态码 → 错误码 → 可重试性）**必须用真故障来验**。

## 为什么起真实进程

适配器的 `timeout` 只在**真实套接字**上生效：ASGI 直连或 `MockTransport`
都不走 socket，超时永远不会触发 —— 那样演练就退化成"又跑了一遍假异常"。
因此本文件起真 uvicorn，用真端口、真 Bearer 鉴权、真故障。

## 同一个 404，两种含义

| 场景 | 错误码 | 语义 |
| --- | --- | --- |
| 查审批单 404 | `INSTANCE_NOT_FOUND` | 单号错了 |
| 下载附件 404 | `ATTACHMENT_MISSING` | 附件被删了 |

两种含义必须保留，否则排障时分不清该去查单号还是查附件。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.adapters.approval.mock_approval_gateway import MockApprovalGateway
from app.adapters.storage.local_file_storage import LocalFileStorage
from app.auth import Actor, Role
from app.api.deps import get_actor, get_db, get_gateway, get_storage
from app.config import PROJECT_ROOT, settings
from app.db import Base, transactional_session
from app.enums import JobStatus, JobType, TaskStatus
from app.main import app
from app.models import ApprovalAttachment, ApprovalTask, WorkflowJob


#: 全权限测试主体（`system_admin` 是唯一覆盖全部 8 项权限的角色）。
#:
#: 本文件测的是**业务行为**，不是身份与授权 —— 装一个全权限主体，
#: 是为了让身份层在测试里"透明"：某个端点日后新增一条权限要求时，
#: 这里不会因为与测试目标无关的原因变红（那种红只会让人去改断言，
#: 而不是去看真正的问题）。
#:
#: ⚠️ **代价必须说清楚**：这些用例**不覆盖**"无权限的人被拒绝"。
#: 401 / 403 / 令牌校验 / 角色映射在 `tests/test_auth_rbac.py` 里验。
_FULL_ACCESS_ACTOR = Actor(
    actor_id="test-actor",
    display_name="test-actor",
    roles=frozenset({Role.SYSTEM_ADMIN.value}),
    tenant_id="default",
)

#: mock 的故障目标（与 `mock_approval/fault_inject.py` 的 TARGETS 对应）
PULL_TARGET = "list_pending"
DETAIL_TARGET = "get_detail"
DOWNLOAD_TARGET = "download"

#: mock 内置夹具里的审批单与附件
FIXTURE_INSTANCE = "HT-2026-0001"
FIXTURE_ATTACHMENT = "A-1001"


# ============================================================
# 真实 mock 进程
# ============================================================


def _free_port() -> int:
    """让内核分配一个空闲端口。

    不用固定端口：开发时手动起的 mock 就在 8001，
    端口冲突表现为"连不上"，与"服务没起来"难以区分，会浪费大量排查时间。
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(base_url: str, timeout: float = 60.0) -> None:
    """轮询健康检查直到 mock 可用。

    不用固定 `sleep`：机器快慢不同，睡短了会偶发失败、睡长了每次都要白等。
    """
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError as exc:
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f"mock 审批系统未能在 {timeout}s 内就绪：{last!r}")


@pytest.fixture(scope="module")
def mock_base_url() -> Iterator[str]:
    """起一个真实的 mock 审批系统进程。"""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "mock_approval.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        _wait_ready(base_url)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            process.kill()
            process.wait(timeout=5)


class MockFaults:
    """通过 HTTP 控制 mock 的故障注入。"""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.mock_approval_token}"}

    def inject(
        self,
        target: str,
        mode: str,
        *,
        instance_id: str | None = None,
        delay_seconds: float = 5.0,
        message: str = "",
    ) -> None:
        response = httpx.post(
            f"{self._base_url}/api/faults",
            headers=self._headers,
            json={
                "target": target,
                "mode": mode,
                "instance_id": instance_id,
                "delay_seconds": delay_seconds,
                "message": message,
            },
            timeout=10.0,
        )
        response.raise_for_status()

    def clear(self) -> None:
        httpx.delete(
            f"{self._base_url}/api/faults", headers=self._headers, timeout=10.0
        ).raise_for_status()

    def active(self) -> list[dict]:
        response = httpx.get(
            f"{self._base_url}/api/faults", headers=self._headers, timeout=10.0
        )
        response.raise_for_status()
        return response.json()["items"]


@pytest.fixture()
def faults(mock_base_url: str) -> Iterator[MockFaults]:
    """故障控制器：**每个测试前后都清空**。

    故障不清空会泄漏到后续测试，表现为"另一处测试莫名失败" ——
    这类跨测试串扰排查起来极其耗时，宁可在夹具里强制清零。
    """
    controller = MockFaults(mock_base_url)
    controller.clear()
    try:
        yield controller
    finally:
        controller.clear()


# ============================================================
# 演练台：真实网关 + 临时库 + 临时存储
# ============================================================


class DrillHarness:
    """与生产装配只差数据库与存储的路径。"""

    def __init__(self, work_dir: Path, base_url: str) -> None:
        self.base_url = base_url
        self.engine = create_engine(
            f"sqlite:///{(work_dir / 'drill.db').as_posix()}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False, future=True
        )
        self.gateway = MockApprovalGateway(
            base_url=base_url,
            token=settings.mock_approval_token,
            timeout=10.0,
        )
        self.storage = LocalFileStorage(work_dir / "objects")
        self.client = TestClient(app)

    # ---------- 装配 ----------

    def install(self) -> None:
        app.dependency_overrides[get_db] = self._session_dependency
        app.dependency_overrides[get_gateway] = lambda: self.gateway
        # 身份层在本文件里"透明"（理由见 _FULL_ACCESS_ACTOR 的注释）
        app.dependency_overrides[get_actor] = lambda: _FULL_ACCESS_ACTOR
        app.dependency_overrides[get_storage] = lambda: self.storage

    def uninstall(self) -> None:
        app.dependency_overrides.clear()
        self.gateway.close()
        self.engine.dispose()

    def _session_dependency(self):
        # 复用生产的**同一份**事务边界（见 app/db.py）
        yield from transactional_session(self.session_factory())

    def use_gateway_timeout(self, seconds: float) -> None:
        """换一个超时更短的网关。

        真实 socket 才会触发超时；这是"超时分类"能被真实验证的前提。
        """
        self.gateway.close()
        self.gateway = MockApprovalGateway(
            base_url=self.base_url,
            token=settings.mock_approval_token,
            timeout=seconds,
        )

    # ---------- 观察窗口 ----------

    def seed_task(self, instance_id: str = FIXTURE_INSTANCE) -> int:
        """直接建任务，跳过拉取。

        工具 3 只要求任务存在（附件记录可缺失，成功时补建），
        因此这里不必先跑一次完整拉取。
        """
        with self.session_factory() as session:
            task = ApprovalTask(
                provider="mock",
                tenant_id="default",
                instance_id=instance_id,
                approval_code=instance_id,
                context_status="complete",
            )
            session.add(task)
            session.commit()
            return task.id

    def tasks(self) -> list[ApprovalTask]:
        with self.session_factory() as session:
            return list(session.execute(select(ApprovalTask)).scalars().all())

    def jobs(self, job_type: JobType | None = None) -> list[WorkflowJob]:
        with self.session_factory() as session:
            statement = select(WorkflowJob)
            if job_type is not None:
                statement = statement.where(WorkflowJob.job_type == job_type.value)
            return list(session.execute(statement).scalars().all())

    def only_job(self, job_type: JobType | None = None) -> WorkflowJob:
        found = self.jobs(job_type)
        assert len(found) == 1, f"期望恰好 1 个作业，实际 {len(found)} 个"
        return found[0]

    def attachments(self) -> list[ApprovalAttachment]:
        with self.session_factory() as session:
            return list(session.execute(select(ApprovalAttachment)).scalars().all())

    # ---------- 调用 ----------

    def download(self, attachment_id: str = FIXTURE_ATTACHMENT) -> httpx.Response:
        return self.client.post(
            "/tools/download_contract_attachment",
            json={"instance_id": FIXTURE_INSTANCE, "attachment_id": attachment_id},
        )

    def pull(self) -> httpx.Response:
        return self.client.post(
            "/tools/list_pending_contract_approvals", json={"limit": 20}
        )

    def detail(self, instance_id: str = FIXTURE_INSTANCE) -> httpx.Response:
        return self.client.post(
            "/tools/get_contract_approval", json={"instance_id": instance_id}
        )


@pytest.fixture()
def harness(
    work_dir: Path, mock_base_url: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[DrillHarness]:
    monkeypatch.setattr(settings, "storage_root", str(work_dir / "storage"))
    built = DrillHarness(work_dir, mock_base_url)
    built.install()
    try:
        yield built
    finally:
        built.uninstall()


# ============================================================
# 1. 瞬时故障：退避重试，不阻塞任务
# ============================================================


def test_injected_500_is_classified_retryable(
    harness: DrillHarness, faults: MockFaults
) -> None:
    """真实 HTTP 500 → `APPROVAL_API_ERROR`（瞬时）→ `retry_wait` + 503。

    ⚠️ 这里**不能**直接断言 `task_status == blocked`：
    一次抖动就产生人工工单，等于把重试机制的意义抹掉了。
    """
    faults.inject(PULL_TARGET, "http_500", message="模拟服务端故障")

    response = harness.pull()

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"

    body = response.json()
    assert body["error_code"] == "APPROVAL_API_ERROR"
    assert body["retryable"] is True

    job = harness.only_job()
    assert job.job_status == JobStatus.RETRY_WAIT.value
    assert job.next_retry_at is not None
    assert job.attempt_no == 1

    # 拉取失败不得留下"拉了一半"的任务
    assert harness.tasks() == []


def test_injected_timeout_is_detected_and_retried(
    harness: DrillHarness, faults: MockFaults
) -> None:
    """真实**读超时** → `APPROVAL_API_TIMEOUT`（瞬时）→ `retry_wait`。

    这条是起真实进程的理由：`timeout` 只在真 socket 上生效。
    mock 阻塞 5 秒，网关只等 0.5 秒 —— 必须由 httpx 抛 `ReadTimeout`，
    适配器再把它映射成带错误码的端口异常。
    """
    harness.seed_task()
    harness.use_gateway_timeout(0.5)
    faults.inject(DOWNLOAD_TARGET, "timeout", delay_seconds=5.0)

    started = time.monotonic()
    response = harness.download()
    elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json()["error_code"] == "APPROVAL_API_TIMEOUT"
    assert response.json()["retryable"] is True
    assert elapsed < 4.0, f"没有真的超时，耗时 {elapsed:.1f}s（疑似阻塞到 mock 返回）"

    job = harness.only_job(JobType.DOWNLOAD)
    assert job.job_status == JobStatus.RETRY_WAIT.value
    # 重试未耗尽 → 任务不该被阻塞
    assert harness.tasks()[0].task_status != TaskStatus.BLOCKED.value


def test_retries_exhaust_before_task_is_blocked(
    harness: DrillHarness, faults: MockFaults
) -> None:
    """**重试耗尽之后**才阻塞任务（`max_attempts` 默认 3）。

    这是"一次抖动不该产生人工工单"与"不能无限重试"两条要求的交界处：
    前两次必须留在 `retry_wait`，第三次才转 `blocked`。

    三次响应都是 503 —— 故障本身是**系统故障**，
    即使任务因重试耗尽而阻塞，调用端该知道的仍然是"系统当前不可用"。
    任务已阻塞这个事实落在库与后续的任务查询接口上。
    """
    harness.seed_task()
    faults.inject(DOWNLOAD_TARGET, "http_500")

    statuses = []
    for _ in range(3):
        response = harness.download()
        assert response.status_code == 503
        statuses.append(harness.only_job(JobType.DOWNLOAD).job_status)

    assert statuses == [
        JobStatus.RETRY_WAIT.value,
        JobStatus.RETRY_WAIT.value,
        JobStatus.FAILED.value,
    ], f"重试预算消耗顺序不对：{statuses}"

    task = harness.tasks()[0]
    assert task.task_status == TaskStatus.BLOCKED.value
    assert task.blocked_stage == JobType.DOWNLOAD.value
    assert task.last_error_code == "APPROVAL_API_ERROR"


# ============================================================
# 2. 确定性故障：不浪费重试，直接给业务结论
# ============================================================


def test_injected_404_on_download_becomes_business_fact(
    harness: DrillHarness, faults: MockFaults
) -> None:
    """下载遇到真实 404 → `ATTACHMENT_MISSING` → **200 + blocked**，且**不重试**。

    这是整个 M3 最容易写错的一处状态码选择：
    附件被删除是**业务结论**（调用成功确认了它），不是系统故障。
    返回 5xx 会让调用端把它当抖动反复重试，任务永远等不到人处理。
    """
    harness.seed_task()
    faults.inject(
        DOWNLOAD_TARGET, "not_found", instance_id=FIXTURE_INSTANCE
    )

    response = harness.download()

    assert response.status_code == 200, "业务结论不该是 4xx/5xx"

    body = response.json()
    assert body["outcome"] == "blocked"
    assert body["error_code"] == "ATTACHMENT_MISSING"
    assert body["task_status"] == TaskStatus.BLOCKED.value
    assert body["blocked_stage"] == "download"

    job = harness.only_job(JobType.DOWNLOAD)
    assert job.job_status == JobStatus.FAILED.value, "确定性错误不该重试"
    assert job.next_retry_at is None
    assert job.attempt_no == 1, "确定性错误不该被重试，尝试次数应停在 1"


def test_injected_404_on_detail_keeps_distinct_meaning(
    harness: DrillHarness, faults: MockFaults
) -> None:
    """同一个 404，在**查审批单**时是 `INSTANCE_NOT_FOUND`（单号错了）。

    语义差别必须保留：排障时"单号错了"要去核对编号，
    "附件被删了"要去问上传人，两者的处理动作完全不同。
    """
    faults.inject(DETAIL_TARGET, "not_found", instance_id=FIXTURE_INSTANCE)

    response = harness.detail()

    assert response.status_code == 404
    assert response.json()["error_code"] == "INSTANCE_NOT_FOUND"
    # 详情是"任务所需基础字段"的来源，取不到就不该留下一条空任务
    assert harness.tasks() == []


def test_injected_auth_failure_is_permanent(harness: DrillHarness) -> None:
    """凭据错误 → 502 且**不重试**。

    重试一万次，被拒绝的凭据也不会变成被接受的。区分它和 500
    是为了让"我们的配置错了"不会被当成"对方服务抖动"而无限重试。
    """
    harness.seed_task()
    harness.gateway.close()
    harness.gateway = MockApprovalGateway(
        base_url=harness.base_url, token="wrong-token", timeout=10.0
    )

    response = harness.download()

    assert response.status_code == 502
    assert response.json()["error_code"] == "AUTH_FAILED"
    assert response.json()["retryable"] is False

    job = harness.only_job(JobType.DOWNLOAD)
    assert job.job_status == JobStatus.FAILED.value
    assert job.next_retry_at is None


# ============================================================
# 3. 恢复：故障消除后必须能继续
# ============================================================


def test_system_recovers_after_fault_is_cleared(
    harness: DrillHarness, faults: MockFaults
) -> None:
    """清掉故障后，同一个调用必须能成功。

    这条防的是"故障演练留下后遗症"：如果一次瞬时失败把对象或幂等键
    永久写坏（例如 job 卡在 running、附件记录被写成 failed 后不再重试），
    系统就再也回不到正常状态 —— 而单看失败路径的断言是发现不了的。
    """
    harness.seed_task()
    faults.inject(DOWNLOAD_TARGET, "http_500")

    failed = harness.download()
    assert failed.status_code == 503
    assert harness.only_job(JobType.DOWNLOAD).job_status == JobStatus.RETRY_WAIT.value

    faults.clear()

    recovered = harness.download()

    assert recovered.status_code == 200
    body = recovered.json()
    assert body["outcome"] == "downloaded"
    # 对象键属于内部实现细节，**不在响应里下发**（见 app/api/tools.py 工具 3）
    assert "object_key" not in body

    # 但内部必须真的写进了内容寻址位置
    stored = harness.attachments()
    assert len(stored) == 1
    assert stored[0].object_key is not None
    assert stored[0].object_key.startswith("sha256/")

    job = harness.only_job(JobType.DOWNLOAD)
    assert job.job_status == JobStatus.SUCCEEDED.value
    assert job.last_error_code is None, "成功后必须清掉上一次的错误码"
