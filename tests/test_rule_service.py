"""批次服务的测试（M5 / T8）。

## 本文件守住的四类"写错了也不报错"

1. **六项只比了其中几项** —— 模型或提示词变了却复用旧批次：
   返回的结论仍是旧模型算的，而记录里写着新模型。**这是版本绑定字段存在的全部意义所在。**
2. **快照没规范化就比字符串** —— dict 键序不同被判成"输入变了"，
   一次无意义的重复触发凭空多出一个批次，`version_no` 白涨。
3. **规则集版本与内容脱钩** —— 改了一条规则却仍然命中旧批次，
   因为版本号只哈希了配置文本、漏了 `rule_version`。**这种漏不会报错，只会复用旧结论。**
4. **聚合结论被顺手落进 `review_results`** —— 决策 ⑦ 的边界只写在文档里会随开发自然腐蚀。

## 为什么夹具用 `schema.sql` 建表而不是 `Base.metadata`

用**交付的 DDL** 建表，才能顺带验证新增列真的写进了 `schema.sql`
（只改 `models.py` 时，ORM 测试会全绿而真实建库少一列）。
`PRAGMA foreign_keys` 是**连接级**设置，ORM 的新连接默认是关的 ——
因此本文件不需要铺满 `tasks → parses` 整条父级链；那是 `test_schema_consistency` 的职责。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import PROJECT_ROOT
from app.enums import ErrorCode, EvaluationStatus, ReasonCode, RiskLevel, RunStatus
from app.models import ContractParse, ParseArtifact, ReviewRule
from app.models import RuleEvaluation as RuleHitRow
from app.rules.applicability import ReviewContext
from app.rules.aggregator import aggregate
from app.rules.evaluator import RuleEvaluation as Evaluation
from app.services.rule_service import (
    SIX_INPUT_FIELDS,
    ActiveRule,
    complete_run,
    context_snapshot_of,
    load_active_rules,
    normalize_json,
    record_evaluations,
    ruleset_snapshot_of,
    ruleset_version_of,
    start_run,
)

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"


def _seed_parent_task(session: Session, *, tenant_id: str = "default") -> int:
    """铺一条父任务（id 必为 1）。

    M7 起查询端点强制**租户可见性**，而它的判据挂在**任务**上 ——
    `review_runs` / `rule_hits` 都没有 `tenant_id`，归属由 `task_id` 传递。
    本文件此前不需要铺 `tasks → parses` 整条父级链（见模块 docstring），
    现在读接口需要了：这是"嵌套资源也要过同一道门"的直接后果，
    不是测试的额外负担 —— 少铺这一条时，接口会干净地返回 404，
    而它看起来像"批次不存在"。
    """
    from app.models import ApprovalTask

    task = ApprovalTask(
        provider="mock",
        tenant_id=tenant_id,
        instance_id="HT-1",
        approval_code="HT-2026-0001",
        task_status="reviewing",
    )
    session.add(task)
    session.flush()
    return task.id


def _view_actor() -> "Actor":
    """直接调用查询端点函数时的主体（M7 起这些函数要求显式身份）。

    ⚠️ 本文件里的 `get_run` / `get_parse` 是**直接调用端点函数**，
    不经过 FastAPI 依赖注入 —— 因此 `actor` 的默认值 `Depends(...)`
    会原样传进来。这里给一个真实主体，而不是让端点把身份做成可选：
    可选的 `actor` 意味着"没传就是匿名"，而匿名恰恰是这些端点**必须**拒绝的。
    """
    from app.auth import Actor, Role

    return Actor(
        actor_id="rule-view",
        display_name="rule-view",
        roles=frozenset({Role.SYSTEM_ADMIN.value}),
        tenant_id="default",
    )


@pytest.fixture()
def session(work_dir: Path) -> Session:
    """独立库：用**交付的 schema.sql** 建表。"""
    path = work_dir / "rules.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()

    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    try:
        with sessionmaker(bind=engine, future=True)() as active:
            yield active
    finally:
        engine.dispose()


def _add_rule(
    session: Session,
    code: str,
    *,
    match_text: str = '{"keywords": ["自动续约"]}',
    rule_version: int = 1,
    risk_level: str = "medium",
    rule_status: str = "active",
    priority: int = 10,
) -> None:
    session.add(
        ReviewRule(
            rule_code=code,
            rule_name=f"{code} 的名称",
            rule_category="自动续约",
            risk_level=risk_level,
            rule_status=rule_status,
            priority=priority,
            rule_version=rule_version,
            match_mode="keyword",
            match_text=match_text,
        )
    )
    session.flush()


def _three_rules(session: Session) -> None:
    _add_rule(session, "R_A", priority=10)
    _add_rule(session, "R_B", priority=20, match_text='{"keywords": ["保密"]}')
    _add_rule(session, "R_C", priority=30, risk_level="high")


def _context() -> ReviewContext:
    return ReviewContext(
        contract_type="procurement", our_contract_label="party_a", our_business_role="buyer"
    )


def _start(session: Session, **overrides: object):
    rules = load_active_rules(session)
    kwargs: dict[str, object] = {
        "task_id": 1,
        "parse_id": 1,
        "context": _context(),
        "rules": rules,
        "model_version": "mock:deterministic",
        "prompt_version": "v1",
        "config_version": "engine-v1",
    }
    kwargs.update(overrides)
    return start_run(session, **kwargs)  # type: ignore[arg-type]


def _evaluation(
    code: str,
    status: EvaluationStatus = EvaluationStatus.HIT,
    risk_level: RiskLevel = RiskLevel.MEDIUM,
) -> Evaluation:
    return Evaluation(
        rule_code=code,
        rule_version=1,
        status=status,
        risk_level=risk_level,
        reason_code=(
            ReasonCode.CONDITION_MATCHED
            if status is EvaluationStatus.HIT
            else ReasonCode.CONDITION_NOT_MATCHED
        ),
        reason_text=f"{code} 的说明",
        evidence_text="证据片段" if status is EvaluationStatus.HIT else None,
        # ⚠️ `evidence_json` 才是**接口返回的那一份**（§4.6 的 `evidence: [...]`）：
        # T6 的 `attach_evidence` 三列成对写入，因此这里也要成对 ——
        # 只设 `evidence_text` 会让"命中却无证据"看起来通过了。
        evidence_json=(
            json.dumps([{"text": "证据片段", "position": {"page": 1}}], ensure_ascii=False)
            if status is EvaluationStatus.HIT
            else None
        ),
        hit_detail={"actual": "0.6"} if status is EvaluationStatus.HIT else {},
    )


# ============================================================
# 1. 规则集版本（验收 12）
# ============================================================


def test_ruleset_version_is_stable_and_order_independent(session: Session) -> None:
    """**验收 12 前半**：同规则集 → 同摘要，且**与传入顺序无关**。

    顺序不是内容：按 `priority` 排一下序就换一个版本号，
    会凭空产生新批次，而"规则集变了"这件事其实没发生。
    """
    _three_rules(session)
    rules = load_active_rules(session)

    assert ruleset_version_of(rules) == ruleset_version_of(tuple(reversed(rules)))
    assert ruleset_version_of(rules) == ruleset_version_of(load_active_rules(session))


def test_ruleset_version_changes_when_a_rule_version_changes(session: Session) -> None:
    """**验收 12 后半**：改一条规则的 `rule_version` → 版本必须变。

    ⚠️ 这是最容易漏的一项：只哈希 `match_text` 之类的内容时，
    "规则升级了版本号"不会换批次 —— 于是新规则集仍然复用旧结论，
    而库里 `ruleset_version` 与 `rule_version` **都写着新值**，两处都不报错。
    """
    _three_rules(session)
    before = ruleset_version_of(load_active_rules(session))

    session.execute(
        text("UPDATE review_rules SET rule_version = 2 WHERE rule_code = 'R_B'")
    )
    session.flush()

    assert ruleset_version_of(load_active_rules(session)) != before


def test_ruleset_version_changes_when_a_rule_content_changes(session: Session) -> None:
    _three_rules(session)
    before = ruleset_version_of(load_active_rules(session))

    session.execute(
        text("UPDATE review_rules SET match_text = '{\"keywords\": [\"续约\"]}' WHERE rule_code = 'R_A'")
    )
    session.flush()

    assert ruleset_version_of(load_active_rules(session)) != before


def test_only_active_rules_are_loaded(session: Session) -> None:
    """停用的规则不参与评价，也不该进快照 —— 否则"停用一条"会换批次，而它压根没跑。"""
    _three_rules(session)
    _add_rule(session, "R_OFF", rule_status="inactive", priority=99)

    codes = {item.config.rule_code for item in load_active_rules(session)}

    assert codes == {"R_A", "R_B", "R_C"}


# ============================================================
# 2. 批次幂等：六项（验收 13 / 23）
# ============================================================


def test_six_identical_inputs_reuse_the_same_run(session: Session) -> None:
    """六项全同 → **复用**：不新建序号、不重跑、不覆盖历史。"""
    _three_rules(session)
    first = _start(session)
    second = _start(session)

    assert second.reused is True
    assert second.run_id == first.run_id
    assert second.version_no == first.version_no


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("parse_id", 2),
        ("model_version", "mock:other"),
        ("prompt_version", "v2"),
        ("config_version", "engine-v2"),
    ],
    ids=["parse", "model", "prompt", "config"],
)
def test_changing_any_single_input_creates_a_new_run(
    session: Session, field: str, value: object
) -> None:
    """**验收 13：逐项遍历六项**（这里 4 项由参数直接改，另 2 项见下方两条）。

    ⚠️ 必须**逐项**，不能抽查一项：抽查会漏掉"六项里只比了三项"这种缺陷 ——
    而它的表现是"换了模型却复用旧结论"，复用的那条**看起来完全正常**。
    """
    _three_rules(session)
    first = _start(session)
    second = _start(session, **{field: value})

    assert second.reused is False
    assert second.version_no == first.version_no + 1


def test_changing_the_context_creates_a_new_run(session: Session) -> None:
    """第 2 项：权威上下文（立场 / 合同类型）变了。"""
    _three_rules(session)
    first = _start(session)
    second = _start(
        session,
        context=ReviewContext(
            contract_type="procurement",
            our_contract_label="party_b",
            our_business_role="seller",
        ),
    )

    assert second.reused is False
    assert second.version_no == first.version_no + 1


def test_changing_the_ruleset_creates_a_new_run(session: Session) -> None:
    """第 3 项：规则集变了。"""
    _three_rules(session)
    first = _start(session)

    _add_rule(session, "R_D", priority=40)
    second = _start(session)

    assert second.reused is False
    assert second.version_no == first.version_no + 1


def test_force_creates_a_new_run_even_when_identical(session: Session) -> None:
    """**验收 23**：`force` 必须能穿透幂等。

    含 `needs_review` 的批次（单次 LLM 失败）需要"修好后再跑一次"。
    没有强制开关时，用户唯一能做的就是**改一个不相关的参数去骗过缓存** ——
    而那会污染版本绑定：库里记着一个从未生效的 `model_version`。
    """
    _three_rules(session)
    first = _start(session)
    forced = _start(session, force=True)

    assert forced.reused is False
    assert forced.version_no == first.version_no + 1


def test_history_is_preserved_when_a_new_run_is_created(session: Session) -> None:
    _three_rules(session)
    first = _start(session)
    record_evaluations(
        session,
        run=first,
        task_id=1,
        rules=load_active_rules(session),
        evaluations=[_evaluation("R_A")],
    )
    second = _start(session, force=True)

    rows = session.execute(
        select(func.count()).select_from(RuleHitRow).where(RuleHitRow.run_id == first.run_id)
    ).scalar_one()

    assert first.run_id != second.run_id
    assert rows == 1, "新建批次不得覆盖旧批次的评价"


def test_version_no_starts_at_one(session: Session) -> None:
    _three_rules(session)

    assert _start(session).version_no == 1


# ============================================================
# 3. 快照规范化
# ============================================================


def test_context_snapshot_is_normalized() -> None:
    """键序不同但语义相同的上下文 → **同一个快照**。

    不规范化会让一次无意义的重复触发凭空多出一个批次（`version_no` 白涨，
    历史里多一份内容完全相同的评价）—— 而 `UNIQUE(task_id, version_no)` 拦不住它，
    因为从数据库的角度看，那确实是一个新的版本号。
    """
    left = normalize_json({"a": 1, "b": 2})
    right = normalize_json({"b": 2, "a": 1})

    assert left == right


def test_context_snapshot_excludes_the_contract_text() -> None:
    """正文属于**解析**（第 1 项输入），不属于上下文。

    混进来会让"解析结果变了"与"立场变了"无法区分 ——
    而这两件事的处置完全不同（重新解析 vs 修正立场）。
    """
    snapshot = json.loads(context_snapshot_of(_context()))

    assert "contract_text" not in snapshot
    assert set(snapshot) == {
        "contract_type",
        "our_contract_label",
        "our_business_role",
        "party_context_status",
        "contract_type_status",
    }


def test_whitespace_only_difference_still_reuses_the_run(session: Session) -> None:
    """同一份语义内容（快照规范化后相同）→ 复用，不新建。"""
    _three_rules(session)
    first = _start(session)
    second = _start(session, context=ReviewContext(**vars(_context())))

    assert second.reused is True
    assert second.run_id == first.run_id


# ============================================================
# 4. 落库
# ============================================================


def test_all_four_states_are_persisted(session: Session) -> None:
    """四态**都要落**，包括 `not_hit` 与 `not_applicable`。

    少了它们就答不出"这条规则为什么没报警" —— 而那正是这张表
    连不适用规则都保留的全部理由（验收 15 / 22）。
    """
    _three_rules(session)
    run = _start(session)
    rules = load_active_rules(session)

    written = record_evaluations(
        session,
        run=run,
        task_id=1,
        rules=rules,
        evaluations=[
            _evaluation("R_A", EvaluationStatus.HIT, RiskLevel.HIGH),
            _evaluation("R_B", EvaluationStatus.NOT_HIT),
            _evaluation("R_C", EvaluationStatus.NOT_APPLICABLE),
        ],
    )

    # ⚠️ 属性是 `evaluation_status`，物理列名才是 `hit_status`（需求 2.4.9 规定）。
    # 按列名写属性会 AttributeError —— 这类"两处各叫一个名字"的映射，
    # 错的那一侧永远是静默的（列名对、属性名错）。
    rows = session.execute(
        select(RuleHitRow.evaluation_status, func.count())
        .where(RuleHitRow.run_id == run.run_id)
        .group_by(RuleHitRow.evaluation_status)
    ).all()

    assert written == 3
    assert dict(rows) == {"hit": 1, "not_hit": 1, "not_applicable": 1}


def test_persisted_row_carries_evidence_and_detail(session: Session) -> None:
    _three_rules(session)
    run = _start(session)

    record_evaluations(
        session,
        run=run,
        task_id=1,
        rules=load_active_rules(session),
        evaluations=[_evaluation("R_A")],
    )

    row = session.execute(select(RuleHitRow).where(RuleHitRow.run_id == run.run_id)).scalars().one()

    assert row.task_id == 1
    assert row.rule_version == 1
    assert row.reason_code == ReasonCode.CONDITION_MATCHED.value
    assert row.evidence_text == "证据片段"
    assert json.loads(row.hit_detail_json or "{}") == {"actual": "0.6"}
    assert row.rule_id == next(
        item.rule_id for item in load_active_rules(session) if item.config.rule_code == "R_A"
    )


def test_unknown_rule_evaluation_is_rejected(session: Session) -> None:
    """评价里出现**不在本批次启用规则集**里的规则 → 抛错，不静默多写一行。

    让它写进去的后果是"某条规则在库里被评了，但它压根不在本批次" ——
    而 `UNIQUE(run_id, rule_id)` 拦不住（那是个合法的 rule_id）。
    """
    _three_rules(session)
    run = _start(session)

    with pytest.raises(ValueError, match="不在本批次的启用规则集内"):
        record_evaluations(
            session,
            run=run,
            task_id=1,
            rules=load_active_rules(session),
            evaluations=[_evaluation("R_NOT_THERE")],
        )


def test_duplicate_rule_evaluation_is_rejected(session: Session) -> None:
    """同一条规则在一个批次里被评两次 → 抛错（而不是交给唯一约束去炸）。"""
    _three_rules(session)
    run = _start(session)

    with pytest.raises(ValueError, match="被评价了两次"):
        record_evaluations(
            session,
            run=run,
            task_id=1,
            rules=load_active_rules(session),
            evaluations=[_evaluation("R_A"), _evaluation("R_A")],
        )


# ============================================================
# 5. 收尾与边界（决策 ⑦ / 验收 22）
# ============================================================


def test_complete_run_marks_completed_and_writes_no_results(session: Session) -> None:
    """**验收 22 的边界断言**：跑完一个批次后，`review_results` **仍为 0**。

    ⚠️ 这条**可证伪**：只写在文档里的边界会随着后续开发自然腐蚀 ——
    M4 的 `M4_ERROR_CODES` 守卫就是这么变成空转的。
    """
    _three_rules(session)
    run = _start(session)
    evaluations = [_evaluation("R_A"), _evaluation("R_B", EvaluationStatus.NOT_HIT)]
    record_evaluations(
        session, run=run, task_id=1, rules=load_active_rules(session), evaluations=evaluations
    )
    complete_run(session, run=run, aggregate=aggregate(evaluations))
    session.commit()

    row = session.execute(text("SELECT run_status, finished_at FROM review_runs")).one()
    results = session.execute(text("SELECT COUNT(*) FROM review_results")).scalar_one()

    assert row.run_status == RunStatus.COMPLETED.value
    assert row.finished_at is not None
    assert results == 0, "M5 不写 review_results（决策 ⑦）"


def test_persisted_rows_match_the_aggregate_counts(session: Session) -> None:
    """落库的行数与聚合计数必须一致 —— 两套算法各算一遍时，差值就是缺陷藏身处。"""
    _three_rules(session)
    run = _start(session)
    evaluations = [
        _evaluation("R_A"),
        _evaluation("R_B", EvaluationStatus.NOT_HIT),
        _evaluation("R_C", EvaluationStatus.NEEDS_REVIEW),
    ]
    record_evaluations(
        session, run=run, task_id=1, rules=load_active_rules(session), evaluations=evaluations
    )
    summary = aggregate(evaluations)

    # ⚠️ 属性是 `evaluation_status`，物理列名才是 `hit_status`（需求 2.4.9 规定）。
    # 按列名写属性会 AttributeError —— 这类"两处各叫一个名字"的映射，
    # 错的那一侧永远是静默的（列名对、属性名错）。
    rows = session.execute(
        select(RuleHitRow.evaluation_status, func.count())
        .where(RuleHitRow.run_id == run.run_id)
        .group_by(RuleHitRow.evaluation_status)
    ).all()
    counts = dict(rows)

    assert counts.get("hit", 0) == summary.counts["hit"]
    assert sum(counts.values()) == summary.total == len(evaluations)


def test_six_input_fields_are_all_compared(session: Session) -> None:
    """`SIX_INPUT_FIELDS` 与 `review_runs` 的列必须对得上。

    判据表写错一个列名时，`_find_reusable` 会 `AttributeError`（那是响的）；
    但**漏写一项**是静的 —— 那一项变了也不新建批次，结论却是旧输入算的。
    """
    columns = {row[1] for row in session.execute(text("PRAGMA table_info(review_runs)"))}

    assert set(SIX_INPUT_FIELDS) <= columns


def test_ruleset_snapshot_is_stored_and_reconstructs_the_rules(session: Session) -> None:
    """**P2 审计**：批次自带当时的**规则集内容**，事后能还原出"按哪版规则判的"。

    只存哈希是不够的：规则记录会被原地修改，之后仅凭哈希与"当前规则表"
    还原不出当时的配置 —— 而那是审计里第一个会被问到的问题。
    """
    _three_rules(session)
    run = _start(session)

    stored = session.execute(
        text("SELECT ruleset_snapshot_json, ruleset_version FROM review_runs WHERE id = :i"),
        {"i": run.run_id},
    ).one()
    snapshot, version = stored[0], stored[1]

    assert snapshot == ruleset_snapshot_of(load_active_rules(session))
    # 版本由快照派生 → 两者不可能漂移（"版本没变、内容却变了"发不出来）
    import hashlib

    assert version == hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    codes = {item["rule_code"] for item in json.loads(snapshot)}
    assert codes == {"R_A", "R_B", "R_C"}


def test_ruleset_snapshot_survives_a_later_rule_edit(session: Session) -> None:
    """规则被改之后，**旧批次仍能还原当时的配置** —— 这正是存快照的目的。"""
    _three_rules(session)
    run = _start(session)
    before = ruleset_snapshot_of(load_active_rules(session))

    session.execute(
        text("UPDATE review_rules SET match_text = '{\"keywords\": [\"改了\"]}' WHERE rule_code = 'R_A'")
    )
    session.flush()

    stored = session.execute(
        text("SELECT ruleset_snapshot_json FROM review_runs WHERE id = :i"), {"i": run.run_id}
    ).scalar_one()

    assert stored == before
    assert ruleset_snapshot_of(load_active_rules(session)) != before


def test_active_rules_expose_ids_for_the_foreign_key(session: Session) -> None:
    """`ActiveRule` 必须带 `rule_id`：`rule_hits.rule_id` 是外键，缺它写不进去。"""
    _three_rules(session)

    for item in load_active_rules(session):
        assert isinstance(item, ActiveRule)
        assert item.rule_id > 0


# ============================================================
# 6. 批次执行（M5 端到端的那一步）
# ============================================================


def _real_document(name: str = "contract_01_clean.pdf"):
    """真实夹具 → 标准文档（证据定位要有真文本才测得出来）。"""
    from app.adapters.parse.pymupdf_extractor import PyMuPdfExtractor
    from app.services.document_builder import DocumentBuilder

    fixtures = PROJECT_ROOT / "mock_approval" / "fixtures"
    with PyMuPdfExtractor.open((fixtures / name).read_bytes()) as extractor:
        return DocumentBuilder(extractor).build()


def _run(session: Session, **overrides: object):
    from app.services.rule_service import run_batch

    document = _real_document()
    kwargs: dict[str, object] = {
        "task_id": 1,
        "parse_id": 1,
        "context": _context(),
        "fields": {},
        "text": "\n".join(page.text for page in document.pages),
        "document": document,
        "model_version": "mock:deterministic",
        "prompt_version": "v1",
        "config_version": "engine-v1",
    }
    kwargs.update(overrides)
    return run_batch(session, **kwargs)  # type: ignore[arg-type]


def _rows_of(session: Session, run_id: int) -> int:
    return session.execute(
        select(func.count()).select_from(RuleHitRow).where(RuleHitRow.run_id == run_id)
    ).scalar_one()


def test_run_batch_evaluates_every_active_rule_once(session: Session) -> None:
    """**验收 1**：每条启用规则**各产生且仅产生一条**评价。

    用三条规则跑通同一段路径：四态计数之和 == 规则数，且库里行数 == 规则数。
    两处都断言，是因为"计数对了但没落库"与"落库了但计数漏了"是两种不同的缺陷。
    """
    _three_rules(session)

    result = _run(session)

    assert result.reused is False
    assert _rows_of(session, result.run_id) == 3
    assert result.aggregate.total == 3
    assert sum(result.aggregate.counts.values()) == len(result.evaluations)


def test_run_batch_reuse_does_not_re_evaluate_anything(session: Session, monkeypatch) -> None:
    """**复用时不重跑 —— 一次都不跑。**

    ⚠️ 这里用**替换掉 `evaluate_rule`** 来证明，而不是比较两次的输出：
    输出相同只能说明"结果一样"，说明不了"没有重跑"。
    而重跑是**有代价且有害**的 —— 含 `needs_review` 的批次会在用户没要求的情况下
    改变结论，且"复用"这个词就不再成立。
    """
    from app.services import rule_service

    _three_rules(session)
    first = _run(session)
    rows_before = _rows_of(session, first.run_id)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("复用批次时不得重新评价任何规则")

    monkeypatch.setattr(rule_service, "evaluate_rule", _boom)
    second = _run(session)

    assert second.reused is True
    assert second.run_id == first.run_id
    assert second.version_no == first.version_no
    assert _rows_of(session, first.run_id) == rows_before
    assert second.aggregate.total == first.aggregate.total


def test_evaluations_round_trip_through_the_database(session: Session) -> None:
    """**往返断言**：写进去什么，读回来必须一样。

    这是 `record_evaluations` 与 `_evaluation_of` 这一对**逆映射**的守卫。
    两处一旦不一致，写入是对的、读回来是错的（或反之），而两边都不报错 ——
    表现是"界面上的理由与实际判定不符"，而没有任何一处日志会提示这件事。
    """
    from app.services.rule_service import evaluations_of_run

    _three_rules(session)
    result = _run(session)

    by_code = {item.rule_code: item for item in evaluations_of_run(session, result.run_id)}

    assert set(by_code) == {item.rule_code for item in result.evaluations}
    for original in result.evaluations:
        copy = by_code[original.rule_code]
        assert copy.status is original.status
        assert copy.risk_level is original.risk_level
        assert copy.reason_code is original.reason_code
        assert copy.reason_text == original.reason_text
        assert copy.evidence_text == original.evidence_text
        assert copy.evidence_json == original.evidence_json
        # JSON 往返后元组会变成列表，因此按**规范化后的形式**比
        assert copy.hit_detail == json.loads(normalize_json(original.hit_detail))


# ============================================================
# 7. 装配：解析产物 → 批次输入（T9 / worker 用）
# ============================================================


class _Storage:
    """只实现 `get` 的假对象存储 —— `load_batch_inputs` 只读不写。"""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def get(self, key: str) -> bytes:
        return self._objects[key]


def _seed_parse(session: Session, document, *, parse_status: str = "succeeded", error_code=None):
    """把 M4 的解析产物写进库（字段 JSON + 标准文档工件）。"""
    from app.models import ApprovalTask
    from app.services.field_extractor import FieldExtractor

    # `context_for_parse` 要读审批任务里的立场/合同类型 —— 缺它就只能报"任务不存在"
    if session.get(ApprovalTask, 1) is None:
        session.add(
            ApprovalTask(
                provider="mock",
                tenant_id="default",
                instance_id="HT-1",
                approval_code="HT-2026-0001",
                task_status="reviewing",
                our_party_contract_label="party_a",
                our_party_business_role="buyer",
                contract_type="procurement",
            )
        )
        session.flush()

    extraction = FieldExtractor(document).extract()
    parse = ContractParse(
        task_id=1,
        attachment_id=1,
        parse_status=parse_status,
        parse_version=1,
        parser_name="pymupdf",
        parser_version="test",
        parse_error_code=error_code,
        parse_error="测试" if error_code else None,
        basic_info_json=extraction.basic_info.model_dump_json(),
        clause_info_json=extraction.clause_info.model_dump_json(),
    )
    session.add(parse)
    session.flush()

    session.add(
        ParseArtifact(
            parse_id=parse.id,
            kind="standard_document",
            object_key="doc-1",
            sha256="a" * 64,
            size_bytes=len(document.model_dump_json()),
            content_type="application/json",
            artifact_version=1,
        )
    )
    session.flush()
    return parse


def test_load_batch_inputs_restores_the_standard_document(session: Session) -> None:
    """装配必须**还原标准文档**，不能只读字段 JSON。

    `keyword` / `regex` / `llm` 三类规则要的是**合同正文**，而正文只存在于文档工件里
    （`DocumentPage.text` 是权威明文）。只读字段的装配会让这三类规则**全部判不了**，
    而报告上看起来像"合同里没有这些内容"。
    """
    from app.services.rule_service import load_batch_inputs

    document = _real_document()
    parse = _seed_parse(session, document)

    inputs = load_batch_inputs(
        session, parse_id=parse.id, storage=_Storage({"doc-1": document.model_dump_json().encode()})
    )

    assert inputs.parse_id == parse.id
    assert len(inputs.document.pages) == len(document.pages)
    assert inputs.document.pages[0].text == document.pages[0].text
    assert "采购合同" in inputs.text


def test_load_batch_inputs_includes_the_derived_fields(session: Session) -> None:
    """**T3a 必须在装配里被接上**：6 条 expr 规则读的是派生字段。

    M4 只抽直接字段，派生字段没有任何生产者 —— 漏掉这一步的表现是
    `is_null` 类规则报"未约定预付款"（一条**业务结论**），而报告上看不出这是实现缺口。
    """
    from app.services.rule_service import load_batch_inputs

    document = _real_document("contract_02_prepay.pdf")
    parse = _seed_parse(session, document)

    inputs = load_batch_inputs(
        session, parse_id=parse.id, storage=_Storage({"doc-1": document.model_dump_json().encode()})
    )

    assert "prepay_ratio" in inputs.fields
    assert inputs.fields["prepay_ratio"].value_decimal == "0.6"
    assert inputs.currency == "CNY"


def test_load_batch_inputs_refuses_a_parse_that_failed_the_gate(session: Session) -> None:
    """**门禁**：`parse_status != 'succeeded'` 的解析不得进入规则评价。

    在残缺的输入上跑 40 条规则，得到的是一份"看起来正常"的报告，
    而它的依据本来就是不可用的。

    ⚠️ 错误码用**库里记着的那个**（M4 写的 `parse_error_code`），不另发明一个：
    另发明会让"解析失败"与"解析没过门禁"看起来是两种不同的故障。
    """
    from app.errors import PermanentError
    from app.services.rule_service import load_batch_inputs

    document = _real_document()
    parse = _seed_parse(session, document, parse_status="failed", error_code="DOCUMENT_EMPTY")

    with pytest.raises(PermanentError) as caught:
        load_batch_inputs(session, parse_id=parse.id, storage=_Storage({}))

    assert caught.value.code is ErrorCode.DOCUMENT_EMPTY
    assert "质量门禁" in caught.value.message


# ============================================================
# 8. 入队 → 执行（T9 链路；评审 P0② / P1 的回归）
# ============================================================


def _enqueue(session: Session, document, **overrides: object):
    from app.services.rule_service import context_for_parse, request_rule_run

    parse = _seed_parse(session, document)
    kwargs: dict[str, object] = {
        "parse_id": parse.id,
        "context": context_for_parse(session, parse.id),
        "prompt_version": "v1",
    }
    kwargs.update(overrides)
    return request_rule_run(session, **kwargs)  # type: ignore[arg-type]


def _storage_for(document) -> "_Storage":
    return _Storage({"doc-1": document.model_dump_json().encode()})


def test_request_rule_run_enqueues_a_pollable_job(session: Session) -> None:
    """**验收新增项**：工具 5 的入队结果必须带**可轮询的作业**。

    只给 `run_id` 的话，调用方只能去轮询批次 —— 而批次一建出来就是 `running`，
    没有"排队中"这个状态，于是"入队了没"这件事无法回答。
    """
    from app.models import WorkflowJob

    _three_rules(session)
    start = _enqueue(session, _real_document())

    assert start.job_id is not None, "TaskRef 必须带 job_id"
    job = session.get(WorkflowJob, start.job_id)
    assert job is not None
    assert job.job_type == "rule"
    assert json.loads(job.input_json)["run_id"] == start.run_id, (
        "作业必须指向它自己那个批次"
    )


def test_repeat_enqueue_reports_the_real_job_status(session: Session) -> None:
    """**评审 P1**：重复请求命中同一批次时，状态取自**作业**，不从 `reused` 推断。

    六项全同但作业仍在排队/运行时，把它答成"completed"会让调用方停止轮询，
    然后去读一个**还没有结论**的批次。
    """
    from app.services.rule_service import context_for_parse, request_rule_run

    _three_rules(session)
    document = _real_document()
    # ⚠️ 解析记录只能种一次：`UNIQUE(attachment_id, parse_version)` 是硬约束，
    # 种两次会以约束冲突的形式报错 —— 与"重复入队"这件事毫无关系。
    parse = _seed_parse(session, document)
    context = context_for_parse(session, parse.id)
    first = request_rule_run(
        session, parse_id=parse.id, context=context, prompt_version="v1"
    )
    second = request_rule_run(
        session, parse_id=parse.id, context=context, prompt_version="v1"
    )

    assert second.reused is True
    assert second.run_id == first.run_id
    assert second.job_status != "completed", "作业还没被 Worker 领取，不得答已完成"
    assert second.job_status == first.job_status


def test_execute_existing_run_uses_the_frozen_ruleset(session: Session) -> None:
    """⚠️ **P0② 的核心回归**：入队到执行之间规则变了，作业仍须评价**它自己那个批次**。

    过去的实现会在这里再调一次 `start_run`，于是建出**另一个**批次：
    作业成功、`result_ref` 指向入队时那个（空的）批次、结论全无。

    断言三件事，缺一不可：

    1. 评价的是**同一个** `run_id`；
    2. 库里批次总数**没有增加**；
    3. 评价条数 == **冻结时**的规则数（新加的第 4 条**不得**被评）。
    """
    from app.models import ReviewRun
    from app.services.rule_service import execute_existing_run

    _three_rules(session)
    document = _real_document()
    queued = _enqueue(session, document)

    # 入队之后：新增一条规则（规则集变了）
    _add_rule(session, "R_LATE", priority=40)

    result = execute_existing_run(
        session, run_id=queued.run_id, storage=_storage_for(document)
    )

    runs = session.execute(select(func.count()).select_from(ReviewRun)).scalar_one()
    assert result.run_id == queued.run_id
    assert runs == 1, "执行不得另建批次"
    assert _rows_of(session, queued.run_id) == 3, "只评冻结时的那 3 条规则"
    assert result.aggregate.total == 3


def test_execute_existing_run_is_idempotent(session: Session) -> None:
    """完整批次再执行一次 → 复用，不重跑、不新增行。"""
    from app.services.rule_service import execute_existing_run

    _three_rules(session)
    document = _real_document()
    queued = _enqueue(session, document)
    first = execute_existing_run(
        session, run_id=queued.run_id, storage=_storage_for(document)
    )
    rows_after_first = _rows_of(session, queued.run_id)

    second = execute_existing_run(
        session, run_id=queued.run_id, storage=_storage_for(document)
    )

    assert second.reused is True
    assert _rows_of(session, queued.run_id) == rows_after_first
    assert second.aggregate.counts == first.aggregate.counts


def test_execute_existing_run_refuses_a_deleted_rule(session: Session) -> None:
    """⚠️ **停用**不影响执行；**删除**必须明确拒绝。

    静默改用"现在还在的那几条"会让本批次的结论与它声称的规则集不一致 ——
    而 `ruleset_version` 仍指向那份旧快照，**两处都不报错**。
    """
    from app.errors import PermanentError
    from app.services.rule_service import execute_existing_run

    _three_rules(session)
    document = _real_document()
    queued = _enqueue(session, document)

    session.execute(text("DELETE FROM review_rules WHERE rule_code = 'R_B'"))
    session.flush()

    with pytest.raises(PermanentError) as caught:
        execute_existing_run(
            session, run_id=queued.run_id, storage=_storage_for(document)
        )

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND
    assert "删除" in caught.value.message


def test_the_full_chain_keeps_the_same_run_id(session: Session) -> None:
    """**验收 29–31 的合成**：入队 → 执行 → 查询，全程指向**同一个批次**。

    ```text
    request_rule_run  → 建批次 run_id=N + 建 RULE 作业（作业输入带 run_id=N）
    run_rule_job      → 执行 run_id=N（不得另建）
    get_run(N)        → 批次 completed、聚合非空、评估条数 == 冻结的规则数
    ```

    ⚠️ 这是"三条独立链路回归"的合成版：分开测能定位是哪一环坏了，
    合起来测才能证明**它们串起来是对的** —— 而"每一环都对、串起来错"
    正是本轮修掉的那个缺陷（作业指向的批次与执行的批次不是同一个）。
    """
    from app.api.jobs import get_run
    from app.models import ReviewRun, WorkflowJob
    from app.services.rule_service import execute_existing_run

    _three_rules(session)
    document = _real_document()
    start = _enqueue(session, document)

    # ① 入队：作业与批次互相指向
    job = session.get(WorkflowJob, start.job_id)
    assert json.loads(job.input_json)["run_id"] == start.run_id

    # ② 执行（worker 走的就是 `run_rule_job`，此处等价调用）
    result = execute_existing_run(
        session, run_id=start.run_id, storage=_storage_for(document)
    )

    # ③ 查询：批次已收尾，且**没有第二个批次**
    payload = get_run(start.run_id, session=session, actor=_view_actor())
    total_runs = session.execute(select(func.count()).select_from(ReviewRun)).scalar_one()

    assert result.run_id == start.run_id
    assert total_runs == 1, "全链路只该有一个批次"
    assert payload["run_status"] == RunStatus.COMPLETED.value
    assert sum(payload["aggregate"]["counts"].values()) == 3
    assert len(payload["evaluations"]) == 3
    assert payload["aggregate"]["summary"], "摘要不得为空"


def test_the_worker_claims_and_executes_a_rule_job(work_dir: Path) -> None:
    """**验收 31 的进程级证明**：真实 `Worker` 领取 RULE 作业 → 分派 → 执行 → 批次收尾。

    前面几条用的是服务层函数；这一条把**真的 Worker**（含领取、租约、条件完成、
    一次提交）接上 `scripts/run_worker.py` 用的同一个 `make_handler`，
    因此"入队 → 领取 → 执行 → 结果"这条链第一次整条跑通。

    ⚠️ Worker 要的是**会话工厂**（每个作业一个独立 Session，不得共享），
    因此这里自建一个临时库 —— 不能复用 `session` 夹具（它是单个 Session）。
    """
    from sqlalchemy.orm import sessionmaker as make_sessionmaker

    from app.enums import JobStatus, JobType
    from app.models import ReviewRun, WorkflowJob
    from scripts.run_worker import make_handler
    from app.worker import Worker

    path = work_dir / "worker-chain.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()

    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    factory = make_sessionmaker(bind=engine, future=True)
    try:
        with factory() as seeding:
            document = _real_document()
            _three_rules(seeding)
            start = _enqueue(seeding, document)
            seeding.commit()

        worker = Worker(
            factory,
            make_handler(_storage_for(document)),
            job_types=[JobType.RULE],
        )
        assert worker.run_once() is True, "应当领到那条 RULE 作业"

        with factory() as check:
            job = check.get(WorkflowJob, start.job_id)
            run = check.get(ReviewRun, start.run_id)
            rows = _rows_of(check, start.run_id)

        assert job.job_status == JobStatus.SUCCEEDED.value
        assert run.run_status == RunStatus.COMPLETED.value
        assert rows == 3, "三条启用规则各产生且仅产生一条评价"
        assert check_run_count(factory) == 1, "全链路只该有一个批次"
    finally:
        engine.dispose()


def check_run_count(factory) -> int:
    from app.models import ReviewRun

    with factory() as session:
        return session.execute(
            select(func.count()).select_from(ReviewRun)
        ).scalar_one()


def _batch_on_fixture(work_dir: Path, fixture: str, *, tag: str):
    """在**独立临时库**（schema + seed）上跑一次完整批次，返回 `(聚合, 按规则码索引的评价)`。

    ⚠️ 每个夹具一个独立库：`UNIQUE(contract_parses.attachment_id, parse_version)` 是硬约束，
    同一个库里种第二份解析会以约束冲突报错 —— 而那与"另一份合同"毫无关系。
    """
    from sqlalchemy.orm import sessionmaker as make_sessionmaker

    from app.services.rule_service import load_batch_inputs, run_batch

    path = work_dir / f"batch-{tag}.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.executescript((PROJECT_ROOT / "db" / "seed.sql").read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()

    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    factory = make_sessionmaker(bind=engine, future=True)
    try:
        with factory() as session:
            document = _real_document(fixture)
            parse = _seed_parse(session, document)
            # ⚠️ 字段必须走**真实装配**（`load_batch_inputs`）：手写一个 `fields={}`
            # 会让所有字段相关规则变成"解析结果里没有这个字段" → needs_review，
            # 于是断言失败的原因与"批次跑得对不对"毫无关系。
            inputs = load_batch_inputs(
                session, parse_id=parse.id, storage=_storage_for(document)
            )
            result = run_batch(
                session,
                task_id=1,
                parse_id=parse.id,
                context=ReviewContext(
                    contract_type="procurement",
                    our_contract_label="party_a",
                    our_business_role="buyer",
                ),
                fields=inputs.fields,
                text=inputs.text,
                document=inputs.document,
                model_version="none:fallback",
                prompt_version="v1",
                config_version="engine-v1",
            )
            session.commit()
            by_code = {item.rule_code: item for item in result.evaluations}
            return result.aggregate, by_code
    finally:
        engine.dispose()


def test_full_batch_on_a_baseline_contract_is_low_risk(work_dir: Path) -> None:
    """**验收 19**：`HT-2026-0001`（低风险基线）→ 总风险 **low**。

    ⚠️ 这条以前在 `verify_m5.py` 里被映射到"worker 链路测试"—— 那条测的是**链路通不通**，
    与"基线的总风险"是两件事。映射错了不会报错，只会让报告写着"已覆盖"而实际没有。

    它同时守住第 3 轮修掉的那条规则缺陷：`PAY_PREPAY_MISSING` 若只按合同类型限定，
    我方是**采购方**（全额验收后付款）时也会命中 medium，把基线推到 medium。
    """
    aggregate, by_code = _batch_on_fixture(work_dir, "contract_01_clean.pdf", tag="baseline")

    assert aggregate.overall_risk_level is RiskLevel.LOW
    assert aggregate.counts["hit"] >= 1, "至少主体信息缺失类会命中"
    assert "PAY_PREPAY_MISSING" not in {
        code for code, item in by_code.items() if item.status is EvaluationStatus.HIT
    }, "我方是采购方时，'没有预付款约定'不是风险"


def test_full_batch_on_the_prepay_contract_is_high_risk(work_dir: Path) -> None:
    """**验收 2 的四条同时断言**（只断言总风险会被别的高风险规则撞中而通过）。

    ```text
    ① PAY_PREPAY_RATIO_HIGH_FOR_BUYER.status == hit
    ② 它的 hit_detail.actual == "0.6"          ← T3a 真的产出了字段
    ③ 它的 evidence_text 含「百分之六十」       ← T6 真的定位到了原文
    ④ overall_risk_level == high
    ```
    """
    aggregate, by_code = _batch_on_fixture(work_dir, "contract_02_prepay.pdf", tag="prepay")

    evaluation = by_code["PAY_PREPAY_RATIO_HIGH_FOR_BUYER"]
    assert evaluation.status is EvaluationStatus.HIT
    assert evaluation.hit_detail.get("actual") == "0.6"
    # ⚠️ 断言**语义**，不绑定具体写法：`60%` / `百分之六十` / `百分之六十点零`
    # 都是合法的 —— 写法取决于合同怎么印，不取决于我们的契约。
    # 三条同时成立才算过：① 预付款语境 ② 比例事实 ③ 是一个**可定位**的证据区间。
    text = evaluation.evidence_text or ""
    assert "预付款" in text, f"主证据不含预付款语境：{text!r}"
    assert any(item in text for item in ("60%", "百分之六十", "六十")), (
        f"主证据不含比例事实：{text!r}"
    )

    spans = json.loads(evaluation.evidence_json or "[]")
    assert spans, "证据不得为空"
    head = spans[0]
    assert head["text"] == text, "主证据必须是 evidence_json 里的第一条"
    # ⚠️ 位置信息嵌在 `position` 里（`{text, position:{...}}` 是 rule_hits 的既定形状）
    position = head["position"]
    assert position["char_end"] > position["char_start"], "证据区间不得为空"
    assert len(position["bbox"]) == 4, "证据必须带可画框的 bbox"
    # 摘要那一处也**没有被丢掉**：全部证据都在 `evidence_json` 里
    assert "60%" in (evaluation.evidence_json or "")
    assert aggregate.overall_risk_level is RiskLevel.HIGH


def test_a_queued_but_empty_run_is_evaluated_not_skipped(session: Session) -> None:
    """⚠️ **已入队、还没有评价**的批次必须**就地评价**，不能当成"复用"跳过。

    工具 5 在入队时就把批次建好（与工具 4 预留解析占位同理），worker 拿到的
    正是这种批次。若按"存在即复用"处理：任务成功、批次为空、结论全无 ——
    而作业状态显示 `succeeded`，调用方拿到一份**没有结论的空结果**。

    就地评价（而不是新建批次）还顺带覆盖了"worker 崩在落库之前"的重试。
    """
    _three_rules(session)
    queued = _start(session)  # 只建批次，不评价

    assert _rows_of(session, queued.run_id) == 0

    result = _run(session)

    assert result.run_id == queued.run_id, "应就地评价既有批次，而不是新建一个"
    assert result.reused is False
    assert _rows_of(session, queued.run_id) == 3


def test_aggregate_of_run_is_recomputed_from_the_persisted_hits(session: Session) -> None:
    """**决策 ⑦ 的可观测含义**：聚合是**现算**的，必须能从落库的依据重算出来。

    不落聚合结论，前提就是"它随时能算回来"。这条断言如果失败，
    说明有信息只在内存里、落库时丢了 —— 而那种缺失在正常使用中**看不出来**，
    只有"重启服务后打开历史批次"才会暴露。
    """
    from app.services.rule_service import aggregate_of_run

    _three_rules(session)
    result = _run(session)

    recomputed = aggregate_of_run(session, result.run_id)

    assert recomputed.overall_risk_level is result.aggregate.overall_risk_level
    assert recomputed.review_status is result.aggregate.review_status
    assert recomputed.counts == result.aggregate.counts
    assert [point.rule_code for point in recomputed.focus_points] == [
        point.rule_code for point in result.aggregate.focus_points
    ]


def test_get_run_returns_the_recomputed_aggregate(session: Session) -> None:
    """`GET /api/runs/{id}` 的载荷。

    这里**直接调用处理函数**（不经 HTTP）：本文件要验的是"读回来什么"，
    而路由注册由应用自身的装配保证 —— 再套一层 TestClient 只会把
    "接口返回错"与"路由没挂上"混成同一个失败。
    """
    from app.api.jobs import get_run

    _three_rules(session)
    _seed_parent_task(session)
    result = _run(session)

    payload = get_run(result.run_id, session=session, actor=_view_actor())

    assert payload["run_id"] == result.run_id
    assert payload["version_no"] == result.version_no
    assert payload["run_status"] == RunStatus.COMPLETED.value
    assert payload["aggregate"] == result.aggregate.to_json()
    # 六项版本绑定（验收 11）：`context_snapshot_json` 不在返回里，
    # 它是内部判据；对外可见的是这四项 + parse_id
    assert payload["parse_id"] == 1
    assert payload["ruleset_version"]
    assert payload["model_version"] == "mock:deterministic"


def test_get_run_returns_all_four_states_with_evidence(session: Session) -> None:
    """四条评价都要返回，**包括未命中的** —— 否则答不出"为什么没报警"。"""
    from app.api.jobs import get_run

    _three_rules(session)
    _seed_parent_task(session)
    run = _start(session)
    record_evaluations(
        session,
        run=run,
        task_id=1,
        rules=load_active_rules(session),
        evaluations=[
            _evaluation("R_A", EvaluationStatus.HIT, RiskLevel.HIGH),
            _evaluation("R_B", EvaluationStatus.NOT_HIT),
            _evaluation("R_C", EvaluationStatus.NOT_APPLICABLE),
        ],
    )

    payload = get_run(run.run_id, session=session, actor=_view_actor())
    by_code = {item["rule_code"]: item for item in payload["evaluations"]}

    assert set(by_code) == {"R_A", "R_B", "R_C"}
    assert by_code["R_A"]["evidence"], "命中的证据必须返回，否则无法核验"
    assert by_code["R_A"]["hit_detail"] == {"actual": "0.6"}
    assert by_code["R_B"]["evaluation_status"] == EvaluationStatus.NOT_HIT.value


def test_get_run_rejects_an_unknown_run_id(session: Session) -> None:
    """不存在的批次 → `RESOURCE_NOT_FOUND`（**不是** `TASK_NOT_FOUND`）。

    后者是业务结论（"还没拉取，先去拉一次"），而这里的正确处置是**核对 id**。
    """
    from app.api.jobs import get_run
    from app.errors import PermanentError

    with pytest.raises(PermanentError) as caught:
        get_run(999_999, session=session, actor=_view_actor())

    assert caught.value.code is ErrorCode.RESOURCE_NOT_FOUND


def test_a_half_finished_batch_reports_only_what_is_persisted(session: Session) -> None:
    """**半截批次**：只落了部分评价、还没收尾。

    聚合只反映已落库的那部分 —— 而 `run_status` 仍是 `running`。
    这条断言的意义是：把半截批次当成完整结论会得到一份"低风险"，
    而真相是**我们还没算完**（`run_status` 是唯一的线索）。
    """
    from app.api.jobs import get_run

    _three_rules(session)
    _seed_parent_task(session)
    run = _start(session)
    record_evaluations(
        session,
        run=run,
        task_id=1,
        rules=load_active_rules(session),
        evaluations=[_evaluation("R_A", EvaluationStatus.HIT, RiskLevel.HIGH)],
    )

    payload = get_run(run.run_id, session=session, actor=_view_actor())

    assert payload["run_status"] == RunStatus.RUNNING.value
    assert payload["aggregate"]["counts"]["hit"] == 1
    assert sum(payload["aggregate"]["counts"].values()) == 1, (
        "只落了一条评价时，四态计数之和必须是 1 —— 而不是规则总数 3"
    )
