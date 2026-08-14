#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断 / 修复部署实例持仓数量缺失（默认针对 513050）。

背景：
  前端持仓页 /api/v1/portfolio/snapshot 每次从 portfolio_trades 实时重放。
  若部署实例显示的数量比预期少（如 513050 显示 14200 而非 15200），
  多半是其 portfolio_trades 缺了一笔买入（此处为 2023-01-12 买入 1000 股 @1.158）。

用法（在【部署实例】的项目根目录、用 dsa 的 venv 运行）：
  # 1) 只读诊断：打印账户、513050 交易流水、重放数量、快照数量
  python scripts/repair_portfolio_missing_trade.py

  # 2) 确认缺那笔后，补录并触发事件溯源重放（自动失效派生快照）
  python scripts/repair_portfolio_missing_trade.py --repair

  # 其他标的：
  python scripts/repair_portfolio_missing_trade.py --symbol 600941 --expected 200 --repair

说明：
  - 自动识别 SQLite / PostgreSQL（读项目根 .env 的 DATABASE_BACKEND / DATABASE_URL）。
  - --repair 用 idempotent 的 dedup_hash，重复执行不会重复插入（冲突时提示已存在）。
  - 仅写入 portfolio_trades 一笔，其余派生表由事件溯源在下次读取时自动重建。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

# 确保能 import 项目内模块
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except Exception:
    pass

from src.services.portfolio_service import PortfolioService  # noqa: E402
from src.storage import DatabaseManager  # noqa: E402


# 已知缺失交易（来自券商成交截图）：2023-01-12 中概互联ETF 513050 买入 1000 股 @1.158
DEFAULT_MISSING_TRADE = {
    "trade_date": date(2023, 1, 12),
    "side": "buy",
    "quantity": 1000.0,
    "price": 1.158,
    "fee": 0.0,
    "tax": 0.0,
}


def _query_trades(session, symbol: str):
    """直接读 portfolio_trades（SQLite / PG 列名一致）。"""
    from sqlalchemy import text
    rows = session.execute(
        text(
            "SELECT account_id, trade_date, side, quantity, price, market, currency, fee, tax "
            "FROM portfolio_trades WHERE symbol = :sym ORDER BY trade_date ASC"
        ),
        {"sym": symbol},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def _replay_qty(trades):
    q = 0.0
    for t in trades:
        q += t["quantity"] if t["side"] == "buy" else -t["quantity"]
    return q


def diagnose(symbol: str):
    db = DatabaseManager()
    svc = PortfolioService()
    with db.session_scope() as session:
        trades = _query_trades(session, symbol)
    replayed = _replay_qty(trades)

    snap = svc.get_portfolio_snapshot(include_realtime=False)
    snap_qty = None
    for acc in snap.get("accounts", []):
        for p in acc.get("positions", []):
            if p.get("symbol") == symbol:
                snap_qty = p.get("quantity")
                break

    print("=" * 60)
    print(f"标的: {symbol}")
    print(f"portfolio_trades 笔数: {len(trades)}")
    print("-" * 60)
    for t in trades:
        print(
            f"  acct={t['account_id']} {t['trade_date']} {t['side']:>4} "
            f"qty={t['quantity']} price={t['price']} {t.get('market')}/{t.get('currency')}"
        )
    print("-" * 60)
    print(f"重放净持仓 (from trades): {replayed}")
    print(f"快照接口返回数量 (live replay): {snap_qty}")
    print("=" * 60)
    return trades, replayed, snap_qty


def repair(symbol: str, expected: float):
    trades, replayed, snap_qty = diagnose(symbol)
    if replayed == expected:
        print(f"[OK] 重放数量 {replayed} 已等于预期 {expected}，无需修复。")
        return

    print(f"[DIAG] 重放 {replayed} != 预期 {expected}，差值 {expected - replayed}")

    # 从已有 513050 交易推导 account_id / market / currency
    ref = next((t for t in trades if t["side"] == "buy"), None)
    if ref is None:
        print("[ABORT] 没有已有的 513050 买入交易可推导账户信息，请手动指定 --account-id。")
        return
    account_id = ref["account_id"]
    market = ref.get("market") or "cn"
    currency = ref.get("currency") or "CNY"

    mt = dict(DEFAULT_MISSING_TRADE)
    # 让缺失交易与已有交易同账户/市场/币种，避免脏数据
    dedup_hash = f"{symbol}-{mt['trade_date'].isoformat()}-{mt['side']}-{int(mt['quantity'])}-{mt['price']}"
    print(
        f"[REPAIR] 将经 PortfolioService.record_trade 补录："
        f"acct={account_id} {mt['trade_date']} {mt['side']} qty={mt['quantity']} "
        f"price={mt['price']} market={market} currency={currency} dedup_hash={dedup_hash}"
    )
    try:
        svc = PortfolioService()
        res = svc.record_trade(
            account_id=account_id,
            symbol=symbol,
            trade_date=mt["trade_date"],
            side=mt["side"],
            quantity=mt["quantity"],
            price=mt["price"],
            fee=mt["fee"],
            tax=mt["tax"],
            market=market,
            currency=currency,
            dedup_hash=dedup_hash,
            note="backfill missing 2023-01-12 buy (513050)",
        )
        print(f"[OK] 写入成功 trade_id={res.get('id')}")
    except Exception as e:  # 捕获幂等冲突等
        print(f"[WARN] record_trade 异常：{type(e).__name__}: {e}")
        print("        若提示 dedup_hash 冲突，说明该笔已存在，无需再补。")

    # 复验
    _, replayed2, snap_qty2 = diagnose(symbol)
    if replayed2 == expected:
        print(f"[DONE] 修复后重放数量 = {replayed2}（与预期一致）。前端将在下次读取时显示正确值。")
    else:
        print(f"[CHECK] 修复后重放数量 = {replayed2}，仍不等于 {expected}，请检查是否还有其他缺失交易。")


def main():
    ap = argparse.ArgumentParser(description="诊断/修复部署实例持仓数量缺失")
    ap.add_argument("--symbol", default="513050", help="标的代码，默认 513050")
    ap.add_argument("--expected", type=float, default=15200.0, help="预期正确持仓数量，默认 15200")
    ap.add_argument("--repair", action="store_true", help="补录缺失交易（不加则仅诊断）")
    args = ap.parse_args()

    if args.repair:
        repair(args.symbol, args.expected)
    else:
        diagnose(args.symbol)


if __name__ == "__main__":
    main()
