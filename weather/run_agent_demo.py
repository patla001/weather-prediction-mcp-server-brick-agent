#!/usr/bin/env python3
"""
Run the weather agent end to end, outside the AI Playground.

Connects to the deployed MCP server over streamable HTTP, gives its tools to a
Databricks Foundation Model endpoint, and runs the full tool-calling loop for a
set of natural-language questions. Prints a transcript suitable for DEMO.md.

The AI Playground is the no-code way to drive these tools, but it has been
returning intermittent INTERNAL_ERROR / "chat completion failed after 5
attempts" responses from the upstream provider. This script exercises exactly
the same pieces - same MCP server, same tool schemas, same system prompt - over
a path we control, so the demo does not depend on that UI being healthy.

Usage:
    python weather/run_agent_demo.py
    python weather/run_agent_demo.py --model databricks-claude-haiku-4-5
    python weather/run_agent_demo.py --question "Do I need an umbrella in Oslo tomorrow?"

Auth comes from the Databricks CLI profile (databricks auth login).
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request

DEFAULT_MCP_URL = os.environ.get(
    "WEATHER_MCP_URL",
    "https://mcp-weather-prediction-2808874854650870.aws.databricksapps.com/mcp",
)
DEFAULT_HOST = os.environ.get(
    "DATABRICKS_HOST", "https://dbc-7e085092-52e4.cloud.databricks.com"
)
DEFAULT_MODEL = "databricks-claude-sonnet-5"
MAX_TURNS = 6

QUESTIONS = [
    "Will it rain in Chicago tomorrow?",
    "Should I bring a jacket to Austin this weekend?",
    "Where should I go this Saturday - San Diego, Seattle, or Denver?",
    "Are there any severe weather alerts for Oklahoma City?",
    "What's the weather in Sprngfeld?",
]


def cli_token(profile: str = "DEFAULT") -> str:
    """Mint an OAuth token from the local Databricks CLI profile."""
    out = subprocess.run(
        ["databricks", "auth", "token", "--profile", profile],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["access_token"]


def post_json(url: str, payload: dict, headers: dict, timeout: int = 180):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
        sid = resp.headers.get("mcp-session-id")
    return body, sid


def parse_sse(body: str) -> dict:
    """FastMCP replies as text/event-stream; pull the JSON out of the data: line."""
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(body)


class MCPSession:
    """Minimal streamable-HTTP MCP client - enough to list and call tools."""

    def __init__(self, url: str, token: str):
        self.url = url
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        body, sid = post_json(self.url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "weather-demo", "version": "1"}},
        }, self.headers)
        info = parse_sse(body)["result"]["serverInfo"]
        self.headers["mcp-session-id"] = sid
        post_json(self.url, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                  self.headers)
        print(f"connected to MCP server: {info['name']} v{info['version']}")

    def list_tools(self) -> list:
        body, _ = post_json(self.url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                            self.headers)
        return parse_sse(body)["result"]["tools"]

    def call(self, name: str, arguments: dict) -> str:
        body, _ = post_json(self.url, {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, self.headers)
        result = parse_sse(body)["result"]
        if result.get("isError"):
            return json.dumps({"status": "error", "message": str(result)})
        return result["content"][0]["text"]


def as_openai_tools(mcp_tools: list) -> list:
    return [{"type": "function", "function": {
        "name": t["name"],
        "description": t.get("description") or "",
        "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
    }} for t in mcp_tools]


def chat(host: str, model: str, token: str, messages: list, tools: list) -> dict:
    body, _ = post_json(
        f"{host}/serving-endpoints/{model}/invocations",
        {"messages": messages, "tools": tools, "max_tokens": 1024},
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    return json.loads(body)["choices"][0]["message"]


def run(question: str, session: MCPSession, tools: list, system: str,
        host: str, model: str, token: str) -> None:
    print("=" * 78)
    print(f"Q: {question}")
    print("=" * 78)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": question}]
    for _ in range(MAX_TURNS):
        msg = chat(host, model, token, messages, tools)
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content"),
                         **({"tool_calls": calls} if calls else {})})
        if not calls:
            print(f"\nAnswer:\n{msg.get('content')}\n")
            return
        for c in calls:
            name = c["function"]["name"]
            args = json.loads(c["function"]["arguments"] or "{}")
            print(f"  tool call: {name}({json.dumps(args)})")
            result = session.call(name, args)
            preview = result if len(result) <= 220 else result[:220] + " ..."
            print(f"  tool result: {preview}")
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
    print("\n(stopped: hit the turn limit)\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--profile", default="DEFAULT")
    ap.add_argument("--prompt", default=os.path.join(os.path.dirname(__file__),
                                                     "AGENT_SYSTEM_PROMPT.md"))
    ap.add_argument("--question", action="append",
                    help="ask a specific question (repeatable); defaults to the demo set")
    args = ap.parse_args()

    token = cli_token(args.profile)
    session = MCPSession(args.mcp_url, token)
    mcp_tools = session.list_tools()
    print(f"tools available: {len(mcp_tools)}")
    print(f"model: {args.model}\n")

    system = open(args.prompt).read()
    tools = as_openai_tools(mcp_tools)
    for q in (args.question or QUESTIONS):
        run(q, session, tools, system, args.host, args.model, token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
