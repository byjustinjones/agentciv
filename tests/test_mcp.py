"""MCP server tests: JSON-RPC over a real stdio subprocess, plus in-process checks."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

from agentciv.mcp_server import TOOLS, AgentCivMCP, MCPServer
from agentciv.server import create_server


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    yield srv
    srv.stop()


class StdioMCP:
    """Drive ``python -m agentciv.mcp_server`` through pipes."""

    def __init__(self, url: str):
        env = {**os.environ, "AGENTCIV_URL": url}
        self.proc = subprocess.Popen([sys.executable, "-m", "agentciv.mcp_server"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, env=env)
        self.next_id = 0

    def send(self, obj) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        resp = json.loads(self.proc.stdout.readline())
        assert resp["id"] == self.next_id and resp["jsonrpc"] == "2.0"
        return resp

    def tool(self, tool_name: str, /, **args) -> tuple[str, bool]:
        res = self.request("tools/call", {"name": tool_name, "arguments": args})["result"]
        return res["content"][0]["text"], res["isError"]

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(10)


@pytest.fixture()
def mcp(server):
    m = StdioMCP(server.url)
    yield m
    m.close()


def test_handshake_and_tool_list(mcp):
    init = mcp.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "pytest", "version": "0"}})["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert "tools" in init["capabilities"] and init["serverInfo"]["name"] == "agentciv"
    mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"})  # no response expected
    assert mcp.request("ping")["result"] == {}
    tools = mcp.request("tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"get_rules", "list_games", "create_game", "join_game", "quickmatch", "get_state", "get_map",
            "submit_orders", "wait_for_turn", "get_result"} <= names
    for t in tools:
        assert t["inputSchema"]["type"] == "object" and t["description"]
    assert mcp.request("no/such/method")["error"]["code"] == -32601


def test_old_protocol_version_is_echoed(mcp):
    init = mcp.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                      "clientInfo": {"name": "x", "version": "0"}})["result"]
    assert init["protocolVersion"] == "2024-11-05"


def test_play_a_game_through_tools(mcp, server):
    mcp.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "pytest", "version": "0"}})
    text, err = mcp.tool("get_rules")
    assert not err and "AgentCiv" in text
    text, err = mcp.tool("get_state")
    assert err and "quickmatch" in text  # not joined yet
    text, err = mcp.tool("create_game", max_players=2, bots=["idle"], max_turns=3, turn_timeout=0)
    assert not err
    gid = text.split()[2].rstrip(".")
    text, err = mcp.tool("list_games")
    assert gid in text
    text, err = mcp.tool("join_game", game_id=gid, name="Claude")
    assert not err and "p2" in text
    text, err = mcp.tool("wait_for_turn", timeout=10)
    assert not err and "turn 0/3" in text and "YOU: p2" in text
    text, err = mcp.tool("get_map")
    assert not err and "Legend" in text
    text, err = mcp.tool("get_state", full=True, include_map=True)
    view = json.loads(text.split("FULL VIEW JSON:\n", 1)[1])
    assert view["you"]["id"] == "p2"
    cap = view["you"]["capital"]
    text, err = mcp.tool("submit_orders", orders=[{"type": "recruit", "city": cap, "unit": "infantry"},
                                                   {"type": "claim", "at": [0, 0]}])
    assert not err and "1 order(s) accepted, 1 rejected" in text and "order #1" in text
    text, err = mcp.tool("submit_orders", orders=[], turn=7)
    assert err and "stale" in text
    for expected in (1, 2):
        text, err = mcp.tool("wait_for_turn", timeout=10)
        assert not err and f"turn {expected}/3" in text, text
        mcp.tool("submit_orders", orders=[])
    text, err = mcp.tool("wait_for_turn", timeout=10)
    assert "GAME OVER" in text
    text, err = mcp.tool("get_result")
    assert not err and "finished on turn 2" in text and "<- you" in text
    text, err = mcp.tool("leaderboard")
    assert not err
    text, err = mcp.tool("nope")
    assert err
    text, err = mcp.tool("join_game", game_id=gid)  # missing argument
    assert err and "bad arguments" in text


def test_quickmatch_tool(mcp):
    text, err = mcp.tool("quickmatch", name="Q", players=2, turn_timeout=5, lobby_timeout=0.1)
    assert not err and "Joined game" in text
    text, err = mcp.tool("wait_for_turn", timeout=10)
    assert not err and "turn 0/150" in text


def test_server_unreachable_and_bad_input():
    out = io.StringIO()
    srv = MCPServer(AgentCivMCP("http://127.0.0.1:9"))
    lines = [
        "not json",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "list_games", "arguments": {}}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "get_rules", "arguments": "oops"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}),
        json.dumps({"id": 3}),
        "",
    ]
    srv.serve(io.StringIO("\n".join(lines) + "\n"), out)
    resps = [json.loads(line) for line in out.getvalue().splitlines()]
    assert resps[0]["error"]["code"] == -32700
    assert resps[1]["result"]["isError"] and "cannot reach" in resps[1]["result"]["content"][0]["text"]
    assert resps[2]["result"]["isError"]
    assert resps[3]["error"]["code"] == -32600
    assert len(resps) == 4


def test_tool_schemas_are_valid_json_schema_objects():
    for t in TOOLS:
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        for req in schema.get("required", []):
            assert req in schema["properties"]
