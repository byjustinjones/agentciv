"""AgentCiv MCP server (stdio, JSON-RPC 2.0, stdlib only).

Lets any MCP-capable agent (Claude Code, Claude Desktop, ...) play AgentCiv
through tools. Start the game server first, then register this as an MCP
server, e.g. for Claude Code::

    claude mcp add agentciv -e AGENTCIV_URL=http://localhost:8765 -- python -m agentciv.mcp_server

The server URL comes from ``AGENTCIV_URL`` (default http://localhost:8765);
``AGENTCIV_KEY`` (optional) is sent with join/quickmatch to register and
protect your player name (a registered name can only be played with its key).
``AGENTCIV_AGENT`` (optional, a JSON object) is the agent manifest sent with
join/quickmatch when the tool call gives no ``agent`` (see AGENT_SCHEMA).
The token obtained by ``join_game``/``quickmatch`` is kept in memory, so the
agent never has to handle it.

Typical loop: ``get_rules`` → ``quickmatch`` → repeat { ``get_state`` →
(barter: ``propose_deal`` / ``respond_to_deal`` / ``say`` / ``wait_for_inbox``) →
``submit_orders`` → ``wait_for_turn`` } until the game is over → ``get_result``.
In a synchronous game (``sync: true``) the deal tools queue their action for
the end of the negotiation round and ``end_round`` (also ``wait_for_inbox`` /
``wait_for_turn`` while negotiating) ends the round, waits for every seat and
returns what the round produced.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

from .engine import constants as C
from .client import (AgentCivClient, ApiError, _deals_lines, agent_from_env, ascii_map, deal_warnings, describe_event,
                     order_warnings, peace_deal_notes, phase_result_lines, summarize_view)

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "agentciv", "title": "AgentCiv", "version": "0.1.0"}

INSTRUCTIONS = """AgentCiv is a simultaneous-turn strategy game for 2-12 players: grow an economy, expand,
trade, negotiate and (optionally) fight. Five ways to win: conquest, wonder, influence, economic, score.
Start with get_rules (read it once), then quickmatch (or list_games + join_game). Each turn: get_state,
decide, submit_orders (resubmitting replaces your orders for that turn), then wait_for_turn. Turns have a
deadline; if you miss it you simply do nothing that turn. Coordinates are [x, y] (x = column).
Barter live during a turn: propose_deal (resources, tiles, per-turn contracts, peace), then wait_for_inbox for
the reply and respond_to_deal (accept | reject | counter | withdraw); say sends messages; list_deals shows your
open deals, contracts and the public deal log. An accepted deal settles at once. The turn resolves as soon as
every player has submitted; diplomacy sent after that applies to the next turn.
Games created with fog: true hide parts of other players' state (rules §14).
Games created with sync: true are synchronous (get_state shows a SYNCHRONOUS TURN line): each turn is a few
negotiation rounds, then an orders phase. In a round, propose_deal / respond_to_deal / say are queued, not applied;
end_round ends your round, waits until every player has ended it (all queued actions are then applied in the turn's
rotating seat order) and returns the results. After the last round diplomacy is closed and submit_orders opens."""

ORDER_HELP = (
    "Order objects (coordinates [x,y]): "
    '{"type":"move","from":[3,4],"path":[[4,4]],"units":{"infantry":2}} | '
    '{"type":"recruit","city":[3,4],"unit":"cavalry","count":2} | '
    '{"type":"build","at":[5,4],"building":"farm"} | {"type":"claim","at":[6,4]} | '
    '{"type":"settle","at":[9,9]} | {"type":"disband","at":[3,4],"units":{"infantry":1}} | '
    '{"type":"market","side":"buy","resource":"stone","qty":40,"limit":2.5} | '
    '{"type":"propose","to":"p2","give":{"wood":50},"get":{"gold":40}} (or the propose_deal tool, '
    'applied at once) | {"type":"accept","deal":"d7"} | '
    '{"type":"propose_treaty","to":"p3","turns":20,"bond":0} (turns 20-40; bond optional, rules §9) | '
    '{"type":"accept_treaty","from":"p3","bond":0} | {"type":"release_treaty","with":"p3"} (both must order it '
    'the same turn) | {"type":"break_treaty","with":"p3"} (costs in you.treaty.break_preview) | '
    '{"type":"bank","gold":60} (gold into your bank, rules §5) | '
    '{"type":"say","to":"p2","text":"Truce?"} | '
    '{"type":"spy","target":"p3","mission":"treasury","invest":40} (fog games) | '
    '{"type":"counterintel","invest":30} (fog games)'
)

BUNDLE_SCHEMA = {
    "type": "object",
    "description": 'a bundle: resources {"food","wood","stone","gold"} (integers), "tiles": [[x,y],...] (your owned '
                   'non-city tiles), and/or a contract "per_turn": {"gold": 5} with "turns": 1-30; with peace, '
                   '"bond": banked gold that side pledges on the treaty (rules §9). {} = nothing',
    "properties": {
        "food": {"type": "integer", "minimum": 0}, "wood": {"type": "integer", "minimum": 0},
        "stone": {"type": "integer", "minimum": 0}, "gold": {"type": "integer", "minimum": 0},
        "tiles": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
        "per_turn": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
        "turns": {"type": "integer", "minimum": 1, "maximum": 30},
        "bond": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}
DEAL_PROPS = {
    "give": {**BUNDLE_SCHEMA, "description": "what YOU hand over. " + BUNDLE_SCHEMA["description"]},
    "get": {**BUNDLE_SCHEMA, "description": "what YOU receive. " + BUNDLE_SCHEMA["description"]},
    "peace": {"type": "integer", "minimum": C.DEAL_PEACE_MIN_TURNS, "maximum": C.DEAL_PEACE_MAX_TURNS,
              "description": "optional: both sides bound by a peace treaty for this many turns on acceptance, "
                             "subject to treaty slots, cooldowns and bonds (rules §9); between partners it renews "
                             "the treaty"},
    "message": {"type": "string", "description": "optional note shown with the deal (<= 300 chars)"},
    "expires_in": {"type": "integer", "minimum": 1, "maximum": 5, "description": "turns the deal stays open (2)"},
}


FOG_SCHEMA = {"type": "boolean", "description": "hide other players' armies outside your sight, their stockpiles, units "
                           "and exact score; enables spy and counterintel orders (rules §14)"}
SYNC_SCHEMA = {"type": "boolean", "description": "synchronous turns: negotiation rounds whose diplomacy is applied "
                                               "together at the end of each round, then an orders phase"}
ROUNDS_SCHEMA = {"type": "integer", "minimum": 0, "maximum": 10,
                 "description": "with sync: negotiation rounds per turn (default 3)"}
TRACK_SCHEMA = {"type": "string", "description": "a frozen evaluation track, e.g. eval-6p-fog-v1 (GET /api/tracks, "
                                               "docs/EVALUATION.md): fixes every other option, seats are anonymous "
                                               "while the game is live and joining needs an agent manifest"}
AGENT_SCHEMA = {
    "type": "object",
    "description": "optional: what plays this seat, shown in the game summary and replay (never used for "
                   "matchmaking). Strings only; keys model, model_version, effort, harness, harness_version, "
                   "prompt_sha256, tools, memory, notes. Default: $AGENTCIV_AGENT.",
    "properties": {k: {"type": "string"} for k in ("model", "model_version", "effort", "harness", "harness_version",
                                                   "prompt_sha256", "tools", "memory", "notes")},
    "additionalProperties": False,
}


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
         "seed": {"type": "integer"},
         "fog": FOG_SCHEMA,
         "sync": SYNC_SCHEMA,
         "negotiation_rounds": ROUNDS_SCHEMA,
         "track": TRACK_SCHEMA,
         "puzzle": {"type": "string",
                    "description": "a diagnostic position (docs/PUZZLES.md): winter, market, contracts or "
                                   "stop-victory. The puzzle fixes everything else; join it to start."}})},
    {"name": "join_game",
     "description": "Join a game lobby by id. Remembers your player id and token for the other tools.",
     "inputSchema": _schema({"game_id": {"type": "string"}, "name": {"type": "string"}, "agent": AGENT_SCHEMA},
                            ["game_id", "name"])},
    {"name": "quickmatch",
     "description": "Join the open quickmatch lobby (or create one). Empty seats are filled with house bots "
                    "after lobby_timeout seconds (default 30). The easiest way to start playing.",
     "inputSchema": _schema({"name": {"type": "string"},
                             "players": {"type": "integer", "minimum": 1, "maximum": 12, "default": 6},
                             "turn_timeout": {"type": "number", "description": "seconds per turn (default 30)"},
                             "lobby_timeout": {"type": "number"},
                             "fog": FOG_SCHEMA,
                             "sync": SYNC_SCHEMA,
                             "track": TRACK_SCHEMA,
                             "agent": AGENT_SCHEMA}, ["name"])},
    {"name": "start_game",
     "description": "Start your game now (fills empty seats with bots if the game was created with fill_with_bots).",
     "inputSchema": _schema({"game_id": {"type": "string"}})},
    {"name": "get_state",
     "description": "Your current view as a compact text summary: resources/income, cities, armies, threats, "
                    "diplomacy, market, victory progress, all players, failed orders and events. Set full=true "
                    "to also get the complete JSON view (map terrain/ownership, armies (in fog games: those in "
                    "your sight), cities).",
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
    {"name": "propose_deal",
     "description": "Propose a deal to another player, applied immediately (barter, any time during a turn). "
                    "Trade resources, land (tiles), contracts (per-turn payments: loans, tribute, rent) and/or peace. "
                    "The other side can accept (settles at once if both can deliver), reject or counter; use "
                    "wait_for_inbox to hear back. Example: give {\"wood\":60}, get {\"gold\":45}.",
     "inputSchema": _schema({"to": {"type": "string", "description": "player id, e.g. p2"}, **DEAL_PROPS},
                            ["to"])},
    {"name": "respond_to_deal",
     "description": "Answer a deal: accept | reject | counter (deals proposed TO you; counter = your new terms, "
                    "give/get from YOUR point of view) or withdraw (your own proposal). Applied immediately.",
     "inputSchema": _schema({"deal": {"type": "string", "description": "deal id, e.g. d7"},
                             "response": {"type": "string", "enum": ["accept", "reject", "counter", "withdraw"]},
                             **DEAL_PROPS}, ["deal", "response"])},
    {"name": "list_deals",
     "description": "Your open deals (incoming with whether they can settle now, outgoing), recently closed deals, "
                    "active contracts, the public log of executed deals and every player's reputation.",
     "inputSchema": _schema()},
    {"name": "say",
     "description": "Send a message now: to a player id (private) or \"all\" (public). Max 10 per turn.",
     "inputSchema": _schema({"to": {"type": "string", "description": "player id or \"all\""},
                             "text": {"type": "string"}}, ["to", "text"])},
    {"name": "wait_for_inbox",
     "description": "Block until something addressed to you happens (a deal proposed/countered/accepted/rejected, "
                    "a message...) or the turn changes, then list it. Use while haggling, before submit_orders.",
     "inputSchema": _schema({"timeout": {"type": "number", "default": 20,
                                         "description": "max seconds to wait (<= 110)"}})},
    {"name": "end_round",
     "description": "Synchronous games only: end your current negotiation round, wait until every player has "
                    "ended it (queued diplomacy is then applied in the turn's rotating seat order), and return the "
                    "results of your actions, new offers/messages and what is open now.",
     "inputSchema": _schema({"timeout": {"type": "number", "default": 60,
                                         "description": "max seconds to wait (<= 110)"}})},
    {"name": "get_result",
     "description": "Final result of a game (winner, condition, placements, scores) or its current status.",
     "inputSchema": _schema({"game_id": {"type": "string"}})},
    {"name": "leaderboard",
     "description": "Player ratings (Weng-Lin / OpenSkill; rating = mu - 3*sigma). mode='fog': the separate "
                    "ratings of fog-of-war games.",
     "inputSchema": _schema({"mode": {"type": "string", "enum": ["standard", "fog"], "default": "standard"}})},
]


class ToolError(Exception):
    pass


class AgentCivMCP:
    """Tool implementations; holds the client (and token) in memory."""

    def __init__(self, url: str | None = None):
        self.client = AgentCivClient(url or os.environ.get("AGENTCIV_URL", "http://localhost:8765"))
        self.key = os.environ.get("AGENTCIV_KEY") or None  # name key (registers / proves your player name)
        self.last_turn = -1
        self.submitted_turn = -1  # last turn we submitted orders for (to notice orders lost in a server crash)
        self.phase_id: int | None = None  # synchronous games: the phase our last get_state showed

    # ------------------------------------------------------------ helpers
    def _need_game(self) -> None:
        if not self.client.token:
            raise ToolError("You have not joined a game yet: call quickmatch or join_game first.")

    def _joined(self, res: dict) -> str:
        self.last_turn = self.submitted_turn = -1
        self.phase_id = None
        return (f"Joined game {res['game_id']} as {res['player_id']} (status: {res.get('status')}). "
                f"Next: call wait_for_turn (it returns when the game starts), then get_state.")

    def _diplomacy(self, action: dict, view: dict | None = None) -> str:
        """Send one diplomacy action; ``view``: the state it is written
        against (fetched here only when the peace notes need it)."""
        self._need_game()
        notes = []
        if action.get("type") == "accept" or (action.get("type") in ("propose", "counter") and action.get("peace")):
            notes = peace_deal_notes(view if view is not None else self.client.state(), [action])
        text = self._send_diplomacy(action)
        if notes:
            text += "\nNotes (peace while on a streak):\n" + "\n".join(f"  - {n}" for n in notes)
        return text

    def _send_diplomacy(self, action: dict) -> str:
        try:
            if self.phase_id is not None:
                res = self.client.diplomacy([action], phase=self.phase_id)
            else:
                res = self.client.diplomacy([action])
        except ApiError as e:
            if e.status == 409:
                raise ToolError(f"{e.message}. Call get_state to see the current turn.") from None
            raise
        r = (res.get("results") or [{}])[0]
        if r.get("ok") and r.get("status") == "queued":
            ph = res.get("phase") or {}
            return (f"Queued for the end of negotiation round {ph.get('round')} of {ph.get('of')} "
                    f"({res.get('queued')} action(s) queued). Nothing is applied until every player has ended "
                    "the round; call end_round when you have nothing more to send.")
        if not r.get("ok") and r.get("status") == "failed":  # accepted, but settlement failed: deal is closed
            raise ToolError(f"Deal {r.get('deal')} FAILED to settle (now closed; nothing moved): {r.get('error')}. "
                            f"Retrying accept will not work; propose new terms if you still want a deal.")
        if not r.get("ok"):
            text = f"Rejected: {r.get('error')}"
            if r.get("example"):
                text += f" — correct shape: {json.dumps(r['example'], separators=(',', ':'))}"
            raise ToolError(text)
        t = action["type"]
        if t == "propose":
            return (f"Deal {r['deal']} proposed to {action['to']}. It stays open until accepted, rejected, "
                    f"countered, withdrawn or expired; call wait_for_inbox to hear back.")
        if t == "counter":
            return f"Countered {r.get('countered')} with deal {r['deal']} (sent to its proposer)."
        if t == "accept":
            return f"Deal {r['deal']} accepted and executed: resources/tiles moved, contracts and peace in force."
        if t == "say":
            return "Message sent."
        return f"Deal {r['deal']} {'rejected' if t == 'reject' else 'withdrawn'}."

    @staticmethod
    def _terms(action: dict, give, get, peace, message, expires_in) -> dict:
        action["give"] = give or {}
        action["get"] = get or {}
        for k, v in (("peace", peace), ("message", message), ("expires_in", expires_in)):
            if v is not None:
                action[k] = v
        return action

    # ------------------------------------------------------------ tools
    def propose_deal(self, to: str, give: dict | None = None, get: dict | None = None, peace: int | None = None,
                     message: str | None = None, expires_in: int | None = None) -> str:
        return self._diplomacy(self._terms({"type": "propose", "to": to}, give, get, peace, message, expires_in))

    def respond_to_deal(self, deal: str, response: str, give: dict | None = None, get: dict | None = None,
                        peace: int | None = None, message: str | None = None, expires_in: int | None = None) -> str:
        if response == "counter":
            if not give and not get and not peace:
                raise ToolError("counter needs your terms: give and/or get (from YOUR point of view), or peace")
            return self._diplomacy(self._terms({"type": "counter", "deal": deal}, give, get, peace, message,
                                               expires_in))
        if response == "reject":
            return self._diplomacy({"type": "reject", "deal": deal, **({"message": message} if message else {})})
        if response == "accept":
            self._need_game()
            action = {"type": "accept", "deal": deal}
            view = self.client.state()
            warnings = deal_warnings(view, [action])
            text = self._diplomacy(action, view)
            if warnings:
                text += "\nWarnings (projected contract payments):\n" + "\n".join(f"  - {w}" for w in warnings)
            return text
        if response == "withdraw":
            return self._diplomacy({"type": "withdraw", "deal": deal})
        raise ToolError("response must be accept, reject, counter or withdraw")

    def say(self, to: str, text: str) -> str:
        return self._diplomacy({"type": "say", "to": to, "text": text})

    def list_deals(self) -> str:
        self._need_game()
        view = self.client.state()
        pid = self.client.player_id
        deals = view.get("deals") or {}
        lines = _deals_lines(deals, view.get("contracts") or [], pid, 50)
        if not (deals.get("open") or []):
            lines.insert(0, "No open deals involving you.")
        lines.append("Reputation (deals | contracts honoured | defaults | treaty betrayals):")
        for p in view.get("players", []):
            rep = p.get("reputation") or {}
            lines.append(f"  {p['id']} {p['name']}{' <- you' if p['id'] == pid else ''}: {rep.get('deals', 0)} | "
                         f"{rep.get('contracts_honoured', 0)} | {rep.get('defaults', 0)} | "
                         f"{rep.get('betrayals', p.get('betrayals', 0))}")
        return "\n".join(line.lstrip("\n") for line in lines)

    def end_round(self, timeout: float = 60) -> str:
        self._need_game()
        timeout = max(0.0, min(float(timeout), 110.0))
        view = self.client.state()
        ph = view.get("phase") if view.get("status") == "running" else None
        if not ph:
            raise ToolError("end_round is for synchronous games; this game is live (use wait_for_inbox and "
                            "submit_orders)." if view.get("status") == "running" else
                            f"The game is {view.get('status')}.")
        if ph.get("id") != self.phase_id:  # a phase this agent has not seen yet: show it instead
            return "A new phase is open.\n\n" + self.get_state()
        if ph.get("kind") != "negotiate":
            return "Negotiation is over for this turn (orders phase): call submit_orders, then wait_for_turn."
        turn0 = view.get("turn")
        seq = max(self.client.inbox_seq, 0)
        if not ph.get("you_done"):
            try:
                self.client.end_round(phase=ph["id"])
            except ApiError as e:
                if e.status != 409:
                    raise
        w = self.client.wait_phase(ph["id"], timeout=timeout)
        if w.get("status") == "running" and (w.get("phase") or {}).get("id") == ph["id"]:
            waiting = ", ".join((w.get("phase") or {}).get("waiting") or []) or "nobody"
            return (f"You ended negotiation round {ph.get('round')}; it is still open (waiting for: {waiting}). "
                    "Call end_round again to keep waiting.")
        view = self.client.state()
        if view.get("status") != "running" or view.get("turn") != turn0:
            return self.get_state()
        lines = [f"Negotiation round {ph.get('round')} of {ph.get('of')} closed."]
        mine = phase_result_lines(view.get("phase") or {}, {ph.get("round")})
        if mine:
            lines += ["Your actions:"] + mine
        box = self.client.inbox(since=seq, timeout=0)
        news = [describe_event(ev, self.client.player_id) for ev in box.get("items") or []]
        if news:
            lines += ["New diplomacy:"] + [f"  {x}" for x in news]
        nxt = view.get("phase") or {}
        self.phase_id = nxt.get("id")
        self.last_turn = max(self.last_turn, view.get("turn", -1))
        if nxt.get("kind") == "negotiate":
            lines.append(f"Now: negotiation round {nxt.get('round')} of {nxt.get('of')}. Queue deals or messages, "
                         "then end_round (list_deals shows everything open).")
        else:
            lines.append("Negotiation is over for this turn: call submit_orders, then wait_for_turn.")
        return "\n".join(lines)

    def _sync_phase(self) -> dict | None:
        """The open phase of a running synchronous game (None: live game or not running)."""
        if self.phase_id is None:
            return None
        view = self.client.state()
        return view.get("phase") if view.get("status") == "running" else None

    def wait_for_inbox(self, timeout: float = 20) -> str:
        self._need_game()
        ph = self._sync_phase()
        if ph is not None:
            if ph.get("id") != self.phase_id:
                return "A new phase is open.\n\n" + self.get_state()
            if ph.get("kind") == "negotiate":
                return self.end_round(max(float(timeout), 60.0))
            return "Diplomacy is closed in the orders phase of this synchronous turn: call submit_orders."
        timeout = max(0.0, min(float(timeout), 110.0))
        box = self.client.inbox(timeout=timeout, turn=self.last_turn if self.last_turn >= 0 else None)
        pid = self.client.player_id
        lines = [describe_event(ev, pid) for ev in box.get("items") or []]
        head = f"Turn {box.get('turn')} ({box.get('status')})"
        if box.get("status") == "running" and box.get("turn") is not None and box["turn"] > self.last_turn >= 0:
            head += " — a NEW TURN has started: call get_state"
        if not lines:
            return head + (": nothing new (timed out). " if box.get("timed_out") else ": nothing new. ") + \
                "Call wait_for_inbox again, or submit_orders."
        return head + ":\n" + "\n".join(f"  {line}" for line in lines) + \
            "\nAnswer with respond_to_deal / propose_deal / say (list_deals shows everything open)."

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

    @staticmethod
    def _agent(agent: dict | None) -> dict | None:
        if agent is not None:
            return agent
        try:
            return agent_from_env()
        except ValueError as e:
            raise ToolError(f"AGENTCIV_AGENT: {e}") from None

    def join_game(self, game_id: str, name: str, agent: dict | None = None) -> str:
        return self._joined(self.client.join(game_id, name, key=self.key, agent=self._agent(agent)))

    def quickmatch(self, name: str, players: int = 6, turn_timeout: float | None = None,
                   lobby_timeout: float | None = None, fog: bool | None = None, agent: dict | None = None,
                   sync: bool | None = None, track: str | None = None) -> str:
        opts = {} if lobby_timeout is None else {"lobby_timeout": lobby_timeout}
        if fog is not None:
            opts["fog"] = fog
        if sync:
            opts["sync"] = True
        if track:
            opts["track"] = track
        return self._joined(self.client.quickmatch(name, players=players, turn_timeout=turn_timeout,
                                                   key=self.key, agent=self._agent(agent), **opts))

    def start_game(self, game_id: str | None = None) -> str:
        res = self.client.start(game_id)
        return f"Game status: {res.get('status')}" + ("" if res.get("started") else " (already started)")

    def get_state(self, full: bool = False, include_map: bool = False) -> str:
        self._need_game()
        view = self.client.state()
        note = ""
        you = view.get("you") or {}
        if (view.get("status") == "running" and you.get("alive", True) and you.get("submitted") is False
                and view["turn"] <= self.submitted_turn):
            # the server restarted from a checkpoint taken before our orders: this turn is played again
            note = (f"NOTE: the server restarted and your orders for turn {view['turn']} were lost; "
                    "submit them again.\n\n")
            self.submitted_turn = self.last_turn = view["turn"] - 1
        if view.get("status") == "running":
            self.last_turn = max(self.last_turn, view["turn"])  # wait_for_turn waits for the next one
            if view.get("phase"):
                self.phase_id = view["phase"].get("id")
        text = note + summarize_view(view, self.client.player_id)
        self.client.inbox_seq = max(self.client.inbox_seq, int(view.get("diplomacy_seq") or 0))
        ph = view.get("phase") if view.get("status") == "running" else None
        if ph and ph.get("kind") == "orders":
            text += (f"\n\nOrders phase: submit orders for turn {view['turn']} with submit_orders and call "
                     "wait_for_turn.")
        elif view.get("status") == "running":
            incoming = [d for d in (view.get("deals") or {}).get("open") or [] if d.get("to") == self.client.player_id]
            if incoming:
                text += (f"\n\n{len(incoming)} deal(s) await your answer: respond_to_deal (accept | reject | "
                         "counter).")
            if ph:
                text += ("\n\nNegotiation round: queue deals or messages (propose_deal, respond_to_deal, say) if "
                         "useful, then call end_round. Orders open after the last round.")
            else:
                text += (f"\n\nBarter now if useful (propose_deal, respond_to_deal, say, wait_for_inbox), then "
                         f"submit orders for turn {view['turn']} with submit_orders and call wait_for_turn.")
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
        self.submitted_turn = res["turn"]
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
        warnings = order_warnings(self.client.state(), orders)
        if warnings:
            lines.append("Warnings (estimates; these orders were accepted but may not work out):")
            lines += [f"  - {w}" for w in warnings]
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
        ph = self._sync_phase()
        if ph is not None:
            timeout = max(0.0, min(float(timeout), 110.0))
            if ph.get("id") != self.phase_id:
                return self.get_state()
            if ph.get("kind") == "negotiate" and not ph.get("you_done"):
                return self.end_round(timeout)
            w = self.client.wait_phase(ph["id"], timeout=timeout)
            if w.get("status") == "running" and (w.get("phase") or {}).get("id") == ph["id"]:
                return (f"Still waiting (turn {w['turn']}, {ph.get('kind')} phase). Call wait_for_turn again.")
            return self.get_state()
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

    def leaderboard(self, mode: str = "standard") -> str:
        rows = self.client.leaderboard(mode)
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
