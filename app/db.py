"""数据库连接与会话管理。

三个容易踩的坑在这里统一处理：

1. **SQLite 相对路径**：`sqlite:///./data/app.db` 若相对"当前工作目录"解析，
   从项目根启动与从 `scripts/` 启动会各生成一个 `app.db`（表现为数据凭空消失）。
   因此统一锚定到项目根目录。
2. **FastAPI 跨线程**：SQLite 默认禁止跨线程使用，而同步请求跑在线程池里，
   必须关掉该检查，否则报 `SQLite objects created in a thread`。
3. **外键默认不生效**：`PRAGMA foreign_keys` 是**连接级**设置，不是数据库级。
   写在 `schema.sql` 里只对执行脚本的那个连接有效；应用从连接池取出的新连接
   默认 OFF，`ON DELETE CASCADE` 根本不会触发。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import PROJECT_ROOT, settings
from app.errors import AppError


def _resolve_db_url(url: str) -> str:
    """把 sqlite 相对路径锚定到项目根目录；非 sqlite 的 URL 原样返回。"""
    # sqlite（可带 +driver）:/// 后面是文件路径
    match = re.match(r"^sqlite(\+\w+)?:///(.+)$", url)
    if not match:
        return url

    driver = match.group(1) or ""
    path = Path(match.group(2))
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    # as_posix()：反斜杠会被当成转义字符导致 URL 解析出错
    return f"sqlite{driver}:///{path.resolve().as_posix()}"


# 模块导入时解析一次，后续统一使用 DB_URL
DB_URL = _resolve_db_url(settings.db_url)
_IS_SQLITE = DB_URL.startswith("sqlite")

# SQLite 需要关闭同线程检查；换成 PostgreSQL 时不适用，故按 DB_URL 动态决定
_connect_args = {"check_same_thread": False} if _IS_SQLITE else {}

engine = create_engine(DB_URL, connect_args=_connect_args, future=True)


if _IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record) -> None:
        """为每个新建连接开启外键（`PRAGMA` 是连接级设置，见模块说明第 3 点）。"""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# autoflush=False：避免查询前"偷偷"把待提交数据写进库
# autocommit=False：SQLAlchemy 2.0 已移除隐式提交，事务边界必须显式控制
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类；`Base.metadata` 用于与 `db/schema.sql` 做结构一致性校验。"""


def transactional_session(session: Session) -> Iterator[Session]:
    """请求级事务边界：成功与**业务失败**都提交，只有未预期异常回滚。

    提交不放在服务层：服务不知道调用方的边界，一个方法可能被组合进更大的事务。

    业务失败**也不能回滚** —— 作业重试状态、任务阻塞、失败日志都是
    "这次尝试发生过"的记录，回滚会让失败变得无迹可查。

    抽成独立函数是为了让 `get_db`、M4 的 Worker 与测试**共用同一份实现**：
    测试里另写一份，语义一旦分叉，测出来的行为与生产不一致，等于没测。
    """
    try:
        yield session
    except AppError:
        session.commit()
        raise
    except Exception:
        session.rollback()
        raise
    else:
        session.commit()
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：按请求提供一个数据库会话（事务语义见 `transactional_session`）。"""
    yield from transactional_session(SessionLocal())
