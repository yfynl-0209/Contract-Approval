"""M5 数据地基的守卫（T1）。

## 这个文件守什么

T1 只加了**三列**与**两个原因码**，看起来"没什么可测的"。但两件事值得写死：

1. **`review_runs` 的三项版本快照，ORM 与 `schema.sql` 必须同时有** ——
   只有一边有，要到**查询时**才炸（`no such column`），而那时离
   "改了 schema 忘了改模型"已经很远了；
2. **两个新码必须是 `ReasonCode` 而不是 `ErrorCode`** ——
   原因码是**结论**（"为什么判不了"），错误码是**故障**（"哪里坏了"）。
   混进 `ErrorCode` 会让人把"币种不可比"当成系统故障去查网络与存储，
   而它其实是一句**业务结论**。

## 一处**刻意不做**的事：三列可为空

不设 `NOT NULL`（也不给默认值）。原因是那些"最小批次"夹具：

`tests/test_schema_consistency.py` 与 `tests/test_data_integrity.py` 里有若干
只插 `(task_id, parse_id, version_no)` 的批次 —— 它们验的是**级联与复合外键**，
与版本绑定正交。若把三列设成 `NOT NULL`：

- 那些夹具要么被迫填**假版本**（噪声变大、且假数据开始出现在断言里）；
- 要么更糟 —— `test_cross_task_parse_is_rejected` 那条"复合外键拦住了跨任务拼接"
  会因为 **`NOT NULL` 先报错**而**照样通过**。断言还在，规则名还叫那个名字，
  但**它验的东西已经没有了**。

因此"六项必须绑定"这条不变量由 **M5 的写入路径 + 验收 11** 守，不靠列约束。
这是一个**权衡后的选择**，不是遗漏 —— 写在这里，避免下次有人"顺手补上 NOT NULL"。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

import app.models  # noqa: F401  导入以触发全部 ORM 模型注册
from app.config import PROJECT_ROOT
from app.enums import ErrorCode, ReasonCode
from app.models import ReviewRun

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"

#: M5 批次绑定的六项输入里，由 T1 新增的三项（另三项是 parse_id / context_snapshot_json / ruleset_version）
VERSION_COLUMNS = ("model_version", "prompt_version", "config_version")

#: T1 新增的两个原因码
NEW_REASON_CODES = (
    ReasonCode.CURRENCY_NOT_COMPARABLE,
    ReasonCode.THRESHOLD_NOT_CONFIGURED,
)


@pytest.fixture()
def schema_conn() -> Iterator[sqlite3.Connection]:
    """按 `schema.sql` 建一个内存库。

    ⚠️ `schema.sql` 里 `PRAGMA foreign_keys` 是**开着**的 —— 因此本文件的断言
    一律走 `PRAGMA table_info` 这类**结构判据**，不靠"插一条试试"：
    后者会顺带要求整条父级链（tasks → attachments → parses）都在。
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    try:
        yield conn
    finally:
        conn.close()


# ============================================================
# 1. 三项版本快照列：schema 与 ORM **两边都要有**
# ============================================================


def test_schema_has_the_three_version_columns(schema_conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in schema_conn.execute("PRAGMA table_info(review_runs)")}

    missing = [name for name in VERSION_COLUMNS if name not in columns]
    assert not missing, f"`schema.sql` 的 review_runs 缺少列：{missing}"


def test_orm_has_the_three_version_columns() -> None:
    """ORM 与 schema 必须**同时**有。

    ⚠️ 只加 `schema.sql` 而不加 `models.py`：库里有列、ORM 读不到，
    症状是"写入正常、读出来是 None"（写走 SQL、读走 ORM 时尤其隐蔽）；
    只加 `models.py` 而不加 `schema.sql`：`init_db.py` 建的库里没有这一列，
    症状是查询时 `no such column` —— 那时离真正的原因已经很远。
    """
    columns = {column.name for column in ReviewRun.__table__.columns}

    missing = [name for name in VERSION_COLUMNS if name not in columns]
    assert not missing, f"`app/models.py` 的 ReviewRun 缺少属性：{missing}"


def test_version_columns_are_nullable_by_design(schema_conn: sqlite3.Connection) -> None:
    """三列必须**可为空** —— 见模块 docstring 的权衡说明。

    ⚠️ 判据取 `PRAGMA table_info` 的 `notnull` 标志，**不是**"插一条最小记录试试"。
    后者顺带要求 `approval_tasks` → `contract_parses` 整条父级链都在
    （`schema.sql` 里 `PRAGMA foreign_keys` 是**开着**的，我初版就是栽在这里：
    报的是 `FOREIGN KEY constraint failed`，与"这三列可不可为空"**毫无关系**）。
    这正是"断言写错"的典型样子 —— 它会失败，但失败的原因不是被测的东西。

    带上 `version_no` 作**对照**：它本来就该是 `NOT NULL`。
    没有对照时，一个把所有列都读成 0 的实现也能让本断言通过。
    """
    notnull = {
        row[1]: row[3] for row in schema_conn.execute("PRAGMA table_info(review_runs)")
    }

    for name in VERSION_COLUMNS:
        assert notnull[name] == 0, (
            f"{name} 被设成了 NOT NULL —— 见本文件顶部：那会破坏最小批次夹具，"
            "并让复合外键测试因为先报 NOT NULL 而虚假通过"
        )

    # 对照：这一列本该是 NOT NULL
    assert notnull["version_no"] == 1


# ============================================================
# 2. 两个新码是**原因码**，不是**错误码**
# ============================================================


@pytest.mark.parametrize("code", NEW_REASON_CODES, ids=lambda c: c.value)
def test_new_codes_are_reason_codes(code: ReasonCode) -> None:
    """存在，且值的形状是 `SCREAMING_SNAKE`（稳定机器码，非自然语言）。"""
    assert isinstance(code, ReasonCode)
    assert code.value == code.value.upper()
    assert " " not in code.value


@pytest.mark.parametrize("code", NEW_REASON_CODES, ids=lambda c: c.value)
def test_new_reason_codes_are_not_error_codes(code: ReasonCode) -> None:
    """⚠️ **不得**混进 `ErrorCode`。

    两者回答的是不同的问题，且**处置动作相反**：

    | | 回答 | 处置 |
    | --- | --- | --- |
    | `ReasonCode` | 为什么**这条规则**判不了 | 人工看这条规则的证据 |
    | `ErrorCode` | **系统**哪里坏了 | 重试 / 查配置 / 查网络 |

    把 `CURRENCY_NOT_COMPARABLE` 放进 `ErrorCode`，它会顺着"可重试性"那条路走
    （未知码默认不可重试），于是"币种不可比"变成一次**任务级失败** ——
    而正确处置只是让某一条规则进入 `needs_review`。
    """
    error_values = {member.value for member in ErrorCode}

    assert code.value not in error_values, (
        f"{code.value} 同时出现在 ErrorCode 中 —— 原因码与错误码是两个层级，不可混用"
    )
