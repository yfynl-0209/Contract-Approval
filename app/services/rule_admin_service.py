"""规则管理：查询、新建、**版本化**修改与激活前校验（M7 / Task 5）。

## 为什么"改一条规则"不能只是一次 UPDATE

一条规则被改错，影响面是**全局的**：所有合同的结论都可能跟着变。因此三件事必须成立：

| 要求 | 落点 |
| --- | --- |
| 改动可追溯 | `audit_events`（`RULE_CREATED` / `RULE_UPDATED`，`task_id` 为空见下） |
| 改动有版本 | `review_rules.rule_version` 单调递增 |
| 改动前必须合法 | `app/rules/validation.py`（与 `check_rules.py` **同一判据**） |

### ⚠️ 规则变更的审计事件**不属于任何任务**

需求 §12 把"规则修改"与"结果确认""评论回写"并列为必须留痕的动作，但改一条规则
影响的是**所有任务**。为它随便挑一个 `task_id`，审计账里就会出现一条
"看起来在说任务 7"的规则变更记录 —— 排障的人会去查任务 7，而真正变的是全局配置。
因此 `audit_events.task_id` 可为空（M7），规则事件的 `target_type='review_rule'`。

### ⚠️ "版本已被使用"为什么能判定

`rule_hits.rule_version` 是**评价当时的规则版本快照**。存在 `(rule_id, rule_version)`
相同的评价行，就说明这个版本**已经被一次真实审查引用过**。

此时就地改内容会让"版本 N 的含义"被静默改写：库里留着"版本 1 判过这个"，
而版本 1 的内容已经不是当初那份了 —— 任何按版本回溯的审计都会指错。
正确做法是**提升 `rule_version`**：新的语义是新版本，旧的引用仍指向旧语义。

（批次快照 `review_runs.ruleset_snapshot_json` 是第二道防线：即便规则行被改，
已冻结的批次仍按当时的配置执行。两者都不能省 —— 快照保护"正在跑的批次"，
版本保护"已经跑过的结论"。）

### 未使用过的版本允许就地修改

一条刚建好、还没被任何批次引用过的规则，改个错别字不必换版本。
强制换版本会让版本号变成噪音（"v1 建错、v2 改名、v3 才是真的"），
而版本号一旦成为噪音，就没人再拿它当审计线索了。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import Actor
from app.context import get_correlation_id
from app.enums import AuditAction, ErrorCode, LogType, MatchMode, RuleStatus
from app.errors import PermanentError
from app.models import AuditEvent, ReviewRule, RuleEvaluation
from app.rules.validation import category_counts, check_rule, ruleset_problems
from app.services import query_service
from app.services.log_service import LogService

__all__ = [
    "CONTENT_FIELDS",
    "EDITABLE_FIELDS",
    "RulesetReport",
    "activate_validation",
    "create_rule",
    "get_rule",
    "list_rules",
    "rule_view",
    "update_rule",
]

#: 可通过管理接口写入的列。**白名单而不是黑名单**：
#: 黑名单在新增列时会默认允许写入，而新增列往往是内部字段（如 `id`）。
EDITABLE_FIELDS: tuple[str, ...] = (
    "rule_name",
    "rule_category",
    "risk_level",
    "rule_status",
    "priority",
    "rule_version",
    "match_mode",
    "match_text",
    "applies_when_json",
    "fallback_match_json",
    "exclude_text",
    "suggestion_text",
)

#: 会改变"这条规则怎么判"的字段 —— **改动它们必须换版本**（见模块 docstring）。
#:
#: ⚠️ `rule_status`（启停用）不在其中：它改变的是"这条规则参不参与本次评价"，
#: 不是"它怎么判"。而且它进批次快照与 `ruleset_version`，
#: 启停用本身就会让后续批次换一个规则集版本 —— 不需要再动规则版本。
#:
#: ⚠️ `priority` **在**其中：它决定评价顺序，而顺序会影响
#: "同一条规则先看哪一处证据"这类结论。
CONTENT_FIELDS: tuple[str, ...] = tuple(
    field for field in EDITABLE_FIELDS if field not in {"rule_status", "rule_version"}
)

#: 构造一条"待校验规则行"所需的全部列（`RuleConfig.from_row` 与语义校验都要用）。
#:
#: ⚠️ 必须包含 `applies_when_json` / `fallback_match_json` 的**键**（值可为 `None`）：
#: `RuleConfig.from_row` 按 `row["..."]` 取值，缺键会抛 `KeyError`
#: —— 那是一个代码缺陷，不是配置问题。
_RULE_COLUMNS: tuple[str, ...] = (
    "rule_code",
    "rule_name",
    "rule_category",
    "risk_level",
    "rule_status",
    "priority",
    "rule_version",
    "match_mode",
    "match_text",
    "applies_when_json",
    "fallback_match_json",
    "exclude_text",
    "suggestion_text",
)


# ============================================================
# 查询
# ============================================================


def rule_view(rule: ReviewRule) -> dict[str, Any]:
    """一条规则的对外形状。列表（`GET /api/rules`）与详情**共用**。

    分成两套时，分叉方式是"某天给详情加了一个字段、忘了列表" ——
    而使用者看到的是同一条规则在两张页面上字段不一样。
    """
    return {"rule_id": rule.id} | {
        name: getattr(rule, name) for name in _RULE_COLUMNS
    }


def list_rules(
    session: Session,
    *,
    page: int = 1,
    page_size: int = query_service.DEFAULT_PAGE_SIZE,
    rule_status: str | None = None,
    rule_category: str | None = None,
    match_mode: str | None = None,
) -> query_service.Page:
    """规则列表。

    排序：`priority ASC, id ASC` —— 与 `load_active_rules` 的**执行顺序一致**。
    按 `created_at` 排会让界面上的顺序与引擎里的执行顺序不同，
    而"界面说第 3 条先跑、引擎实际先跑第 7 条"是一种没人会去核对的偏差。
    """
    stmt = select(ReviewRule)
    # 白名单校验与任务/作业列表同一份判据（`query_service.check_member`）：
    # 各处自己写的 `in` 判断会让同一个拼写错误在不同接口上有不同表现。
    rule_status = query_service.check_member(
        rule_status, RULE_STATUSES, parameter="rule_status"
    )
    match_mode = query_service.check_member(
        match_mode, MATCH_MODES, parameter="match_mode"
    )
    if rule_status is not None:
        stmt = stmt.where(ReviewRule.rule_status == rule_status)
    if rule_category is not None:
        stmt = stmt.where(ReviewRule.rule_category == rule_category)
    if match_mode is not None:
        stmt = stmt.where(ReviewRule.match_mode == match_mode)
    stmt = stmt.order_by(ReviewRule.priority.asc(), ReviewRule.id.asc())
    return query_service.paginate(session, stmt, page=page, page_size=page_size)


def get_rule(session: Session, *, rule_code: str) -> ReviewRule:
    """按 `rule_code` 取规则，不存在时 404。

    ⚠️ 用 `RULE_NOT_FOUND` 而不是 `RESOURCE_NOT_FOUND`：前者是**这个资源**的
    稳定机器码，调用方据此能分支到"核对 rule_code"；后者是通用引用错误，
    会让"规则不存在"与"任务不存在"在接口上长得一样。
    """
    rule = session.execute(
        select(ReviewRule).where(ReviewRule.rule_code == rule_code)
    ).scalar_one_or_none()
    if rule is None:
        raise PermanentError(
            f"规则 {rule_code!r} 不存在", code=ErrorCode.RULE_NOT_FOUND
        )
    return rule


# ============================================================
# 新建
# ============================================================


def create_rule(
    session: Session, *, payload: Mapping[str, Any], actor: Actor
) -> ReviewRule:
    """新建一条规则。**先校验配置、再落库**。

    Raises:
        PermanentError: 配置非法（`RULE_CONFIG_INVALID`，400）或 `rule_code` 重复。
    """
    rule_code = str(payload["rule_code"])
    existing = session.execute(
        select(ReviewRule.id).where(ReviewRule.rule_code == rule_code)
    ).scalar_one_or_none()
    if existing is not None:
        # ⚠️ 先查一次是为了给出**能看懂**的错误。数据库的 UNIQUE 仍是最后防线
        # （并发下两次插入都能通过上面的查询），因此下面的 IntegrityError
        # 也要翻译成同一个结论，而不是 500。
        raise PermanentError(
            f"规则 {rule_code!r} 已存在：`rule_code` 是稳定标识，"
            "它进历史评价与批次快照，因此不提供改名入口；请改用「修改」",
            code=ErrorCode.RULE_CONFIG_INVALID,
        )

    row = {name: payload.get(name) for name in _RULE_COLUMNS}
    row["rule_code"] = rule_code
    _assert_config_ok(row)

    rule = ReviewRule(
        rule_code=rule_code,
        **{name: row[name] for name in EDITABLE_FIELDS},
    )

    try:
        # ⚠️ 用 SAVEPOINT 隔开这次插入：唯一约束冲突时只回滚这一步，
        # 不会把调用方在同一事务里的其他改动一起丢掉
        # （与 `workflow/jobs.py::create_job` 同一手法）。
        with session.begin_nested():
            session.add(rule)
            session.flush()
    except IntegrityError as exc:
        raise PermanentError(
            f"规则 {rule_code!r} 已存在（并发插入被唯一约束拒绝）；"
            "`rule_code` 是稳定标识，不提供改名入口，请改用「修改」",
            code=ErrorCode.RULE_CONFIG_INVALID,
        ) from exc

    _audit(
        session,
        action=AuditAction.RULE_CREATED,
        rule=rule,
        actor=actor,
        detail={
            "rule_code": rule.rule_code,
            "rule_version": rule.rule_version,
            "match_mode": rule.match_mode,
            "rule_status": rule.rule_status,
            "risk_level": rule.risk_level,
        },
    )
    _log(session, actor=actor, message=f"新建规则 {rule.rule_code}（v{rule.rule_version}）")
    return rule


# ============================================================
# 修改（版本化）
# ============================================================


def update_rule(
    session: Session,
    *,
    rule_code: str,
    changes: Mapping[str, Any],
    actor: Actor,
) -> tuple[ReviewRule, bool]:
    """按 `changes` 修改规则；返回 `(规则, 是否真的发生了变化)`。

    `changes` 只含**调用方显式提供**的字段（见 `UpdateRuleRequest` 的说明）。

    Raises:
        PermanentError: 规则不存在（404）；配置非法（400）；
            就地改写一个**已被审查引用过**的版本（`RULE_VERSION_IN_USE`，409）。
    """
    rule = get_rule(session, rule_code=rule_code)

    unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
    if unknown:
        raise PermanentError(
            f"不可修改的字段：{unknown}（可改：{list(EDITABLE_FIELDS)}）",
            code=ErrorCode.RULE_CONFIG_INVALID,
        )

    applied = {
        name: value for name, value in changes.items() if getattr(rule, name) != value
    }
    if not applied:
        # 一次没有实际变化的 PATCH 是**幂等重放**，不是一次变更：
        # 不换版本、不追加审计事件（否则审计账里会多出"改了多少次"的噪音）。
        return rule, False

    content_changed = any(name in CONTENT_FIELDS for name in applied)
    requested_version = int(applied.get("rule_version", rule.rule_version))
    _assert_version_transition(
        session, rule=rule, requested_version=requested_version,
        content_changed=content_changed,
    )

    merged = _row_of(rule) | applied
    merged["rule_version"] = requested_version
    _assert_config_ok(merged)

    for name, value in applied.items():
        setattr(rule, name, value)

    _audit(
        session,
        action=AuditAction.RULE_UPDATED,
        rule=rule,
        actor=actor,
        detail={
            "rule_code": rule.rule_code,
            "changed_fields": sorted(applied),
            "content_changed": content_changed,
            "rule_version": rule.rule_version,
        },
    )
    _log(
        session,
        actor=actor,
        message=(
            f"修改规则 {rule.rule_code}（v{rule.rule_version}）："
            f"{'、'.join(sorted(applied))}"
        ),
    )
    session.flush()
    return rule, True


def _assert_version_transition(
    session: Session,
    *,
    rule: ReviewRule,
    requested_version: int,
    content_changed: bool,
) -> None:
    """版本迁移的合法性（这是"版本化修改"的核心判据）。

    | 情形 | 结论 |
    | --- | --- |
    | 版本倒退（`<` 当前） | 拒绝 —— 历史评价已按旧版本留痕 |
    | 改了内容、版本不变、**该版本已被引用** | 拒绝（提示提升版本） |
    | 改了内容、版本不变、该版本未被引用 | 允许（首次使用前修正） |
    | 改了内容、版本提升 | 允许 |
    """
    if requested_version < rule.rule_version:
        raise PermanentError(
            f"规则 {rule.rule_code} 的版本不能倒退："
            f"当前 v{rule.rule_version}，请求 v{requested_version} —— "
            "历史评价已经按旧版本留痕，降版本会让它们指向一个语义更旧的配置",
            code=ErrorCode.RULE_VERSION_IN_USE,
        )

    if not content_changed or requested_version > rule.rule_version:
        return

    if _version_in_use(session, rule=rule):
        raise PermanentError(
            f"规则 {rule.rule_code} 的 v{rule.rule_version} 已被审查批次引用过，"
            "不能就地改写它的判定语义。请提升 rule_version"
            "（例如 rule_version="
            f"{rule.rule_version + 1}）—— 新的语义应是新版本，"
            "旧的引用仍指向旧语义",
            code=ErrorCode.RULE_VERSION_IN_USE,
        )


def _version_in_use(session: Session, *, rule: ReviewRule) -> bool:
    """该 (rule_id, rule_version) 是否已被至少一条规则评价引用。

    ⚠️ 判据是 `rule_hits`，**不是**"当前规则集"。`rule_hits.rule_version`
    是评价当时写入的版本快照，所以它回答的正是"这个版本被真正用过吗"。
    """
    found = session.execute(
        select(RuleEvaluation.id)
        .where(
            RuleEvaluation.rule_id == rule.id,
            RuleEvaluation.rule_version == rule.rule_version,
        )
        .limit(1)
    ).scalar_one_or_none()
    return found is not None


# ============================================================
# 激活前校验（"reload"）
# ============================================================


@dataclass(frozen=True, slots=True)
class RulesetReport:
    """整批规则的体检结论。"""

    total: int
    active: int
    inactive: int
    categories: dict[str, int]
    ruleset_version: str

    def as_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "active": self.active,
            "inactive": self.inactive,
            "categories": self.categories,
            "ruleset_version": self.ruleset_version,
        }


def activate_validation(session: Session, *, actor: Actor) -> RulesetReport:
    """**激活前校验**：把整批规则过一遍同一套判据，通过才给出规则集版本。

    ## 为什么叫 reload 而这里只做校验

    规则**没有缓存**：每次评价都从 `review_rules` 现读（`load_active_rules`）。
    因此"重新加载"这个动作在实现上不存在 —— 真正需要的是一个**闸门**：
    规则改完之后，在它被下一个批次用上之前，必须有一次"整批都合法吗"的检查。

    这个闸门不通过就返回 400 与**全部**问题（而不是第一个）：配置是人手写的，
    一次报一条会让人反复往返。

    Raises:
        PermanentError: 存在非法配置（`RULE_CONFIG_INVALID`，400）。原始问题列表
            在消息里逐条列出。
    """
    rows = _all_rule_rows(session)

    problems = ruleset_problems(rows)
    if problems:
        raise PermanentError(
            f"规则集未通过激活前校验，共 {len(problems)} 处问题：\n- "
            + "\n- ".join(problems),
            code=ErrorCode.RULE_CONFIG_INVALID,
        )

    active = [row for row in rows if row.get("rule_status") == "active"]
    ruleset_version = _ruleset_version(active)

    report = RulesetReport(
        total=len(rows),
        active=len(active),
        inactive=len(rows) - len(active),
        categories=dict(sorted(category_counts(rows).items())),
        ruleset_version=ruleset_version,
    )

    _log(
        session,
        actor=actor,
        message=(
            f"规则集激活前校验通过：{report.total} 条"
            f"（启用 {report.active} / 停用 {report.inactive}），"
            f"规则集版本 {ruleset_version[:12]}…"
        ),
    )
    session.flush()
    return report


def _ruleset_version(active_rows: Sequence[Mapping[str, Any]]) -> str:
    """规则集版本 = 快照摘要，**与批次用的那个由同一个函数算出**。

    ⚠️ 直接在管理接口里另算一份（比如另拼一个 JSON）会让两个"规则集版本"
    各自漂移：界面显示"当前 vX"，而新批次记的是 vY —— 而两者都自称是
    "当前规则集的版本"。
    """
    from app.schemas import RuleConfig
    from app.services.rule_service import ActiveRule, ruleset_version_of

    rules = tuple(
        ActiveRule(
            rule_id=int(row["id"]),
            config=RuleConfig.from_row(row),
            raw=dict(row),
        )
        for row in active_rows
    )
    return ruleset_version_of(rules)


# ============================================================
# 内部工具
# ============================================================


def _row_of(rule: ReviewRule) -> dict[str, Any]:
    """ORM 行 → 按列名取值的映射（与 `rule_service._row_mapping` 同义）。"""
    return {name: getattr(rule, name) for name in _RULE_COLUMNS}


def _all_rule_rows(session: Session) -> list[dict[str, Any]]:
    """读全部规则（含停用）并按列名转成映射。

    ⚠️ 校验要覆盖**停用**规则：一次"先停用、再改、再启用"的运维流程里，
    停用期间配置是坏的不会被任何人发现，直到启用那一刻才炸 ——
    而那时它已经进了一个批次。
    """
    rows = (
        session.execute(select(ReviewRule).order_by(ReviewRule.priority, ReviewRule.id))
        .scalars()
        .all()
    )
    return [{name: getattr(row, name) for name in _RULE_COLUMNS} | {"id": row.id} for row in rows]


def _assert_config_ok(row: Mapping[str, Any]) -> None:
    """用**与命令行同一套判据**校验一条待写入的规则配置。

    Raises:
        PermanentError: 配置非法（`RULE_CONFIG_INVALID`，400），消息含全部问题。
    """
    check = check_rule(row)
    if check.problems:
        raise PermanentError(
            f"规则 {check.rule_code} 配置非法：\n- " + "\n- ".join(check.problems),
            code=ErrorCode.RULE_CONFIG_INVALID,
        )


def _audit(
    session: Session,
    *,
    action: AuditAction,
    rule: ReviewRule,
    actor: Actor,
    detail: Mapping[str, Any],
) -> None:
    """写不可变审计事件。

    ⚠️ `task_id` 显式留空：规则变更影响**所有任务**，它不属于任何一条任务。
    随便挑一个 task_id 会让审计账里出现一条"看起来在说某条任务"的记录。
    """
    session.add(
        AuditEvent(
            task_id=None,
            actor_id=actor.actor_id,
            actor_name=actor.display_name,
            action=action.value,
            target_type="review_rule",
            target_id=rule.id,
            correlation_id=get_correlation_id(),
            detail_json=json.dumps(dict(detail), ensure_ascii=False),
        )
    )


def _log(session: Session, *, actor: Actor, message: str) -> None:
    """系统级日志（`task_id=NULL`：规则变更不属于任何单条任务）。"""
    LogService(session, operator=actor.display_name).log(
        log_type=LogType.SYSTEM, task_id=None, message=message
    )


#: 过滤参数的白名单（取自枚举的唯一定义处）。
RULE_STATUSES: frozenset[str] = frozenset(item.value for item in RuleStatus)
MATCH_MODES: frozenset[str] = frozenset(item.value for item in MatchMode)
