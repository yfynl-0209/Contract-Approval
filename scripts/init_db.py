#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据库初始化脚本（里程碑 M1）。

执行 `db/schema.sql` 建表，可选执行 `db/seed.sql` 灌入规则种子数据。

**为什么用标准库 sqlite3 而不是 SQLAlchemy？**
本脚本是交付物之一，需要在"只装了 Python、什么包都没装"的环境下也能跑，
因此只依赖标准库；SQLAlchemy 的职责交给 `app/models.py`（运行时用）。

用法：
    python scripts/init_db.py             # 建表 + 灌入规则种子数据
    python scripts/init_db.py --no-seed   # 只建表
    python scripts/init_db.py --reset     # 先删除已有数据库再重建
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# 项目根目录 = scripts/ 的上一级。用 __file__ 推导而非当前工作目录，
# 保证从任意位置调用本脚本都能正确定位 db/ 与 data/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"
SEED_FILE = PROJECT_ROOT / "db" / "seed.sql"
ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_DB_URL = "sqlite:///./data/app.db"


def load_env_file(path: Path) -> None:
    """加载 `.env`（仅用标准库，不引入 python-dotenv）。

    **为什么必须做这件事：**
    应用侧通过 pydantic-settings 读取 `.env`，而本脚本原本只调用 `os.getenv()`。
    一旦 `.env` 里改了 `DB_URL`，应用会连到新库，而初始化脚本仍去建默认库——
    两者指向不同的数据库，表现为"明明初始化过了，应用却说没有表"。

    解析规则与 pydantic-settings 保持一致：
      - 支持 `KEY=VALUE`、`#` 注释、空行、值两侧的引号；
      - **已存在的真实环境变量优先**，不会被 `.env` 覆盖。
    """
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")

        if key and key not in os.environ:
            os.environ[key] = value


def resolve_db_path() -> Path:
    """从环境变量 `DB_URL` 解析出 SQLite 文件路径，默认 `./data/app.db`。

    与 `app/db.py` 的 `_resolve_db_url` 保持同一套路径规则：
    相对路径一律锚定到项目根目录，避免生成多份数据库文件。
    """
    url = os.getenv("DB_URL") or DEFAULT_DB_URL

    # 本脚本只处理 SQLite；配了其他数据库时给出明确警告并回落默认值，
    # 而不是静默失败或抛异常
    if not url.startswith("sqlite"):
        print(f"[警告] 当前仅支持 sqlite，检测到 DB_URL={url}，改用默认路径")
        url = DEFAULT_DB_URL

    # 去掉 sqlite:/// 前缀（可带 +driver），只保留文件路径部分
    rel = re.sub(r"^sqlite(\+\w+)?:///", "", url)

    path = Path(rel)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def read_sql(path: Path) -> str:
    """读取 SQL 文件内容（UTF-8）。

    显式指定编码：脚本内含大量中文注释与规则文本，
    在 Windows 默认 GBK 环境下不指定会直接读乱码。
    """
    if not path.exists():
        raise FileNotFoundError(f"缺少 SQL 文件：{path}")
    return path.read_text(encoding="utf-8")


def check_foreign_keys(conn: sqlite3.Connection) -> list[str]:
    """外键定义自检：所有引用必须指向真实存在的表与列。

    手写 SQL 最容易犯的错就是表名拼错（`approval_task` vs `approval_tasks`），
    而 SQLite 建表时**不会校验**被引用的表是否存在，
    错误会一直潜伏到运行时才暴露。这里在初始化阶段就主动查一遍。
    """
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    problems: list[str] = []

    for table in sorted(tables):
        # PRAGMA foreign_key_list 返回
        # (id, seq, ref_table, from_col, to_col, on_update, on_delete, match)
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            ref_table, from_col, to_col = fk[2], fk[3], fk[4]
            if ref_table not in tables:
                problems.append(f"{table}.{from_col} → 不存在的表 {ref_table}")
                continue
            if to_col is None:
                # SQLite 在引用父表主键时 `to` 可能为 NULL，
                # 此时无法（也无需）校验具体列名
                continue
            ref_cols = {
                row[1] for row in conn.execute(f"PRAGMA table_info({ref_table})")
            }
            if to_col not in ref_cols:
                problems.append(f"{table}.{from_col} → 不存在的列 {ref_table}.{to_col}")

    return problems


# 与 app/schemas.py 的 AppliesWhenConfig 保持一致。
# 这里只做标准库级别的顶层键检查；完整的受控校验（枚举合法性、expr 字段白名单、
# 正则可编译、fallback 只能用于 llm 规则等）由 scripts/check_rules.py 用 Pydantic 完成
# ——init_db.py 必须保持"零依赖即可运行"。
_ALLOWED_APPLIES_KEYS = {
    "contract_types",
    "our_contract_labels",
    "our_business_roles",
    "requires_any_keyword",
}
_ALLOWED_MATCH_MODES = {"keyword", "regex", "llm", "expr"}


def check_rule_configs(conn: sqlite3.Connection) -> list[str]:
    """规则配置的基础结构自检（仅使用标准库）。

    检查 match_mode 取值、三个 JSON 字段是否为合法 JSON 对象、
    以及 applies_when_json 的顶层键是否在受控范围内。

    注意这只是**粗筛**：完整的受控校验见 `scripts/check_rules.py`。
    """
    problems: list[str] = []

    rows = conn.execute(
        "SELECT rule_code, match_mode, applies_when_json, match_text, "
        "fallback_match_json FROM review_rules"
    )
    for rule_code, match_mode, applies_raw, match_raw, fallback_raw in rows:
        if match_mode not in _ALLOWED_MATCH_MODES:
            problems.append(f"{rule_code}: match_mode={match_mode!r} 非法")

        for field_name, raw, required in (
            ("match_text", match_raw, True),
            ("applies_when_json", applies_raw, False),
            ("fallback_match_json", fallback_raw, False),
        ):
            if raw is None or not str(raw).strip():
                if required:
                    problems.append(f"{rule_code}: 缺少 {field_name}")
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                problems.append(f"{rule_code}: {field_name} 不是合法 JSON（{exc}）")
                continue

            if not isinstance(data, dict):
                problems.append(f"{rule_code}: {field_name} 必须是 JSON 对象")
                continue

            if field_name == "applies_when_json":
                unknown = sorted(set(data) - _ALLOWED_APPLIES_KEYS)
                if unknown:
                    problems.append(
                        f"{rule_code}: applies_when_json 含未知键 {unknown}"
                    )

    return problems


def report(conn: sqlite3.Connection) -> None:
    """打印建表结果：表清单 + 每张表的行数。

    这是 M1 的验收依据——能一眼看出 8 张表是否齐全、
    review_rules 是否成功写入 32 条规则。
    """
    # 从 sqlite_master 读取表名，排除 sqlite_ 开头的内部表
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]

    print(f"\n共 {len(tables)} 张表：")
    for name in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        # :<24 左对齐补空格、:>5 右对齐，让输出整齐便于截图
        print(f"  - {name:<24} {count:>5} 行")


def init_db(db_path: Path, with_seed: bool, reset: bool) -> None:
    """建库主流程：可选删库 → 建表 → 灌种子数据 → 打印报告。"""
    # 先确保 data/ 目录存在，否则 sqlite3.connect 会因目录不存在而报错
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # --reset：直接删掉文件重建。SQLite 的"删库"就是删文件
    if reset and db_path.exists():
        db_path.unlink()
        print(f"[reset] 已删除旧库：{db_path.name}")

    conn = sqlite3.connect(db_path)
    try:
        # executescript 可一次执行多条语句，且能处理 PRAGMA；
        # 注意它会隐式提交，所以天然不受"未 commit"影响
        conn.executescript(read_sql(SCHEMA_FILE))
        print(f"[schema] 建表完成 -> {db_path}")

        if with_seed:
            # seed.sql 开头有 DELETE FROM review_rules，因此可重复执行（幂等）。
            # 这样调规则时可以反复灌种子而不产生重复规则
            conn.executescript(read_sql(SEED_FILE))
            print("[seed]  规则种子数据写入完成")

        conn.commit()

        # 外键自检：SQLite 建表时不校验被引用对象是否存在，必须主动查
        problems = check_foreign_keys(conn)
        if problems:
            print("\n[外键自检] 发现问题：")
            for item in problems:
                print(f"  - {item}")
        else:
            print("\n[外键自检] 全部外键引用有效")

        # 规则配置基础自检（完整受控校验见 scripts/check_rules.py）
        rule_problems = check_rule_configs(conn)
        if rule_problems:
            print("\n[规则自检] 发现问题：")
            for item in rule_problems:
                print(f"  - {item}")
        else:
            print("[规则自检] 规则配置基础结构正常")

        report(conn)
    finally:
        # 无论成功失败都关闭连接，避免文件被占用导致后续 --reset 删不掉
        conn.close()

    print("\n[完成] M1 数据库就绪")


def main() -> int:
    """命令行入口。返回 0 表示成功，非 0 表示失败（便于脚本串联）。"""
    parser = argparse.ArgumentParser(description="初始化合同审批审查系统的 SQLite 数据库")
    parser.add_argument("--no-seed", action="store_true", help="只建表，不灌规则种子数据")
    parser.add_argument("--reset", action="store_true", help="删除已有数据库后重建")
    args = parser.parse_args()

    # 先读 .env：否则 DB_URL 与应用侧不一致，会建出另一个数据库
    load_env_file(ENV_FILE)

    db_path = resolve_db_path()
    print(f"配置文件：{ENV_FILE if ENV_FILE.exists() else '（未找到，使用默认值）'}")
    print(f"目标数据库：{db_path}\n")

    try:
        init_db(db_path, with_seed=not args.no_seed, reset=args.reset)
    except FileNotFoundError as exc:
        # SQL 文件缺失属于"配置不全"，给出可读的错误提示而非堆栈
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        # SQL 语法错误（例如 schema.sql 写错）统一归类为 SQL 错误
        print(f"[SQL 错误] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
