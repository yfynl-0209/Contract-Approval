"""健康检查测试：区分"给人看的诊断接口"与"给编排系统看的判据"。

两个接口的分工是本文件的主题：

| 接口 | 给谁看 | 依赖不可用时 |
| --- | --- | --- |
| `/health` | 人（诊断） | 仍 **200**，靠响应体字段说明真实状态 |
| `/health/live` | 编排系统（liveness） | 仍 **200**（进程活着） |
| `/health/ready` | 编排系统 / 负载均衡（readiness） | **503** |

**为什么必须分开**：`/health` 为了让人能拿到完整诊断信息而刻意"永远 200"，
但正因如此，它**不能**作为负载均衡的判据——数据库断开时它照样 200，
流量会被继续送给一个注定失败的实例。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app

CLIENT = TestClient(app)


# ============================================================
# 存活检查
# ============================================================


def test_health_live_returns_200() -> None:
    """进程活着就返回 200。"""
    response = CLIENT.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_health_live_is_independent_of_dependencies(monkeypatch) -> None:
    """**数据库断开时存活检查仍须 200**。

    若存活检查也依赖数据库，编排系统会认为"进程死了"而反复重启容器——
    但真正的问题在数据库，重启应用永远修不好，只会放大故障。
    """
    monkeypatch.setattr(main_module, "_probe_db", lambda: (False, []))

    response = CLIENT.get("/health/live")
    assert response.status_code == 200, "存活检查不得因依赖故障而失败"


# ============================================================
# 就绪检查
# ============================================================


def test_health_ready_returns_200_when_database_available() -> None:
    """依赖就绪时返回 200，并回传关键依赖的检查结果。"""
    response = CLIENT.get("/health/ready")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] is True
    assert body["checks"]["table_count"] == 11


def test_health_ready_returns_503_when_database_unavailable(monkeypatch) -> None:
    """**依赖不可用时必须返回 503**，负载均衡器才会把流量切走。"""
    monkeypatch.setattr(main_module, "_probe_db", lambda: (False, []))

    response = CLIENT.get("/health/ready")
    assert response.status_code == 503

    body = response.json()
    assert body["status"] == "not_ready"
    assert body["reason"] == "DATABASE_UNAVAILABLE"
    assert body["checks"]["database"] is False


# ============================================================
# 诊断接口（保留，但不得当作就绪判据）
# ============================================================


def test_health_diagnostics_returns_200_with_table_list() -> None:
    """诊断接口回传表清单，便于确认 db/schema.sql 是否已执行。"""
    response = CLIENT.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["db_ok"] is True
    assert body["table_count"] == 11
    assert "workflow_jobs" in body["tables"]
    # M4 新增的工件表也必须被探到 —— 表数写死是有意的：
    # 新增表必须显式改这条断言，而不是悄悄多出一张没人记得的表。
    assert "parse_artifacts" in body["tables"]
    assert body["llm_enabled"] is False


def test_health_diagnostics_stays_200_even_when_database_unavailable(
    monkeypatch,
) -> None:
    """诊断接口刻意保持 200：换成状态码会让调用方拿不到诊断细节。

    这条同时是"它不适合当就绪判据"的证据——
    依赖已断开，它依然返回 200。
    """
    monkeypatch.setattr(main_module, "_probe_db", lambda: (False, []))

    response = CLIENT.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["db_ok"] is False
    assert body["tables"] == []
