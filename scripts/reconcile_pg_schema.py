#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""加宽 PostgreSQL (dsa schema) 中过窄的 VARCHAR 列，以容纳源 SQLite 的真实数据。

背景：
    切换 / 镜像到 PostgreSQL 时，生产库 dsa schema 可能是旧模型建的，某些
    VARCHAR(n) 列比当前数据窄，导致 scripts/sync_to_postgres.py 插入时报
    `StringDataRightTruncation: value too long for type character varying(n)`。

本脚本做法（仅加宽真正溢出的列，安全可重复）：
    1. 读取源 SQLite（config.database_path）中每个 String 列的实际最大长度；
    2. 对 dsa schema 中对应列，仅当源数据实际最大长度 > 当前列宽（即会触发截断）时，
       执行 `ALTER COLUMN ... TYPE VARCHAR(数据最大长度 + BUFFER)`；
    3. 不收窄、不删数据；幂等，可反复运行；仅打印改动。

典型使用流程（在目标环境执行）：
    python scripts/sync_to_postgres.py            # 首次会建表，insert 阶段可能报截断
    python scripts/reconcile_pg_schema.py         # 按真实数据加宽过窄列
    python scripts/sync_to_postgres.py            # 再次全量刷新，此时不再截断

参数：
    --dry-run   仅报告将要加宽的列，不修改数据库。
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text, String

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 仅当数据实际超过当前列宽时才加宽；加宽时在最大长度之上预留的小缓冲，
# 避免完全相同的边界值再次触发截断。非溢出列一律不动，保持最小变更。
BUFFER = 16


def _sqlite_max_lengths(sqlite_engine) -> dict[tuple[str, str], int]:
    """返回 {(table, column): 源库该文本列实际最大长度}。"""
    from src.storage import Base

    out: dict[tuple[str, str], int] = {}
    with sqlite_engine.connect() as conn:
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if isinstance(col.type, String) and col.type.length:
                    try:
                        row = conn.execute(
                            text(f'SELECT MAX(LENGTH("{col.name}")) FROM "{table.name}"')
                        ).fetchone()
                        mx = row[0] if row else None
                    except Exception:
                        mx = None
                    out[(table.name, col.name)] = int(mx) if mx is not None else 0
    return out


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Widen narrow VARCHAR columns in dsa schema")
    parser.add_argument("--dry-run", action="store_true", help="仅报告，不修改数据库")
    args = parser.parse_args()

    from src.config import get_config, setup_env

    setup_env()
    cfg = get_config()
    schema = cfg.postgres_schema or "dsa"
    pg_url = (cfg.database_url or "").strip()
    if "user:password" in pg_url:
        print("[WARN] DATABASE_URL 仍为占位符，请先在 .env 配置真实 PostgreSQL 连接串。")
        return 2

    sqlite_engine = create_engine(f"sqlite:///{Path(cfg.database_path).resolve()}")
    pg_engine = create_engine(pg_url, pool_pre_ping=True)

    max_lens = _sqlite_max_lengths(sqlite_engine)
    from src.storage import Base

    plan: list[tuple[str, str, int, int]] = []
    with pg_engine.connect() as conn:
        for table in Base.metadata.tables.values():
            for col in table.columns:
                if not (isinstance(col.type, String) and col.type.length):
                    continue
                cur = conn.execute(
                    text(
                        "SELECT character_maximum_length FROM information_schema.columns "
                        "WHERE table_schema=:s AND table_name=:t AND column_name=:c"
                    ),
                    {"s": schema, "t": table.name, "c": col.name},
                ).fetchone()
                if not cur:
                    continue
                current = cur[0]
                if current is None:  # 无长度限制（TEXT 等），跳过
                    continue
                data_max = max_lens.get((table.name, col.name), 0)
                # 只处理真正溢出（源数据最大长度超过当前列宽）的列
                if data_max <= current:
                    continue
                target = data_max + BUFFER
                plan.append((table.name, col.name, current, target))

    if not plan:
        print("[OK] 所有 VARCHAR 列宽度已足够，无需加宽。")
        return 0

    print(f"{'TABLE':34s} {'COLUMN':30s} {'CUR':>5s} -> {'TARGET':>6s}")
    print("-" * 80)
    for t, c, cur, tgt in plan:
        print(f"{t:34s} {c:30s} {cur:5d} -> {tgt:6d}")

    if args.dry_run:
        print("\n[dry-run] 未修改数据库。")
        return 0

    with pg_engine.begin() as conn:
        for t, c, cur, tgt in plan:
            conn.execute(
                text(
                    f'ALTER TABLE {schema}."{t}" ALTER COLUMN "{c}" '
                    f"TYPE VARCHAR({tgt}) USING \"{c}\"::VARCHAR({tgt})"
                )
            )
            print(f"  widened {schema}.{t}.{c}  {cur} -> {tgt}")
    print("\n[OK] 列加宽完成，可重新运行 scripts/sync_to_postgres.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
