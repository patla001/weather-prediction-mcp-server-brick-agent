#!/usr/bin/env python
"""
Local smoke test for the weather-prediction MCP server.

Runs every tool through a real in-memory MCP client (fastmcp.Client bound
straight to the server object), so it exercises the same code path a
Databricks Agent Bricks agent takes: MCP tool call in, JSON dict out. No
Databricks workspace, no deployment, and no API key needed - Open-Meteo
and weather.gov are both keyless.

Usage:
    pip install -r requirements.txt
    PREDICTION_LOG_ENABLED=false python test_weather_tools.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The prediction log needs Lakebase; off by default for a local smoke test.
os.environ.setdefault("PREDICTION_LOG_ENABLED", "false")

from fastmcp import Client  # noqa: E402

from weather_mcp_server import mcp  # noqa: E402

CASES = [
    ("get_current_weather", {"location": "Chicago"}),
    ("get_forecast", {"location": "Austin, TX", "days": 3}),
    ("predict_umbrella_needed", {"location": "Chicago", "date": "tomorrow"}),
    ("predict_umbrella_needed", {"location": "Mumbai", "date": "tomorrow"}),
    ("get_travel_recommendation", {"location": "92182", "date": "tomorrow"}),
    ("get_severe_weather_alerts", {"location": "Miami"}),
    ("get_historical_weather", {"location": "Seattle", "date": "yesterday"}),
    ("get_historical_weather", {"location": "Anchorage", "date": "2026-02-14"}),
    ("compare_cities", {"locations": ["San Diego", "Chicago", "Seattle"], "date": "tomorrow"}),
    ("get_recent_predictions", {"limit": 5}),
    ("get_current_user", {}),
    # Error paths: the agent should see clean messages, never a traceback.
    ("get_current_weather", {"location": "Nowherecityville"}),
    ("get_forecast", {"location": "Chicago", "days": 99}),
    ("get_historical_weather", {"location": "Chicago", "date": "2099-01-01"}),
    ("predict_umbrella_needed", {"location": "Chicago", "date": "not-a-date"}),
]


async def main() -> int:
    failures = 0
    async with Client(mcp) as client:
        tools = await client.list_tools()
        print(f"=== {len(tools)} tools registered ===")
        for tool in tools:
            print(f"  - {tool.name}")

        for name, args in CASES:
            print(f"\n=== {name}({json.dumps(args)}) ===")
            result = await client.call_tool(name, args)
            data = result.data
            print(json.dumps(data, indent=2, default=str)[:1400])
            if not isinstance(data, dict) or "status" not in data:
                print("!! tool did not return a status field")
                failures += 1

    print(f"\n{'FAILURES: ' + str(failures) if failures else 'All tool calls returned a status.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
