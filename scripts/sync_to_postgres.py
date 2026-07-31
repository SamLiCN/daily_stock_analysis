#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将本地 SQLite 中的全部 DSA 表镜像到 PostgreSQL（库 myagent / schema dsa）。

采用「幂等全量刷新」：按外键拓扑序，先清空 PostgreSQL 目标表再重新插入
当前 SQLite 行，最后重置 PostgreSQL 序列。可反复运行，不会重复累积。

SQLite 里以 TEXT 存储的日期/时间列，经由 ORM 类型转换会自动写成
PostgreSQL 的 TIMESTAMP，无需手工转换。

用法：
    python scripts/sync_to_postgres.py              # 镜像 SQLite -> PostgreSQL
    python scripts/sync_to_postgres.py --verify     # 只读：对比两边行数（需 PG 可达）
    python scripts/sync_to_postgres.py --check-refs # 仅做 SQLite 引用完整性校验

说明：在 dual 模式下，应用仍以 SQLite 为主库运行；定期执行本脚本即可让
PostgreSQL 保持为可验证的镜像。待确认稳定后，将 .env 的 DATABASE_BACKEND
改为 postgres 即完成切换。
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, text, insert, select, delete, func

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 每批插入行数，避免单次语句过大
_CHUNK = 500


def _topo_order(tables):
    """按外键依赖对表做拓扑排序，父表在前、子表在后。"""
    deps = {t.name: set() for t in tables}
    by_name = {t.name: t for t in tables}
    for t in tables:
        for fk in t.foreign_keys:
            ref = fk.column.table.name
            if ref in deps and ref != t.name:
                deps[t.name].add(ref)

    ordered: list[str] = []
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        for dep in sorted(deps[name]):
            visit(dep)
        ordered.append(name)

    for name in sorted(deps):
        visit(name)
    return [by_name[n] for n in ordered]


def _check_referential_integrity(sqlite_engine, tables):
    """返回孤儿引用列表（子表外键指向不存在的父表行）。"""
    from sqlalchemy.orm import sessionmaker

    errors: list[str] = []
    SqlSession = sessionmaker(bind=sqlite_engine)
    with SqlSession() as s:
        for t in tables:
            for fk in t.foreign_keys:
                ref_table = fk.column.table
                ref_col = fk.column
                local_cols = [c for c in t.columns if fk in c.foreign_keys]
                for local in local_cols:
                    orphan_q = (
                        select(func.count())
                        .select_from(t)
                        .where(local.isnot(None))
                        .where(local.notin_(select(ref_col).select_from(ref_table)))
                    )
                    n = s.execute(orphan_q).scalar() or 0
                    if n:
                        errors.append(
                            f"{t.name}.{local.name} 有 {n} 行引用了不存在的 "
                            f"{ref_table.name}.{ref_col.name}"
                        )
    return errors


def _reset_sequences(pg_session, schema: str, tables) -> None:
    """重置 PostgreSQL 自增主键序列，使其大于当前最大 id。"""
    from sqlalchemy import Integer, BigInteger

    for t in tables:
        pk = list(t.primary_key.columns)
        if len(pk) != 1:
            continue
        col = pk[0]
        if not isinstance(col.type, (Integer, BigInteger)):
            continue
        if col.autoincrement is False:
            continue
        seq = pg_session.execute(
            text(
                f"SELECT pg_get_serial_sequence('{schema}.{t.name}', '{col.name}')"
            )
        ).scalar()
        if not seq:
            continue
        pg_session.execute(
            text(
                f"SELECT setval('{seq}', COALESCE("
                f"(SELECT MAX({col.name}) FROM {schema}.{t.name}), 1))"
            )
        )


def run_mirror(*, verify_only: bool = False, check_refs: bool = False) -> int:
    from src.config import get_config, setup_env

    setup_env()
    config = get_config()
    sqlite_path = Path(config.database_path).resolve()
    schema = config.postgres_schema or "dsa"
    pg_url = (config.database_url or "").strip()

    if "user:password" in pg_url:
        print("[WARN] DATABASE_URL 仍为占位符，请先在 .env 配置真实 PostgreSQL 连接串。")
        return 2

    sqlite_url = f"sqlite:///{sqlite_path}"
    if not sqlite_path.exists():
        print(f"[WARN] SQLite 文件不存在: {sqlite_path}")
        return 2

    # 导入 storage 以把全部 ORM 模型加载到 Base.metadata
    from src.storage import Base

    tables = list(Base.metadata.tables.values())
    order = _topo_order(tables)

    sqlite_engine = create_engine(sqlite_url)
    pg_engine = create_engine(
        pg_url,
        pool_pre_ping=True,
        pool_size=config.pg_pool_size,
        max_overflow=config.pg_max_overflow,
        pool_recycle=config.pg_pool_recycle,
    )

    if check_refs:
        orphans = _check_referential_integrity(sqlite_engine, tables)
        if orphans:
            for msg in orphans:
                print("[REF ERROR]", msg)
            return 1
        print("[OK] 引用完整性校验通过")
        return 0

    # 读取阶段：SQLite 无 schema 概念，确保 schema=None
    for t in tables:
        t.schema = None
    from sqlalchemy.orm import sessionmaker

    SqlSession = sessionmaker(bind=sqlite_engine)
    data: dict[str, list[dict]] = {}
    src_counts: dict[str, int] = {}
    with SqlSession() as s:
        for t in tables:
            rows = s.execute(select(t)).mappings().all()
            data[t.name] = [dict(r) for r in rows]
            src_counts[t.name] = len(rows)

    # 写入阶段：统一放入目标 schema
    for t in tables:
        t.schema = schema
    with pg_engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    Base.metadata.create_all(pg_engine)

    PgSession = sessionmaker(bind=pg_engine)
    if not verify_only:
        with PgSession() as s:
            # 逆拓扑序清空（子表先于父表），避免 FK 冲突
            for t in reversed(order):
                s.execute(delete(t))
            s.commit()
            # 拓扑序写入
            for t in order:
                rows = data.get(t.name, [])
                if not rows:
                    continue
                for i in range(0, len(rows), _CHUNK):
                    s.execute(insert(t), rows[i : i + _CHUNK])
            s.commit()
            _reset_sequences(s, schema, tables)
            s.commit()
        print("镜像写入完成。")

    # 行数对比
    print("\n=== 行数对比 (SQLite -> PostgreSQL) ===")
    with PgSession() as ps:
        for t in order:
            dst = ps.execute(select(func.count()).select_from(t)).scalar() or 0
            src = src_counts.get(t.name, 0)
            flag = "OK" if src == dst else "MISMATCH"
            print(f"  {t.name:34s} {src:6d} -> {dst:6d}  [{flag}]")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Mirror DSA SQLite tables to PostgreSQL")
    parser.add_argument("--verify", action="store_true", help="只读对比两边行数")
    parser.add_argument("--check-refs", action="store_true", help="仅做 SQLite 引用完整性校验")
    args = parser.parse_args()

    if args.check_refs and not args.verify:
        return run_mirror(check_refs=True)
    return run_mirror(verify_only=args.verify)


if __name__ == "__main__":
    raise SystemExit(main())
