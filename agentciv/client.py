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

Barter live with other players during a turn (docs/DESIGN.md §13)::

    r = c.propose("p2", give={"wood": 60}, get={"gold": 45}, message="surplus wood")
    box = c.inbox(timeout=20)               # long-poll: proposals, counters, acceptances, messages
    for ev in box["items"]:
        if ev["type"] == "deal_countered" and ev["to"] == c.player_id:
            c.accept(ev["new"]["id"])       # or c.counter(...), c.reject(...)

A bot passed to :func:`run_bot` may define ``negotiate(view) -> list[action]``:
it is called at the start of every turn and whenever something arrives in
your inbox, until shortly before the deadline.

Also included: :func:`summarize_view` (compact text summary of a view, handy for
LLM agents), :func:`ascii_map`, :func:`bundle_str` and :func:`describe_event`.

Command line::

    python -m agentciv.client --url http://localhost:8765 --bot strategist --name MyBot --quickmatch
"""
from __future__ import annotations

import argparse
import json
import math
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
        self.inbox_seq = 0  # diplomacy_seq seen by the last inbox() call (the default ``since`` of the next)

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
        self.inbox_seq = 0
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

    # ------------------------------------------------------------ barter (§13)
    def diplomacy(self, actions, turn: int | None = None, game_id: str | None = None) -> dict:
        """Send diplomacy actions (``propose``, ``counter``, ``accept``,
        ``reject``, ``withdraw``, ``say``); they apply immediately. ``actions``
        is a list (or one action dict). With ``turn``, a stale turn raises
        ApiError(409). Returns ``{"results": [{"index","ok","deal"?,"error"?}],
        "ok", "seq", "turn", "deadline"}``."""
        if isinstance(actions, dict):
            actions = [actions]
        body: dict = {"actions": list(actions)}
        if turn is not None:
            body["turn"] = turn
        return self._request("POST", f"/api/games/{self._gid(game_id)}/diplomacy", body, auth=True)

    def _one(self, action: dict) -> dict:
        res = self.diplomacy([action])
        out = dict(res["results"][0]) if res.get("results") else {"ok": False, "error": "no result"}
        out.setdefault("seq", res.get("seq"))
        return out

    @staticmethod
    def _terms(action: dict, give, get, peace, message, expires_in) -> dict:
        action["give"] = give or {}
        action["get"] = get or {}
        if peace:
            action["peace"] = peace
        if message:
            action["message"] = message
        if expires_in is not None:
            action["expires_in"] = expires_in
        return action

    def propose(self, to: str, give: dict | None = None, get: dict | None = None, peace: int | None = None,
                message: str | None = None, expires_in: int | None = None) -> dict:
        """Propose a deal to ``to``: you hand over ``give`` and receive ``get``.
        A bundle holds resources (``{"wood": 60}``), ``"tiles": [[x, y]]`` and/or
        a contract ``"per_turn": {"gold": 5}, "turns": 10``; ``peace`` = k
        turns of peace on acceptance. Returns ``{"index","ok","deal"|"error","seq"}``."""
        return self._one(self._terms({"type": "propose", "to": to}, give, get, peace, message, expires_in))

    def counter(self, deal: str, give: dict | None = None, get: dict | None = None, peace: int | None = None,
                message: str | None = None, expires_in: int | None = None) -> dict:
        """Counter a deal proposed to you; ``give``/``get`` are from YOUR point
        of view. The old deal closes; the result's ``deal`` is the new one."""
        return self._one(self._terms({"type": "counter", "deal": deal}, give, get, peace, message, expires_in))

    def accept(self, deal: str) -> dict:
        """Accept a deal proposed to you: it settles at once (``status``
        ``accepted``) or fails (``ok`` false, ``status`` ``failed``) if either
        side can't deliver right now."""
        return self._one({"type": "accept", "deal": deal})

    def reject(self, deal: str, message: str | None = None) -> dict:
        a = {"type": "reject", "deal": deal}
        if message:
            a["message"] = message
        return self._one(a)

    def withdraw(self, deal: str) -> dict:
        """Withdraw one of your own open proposals."""
        return self._one({"type": "withdraw", "deal": deal})

    def say(self, to: str, text: str) -> dict:
        """Send a message to a player (private) or to ``"all"`` (public)."""
        return self._one({"type": "say", "to": to, "text": text})

    def inbox(self, since: int | None = None, timeout: float = 30.0, turn: int | None = None,
              game_id: str | None = None) -> dict:
        """Long-poll for diplomacy addressed to / visible to you with ``seq >
        since`` (default: where the previous call left off). Returns
        ``{"seq","items","turn","status","deadline","timed_out"}`` as soon as
        there are items, the turn or status changes, or ``timeout`` passes.
        ``turn``: the turn you are playing — returns at once if the game has
        moved on (otherwise a turn that ended just before the call is missed)."""
        if since is None:
            since = self.inbox_seq
        res = self._request("GET", f"/api/games/{self._gid(game_id)}/inbox",
                            query={"since": since, "timeout": timeout, "turn": turn}, auth=True,
                            timeout=timeout + 15)
        if game_id in (None, self.game_id):
            self.inbox_seq = max(self.inbox_seq, int(res.get("seq") or 0))
        return res

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
def _as_bot(bot_or_callable, seed: int = 0):
    if isinstance(bot_or_callable, str):
        from .bots import get_bot
        return get_bot(bot_or_callable, seed)
    return bot_or_callable


def _as_act(bot_or_callable, seed: int = 0) -> Callable[[dict], list]:
    bot_or_callable = _as_bot(bot_or_callable, seed)
    if hasattr(bot_or_callable, "act"):
        return bot_or_callable.act
    if callable(bot_or_callable):
        return bot_or_callable
    raise TypeError("expected a Bot, a callable view -> orders, or a built-in bot name")


def _negotiate_fn(bot) -> Callable[[dict], list] | None:
    """``bot.negotiate`` unless missing or the do-nothing default of Bot."""
    fn = getattr(bot, "negotiate", None)
    if not callable(fn) or isinstance(bot, type):
        return None
    try:
        from .bots.base import Bot
        base = getattr(Bot, "negotiate", None)
    except Exception:  # pragma: no cover - the bots package is optional for the SDK
        base = None
    if base is not None and getattr(type(bot), "negotiate", None) is base:
        return None
    return fn


def _involved(view: dict, pid: str) -> list:
    return [d for d in ((view.get("deals") or {}).get("open") or []) if pid in (d.get("from"), d.get("to"))]


def run_bot(bot_or_callable, base_url: str = DEFAULT_URL, game_id: str | None = None,
            name: str | None = None, quickmatch: bool = False, players: int = 6,
            turn_timeout: float | None = None, client: AgentCivClient | None = None,
            verbose: bool = False, seed: int = 0, key: str | None = None,
            negotiate: Callable[[dict], list] | None = None, negotiate_window: float = 2.0,
            deadline_margin: float = 1.0) -> dict:
    """Play one game remotely and return a result dict.

    ``bot_or_callable`` is a :class:`agentciv.bots.base.Bot`, any callable
    ``view -> list[order]``, or a built-in bot name. Either join ``game_id``,
    or use ``quickmatch=True`` (``players`` seats), or pass an already-joined
    ``client``. Loops wait → state → act → submit until the game finishes.
    ``key`` registers/proves ownership of ``name`` (see :meth:`AgentCivClient.join`).

    **Bartering.** If the bot has ``negotiate(view) -> list[action]`` (or
    ``negotiate=`` is given), every turn it is called on a fresh view before
    ``act``; its actions go out through ``/diplomacy`` at once. While it has
    open deals it keeps answering inbox events for up to ``negotiate_window``
    seconds before acting; after submitting orders it keeps polling the inbox
    (negotiating on every new item, and re-running ``act`` when one of its
    deals executed) until the turn ends or ``deadline_margin`` seconds before
    the deadline.

    Returns ``{"game_id","player_id","name","result","place","won","turns"}``.
    """
    bot = _as_bot(bot_or_callable, seed)
    act = _as_act(bot)
    negotiate = negotiate or _negotiate_fn(bot)
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

    def run_negotiate(view: dict) -> bool:
        """negotiate + send; True if one of our own ``accept``s executed a deal
        (the inbox never shows our own actions, so this is how we learn it)."""
        try:
            actions = negotiate(view) or []
            if isinstance(actions, dict):
                actions = [actions]
        except Exception as e:
            say(f"[{name}] negotiate error on turn {view.get('turn')}: {type(e).__name__}: {e}")
            return False
        if not actions:
            return False
        try:
            res = c.diplomacy(actions, turn=view["turn"])
        except (ApiError, OSError) as e:
            say(f"[{name}] diplomacy failed: {e}")
            return False
        results = res.get("results", [])
        bad = [r for r in results if not r.get("ok")]
        if bad:
            say(f"[{name}] turn {view['turn']}: {len(bad)} diplomacy errors, e.g. {bad[0]}")
        return any(r.get("ok") and r.get("status") == "accepted" for r in results)

    def submit(orders, turn: int) -> None:
        try:
            res = c.submit_orders(orders or [], turn=turn)
            if res.get("errors"):
                say(f"[{name}] turn {turn}: {len(res['errors'])} order errors, e.g. {res['errors'][0]}")
        except ApiError as e:
            if e.status != 409:  # 409: the turn already resolved; just move on
                say(f"[{name}] submit failed: {e}")
        except OSError as e:
            say(f"[{name}] submit failed: {e}")

    def act_safe(view: dict) -> list:
        try:
            return act(view)
        except Exception as e:
            say(f"[{name}] bot error on turn {view.get('turn')}: {type(e).__name__}: {e}")
            return []

    def poll(turn: int, until: float, before_orders: bool) -> bool:
        """Answer inbox events (negotiate on a fresh view) until the turn
        changes or ``until``; before the orders are in, also stop once we have
        no open deals. After the orders are in, returns True as soon as one of
        our deals executed, including one we just accepted ourselves (the
        orders should be recomputed)."""
        while True:
            left = until - time.time()
            if left <= 0:
                return False
            if before_orders:
                try:
                    if not _involved(c.state(), c.player_id):
                        return False
                except (ApiError, OSError):
                    return False
            try:
                box = c.inbox(timeout=min(left, 10.0), turn=turn)
            except (ApiError, OSError):
                return False
            if box.get("status") != "running" or box.get("turn") != turn:
                return False
            items = box.get("items") or []
            if not items:
                continue
            executed = any(e.get("type") == "deal_executed" and c.player_id in (e.get("from"), e.get("to"))
                           for e in items)
            try:
                view = c.state()
            except (ApiError, OSError):
                return False
            if view.get("turn") != turn or view.get("status") != "running":
                return False
            if run_negotiate(view):
                executed = True
            if executed and not before_orders:
                return True

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
        if negotiate is None:
            submit(act_safe(view), turn)
            continue
        # --- bartering turn: negotiate, answer for a moment, act, keep answering until the turn ends
        deadline = view.get("deadline")
        now = time.time()
        end = (deadline - deadline_margin) if deadline else math.inf
        c.inbox_seq = max(c.inbox_seq, int(view.get("diplomacy_seq") or 0))
        run_negotiate(view)
        window = min(now + negotiate_window, now + max(0.0, (end - now) * 0.4)) if deadline else now + negotiate_window
        poll(turn, window, before_orders=True)
        try:
            view = c.state()
        except (ApiError, OSError):
            pass
        if view.get("turn") != turn or view.get("status") != "running":
            continue
        submit(act_safe(view), turn)
        while poll(turn, end, before_orders=False):  # one of our deals executed: recompute orders
            try:
                view = c.state()
            except (ApiError, OSError):
                break
            if view.get("turn") != turn or view.get("status") != "running":
                break
            submit(act_safe(view), turn)

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


def _reputation_str(p: dict) -> str:
    rep = p.get("reputation")
    if not isinstance(rep, dict):
        return f" | betrayals {p['betrayals']}" if p.get("betrayals") else ""
    return (f" | deals {rep.get('deals', 0)}, honoured {rep.get('contracts_honoured', 0)}, "
            f"defaults {rep.get('defaults', 0)}, betrayals {rep.get('betrayals', 0)}")


def _units_str(units: dict) -> str:
    return " ".join(f"{n} {u}" for u, n in units.items() if n) or "none"


def seasonal_income(view: dict, pid: str | None = None) -> dict:
    """This turn's income (the engine already applies the current season's
    modifier to ``income`` in the view), before upkeep."""
    pid = pid or (view.get("you") or {}).get("id")
    you = view.get("you") or {}
    inc = you.get("income") or next((p.get("income", {}) for p in view.get("players", []) if p["id"] == pid), {})
    return dict(inc)


def _next_season_food_mod(view: dict) -> tuple[str | None, float]:
    season = view.get("season") or {}
    for s in ((view.get("costs") or {}).get("seasons") or {}).get("cycle", []):
        if s.get("name") == season.get("next"):
            return s["name"], s.get("modifiers", {}).get("food", 1.0)
    return None, 1.0


def _food_outlook(view: dict, pid: str, food: int, upkeep: int, extra_upkeep: int = 0) -> list[str]:
    """Food balance at the end of this turn and a starvation warning (§ upkeep)."""
    you = view.get("you") or {}
    gain = seasonal_income(view, pid).get("food", 0)
    upkeep += extra_upkeep
    end = food + gain - upkeep
    lines = [f"Food this turn: {food} + {gain} income - {upkeep} upkeep = {end} at turn end."]
    if end < 0:
        lines.append(f"WARNING: food runs out this turn; about {math.ceil(-end / 2)} unit(s) will starve "
                     "(highest-upkeep units first) unless you add food or disband units.")
    season = view.get("season") or {}
    nxt, mod = _next_season_food_mod(view)
    cur_mod = ((season.get("modifiers") or {}).get("food") or 1.0)
    base_food = gain / cur_mod  # income in the view is already seasoned
    next_net = int(math.floor(base_food * mod + 1e-9)) - upkeep
    if nxt and next_net < 0:
        at_switch = end + max(0, (season.get("turns_left") or 1) - 1) * (gain - upkeep)
        turns = max(at_switch, 0) // -next_net
        lasts = f"stored food lasts about {turns} turn(s) of {nxt}" if turns else "units start starving at once"
        lines.append(f"Next season ({nxt}, in {season.get('turns_left')} turn(s)) food income becomes "
                     f"{next_net + upkeep}/turn against {upkeep} upkeep: {next_net:+d}/turn, so {lasts}.")
    return lines


def _my_last_turn_problems(view: dict, pid: str, max_events: int) -> list[str]:
    mine = [e for e in view.get("events", []) if e.get("player") == pid
            and e.get("type") in ("order_failed", "starvation")]
    if not mine:
        return []
    out = ["LAST TURN, THESE FAILED OR HURT YOU:"]
    for e in mine[:max_events]:
        if e["type"] == "starvation":
            out.append(f"  starvation: food short by {e.get('deficit')}, lost {_units_str(e.get('lost') or {})}")
        else:
            out.append(f"  order #{e.get('index')} ({e.get('order_type')}) failed: {e.get('reason')}")
    return out


def _progress_str(p: dict, thr: dict) -> str:
    vp = p.get("victory_progress") or {}
    rt = thr.get("relic_turns") or 16
    return (f"capitals {p.get('capitals_held', 0)}/{thr.get('conquest_capitals', '?')}, "
            f"wonder {p.get('wonder_stage', 0)}/{thr.get('wonder_stage', 5)}, "
            f"relics {p.get('relics_held', 0)} held {p.get('relics_guarded', 0)} guarded "
            f"(need {thr.get('relics_needed', '?')}) streak {p.get('relic_streak', 0)}/{rt}, "
            f"influence {vp.get('influence', 0) * 100:.0f}%, economic {vp.get('economic', 0) * 100:.0f}%")


def _relic_lines(view: dict, players: dict, rules: dict) -> list[str]:
    relics = (view.get("map") or {}).get("relics", [])
    if not relics:
        return []
    on_tile: dict = {}
    for a in view.get("armies", []):
        on_tile.setdefault((a["x"], a["y"]), []).append(a)
    out = ["Relics (units standing on each tile are public):"]
    for r in relics:
        stacks = on_tile.get((r["x"], r["y"]), [])
        units = "; ".join(f"{a['owner']} {_units_str(a['units'])} (power {_power(a['units'], rules)})"
                          for a in stacks if any(a["units"].values())) or "no units"
        owner = r.get("owner")
        streak = f", owner's streak {players[owner].get('relic_streak', 0)}" if owner in players else ""
        out.append(f"  [{r['x']},{r['y']}] owner {owner or 'none'}, "
                   f"{'guarded' if r.get('guarded') else 'unguarded'}: {units}{streak}")
    return out


def _auction_price(res: float, gold: float, net: int) -> float:
    """Average price of ``net`` units bought (>0) or sold (<0) against a pool (engine/market.py)."""
    if net == 0 or res <= 0:
        return gold / res if res > 0 else math.inf
    k = res * gold
    if net > 0:
        return (k / (res - net) - gold) / net if net < res else math.inf
    return (gold - k / (res - net)) / -net


def _pending_gives(view: dict, pid: str) -> tuple[dict, list]:
    """Resources you'd hand over at once if the other side accepted your open offers now."""
    total: dict = {}
    ids = []
    for d in (view.get("deals") or {}).get("open") or []:
        if d.get("from") != pid:
            continue
        give = {r: v for r, v in (d.get("give") or {}).items() if r in RESOURCES and isinstance(v, (int, float)) and v}
        if give:
            ids.append(d["id"])
            for r, v in give.items():
                total[r] = total.get(r, 0) + v
    return total, ids


def order_warnings(view: dict, orders: list) -> list[str]:
    """Estimated problems with an order list that pre-validation cannot see:
    market orders that will likely fail (not enough gold when that resource
    clears — the market clears food, then wood, then stone, and a sale only
    funds buys of resources cleared after it — or a price limit the estimated
    price misses); recruit/settle orders the resources on hand after the market
    won't cover; food running out once new units' upkeep is added; and open
    offers of yours whose acceptance would take resources before your orders
    run. Estimates use current pool prices and your orders alone."""
    you = view.get("you") or {}
    pid = you.get("id")
    if not pid or not isinstance(orders, list):
        return []
    rules = view.get("costs") or {}
    res = dict(you.get("resources") or {})
    fee = you.get("market_fee") or 0.05
    pools = (view.get("market") or {}).get("pools") or {}
    market_order = (rules.get("market") or {}).get("resources") or ["food", "wood", "stone"]
    orders = [o for o in orders if isinstance(o, dict)]
    warnings = []
    after = dict(res)  # resources once the market has cleared
    failed_at = None   # position in market_order of the first buy that likely fails
    sold_at = []       # positions of resources sold
    for i, r in enumerate(market_order):
        mine = [o for o in orders if o.get("type") == "market" and o.get("resource") == r]
        buy = sum(int(o.get("qty") or 0) for o in mine if o.get("side") == "buy")
        sell = sum(int(o.get("qty") or 0) for o in mine if o.get("side") == "sell")
        if not buy and not sell:
            continue
        pool = pools.get(r) or {}
        price = _auction_price(pool.get("resource", 0), pool.get("gold", 0), buy - sell)
        for o in mine:
            lim = o.get("limit")
            if not isinstance(lim, (int, float)):
                continue
            if o.get("side") == "buy" and price > lim:
                warnings.append(f"market buy of {o.get('qty')} {r} has limit {lim} but the price is about "
                                f"{price:.3f}: it will likely FAIL (limit orders are dropped, not partly filled)")
                buy -= int(o.get("qty") or 0)
            elif o.get("side") == "sell" and price < lim:
                warnings.append(f"market sell of {o.get('qty')} {r} has limit {lim} but the price is about "
                                f"{price:.3f}: it will likely FAIL")
                sell -= int(o.get("qty") or 0)
        cost = math.ceil(buy * price * (1 + fee)) if buy > 0 else 0
        if buy > 0 and cost > after.get("gold", 0):
            warnings.append(f"market buy of {buy} {r} costs about {cost} gold but only about "
                            f"{after.get('gold', 0)} gold is available when {r} clears; it will likely FAIL")
            failed_at = i if failed_at is None else failed_at
            buy, cost = 0, 0
        if sell > 0:
            sold_at.append(i)
        after["gold"] = after.get("gold", 0) + (math.floor(sell * price * (1 - fee)) if sell > 0 else 0) - cost
        after[r] = after.get(r, 0) + max(buy, 0) - max(sell, 0)
    if failed_at is not None and any(i > failed_at for i in sold_at):
        warnings.append("the market clears " + ", ".join(market_order) + " in that order: gold from selling a "
                        "resource later in that list is not available for an earlier buy the same turn")

    unit_specs = rules.get("units") or {}
    extra_upkeep = 0
    need: dict = {}
    for o in orders:
        cost = {}
        if o.get("type") == "recruit" and o.get("unit") in unit_specs:
            n = int(o.get("count") or 0)
            spec = unit_specs[o["unit"]]
            extra_upkeep += n * spec.get("upkeep", 0)
            cost = {r: n * v for r, v in (spec.get("cost") or {}).items()}
        elif o.get("type") == "settle":
            cost = dict(you.get("settle_cost") or {})
        for r, v in cost.items():
            need[r] = need.get(r, 0) + v
    short = {r: v - after.get(r, 0) for r, v in need.items() if v > after.get(r, 0)}
    if short:
        warnings.append("recruit/settle orders need " + ", ".join(f"{need[r]} {r}" for r in short)
                        + " but only about " + ", ".join(f"{after.get(r, 0)} {r}" for r in short)
                        + " will be on hand when actions run (after the market; income arrives at the end of "
                        "the turn): the later ones will FAIL")
    elif extra_upkeep or after.get("food", 0) < res.get("food", 0):
        warnings += _food_outlook(view, pid, after.get("food", 0) - need.get("food", 0),
                                  you.get("upkeep", 0), extra_upkeep)[1:]
    pending, ids = _pending_gives(view, pid)
    if pending:
        tight = [r for r, v in pending.items() if after.get(r, 0) - need.get(r, 0) - v < 0]
        if tight:
            warnings.append(f"your open offers {', '.join(ids)} would hand over "
                            + ", ".join(f"{pending[r]} {r}" for r in tight)
                            + " the moment they are accepted, before these orders run; if that happens, "
                            "some of these orders will fail (withdraw an offer to keep the resources)")
    return warnings


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
                 "* relic  UPPERCASE = units present (@, C and * tiles may also hold units: see the city and relic "
                 "lists in the state summary). 2nd char = owner: " + legend)
    return "\n".join(lines)


def bundle_str(b: dict | None) -> str:
    """Human-readable deal bundle, e.g. ``60 wood + tiles [5,6] + 5 gold/turn for 10 turns``."""
    b = b or {}
    parts = [f"{b[r]} {r}" for r in ("food", "wood", "stone", "gold") if b.get(r)]
    if b.get("tiles"):
        parts.append("tile" + ("s " if len(b["tiles"]) > 1 else " ") + " ".join(f"[{x},{y}]" for x, y in b["tiles"]))
    if b.get("per_turn"):
        per = ", ".join(f"{v} {r}" for r, v in b["per_turn"].items())
        parts.append(f"{per}/turn for {b.get('turns')} turns")
    return " + ".join(parts) or "nothing"


def _deal_terms(d: dict) -> str:
    txt = f"{d['from']} gives {bundle_str(d.get('give'))}; {d['to']} gives {bundle_str(d.get('get'))}"
    if d.get("peace"):
        txt += f"; peace {d['peace']} turns"
    return txt


def _deals_lines(deals: dict, contracts: list, pid: str | None, max_deals: int) -> list[str]:
    out: list[str] = []
    opened = deals.get("open") or []
    if pid:
        incoming = [d for d in opened if d.get("to") == pid]
        outgoing = [d for d in opened if d.get("from") == pid]
    else:
        incoming, outgoing = [], opened
    if incoming or outgoing:
        out.append("\nOpen deals (barter; answer with propose/counter/accept/reject/withdraw):")
    for d in incoming[:max_deals]:
        ok = "deliverable now" if d.get("deliverable", True) else f"NOT deliverable now: {d.get('problem')}"
        msg = f' — "{d["message"]}"' if d.get("message") else ""
        out.append(f"  {d['id']} TO YOU from {d['from']}: {_deal_terms(d)} (expires after turn "
                   f"{d.get('expires_turn')}; {ok}){msg} — accept with {{\"type\":\"accept\",\"deal\":\"{d['id']}\"}}")
    for d in outgoing[:max_deals]:
        who = "yours, waiting for" if pid else "awaiting"
        out.append(f"  {d['id']} ({who} {d['to']}): {_deal_terms(d)} (expires after turn {d.get('expires_turn')})")
    recent = (deals.get("recent") or [])[:max(3, max_deals // 2)]
    if recent and pid:
        out.append("Your recently closed deals: " + "; ".join(
            f"{d['id']} {d.get('status')}" + (f" ({d['reason']})" if d.get("reason") and d.get("status") != "accepted"
                                                else "") for d in recent))
    if contracts:
        mine = [k for k in contracts if pid in (k.get("payer"), k.get("payee"))] if pid else []
        others = [k for k in contracts if k not in mine]
        out.append("Contracts (paid each turn after yields, before upkeep; a missed payment = default, -25 "
                   "influence):")
        for k in (mine + others)[:max_deals]:
            tag = " <- you pay" if k.get("payer") == pid else (" <- you receive" if k.get("payee") == pid else "")
            per = ", ".join(f"{v} {r}" for r, v in (k.get("per_turn") or {}).items())
            out.append(f"  {k['id']}: {k['payer']} pays {k['payee']} {per}/turn, {k.get('turns_left')} turns left{tag}")
    log = (deals.get("log") or [])[-max(3, max_deals // 2):]
    if log:
        out.append("Recent public deals: " + "; ".join(
            f"t{e.get('turn')} {e['id']}: {_deal_terms(e)}" for e in reversed(log)))
    return out


def describe_event(ev: dict, pid: str | None = None) -> str:
    """One line of text for a diplomacy event from :meth:`AgentCivClient.inbox`."""
    t = ev.get("type")
    who = lambda p: "you" if p and p == pid else p  # noqa: E731
    if t == "deal_proposed":
        d = ev.get("deal") or {}
        msg = f' — "{d["message"]}"' if d.get("message") else ""
        return (f"{who(ev.get('from'))} proposed deal {d.get('id')} to {who(ev.get('to'))}: {_deal_terms(d)} "
                f"(expires after turn {d.get('expires_turn')}){msg}")
    if t == "deal_countered":
        d = ev.get("new") or {}
        msg = f' — "{d["message"]}"' if d.get("message") else ""
        return (f"{who(d.get('from'))} countered deal {ev.get('deal')} with {d.get('id')} to {who(d.get('to'))}: "
                f"{_deal_terms(d)}{msg}")
    if t == "deal_executed":
        c = f"; contracts {', '.join(ev['contracts'])}" if ev.get("contracts") else ""
        return f"deal {ev.get('deal')} EXECUTED: {_deal_terms(ev)}{c}"
    if t == "deal_rejected":
        m = f': "{ev["message"]}"' if ev.get("message") else ""
        return f"{who(ev.get('by'))} rejected deal {ev.get('deal')}{m}"
    if t == "deal_withdrawn":
        return f"deal {ev.get('deal')} withdrawn ({ev.get('reason')})"
    if t == "deal_failed":
        return f"deal {ev.get('deal')} FAILED on acceptance: {ev.get('reason')}"
    if t == "deal_expired":
        return f"deal {ev.get('deal')} expired"
    if t == "contract_paid":
        paid = ", ".join(f"{v} {r}" for r, v in (ev.get("paid") or {}).items())
        return (f"contract {ev.get('contract')}: {who(ev.get('payer'))} paid {who(ev.get('payee'))} {paid} "
                f"({ev.get('turns_left')} turns left)")
    if t == "contract_completed":
        return f"contract {ev.get('contract')} completed ({who(ev.get('payer'))} → {who(ev.get('payee'))})"
    if t == "contract_default":
        return (f"contract {ev.get('contract')} DEFAULTED: {who(ev.get('payer'))} could not pay "
                f"{who(ev.get('payee'))} (penalty {ev.get('penalty')} influence)")
    if t == "say":
        to = "everyone" if ev.get("to") == "all" else who(ev.get("to"))
        return f"{who(ev.get('from'))} → {to}: {ev.get('text')}"
    fields = {k: v for k, v in ev.items() if k not in ("type", "turn", "seq")}
    return f"{t}: {json.dumps(fields, separators=(',', ':'))[:200]}"


def summarize_view(view: dict, pid: str | None = None, max_events: int = 12, max_messages: int = 6,
                   max_deals: int = 10) -> str:
    """A compact, LLM-friendly text summary of a view: your economy, cities,
    armies, nearby threats, diplomacy (open deals for you, contracts,
    reputation, recent public deals), market, victory progress and a
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
        inc = seasonal_income(view, pid)
        caps = you.get("caps") or {}
        parts = []
        for r in RESOURCES:
            cap = f"/{caps[r]}" if r in caps else ""
            parts.append(f"{r} {res.get(r, 0)}{cap} ({inc.get(r, 0):+d})")
        out.append(f"\nYOU: {pid} {me.get('name')} — {'alive' if me.get('alive', True) else 'ELIMINATED'}, "
                   f"score {me.get('score')}, {me.get('cities')} cities, {me.get('tiles')} tiles, "
                   f"military power {me.get('military_power')}.")
        out.append("Resources (income this turn with the season modifier, before upkeep): " + ", ".join(parts))
        upkeep = you.get("upkeep", me.get("upkeep", 0))
        if you:
            sc = you.get("settle_cost") or {}
            out.append(f"Upkeep {upkeep} food/turn. Claim costs "
                       f"{you.get('claim_cost')} influence. Settle costs "
                       f"{', '.join(f'{v} {k}' for k, v in sc.items())}. Market fee "
                       f"{(you.get('market_fee') or 0) * 100:.0f}%. Orders submitted this turn: "
                       f"{'yes' if you.get('submitted') else 'no'}.")
        out += _food_outlook(view, pid, res.get("food", 0), upkeep)
        pending, ids = _pending_gives(view, pid)
        if pending:
            out.append(f"Promised in your open offers ({', '.join(ids)}): {_units_str(pending)} — handed over the "
                       "moment an offer is accepted, even mid-turn before your orders run.")
        out += _my_last_turn_problems(view, pid, max_events)

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

        # other players' armies within 3 tiles of your cities (reported as facts, not judged)
        partners = {t["b"] if t["a"] == pid else t["a"] for t in view.get("treaties", [])
                    if pid in (t["a"], t["b"])}
        nearby = []
        for a in view.get("armies", []):
            if a["owner"] == pid:
                continue
            near = [(max(abs(a["x"] - c["x"]), abs(a["y"] - c["y"])), c) for c in my_cities]
            near = [(d, c) for d, c in near if d <= 3]
            if near:
                d, c = min(near, key=lambda t: t[0])
                tag = " (treaty partner)" if a["owner"] in partners else ""
                nearby.append(f"  {a['owner']}{tag} at [{a['x']},{a['y']}]: {_units_str(a['units'])} "
                               f"(power {_power(a['units'], rules)}), {d} tile(s) from {c['name']}")
        out.append("Other players' armies within 3 tiles of your cities: " + ("\n" + "\n".join(nearby) if nearby else "none"))

        # every other player's city: walls and the units standing in it (all public)
        on_tile = {(a["x"], a["y"], a["owner"]): a["units"] for a in view.get("armies", [])}
        others = [c for c in view.get("cities", []) if c["owner"] != pid]
        lines = []
        for c in others:
            b = c.get("buildings", {})
            units = on_tile.get((c["x"], c["y"], c["owner"])) or {}
            inside = (f"{_units_str(units)} (unit power {_power(units, rules)})"
                      if any(units.values()) else "no units")
            lines.append(f"  {c['owner']} {c['name']} at [{c['x']},{c['y']}]"
                         f"{' (original capital)' if c.get('capital') else ''}: walls {b.get('walls', 0)}, "
                         f"garrison {c.get('garrison', 0)}, {inside}, wonder stage {c.get('wonder_stage', 0)}")
        out.append(f"Other players' cities ({len(others)}): " + ("\n" + "\n".join(lines) if lines else "none"))

    # diplomacy
    treaties = view.get("treaties", [])
    if pid and me:
        turn = view.get("turn") or 0
        mine = {(t["b"] if t["a"] == pid else t["a"]): t["until_turn"] for t in treaties if pid in (t["a"], t["b"])}
        lines = []
        for q in sorted(players):
            if q == pid or not players[q].get("alive", True):
                continue
            if q in mine:
                left = mine[q] - turn
                soon = " — ENDS SOON" if left <= 5 else ""
                lines.append(f"{q}: peace until turn {mine[q]} ({left} turn(s) left){soon}")
            else:
                lines.append(f"{q}: NO treaty (either side may attack)")
        if lines:
            out.append("\nYour treaties: " + "; ".join(lines))
    others_t = [t for t in treaties if pid not in (t["a"], t["b"])]
    if others_t:
        out.append(("Other treaties: " if pid and me else "\nTreaties: ")
                   + "; ".join(f"{t['a']}–{t['b']} until turn {t['until_turn']}" for t in others_t))
    props = [p for p in view.get("treaty_proposals", []) if p.get("to") == pid]
    for p in props:
        out.append(f"Treaty proposal from {p['from']} ({p['turns']} turns) — accept THIS turn with "
                   f"{{\"type\":\"accept_treaty\",\"from\":\"{p['from']}\"}}")
    deals = view.get("deals")
    if deals is None:  # an older server: only resource-for-resource offers
        for o in view.get("trade_offers", []):
            direction = "to you" if o["to"] == pid else ("from you" if o["from"] == pid else "")
            out.append(f"Trade offer {o['id']} {o['from']}→{o['to']} {direction}: gives "
                       f"{_units_str(o['give'])} for {_units_str(o['want'])} (expires after turn {o['expires_turn']})")
    else:
        out += _deals_lines(deals, view.get("contracts") or [], pid, max_deals)

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
    out.append("Players (score | cities tiles | power | food wood stone gold infl | victory progress toward every "
               "condition | reputation):")
    for p in sorted(players.values(), key=lambda p: -p.get("score", 0)):
        flag = "" if p.get("alive", True) else " [eliminated]"
        me_tag = " <- you" if p["id"] == pid else ""
        r = p.get("resources", {})
        out.append(f"  {p['id']} {p['name']}{flag}{me_tag}: {p.get('score')} | {p.get('cities')} {p.get('tiles')} | "
                   f"{p.get('military_power')} | "
                   + " ".join(str(r.get(k, 0)) for k in RESOURCES) + f" | {_progress_str(p, thr)}" + _reputation_str(p))
    out += _relic_lines(view, players, rules)

    events = view.get("events", [])
    if events:
        mine = [e for e in events if e.get("player") == pid and e.get("type") in ("order_failed", "starvation")]
        others = [e for e in events if e not in mine and e.get("type") in (
            "battle", "city_captured", "city_founded", "eliminated", "treaty_signed", "treaty_broken",
            "wonder_stage", "starvation", "trade_executed", "victory", "tile_captured", "contract_default")]
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
