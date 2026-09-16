"""Alembic 迁移环境（M9）。

URL 解析优先级（见 alembic.ini 顶注）：
1. `config.attributes["url"]` —— 编程调用（迁移往返测试每条测试一个临时库）；
2. 环境变量 `M9_DATABASE_URL`；
3. `app/config.py::settings.db_url`（开发默认 SQLite —— 迁移同样能在 SQLite 上跑，
   这让"迁移与 schema.sql 是否等价"可以在本机直接验证）。

target_metadata 是 `app.db.Base.metadata`（`app.models` 全量注册）。
基线迁移是**手工核对过的** autogenerate 产物：CHECK / 复合外键 / 部分唯一索引 /
server_default 全部显式编码 —— autogenerate 只负责起草，正确性由
`tests/postgres/test_migrations.py` 的往返与结构比对测试守住。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db import Base
import app.models  # noqa: F401  —— 副作用：把全部表注册进 Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    url = config.attributes.get("url")
    if url:
        return str(url)
    url = os.environ.get("M9_DATABASE_URL")
    if url:
        return url
    from app.config import settings

    return settings.db_url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库（`alembic upgrade --sql`）。"""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：真实连接执行。**NullPool**：迁移是一次性短任务，不留池。"""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
