#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把生产 PostgreSQL 库（myagent / schema dsa）以覆盖方式同步到测试库（myagent_demo / schema dsa）。

自包含实现：仅依赖 `psycopg`（psycopg3）+ 标准库，不 import 仓库 `src`。
表清单 / 外键拓扑 / 自增序列全部在运行时从 pg_catalog / information_schema 动态读取，
因此生产或测试 schema 演进后无需改脚本。

行为：
  - 默认（不带参数）= dry-run：只读对比生产与测试每张表的行数，打印将覆盖的范围，不写任何数据。
  - `--apply`           = 真正覆盖（默认先对测试库做 COPY 备份，再单事务 TRUNCATE + COPY + 序列重置）。
  - `--no-backup`       = 配合 --apply 使用，跳过覆盖前备份（不推荐）。
  - `--restore <目录>`   = 用某次备份目录把测试库回滚到该快照。
  - `--list-backups`    = 列出本地备份目录。

安全护栏（写库脚本，防误操作）：
  - 源库连接强制只读会话（default_transaction_read_only=on），任何对源库的写操作都会失败。
  - 目标库名必须等于 SYNC_TARGET_DB（默认 myagent_demo），否则拒绝；源库名 != 目标库名，否则拒绝。
  - 覆盖在单个事务内完成（TRUNCATE 全部表 -> COPY 重灌 -> 重置序列），失败自动回滚，读端看不到半成品。

配置读取优先级：环境变量 > 仓库根 .env > 内置默认值。
相关配置（均可用环境变量覆盖，键名一致）：
  DATABASE_URL             测试库（目标）连接串，复用仓库既有配置，形如 postgresql+psycopg://...
  SYNC_TARGET_DATABASE_URL 可选：显式指定测试库（目标）完整连接串；在 NAS 上若 .env 的 DATABASE_URL
                           指向生产库，务必设置本项为测试库连接串（否则护栏会按源==目标拒绝执行）
  SYNC_SOURCE_DB           生产库名（默认 myagent），源连接串由目标连接串换库名推导
  SYNC_SOURCE_DATABASE_URL 可选：显式指定完整生产连接串（不配置则按 SYNC_SOURCE_DB 推导）
  SYNC_TARGET_DB           目标库名白名单（默认 myagent_demo），用于防误覆盖护栏
  POSTGRES_SCHEMA          schema 名（默认 dsa）
  SYNC_BACKUP_DIR          备份根目录（默认 <仓库>/data/sync_backups）
  SYNC_BACKUP_KEEP         保留最近 N 份备份（默认 7）

用法示例：
  python scripts/sync_prod_to_demo.py                # dry-run 对比
  python scripts/sync_prod_to_demo.py --apply        # 覆盖（含备份）
  python scripts/sync_prod_to_demo.py --restore data/sync_backups/20260814T210000
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import logging.handlers
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

try:
    import psycopg
    from psycopg import sql
except ImportError:  # pragma: no cover - 环境缺失提示
    print(
        "[ERROR] 缺少 psycopg（psycopg3）。请先安装：pip install 'psycopg[binary]>=3.1'",
        file=sys.stderr,
    )
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

DEFAULTS = {
    "SYNC_SOURCE_DB": "myagent",
    "SYNC_TARGET_DB": "myagent_demo",
    "POSTGRES_SCHEMA": "dsa",
    "SYNC_BACKUP_KEEP": "7",
    # SYNC_BACKUP_DIR 动态推导：<仓库>/data/sync_backups
}

_log = logging.getLogger("sync_prod_to_demo")


# --------------------------------------------------------------------------- #
# 配置加载
# --------------------------------------------------------------------------- #
def _load_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析（避免引入 python-dotenv 依赖）。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def get_config() -> dict[str, str]:
    import os

    cfg = dict(DEFAULTS)
    env_file = _load_env_file(ENV_FILE)
    for key in list(DEFAULTS) + [
        "DATABASE_URL",
        "SYNC_SOURCE_DATABASE_URL",
        "SYNC_TARGET_DATABASE_URL",
        "SYNC_BACKUP_DIR",
    ]:
        os_val = os.environ.get(key)
        if os_val:
            cfg[key] = os_val
        elif key in env_file:
            cfg[key] = env_file[key]
    if "SYNC_BACKUP_DIR" not in cfg or not cfg["SYNC_BACKUP_DIR"]:
        cfg["SYNC_BACKUP_DIR"] = str(ROOT / "data" / "sync_backups")
    return cfg


# --------------------------------------------------------------------------- #
# 连接串处理
# --------------------------------------------------------------------------- #
_ALIASES = (
    "postgresql+psycopg://",
    "postgresql+psycopg2://",
)


def _normalize_url(url: str) -> str:
    url = url.strip()
    for alias in _ALIASES:
        if url.startswith(alias):
            url = "postgresql://" + url[len(alias):]
            break
    return url


def _dbname_of(url: str) -> str:
    path = urlsplit(_normalize_url(url)).path or "/"
    return unquote(path.lstrip("/"))


def _swap_dbname(url: str, new_db: str) -> str:
    parts = urlsplit(_normalize_url(url))
    return urlunsplit(parts._replace(path="/" + new_db))


# --------------------------------------------------------------------------- #
# PostgreSQL 对象发现（schema 演进时自动适配）
# --------------------------------------------------------------------------- #
def iter_tables(conn, schema: str):
    """返回 schema 下所有普通表名（按名称排序）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind = 'r'
            ORDER BY c.relname
            """,
            (schema,),
        )
        return [row[0] for row in cur.fetchall()]


def topo_order(conn, schema: str, tables: list[str]) -> list[str]:
    """按外键依赖返回「父表在前、子表在后」的拓扑序；有环时降级为仅按名称并告警。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tc.table_name, ccu.table_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
             AND tc.constraint_schema = ccu.constraint_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = %s
              AND ccu.table_schema = %s
            """,
            (schema, schema),
        )
        edges = {r for r in cur.fetchall() if r[0] != r[1]}

    deps: dict[str, set[str]] = {t: set() for t in tables}
    for child, parent in edges:
        if child in deps and parent in deps:
            deps[child].add(parent)

    ordered: list[str] = []
    state: dict[str, int] = {}  # 0=未访问 1=访问中 2=完成

    def visit(t: str) -> None:
        if state.get(t) == 2:
            return
        if state.get(t) == 1:
            return  # 环：跳过，避免死循环
        state[t] = 1
        for dep in sorted(deps[t]):
            visit(dep)
        state[t] = 2
        ordered.append(t)

    for t in sorted(tables):
        visit(t)

    if len(ordered) != len(tables):
        missing = [t for t in tables if t not in ordered]
        _log.warning("外键依赖存在环，以下表降级为按名称追加：%s", missing)
        ordered.extend(sorted(missing))
    return ordered


def iter_sequences(conn, schema: str):
    """返回 schema 下被表列拥有的自增序列 [(表名, 列名, 序列名), ...]。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.relname, a.attname, seq.relname
            FROM pg_class seq
            JOIN pg_depend d ON d.objid = seq.oid AND d.deptype = 'a'
            JOIN pg_class t ON t.oid = d.refobjid
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
            WHERE seq.relkind = 'S'
              AND t.relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = %s)
            ORDER BY seq.relname
            """,
            (schema,),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# 核心搬运
# --------------------------------------------------------------------------- #
def _copy_out(conn, schema: str, table: str) -> bytes:
    stmt = sql.SQL("COPY (SELECT * FROM {tbl}) TO STDOUT").format(
        tbl=sql.Identifier(schema, table)
    )
    with conn.cursor() as cur:
        with cur.copy(stmt) as c:
            return b"".join(c)


def _copy_in(conn, schema: str, table: str, data: bytes) -> None:
    stmt = sql.SQL("COPY {tbl} FROM STDIN").format(
        tbl=sql.Identifier(schema, table)
    )
    with conn.cursor() as cur:
        with cur.copy(stmt) as c:
            c.write(data)


def _truncate_all(conn, schema: str, tables: list[str]) -> None:
    if not tables:
        return
    targets = sql.SQL(", ").join(sql.Identifier(schema, t) for t in tables)
    stmt = sql.SQL("TRUNCATE {targets} CASCADE").format(targets=targets)
    with conn.cursor() as cur:
        cur.execute(stmt)


def _reset_sequences(conn, schema: str, sequences) -> None:
    for table, column, _seq in sequences:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT setval(pg_get_serial_sequence(%s, %s), "
                    "COALESCE((SELECT MAX({col}) FROM {tbl}), 1))"
                ).format(
                    col=sql.Identifier(column),
                    tbl=sql.Identifier(schema, table),
                ),
                (f"{schema}.{table}", column),
            )


def _count(conn, schema: str, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT count(*) FROM {tbl}").format(
                tbl=sql.Identifier(schema, table)
            )
        )
        return cur.fetchone()[0]


def _connect(url: str, *, read_only: bool = False):
    kwargs = {}
    if read_only:
        kwargs["options"] = "-c default_transaction_read_only=on"
    return psycopg.connect(_normalize_url(url), autocommit=False, **kwargs)


# --------------------------------------------------------------------------- #
# 备份 / 恢复
# --------------------------------------------------------------------------- #
def _ensure_backup_limits(backup_root: Path, keep: int) -> None:
    if keep <= 0:
        return
    dirs = sorted(
        (p for p in backup_root.iterdir() if p.is_dir()), reverse=True
    )
    for stale in dirs[keep:]:
        _log.info("清理过期备份：%s", stale)
        shutil.rmtree(stale, ignore_errors=True)


def create_backup(conn, schema: str, tables: list[str], backup_root: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = backup_root / ts / "data"
    dest.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    for t in tables:
        data = _copy_out(conn, schema, t)
        counts[t] = data.count(b"\n")
        with gzip.open(dest / f"{t}.gz", "wb") as fh:
            fh.write(data)

    manifest = {
        "created_at": ts,
        "schema": schema,
        "source_db": conn.info.dbname,
        "tables": tables,
        "counts": counts,
    }
    (backup_root / ts / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _log.info("已备份测试库到 %s（%d 张表）", dest, len(tables))
    return backup_root / ts


def restore_backup(conn, schema: str, backup_dir: Path, tables: list[str]) -> None:
    manifest_file = backup_dir / "manifest.json"
    if not manifest_file.exists():
        raise RuntimeError(f"备份目录缺少 manifest.json：{backup_dir}")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    tables = manifest.get("tables") or tables
    _log.info("开始回滚测试库到 %s（%d 张表）", backup_dir, len(tables))

    _truncate_all(conn, schema, tables)
    data_dir = backup_dir / "data"
    for t in tables:
        gz = data_dir / f"{t}.gz"
        if not gz.exists():
            raise RuntimeError(f"备份缺表文件：{gz}")
        with gzip.open(gz, "rb") as fh:
            _copy_in(conn, schema, t, fh.read())
    _reset_sequences(conn, schema, iter_sequences(conn, schema))
    conn.commit()
    _log.info("回滚完成")


# --------------------------------------------------------------------------- #
# 动作实现
# --------------------------------------------------------------------------- #
def dry_run(src_conn, dst_conn, schema: str, tables: list[str], order: list[str]) -> int:
    _log.info("=== dry-run（只读对比，不写数据）===")
    _log.info("源库=%s  目标库=%s  schema=%s", src_conn.info.dbname, dst_conn.info.dbname, schema)

    src_tables = iter_tables(src_conn, schema)
    dst_tables = iter_tables(dst_conn, schema)
    only_src = sorted(set(src_tables) - set(dst_tables))
    only_dst = sorted(set(dst_tables) - set(src_tables))
    if only_src:
        _log.warning("仅存在于生产、测试缺失的表：%s", only_src)
    if only_dst:
        _log.warning("仅存在于测试、生产没有的表（覆盖后仍保留，不会被删除）：%s", only_dst)

    print(f"\n{'表名':32s} {'生产行数':>10s} {'测试行数':>10s}  动作")
    diff = 0
    for t in order:
        s, d = _count(src_conn, schema, t), _count(dst_conn, schema, t)
        same = "OK" if s == d else "DIFF"
        if s != d:
            diff += 1
        print(f"{t:32s} {s:10d} {d:10d}  {same}")
    print(f"\n{diff}/{len(order)} 张表与测试库行数不同；--apply 将 TRUNCATE+重整全部 {len(order)} 张表并重置序列。")
    return 0


def apply_sync(src_conn, dst_conn, schema: str, order: list[str], cfg: dict, *, do_backup: bool) -> int:
    _log.info("=== 执行覆盖同步 ===")
    _log.info("源库=%s -> 目标库=%s  schema=%s，共 %d 张表",
              src_conn.info.dbname, dst_conn.info.dbname, schema, len(order))

    if do_backup:
        backup_root = Path(cfg["SYNC_BACKUP_DIR"])
        create_backup(dst_conn, schema, order, backup_root)
        _ensure_backup_limits(backup_root, int(cfg["SYNC_BACKUP_KEEP"]))
        # 关闭备份读语句隐式开启的事务，避免与覆盖写事务串在一起
        dst_conn.rollback()

    # 源：单一一致性快照（只读、可重复读）
    # 关闭 discovery 阶段（iter_tables/topo_order）隐式开启的事务后再显式开快照
    src_conn.rollback()
    src_cur = src_conn.cursor()
    src_cur.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")

    # 目标：单事务覆盖
    try:
        _truncate_all(dst_conn, schema, order)
        for t in order:
            data = _copy_out(src_conn, schema, t)
            _copy_in(dst_conn, schema, t, data)
            _log.debug("  %s 已重灌", t)
        _reset_sequences(dst_conn, schema, iter_sequences(src_conn, schema))
        dst_conn.commit()
    except Exception:
        dst_conn.rollback()
        raise
    finally:
        try:
            src_cur.execute("COMMIT")
        except Exception:
            src_conn.rollback()

    # 行数核对
    src_cnt = {t: _count(src_conn, schema, t) for t in order}
    print(f"\n{'表名':32s} {'生产':>8s} {'测试':>8s}  状态")
    bad = 0
    for t in order:
        dst_cnt = _count(dst_conn, schema, t)
        ok = src_cnt[t] == dst_cnt
        bad += 0 if ok else 1
        print(f"{t:32s} {src_cnt[t]:8d} {dst_cnt:8d}  {'OK' if ok else 'MISMATCH'}")
    if bad:
        _log.error("行数核对存在 %d 张表不一致", bad)
        return 1
    _log.info("覆盖同步完成，%d/%d 张表行数一致", len(order), len(order))
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _setup_logging() -> None:
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "sync_prod_to_demo.log",
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    _log.addHandler(handler)
    _log.addHandler(console)
    _log.setLevel(logging.INFO)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生产库 -> 测试库 覆盖同步（自包含 psycopg 实现）"
    )
    parser.add_argument("--apply", action="store_true", help="真正覆盖（默认 dry-run）")
    parser.add_argument("--no-backup", action="store_true", help="--apply 时跳过覆盖前备份")
    parser.add_argument("--restore", metavar="DIR", help="用备份目录回滚测试库")
    parser.add_argument("--list-backups", action="store_true", help="列出本地备份")
    args = parser.parse_args()

    _setup_logging()
    try:
        return _dispatch(args)
    except Exception:
        _log.exception("同步脚本异常退出")
        return 1


def _dispatch(args) -> int:
    cfg = get_config()
    schema = cfg["POSTGRES_SCHEMA"]

    target_url = cfg.get("SYNC_TARGET_DATABASE_URL") or cfg.get("DATABASE_URL", "")
    if not target_url:
        _log.error("未配置测试库连接串（SYNC_TARGET_DATABASE_URL 或 DATABASE_URL），请检查 .env")
        return 2
    source_url = cfg.get("SYNC_SOURCE_DATABASE_URL") or _swap_dbname(
        target_url, cfg["SYNC_SOURCE_DB"]
    )

    src_db = _dbname_of(source_url)
    dst_db = _dbname_of(target_url)
    expect_target = cfg["SYNC_TARGET_DB"]

    # 防误操作护栏
    if src_db == dst_db:
        _log.error("护栏：源库名 == 目标库名（%s），拒绝执行。", src_db)
        return 2
    if dst_db != expect_target:
        _log.error("护栏：目标库名应为 '%s'，实际为 '%s'，拒绝执行。", expect_target, dst_db)
        return 2

    if args.list_backups:
        root = Path(cfg["SYNC_BACKUP_DIR"])
        if not root.exists():
            print("暂无备份目录。")
            return 0
        for p in sorted(root.iterdir(), reverse=True):
            if p.is_dir():
                print(p.name)
        return 0

    src_conn = _connect(source_url, read_only=True)
    try:
        dst_conn = _connect(target_url)
    except Exception:
        src_conn.close()
        raise

    try:
        tables = iter_tables(src_conn, schema)
        order = topo_order(src_conn, schema, tables)
        if not order:
            _log.error("schema '%s' 下没有发现任何表，终止。", schema)
            return 2

        if args.restore:
            _log.warning("将用 %s 覆盖测试库当前数据（涉及 %d 张表）", args.restore, len(order))
            restore_backup(dst_conn, schema, Path(args.restore), order)
            return 0

        if args.apply:
            return apply_sync(src_conn, dst_conn, schema, order, cfg,
                              do_backup=not args.no_backup)

        return dry_run(src_conn, dst_conn, schema, tables, order)
    finally:
        dst_conn.close()
        src_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())