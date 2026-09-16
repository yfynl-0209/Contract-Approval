"""规则配置的**一致性与语义校验**（同一处判据，命令行与规则管理接口共用）。

## 为什么必须抽到 `app/` 而不是留在脚本里

`scripts/check_rules.py` 在 M1.5 就有了：它校验每条规则的受控结构、缺失类规则是否
限定了适用范围、`absent=true` 与 `exclude_text` 是否同时出现……这些判据有两个使用方：

| 使用方 | 时机 |
| --- | --- |
| `scripts/check_rules.py` | 提交前 / 回归 / 部署前 |
| `POST /api/rules/reload` | **激活前**（M7） |

两处各写一遍时的分叉方式是"某天给其中一处加了一条检查"——而另一处**不会报错**，
只是安静地放行了一批本该拦下的配置。因此判据只有一份实现，两处都从它取。

⚠️ 本模块**只读不写**：它回答"这批配置能不能用"，不负责落库。
落库路径在 `app/services/rule_admin_service.py`。

## 与 `app/schemas.py` 的分工

`RuleConfig.from_row` 负责**单字段的受控结构**（未知键、非法枚举、正则能否编译）；
本模块负责**跨字段 / 跨规则**的语义：

- 缺失类规则（`absent=true`）未限定适用范围 → 必然误报；
- `absent=true` 同时配 `exclude_text` → 作者的预期与运行时不符（且是静默的）；
- `llm` 规则未配 `fallback_match_json` → 无模型时只能返回 `needs_review`，
  而项目计划 §5.4 明确要求 `llm` 规则必须给出显式降级条件；
- 11 类风险覆盖是否完整。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.rules.scoping import needs_scope
from app.schemas import RuleConfig, RuleConfigError, parse_match_config

#: 需求文档 2.4.6 要求的 11 类风险。
#:
#: 它是**规则集的属性**（"11 类都覆盖到了吗"），不是单条规则的属性 ——
#: 因此只在 `ruleset_problems` 里判，不在 `check_rule` 里判。
REQUIRED_CATEGORIES: frozenset[str] = frozenset(
    {
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
    }
)

#: `llm` 规则的必备降级条件字段名（错误消息里要用到）
LLM_FALLBACK_FIELD = "fallback_match_json"


@dataclass(frozen=True)
class RuleCheck:
    """一条规则的校验结论。

    `config` 在配置非法时为 `None` —— 与"配置合法但没有适用条件"区分开：
    前者是**错误**，后者是**正常的全局适用**。
    """

    rule_code: str
    config: RuleConfig | None
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


def check_rule(row: Mapping[str, Any]) -> RuleCheck:
    """校验**单条**规则，返回全部问题（而不是"第一个问题就返回"）。

    一次给出全部问题的理由：规则配置是人手写的，改一处再跑一次才发现下一处
    会让人反复往返 —— 而这些问题**互不依赖**，一次报全没有任何代价。

    参数 `row` 是**按列名取值**的映射（`review_rules` 的一行）。
    """
    code = str(row.get("rule_code") or "<未知>")
    problems: list[str] = []

    try:
        config = RuleConfig.from_row(row)
    except RuleConfigError as exc:
        # 结构非法时后面的语义检查全部无从谈起（连 match_mode 都读不出来）
        return RuleCheck(rule_code=code, config=None, problems=(str(exc),))

    problems.extend(_semantic_problems(row, code, config))

    return RuleCheck(rule_code=code, config=config, problems=tuple(problems))


def _semantic_problems(
    row: Mapping[str, Any], code: str, config: RuleConfig
) -> list[str]:
    """跨字段的语义问题（结构问题已由 `RuleConfig.from_row` 拦下）。"""
    problems: list[str] = []

    if config.match_mode.value == "llm" and config.fallback_condition is None:
        # ⚠️ 不是"锦上添花"：无模型时没有 fallback 的 llm 规则只能返回
        # `needs_review`，于是**整份合同的结论完整性降级**。
        # 计划 §5.4 把它列为硬要求，因此这里按**问题**处理，不是警告。
        problems.append(
            f"{code}: llm 规则缺少 {LLM_FALLBACK_FIELD} —— "
            "无模型时只能返回 needs_review，整份结论的完整性会被拉低"
        )

    if config.match_mode.value != "keyword":
        return problems

    # 只有 keyword 才需要看 absent / exclude_text（其余模式没有"缺失"语义）
    try:
        match = parse_match_config("keyword", str(row["match_text"]), rule_code=code)
    except RuleConfigError:
        # 结构非法时上面已经记录过，这里不再重复报同一条
        return problems

    absent = bool(getattr(match, "absent", False))
    if not absent:
        return problems

    # 缺失类规则最容易误报：一份标准商品采购合同没有知识产权条款是**正常**的。
    # 只有"任何合同都应当具备的条款"（违约责任 / 争议解决）才允许全局适用。
    if not row.get("applies_when_json") and needs_scope(code):
        problems.append(
            f"{code}: 缺失类规则（absent=true）未限定适用范围，会产生误报"
        )

    # 否定词表**仅对存在类规则**生效（见 `app/rules/matching.py` 的 match_keyword）。
    # 两者同时出现时，配置作者显然期待"出现且未被否定才算不缺失"，
    # 而运行时**不会**那样做 —— 而差异是静默的：规则照常给出结论，
    # 只是结论不是作者想的那个。与其猜，不如让它在加载阶段失败。
    if row.get("exclude_text"):
        problems.append(
            f"{code}: 缺失类规则（absent=true）不能同时配置 exclude_text —— "
            "否定词表仅对存在类规则生效"
        )

    return problems


def check_ruleset(rows: Sequence[Mapping[str, Any]]) -> list[RuleCheck]:
    """逐条校验，顺序与输入一致。"""
    return [check_rule(row) for row in rows]


def category_counts(rows: Sequence[Mapping[str, Any]]) -> Counter:
    """各规则类别的条数（报告用）。"""
    return Counter(str(row.get("rule_category")) for row in rows)


def missing_categories(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """需求 2.4.6 的 11 类里，**一条规则都没有**的类别（已排序）。"""
    return sorted(REQUIRED_CATEGORIES - set(category_counts(rows)))


def ruleset_problems(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """整批规则的**全部**问题：每条规则的问题 + 类别覆盖缺失。

    这是"激活前校验"的判据：空列表 = 这批配置可以用。
    """
    problems: list[str] = []
    for check in check_ruleset(rows):
        problems.extend(check.problems)
    missing = missing_categories(rows)
    if missing:
        problems.append(f"缺少规则类别：{missing}")
    return problems
