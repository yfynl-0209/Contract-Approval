#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""规则库配置校验（不依赖规则引擎，可独立运行）。

**为什么需要这道检查：**

规则配置在数据库里只是 JSON 文本，数据库不校验内容。没有这道检查，
一个键名拼写错误（`contract_type` 少个 s）或字段名拼错（`prepay_ratios`）
要等到 M5 规则引擎执行到那条规则时才暴露，而那时的表现是
**"这条规则没有报风险"**——最糟糕的一类静默失败。

因此把它做成独立脚本：可以在提交前、回归时随时跑一遍。

检查内容：
  1. 每条规则的 `applies_when_json` / `match_text` / `fallback_match_json`
     是否符合受控结构（未知键、非法枚举、缺参、正则无法编译）；
  2. `expr` 规则引用的字段名是否在 `app/rules/fields.py` 白名单内；
  3. 缺失类规则（`absent=true`）是否限定了适用范围（否则必然误报）；
  4. llm 规则是否给出显式降级条件；
  5. rule_code 唯一、11 类覆盖完整；
  6. `absent=true` 与 `exclude_text` 不同时出现（否定词表仅对存在类生效）。

用法：
    python scripts/check_rules.py

退出码：0 全部通过；1 存在非法配置或覆盖不全。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

# 直接以 `python scripts/check_rules.py` 运行时，sys.path[0] 是 scripts/ 而不是项目根目录，
# 因此这里显式把项目根目录加入模块搜索路径。
# （init_db.py 不需要这一步：它只用标准库，不 import app.*）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402

from app.db import engine  # noqa: E402
from app.rules.fields import ALL_EXPR_FIELDS  # noqa: E402
from app.rules.validation import (  # noqa: E402
    REQUIRED_CATEGORIES,
    RuleCheck,
    check_ruleset,
    missing_categories,
)
from app.schemas import RuleConfig, RuleConfigError, parse_match_config  # noqa: E402

EXIT_OK = 0
EXIT_PROBLEM = 1


def fetch_rules() -> list[dict]:
    """读取全部规则，按优先级排序。"""
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM review_rules ORDER BY priority, id")
            ).mappings().all()
    except Exception as exc:  # noqa: BLE001 - 需要给出可读提示而非堆栈
        print(
            "[错误] 无法读取 review_rules 表。\n"
            "       请先执行：python scripts/init_db.py --reset\n"
            f"       原始错误：{exc}",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_PROBLEM) from exc
    return [dict(row) for row in rows]


def validate(rows: list[dict]) -> tuple[list[RuleConfig], list[str]]:
    """逐条校验，返回（合法配置列表, 问题列表）。

    ⚠️ 判据**不在本脚本里**：与 `POST /api/rules/reload` 共用
    `app/rules/validation.py`。两处各写一遍时的分叉方式是"某天给其中一处
    加了一条检查"，而另一处不报错、只是放行。
    """
    checks: list[RuleCheck] = check_ruleset(rows)
    configs = [item.config for item in checks if item.config is not None]
    problems = [problem for item in checks for problem in item.problems]
    return configs, problems


def print_report(rows: list[dict], configs: list[RuleConfig], problems: list[str]) -> None:
    total = len(rows)
    categories = Counter(row["rule_category"] for row in rows)
    modes = Counter(row["match_mode"] for row in rows)

    unrestricted = sum(1 for c in configs if c.applies_when is None)
    scoped = len(configs) - unrestricted
    sensitive = [c for c in configs if c.is_direction_sensitive]
    llm_rules = [c for c in configs if c.match_mode == "llm"]
    llm_with_fallback = [c for c in llm_rules if c.fallback_condition is not None]

    print(f"\n规则总数：{total}")

    print("\n【11 类覆盖】")
    for name in sorted(REQUIRED_CATEGORIES):
        count = categories.get(name, 0)
        # 只用 GBK 可表示的字符：Windows 控制台默认 cp936，
        # 直接输出 ✓ / ✗ 这类符号会抛 UnicodeEncodeError
        mark = "有" if count else "无"
        print(f"  [{mark}] {name:<12} {count:>2} 条")
    extra = set(categories) - REQUIRED_CATEGORIES
    if extra:
        print(f"  （另有未归类：{sorted(extra)}）")

    print("\n【匹配模式分布】")
    for mode, count in sorted(modes.items()):
        print(f"  {mode:<8} {count:>2} 条")

    print("\n【适用范围】")
    print(f"  带适用条件（applies_when）  {scoped:>2} 条")
    print(f"  全局适用（NULL）            {unrestricted:>2} 条")
    print(f"  其中方向敏感（依赖立场）    {len(sensitive):>2} 条")

    print("\n【字段生产者覆盖】")
    # ⚠️ 这道覆盖**不是**靠本脚本守的：`app/rules/fields.py` 在**导入时**就断言
    # "白名单里的每个字段都有生产者"，而本脚本 import 了它 —— 缺生产者时
    # 本脚本**根本起不来**（报错直指缺哪个字段）。这里只把结果**显示**出来。
    #
    # 为什么不在这里再查一遍"启用的 expr 规则所引用的字段有没有生产者"：
    # 那会是**死代码** —— 引用未登记字段的规则，早已被上面的
    # `RuleConfig.from_row`（白名单校验）拦下，两处判据完全相同。
    # 一个"看起来在检查、其实永远为真"的检查，比没有检查更糟。
    referenced: set[str] = set()
    for row in rows:
        if row.get("match_mode") != "expr":
            continue
        try:
            match = parse_match_config(
                "expr", str(row["match_text"]), rule_code=row["rule_code"]
            )
        except RuleConfigError:
            continue  # 非法配置已在上方记录
        field_code = getattr(match, "field", None)
        if field_code:
            referenced.add(field_code)

    print(f"  expr 可引用字段 {len(ALL_EXPR_FIELDS)} 个，全部有运行期生产者")
    print(f"  本次规则实际引用 {len(referenced)} 个：{sorted(referenced)}")

    print("\n【LLM 规则降级覆盖】")
    print(f"  llm 规则共 {len(llm_rules)} 条，其中 {len(llm_with_fallback)} 条配置了显式降级")
    for config in llm_rules:
        if config.fallback_condition is None:
            print(
                f"    [!] {config.rule_code} 缺少 fallback，无模型时只能返回 needs_review"
            )

    absent_categories = missing_categories(rows)

    print("\n【校验结果】")
    if problems:
        print(f"  [x] 发现 {len(problems)} 处问题：")
        for item in problems:
            print(f"    - {item}")
    else:
        print("  [ok] 全部规则配置合法")

    if absent_categories:
        print(f"  [x] 缺少规则类别：{absent_categories}")


def main() -> int:
    rows = fetch_rules()
    if not rows:
        print(
            "[错误] review_rules 表为空。请先执行：python scripts/init_db.py --reset",
            file=sys.stderr,
        )
        return EXIT_PROBLEM

    configs, problems = validate(rows)
    print_report(rows, configs, problems)

    if problems or missing_categories(rows):
        print("\n[结果] 未通过")
        return EXIT_PROBLEM

    print("\n[结果] 通过")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
