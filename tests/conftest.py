"""pytest 共享夹具。

**为什么临时目录要放在项目内：**
pytest 默认使用 `%TEMP%\\pytest-of-<user>`，在部分 Windows 环境中
会因权限限制或安全软件锁导致 `PermissionError: [WinError 5]`。
统一改用项目内的 `.pytest_tmp/`（已在 .gitignore 中忽略），测试结束即清理。
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"
SEED_FILE = PROJECT_ROOT / "db" / "seed.sql"
TMP_ROOT = PROJECT_ROOT / ".pytest_tmp"


def new_temp_dir(prefix: str) -> Path:
    """在项目内创建一个临时目录。"""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=TMP_ROOT))


def _build_db(path: Path, *, with_seed: bool) -> sqlite3.Connection:
    """按真实交付脚本建库：先 schema.sql，可选再 seed.sql。"""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    if with_seed:
        conn.executescript(SEED_FILE.read_text(encoding="utf-8"))
    # sqlite3.Row 让结果既能按下标也能按列名访问
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture()
def work_dir() -> Iterator[Path]:
    """单个测试使用的临时目录。"""
    path = new_temp_dir("test-")
    try:
        yield path
    finally:
        # ⚠️ 本仓库在 CodeBuddy IDE 内运行时，sitecustomize 的 safe-delete
        # 守卫会对"单次删除文件数 ≥500"的 rmtree 抛 `SystemExit`（要求人工确认）。
        # 测试临时目录是**我们自己刚创建**的，清理失败不应把 teardown 打成
        # ERROR（85 个假错误全是它）。残留目录留在 .pytest_tmp 下，无碍。
        try:
            shutil.rmtree(path, ignore_errors=True)
        except BaseException:  # noqa: BLE001 - 清理绝不能反过来污染测试结果
            pass


@pytest.fixture(scope="module")
def schema_conn() -> Iterator[sqlite3.Connection]:
    """只有表结构、没有数据的临时库（用于结构比对）。"""
    path = new_temp_dir("schema-")
    conn = _build_db(path / "schema.db", with_seed=False)
    try:
        yield conn
    finally:
        conn.close()
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="module")
def seeded_conn() -> Iterator[sqlite3.Connection]:
    """执行了 schema.sql + seed.sql 的临时库。

    用于校验**规则种子数据本身**是否合法——
    这样 seed.sql 写错会在 pytest 阶段暴露，而不是等到 M5 运行时。
    """
    path = new_temp_dir("seeded-")
    conn = _build_db(path / "seeded.db", with_seed=True)
    try:
        yield conn
    finally:
        conn.close()
        shutil.rmtree(path, ignore_errors=True)
