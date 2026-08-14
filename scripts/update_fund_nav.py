# -*- coding: utf-8 -*-
"""
手工更新场外开放式基金（非 ETF）的单位净值到 stock_daily 表。

背景
----
daily_stock_analysis 的价格抓取通道只支持：
  - A 股股票 (ak.stock_zh_a_hist)
  - 港股 (ak.stock_hk_hist)
  - ETF 场内基金 (ak.fund_etf_hist_em / fund_etf_spot_em)
  - 美股 (yfinance)
但**没有场外开放式基金（如 006327）的净值(NAV)通道**，导致：
  - 每日净值更新取不到（走股票接口 → 无数据）
  - 持仓页/分析取不到价格（依赖 stock_daily 里的价）
本脚本用 akshare 的 `fund_open_fund_info_em(symbol, "单位净值走势")`
直接抓取开放式基金净值，并复用项目自身的 `DatabaseManager.save_daily_data`
upsert 进 stock_daily，从而让净值更新/分析/持仓页恢复可用。

用法（在部署实例的项目根目录，用 dsa 的 venv 执行）
---------------------------------------------------
  # 只读诊断：抓取并打印最新净值，不写库
  python scripts/update_fund_nav.py --codes 006327 --dry-run

  # 真正写入（默认全量历史；可用 --since 限制起点）
  python scripts/update_fund_nav.py --codes 006327
  python scripts/update_fund_nav.py --codes 006327 011609 --since 2024-01-01

说明
----
  - 净值没有 OHLC，统一把 open/high/low/close 都填为单位净值；
    volume/amount 填 0；pct_chg 填日增长率(%)。
  - 自动读取部署实例自己的 .env（SQLite 或 PostgreSQL 均可）。
  - 幂等：save_daily_data 按 (code,date) upsert，重复跑不会重复插入。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Optional

# 允许以 `python scripts/update_fund_nav.py` 方式直接运行（保证能 import src）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("update_fund_nav")


def _fetch_open_fund_nav(code: str, since: Optional[str]) -> Optional[Dict[str, Any]]:
    """Fetch unit-NAV history for an open-end fund via AkShare.

    Returns dict with keys: df (normalized), latest (row), rows (int) or None.
    """
    try:
        import akshare as ak
        import pandas as pd
    except Exception as exc:  # pragma: no cover
        logger.error("akshare 未安装: %s", exc)
        return None

    try:
        raw = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
    except Exception as exc:
        logger.error("fund_open_fund_info_em(%s) 失败: %s", code, exc)
        return None

    if raw is None or raw.empty:
        logger.warning("%s 未返回净值数据", code)
        return None

    # 归一化列名：净值日期 / 单位净值 / 日增长率
    keep = {}
    for col in raw.columns:
        c = str(col)
        if "净值日期" in c or "日期" in c:
            keep[col] = "date"
        elif "单位净值" in c:
            keep[col] = "close"
        elif "日增长率" in c or "增长率" in c:
            keep[col] = "pct_chg"
    if "date" not in keep.values() or "close" not in keep.values():
        logger.error("%s 净值返回列不符合预期: %s", code, list(raw.columns))
        return None

    df = raw.rename(columns=keep)[list(keep.values())].copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    if since:
        try:
            since_d = datetime.strptime(since, "%Y-%m-%d").date()
            df = df[df["date"] >= since_d]
        except Exception:
            logger.warning("since 参数无效，忽略: %s", since)
    df = df.sort_values("date")
    df["open"] = df["close"]
    df["high"] = df["close"]
    df["low"] = df["close"]
    df["volume"] = 0.0
    df["amount"] = 0.0
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    if "pct_chg" in df.columns:
        df["pct_chg"] = pd.to_numeric(df["pct_chg"], errors="coerce")
    df = df.dropna(subset=["close"])

    latest = df.iloc[-1] if not df.empty else None
    return {
        "df": df,
        "latest": latest,
        "rows": len(df),
    }


def _write_to_db(code: str, df, data_source: str) -> int:
    from src.storage import DatabaseManager
    db = DatabaseManager()
    return db.save_daily_data(df, code, data_source)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="手工更新场外开放式基金净值到 stock_daily")
    parser.add_argument("--codes", nargs="+", default=["006327"], help="基金代码列表")
    parser.add_argument("--since", default=None, help="只抓取该日期之后的净值，如 2024-01-01")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    parser.add_argument("--data-source", default="AkshareFundNAV", help="写入 data_source 标记")
    args = parser.parse_args(argv)

    total_written = 0
    for code in args.codes:
        code = code.strip()
        logger.info("处理基金 %s ...", code)
        result = _fetch_open_fund_nav(code, args.since)
        if not result or result["rows"] == 0:
            logger.warning("%s 无可用净值，跳过", code)
            continue
        latest = result["latest"]
        logger.info(
            "%s 取到 %d 行净值，最新: %s 单位净值=%s 日增长率=%s",
            code, result["rows"], latest["date"], latest["close"],
            latest.get("pct_chg"),
        )
        if args.dry_run:
            logger.info("%s [dry-run] 未写入数据库", code)
            continue
        written = _write_to_db(code, result["df"], args.data_source)
        logger.info("%s 写入 stock_daily 新增 %d 条（upsert 覆盖其余）", code, written)
        total_written += written

    if not args.dry_run:
        logger.info("完成，共新增 %d 条净值记录", total_written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
