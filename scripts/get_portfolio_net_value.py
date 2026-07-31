#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从「当前数据库」读取持仓净值（net value）。

净值 = portfolio_daily_snapshots.total_equity。
本脚本按当前配置（DATABASE_BACKEND=sqlite/postgres/dual）自动连接对应库，
取到每个账户「最新快照日」的净值并汇总。

用法：
    python scripts/get_portfolio_net_value.py            # 读取当前库最新净值
    python scripts/get_portfolio_net_value.py --as-of 2026-07-31
    python scripts/get_portfolio_net_value.py --account 1
    python scripts/get_portfolio_net_value.py --live     # 走 PortfolioService 实时重放（会回写快照）

说明：
- 快照表是派生缓存（事件重放），total_equity 已是该账户当日的净资产（现金+市值）。
- 跨账户汇总时若各账户本位币不同，简单相加不准确；本脚本同时给出「按账户本币」与
  「CNY 汇总（经 FX 转换，无汇率时回退 1:1）」两种口径，并标注 fx_stale。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, func
from src.config import get_config, setup_env
from src.storage import (
    Base,
    DatabaseManager,
    PortfolioAccount,
    PortfolioDailySnapshot,
)


def _latest_snapshot_per_account(session, as_of, account_id=None):
    """返回每个账户在 <= as_of 的最新一条快照行。"""
    snap = PortfolioDailySnapshot
    # 子查询：每个账户的最大快照日（<= as_of）
    subq = (
        select(snap.account_id, func.max(snap.snapshot_date).label("max_date"))
        .where(snap.snapshot_date <= as_of)
        .group_by(snap.account_id)
    )
    if account_id is not None:
        subq = subq.where(snap.account_id == account_id)
    subq = subq.subquery()

    stmt = (
        select(snap)
        .join(
            subq,
            (snap.account_id == subq.c.account_id)
            & (snap.snapshot_date == subq.c.max_date),
        )
        .order_by(snap.account_id, snap.cost_method)
    )
    return session.execute(stmt).scalars().all()


def get_net_value_from_db(as_of: date, account_id=None) -> dict:
    """直接读取当前数据库快照表，返回净值口径。"""
    config = get_config()
    dm = DatabaseManager.get_instance()
    schema = config.postgres_schema or "dsa"
    # 统一设置 schema（sqlite 下 schema=None 由 Base.metadata 内部控制，不影响查询）
    for t in Base.metadata.tables.values():
        t.schema = schema if config.database_backend in ("postgres", "dual") else None

    with dm.get_session() as session:
        accounts = session.execute(select(PortfolioAccount)).scalars().all()
        acct_map = {a.id: a for a in accounts}
        rows = _latest_snapshot_per_account(session, as_of, account_id)

        per_account = []
        naive_total = 0.0  # 同币假设下的简单加总（仅供参考）
        cny_total = 0.0
        fx_stale = False
        for r in rows:
            acct = acct_map.get(r.account_id)
            base_ccy = acct.base_currency if acct else (r.base_currency or "CNY")
            # 简化 FX：本脚本不引入完整汇率表查询，跨币汇总用 accounts 本位币标注；
            # 若需精确 CNY 汇总，请改用 --live（PortfolioService 已含 FX 转换）。
            per_account.append(
                {
                    "account_id": r.account_id,
                    "account_name": getattr(acct, "name", None),
                    "snapshot_date": r.snapshot_date.isoformat()
                    if r.snapshot_date
                    else None,
                    "cost_method": r.cost_method,
                    "base_currency": base_ccy,
                    "total_cash": r.total_cash,
                    "total_market_value": r.total_market_value,
                    "total_equity": r.total_equity,
                    "unrealized_pnl": r.unrealized_pnl,
                    "realized_pnl": r.realized_pnl,
                    "fx_stale": r.fx_stale,
                }
            )
            naive_total += float(r.total_equity or 0.0)
            # 本位币即 CNY（本项目账户默认 CNY）时直接计入 CNY 汇总
            if base_ccy == "CNY":
                cny_total += float(r.total_equity or 0.0)
            else:
                fx_stale = True  # 非 CNY 账户未在脚本内做汇率换算
            fx_stale = fx_stale or bool(r.fx_stale)

    return {
        "as_of": as_of.isoformat(),
        "backend": config.database_backend,
        "account_count": len({p["account_id"] for p in per_account}),
        "per_account": per_account,
        "naive_total_equity": round(naive_total, 6),
        "cny_total_equity": round(cny_total, 6),
        "fx_stale": fx_stale,
    }


def _print_result(res: dict) -> None:
    print(f"数据库 backend : {res['backend']}")
    print(f"估值日期 as_of: {res['as_of']}")
    print(f"账户数        : {res['account_count']}")
    print("-" * 78)
    print(
        f"{'acct':>4}  {'name':<14} {'date':<11} {'method':<6} "
        f"{'ccy':<4} {'equity':>16} {'mv':>14} {'cash':>12}"
    )
    print("-" * 78)
    for p in res["per_account"]:
        name = (p["account_name"] or "")[:14]
        print(
            f"{p['account_id']:>4}  {name:<14} {p['snapshot_date'] or '':<11} "
            f"{p['cost_method']:<6} {p['base_currency']:<4} "
            f"{p['total_equity']:>16.2f} {p['total_market_value']:>14.2f} "
            f"{p['total_cash']:>12.2f}"
        )
    print("-" * 78)
    print(f"同币合计净值(naive) : {res['naive_total_equity']:.2f}")
    print(f"CNY 口径净值        : {res['cny_total_equity']:.2f}  (仅含本位币=CNY 账户)")
    print(f"fx_stale            : {res['fx_stale']}")


def _live_path(as_of: date, account_id=None) -> None:
    """走 PortfolioService 实时重放（最权威，含 FX 转换与跨账户 CNY 汇总）。"""
    from src.services.portfolio_service import PortfolioService

    svc = PortfolioService()
    snap = svc.get_portfolio_snapshot(
        account_id=account_id, as_of=as_of, include_realtime=True
    )
    print("== PortfolioService.get_portfolio_snapshot() (实时重放) ==")
    print(f"as_of={snap.get('as_of')} currency={snap.get('currency')} "
          f"accounts={snap.get('account_count')}")
    print(f"净资产 total_equity (CNY) : {snap.get('total_equity')}")
    print(f"总市值 total_market_value : {snap.get('total_market_value')}")
    print(f"总现金 total_cash         : {snap.get('total_cash')}")
    print(f"未实现盈亏 unrealized_pnl : {snap.get('unrealized_pnl')}")
    print(f"已实现盈亏 realized_pnl   : {snap.get('realized_pnl')}")
    print(f"fx_stale                 : {snap.get('fx_stale')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="读取当前数据库的持仓净值")
    parser.add_argument("--as-of", default=date.today().isoformat(),
                        help="估值日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--account", type=int, default=None, help="只看某账户")
    parser.add_argument("--live", action="store_true",
                        help="走 PortfolioService 实时重放（最权威，会回写快照）")
    args = parser.parse_args()

    setup_env()
    as_of = date.fromisoformat(args.as_of)

    if args.live:
        _live_path(as_of, args.account)
        return 0

    res = get_net_value_from_db(as_of, args.account)
    _print_result(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
