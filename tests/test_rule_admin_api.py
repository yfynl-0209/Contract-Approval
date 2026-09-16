"""规则管理接口（M7 / Task 5）：查询、新建、**版本化**修改与激活前校验。

## 本文件守住的是"改一条规则"这类动作特有的风险

改动一条规则影响的是**所有合同**的结论，因此三件事必须成立：

1. **改动前必须合法**：非法配置**一个字段都不许写**（`transactional_session`
   对业务异常也提交，"先写一半再报错"会留下"报错了但数据已改"的记录）。
2. **版本化**：已被真实审查引用过的版本不能就地改内容 ——
   那会静默改写"版本 N 的含义"。判据是 `rule_hits.rule_version`
   这个**评价当时的快照**，而不是"当前规则集"。
3. **留痕**：每次真实变更写不可变审计事件，且**不属于任何任务**
   （`task_id` 为空 —— 全局配置变更挂到某条任务上会把人引向错误的方向）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_actor, get_db
from app.auth import Actor, AuthenticationError, Role
from app.config import PROJECT_ROOT
from app.db import transactional_session
from app.enums import AuditAction, RuleStatus
from app.main import app
from app.models import (
    ApprovalAttachment,
    ApprovalTask,
    AuditEvent,
    ContractParse,
    ReviewRule,
    ReviewRun,
    RuleEvaluation,
)

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"

#: 需求文档 2.4.6 的 11 类风险（激活前校验要求全覆盖）。
ALL_CATEGORIES = (
    "预付款比例",
    "付款周期",
    "自动续约",
    "违约责任",
    "管辖地",
    "主体信息缺失",
    "金额缺失",
    "保密缺失",
    "数据处理",
    "知识产权",
    "验收标准缺失",
)


def _actor(name: str, *, roles: list[Role] | None = None) -> Actor:
    return Actor(
        actor_id=name,
        display_name=name,
        roles=frozenset(role.value for role in (roles or [Role.SYSTEM_ADMIN])),
        tenant_id="tenant-a",
    )


_ADMIN = _actor("admin-a")
#: 法务审核人：有审查链路上的全部业务动作，但**没有** `rule:manage`。
_REVIEWER = _actor("reviewer-1", roles=[Role.LEGAL_REVIEWER])
_AUDITOR = _actor("auditor-1", roles=[Role.READ_ONLY_AUDITOR])


class _Harness:
    def __init__(self, work_dir: Path) -> None:
        path = work_dir / "rules.db"
        conn = sqlite3.connect(path)
        try:
            conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()

        self.engine = create_engine(
            f"sqlite:///{path.as_posix()}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        self.factory = sessionmaker(
            bind=self.engine, autoflush=False, autocommit=False, future=True
        )
        self.client = TestClient(app)
        self.current = {"actor": _ADMIN}

    def install(self) -> None:
        def session_dependency():
            yield from transactional_session(self.factory())

        app.dependency_overrides[get_db] = session_dependency
        app.dependency_overrides[get_actor] = lambda: self.current["actor"]

    def uninstall(self) -> None:
        app.dependency_overrides.clear()
        self.engine.dispose()

    def act_as(self, actor: Actor) -> None:
        self.current["actor"] = actor

    def session(self) -> Session:
        return self.factory()

    # --- 请求 ---

    def get(self, url: str):
        return self.client.get(url)

    def post(self, url: str, payload: dict):
        return self.client.post(url, json=payload)

    def patch(self, url: str, payload: dict):
        return self.client.patch(url, json=payload)

    # --- 种子 ---

    def seed_rule(
        self,
        rule_code: str,
        *,
        priority: int = 100,
        rule_version: int = 1,
        rule_status: str = RuleStatus.ACTIVE.value,
        match_mode: str = "keyword",
        match_text: str = '{"keywords": ["自动续约"]}',
        rule_category: str = "自动续约",
        applies_when_json: str | None = None,
        fallback_match_json: str | None = None,
        risk_level: str = "medium",
    ) -> int:
        with self.session() as session:
            rule = ReviewRule(
                rule_code=rule_code,
                rule_name=f"{rule_code} 名称",
                rule_category=rule_category,
                risk_level=risk_level,
                rule_status=rule_status,
                priority=priority,
                rule_version=rule_version,
                match_mode=match_mode,
                match_text=match_text,
                applies_when_json=applies_when_json,
                fallback_match_json=fallback_match_json,
            )
            session.add(rule)
            session.commit()
            return rule.id

    def seed_all_categories(self) -> None:
        """每个类别一条合法规则（激活前校验要求 11 类全覆盖）。"""
        for index, category in enumerate(ALL_CATEGORIES):
            self.seed_rule(
                f"R_{index}",
                priority=10 + index,
                rule_category=category,
                # 关键词取类别名本身，保证与类别语义一致
                match_text=json.dumps({"keywords": [category]}, ensure_ascii=False),
            )

    def seed_evaluation_of(self, rule_id: int, *, rule_version: int = 1) -> None:
        """给某条规则造一条**已发生的评价**（证明该版本被引用过）。"""
        with self.session() as session:
            task = ApprovalTask(
                provider="mock",
                tenant_id="tenant-a",
                instance_id=f"HT-{rule_id}",
                approval_code=f"HT-{rule_id}",
                task_status="reviewing",
                context_status="complete",
            )
            session.add(task)
            session.flush()
            attachment = ApprovalAttachment(
                task_id=task.id,
                attachment_id=f"A-{rule_id}",
                file_name="contract.pdf",
                download_status="success",
            )
            session.add(attachment)
            session.flush()
            parse = ContractParse(
                task_id=task.id,
                attachment_id=attachment.id,
                parse_status="succeeded",
                parse_version=1,
            )
            session.add(parse)
            session.flush()
            run = ReviewRun(
                task_id=task.id, parse_id=parse.id, version_no=1, run_status="completed"
            )
            session.add(run)
            session.flush()
            session.add(
                RuleEvaluation(
                    run_id=run.id,
                    task_id=task.id,
                    rule_id=rule_id,
                    rule_version=rule_version,
                    risk_level="medium",
                    evaluation_status="hit",
                )
            )
            session.commit()

    # --- 读回 ---

    def rule_row(self, rule_code: str) -> ReviewRule:
        with self.session() as session:
            row = session.execute(
                select(ReviewRule).where(ReviewRule.rule_code == rule_code)
            ).scalar_one()
            session.expunge(row)
            return row

    def rule_count(self) -> int:
        with self.session() as session:
            return len(list(session.execute(select(ReviewRule.id)).scalars()))

    def system_audit(self) -> list[AuditEvent]:
        """系统级（不属于任何任务）的审计事件 —— 规则变更就记在这里。"""
        with self.session() as session:
            return list(
                session.execute(
                    select(AuditEvent).where(AuditEvent.task_id.is_(None))
                ).scalars()
            )


@pytest.fixture()
def harness(work_dir: Path):
    built = _Harness(work_dir)
    built.install()
    try:
        yield built
    finally:
        built.uninstall()


# ============================================================
# 1. 查询
# ============================================================


def test_rule_list_follows_the_execution_order(harness: _Harness) -> None:
    """**验收**：列表顺序 = 引擎执行顺序（`priority ASC, id ASC`）。

    ⚠️ 按 `created_at` 排序时，界面上"第 3 条先跑"与引擎里实际先跑的那条
    可以完全不同 —— 而这种偏差没有任何人会去核对。
    """
    harness.seed_rule("R_C", priority=30)
    harness.seed_rule("R_A", priority=10)
    harness.seed_rule("R_B", priority=20)

    body = harness.get("/api/rules").json()

    assert body["total"] == 3
    assert [item["rule_code"] for item in body["items"]] == ["R_A", "R_B", "R_C"]


def test_rule_detail_has_the_same_shape_as_the_list_row(harness: _Harness) -> None:
    """列表行与详情逐字同形 —— 否则界面要再发一次详情请求，而两次判据会分叉。"""
    harness.seed_rule("R_1", priority=10)

    row = harness.get("/api/rules").json()["items"][0]
    detail = harness.get("/api/rules/R_1").json()

    assert row == detail


def test_unknown_filter_value_is_rejected_instead_of_returning_nothing(
    harness: _Harness,
) -> None:
    """拼错的过滤值 → 400。

    空列表会被读成"没有这条规则"，而真相是"这个过滤值从来不存在"。
    """
    harness.seed_rule("R_1")

    assert harness.get("/api/rules?rule_status=active").json()["total"] == 1
    assert harness.get("/api/rules?rule_status=actived").status_code == 400
    assert harness.get("/api/rules?match_mode=semantic").status_code == 400


def test_missing_rule_is_a_404_with_its_own_code(harness: _Harness) -> None:
    """`RULE_NOT_FOUND` 而不是通用的 `RESOURCE_NOT_FOUND`：调用方据此分支。"""
    response = harness.get("/api/rules/NOPE")

    assert response.status_code == 404
    assert response.json()["error_code"] == "RULE_NOT_FOUND"


# ============================================================
# 2. 新建：先校验、再落库
# ============================================================


def test_create_rejects_an_invalid_config_without_writing_anything(
    harness: _Harness,
) -> None:
    """**验收**：配置非法 → 400，且库里**一行都没多**。

    ⚠️ `transactional_session` 对业务异常**也提交**，"先写一半再报错"
    会留下一条看起来完全正常的记录。因此校验必须在写入之前完成。
    """
    before = harness.rule_count()

    response = harness.post(
        "/api/rules",
        {
            "rule_code": "R_BAD",
            "rule_name": "坏规则",
            "match_mode": "keyword",
            "match_text": '{"keywords": []}',  # 空数组是非法写法
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "RULE_CONFIG_INVALID"
    assert harness.rule_count() == before


def test_create_applies_the_same_semantic_judge_as_the_cli_check(
    harness: _Harness,
) -> None:
    """缺失类规则未限定适用范围 → 拒绝。

    `absent=true` 的规则最容易误报：一份标准商品采购合同没有知识产权条款
    是**正常**的。这条判据与 `scripts/check_rules.py` 共用一份实现
    （`app/rules/validation.py`），两处各写一遍时迟早分叉。
    """
    response = harness.post(
        "/api/rules",
        {
            "rule_code": "R_MISSING",
            "rule_name": "缺失类",
            "match_mode": "keyword",
            "match_text": '{"keywords": ["保密条款"], "absent": true}',
        },
    )

    assert response.status_code == 400, response.text
    assert "未限定适用范围" in response.text


def test_create_rejects_a_duplicate_rule_code(harness: _Harness) -> None:
    """`rule_code` 是稳定标识：重复 → 400，并提示改用「修改」。"""
    harness.seed_rule("R_1")

    response = harness.post(
        "/api/rules",
        {
            "rule_code": "R_1",
            "rule_name": "重名",
            "match_mode": "keyword",
            "match_text": '{"keywords": ["x"]}',
        },
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "RULE_CONFIG_INVALID"
    assert "修改" in response.text


def test_create_writes_a_system_level_audit_event(harness: _Harness) -> None:
    """**验收**：规则变更进不可变审计账，且**不属于任何任务**。

    ⚠️ 为它随便挑一个 `task_id`，审计里就会出现一条"看起来在说某条任务"的
    规则变更记录 —— 排障的人会去查那条任务，而真正变的是全局配置。
    """
    response = harness.post(
        "/api/rules",
        {
            "rule_code": "R_NEW",
            "rule_name": "新规则",
            "match_mode": "keyword",
            "match_text": '{"keywords": ["自动续约"]}',
            "rule_category": "自动续约",
            "risk_level": "high",
            "priority": 15,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["rule_version"] == 1

    events = harness.system_audit()
    assert [event.action for event in events] == [AuditAction.RULE_CREATED.value]
    assert events[0].task_id is None
    assert events[0].target_type == "review_rule"
    assert events[0].actor_id == "admin-a"


# ============================================================
# 3. 版本化修改
# ============================================================


def test_updating_a_used_version_in_place_is_refused(harness: _Harness) -> None:
    """**验收**：已被审查引用过的版本**不能就地改写判定语义** → 409。

    判据是 `rule_hits.rule_version`（评价当时的快照）。就地改内容会让
    「版本 1 的含义」被静默改写：库里留着"版本 1 判过这个"，
    而版本 1 的内容已经不是当初那份了 —— 任何按版本回溯的审计都会指错。
    """
    rule_id = harness.seed_rule("R_1", rule_version=1, match_text='{"keywords": ["甲"]}')
    harness.seed_evaluation_of(rule_id, rule_version=1)

    response = harness.patch("/api/rules/R_1", {"match_text": '{"keywords": ["乙"]}'})

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "RULE_VERSION_IN_USE"
    assert "rule_version" in response.text, "拒绝必须告诉操作员**正确的做法**"
    # 拒绝发生在写入之前：内容与版本都没动
    row = harness.rule_row("R_1")
    assert json.loads(row.match_text)["keywords"] == ["甲"]
    assert row.rule_version == 1


def test_bumping_the_version_allows_the_change(harness: _Harness) -> None:
    """提升版本 = "新的语义是新版本"：允许，且旧引用仍指向旧语义。"""
    rule_id = harness.seed_rule("R_1", rule_version=1, match_text='{"keywords": ["甲"]}')
    harness.seed_evaluation_of(rule_id, rule_version=1)

    response = harness.patch(
        "/api/rules/R_1",
        {"match_text": '{"keywords": ["乙"]}', "rule_version": 2},
    )

    assert response.status_code == 200, response.text
    assert response.json()["rule_version"] == 2
    assert response.json()["changed"] is True
    assert json.loads(harness.rule_row("R_1").match_text)["keywords"] == ["乙"]

    actions = [event.action for event in harness.system_audit()]
    assert actions == [AuditAction.RULE_UPDATED.value]


def test_in_place_edit_is_allowed_before_the_first_use(harness: _Harness) -> None:
    """还没被任何批次引用过的版本可以就地改：否则版本号会变成噪音。

    "v1 建错、v2 改名、v3 才是真的"—— 版本号一旦成为噪音，
    就没人再拿它当审计线索了。
    """
    harness.seed_rule("R_1", rule_version=1, match_text='{"keywords": ["甲"]}')

    response = harness.patch("/api/rules/R_1", {"match_text": '{"keywords": ["乙"]}'})

    assert response.status_code == 200, response.text
    assert response.json()["rule_version"] == 1
    assert response.json()["changed"] is True


def test_version_cannot_go_backwards(harness: _Harness) -> None:
    """降版本会让历史评价指向一个语义更旧的配置 → 409。"""
    harness.seed_rule("R_1", rule_version=3)

    response = harness.patch("/api/rules/R_1", {"rule_version": 2})

    assert response.status_code == 409
    assert response.json()["error_code"] == "RULE_VERSION_IN_USE"
    assert harness.rule_row("R_1").rule_version == 3


def test_a_noop_patch_is_not_a_change(harness: _Harness) -> None:
    """没有实际变化的 PATCH 是**幂等重放**：不换版本、不追加审计事件。

    否则审计账里会多出"改了多少次"的噪音，而"本周改了几条规则"直接失真。
    """
    harness.seed_rule("R_1", match_text='{"keywords": ["甲"]}')
    before = len(harness.system_audit())

    response = harness.patch("/api/rules/R_1", {"match_text": '{"keywords": ["甲"]}'})

    assert response.status_code == 200, response.text
    assert response.json()["changed"] is False
    assert len(harness.system_audit()) == before


def test_activation_toggle_does_not_need_a_version_bump(harness: _Harness) -> None:
    """启停用改变的是"参不参与评价"，不是"怎么判" —— 不需要换版本。"""
    harness.seed_rule("R_1", rule_version=1)

    response = harness.patch("/api/rules/R_1", {"rule_status": "inactive"})

    assert response.status_code == 200, response.text
    assert response.json()["rule_status"] == RuleStatus.INACTIVE.value
    assert response.json()["rule_version"] == 1

    back = harness.patch("/api/rules/R_1", {"rule_status": "active"})
    assert back.json()["rule_status"] == RuleStatus.ACTIVE.value


def test_partial_update_does_not_clear_unmentioned_fields(harness: _Harness) -> None:
    """**验收**：只提供 `rule_name` 时，`applies_when_json` **不许**被清空。

    ⚠️ 全字段可选的请求体若按"缺省即清空"处理，一次只想改名字的调用会把
    适用范围悄悄清成 `NULL` —— 规则从"仅对软件合同适用"退化成"全局适用"，
    而响应里看不出任何异常。
    """
    harness.seed_rule(
        "R_1",
        applies_when_json='{"contract_types": ["software_service"]}',
    )

    response = harness.patch("/api/rules/R_1", {"rule_name": "改名了"})

    assert response.status_code == 200, response.text
    row = harness.rule_row("R_1")
    assert row.rule_name == "改名了"
    assert row.applies_when_json == '{"contract_types": ["software_service"]}'


def test_update_rejects_an_unknown_field(harness: _Harness) -> None:
    """未知字段被请求模型拒绝（`extra="forbid"`），而不是被静默忽略。

    静默忽略的后果：调用方以为改了 `rule_code`，而它没有 ——
    并且**没有任何提示**。
    """
    harness.seed_rule("R_1")

    response = harness.patch("/api/rules/R_1", {"rule_code": "R_2"})

    assert response.status_code == 422


def test_update_requires_a_valid_config(harness: _Harness) -> None:
    """改成一个非法配置同样被拒 —— 校验不看"是新建还是修改"。"""
    harness.seed_rule("R_1")

    response = harness.patch("/api/rules/R_1", {"match_text": "不是 JSON"})

    assert response.status_code == 400
    assert response.json()["error_code"] == "RULE_CONFIG_INVALID"
    assert harness.rule_row("R_1").match_text == '{"keywords": ["自动续约"]}'


# ============================================================
# 4. 激活前校验
# ============================================================


def test_reload_passes_for_a_complete_valid_ruleset(harness: _Harness) -> None:
    """**验收**：整批合法且 11 类齐 → 通过，并给出规则集版本（64 位摘要）。"""
    harness.seed_all_categories()

    response = harness.post("/api/rules/reload", {})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == len(ALL_CATEGORIES)
    assert body["active"] == len(ALL_CATEGORIES)
    assert body["inactive"] == 0
    assert len(body["ruleset_version"]) == 64
    assert set(body["categories"]) == set(ALL_CATEGORIES)


def test_reload_reports_every_problem_at_once(harness: _Harness) -> None:
    """**验收**：一次报**全部**问题，不是第一个。

    配置是人手写的，一次报一条会让人反复往返 —— 而这些问题互不依赖，
    一次报全没有任何代价。
    """
    # 一条 llm 规则缺降级条件（§5.4 的硬要求）
    harness.seed_rule(
        "R_LLM_NO_FALLBACK",
        match_mode="llm",
        match_text='{"instruction": "判断…"}',
        rule_category="违约责任",
    )
    # 一条缺失类规则未限定适用范围
    harness.seed_rule(
        "R_MISSING_UNSCOPED",
        match_text='{"keywords": ["保密条款"], "absent": true}',
        rule_category="保密缺失",
    )

    response = harness.post("/api/rules/reload", {})

    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "RULE_CONFIG_INVALID"
    assert "R_LLM_NO_FALLBACK" in response.text
    assert "R_MISSING_UNSCOPED" in response.text
    # 覆盖缺失也要报出来 —— 否则"校验通过"会把两类问题混为一类
    assert "缺少规则类别" in response.text


def test_reload_also_checks_inactive_rules(harness: _Harness) -> None:
    """**验收**：校验覆盖**停用**的规则。

    「先停用、再改、再启用」的流程里，停用期间配置是坏的不会被任何人发现，
    直到启用那一刻才炸 —— 而那时它已经进了一个批次。
    """
    harness.seed_all_categories()
    harness.seed_rule(
        "R_BAD_INACTIVE",
        rule_status=RuleStatus.INACTIVE.value,
        match_text='{"keywords": []}',
    )

    response = harness.post("/api/rules/reload", {})

    assert response.status_code == 400, response.text
    assert "R_BAD_INACTIVE" in response.text


# ============================================================
# 5. 权限：逐条路由证明
# ============================================================


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "/api/rules"),
        ("GET", "/api/rules/R_1"),
        ("POST", "/api/rules"),
        ("PATCH", "/api/rules/R_1"),
        ("POST", "/api/rules/reload"),
    ],
    ids=["list", "detail", "create", "update", "reload"],
)
@pytest.mark.parametrize("role", [Role.LEGAL_REVIEWER, Role.READ_ONLY_AUDITOR])
def test_rule_management_is_admin_only(
    harness: _Harness, method: str, url: str, role: Role
) -> None:
    """**验收**：规则管理**逐条路由**只对管理员开放（403）。

    规则是"系统怎么判"的输入，改一条会改变**所有**合同的结论 ——
    它与"审这份合同"不是同一个权限层级。只断言某一条路由时，
    一个把 `rule:manage` 只挂在列表上的实现照样通过。
    """
    harness.seed_rule("R_1")
    harness.act_as(_actor("someone", roles=[role]))

    payload = {
        "rule_code": "R_NEW",
        "rule_name": "新规则",
        "match_mode": "keyword",
        "match_text": '{"keywords": ["x"]}',
    }
    response = harness.client.request(method, url, json=payload)

    assert response.status_code == 403, f"{method} {url} 不应对外开放：{response.text}"
    assert response.json()["error_code"] == "PERMISSION_DENIED"


def test_rule_management_requires_authentication(harness: _Harness) -> None:
    """没有身份 → 401（而不是 403）：调用方应去拿一份身份，而不是换账号。"""

    def denied() -> Actor:
        raise AuthenticationError("缺少可信身份")

    app.dependency_overrides[get_actor] = denied
    response = harness.get("/api/rules")

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"
    assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"


def test_rule_audit_events_are_visible_through_the_audit_endpoint(
    harness: _Harness,
) -> None:
    """规则变更可通过 `GET /api/audit?include_system=true` 查到（需 `audit:read`）。"""
    harness.post(
        "/api/rules",
        {
            "rule_code": "R_NEW",
            "rule_name": "新规则",
            "match_mode": "keyword",
            "match_text": '{"keywords": ["自动续约"]}',
        },
    )

    body = harness.get("/api/audit?include_system=true").json()

    assert body["total"] == 1
    event = body["items"][0]
    assert event["action"] == AuditAction.RULE_CREATED.value
    assert event["task_id"] is None
    assert event["target_type"] == "review_rule"
