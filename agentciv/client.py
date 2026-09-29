"""AgentCiv Python SDK (stdlib only).

Connect to a running server (``python -m agentciv.server``) and play::

    from agentciv.client import AgentCivClient

    c = AgentCivClient("http://localhost:8765")
    c.quickmatch("MyAgent")                 # or c.join(game_id, "MyAgent")
    turn = -1
    while True:
        w = c.wait(since_turn=turn)         # long-poll until a new turn starts
        if w["status"] == "finished":
            break
        if w["status"] != "running":
            continue
        view = c.state()                    # your player view (docs/DESIGN.md §10)
        turn = view["turn"]
        c.submit_orders([{"type": "claim", "at": [5, 4]}], turn=turn)
    print(c.state()["victory"]["result"])

Or let :func:`run_bot` drive the loop for any ``view -> orders`` function or a
built-in bot::

    from agentciv.client import run_bot
    run_bot("strategist", "http://localhost:8765", quickmatch=True, name="my-strategist")

Also included: :func:`summarize_view` (compact text summary of a view, handy for
LLM agents) and :func:`ascii_map`.

Command line::

    python -m agentciv.client --url http://localhost:8765 --bot strategist --name MyBot --quickmatch
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

DEFAULT_URL = "http://localhost:8765"
RESOURCES = ("food", "wood", "stone", "gold", "influence")
UNIT_TYPES = ("infantry", "archer", "cavalry", "siege")


class ApiError(Exception):
    """An HTTP error returned by the server. ``status`` is the HTTP status,
    ``body`` the decoded JSON body (e.g. ``{"error": ..., "turn": ...}``)."""

    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.body = body if body is not None else {}


class AgentCivClient:
    """Thin wrapper over the HTTP API (docs/DESIGN.md §12).

    After :meth:`join` or :meth:`quickmatch` the client remembers
    ``game_id``, ``player_id`` and ``token``, so later calls need no arguments.
    """

    def __init__(self, base_url: str = DEFAULT_URL, token: str | None = None,
                 game_id: str | None = None, player_id: str | None = None, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.game_id = game_id
        self.player_id = player_id
        self.timeout = timeout
        self.creator_tokens: dict[str, str] = {}  # game_id -> creator_token of games this client created

    # ------------------------------------------------------------ transport
    def _request(self, method: str, path: str, body: Any = None, query: dict | None = None,
                 auth: bool = False, raw: bool = False, timeout: float | None = None, token: str | None = None):
        url = self.base_url + path
        if query:
            q = {k: v for k, v in query.items() if v is not None}
            if q:
                url += "?" + urllib.parse.urlencode(q)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token or (auth and self.token):
            headers["Authorization"] = f"Bearer {token or self.token}"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(text)
                msg = parsed.get("error", text) if isinstance(parsed, dict) else text
            except ValueError:
                parsed, msg = None, text or e.reason
            raise ApiError(e.code, str(msg), parsed) from None
        if raw:
            return payload.decode("utf-8")
        return json.loads(payload) if payload else None

    def _gid(self, game_id: str | None) -> str:
        gid = game_id or self.game_id
        if not gid:
            raise ValueError("no game_id: pass one or join a game first")
        return urllib.parse.quote(gid, safe="")

    def _remember(self, res: dict) -> dict:
        self.game_id = res["game_id"]
        self.player_id = res["player_id"]
        self.token = res["token"]
        return res

    # ------------------------------------------------------------ lobby
    def create_game(self, **options) -> str:
        """Create a game and return its id. Options (all optional):
        ``name, max_players, min_players, turn_timeout, max_turns, seed, bots,
        fill_with_bots, lobby_timeout, turn_delay, rated``. The returned
        ``creator_token`` is remembered: :meth:`start` uses it."""
        res = self._request("POST", "/api/games", options)
        if res.get("creator_token"):
            self.creator_tokens[res["game_id"]] = res["creator_token"]
        return res["game_id"]

    def list_games(self) -> list[dict]:
        return self._request("GET", "/api/games")

    def game(self, game_id: str | None = None) -> dict:
        """Summary of one game (players, status, settings)."""
        return self._request("GET", f"/api/games/{self._gid(game_id)}")

    def join(self, game_id: str, name: str, key: str | None = None) -> dict:
        """Join a lobby. Returns ``{"game_id","player_id","token"}`` and remembers them.
        ``key`` (8-200 chars, optional) registers ``name`` on first use; a
        registered name can only be played with its key."""
        body = {"name": name}
        if key is not None:
            body["key"] = key
        return self._remember(self._request("POST", f"/api/games/{urllib.parse.quote(game_id, safe='')}/join",
                                            body))

    def quickmatch(self, name: str, players: int = 6, turn_timeout: float | None = None,
                   key: str | None = None, **options) -> dict:
        """Join the open quickmatch lobby for ``players`` seats (creating one if
        needed). The lobby fills with house bots after ``lobby_timeout``
        seconds (default 30). ``key``: see :meth:`join`."""
        body = {"name": name, "players": players, **options}
        if turn_timeout is not None:
            body["turn_timeout"] = turn_timeout
        if key is not None:
            body["key"] = key
        return self._remember(self._request("POST", "/api/quickmatch", body))

    def start(self, game_id: str | None = None, token: str | None = None) -> dict:
        """Start a lobby now. Once remote players are seated this needs a
        seated player's token (used automatically after :meth:`join`) or the
        creator token (remembered by :meth:`create_game`)."""
        gid = game_id or self.game_id
        tok = token or self.creator_tokens.get(gid or "") or (self.token if gid == self.game_id else None)
        return self._request("POST", f"/api/games/{self._gid(game_id)}/start", {}, token=tok)

    # ------------------------------------------------------------ playing
    def state(self, game_id: str | None = None, spectator: bool = False) -> dict:
        """Your player view (or the spectator view if not joined / ``spectator=True``)."""
        return self._request("GET", f"/api/games/{self._gid(game_id)}/state", auth=not spectator)

    def submit_orders(self, orders: list, turn: int | None = None, game_id: str | None = None,
                      ready: bool = True) -> dict:
        """Submit (or replace) your orders for ``turn``. Returns
        ``{"accepted","errors","turn","deadline","ready"}`` where each error is
        ``{"index","error"}`` plus an ``example`` of the order's correct shape;
        raises ApiError(409) if ``turn`` is stale. ``ready=False`` submits a
        draft: the turn won't resolve early on your account until you resubmit
        with ``ready=True`` (the deadline still applies)."""
        body: dict = {"orders": orders}
        if turn is not None:
            body["turn"] = turn
        if not ready:
            body["ready"] = False
        return self._request("POST", f"/api/games/{self._gid(game_id)}/orders", body, auth=True)

    def wait(self, since_turn: int | None = None, timeout: float = 30.0, game_id: str | None = None) -> dict:
        """Long-poll until ``turn > since_turn`` (or the game finishes / timeout).
        Returns ``{"turn","status","deadline","timed_out"}``."""
        return self._request("GET", f"/api/games/{self._gid(game_id)}/wait",
                             query={"since_turn": since_turn, "timeout": timeout}, timeout=timeout + 15)

    # ------------------------------------------------------------ info
    def rules(self) -> str:
        """The rules guide (markdown)."""
        return self._request("GET", "/api/rules", raw=True)

    def rules_json(self) -> dict:
        return self._request("GET", "/api/rules.json")

    def leaderboard(self) -> list[dict]:
        return self._request("GET", "/api/leaderboard")

    def bots(self) -> list[str]:
        return self._request("GET", "/api/bots")

    def replay(self, game_id: str | None = None) -> dict:
        return self._request("GET", f"/api/games/{self._gid(game_id)}/replay", timeout=120)


# ====================================================================== run_bot
def _as_act(bot_or_callable, seed: int = 0) -> Callable[[dict], list]:
    if isinstance(bot_or_callable, str):
        from .bots import get_bot
        bot_or_callable = get_bot(bot_or_callable, seed)
    if hasattr(bot_or_callable, "act"):
        return bot_or_callable.act
    if callable(bot_or_callable):
        return bot_or_callable
    raise TypeError("expected a Bot, a callable view -> orders, or a built-in bot name")


def run_bot(bot_or_callable, base_url: str = DEFAULT_URL, game_id: str | None = None,
            name: str | None = None, quickmatch: bool = False, players: int = 6,
            turn_timeout: float | None = None, client: AgentCivClient | None = None,
            verbose: bool = False, seed: int = 0, key: str | None = None) -> dict:
    """Play one game remotely and return a result dict.

    ``bot_or_callable`` is a :class:`agentciv.bots.base.Bot`, any callable
    ``view -> list[order]``, or a built-in bot name. Either join ``game_id``,
    or use ``quickmatch=True`` (``players`` seats), or pass an already-joined
    ``client``. Loops wait → state → act → submit until the game finishes.
    ``key`` registers/proves ownership of ``name`` (see :meth:`AgentCivClient.join`).

    Returns ``{"game_id","player_id","name","result","place","won","turns"}``.
    """
    act = _as_act(bot_or_callable, seed)
    if name is None:
        name = (bot_or_callable if isinstance(bot_or_callable, str)
                else getattr(bot_or_callable, "name", "bot")) + "-remote"
    c = client or AgentCivClient(base_url)
    if c.token is None:
        if game_id:
            c.join(game_id, name, key=key)
        elif quickmatch:
            c.quickmatch(name, players=players, turn_timeout=turn_timeout, key=key)
        else:
            raise ValueError("pass game_id=..., quickmatch=True, or a joined client")
    say = (lambda *a: print(*a, file=sys.stderr, flush=True)) if verbose else (lambda *a: None)
    say(f"[{name}] joined {c.game_id} as {c.player_id}")

    last = -1
    failures = 0
    view: dict | None = None
    while True:
        try:
            w = c.wait(since_turn=last, timeout=30)
            if w["status"] == "lobby":
                continue
            view = c.state()
            failures = 0
        except (ApiError, OSError) as e:  # transient network trouble: back off and retry
            if isinstance(e, ApiError) and e.status in (401, 403, 404):
                raise
            failures += 1
            if failures > 20:
                raise
            time.sleep(min(5.0, 0.2 * failures))
            continue
        if view["status"] == "finished":
            break
        turn = view["turn"]
        if turn <= last:
            continue
        last = turn
        you = view.get("you") or {}
        if not you.get("alive", True):
            continue  # eliminated: just follow the game to the end
        try:
            orders = act(view)
        except Exception as e:
            say(f"[{name}] bot error on turn {turn}: {type(e).__name__}: {e}")
            orders = []
        try:
            res = c.submit_orders(orders or [], turn=turn)
            if res.get("errors"):
                say(f"[{name}] turn {turn}: {len(res['errors'])} order errors, e.g. {res['errors'][0]}")
        except ApiError as e:
            if e.status != 409:  # 409: the turn already resolved; just move on
                say(f"[{name}] submit failed: {e}")
        except OSError as e:
            say(f"[{name}] submit failed: {e}")

    result = (view or {}).get("victory", {}).get("result") or {}
    places = result.get("placements") or []
    place = places.index(c.player_id) + 1 if c.player_id in places else None
    out = {"game_id": c.game_id, "player_id": c.player_id, "name": name, "result": result,
           "place": place, "won": result.get("winner") == c.player_id, "turns": (view or {}).get("turn")}
    say(f"[{name}] game over: place {place}/{len(places)}, winner {result.get('winner')} "
        f"by {result.get('condition')}")
    return out


# ====================================================================== text helpers
_OWNER_SYMBOLS = "123456789abc"


def _owner_symbol(pid: str | None) -> str:
    if not pid:
        return " "
    try:
        return _OWNER_SYMBOLS[int(pid[1:]) - 1]
    except (ValueError, IndexError):
        return "?"


def _units_str(units: dict) -> str:
    return " ".join(f"{n} {u}" for u, n in units.items() if n) or "none"


def _power(units: dict, rules: dict | None = None) -> int:
    strength = {"infantry": 10, "archer": 8, "cavalry": 12, "siege": 4}
    if rules:
        for u, spec in (rules.get("units") or {}).items():
            if isinstance(spec, dict) and "strength" in spec:
                strength[u] = spec["strength"]
    return sum(n * strength.get(u, 0) for u, n in units.items())


def ascii_map(view: dict, pid: str | None = None) -> str:
    """Render the map as text, 2 characters per tile.

    1st char: terrain (``.`` plains, ``f`` forest, ``h`` hills, ``g`` gold,
    ``m`` mountain, ``~`` water), replaced by ``@`` original capital,
    ``C`` other city, ``*`` relic; UPPERCASE terrain letter (``P F H G``)
    = units present (see the army list). 2nd char: owner (``1``–``9``,
    ``a``–``c`` for p1–p12) or blank if unowned.
    """
    m = view.get("map") or {}
    w, h = m.get("width", 0), m.get("height", 0)
    if not w or not h:
        return "(no map yet: the game has not started)"
    terrain = m["terrain"]
    owner = m["owner"]
    cities = {(c["x"], c["y"]): c for c in view.get("cities", [])}
    relics = {(r["x"], r["y"]) for r in m.get("relics", [])}
    armies = {(a["x"], a["y"]) for a in view.get("armies", []) if any(a["units"].values())}
    upper = {".": "P", "f": "F", "h": "H", "g": "G"}
    lines = ["    " + "".join(f"{x // 10 if x >= 10 else ' '} " for x in range(w)),
             "    " + "".join(f"{x % 10} " for x in range(w))]
    for y in range(h):
        row = []
        for x in range(w):
            t = terrain[y][x]
            if (x, y) in cities:
                ch = "@" if cities[(x, y)].get("capital") else "C"
            elif (x, y) in relics:
                ch = "*"
            elif (x, y) in armies:
                ch = upper.get(t, t.upper())
            else:
                ch = t
            row.append(ch + _owner_symbol(owner[y][x]))
        lines.append(f"{y:>3} " + "".join(row))
    names = {p["id"]: p["name"] for p in view.get("players", [])}
    you = pid or (view.get("you") or {}).get("id")
    legend = ", ".join(f"{_owner_symbol(p)}={p}{' (you)' if p == you else ''} {names[p]}" for p in names)
    lines.append("")
    lines.append("Legend: . plains  f forest  h hills  g gold  m mountain  ~ water  @ capital  C city  "
                 "* relic  UPPERCASE = units present. 2nd char = owner: " + legend)
    return "\n".join(lines)


def summarize_view(view: dict, pid: str | None = None, max_events: int = 12, max_messages: int = 6) -> str:
    """A compact, LLM-friendly text summary of a view: your economy, cities,
    armies, nearby threats, diplomacy, market, victory progress and a
    leaderboard of all players."""
    you = view.get("you") or {}
    pid = pid or you.get("id")
    players = {p["id"]: p for p in view.get("players", [])}
    me = players.get(pid, {})
    rules = view.get("costs") or {}
    out: list[str] = []
    season = view.get("season") or {}
    mods = season.get("modifiers") or {}
    mod_txt = ", ".join(f"{r} x{v:g}" for r, v in mods.items() if v != 1.0) or "no modifiers"
    out.append(f"Game {view.get('game_id')} \"{view.get('name') or ''}\" — turn {view.get('turn')}/"
               f"{view.get('max_turns')} ({view.get('status')}). Season {season.get('name')} ({mod_txt}; "
               f"{season.get('turns_left')} turn(s) left, next {season.get('next')}).")
    if view.get("deadline"):
        out.append(f"Turn deadline in {max(0.0, view['deadline'] - time.time()):.0f}s.")

    if pid and me:
        res = you.get("resources") or me.get("resources", {})
        inc = you.get("income") or me.get("income", {})
        caps = you.get("caps") or {}
        parts = []
        for r in RESOURCES:
            cap = f"/{caps[r]}" if r in caps else ""
            parts.append(f"{r} {res.get(r, 0)}{cap} ({inc.get(r, 0):+d})")
        out.append(f"\nYOU: {pid} {me.get('name')} — {'alive' if me.get('alive', True) else 'ELIMINATED'}, "
                   f"score {me.get('score')}, {me.get('cities')} cities, {me.get('tiles')} tiles, "
                   f"military power {me.get('military_power')}.")
        out.append("Resources (income/turn, before upkeep): " + ", ".join(parts))
        if you:
            sc = you.get("settle_cost") or {}
            out.append(f"Upkeep {you.get('upkeep', me.get('upkeep', 0))} food/turn. Claim costs "
                       f"{you.get('claim_cost')} influence. Settle costs "
                       f"{', '.join(f'{v} {k}' for k, v in sc.items())}. Market fee "
                       f"{(you.get('market_fee') or 0) * 100:.0f}%. Orders submitted this turn: "
                       f"{'yes' if you.get('submitted') else 'no'}.")

        my_cities = [c for c in view.get("cities", []) if c["owner"] == pid]
        out.append(f"\nYour cities ({len(my_cities)}):")
        for c in my_cities:
            b = c.get("buildings", {})
            out.append(f"  {c['name']} at [{c['x']},{c['y']}]{' (original capital)' if c.get('capital') else ''}: "
                       f"walls {b.get('walls', 0)}, warehouse {b.get('warehouse', 0)}, market_hall "
                       f"{b.get('market_hall', 0)}, wonder stage {c.get('wonder_stage', 0)}")
        my_armies = [a for a in view.get("armies", []) if a["owner"] == pid]
        out.append(f"Your armies ({len(my_armies)}): " + ("; ".join(
            f"[{a['x']},{a['y']}] {_units_str(a['units'])}" for a in my_armies) or "none"))
        m = view.get("map") or {}
        if m.get("width"):
            imps: dict[str, int] = {}
            owner = m["owner"]
            for imp in m.get("improvements", []):
                if owner[imp["y"]][imp["x"]] == pid:
                    imps[imp["building"]] = imps.get(imp["building"], 0) + 1
            out.append("Your improvements: " + (", ".join(f"{k} {v}" for k, v in sorted(imps.items())) or "none"))

        # threats: hostile armies within 3 tiles of your cities
        partners = {t["b"] if t["a"] == pid else t["a"] for t in view.get("treaties", [])
                    if pid in (t["a"], t["b"])}
        threats = []
        for a in view.get("armies", []):
            if a["owner"] == pid:
                continue
            near = [(max(abs(a["x"] - c["x"]), abs(a["y"] - c["y"])), c) for c in my_cities]
            near = [(d, c) for d, c in near if d <= 3]
            if near:
                d, c = min(near, key=lambda t: t[0])
                tag = " (treaty partner)" if a["owner"] in partners else ""
                threats.append(f"  {a['owner']}{tag} at [{a['x']},{a['y']}]: {_units_str(a['units'])} "
                               f"(power {_power(a['units'], rules)}), {d} tile(s) from {c['name']}")
        out.append("Threats near your cities: " + ("\n" + "\n".join(threats) if threats else "none"))

    # diplomacy
    treaties = view.get("treaties", [])
    if treaties:
        out.append("\nTreaties: " + "; ".join(f"{t['a']}–{t['b']} until turn {t['until_turn']}" for t in treaties))
    props = [p for p in view.get("treaty_proposals", []) if p.get("to") == pid]
    for p in props:
        out.append(f"Treaty proposal from {p['from']} ({p['turns']} turns) — accept THIS turn with "
                   f"{{\"type\":\"accept_treaty\",\"from\":\"{p['from']}\"}}")
    for o in view.get("trade_offers", []):
        direction = "to you" if o["to"] == pid else ("from you" if o["from"] == pid else "")
        out.append(f"Trade offer {o['id']} {o['from']}→{o['to']} {direction}: gives "
                   f"{_units_str(o['give'])} for {_units_str(o['want'])} (expires after turn {o['expires_turn']})")

    mk = view.get("market") or {}
    if mk.get("prices"):
        out.append("\nMarket prices (gold per unit): " + ", ".join(f"{r} {p:.2f}" for r, p in mk["prices"].items())
                   + f"; fee {mk.get('fee', 0) * 100:.0f}% (2% with a market_hall).")

    vic = view.get("victory") or {}
    thr = vic.get("thresholds") or {}
    if thr:
        out.append(f"\nVictory thresholds: conquest {thr.get('conquest_capitals')} original capitals, wonder stage "
                   f"{thr.get('wonder_stage')}, influence {thr.get('influence')}, relics "
                   f"{thr.get('relics_needed')}/{thr.get('relics_total')} held for {thr.get('relic_turns')} turns, "
                   f"economic {thr.get('economic_gold')} gold, else best score at turn {thr.get('max_turns')}.")
    out.append("Players (score | cities tiles | power | gold infl | wonder relics | best victory progress):")
    for p in sorted(players.values(), key=lambda p: -p.get("score", 0)):
        vp = {k: v for k, v in (p.get("victory_progress") or {}).items() if k != "score"}
        best = max(vp.items(), key=lambda kv: kv[1]) if vp else ("-", 0)
        flag = "" if p.get("alive", True) else " [eliminated]"
        me_tag = " <- you" if p["id"] == pid else ""
        r = p.get("resources", {})
        out.append(f"  {p['id']} {p['name']}{flag}{me_tag}: {p.get('score')} | {p.get('cities')} {p.get('tiles')} | "
                   f"{p.get('military_power')} | {r.get('gold', 0)} {r.get('influence', 0)} | "
                   f"{p.get('wonder_stage', 0)} {p.get('relics_held', 0)} | {best[0]} {best[1] * 100:.0f}%"
                   + (f" | betrayals {p['betrayals']}" if p.get("betrayals") else ""))
    relics = (view.get("map") or {}).get("relics", [])
    if relics:
        out.append("Relics: " + ", ".join(f"[{r['x']},{r['y']}] {r['owner'] or 'unowned'}" for r in relics))

    events = view.get("events", [])
    if events:
        mine = [e for e in events if e.get("type") == "order_failed" and e.get("player") == pid]
        others = [e for e in events if e not in mine and e.get("type") in (
            "battle", "city_captured", "city_founded", "eliminated", "treaty_signed", "treaty_broken",
            "wonder_stage", "starvation", "trade_executed", "victory", "tile_captured")]
        if mine:
            out.append("\nYour failed orders last turn:")
            out += [f"  #{e.get('index')} {e.get('order_type')}: {e.get('reason')}" for e in mine[:max_events]]
        if others:
            out.append("Notable events last turn:")
            for e in others[:max_events]:
                fields = {k: v for k, v in e.items() if k not in ("turn", "type", "powers", "losses")}
                out.append(f"  {e['type']}: " + json.dumps(fields, separators=(",", ":"))[:200])
    msgs = view.get("messages", [])[-max_messages:]
    if msgs:
        out.append("\nRecent messages:")
        out += [f"  t{m['turn']} {m['from']}→{m['to']}: {m['text'][:200]}" for m in msgs]
    res = vic.get("result")
    if res:
        out.append(f"\nGAME OVER: winner {res.get('winner')} by {res.get('condition')} on turn {res.get('turn')}; "
                   f"placements {res.get('placements')}")
    return "\n".join(out)


# ====================================================================== CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agentciv.client",
                                     description="Run a built-in AgentCiv bot against a remote server.")
    parser.add_argument("--url", default=DEFAULT_URL, help="server URL (default %(default)s)")
    parser.add_argument("--bot", default="strategist", help="built-in bot name (see GET /api/bots)")
    parser.add_argument("--name", default=None, help="player name (default '<bot>-remote')")
    parser.add_argument("--game", default=None, help="game id to join")
    parser.add_argument("--quickmatch", action="store_true", help="join/create a quickmatch lobby")
    parser.add_argument("--players", type=int, default=6, help="quickmatch lobby size (default 6)")
    parser.add_argument("--turn-timeout", type=float, default=None, help="quickmatch turn timeout (seconds)")
    parser.add_argument("--seed", type=int, default=0, help="bot seed")
    parser.add_argument("--key", default=os.environ.get("AGENTCIV_KEY") or None,
                        help="secret key for your name: registers it on first use, then only this key can play "
                             "under it (default $AGENTCIV_KEY)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    if not args.game and not args.quickmatch:
        parser.error("pass --game ID or --quickmatch")
    try:
        res = run_bot(args.bot, args.url, game_id=args.game, name=args.name, quickmatch=args.quickmatch,
                      players=args.players, turn_timeout=args.turn_timeout, verbose=not args.quiet,
                      seed=args.seed, key=args.key)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
