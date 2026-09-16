"""Worker 的 LLM 接线（M11 Task 2 / Task 4）。

⚠️ 断言的是**接线**，不是"模型判得准不准" —— 后者是合格性脚本的事。
全部走**生产同款**领取路径（schema.sql 库 + `Worker.run_once`），不手搓 JobRun。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import PROJECT_ROOT
from app.enums import JobType
from app.errors import PermanentError

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"


@pytest.fixture()
def factory(work_dir: Path) -> sessionmaker:
    """独立库：交付的 schema.sql 建表（与 parse-worker 集成测试同一套路）。"""
    path = work_dir / "llm-wiring.db"
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


def _seed_rule_job(session, *, model_version: str) -> int:
    """入队一条 RULE 作业（与 tests/test_rule_service.py 的入队方式相同）。

    ⚠️ `request_rule_run` **不提交**（调用方决定事务边界）—— 而本测试的
    Worker 用**另一个**会话领取，不提交的话作业行对外不可见，永远领不到。
    """
    from tests.test_rule_service import _enqueue, _real_document, _three_rules

    _three_rules(session)
    start = _enqueue(session, _real_document(), model_version=model_version)
    session.commit()
    return start.run_id


def _seed_run_with_model_version(session, *, model_version: str) -> int:
    """Task 4 用：入队时把 `model_version` 冻结进批次（同一冻结路径）。"""
    return _seed_rule_job(session, model_version=model_version)


# ============================================================
# Task 2：判定钩子沿 make_handler 传到 run_rule_job
# ============================================================


def test_the_rule_handler_forwards_the_judge(monkeypatch, factory) -> None:
    from app.worker import Worker
    from scripts import run_worker

    seen: dict[str, object] = {}

    def fake_run_rule_job(session, *, run_id, storage, llm_judge=None, **kwargs):
        seen["llm_judge"] = llm_judge
        seen["run_id"] = run_id

    monkeypatch.setattr(run_worker, "run_rule_job", fake_run_rule_job)

    def judge(spec, text):  # noqa: ANN001 - 固定结论的替身
        from app.enums import ReasonCode
        from app.rules.evaluator import MatchResult, MatchVerdict

        return MatchResult(
            MatchVerdict.NOT_MATCHED, ReasonCode.CONDITION_NOT_MATCHED, "stub"
        )

    session = factory()
    try:
        _seed_rule_job(session, model_version="none:fallback")
    finally:
        session.close()

    worker = Worker(
        factory,
        run_worker.make_handler(None, llm_judge=judge),  # RULE 分支不碰 storage
        job_types=[JobType.RULE],
    )
    assert worker.run_once() is True
    assert seen["llm_judge"] is judge, "判定钩子没被传下去 —— llm 规则会静默走 fallback"


def test_omitting_the_judge_keeps_today_behaviour(monkeypatch, factory) -> None:
    """不传 = 没有模型 = 纯规则模式。这条是"零配置可运行"的守卫。"""
    from app.worker import Worker
    from scripts import run_worker

    seen: dict[str, object] = {"llm_judge": "未赋值"}

    def fake_run_rule_job(session, *, run_id, storage, llm_judge=None, **kwargs):
        seen["llm_judge"] = llm_judge

    monkeypatch.setattr(run_worker, "run_rule_job", fake_run_rule_job)

    session = factory()
    try:
        _seed_rule_job(session, model_version="none:fallback")
    finally:
        session.close()

    worker = Worker(factory, run_worker.make_handler(None), job_types=[JobType.RULE])
    assert worker.run_once() is True
    assert seen["llm_judge"] is None


# ============================================================
# Task 4：批次声明的模型版本与执行环境必须一致
# ============================================================


def test_a_run_declaring_qwen_fails_loudly_when_the_worker_has_no_model(factory) -> None:
    """批次声称用了模型，而执行方没有模型 —— 必须失败，不得静默走 fallback。"""
    from scripts.run_worker import _ensure_model_matches_run

    session = factory()
    try:
        run_id = _seed_run_with_model_version(
            session, model_version="openai-compatible:qwen-plus"
        )
        with pytest.raises(PermanentError, match="模型"):
            _ensure_model_matches_run(
                session, run_id=run_id, current_model_version="none:fallback"
            )
    finally:
        session.close()


def test_the_two_legacy_values_disagree_loudly(factory) -> None:
    """M5–M10 期间入队的批次声明 `none:fallback`，接上模型后重跑必须报错而不是换答案。"""
    from scripts.run_worker import _ensure_model_matches_run

    session = factory()
    try:
        run_id = _seed_run_with_model_version(session, model_version="none:fallback")
        with pytest.raises(PermanentError):
            _ensure_model_matches_run(
                session,
                run_id=run_id,
                current_model_version="openai-compatible:qwen-plus",
            )
    finally:
        session.close()


def test_matching_versions_pass(factory) -> None:
    from scripts.run_worker import _ensure_model_matches_run

    session = factory()
    try:
        run_id = _seed_run_with_model_version(session, model_version="none:fallback")
        _ensure_model_matches_run(
            session, run_id=run_id, current_model_version="none:fallback"
        )
    finally:
        session.close()
