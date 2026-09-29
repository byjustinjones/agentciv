"""End-to-end: the real HTTP server + a remote SDK bot + an MCP-driven player
(``python -m agentciv.mcp_server`` subprocess speaking scripted JSON-RPC) + four
house bots play a short game to completion; then the replay (full and
compact), the game list and the leaderboard reflect it.
"""
from __future__ import annotations

import gzip
import json
import subprocess
import sys
import threading
import urllib.request

import pytest

from agentciv.client import AgentCivClient, run_bot
from agentciv.server import create_server
from agentciv.server.manager import bot_available

MAX_TURNS = 6


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    yield srv
    srv.stop()


class MCPPlayer:
    """Drives the MCP server over stdio like an MCP client (Claude Code etc.) would."""

    def __init__(self, url: str):
        self.proc = subprocess.Popen([sys.executable, "-m", "agentciv.mcp_server", "--url", url],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.next_id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, "MCP server closed its stdout"
        resp = json.loads(line)
        assert resp["id"] == self.next_id, resp
        return resp

    def notify(self, method: str) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def tool(self, tool_name: str, /, **args) -> tuple[str, bool]:
        res = self.request("tools/call", {"name": tool_name, "arguments": args})["result"]
        return res["content"][0]["text"], res["isError"]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(10)
        except Exception:
            self.proc.kill()


def _claim_target(view: dict) -> list[int] | None:
    """An unowned passable tile 4-adjacent to our territory (a plausible claim)."""
    me = view["you"]["id"]
    m = view["map"]
    impassable = {ch for ch, t in view["costs"]["map"]["terrain"].items() if not t["passable"]}
    relics = {(r["x"], r["y"]) for r in m["relics"]}
    for y in range(m["height"]):
        for x in range(m["width"]):
            if m["owner"][y][x] is not None or m["terrain"][y][x] in impassable or (x, y) in relics:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < m["width"] and 0 <= ny < m["height"] and m["owner"][ny][nx] == me:
                    return [x, y]
    return None


def mcp_play(url: str, game_id: str, log: list) -> None:
    """A scripted MCP 'agent': read the rules, join, then state -> orders -> wait until the end."""
    p = MCPPlayer(url)
    try:
        init = p.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "e2e", "version": "0"}})["result"]
        assert init["serverInfo"]["name"] == "agentciv"
        p.notify("notifications/initialized")
        assert "submit_orders" in {t["name"] for t in p.request("tools/list")["result"]["tools"]}
        rules, err = p.tool("get_rules")
        assert not err and "HTTP API quick reference" in rules  # the server appends the API guide
        text, err = p.tool("join_game", game_id=game_id, name="MCP-Agent")
        assert not err and "Joined game" in text, text
        first = True
        while True:
            text, err = p.tool("wait_for_turn", timeout=20)
            assert not err, text
            if "Still waiting" in text:
                continue
            if "GAME OVER" in text:
                log.append(("over", text))
                break
            full, err = p.tool("get_state", full=True)
            assert not err
            view = json.loads(full.split("FULL VIEW JSON:\n", 1)[1])
            orders = []
            target = _claim_target(view)
            if target:
                orders.append({"type": "claim", "at": target})
            if first:  # a malformed order: the reply must say what is wrong and show the right shape
                bad, err = p.tool("submit_orders", orders=orders + [{"type": "claim", "at": "6,4"}],
                                  turn=view["turn"])
                assert not err and "1 rejected" in bad and 'correct shape: {"type":"claim"' in bad, bad
                log.append(("bad", bad))
                first = False
            text, err = p.tool("submit_orders", orders=orders, turn=view["turn"])
            if err and "stale turn" in text:
                continue  # the turn resolved meanwhile (deadline): just go on
            assert not err and "0 rejected" in text, text
            log.append(("turn", view["turn"]))
        result, err = p.tool("get_result")
        assert not err and "finished" in result
        log.append(("result", result))
    except BaseException as e:  # surface failures from the thread
        log.append(("error", repr(e)))
        raise
    finally:
        p.close()


def test_sdk_bot_mcp_player_and_house_bots_play_to_completion(server):
    url = server.url
    host = AgentCivClient(url)
    server.manager.open_ratings = True  # a short custom game only counts on an open-ratings server
    house = [b for b in ("strategist", "economist", "rusher", "turtle") if bot_available(b)]
    house += ["idle"] * (4 - len(house))
    gid = host.create_game(name="e2e", max_players=6, bots=house, turn_timeout=20, max_turns=MAX_TURNS)
    assert host.game(gid)["status"] == "lobby"

    sdk_out: dict = {}
    sdk = threading.Thread(target=lambda: sdk_out.update(run_bot("economist", url, game_id=gid, name="SDK-Agent")))
    mcp_log: list = []
    mcp = threading.Thread(target=mcp_play, args=(url, gid, mcp_log))
    sdk.start()
    mcp.start()
    sdk.join(120)
    mcp.join(120)
    assert not sdk.is_alive() and not mcp.is_alive()
    assert not [e for e in mcp_log if e[0] == "error"], mcp_log

    # the game ran to completion with both remote players seated
    summary = host.game(gid)
    res = summary["result"]
    assert summary["status"] == "finished" and res is not None
    names = {p["name"]: p for p in summary["players"]}
    assert not names["SDK-Agent"]["is_bot"] and not names["MCP-Agent"]["is_bot"]
    assert sum(p["is_bot"] for p in summary["players"]) == 4
    assert sdk_out["game_id"] == gid and sdk_out["place"] in range(1, 7)
    turns_played = [t for kind, t in mcp_log if kind == "turn"]
    assert turns_played == list(range(len(turns_played))) and len(turns_played) >= min(3, res["turn"])
    assert any(kind == "over" for kind, _ in mcp_log)

    # replay: one frame per turn, full and compact agree
    full = host.replay(gid)
    assert full["result"] == res
    n = len(full["frames"])
    assert [f["turn"] for f in full["frames"]] == list(range(n)) and full["frames"][-1]["status"] == "finished"
    compact = json.loads(_get(url, f"/api/games/{gid}/replay?compact=1"))
    assert compact["compact"] is True and compact["total_frames"] == n and len(compact["frames"]) == n
    last = compact["frames"][-1]
    assert "costs" not in last and "terrain" not in last["map"] and "history" not in last["market"]
    last["map"]["terrain"] = compact["static"]["terrain"]
    last["costs"] = compact["static"]["costs"]
    expect = dict(full["frames"][-1])
    expect["market"] = {k: v for k, v in expect["market"].items() if k != "history"}
    assert last == expect
    part = json.loads(_get(url, f"/api/games/{gid}/replay?compact=1&from=2&to=3"))
    assert [f["turn"] for f in part["frames"]] == [2, 3] and part["from"] == 2 and part["to"] == 3

    # leaderboard: remote agents by name, house bots by bot type
    board = {r["name"] for r in host.leaderboard()}
    assert {"SDK-Agent", "MCP-Agent"} <= board and set(house) <= board
    # the game is listed as finished
    assert any(g["game_id"] == gid and g["status"] == "finished" for g in host.list_games())


def _get(url: str, path: str, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url + path, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return data
