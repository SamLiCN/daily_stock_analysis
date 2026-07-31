#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性全量迁移：SQLite -> PostgreSQL（库 myagent / schema dsa）。

步骤：
  1. 先做 SQLite 引用完整性校验（PG 会强制外键，SQLite 当前未强制）；
  2. 再执行与 sync_to_postgres.py 相同的幂等全量镜像；
  3. 打印两边行数对比供核对。

迁移完成后，将 .env 的 DATABASE_BACKEND 改为 postgres 即完成切换；
原 .db 文件保持不动，可随时回退。

用法：
    python scripts/migrate_to_postgres.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sync_to_postgres import run_mirror


def main() -> int:
    print(">>> 步骤 1/3：引用完整性校验")
    rc = run_mirror(check_refs=True)
    if rc != 0:
        print("[ABORT] 引用完整性校验未通过，请先修正数据再迁移。")
        return rc

    print("\n>>> 步骤 2/3：执行全量镜像")
    rc = run_mirror(verify_only=False)
    if rc != 0:
        print("[FAIL] 镜像未成功完成，请检查 PostgreSQL 连接与权限。")
        return rc

    print("\n>>> 步骤 3/3：核对完成")
    print("若上方行数全部 [OK]，将 .env 的 DATABASE_BACKEND=postgres 即可切换。")
    print("原 .db 文件保持不变，如需回退改回 DATABASE_BACKEND=sqlite 即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
