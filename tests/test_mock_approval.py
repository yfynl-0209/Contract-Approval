"""mock 审批系统测试（M2）。

覆盖三类关键行为：

1. **接入契约**——鉴权、待办字段、详情必须携带 4 个权威业务事实字段；
2. **幂等回写**（决议 D6）——同 `idempotency_key` 重复调用返回第一次的结果，
   且**不会重复生成评论**。这是并发回写的最后一道防线之一；
3. **故障注入**——500 / 404 能按目标接口与指定审批单精确触发，
   让"接口调用失败 → blocked → 人工重试"这条分支可复现。

另外验证 `sample_pdf` 生成的"扫描件"确实是**结构正常但没有文本层**的 PDF——
如果它有文本层，逐页路由就不会走 OCR，OCR 路径的演示会失真。
"""

from __future__ import annotations

from collections.abc import Iterator

import fitz
import pytest
from fastapi.testclient import TestClient

from mock_approval.fault_inject import faults
from mock_approval.main import TOKEN, app
from mock_approval.sample_pdf import build_for_kind
from mock_approval.store import store

AUTH = {"Authorization": f"Bearer {TOKEN}"}
INSTANCE = "HT-2026-0001"
#: 该审批单的第二个附件在 fixtures 中标记为不可用（模拟审批系统已删除）
MISSING_ATTACHMENT_INSTANCE = "HT-2026-0005"
MISSING_ATTACHMENT_ID = "A-5002"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """每个测试前后都清空故障与评论状态，保证互不影响。"""
    faults.clear()
    store.reset_runtime_state()
    with TestClient(app) as test_client:
        yield test_client
    faults.clear()
    store.reset_runtime_state()


# ============================================================
# 1. 接入契约
# ============================================================


def test_health_needs_no_auth(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_pending_requires_token(client: TestClient) -> None:
    """模拟真实企业审批系统的鉴权：无凭证必须被拒绝。"""
    assert client.get("/api/instances/pending").status_code == 401


def test_pending_returns_six_instances(client: TestClient) -> None:
    """M2 完成标志之一：能拉到 6 条待办。"""
    response = client.get("/api/instances/pending", headers=AUTH)
    assert response.status_code == 200

    body = response.json()
    assert body["total"] == 6

    first = body["items"][0]
    assert set(first) == {
        "approval_code",
        "approval_title",
        "applicant_name",
        "apply_time",
        "attachment_count",
    }


def test_detail_carries_authoritative_context(client: TestClient) -> None:
    """详情必须携带 4 个权威业务事实字段。

    这些是"申请人填写"的业务事实，不是从合同文本推断的——
    缺了它们，方向敏感规则就没有语义基础。
    """
    body = client.get(f"/api/instances/{INSTANCE}", headers=AUTH).json()

    assert body["our_party_name"] == "示例科技有限公司"
    assert body["our_party_contract_label"] == "party_a"
    assert body["our_party_business_role"] == "buyer"
    assert body["contract_type"] == "procurement"
    assert body["form_data"]
    assert body["attachments"]


def test_fixtures_cover_required_demo_scenarios(client: TestClient) -> None:
    """6 条待办必须覆盖全部演示场景，否则后面的验收没有数据支撑。"""
    scenarios = {
        item["approval_code"]: item
        for item in client.get("/api/instances/pending?limit=100", headers=AUTH).json()["items"]
    }
    assert set(scenarios) == {
        "HT-2026-0001",  # 低风险基线
        "HT-2026-0002",  # 预付款 60%，方向敏感规则
        "HT-2026-0003",  # 软件开发合同，IP 缺失应命中
        "HT-2026-0004",  # 标准品采购，IP 应为 not_applicable
        "HT-2026-0005",  # 扫描件 + 缺失附件
        "HT-2026-0006",  # 立场冲突
    }


def test_download_returns_pdf_bytes(client: TestClient) -> None:
    response = client.get(
        f"/api/instances/{INSTANCE}/attachments/A-1001/download", headers=AUTH
    )
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")
    assert response.headers["x-content-kind"] == "text_pdf"


def test_download_unavailable_attachment_returns_404(client: TestClient) -> None:
    """附件被删除时必须返回错误 —— 审查系统据此进入 blocked 并允许人工重试。"""
    response = client.get(
        f"/api/instances/{MISSING_ATTACHMENT_INSTANCE}/attachments/"
        f"{MISSING_ATTACHMENT_ID}/download",
        headers=AUTH,
    )
    assert response.status_code == 404
    assert "已被删除" in response.json()["detail"]


# ============================================================
# 2. 幂等回写（决议 D6）
# ============================================================


def _write(client: TestClient, key: str, content: str = "demo review comment"):
    return client.post(
        f"/api/instances/{INSTANCE}/comments",
        headers=AUTH,
        json={"content": content, "idempotency_key": key, "operator_name": "demo"},
    )


def test_write_comment_success(client: TestClient) -> None:
    response = _write(client, "demo-key-0001")
    assert response.status_code == 200

    body = response.json()
    assert body["replayed"] is False
    assert body["comment_id"]
    assert store.comment_count(INSTANCE) == 1


def test_write_comment_is_idempotent(client: TestClient) -> None:
    """同键重复调用必须返回**第一次**的结果，且不重复生成评论。"""
    first = _write(client, "demo-key-0002").json()
    second = _write(client, "demo-key-0002").json()

    assert second["replayed"] is True
    assert second["comment_id"] == first["comment_id"]
    assert second["created_at"] == first["created_at"]
    assert store.comment_count(INSTANCE) == 1, "同键重复调用产生了重复评论"


def test_write_comment_different_key_creates_new(client: TestClient) -> None:
    _write(client, "demo-key-0003")
    _write(client, "demo-key-0004")
    assert store.comment_count(INSTANCE) == 2


def test_same_key_with_different_content_is_rejected(client: TestClient) -> None:
    """同键但内容不同必须返回 409，而不是把前一个请求的结果当作重放。

    这是对初版的修正：若把它当重放，调用方会拿到**别人的评论**却以为是自己写的。
    """
    first = _write(client, "demo-key-conflict", content="第一条意见").json()

    response = _write(client, "demo-key-conflict", content="完全不同的第二条意见")
    assert response.status_code == 409
    assert "幂等键冲突" in response.json()["detail"]

    # 关键：既没有写入新评论，也没有把第一条评论回吐给调用方
    assert store.comment_count(INSTANCE) == 1
    assert store.list_comments(INSTANCE)[0]["comment_id"] == first["comment_id"]


def test_same_key_reused_on_another_instance_is_rejected(
    client: TestClient,
) -> None:
    """同一个键用到别的审批单上也必须拒绝 —— 指纹包含审批单号。"""
    _write(client, "demo-key-cross")

    response = client.post(
        "/api/instances/HT-2026-0003/comments",
        headers=AUTH,
        json={
            "content": "demo review comment",
            "idempotency_key": "demo-key-cross",
        },
    )
    assert response.status_code == 409
    assert store.comment_count("HT-2026-0003") == 0


def test_write_comment_rejects_unknown_instance(client: TestClient) -> None:
    response = client.post(
        "/api/instances/NOT-EXIST/comments",
        headers=AUTH,
        json={"content": "x", "idempotency_key": "demo-key-0005"},
    )
    assert response.status_code == 404


def test_write_comment_rejects_short_idempotency_key(client: TestClient) -> None:
    """幂等键太短（容易碰撞）应被拒绝。"""
    response = _write(client, "short")
    assert response.status_code == 422


# ============================================================
# 3. 故障注入
# ============================================================


def test_fault_injection_returns_500(client: TestClient) -> None:
    """M2 完成标志之二：注入故障后接口返回 500。"""
    client.post(
        "/api/faults",
        headers=AUTH,
        json={"target": "list_pending", "mode": "http_500", "message": "演示用"},
    )
    response = client.get("/api/instances/pending", headers=AUTH)
    assert response.status_code == 500
    assert "注入故障" in response.json()["detail"]


def test_fault_is_scoped_to_single_instance(client: TestClient) -> None:
    """只让某一条审批单失败，其余必须正常 —— 演示时不能把整个系统打挂。"""
    client.post(
        "/api/faults",
        headers=AUTH,
        json={"target": "download", "mode": "http_500", "instance_id": INSTANCE},
    )
    assert (
        client.get(
            f"/api/instances/{INSTANCE}/attachments/A-1001/download", headers=AUTH
        ).status_code
        == 500
    )
    assert (
        client.get(
            "/api/instances/HT-2026-0003/attachments/A-1003/download", headers=AUTH
        ).status_code
        == 200
    )


def test_fault_not_found_mode(client: TestClient) -> None:
    client.post(
        "/api/faults",
        headers=AUTH,
        json={"target": "get_detail", "mode": "not_found"},
    )
    assert client.get(f"/api/instances/{INSTANCE}", headers=AUTH).status_code == 404


def test_clear_faults(client: TestClient) -> None:
    client.post("/api/faults", headers=AUTH, json={"target": "list_pending"})
    assert client.get("/api/instances/pending", headers=AUTH).status_code == 500

    cleared = client.delete("/api/faults", headers=AUTH).json()
    assert cleared["cleared"] == 1
    assert client.get("/api/instances/pending", headers=AUTH).status_code == 200


def test_fault_rejects_unknown_target(client: TestClient) -> None:
    response = client.post(
        "/api/faults", headers=AUTH, json={"target": "not_a_real_target"}
    )
    assert response.status_code == 400


# ============================================================
# 4. 占位 PDF 的行为
# ============================================================


def test_text_pdf_has_text_layer() -> None:
    """text_pdf 必须能被抽出文字，否则文本抽取路径没有东西可解析。"""
    with fitz.open(stream=build_for_kind("text_pdf"), filetype="pdf") as doc:
        text = "".join(page.get_text() for page in doc)
    assert "PLACEHOLDER" in text


def test_scan_pdf_has_no_text_layer() -> None:
    """scan_pdf 必须是**结构正常但没有文本层**的 PDF。

    只有这样才能让逐页路由判为"需要 OCR"，OCR 路径的演示才不失真；
    而且它不是损坏文件——损坏文件走的是另一条分支（blocked）。
    """
    data = build_for_kind("scan_pdf")
    assert data.startswith(b"%PDF-")

    with fitz.open(stream=data, filetype="pdf") as doc:
        assert doc.page_count == 1
        text = "".join(page.get_text() for page in doc)
    assert text.strip() == ""
