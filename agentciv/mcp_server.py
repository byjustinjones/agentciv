"""AgentCiv MCP server (stdio, JSON-RPC 2.0, stdlib only).

Lets any MCP-capable agent (Claude Code, Claude Desktop, ...) play AgentCiv
through tools. Start the game server first, then register this as an MCP
server, e.g. for Claude Code::

    claude mcp add agentciv -e AGENTCIV_URL=http://localhost:8765 -- python -m agentciv.mcp_server

The server URL comes from ``AGENTCIV_URL`` (default http://localhost:8765);
``AGENTCIV_KEY`` (optional) is sent with join/quickmatch to register and
protect your player name (a registered name can only be played with its key).
The token obtained by ``join_game``/``quickmatch`` is kept in memory, so the
agent never has to handle it.

Typical loop: ``get_rules`` → ``quickmatch`` → repeat { ``get_state`` →
``submit_orders`` → ``wait_for_turn`` } until the game is over → ``get_result``.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

from .client import AgentCivClient, ApiError, ascii_map, summarize_view

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "agentciv", "title": "AgentCiv", "version": "0.1.0"}

INSTRUCTIONS = """AgentCiv is a simultaneous-turn strategy game for 2-12 players: grow an economy, expand,
trade, negotiate and (optionally) fight. Six ways to win: conquest, wonder, influence, relics, economic, score.
Start with get_rules (read it once), then quickmatch (or list_games + join_game). Each turn: get_state,
decide, submit_orders (resubmitting replaces your orders for that turn), then wait_for_turn. Turns have a
deadline; if you miss it you simply do nothing that turn. Coordinates are [x, y] (x = column)."""

ORDER_HELP = (
    "Order objects (coordinates [x,y]): "
    '{"type":"move","from":[3,4],"path":[[4,4]],"units":{"infantry":2}} | '
    '{"type":"recruit","city":[3,4],"unit":"cavalry","count":2} | '
    '{"type":"build","at":[5,4],"building":"farm"} | {"type":"claim","at":[6,4]} | '
    '{"type":"settle","at":[9,9]} | {"type":"disband","at":[3,4],"units":{"infantry":1}} | '
    '{"type":"market","side":"buy","resource":"stone","qty":40,"limit":2.5} | '
    '{"type":"offer_trade","to":"p2","give":{"wood":50},"want":{"gold":40}} | '
    '{"type":"accept_trade","offer_id":"t7"} | {"type":"propose_treaty","to":"p3","turns":20} | '
    '{"type":"accept_treaty","from":"p3"} | {"type":"break_treaty","with":"p3"} | '
    '{"type":"message","to":"p2","text":"Truce?"}'
)


def _schema(props: dict | None = None, required: list | None = None) -> dict:
    s: dict = {"type": "object", "properties": props or {}, "additionalProperties": False}
    if required:
        s["required"] = required
    return s


TOOLS = [
    {"name": "get_rules",
     "description": "Get the full AgentCiv rules guide (markdown), or the numeric cost tables (format='json'). "
                    "Read the markdown once before playing.",
     "inputSchema": _schema({"format": {"type": "string", "enum": ["markdown", "json"], "default": "markdown"}})},
    {"name": "list_games",
     "description": "List games on the server (id, name, status, turn, players).",
     "inputSchema": _schema()},
    {"name": "create_game",
     "description": "Create a new game lobby. House bots fill seats listed in 'bots'. Returns the game id; "
                    "then call join_game to take a seat (the game auto-starts when full).",
     "inputSchema": _schema({
         "name": {"type": "string"},
         "max_players": {"type": "integer", "minimum": 1, "maximum": 12, "default": 6},
         "min_players": {"type": "integer", "minimum": 1, "maximum": 12, "default": 2},
         "bots": {"type": "array", "items": {"type": "string"},
                  "description": "built-in bots to seat, e.g. [\"strategist\",\"economist\",\"rusher\"]"},
         "fill_with_bots": {"type": "boolean", "description": "fill empty seats with bots on start"},
         "turn_timeout": {"type": "number", "description": "seconds per turn (0 = wait for everyone)"},
         "max_turns": {"type": "integer", "minimum": 1, "maximum": 1000},
         "lobby_timeout": {"type": "number", "description": "auto-start after this many seconds"},
         "seed": {"type": "integer"}})},
    {"name": "join_game",
     "description": "Join a game lobby by id. Remembers your player id and token for the other tools.",
     "inputSchema": _schema({"game_id": {"type": "string"}, "name": {"type": "string"}}, ["game_id", "name"])},
    {"name": "quickmatch",
     "description": "Join the open quickmatch lobby (or create one). Empty seats are filled with house bots "
                    "after lobby_timeout seconds (default 30). The easiest way to start playing.",
     "inputSchema": _schema({"name": {"type": "string"},
                             "players": {"type": "integer", "minimum": 1, "maximum": 12, "default": 6},
                             "turn_timeout": {"type": "number", "description": "seconds per turn (default 30)"},
                             "lobby_timeout": {"type": "number"}}, ["name"])},
    {"name": "start_game",
     "description": "Start your game now (fills empty seats with bots if the game was created with fill_with_bots).",
     "inputSchema": _schema({"game_id": {"type": "string"}})},
    {"name": "get_state",
     "description": "Your current view as a compact text summary: resources/income, cities, armies, threats, "
                    "diplomacy, market, victory progress, all players, failed orders and events. Set full=true "
                    "to also get the complete JSON view (map terrain/ownership, all armies and cities).",
     "inputSchema": _schema({"full": {"type": "boolean", "default": False},
                             "include_map": {"type": "boolean", "default": False,
                                             "description": "append the ASCII map"}})},
    {"name": "get_map",
     "description": "ASCII map: terrain, owners, cities (@ capital, C city), relics (*), units (UPPERCASE).",
     "inputSchema": _schema()},
    {"name": "submit_orders",
     "description": "Submit your orders for the current turn (replaces any earlier submission this turn). "
                    "Returns accepted count and per-order errors to fix and resubmit. " + ORDER_HELP,
     "inputSchema": _schema({"orders": {"type": "array", "items": {"type": "object"}},
                             "turn": {"type": "integer", "description": "turn these orders are for "
                                                                         "(default: the current turn)"},
                             "ready": {"type": "boolean", "default": True,
                                       "description": "false = draft: keep the turn open for you until you "
                                                      "resubmit with ready=true (the deadline still applies)"}},
                            ["orders"])},
    {"name": "wait_for_turn",
     "description": "Block until the next turn starts (after the last turn you saw) or the game ends, then "
                    "return the new state summary. Call this after submit_orders.",
     "inputSchema": _schema({"timeout": {"type": "number", "default": 50,
                                         "description": "max seconds to wait (<= 110)"}})},
    {"name": "get_result",
     "description": "Final result of a game (winner, condition, placements, scores) or its current status.",
     "inputSchema": _schema({"game_id": {"type": "string"}})},
    {"name": "leaderboard",
     "description": "Player ratings (Weng-Lin / OpenSkill; rating = mu - 3*sigma).",
     "inputSchema": _schema()},
]


class ToolError(Exception):
    pass


class AgentCivMCP:
    """Tool implementations; holds the client (and token) in memory."""

    def __init__(self, url: str | None = None):
        self.client = AgentCivClient(url or os.environ.get("AGENTCIV_URL", "http://localhost:8765"))
        self.key = os.environ.get("AGENTCIV_KEY") or None  # name key (registers / proves your player name)
        self.last_turn = -1

    # ------------------------------------------------------------ helpers
    def _need_game(self) -> None:
        if not self.client.token:
            raise ToolError("You have not joined a game yet: call quickmatch or join_game first.")

    def _joined(self, res: dict) -> str:
        self.last_turn = -1
        return (f"Joined game {res['game_id']} as {res['player_id']} (status: {res.get('status')}). "
                f"Next: call wait_for_turn (it returns when the game starts), then get_state.")

    # ------------------------------------------------------------ tools
    def get_rules(self, format: str = "markdown") -> str:
        if format == "json":
            return json.dumps(self.client.rules_json(), indent=1)
        return self.client.rules()

    def list_games(self) -> str:
        games = self.client.list_games()
        if not games:
            return "No games. Use quickmatch or create_game."
        lines = []
        for g in games[:50]:
            ps = ", ".join(f"{p['id']} {p['name']}{' (bot)' if p.get('is_bot') else ''}" for p in g["players"])
            lines.append(f"{g['game_id']} \"{g['name']}\" {g['status']} turn {g['turn']} "
                         f"[{len(g['players'])}/{g['max_players']}] {ps}")
        return "\n".join(lines)

    def create_game(self, **options) -> str:
        gid = self.client.create_game(**options)
        return f"Created game {gid}. Call join_game with game_id={gid!r} to take a seat."

    def join_game(self, game_id: str, name: str) -> str:
        return self._joined(self.client.join(game_id, name, key=self.key))

    def quickmatch(self, name: str, players: int = 6, turn_timeout: float | None = None,
                   lobby_timeout: float | None = None) -> str:
        opts = {} if lobby_timeout is None else {"lobby_timeout": lobby_timeout}
        return self._joined(self.client.quickmatch(name, players=players, turn_timeout=turn_timeout,
                                                   key=self.key, **opts))

    def start_game(self, game_id: str | None = None) -> str:
        res = self.client.start(game_id)
        return f"Game status: {res.get('status')}" + ("" if res.get("started") else " (already started)")

    def get_state(self, full: bool = False, include_map: bool = False) -> str:
        self._need_game()
        view = self.client.state()
        if view.get("status") == "running":
            self.last_turn = max(self.last_turn, view["turn"])  # wait_for_turn waits for the next one
        text = summarize_view(view, self.client.player_id)
        if view.get("status") == "running":
            text += (f"\n\nSubmit orders for turn {view['turn']} with submit_orders, then call wait_for_turn.")
        if include_map:
            text += "\n\n" + ascii_map(view, self.client.player_id)
        if full:
            text += "\n\nFULL VIEW JSON:\n" + json.dumps(view, separators=(",", ":"))
        return text

    def get_map(self) -> str:
        self._need_game()
        return ascii_map(self.client.state(), self.client.player_id)

    def submit_orders(self, orders: list, turn: int | None = None, ready: bool = True) -> str:
        self._need_game()
        if not isinstance(orders, list):
            raise ToolError("orders must be an array of order objects")
        try:
            res = self.client.submit_orders(orders, turn=turn, ready=bool(ready))
        except ApiError as e:
            if e.status == 409:
                raise ToolError(f"{e.message}. Call get_state to see the current turn.") from None
            raise
        self.last_turn = max(self.last_turn, res["turn"])
        lines = [f"Turn {res['turn']}: {res['accepted']} order(s) accepted, {len(res['errors'])} rejected."]
        for err in res["errors"]:
            idx = err.get("index", -1)
            what = json.dumps(orders[idx]) if 0 <= idx < len(orders) else ""
            line = f"  order #{idx} {what}: {err.get('error')}"
            if err.get("hint"):
                line += f" (hint: {err['hint']})"
            if err.get("example"):
                line += f" — correct shape: {json.dumps(err['example'], separators=(',', ':'))}"
            lines.append(line)
        if res["errors"]:
            lines.append("Fix the rejected orders and resubmit the WHOLE list now (resubmitting replaces it; "
                         "the turn waits only ~2 s for a fix — submit with ready=false first if you need longer), "
                         "or call wait_for_turn.")
        elif not res.get("ready", True):
            lines.append("Draft saved (ready=false): resubmit with ready=true when done.")
        else:
            lines.append("Call wait_for_turn to wait for the turn to resolve.")
        return "\n".join(lines)

    def wait_for_turn(self, timeout: float = 50) -> str:
        self._need_game()
        timeout = max(0.0, min(float(timeout), 110.0))
        w = self.client.wait(since_turn=self.last_turn, timeout=timeout)
        if w.get("timed_out"):
            return (f"Still waiting (status {w['status']}, turn {w['turn']}). "
                    f"Call wait_for_turn again.")
        return self.get_state()

    def get_result(self, game_id: str | None = None) -> str:
        gid = game_id or self.client.game_id
        if not gid:
            raise ToolError("pass game_id (or join a game first)")
        g = self.client.game(gid)
        res = g.get("result")
        if not res:
            return f"Game {gid} is {g['status']} (turn {g['turn']}); no result yet."
        names = {p["id"]: p["name"] for p in g["players"]}
        places = "\n".join(f"  {i}. {pid} {names.get(pid, '')} (score {res['scores'].get(pid)})"
                           + (" <- you" if pid == self.client.player_id and gid == self.client.game_id else "")
                           for i, pid in enumerate(res["placements"], 1))
        return (f"Game {gid} finished on turn {res['turn']}: winner {res['winner']} "
                f"{names.get(res['winner'], '')} by {res['condition']}.\n{places}")

    def leaderboard(self) -> str:
        rows = self.client.leaderboard()
        if not rows:
            return "No rated games yet."
        return "\n".join(f"{i}. {r['name']}: rating {r['rating']} (mu {r['mu']}, sigma {r['sigma']}), "
                         f"{r['games']} games, {r['wins']} wins, avg place {r['avg_place']}"
                         for i, r in enumerate(rows[:50], 1))

    # ------------------------------------------------------------ dispatch
    def has_tool(self, name) -> bool:
        return isinstance(name, str) and name in {t["name"] for t in TOOLS} and callable(getattr(self, name, None))

    def call(self, name: str, args: dict) -> str:
        if not self.has_tool(name):
            raise ToolError(f"unknown tool {name!r}")
        return getattr(self, name)(**args)


class MCPServer:
    """Minimal MCP (JSON-RPC 2.0 over newline-delimited stdio) server."""

    def __init__(self, tools: AgentCivMCP | None = None):
        self.tools = tools or AgentCivMCP()

    @staticmethod
    def _result(msg_id, result) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def handle(self, msg: Any) -> dict | None:
        """Handle one decoded JSON-RPC message; returns the response (or None
        for notifications)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
                return None  # a response to a request we never sent: ignore
            return self._error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        method = msg["method"]
        msg_id = msg.get("id")
        is_notification = "id" not in msg
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return None if is_notification else self._error(msg_id, -32602, "params must be an object")
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
                result = {"protocolVersion": version,
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": SERVER_INFO,
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                if is_notification:
                    return None  # a notification gets no reply, so never run a tool (and its side effects) for one
                result = self._call_tool(params)
            elif method.startswith("notifications/"):
                return None
            else:
                return None if is_notification else self._error(msg_id, -32601, f"method not found: {method}")
        except Exception as e:  # pragma: no cover - defensive
            traceback.print_exc(file=sys.stderr)
            return None if is_notification else self._error(msg_id, -32603, f"internal error: {e}")
        return None if is_notification else self._result(msg_id, result)

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return self._tool_error("arguments must be an object")
        if not self.tools.has_tool(name):
            return self._tool_error(f"unknown tool {name!r}")
        try:
            text = self.tools.call(name, args)
        except TypeError as e:
            return self._tool_error(f"bad arguments for {name}: {e}")
        except ToolError as e:
            return self._tool_error(str(e))
        except ApiError as e:
            return self._tool_error(f"server error {e.status}: {e.message}")
        except OSError as e:
            return self._tool_error(f"cannot reach the AgentCiv server at {self.tools.client.base_url} ({e}). "
                                    f"Is it running? (python -m agentciv.server)")
        except Exception as e:  # a failing tool is a tool result with isError, not a JSON-RPC protocol error
            traceback.print_exc(file=sys.stderr)
            return self._tool_error(f"{name} failed: {type(e).__name__}: {e}")
        return {"content": [{"type": "text", "text": text}], "isError": False}

    @staticmethod
    def _tool_error(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "isError": True}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                resp: Any = self._error(None, -32700, "parse error")
            else:
                if isinstance(msg, list):  # legacy JSON-RPC batch
                    resp = [r for r in (self.handle(m) for m in msg) if r is not None] or None
                else:
                    resp = self.handle(msg)
            if resp is not None:
                stdout.write(json.dumps(resp, separators=(",", ":")) + "\n")
                stdout.flush()


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python -m agentciv.mcp_server",
                                     description="AgentCiv MCP server (stdio). Server URL from --url or AGENTCIV_URL.")
    parser.add_argument("--url", default=None, help="AgentCiv server URL (default $AGENTCIV_URL or http://localhost:8765)")
    args = parser.parse_args(argv)
    MCPServer(AgentCivMCP(args.url)).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
