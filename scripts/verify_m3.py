#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""M3 验收证据生成器 —— 一条命令跑完 19 条验收标准。

用法::

    python scripts/verify_m3.py            # 跑完自动清理临时现场
    python scripts/verify_m3.py --keep     # 保留现场供人工查看

退出码：全部通过 0；任一未通过 1（可直接用作流水线门禁）。

## 为什么单独写一个脚本，而不是"跑 pytest 就行了"

`pytest` 回答的是"代码还对吗"，它**不回答"需求满足了吗"**。
验收标准是与干系人的约定，需要的是**逐条对应**的证据：
每条给出实测值，让人不必翻测试代码去判断哪条对应哪条。

## 两类证据，都必须是真的

| 类别 | 做法 | 覆盖 |
| --- | --- | --- |
| **活体实测** | 本脚本自己起真 mock 进程 + 真适配器 + 真数据库，走完整流程并打印实测数字 | 1 / 4 / 5 / 7 / 8 / 9 / 12 / 13 / 15 |
| **指定用例** | 运行覆盖该条的既有测试，报告实际通过情况 | 2 / 3 / 6 / 10 / 11 / 14 / 17 / 18 / 19 |
| **全量回归** | 跑完整测试集 | 16 |

⚠️ 第二类**不复制断言**。验收标准是与干系人的约定，单元测试是代码的回归网；
把断言抄第二遍，只会得到两份会各自漂移的真相。

## 为什么活体部分不复用测试里的辅助函数

"独立验证"的意义就在于**不依赖被验证者的自我描述**。
如果脚本复用测试的夹具与假适配器，那么"测试本身写错了"这种情况
它同样发现不了 —— 那它就只是一份更啰嗦的 `pytest` 报告。
因此活体部分一切从外部观察：真 HTTP、真 SQLite 文件、真对象目录。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# 直接以 `python scripts/verify_m3.py` 运行时，sys.path[0] 是 scripts/ 而不是项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.adapters.approval.mock_approval_gateway import (  # noqa: E402
    MockApprovalGateway,
)
from app.adapters.storage.local_file_storage import LocalFileStorage  # noqa: E402
from app.api.deps import get_db, get_gateway, get_storage  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Base, transactional_session  # noqa: E402
from app.main import app  # noqa: E402
from app.ports.object_storage import content_addressed_key  # noqa: E402

#: mock 夹具里那条"附件已被删除"的附件
MISSING_INSTANCE = "HT-2026-0005"
MISSING_ATTACHMENT = "A-5002"

#: 验收 8 使用的附件（内容寻址去重的观测点）。
#:
#: ⚠️ 这里原先写的是"两个**内容完全相同**的附件（都来自 mock 的 text_pdf 生成器）"。
#: T2 把夹具换成各自真实的中文合成合同后，那句前提**不再成立** ——
#: 详见 `_probe_content_dedup` 的说明。
DEDUP_INSTANCE = "HT-2026-0002"
DEDUP_ATTACHMENT = "A-1002"

#: 键与摘要比对时截断到多少字符（只为输出可读）
_KEY_PREFIX_LENGTH = 28


# ============================================================
# 验收标准目录
# ============================================================


@dataclass(frozen=True)
class Criterion:
    """一条验收标准及其证据来源。"""

    number: int
    title: str
    #: 活体探测的键（`None` 表示本条不用活体探测）
    live: str | None = None
    #: 覆盖本条的 pytest 节点（空表示走全量回归）
    tests: tuple[str, ...] = ()


CRITERIA: tuple[Criterion, ...] = (
    Criterion(1, "连续拉取 3 次，approval_tasks 仍只有 6 行", live="pull_idempotent"),
    Criterion(
        2,
        "同 approval_code、不同 tenant_id 可共存",
        tests=(
            "tests/test_schema_consistency.py"
            "::test_same_approval_code_in_another_tenant_is_allowed",
        ),
    ),
    Criterion(
        3,
        "同租户同 instance_id 重复插入被拒绝",
        tests=(
            "tests/test_schema_consistency.py"
            "::test_duplicate_instance_in_same_tenant_rejected",
        ),
    ),
    Criterion(4, "拉取后 6 条任务 context_status='missing'", live="missing_context"),
    Criterion(5, "详情同步后 complete，4 字段与外部一致，form_data 已落库", live="detail_sync"),
    Criterion(
        6,
        "详情变化后可再次同步（作业幂等键含版本，不被永久阻断）",
        tests=(
            "tests/test_pull_service.py::test_changed_context_produces_a_new_job",
            "tests/test_pull_service.py"
            "::test_detail_sync_can_run_again_after_values_change",
        ),
    ),
    Criterion(
        7,
        # 措辞刻意写"对象键可用"而不是"响应返回对象键"：
        # 要验的是对象真的落在内容寻址位置上，不是把它下发给调用方
        "附件下载：SHA-256 一致、对象键可用、物化路径可读",
        live="attachment_stored",
    ),
    Criterion(
        8,
        "同一内容只占一个对象（对象键由内容摘要推导，内容寻址）",
        live="content_dedup",
    ),
    Criterion(9, "附件缺失 → 附件 failed、任务 blocked、错误码 ATTACHMENT_MISSING", live="attachment_missing"),
    Criterion(
        10,
        "注入 500 → 重试；注入 404 → 不重试直接 blocked",
        tests=(
            "tests/test_fault_drills.py::test_injected_500_is_classified_retryable",
            "tests/test_fault_drills.py"
            "::test_injected_404_on_download_becomes_business_fact",
            "tests/test_fault_drills.py::test_retries_exhaust_before_task_is_blocked",
        ),
    ),
    Criterion(
        11,
        "存储不可用可重试；路径越界不可重试",
        tests=(
            "tests/test_adapter_local_storage.py::test_other_os_error_is_transient",
            "tests/test_adapter_local_storage.py"
            "::test_traversal_attempt_writes_nothing_outside_root",
            "tests/test_schema_consistency.py"
            "::test_error_code_retryability_classification",
        ),
    ),
    Criterion(12, "三个工具可经 REST 实际调用；blocked 场景返回 200 而非 5xx", live="rest_callable"),
    Criterion(13, "三个工具各写一条 workflow_jobs，同输入版本不新建", live="job_ledger"),
    Criterion(
        14,
        "合约测试：网关与对象存储满足端口语义",
        tests=(
            "tests/test_adapter_local_storage.py::TestLocalFileStorageContract",
            "tests/contract/test_contract_hygiene.py",
            "tests/test_ports_and_errors.py",
            "tests/test_adapter_mock_gateway.py",
        ),
    ),
    Criterion(15, "日志不含合同正文与 form_data 敏感字段", live="log_redaction"),
    Criterion(16, "既有测试全绿"),
    Criterion(
        17,
        "数据库层拒绝非法状态与非法计数",
        # 引用到文件级：该用例是**参数化**的，写裸名会让 pytest 采集报错
        # （`ERROR: not found`），一条错节点会让整批都拿不到结果。
        tests=("tests/test_data_integrity.py",),
    ),
    Criterion(
        18,
        "updated_at 随 ORM 更新前进",
        tests=("tests/test_data_integrity.py::test_updated_at_advances_on_orm_update",),
    ),
    Criterion(
        19,
        "schema.sql 与 models.py 的 CHECK 约束双向一致",
        tests=(
            "tests/test_data_integrity.py"
            "::test_check_constraints_match_between_sql_and_orm",
        ),
    ),
)


@dataclass
class Finding:
    """一条验收标准的判定与证据。"""

    criterion: Criterion
    ok: bool
    measured: str
    source: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(finding.ok for finding in self.findings)

    @property
    def passed_count(self) -> int:
        return sum(1 for finding in self.findings if finding.ok)


# ============================================================
# mock 进程
# ============================================================


def _free_port() -> int:
    """让内核分配空闲端口，避免与开发时手动的 8001 冲突。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(base_url: str, timeout: float = 60.0) -> None:
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


def _start_mock() -> tuple[subprocess.Popen, str]:
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
    _wait_ready(base_url)
    return process, base_url


# ============================================================
# 活体实测
# ============================================================


class LiveRun:
    """一次完整的端到端跑动，从外部观察系统（真 HTTP / 真文件 / 真 SQL）。"""

    def __init__(self, work_dir: Path, base_url: str) -> None:
        self.work_dir = work_dir
        self.base_url = base_url
        # 物化路径与对象存储都由 settings.storage_path 派生，改一处即可
        self.storage_root = work_dir / "storage"
        settings.storage_root = str(self.storage_root)

        self.engine = create_engine(
            f"sqlite:///{(work_dir / 'acceptance.db').as_posix()}",
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
        self.storage = LocalFileStorage(self.storage_root / "objects")
        self.client = TestClient(app)
        #: 首轮详情响应（由 `_probe_job_ledger` 填入，供验收 5 / 12 复用）
        self.first_detail_response: httpx.Response | None = None

    # ---------- 装配 ----------

    def install(self) -> None:
        app.dependency_overrides[get_db] = self._session_dependency
        app.dependency_overrides[get_gateway] = lambda: self.gateway
        app.dependency_overrides[get_storage] = lambda: self.storage

    def uninstall(self) -> None:
        app.dependency_overrides.clear()
        self.gateway.close()
        self.engine.dispose()

    def _session_dependency(self):
        yield from transactional_session(self.session_factory())

    # ---------- 观察窗口（一律直接读库/读盘，不经过业务代码）----------

    def _scalar(self, sql: str) -> object:
        with self.session_factory() as session:
            return session.execute(text(sql)).scalar()

    def _rows(self, sql: str) -> list[tuple]:
        with self.session_factory() as session:
            return [tuple(row) for row in session.execute(text(sql)).all()]

    def _object_files(self) -> list[Path]:
        root = self.storage_root / "objects"
        return [path for path in root.rglob("*") if path.is_file()]

    # ---------- 调用 ----------

    def pull(self) -> httpx.Response:
        return self.client.post(
            "/tools/list_pending_contract_approvals", json={"limit": 20}
        )

    def detail(self, instance_id: str) -> httpx.Response:
        return self.client.post(
            "/tools/get_contract_approval", json={"instance_id": instance_id}
        )

    def download(self, instance_id: str, attachment_id: str) -> httpx.Response:
        return self.client.post(
            "/tools/download_contract_attachment",
            json={"instance_id": instance_id, "attachment_id": attachment_id},
        )

    # ---------- 完整跑动 ----------

    def run(self) -> dict[str, tuple[bool, str]]:
        """按验收顺序跑完整流程，返回 `{活体键: (是否通过, 实测值)}`。"""
        results: dict[str, tuple[bool, str]] = {}

        results["pull_idempotent"] = self._probe_pull_idempotent()
        results["missing_context"] = self._probe_missing_context()

        # 作业台账必须**在详情同步探测之前**跑：它的第一轮就是
        # "三个工具各调一次"，此刻每种作业恰好一条。
        # 若先跑详情同步，第一轮会变成"详情第二次调用"，
        # 作业类型里就出现两条 detail，无法验证"各写一条"。
        results["job_ledger"] = self._probe_job_ledger()
        results["detail_sync"] = self._probe_detail_sync(self.first_detail_response)

        results["attachment_stored"] = self._probe_attachment_stored()
        results["content_dedup"] = self._probe_content_dedup()
        results["attachment_missing"] = self._probe_attachment_missing()
        results["rest_callable"] = self._probe_rest_callable(self.first_detail_response)
        results["log_redaction"] = self._probe_log_redaction()

        return results

    # ---------- 逐条探测 ----------

    def _probe_pull_idempotent(self) -> tuple[bool, str]:
        """验收 1：连续拉取 3 次，approval_tasks 仍只有 6 行。"""
        rounds = [self.pull() for _ in range(3)]
        bodies = [response.json() for response in rounds]
        rows = self._scalar("SELECT COUNT(*) FROM approval_tasks")

        created = [body["created"] for body in bodies]
        updated = [body["updated"] for body in bodies]
        ok = (
            all(response.status_code == 200 for response in rounds)
            and rows == 6
            and created == [6, 0, 0]
        )

        return ok, (
            f"三次 created={created} / updated={updated}；"
            f"approval_tasks 行数={rows}（期望 6）"
        )

    def _probe_missing_context(self) -> tuple[bool, str]:
        """验收 4：拉取后的任务立场未知，context_status 必然是 missing。"""
        missing = self._scalar(
            "SELECT COUNT(*) FROM approval_tasks WHERE context_status = 'missing'"
        )
        total = self._scalar("SELECT COUNT(*) FROM approval_tasks")
        return missing == 6, f"context_status='missing' 的任务 {missing}/{total}"

    def _probe_detail_sync(self, response: httpx.Response) -> tuple[bool, str]:
        """验收 5：详情同步把权威上下文推进到 complete，且与外部真值一致。

        真值由本脚本**另外取一次**外部接口得到，而不是读我们自己写进库的值 ——
        否则"我们把值写对了"这件事根本没被检验。
        """
        body = response.json()
        truth = self.gateway.get_detail("HT-2026-0001")

        fields = (
            "our_party_name",
            "our_party_contract_label",
            "our_party_business_role",
            "contract_type",
        )
        mismatches = [
            name
            for name in fields
            if body.get(name) != getattr(truth.context, name)
        ]

        form_data = self._scalar(
            "SELECT form_data_json FROM approval_tasks "
            "WHERE instance_id = 'HT-2026-0001'"
        )

        ok = (
            response.status_code == 200
            and body.get("context_status") == "complete"
            and not mismatches
            and bool(form_data)
        )

        return ok, (
            f"context_status={body.get('context_status')}；"
            f"4 个字段与外部台账逐字段一致={not mismatches}"
            f"{'' if not mismatches else f'（不一致：{mismatches}）'}；"
            f"form_data_json 已落库={bool(form_data)}"
        )

    def _all_tools_once(self) -> httpx.Response:
        """三个工具各调一次，返回详情的响应（供验收 5 / 12 复用）。"""
        self.pull()
        detail_response = self.detail("HT-2026-0001")
        self.download("HT-2026-0001", "A-1001")
        return detail_response

    def _probe_job_ledger(self) -> tuple[bool, str]:
        """验收 13：三个工具各写一条作业；同输入版本重复调用不新建。

        判据是**稳定**而不是"永远只有三条"：详情与下载的作业版本都取自
        **已存**值，"第一次真正写入数据"会把版本键推进一版，从第二轮之后才固定。
        这与"版本必须能变"是同一件事的两面 —— 能变，才不会把对象永久卡死。
        """
        self.first_detail_response = self._all_tools_once()
        kinds = sorted(
            row[0] for row in self._rows("SELECT job_type FROM workflow_jobs")
        )
        first_round = self._scalar("SELECT COUNT(*) FROM workflow_jobs")

        self._all_tools_once()  # 这一轮把详情/下载的版本推进一版
        settled = self._scalar("SELECT COUNT(*) FROM workflow_jobs")

        self._all_tools_once()  # 版本已稳定
        after = self._scalar("SELECT COUNT(*) FROM workflow_jobs")

        ok = kinds == ["detail", "download", "pull"] and after == settled
        return ok, (
            f"首轮作业类型={kinds}（期望 detail/download/pull 各一）；"
            f"作业数 首轮 {first_round} → 版本稳定后 {settled} → 再调一轮 {after}"
            f"（期望不再增长）"
        )

    def _probe_attachment_stored(self) -> tuple[bool, str]:
        """验收 7：SHA-256 一致、对象键对应的对象可读、物化路径可读。

        ⚠️ **对象键从数据库读，不从响应取**。
        它属于内部实现细节，接口刻意不下发（见 `app/api/tools.py` 工具 3 的说明）。
        本条要验的是"对象确实落在了内容寻址位置上"，而**不是**
        "把它返回给了调用方" —— 后者恰恰是要避免的。
        """
        response = self.download("HT-2026-0001", "A-1001")
        body = response.json()

        object_key = self._scalar(
            "SELECT object_key FROM approval_attachments "
            "WHERE attachment_id = 'A-1001'"
        )

        # ⚠️ 两个位置的**基准不同**，混用会得到"文件不存在"的假失败：
        #   object_key 相对的是**对象根**（<storage_root>/objects）；
        #   file_path  相对的是 storage_root（物化工作目录与对象根是并列的）。
        object_file = self.storage_root / "objects" / str(object_key or "")
        materialized = self.storage_root / str(body.get("file_path", ""))

        object_ok = object_file.is_file()
        materialized_ok = materialized.is_file()
        digest = hashlib.sha256(object_file.read_bytes()).hexdigest() if object_ok else ""
        same_bytes = (
            object_ok
            and materialized_ok
            and object_file.read_bytes() == materialized.read_bytes()
        )

        ok = (
            response.status_code == 200
            and body.get("outcome") == "downloaded"
            and "object_key" not in body
            and object_ok
            and materialized_ok
            and digest == body.get("file_checksum")
            and same_bytes
            and str(body.get("file_path", "")).startswith("workspace/")
        )

        return ok, (
            f"HTTP {response.status_code} / outcome={body.get('outcome')}；"
            f"sha256={str(body.get('file_checksum'))[:16]}…；"
            f"对象键（取自库）={str(object_key)[:24]}… 可读={object_ok}；"
            f"物化路径可读={materialized_ok}；"
            f"响应未下发对象键={'object_key' not in body}；"
            f"回读摘要与声明一致={digest == body.get('file_checksum')}；"
            f"两者字节相同={same_bytes}"
        )

    def _probe_content_dedup(self) -> tuple[bool, str]:
        """验收 8：同一内容只占一个对象（内容寻址）。

        ## 取证方式在 M4/T2 之后改过，原因必须留下来

        原先这条靠"A-1001 与 A-1002 的内容**恰好**相同"来演示 ——
        因为 M2 的占位 PDF 是运行时按固定模板生成的，两份附件字节全同。

        那是**偶然条件，不是需求本身**。T2 把夹具换成各自真实的中文合成合同之后，
        这个碰撞自然消失了：两份合同的正文本来就不一样。

        ## 改为验证**机制**，而不是验证**一次巧合**

        断言"对象键 == 由内容摘要推导出的键"。这一条成立时，
        "同一内容只占一个对象"对**所有**输入都必然成立 ——
        无论它来自哪个任务、下载多少次、几个任务同时下载。
        比"碰巧两份文件一样"强得多，而且不会因为夹具换了内容就失效。

        ⚠️ 若只是把这条判据放宽成"对象数没变"，就会退化成
        "这次下载没出错" —— 那和内容寻址没有任何关系。

        **两件事都要验，缺一不可**：

        ① **结构性**：对象键必须**等于**内容摘要推导出的键；
        ② **行为性**：同一内容再下载一次，对象数**不增**。

        只验 ① 是"公式对但没人走过"；只验 ② 则区分不开
        "去重生效"与"这次根本没写入"。
        """
        response = self.download(DEDUP_INSTANCE, DEDUP_ATTACHMENT)
        body = response.json()

        # ① 对象键必须由内容推导
        checksum = body.get("file_checksum")
        suffix = Path(str(body.get("file_name", ""))).suffix.lstrip(".") or "bin"
        derived_key = content_addressed_key(str(checksum), suffix=suffix)
        stored_key = self._scalar(
            "SELECT object_key FROM approval_attachments "
            f"WHERE attachment_id = '{DEDUP_ATTACHMENT}'"
        )
        key_is_content_addressed = stored_key == derived_key

        # ② 同一内容再来一次：对象数不得增加
        first_count = len(self._object_files())
        self.download(DEDUP_INSTANCE, DEDUP_ATTACHMENT)
        second_count = len(self._object_files())
        no_new_object = second_count == first_count

        ok = (
            response.status_code == 200
            and bool(checksum)
            and key_is_content_addressed
            and no_new_object
        )

        return ok, (
            f"落库对象键={str(stored_key)[:_KEY_PREFIX_LENGTH]}…；"
            f"由内容摘要推导={derived_key[:_KEY_PREFIX_LENGTH]}…；"
            f"两者相同={key_is_content_addressed}"
            f"（同一内容不可能占两个对象）；"
            f"同内容再下一次：对象数 {first_count} → {second_count}"
            f"（未新增={no_new_object}）"
        )

    def _probe_attachment_missing(self) -> tuple[bool, str]:
        """验收 9：附件缺失 → 200 + blocked，且附件记录为 failed。

        先同步一次详情，让附件记录存在 —— 这是控制台上的正常流程
        （先看到附件清单，再触发下载）。
        """
        self.detail(MISSING_INSTANCE)
        response = self.download(MISSING_INSTANCE, MISSING_ATTACHMENT)
        body = response.json()

        task = self._rows(
            "SELECT task_status, blocked_stage, last_error_code "
            "FROM approval_tasks "
            f"WHERE instance_id = '{MISSING_INSTANCE}'"
        )
        attachment = self._rows(
            "SELECT download_status FROM approval_attachments "
            f"WHERE attachment_id = '{MISSING_ATTACHMENT}'"
        )

        task_row = task[0] if task else (None, None, None)
        attachment_status = attachment[0][0] if attachment else None

        ok = (
            response.status_code == 200
            and body.get("outcome") == "blocked"
            and body.get("error_code") == "ATTACHMENT_MISSING"
            and task_row == ("blocked", "download", "ATTACHMENT_MISSING")
            and attachment_status == "failed"
        )

        return ok, (
            f"HTTP {response.status_code} / outcome={body.get('outcome')}；"
            f"任务(task_status, blocked_stage, last_error_code)={task_row}；"
            f"附件记录 download_status={attachment_status}（期望 failed）"
        )

    def _probe_rest_callable(self, detail_response: httpx.Response) -> tuple[bool, str]:
        """验收 12：三个工具都能经 REST 调用，且响应带 outcome。"""
        responses = {
            "工具1": self.pull(),
            "工具2": detail_response,
            "工具3": self.download("HT-2026-0001", "A-1001"),
            "工具3(blocked)": self.download(MISSING_INSTANCE, MISSING_ATTACHMENT),
        }
        outcomes = {
            name: (response.status_code, response.json().get("outcome"))
            for name, response in responses.items()
        }

        ok = all(
            status == 200 and outcome is not None
            for status, outcome in outcomes.values()
        )
        blocked_status = outcomes["工具3(blocked)"][0]

        return ok, (
            f"{outcomes}；blocked 场景 HTTP {blocked_status}（期望 200 而非 5xx）"
        )

    def _probe_log_redaction(self) -> tuple[bool, str]:
        """验收 15：日志不得含审批表单里的敏感值。

        判定方式是"把外部台账里的表单值拿去搜日志"，而不是搜某个固定字符串 ——
        固定字符串只能证明"这一条没漏"，换个字段就漏了。
        """
        secrets: set[str] = set()
        pending = [row[0] for row in self._rows(
            "SELECT instance_id FROM approval_tasks"
        )]
        for instance_id in pending:
            try:
                truth = self.gateway.get_detail(instance_id)
            except Exception:  # noqa: BLE001 - 取不到就不参与本次检查
                continue
            secrets.update(_string_values(truth.form_data))

        # 太短的字符串会与日志里的正常词撞上，只检查有辨识度的
        secrets = {value for value in secrets if len(value) >= 4}

        logs = [row[0] or "" for row in self._rows("SELECT log_content FROM task_logs")]
        leaked = sorted(
            {value for value in secrets if any(value in content for content in logs)}
        )

        return not leaked, (
            f"扫描 {len(logs)} 条日志、{len(secrets)} 个表单敏感值 → 命中 {len(leaked)}"
            f"{'' if not leaked else f'（泄漏：{leaked[:3]}）'}"
        )


def _string_values(payload: object) -> set[str]:
    """递归收集一个结构里所有非空字符串值。"""
    found: set[str] = set()
    if isinstance(payload, str):
        if payload.strip():
            found.add(payload.strip())
    elif isinstance(payload, dict):
        for value in payload.values():
            found |= _string_values(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            found |= _string_values(value)
    return found


# ============================================================
# 指定用例
# ============================================================

_OUTCOME_PATTERN = re.compile(r"^(?P<node>\S+::\S+)\s+(?P<outcome>PASSED|FAILED|ERROR)\b")


def _outcomes_for(node: str, outcomes: dict[str, str]) -> list[str]:
    """取某个引用节点下的全部结果。

    允许把引用写到**文件**或**类**一级（`tests/contract/test_x.py`、
    `...::TestLocalFileStorageContract`），也可以写到具体用例。
    但 `-v` 的输出是按**单个测试**逐行给的，因此这里按前缀归集。

    ⚠️ 匹配必须精确到边界。只用 `startswith(node)` 会让
    `test_invalid_key` 意外吃到 `test_invalid_key_extra`，
    于是漏掉的用例被别人的通过掩盖。因此只接受三种形态：

    - 完全相等
    - `node[参数]`（参数化用例）
    - `node::成员`（文件 → 用例，或类 → 方法）
    """
    if node in outcomes:
        return [outcomes[node]]
    results: list[str] = []
    for key, outcome in outcomes.items():
        if key.startswith(node + "[") or key.startswith(node + "::"):
            results.append(outcome)
    return results


def _run_pytest(nodes: list[str]) -> tuple[dict[str, str], str, str]:
    """跑指定节点，返回 `({节点: 结果}, 摘要行, stderr 摘要)`。

    ## 为什么逐条解析而不是只看退出码

    一条验收标准引用多个用例。整体失败只能说明"有问题"，
    说不清**是哪条引用没过**，而报告的价值恰恰在于指出具体那一条。

    ## 为什么每条验收标准**单独**起一次 pytest

    因为 pytest 遇到"不存在的节点"（例如把参数化用例写成裸名）
    会**整批拒绝执行**并只往 stderr 打一行 `ERROR: not found`。
    合并成一次调用时，一个笔误会让 9 条验收标准全部变成"未采集到结果"，
    真正的错因反而被淹没。分开跑之后，出错只影响它自己那一行，
    并且 stderr 会如实写进报告。
    """
    command = [
        sys.executable,
        "-m",
        "pytest",
        *nodes,
        "-v",
        "--tb=short",
        "-p",
        "no:cacheprovider",
    ]
    completed = subprocess.run(
        command, cwd=str(PROJECT_ROOT), capture_output=True, text=True, errors="replace"
    )

    outcomes: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        match = _OUTCOME_PATTERN.match(line.strip())
        if match:
            outcomes[match.group("node")] = match.group("outcome")

    summary = ""
    for line in reversed(completed.stdout.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            summary = line.strip()
            break

    stderr_tail = ""
    if not outcomes:
        for line in completed.stderr.splitlines():
            if "not found" in line or "ERROR" in line:
                stderr_tail = line.strip()
                break
        stderr_tail = stderr_tail or "pytest 未产出可解析的结果"

    return outcomes, summary, stderr_tail


def _run_full_suite() -> tuple[bool, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        errors="replace",
    )
    summary = ""
    for line in reversed(completed.stdout.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            summary = line.strip()
            break
    return completed.returncode == 0, summary or "（未解析到摘要）"


# ============================================================
# 主流程
# ============================================================


def _print_report(report: Report, live_root: Path | None) -> None:
    print()
    print("=" * 96)
    print("M3 验收证据报告（19 条）")
    print("=" * 96)

    for finding in report.findings:
        mark = "PASS" if finding.ok else "FAIL"
        print(f"[{finding.criterion.number:>2}] {mark}  {finding.criterion.title}")
        print(f"      实测 : {finding.measured}")
        print(f"      来源 : {finding.source}")

    print("-" * 96)
    total = len(report.findings)
    print(f"结论：{report.passed_count}/{total} 通过")
    if live_root is not None:
        print(f"现场：{live_root}")
    print("=" * 96)


def main() -> int:
    parser = argparse.ArgumentParser(description="M3 验收证据生成器")
    parser.add_argument(
        "--keep", action="store_true", help="保留临时现场供人工查看（默认清理）"
    )
    args = parser.parse_args()

    live_root = Path(tempfile.mkdtemp(prefix="verify-m3-", dir=str(PROJECT_ROOT)))
    mock_process = None
    report = Report()

    try:
        print(f"临时现场：{live_root}")
        print("启动 mock 审批系统 ...")
        mock_process, base_url = _start_mock()

        print("跑活体验收流程（真 HTTP / 真 SQLite / 真对象目录）...")
        live = LiveRun(live_root, base_url)
        live.install()
        try:
            measurements = live.run()
        finally:
            live.uninstall()

        # ---- 活体实测条目 ----
        for criterion in CRITERIA:
            if criterion.live is None:
                continue
            ok, measured = measurements[criterion.live]
            report.findings.append(
                Finding(criterion, ok, measured, "本脚本活体实测（真 mock 进程 + 真适配器）")
            )

        # ---- 指定用例条目 ----
        cited = [criterion for criterion in CRITERIA if criterion.tests]
        for criterion in cited:
            print(f"运行指定用例：验收 {criterion.number} ...")
            outcomes, summary, stderr_tail = _run_pytest(list(criterion.tests))

            results = {node: _outcomes_for(node, outcomes) for node in criterion.tests}
            passed = sum(
                1
                for values in results.values()
                if values and all(value == "PASSED" for value in values)
            )
            missing = [node for node, values in results.items() if not values]

            ok = passed == len(criterion.tests) and not missing
            detail = f"{passed}/{len(criterion.tests)} 个引用节点全部通过"
            if missing:
                detail += f"；未采集到结果：{missing}"
            if stderr_tail:
                detail += f"；pytest 报错：{stderr_tail}"
            report.findings.append(
                Finding(criterion, ok, detail, "tests/ 指定用例 · " + (summary or "无摘要"))
            )

        # ---- 全量回归 ----
        # 不做"跳过"开关：这条本身就是一条验收标准，
        # 跳过却记成通过，等于报告里出现了未经检验的绿点。
        regression = next(c for c in CRITERIA if c.number == 16)
        print("运行全量回归 ...")
        ok, summary = _run_full_suite()
        report.findings.append(Finding(regression, ok, summary, "pytest 全量"))

        report.findings.sort(key=lambda finding: finding.criterion.number)
        _print_report(report, live_root if args.keep else None)
        return 0 if report.ok else 1

    finally:
        if mock_process is not None:
            mock_process.terminate()
            try:
                mock_process.wait(timeout=20)
            except subprocess.TimeoutExpired:  # pragma: no cover
                mock_process.kill()
                mock_process.wait(timeout=5)
        if not args.keep:
            shutil.rmtree(live_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
