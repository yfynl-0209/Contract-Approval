"""入队时冻结真实的 `model_version`（M11 Task 3）—— REST 与 MCP 两条路径。

两条形态必须从同一个函数（`model_version_of`）取版本：
分叉的后果见 `app/composition/llm_pipeline.py` 的 docstring。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auth import Actor, Role
from app.config import PROJECT_ROOT
from app.models import ReviewRun

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"


class StubGateway:
    """最小 LLMGateway 替身：只回答身份（本文件不断言判断质量）。"""

    @property
    def available(self) -> bool:
        return True

    @property
    def model_id(self) -> str:
        return "openai-compatible:qwen-plus"

    def extract_json(self, system, user, schema):  # noqa: ANN001
        return None

    def complete_text(self, system, user):  # noqa: ANN001
        return None


def _actor() -> Actor:
    return Actor(
        actor_id="t",
        display_name="t",
        roles=frozenset({Role.SYSTEM_ADMIN.value}),
        tenant_id="default",
    )


@pytest.fixture()
def factory(work_dir: Path) -> sessionmaker:
    path = work_dir / "model-version.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()
    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    try:
        yield sessionmaker(bind=engine, future=True)
    finally:
        engine.dispose()


@pytest.fixture()
def seeded_parse(factory):
    """一次已过门禁的解析 + 规则集（工具 5 的入队前提）。"""
    from tests.test_rule_service import _real_document, _seed_parse, _three_rules

    session = factory()
    try:
        _three_rules(session)
        document = _real_document()
        parse = _seed_parse(session, document)
        session.commit()
        return session, parse.id
    finally:
        session.close()


def _model_versions_of(session, parse_id: int) -> list[str]:
    rows = session.execute(
        select(ReviewRun).where(ReviewRun.parse_id == parse_id)
    ).scalars().all()
    return [row.model_version for row in rows]


def test_the_facade_freezes_the_declared_model_version(factory, seeded_parse) -> None:
    """没接模型 → `none:fallback`；接了 → `openai-compatible:<model>`。"""
    from app import tool_facade

    session, parse_id = seeded_parse
    tool_facade.run_contract_rules(
        str(parse_id), session=session, actor=_actor(), llm=StubGateway()
    )
    session.commit()

    tool_facade.run_contract_rules(
        str(parse_id), session=session, actor=_actor(), llm=None
    )
    session.commit()

    versions = _model_versions_of(session, parse_id)
    assert versions == ["openai-compatible:qwen-plus", "none:fallback"], (
        f"批次声明的模型与注入的网关不符：{versions}"
    )


def test_the_rest_route_forwards_the_injected_gateway(
    factory, seeded_parse, monkeypatch
) -> None:
    """`get_llm` 依赖被注入到工具 5 路由（可被 dependency_overrides 覆盖）。"""
    from app import tool_facade
    from app.api.deps import get_actor, get_db, get_llm
    from app.main import app

    session, parse_id = seeded_parse
    captured: dict[str, object] = {}

    real_facade = tool_facade.run_contract_rules

    def spying_facade(case_id, *, session, actor, force=False, llm=None):
        captured["llm"] = llm
        return real_facade(
            case_id, session=session, actor=actor, force=force, llm=llm
        )

    monkeypatch.setattr(tool_facade, "run_contract_rules", spying_facade)

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_actor] = _actor
    app.dependency_overrides[get_llm] = lambda: StubGateway()
    try:
        client = TestClient(app)
        response = client.post("/tools/run_contract_rules", json={"parse_id": parse_id})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert captured["llm"] is not None, "路由没把注入的网关传给门面"
    assert captured["llm"].model_id == "openai-compatible:qwen-plus"


def test_the_mcp_tool_five_exposes_the_same_model_version(monkeypatch) -> None:
    """MCP 形态与 REST 从**同一个函数**取网关 —— 分叉见 llm_pipeline docstring。"""
    from app import tool_facade
    from app.mcp_server import build_mcp_server

    captured: dict[str, object] = {}

    def spying_facade(case_id, *, session, actor, force=False, llm=None):
        captured["llm"] = llm
        return {"outcome": "queued", "task_ref": {"job_id": 1, "run_id": 1}}

    monkeypatch.setattr(tool_facade, "run_contract_rules", spying_facade)

    stub = StubGateway()
    server = build_mcp_server(
        gateway=object(),  # 工具 5 被替身拦截，其余工具不会真调
        storage=object(),
        engine_version="test",
        actor=_actor(),
        llm=stub,
    )
    tools = server._tool_manager._tools  # noqa: SLF001 - 测试取注册的原始函数
    tools["run_contract_rules"].fn("1")

    assert captured["llm"] is stub, "MCP 路径没把网关传给门面"
