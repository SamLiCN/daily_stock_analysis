#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Example MCP client for the project's mcp_server.py (stdio transport).

演示如何用官方 `mcp` SDK 的 `mcp.client.stdio` 直接连上本仓库的
`mcp_server.py`（一个走 stdio 的 MCP server），完成握手、列工具、调工具。

与服务端约束一致（见 mcp_server.py / 工作记忆 MEMORY.md）：
  - 服务端用 mcp 2.0 lowlevel Server，工具返回 `structured_content`(dict)
    同时也返回 `content`(JSON 文本) 兜底，客户端两种都能解析。
  - 服务端会自动读项目根 .env（与 cwd 无关），本客户端只需把
    `mcp_server.py` 作为子进程拉起即可，无需额外注入数据库连接。

运行（用项目 venv，确保 mcp 包可用）：
    ./.venv/bin/python examples/mcp_client_example.py          # 只读演示
    ./.venv/bin/python examples/mcp_client_example.py --write  # 额外演示 record_trade

安全说明：`--write` 会真实写入 portfolio_trades（事件溯源真相来源），
且用固定 trade_uid 做幂等，重复运行返回 conflict 而非重复插入。
建议先用 list_accounts 拿到真实 account_id 再写入。
"""

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = PROJECT_ROOT / "mcp_server.py"

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def run(write_demo: bool = False) -> None:
    # 用「当前解释器」拉起 server，保证 mcp / mcp_types 与本项目一致可导入。
    # args 只传 mcp_server.py：服务端自己负责加载 .env 与初始化数据库。
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_PATH)],
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            # 1) MCP 握手（必须，交换协议版本 / 能力）
            init = await session.initialize()
            print(f"[handshake] server: {init.server_info.name} "
                  f"v{init.server_info.version} / proto {init.protocol_version}")

            # 2) 列出服务端暴露的工具
            tools = await session.list_tools()
            print(f"\n[list_tools] {len(tools.tools)} tools:")
            for t in tools.tools:
                print(f"  - {t.name}: {t.description.splitlines()[0]}")

            # 3) 只读工具：账户列表
            res = await session.call_tool("list_accounts", {"include_inactive": False})
            print("\n[call_tool: list_accounts]")
            _print_result(res)

            # 4) 只读工具：持仓净值（默认全部账户、实时价）
            res = await session.call_tool("get_portfolio_net_value", {})
            print("\n[call_tool: get_portfolio_net_value]")
            _print_result(res)

            # 4b) 只读工具：交易流水（按账户/日期/标的/方向筛选 + 分页）
            res = await session.call_tool(
                "get_trades",
                {"page": 1, "page_size": 5, "side": "buy"},
            )
            print("\n[call_tool: get_trades] (交易流水，筛选 side=buy)")
            _print_result(res)

            # 5)（可选）写入工具：录入交易，带幂等 trade_uid
            if write_demo:
                res = await session.call_tool(
                    "record_trade",
                    {
                        "account_id": 1,
                        "symbol": "600519",
                        "trade_date": "2026-08-01",
                        "side": "buy",
                        "quantity": 100,
                        "price": 1500.0,
                        "trade_uid": "demo-client-600519-2026-08-01-buy",
                    },
                )
                print("\n[call_tool: record_trade] (write, idempotent)")
                _print_result(res)


def _print_result(res) -> None:
    """打印一次 call_tool 的结果，兼容 structured_content 与 content 文本。"""
    if getattr(res, "is_error", False):
        payload = res.structured_content if res.structured_content is not None else (
            res.content[0].text if res.content else None
        )
        print("  is_error: True")
        print("  payload:", json.dumps(payload, ensure_ascii=False)[:600])
        return

    # 优先用 structured_content（服务端透传的 dict）；缺失时回退解析 content 文本
    if res.structured_content is not None:
        data = res.structured_content
    else:
        data = json.loads(res.content[0].text)

    print("  is_error: False")
    if isinstance(data, dict):
        print("  keys:", list(data.keys()))
    print("  payload:", json.dumps(data, ensure_ascii=False)[:600])


if __name__ == "__main__":
    asyncio.run(run(write_demo="--write" in sys.argv))
