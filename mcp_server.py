#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP server exposing daily-stock-analysis portfolio tools over stdio.

为 myagent 等 MCP 客户端提供持仓查询（净值 / 明细 / 账户）与写入（录入交易 / 录入资金流水）工具。

运行（stdio，默认；myagent 通过子进程方式拉起本进程）：
    python mcp_server.py

工具：
    get_portfolio_net_value  持仓净值（净资产 = 现金 + 市值），实时重放口径
    get_portfolio_positions  当前持仓明细（数量 / 均价 / 市值 / 浮动盈亏）
    list_accounts           账户列表
    get_trades              查询交易流水（portfolio_trades，按账户/日期/标的/方向筛选+分页）
    record_trade            录入交易（写入 portfolio_trades，事件溯源真相来源）
    record_cash_ledger      录入资金流水（入金 / 出金，写入 portfolio_cash_ledger）

说明：
    - 复用项目 .env（DATABASE_BACKEND / DATABASE_URL 等），自动适配 SQLite / PostgreSQL。
    - 净值与持仓明细基于 PortfolioService.get_portfolio_snapshot 实时重放交易流水，
      是权威口径；该调用会顺带回写快照缓存表（幂等刷新，不影响事实数据）。
    - 使用 mcp 2.0 原生低层 Server API（该版本已移除 FastMCP）。
"""

import sys
import json
from datetime import date
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from src.config import setup_env, get_config
from src.services.portfolio_service import PortfolioService, PortfolioConflictError
from src.repositories.portfolio_repo import PortfolioRepository

# 加载 .env（按 src/config.py 的父目录定位，与 cwd 无关），并初始化
# DatabaseManager 单例（建表 / 建 PostgreSQL 的 dsa schema）。
setup_env()
get_config()


# ---------------------------------------------------------------------------
# 业务封装（复用现有 service，保证口径一致）
# ---------------------------------------------------------------------------

def _service() -> PortfolioService:
    return PortfolioService(repo=PortfolioRepository())


def _parse_as_of(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return date.fromisoformat(value)


def get_net_value(
    account_id: Optional[int] = None,
    as_of: Optional[str] = None,
    cost_method: str = "fifo",
    include_realtime: bool = True,
) -> dict:
    return _service().get_portfolio_snapshot(
        account_id=account_id,
        as_of=_parse_as_of(as_of),
        cost_method=cost_method or "fifo",
        include_realtime=include_realtime,
    )


def get_positions(
    account_id: Optional[int] = None,
    as_of: Optional[str] = None,
    cost_method: str = "fifo",
    include_realtime: bool = True,
) -> dict:
    snap = get_net_value(account_id, as_of, cost_method, include_realtime)
    positions: list[dict] = []
    for acc in snap.get("accounts", []):
        for p in acc.get("positions", []):
            positions.append(
                {
                    "account_id": acc.get("account_id"),
                    "account_name": acc.get("account_name"),
                    "symbol": p.get("symbol"),
                    "market": p.get("market"),
                    "currency": p.get("currency"),
                    "quantity": p.get("quantity"),
                    "avg_cost": p.get("avg_cost"),
                    "last_price": p.get("last_price"),
                    "total_cost": p.get("total_cost"),
                    "market_value_base": p.get("market_value_base"),
                    "unrealized_pnl_base": p.get("unrealized_pnl_base"),
                    "unrealized_pnl_pct": p.get("unrealized_pnl_pct"),
                    "valuation_currency": p.get("valuation_currency"),
                    "price_date": p.get("price_date"),
                    "price_stale": p.get("price_stale"),
                }
            )
    return {
        "as_of": snap.get("as_of"),
        "currency": snap.get("currency"),
        "cost_method": snap.get("cost_method"),
        "account_count": snap.get("account_count"),
        "position_count": len(positions),
        "total_equity": snap.get("total_equity"),
        "total_market_value": snap.get("total_market_value"),
        "total_unrealized_pnl": snap.get("unrealized_pnl"),
        "fx_stale": snap.get("fx_stale"),
        "positions": positions,
    }


def list_accounts(include_inactive: bool = False) -> list:
    return _service().list_accounts(include_inactive=include_inactive)


def get_trades(
    account_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """查询交易流水（portfolio_trades，事件溯源真相来源之一）。

    支持按账户、成交日期区间、标的代码、买卖方向筛选，并分页返回。
    复用 PortfolioService.list_trade_events（自带账户/日期/分页/标的有效性校验），
    返回 {items:[...], total, page, page_size}。
    """
    return _service().list_trade_events(
        account_id=int(account_id) if account_id is not None else None,
        date_from=_parse_as_of(date_from),
        date_to=_parse_as_of(date_to),
        symbol=symbol,
        side=side,
        page=int(page or 1),
        page_size=int(page_size or 20),
    )


def record_trade(
    *,
    account_id: int,
    symbol: str,
    trade_date: str,
    side: str,
    quantity: float,
    price: float,
    fee: float = 0.0,
    tax: float = 0.0,
    market: Optional[str] = None,
    currency: Optional[str] = None,
    trade_uid: Optional[str] = None,
    dedup_hash: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """录入一笔交易（真实写入 portfolio_trades）。

    返回新建行 id 与回显字段；若 trade_uid/dedup_hash 重复则抛 PortfolioConflictError。
    """
    result = _service().record_trade(
        account_id=int(account_id),
        symbol=symbol,
        trade_date=_parse_as_of(trade_date),
        side=side,
        quantity=float(quantity),
        price=float(price),
        fee=float(fee or 0.0),
        tax=float(tax or 0.0),
        market=market,
        currency=currency,
        trade_uid=trade_uid,
        dedup_hash=dedup_hash,
        note=note,
    )
    return {
        "status": "created",
        "id": result["id"],
        "account_id": int(account_id),
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
        "trade_date": trade_date,
        "currency": currency,
        "fee": fee,
        "tax": tax,
        "note": note,
    }


def record_cash_ledger(
    *,
    account_id: int,
    event_date: str,
    direction: str,
    amount: float,
    currency: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """录入一笔资金流水（真实写入 portfolio_cash_ledger）。"""
    result = _service().record_cash_ledger(
        account_id=int(account_id),
        event_date=_parse_as_of(event_date),
        direction=direction,
        amount=float(amount),
        currency=currency,
        note=note,
    )
    return {
        "status": "created",
        "id": result["id"],
        "account_id": int(account_id),
        "direction": direction,
        "amount": amount,
        "event_date": event_date,
        "currency": currency,
        "note": note,
    }


# ---------------------------------------------------------------------------
# MCP 工具元信息
# ---------------------------------------------------------------------------

_TOOL_SPECS: list[types.Tool] = [
    types.Tool(
        name="get_portfolio_net_value",
        description=(
            "获取持仓净值（净资产 = 现金 + 市值）。基于事件溯源实时重放交易流水计算，"
            "是净值的权威口径。返回 CNY 聚合净值与每个账户的净值明细。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "integer",
                    "description": "指定账户 ID 只算该账户；留空算全部账户汇总",
                },
                "as_of": {
                    "type": "string",
                    "description": "估值日期 YYYY-MM-DD，默认今天",
                },
                "cost_method": {
                    "type": "string",
                    "enum": ["fifo", "avg"],
                    "description": "成本计算法，默认 fifo",
                },
                "include_realtime": {
                    "type": "boolean",
                    "description": "是否用实时价格估值；false 用最后已知价（离线更快），默认 true",
                },
            },
            "required": [],
        },
    ),
    types.Tool(
        name="get_portfolio_positions",
        description=(
            "获取当前持仓明细（每只股票的数量、均价、最新价、市值、浮动盈亏）。"
            "与净值同一口径（实时重放），保证一致。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "integer",
                    "description": "指定账户 ID 只算该账户；留空算全部账户",
                },
                "as_of": {
                    "type": "string",
                    "description": "估值日期 YYYY-MM-DD，默认今天",
                },
                "cost_method": {
                    "type": "string",
                    "enum": ["fifo", "avg"],
                    "description": "成本计算法，默认 fifo",
                },
                "include_realtime": {
                    "type": "boolean",
                    "description": "是否用实时价格估值；false 用最后已知价，默认 true",
                },
            },
            "required": [],
        },
    ),
    types.Tool(
        name="list_accounts",
        description="列出所有持仓账户（ID、名称、券商、市场、本位币、是否启用）。",
        input_schema={
            "type": "object",
            "properties": {
                "include_inactive": {
                    "type": "boolean",
                    "description": "是否包含已停用账户，默认 false",
                },
            },
            "required": [],
        },
    ),
    types.Tool(
        name="get_trades",
        description=(
            "查询交易流水（portfolio_trades，事件溯源的真相来源之一）。"
            "支持按账户、成交日期区间、标的代码、买卖方向筛选，并分页返回。"
            "每条记录含 id / account_id / trade_uid / symbol / side / quantity / price /"
            "fee / tax / trade_date / note 等字段。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "integer",
                    "description": "限定账户 ID（来自 list_accounts）；留空查所有启用账户",
                },
                "date_from": {
                    "type": "string",
                    "description": "起始成交日期 YYYY-MM-DD（含）",
                },
                "date_to": {
                    "type": "string",
                    "description": "截止成交日期 YYYY-MM-DD（含）",
                },
                "symbol": {
                    "type": "string",
                    "description": "标的代码筛选，如 600519 / hk00700 / AAPL；支持逗号分隔多个",
                },
                "side": {
                    "type": "string",
                    "enum": ["buy", "sell"],
                    "description": "买卖方向筛选",
                },
                "page": {
                    "type": "integer",
                    "description": "页码，从 1 开始，默认 1",
                },
                "page_size": {
                    "type": "integer",
                    "description": "每页条数，默认 20，上限 100",
                },
            },
            "required": [],
        },
    ),
    types.Tool(
        name="record_trade",
        description=(
            "录入一笔交易（写入 portfolio_trades，事件溯源的真相来源之一）。"
            "支持买入(buy)/卖出(sell)；会自动失效该账户的持仓与每日快照派生缓存，"
            "并在下次净值重放时计入。这是真实写入操作。\n"
            "建议传入 trade_uid（例如 'acc1-600519-2026-08-01-buy'）实现幂等："
            "重复提交相同 trade_uid 会被拒绝，返回 status=conflict。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_id": {"type": "integer", "description": "账户 ID（来自 list_accounts）"},
                "symbol": {"type": "string", "description": "标的代码，如 600519 / hk00700 / AAPL"},
                "trade_date": {"type": "string", "description": "成交日期 YYYY-MM-DD"},
                "side": {"type": "string", "enum": ["buy", "sell"], "description": "买卖方向"},
                "quantity": {"type": "number", "description": "成交数量（>0）"},
                "price": {"type": "number", "description": "成交价格（>0）"},
                "fee": {"type": "number", "description": "手续费，默认 0"},
                "tax": {"type": "number", "description": "税费（印花税等），默认 0"},
                "market": {"type": "string", "description": "市场 cn/hk/us，留空按账户默认"},
                "currency": {"type": "string", "description": "币种，留空按市场默认"},
                "trade_uid": {"type": "string", "description": "幂等键；重复提交会被拒绝（强烈建议传入）"},
                "dedup_hash": {"type": "string", "description": "去重哈希（可选，与 trade_uid 二选一）"},
                "note": {"type": "string", "description": "备注"},
            },
            "required": ["account_id", "symbol", "trade_date", "side", "quantity", "price"],
        },
    ),
    types.Tool(
        name="record_cash_ledger",
        description=(
            "录入一笔资金流水（入金/出金，写入 portfolio_cash_ledger，事件溯源真相来源之一）。"
            "会自动失效该账户派生缓存。这是真实写入操作。\n"
            "direction=in 表示入金/存入，out 表示出金/取出。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_id": {"type": "integer", "description": "账户 ID（来自 list_accounts）"},
                "event_date": {"type": "string", "description": "发生日期 YYYY-MM-DD"},
                "direction": {"type": "string", "enum": ["in", "out"], "description": "in=入金/存入, out=出金/取出"},
                "amount": {"type": "number", "description": "金额（>0，账户本位币或指定币种）"},
                "currency": {"type": "string", "description": "币种，留空按账户本位币"},
                "note": {"type": "string", "description": "备注"},
            },
            "required": ["account_id", "event_date", "direction", "amount"],
        },
    ),
]


# ---------------------------------------------------------------------------
# MCP handlers（mcp 2.0 lowlevel：构造时注入回调）
# ---------------------------------------------------------------------------

async def _handle_list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(tools=_TOOL_SPECS)


def _tool_ok(payload: dict) -> types.CallToolResult:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=payload,
    )


def _tool_err(payload: dict) -> types.CallToolResult:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structured_content=payload,
        is_error=True,
    )


async def _handle_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    name = params.name
    args = params.arguments or {}
    try:
        if name == "get_portfolio_net_value":
            payload = get_net_value(
                account_id=args.get("account_id"),
                as_of=args.get("as_of"),
                cost_method=args.get("cost_method", "fifo"),
                include_realtime=args.get("include_realtime", True),
            )
        elif name == "get_portfolio_positions":
            payload = get_positions(
                account_id=args.get("account_id"),
                as_of=args.get("as_of"),
                cost_method=args.get("cost_method", "fifo"),
                include_realtime=args.get("include_realtime", True),
            )
        elif name == "list_accounts":
            accs = list_accounts(include_inactive=bool(args.get("include_inactive", False)))
            payload = {"count": len(accs), "accounts": accs}
        elif name == "get_trades":
            payload = get_trades(
                account_id=args.get("account_id"),
                date_from=args.get("date_from"),
                date_to=args.get("date_to"),
                symbol=args.get("symbol"),
                side=args.get("side"),
                page=args.get("page", 1),
                page_size=args.get("page_size", 20),
            )
        elif name == "record_trade":
            payload = record_trade(
                account_id=args["account_id"],
                symbol=args["symbol"],
                trade_date=args["trade_date"],
                side=args["side"],
                quantity=args["quantity"],
                price=args["price"],
                fee=args.get("fee", 0.0),
                tax=args.get("tax", 0.0),
                market=args.get("market"),
                currency=args.get("currency"),
                trade_uid=args.get("trade_uid"),
                dedup_hash=args.get("dedup_hash"),
                note=args.get("note"),
            )
        elif name == "record_cash_ledger":
            payload = record_cash_ledger(
                account_id=args["account_id"],
                event_date=args["event_date"],
                direction=args["direction"],
                amount=args["amount"],
                currency=args.get("currency"),
                note=args.get("note"),
            )
        else:
            raise ValueError(f"unknown tool: {name}")
        return _tool_ok(payload)
    except PortfolioConflictError as exc:
        return _tool_err({"status": "conflict", "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - 工具级错误以 is_error 返回，便于调用方自纠错
        return _tool_err({"status": "error", "message": str(exc)})


server = Server(
    "daily-stock-portfolio",
    on_list_tools=_handle_list_tools,
    on_call_tool=_handle_call_tool,
)


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    anyio.run(_main)
