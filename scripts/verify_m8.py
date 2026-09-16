"""M8 验收证据生成器：五模块连续走通 + 薄出口 + 契约漂移检查。

## 与 verify_m5–m7 的差别

前几个里程碑的验收脚本逐条引用 pytest 节点（后端测试是当时的交付物）。
M8 的交付物**一半在前端**（vitest，不是 pytest），因此：

- **本脚本**只做必须在"真后端 + 真序列化"上验证的三件事：
  1. **五模块连续走通**——按控制台五个模块取数的顺序，把一条合同从入库推到
     「已回写」，每一步用**前端真正消费的接口**取数（而不是工具的返回体）；
  2. **薄出口**（`POST /api/results/{id}/comment`）的版本化与确认失效；
  3. **契约漂移检查**——把每一步真实响应的**键集合**与
     `frontend/src/api/apiShapes.json`（钉住的形状）逐字段比对。
     前端有一个 vitest 测试拿**同一份 JSON**去核它自己的类型化样本：
     两端都被钉在同一份文件上，谁漂了都会在各自的门禁里红。
- **前端门禁**（lint / typecheck / vitest / build+产物泄漏检查）由
  `verify_m8.ps1` 统一编排——它们是命令，不是 pytest 节点。

## 五模块走查的两个替身（都要说明理由）

| 替身 | 代替什么 | 为什么 |
| --- | --- | --- |
| 内存对象存储（`_Storage`） | MinIO/本地盘 | 解析读的是**存储**；把附件字节直接放进存储后，下载这步的 HTTP 部分与"控制台看到什么"无关 |
| `_FakeCommentGateway` | 外部审批系统 | 回写的送达结果（成功/失败）由它决定；M6 的测试用同一手法 |

解析与规则跑的是**与生产相同的 Worker 入口**（`scripts.run_worker.make_handler`），
不是绕过 Worker 的直调 —— "作业被执行"本身就是链路的一部分。

用法：
    python scripts/verify_m8.py            # 按钉住的形状逐条验收
    python scripts/verify_m8.py --update   # 后端形状变了 → 重新生成钉住文件
                                           # （提交这份 diff，让前端测试指出要跟的地方）
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 验收脚本不允许因为输出不出去而中止（Windows GBK 终端下 U+26A0 会炸）
try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
except (AttributeError, io.UnsupportedOperation):  # pragma: no cover
    pass

# ⚠️ 本脚本是**第一个**直接 import `app` 的脚本（verify_m5–m7 走 pytest 子进程，
# 不需要它）。以脚本方式运行时 sys.path 里没有项目根，必须显式加 ——
# 否则 `ModuleNotFoundError: No module named 'app'`，且报错位置离原因很远。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.adapters.parse.pymupdf_extractor import ENGINE_VERSION
from app.api.deps import get_actor, get_db, get_storage
from app.db import transactional_session
from app.enums import JobType, TaskStatus
from app.main import app
from app.models import ApprovalAttachment, ApprovalTask, ReviewRule
from app.outbox import OutboxDispatcher
from app.ports.approval_gateway import WriteCommentResultDTO
from app.services.parse_service import request_parse

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"
SHAPES_FILE = PROJECT_ROOT / "frontend" / "src" / "api" / "apiShapes.json"

from app.auth import Actor, Role

#: 全权限主体（`system_admin` 覆盖全部 8 项）：本脚本验的是**链路与形状**，
#: 不是授权 —— 那在 test_auth_rbac.py 里逐权限验过。
_ACTOR = Actor(
    actor_id="ops-1",
    display_name="验收主体",
    roles=frozenset({Role.SYSTEM_ADMIN.value}),
    tenant_id="default",
)


# ============================================================
# 替身
# ============================================================


class _Storage:
    """只实现 `get`/`put`/`exists` 的假对象存储（与解析集成测试同一手法）。"""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, *, content_type: str):
        from app.ports.object_storage import ObjectRef

        self._objects[key] = data
        return ObjectRef(
            key=key,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            content_type=content_type,
        )

    def get(self, key: str) -> bytes:
        return self._objects[key]

    def exists(self, key: str) -> bool:
        return key in self._objects

    def presign_get(self, key: str, *, expires_in: int) -> str:  # pragma: no cover
        return f"memory://{key}"


class _FakeCommentGateway:
    """回写网关替身：接受一切（走查要的是"送达"这条路径）。"""

    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []

    def write_comment(
        self, instance_id: str, content: str, *, idempotency_key: str, operator_name=None
    ) -> WriteCommentResultDTO:
        from app.enums import WriteStatus

        self.comments.append((instance_id, idempotency_key))
        return WriteCommentResultDTO(
            write_status=WriteStatus.SUCCESS.value,
            external_comment_id=f"c-{len(self.comments)}",
        )


def _pdf_bytes(variant: str = "A") -> bytes:
    """一份最小的可解析 PDF（有文字 → 有块 → 有几何）。

    ⚠️ **必须用 ASCII 文本**：`insert_text` 默认的基座字体（helv）编码不了
    中文，字形会被**静默丢弃**，解析端拿到的是 `DOCUMENT_EMPTY` ——
    而 PDF 本身看起来完全正常。走查只需要"有块可画"，不需要真实字段值。

    ⚠️ `variant` 改变正文 → 改变 cache_key：三段现场用同一份字节时，
    后两段会命中**解析缓存**、复用第一段的解析记录，本段预留的记录
    永远停在 `pending` —— 症状（"解析没跑"）与病因（"缓存命中"）完全错位。
    """
    import fitz

    doc = fitz.open()
    try:
        page = doc.new_page(width=400, height=200)
        page.insert_text(fitz.Point(20, 60), f"Procurement Contract {variant}", fontsize=12)
        page.insert_text(fitz.Point(20, 90), "Party A: Example Technology Co., Ltd.", fontsize=10)
        page.insert_text(
            fitz.Point(20, 120), "Total Amount: CNY 1,200,000.00", fontsize=10
        )
        return doc.tobytes()
    finally:
        doc.close()


# ============================================================
# 现场
# ============================================================


@dataclass
class Harness:
    factory: sessionmaker
    client: TestClient
    storage: _Storage
    cleanup_dirs: list[str] = field(default_factory=list)

    def close(self) -> None:
        app.dependency_overrides.clear()


def build_harness() -> Harness:
    tmp = tempfile.mkdtemp(prefix="verify-m8-")
    db_path = Path(tmp) / "m8.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()

    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    client = TestClient(app)

    storage = _Storage()

    def session_dependency():
        # 复用 `app/db.py` 的同一份事务边界实现（理由见 test_m6_api 的同款注释）
        yield from transactional_session(factory())

    app.dependency_overrides[get_db] = session_dependency
    app.dependency_overrides[get_actor] = lambda: _ACTOR
    # ⚠️ 存储也必须覆盖：工件端点（标准文档）从 storage 读字节 ——
    # 不覆盖的话它读的是真实本地盘，而字节在内存替身里，
    # 报出来的是"对象不存在"，与真正的原因（读错了存储）毫无关系。
    app.dependency_overrides[get_storage] = lambda: storage

    return Harness(factory=factory, client=client, storage=storage, cleanup_dirs=[tmp])


def seed_chain(
    harness: Harness, *, pdf: bytes, instance_id: str, object_key: str
) -> tuple[int, int]:
    """直接入库：任务（可信立场）+ 附件（已下载成功，字节放进对象存储）。

    ⚠️ "下载"这一步以**直连对象存储**代替：控制台的五个模块取的都是
    `/api/*`，附件字节怎么进的存储与它们无关 —— 而 HTTP 网关的进程编排
    不是本脚本要验的东西（M3/M4 的验收已经覆盖过它）。
    """
    with harness.factory() as session:
        task = ApprovalTask(
            provider="mock",
            tenant_id="default",
            instance_id=instance_id,
            approval_code=instance_id,
            approval_title="设备采购合同",
            applicant_name="示例科技有限公司",
            task_status=TaskStatus.REVIEWING.value,
            write_status="not_written",
            context_status="confirmed",
            context_source="approval_system",
            our_party_name="示例科技有限公司",
            our_party_contract_label="party_a",
            our_party_business_role="buyer",
            contract_type="procurement",
        )
        session.add(task)
        session.flush()

        attachment = ApprovalAttachment(
            task_id=task.id,
            attachment_id="ATT-0001",
            file_name="contract.pdf",
            object_key=object_key,
            file_checksum=hashlib.sha256(pdf).hexdigest(),
            download_status="success",
            content_type="application/pdf",
        )
        session.add(attachment)
        session.flush()

        # 一条最小规则（让 GET /api/rules 有行可取）。⚠️ 必须 **active** 且
        # 关键词命中走查 PDF 的正文：这样规则评价真的发生，`runEvaluation`
        # 的钉住形状才不是空对象（空对象对漂移检查毫无约束力）。
        # ⚠️ rule_code 带 instance 后缀：三段现场各自种一条，撞唯一约束的
        # 教训和上面 instance_id 是同一个。
        session.add(
            ReviewRule(
                rule_code=f"M8_{instance_id}",
                rule_name="验收走查占位规则",
                rule_category="其他",
                risk_level="low",
                rule_status="active",
                priority=999,
                rule_version=1,
                match_mode="keyword",
                # ⚠️ match_text 存的是 **JSON 对象**（`KeywordMatchConfig`：
                # {"keywords": [...]}），不是裸字符串也不是数组 ——
                # 评价装载在**批次开始时**解析全部规则，写错的规则让整个批次起不来。
                match_text='{"keywords": ["Procurement"]}',
            )
        )
        session.commit()
        return task.id, attachment.id


def _allowed_types() -> tuple[str, ...]:
    """与生产入口 `run_worker.main` 相同的白名单构造。"""
    from app.config import settings

    return tuple(
        item.strip()
        for item in settings.attachment_allowed_types.split(",")
        if item.strip()
    )


def run_parse(harness: Harness, attachment_id: int) -> int:
    from app.worker import Worker
    from scripts.run_worker import make_handler

    with harness.factory() as session:
        result = request_parse(
            session, document_id=attachment_id, engine_version=ENGINE_VERSION
        )
        session.commit()
        parse_id = result.parse_id

    worker = Worker(
        harness.factory,
        make_handler(harness.storage, allowed_types=_allowed_types()),
        job_types=[JobType.PARSE],
    )
    worker.run_once()
    # ⚠️ `run_once() is True` 只说明**领到了作业**，不代表它成功：
    # 失败的作业也走完一次领取。验收现场里绝不能带着失败的解析继续走
    # （后面的每一步都会以莫名其妙的方式失败），所以这里直接把作业错误炸出来。
    with harness.factory() as session:
        from app.models import ContractParse, WorkflowJob

        parse = session.get(ContractParse, parse_id)
        job = session.execute(select(WorkflowJob)).scalars().first()
        if parse is None or parse.parse_status != "succeeded":
            raise AssertionError(
                f"解析未成功：parse_status={parse and parse.parse_status}"
                f"，parse_error={parse and (parse.parse_error_code, parse.parse_error)}"
                f"，job={job and (job.job_status, job.last_error_code, job.last_error_text)}"
            )
    return parse_id


def run_rules(harness: Harness, parse_id: int) -> int:
    from app.worker import Worker
    from scripts.run_worker import make_handler

    response = harness.client.post("/tools/run_contract_rules", json={"parse_id": parse_id})
    assert response.status_code == 200, f"工具 5 入队失败：{response.text}"
    # 工具 4/5 是**异步**的，返回 `task_ref`（作业引用）；
    # run_id 要等作业成功后从 `GET /api/jobs/{id}` 的 `result_ref` 拿 ——
    # 这正是 §4.4.3"同事务"保证的读取路径，走查照着它走。
    job_id = int(response.json()["task_ref"]["job_id"])

    worker = Worker(
        harness.factory,
        make_handler(harness.storage, allowed_types=_allowed_types()),
        job_types=[JobType.RULE],
    )
    worker.run_once()

    job = harness.client.get(f"/api/jobs/{job_id}").json()
    result_ref = job.get("result_ref")
    assert result_ref is not None, f"规则作业未成功：{job['job_status']} {job.get('last_error_code')}"
    return int(result_ref["run_id"])


# ============================================================
# 键集合（契约漂移检查）
# ============================================================


def keys_of(payload: Any) -> Any:
    """递归取"对象形状"：dict → 键集合（排序）；list → 取首元素的形状。"""
    if isinstance(payload, dict):
        return {key: keys_of(value) for key, value in sorted(payload.items())}
    if isinstance(payload, list):
        return [keys_of(item) for item in payload[:1]]
    return None


def collect_shapes(harness: Harness, *, task_id: int, parse_id: int, run_id: int) -> dict:
    """按**前端真正消费的接口**收集键集合。"""
    client = harness.client
    me = client.get("/api/me").json()
    task_row = client.get("/api/tasks").json()["items"][0]
    task_detail = client.get(f"/api/tasks/{task_id}").json()
    parse = client.get(f"/api/parses/{parse_id}").json()
    document = client.get(f"/api/parses/{parse_id}/document").json()
    run = client.get(f"/api/runs/{run_id}").json()
    results = client.get("/api/results").json()["items"][0]
    jobs = client.get("/api/jobs").json()["items"][0]
    logs = client.get(f"/api/logs/{task_id}").json()["items"][0]
    audit = client.get("/api/audit").json()["items"][0]
    rules = client.get("/api/rules").json()["items"][0]
    writeback_id = task_detail["writeback"]["latest_attempt_id"]
    writeback = client.get(f"/api/writebacks/{writeback_id}").json()

    return {
        "me": keys_of(me),
        "taskRow": keys_of(task_row),
        "taskDetail": keys_of(task_detail),
        "parseRecord": keys_of(parse),
        "documentPage": keys_of(document["document"]["pages"][0]),
        "runDetail": keys_of(run),
        "runAggregate": keys_of(run["aggregate"]),
        "runEvaluation": keys_of(run["evaluations"][0]) if run["evaluations"] else {},
        "resultRow": keys_of(results),
        "jobRecord": keys_of(jobs),
        "logRow": keys_of(logs),
        "auditRow": keys_of(audit),
        "ruleRow": keys_of(rules),
        "writebackAttempt": keys_of(writeback),
    }


# ============================================================
# 验收条目
# ============================================================


@dataclass
class Finding:
    number: str
    title: str
    ok: bool
    detail: str
    unmet: bool = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M8 验收证据生成器")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--update",
        action="store_true",
        help="后端响应形状变了 → 重新生成 apiShapes.json（提交 diff 让前端跟上）",
    )
    args = parser.parse_args(argv)

    findings: list[Finding] = []
    harness = build_harness()
    try:
        findings.extend(walkthrough(harness))
        findings.extend(thin_exit(harness))
        findings.extend(contract_drift(harness, update=args.update))
    finally:
        harness.close()

    findings.append(
        Finding(
            "10",
            "真浏览器回归（e2e 三套件 + 五模块截图）",
            ok=False,
            detail=(
                "Playwright 与浏览器二进制未安装（与 canvas 同一处理：装不上的依赖"
                "不挡其余门禁）。三个 spec 已写好（e2e/security|accessibility|performance），"
                "文件头注明运行前置；在有浏览器的环境运行 `npx playwright test e2e/`。"
            ),
            unmet=True,
        )
    )

    passed = sum(1 for item in findings if item.ok and not item.unmet)
    unmet = [item for item in findings if item.unmet]
    failed = [item for item in findings if not item.ok and not item.unmet]

    print("\n[M8 acceptance evidence]")
    for item in findings:
        mark = "[UNMET]" if item.unmet else ("[OK]  " if item.ok else "[FAIL]")
        print(f"  {mark} {item.number:>2}. {item.title}")
        if args.verbose or not item.ok:
            print(f"        measured: {item.detail}")

    print(
        f"\n结论：{passed}/{len(findings) - len(unmet)} 通过"
        f"（未满足 {len(unmet)} 条，未通过 {len(failed)} 条）"
    )
    return 0 if not failed else 1


def walkthrough(harness: Harness) -> list[Finding]:
    """五模块连续走通：每一步都用**前端消费的接口**取数。"""
    findings: list[Finding] = []
    pdf = _pdf_bytes("A")
    harness.storage.put("attach-m8", pdf, content_type="application/pdf")
    task_id, attachment_id = seed_chain(
        harness, pdf=pdf, instance_id="HT-2026-0001", object_key="attach-m8"
    )

    # ---- 模块 1：待办列表 ----
    tasks = harness.client.get("/api/tasks").json()
    summary = harness.client.get("/api/tasks/summary").json()
    ok = tasks["total"] >= 1 and tasks["items"][0]["approval_code"] == "HT-2026-0001"
    findings.append(
        Finding(
            "1",
            "模块 1：待办列表（/api/tasks + /summary）看到入库的合同",
            ok,
            f"total={tasks['total']}，by_status={summary.get('by_status')}",
        )
    )

    # ---- 模块 2：详情 ----
    detail = harness.client.get(f"/api/tasks/{task_id}").json()
    ok = (
        detail["context_status"] == "confirmed"
        and detail["our_party_business_role"] == "buyer"
        and detail["latest_parse_id"] is None
    )
    findings.append(
        Finding(
            "2",
            "模块 2：详情给出可信立场与链路占位（解析前 latest_parse_id 为空）",
            ok,
            f"context_status={detail['context_status']}，role={detail['our_party_business_role']}",
        )
    )

    # ---- 模块 3：解析 ----
    parse_id = run_parse(harness, attachment_id)
    parse = harness.client.get(f"/api/parses/{parse_id}").json()
    document = harness.client.get(f"/api/parses/{parse_id}/document").json()
    pages = document["document"]["pages"]
    ok = parse["parse_status"] == "succeeded" and len(pages) == 1 and len(pages[0]["blocks"]) > 0
    findings.append(
        Finding(
            "3",
            "模块 3：解析成功，标准文档有页、有块（画得出证据框）",
            ok,
            f"parse_status={parse['parse_status']}，pages={len(pages)}，"
            f"blocks={len(pages[0]['blocks'])}，page_size={pages[0]['width']}x{pages[0]['height']}",
        )
    )

    # ---- 模块 4：规则命中 ----
    run_id = run_rules(harness, parse_id)
    run = harness.client.get(f"/api/runs/{run_id}").json()
    counts = run["aggregate"]["counts"]
    ok = run["run_status"] == "completed" and set(counts) == {
        "hit",
        "not_hit",
        "not_applicable",
        "needs_review",
    }
    findings.append(
        Finding(
            "4",
            "模块 4：批次完成，四态计数齐全，评价整批返回",
            ok,
            f"run_status={run['run_status']}，counts={counts}，"
            f"evaluations={len(run['evaluations'])} 条",
        )
    )

    # ---- 模块 5：结果 ----
    aggregate = run["aggregate"]
    saved = harness.client.post(
        "/tools/save_review_result",
        json={
            "run_id": run_id,
            "overall_risk_level": aggregate["overall_risk_level"],
            "summary_text": aggregate["summary"],
            "focus_points": [point.get("reason_text") or point["rule_code"] for point in aggregate["focus_points"]],
            "comment_text": "【验收走查】回写正文 v1",
        },
    )
    assert saved.status_code == 200, saved.text
    result_id = int(saved.json()["result_id"])
    row = harness.client.get(f"/api/results/{result_id}").json()
    confirmed = harness.client.post(f"/api/results/{result_id}/confirm").json()
    ok = row["confirmation_valid"] is False and confirmed["confirmation_valid"] is True
    findings.append(
        Finding(
            "5",
            "模块 5：保存（口径=聚合）→ 人工确认 → confirmation_valid 由后端翻真",
            ok,
            f"result_id={result_id}，确认前={row['confirmation_valid']}，确认后={confirmed['confirmation_valid']}",
        )
    )

    # ---- 回写链（模块 5 的终点） ----
    writeback = harness.client.post(
        "/tools/write_approval_comment",
        json={"instance_id": "HT-2026-0001", "result_id": result_id},
    ).json()
    assert writeback["outcome"] == "accepted", writeback
    attempt_id = writeback["writeback_ref"]["attempt_id"]
    gateway = _FakeCommentGateway()
    dispatched = OutboxDispatcher(harness.factory, gateway).run_once()
    attempt = harness.client.get(f"/api/writebacks/{attempt_id}").json()
    task_status = harness.client.get(f"/api/tasks/{task_id}").json()["task_status"]
    ok = (
        dispatched is True
        and attempt["delivery"] is not None
        and attempt["delivery"]["event_status"] == "delivered"
        and task_status == "done"
    )
    findings.append(
        Finding(
            "6",
            "回写链：登记意图 → Outbox 送达 → 任务 done",
            ok,
            f"attempt={attempt_id}，event_status={attempt['delivery']['event_status']}，"
            f"task_status={task_status}，外部评论 {len(gateway.comments)} 条",
        )
    )
    return findings


def thin_exit(harness: Harness) -> list[Finding]:
    """薄出口：改正文 → 新版本 + 旧确认失效 + 幂等；返回形状与工具 6 一致。"""
    findings: list[Finding] = []
    pdf = _pdf_bytes("B")
    harness.storage.put("attach-m8-b", pdf, content_type="application/pdf")
    task_id, attachment_id = seed_chain(
        harness, pdf=pdf, instance_id="HT-2026-0002", object_key="attach-m8-b"
    )
    parse_id = run_parse(harness, attachment_id)
    run_id = run_rules(harness, parse_id)
    aggregate = harness.client.get(f"/api/runs/{run_id}").json()["aggregate"]

    def save(comment: str) -> int:
        response = harness.client.post(
            "/tools/save_review_result",
            json={
                "run_id": run_id,
                "overall_risk_level": aggregate["overall_risk_level"],
                "summary_text": aggregate["summary"],
                "focus_points": [],
                "comment_text": comment,
            },
        )
        assert response.status_code == 200, response.text
        return int(response.json()["result_id"])

    result_id = save("原始正文")
    harness.client.post(f"/api/results/{result_id}/confirm")

    edit = harness.client.post(
        f"/api/results/{result_id}/comment", json={"comment_text": "人工改过的正文"}
    )
    body = edit.json()
    old = harness.client.get(f"/api/results/{result_id}").json()
    again = harness.client.post(
        f"/api/results/{result_id}/comment", json={"comment_text": "人工改过的正文"}
    ).json()

    ok = (
        edit.status_code == 200
        and body["outcome"] == "saved"
        and body["version_no"] == 2
        and old["confirmation_valid"] is False
        and again["outcome"] == "reused"
        and again["result_id"] == body["result_id"]
    )
    findings.append(
        Finding(
            "7",
            "薄出口：改正文 → 新版本 + 旧确认自动失效 + 重复提交复用",
            ok,
            f"v{body['version_no']}（outcome={body['outcome']}），"
            f"旧版本确认={old['confirmation_valid']}，二次={again['outcome']}",
        )
    )

    tool6 = harness.client.post(
        "/tools/save_review_result",
        json={
            "run_id": run_id,
            "overall_risk_level": aggregate["overall_risk_level"],
            "summary_text": aggregate["summary"],
            "focus_points": [],
            "comment_text": "外部调用方改的",
        },
    ).json()
    same_shape = set(body) == set(tool6)
    findings.append(
        Finding(
            "8",
            "薄出口返回**键集合**与工具 6 逐字相同（共用同一实现的可测证据）",
            same_shape,
            f"键差集={set(body) ^ set(tool6) or '无'}",
        )
    )
    return findings


def contract_drift(harness: Harness, *, update: bool) -> list[Finding]:
    """真实响应的键集合 vs 钉住的形状（`frontend/src/api/apiShapes.json`）。"""
    pdf = _pdf_bytes("C")
    harness.storage.put("attach-m8-c", pdf, content_type="application/pdf")
    task_id, attachment_id = seed_chain(
        harness, pdf=pdf, instance_id="HT-2026-0003", object_key="attach-m8-c"
    )
    parse_id = run_parse(harness, attachment_id)
    run_id = run_rules(harness, parse_id)
    aggregate = harness.client.get(f"/api/runs/{run_id}").json()["aggregate"]
    harness.client.post(
        "/tools/save_review_result",
        json={
            "run_id": run_id,
            "overall_risk_level": aggregate["overall_risk_level"],
            "summary_text": aggregate["summary"],
            "focus_points": [],
            "comment_text": "形状走查正文",
        },
    )
    result_id = harness.client.get("/api/results").json()["items"][0]["result_id"]
    harness.client.post(f"/api/results/{result_id}/confirm")
    harness.client.post(
        "/tools/write_approval_comment",
        json={"instance_id": "HT-2026-0003", "result_id": result_id},
    )
    OutboxDispatcher(harness.factory, _FakeCommentGateway()).run_once()

    live = collect_shapes(harness, task_id=task_id, parse_id=parse_id, run_id=run_id)

    if update:
        SHAPES_FILE.write_text(
            json.dumps(live, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return [
            Finding(
                "9",
                "契约漂移检查（--update：钉住的形状已重新生成）",
                True,
                f"已写入 {SHAPES_FILE.name}。提交这份 diff——前端 apiShape.test.ts "
                "会用同一份文件核它自己的类型化样本，哪里对不上会红在哪里。",
            )
        ]

    if not SHAPES_FILE.exists():
        return [
            Finding(
                "9",
                "契约漂移检查",
                False,
                f"钉住的形状文件不存在：{SHAPES_FILE}。先跑 "
                "`python scripts/verify_m8.py --update` 生成并提交。",
            )
        ]

    pinned = json.loads(SHAPES_FILE.read_text(encoding="utf-8"))
    differences: list[str] = []

    def compare(path: str, expected: Any, actual: Any) -> None:
        if isinstance(expected, dict) and isinstance(actual, dict):
            for key in sorted(set(expected) | set(actual)):
                if key not in actual:
                    differences.append(f"{path}.{key}：后端已不再返回（前端可能还在读）")
                elif key not in expected:
                    differences.append(f"{path}.{key}：后端新增字段（前端契约未跟）")
                else:
                    compare(f"{path}.{key}", expected[key], actual[key])
        elif json.dumps(expected, sort_keys=True) != json.dumps(actual, sort_keys=True):
            differences.append(f"{path}：形状不同（钉住={expected}，实测={actual}）")

    for name, shape in pinned.items():
        compare(name, shape, live.get(name))

    return [
        Finding(
            "9",
            "契约漂移检查（真实响应 vs 钉住形状，前端测试核同一份文件）",
            not differences,
            "；".join(differences) if differences else "全部端点的键集合与钉住形状一致",
        )
    ]


if __name__ == "__main__":
    raise SystemExit(main())
