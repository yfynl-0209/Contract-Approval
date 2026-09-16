"""一次性迁移作业（compose `migrate` 服务，M9 Task 7 的"显式一次性作业"）。

流程：
1. `alembic upgrade head` —— 把库迁移到最新结构；
2. `review_rules` 为空时灌入幂等种子（`db/seed.sql` 自身以 DELETE 开头，
   但这里仍以"空表才灌"为条件 —— 运维手工加过规则的生产库**绝不**被覆盖）。

⚠️ SQLite 交付路径不受影响：本地开发仍用 `scripts/init_db.py`（schema.sql 建表）；
本脚本只面向 PostgreSQL 容器拓扑。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from app.db import engine

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(cfg, "head")
    print("[migrate] alembic upgrade head 完成")

    seed_file = PROJECT_ROOT / "db" / "seed.sql"
    with engine.begin() as conn:
        count = conn.execute(text("SELECT count(*) FROM review_rules")).scalar_one()
        if count == 0 and seed_file.exists():
            # psycopg3 单次 execute 支持多语句（分号分隔）—— seed.sql 是纯 INSERT/DELETE
            conn.exec_driver_sql(seed_file.read_text(encoding="utf-8"))
            print("[seed] 规则种子数据写入完成（空表）")
        else:
            print(f"[seed] review_rules 已有 {count} 条规则 —— 跳过种子（不覆盖生产数据）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
