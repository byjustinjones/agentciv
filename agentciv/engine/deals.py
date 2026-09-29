"""Barter & deals: live negotiation, contracts and reputation (docs/DESIGN.md §13).

Two layers:

* **Pure helpers** (no :class:`Game` needed; usable by bots, the SDK and
  tools working on a JSON state view): :func:`parse_bundle`,
  :func:`check_bundle`, :func:`parse_action`, :func:`view_delivery_problem`,
  :func:`view_deal_problem`, :func:`bundle_value`.
* **Engine logic** operating on a :class:`~agentciv.engine.game.Game`:
  :func:`run_actions` (behind ``Game.diplomacy``), :func:`apply_action`
  (also used for diplomacy actions inside orders, phase 1),
  :func:`pay_contracts` (phase 7), :func:`expire_deals` (phase 8),
  :func:`on_eliminated` and :func:`view_part` (state views).

A *bundle* is what one side hands over::

    {"wood": 50, "gold": 10,            # immediate resources (tradable only)
     "tiles": [[5, 6]],                 # owned non-city, non-relic tiles
     "per_turn": {"gold": 5}, "turns": 10}   # a contract

Deals are plain dicts (JSON-serialisable) stored on the game; nothing here
uses randomness, so replaying the orders plus ``Game.diplomacy_log`` from the
same seed reproduces a game exactly.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import constants as C

if TYPE_CHECKING:  # pragma: no cover
    from .game import Game

ACTION_TYPES = ("propose", "counter", "accept", "reject", "withdraw", "say")
# legacy order names -> canonical action type
ALIASES = {"offer_trade": "propose", "accept_trade": "accept", "message": "say"}
ALL_ACTION_TYPES = ACTION_TYPES + tuple(ALIASES)
DEAL_ACTIONS = ("counter", "accept", "reject", "withdraw")  # actions on an existing deal
STATUSES = ("open", "accepted", "countered", "rejected", "withdrawn", "expired", "failed")
EVENT_TYPES = ("deal_proposed", "deal_countered", "deal_executed", "deal_rejected", "deal_withdrawn",
               "deal_expired", "deal_failed", "contract_paid", "contract_default", "contract_completed",
               "say")
BUNDLE_KEYS = C.TRADABLE + ("tiles", "per_turn", "turns")


def jcopy(v):
    """Fast deep copy of plain JSON data (dicts, lists, scalars)."""
    if isinstance(v, dict):
        return {k: jcopy(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jcopy(x) for x in v]
    return v


class DealError(ValueError):
    """Malformed deal/action; the message is meant for the agent."""


# ==========================================================================
# pure parsing helpers
# ==========================================================================
def _int(v, what: str) -> int:
    if isinstance(v, bool):
        raise DealError(f"{what} must be an integer")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        # ASCII and short: int() raises a plain ValueError for Unicode digits
        # like "²" and for digit strings over 4300 characters
        if s.isascii() and s.lstrip("-").isdigit() and len(s) <= 12:
            return int(s)
    raise DealError(f"{what} must be an integer")


def _qty(v, what: str) -> int:
    n = _int(v, what)
    if n < 0:
        raise DealError(f"{what} must be >= 0")
    if n > C.DEAL_MAX_QTY:
        raise DealError(f"{what} must be <= {C.DEAL_MAX_QTY}")
    return n


def _text(v, what: str, limit: int, required: bool = False) -> str | None:
    if v is None or (not required and v == ""):
        if required:
            raise DealError(f"{what} is required")
        return None
    if not isinstance(v, str):
        raise DealError(f"{what} must be a string")
    if required and not v.strip():
        raise DealError(f"{what} must not be empty")
    if len(v) > limit:
        raise DealError(f"{what} longer than {limit} characters")
    return v


def _tile(v, width: int | None, height: int | None) -> list:
    if isinstance(v, dict):
        if "x" not in v or "y" not in v:
            raise DealError("tiles must be [x, y]")
        x, y = v["x"], v["y"]
    elif isinstance(v, (list, tuple)) and len(v) == 2:
        x, y = v
    else:
        raise DealError("tiles must be a list of [x, y]")
    x, y = _int(x, "tile x"), _int(y, "tile y")
    if width is not None and height is not None and not (0 <= x < width and 0 <= y < height):
        raise DealError(f"tile [{x}, {y}] is off the map")
    if x < 0 or y < 0:
        raise DealError(f"tile [{x}, {y}] is off the map")
    return [x, y]


def _resources(v, what: str) -> dict:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise DealError(f'{what} must be an object like {{"gold": 5}}')
    out = {}
    for r, amt in v.items():
        if r not in C.TRADABLE:
            if r == "influence":
                raise DealError(f"{what}: influence is not tradable")
            raise DealError(f"{what}: {r!r} is not tradable (tradable: {', '.join(C.TRADABLE)})")
        n = _qty(amt, f"{what}.{r}")
        if n:
            out[r] = n
    return {r: out[r] for r in C.TRADABLE if r in out}


def parse_bundle(raw, width: int | None = None, height: int | None = None,
                 resources_only: bool = False, what: str = "bundle") -> dict:
    """Normalise a bundle (raises :class:`DealError`). Zero quantities are
    dropped; the result has only the non-empty keys, resources in canonical
    order, then ``tiles``, ``per_turn`` and ``turns``. ``{}`` = nothing."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DealError(f'{what} must be an object like {{"wood": 50}}')
    for k in raw:
        if k not in BUNDLE_KEYS:
            if k == "influence":
                raise DealError(f"{what}: influence is not tradable")
            raise DealError(f"{what}: unknown key {k!r} (valid: {', '.join(BUNDLE_KEYS)})")
        if resources_only and k not in C.TRADABLE:
            raise DealError(f"{what}: only resources ({', '.join(C.TRADABLE)}) can be traded with this order")
    out = _resources({r: raw[r] for r in C.TRADABLE if r in raw}, what)
    tiles = raw.get("tiles")
    if tiles is not None and tiles != []:
        if not isinstance(tiles, (list, tuple)):
            raise DealError(f"{what}.tiles must be a list of [x, y]")
        if len(tiles) > C.DEAL_MAX_TILES:
            raise DealError(f"{what}: at most {C.DEAL_MAX_TILES} tiles per bundle")
        parsed = [_tile(t, width, height) for t in tiles]
        if len({tuple(t) for t in parsed}) != len(parsed):
            raise DealError(f"{what}.tiles lists a tile twice")
        out["tiles"] = parsed
    per = _resources(raw.get("per_turn"), f"{what}.per_turn")
    if per:
        if raw.get("turns") is None:
            raise DealError(f"{what}: per_turn needs 'turns' ({C.DEAL_CONTRACT_MIN_TURNS}-{C.DEAL_CONTRACT_MAX_TURNS})")
        turns = _int(raw.get("turns"), f"{what}.turns")
        if not C.DEAL_CONTRACT_MIN_TURNS <= turns <= C.DEAL_CONTRACT_MAX_TURNS:
            raise DealError(f"{what}.turns must be {C.DEAL_CONTRACT_MIN_TURNS}..{C.DEAL_CONTRACT_MAX_TURNS}")
        out["per_turn"] = per
        out["turns"] = turns
    return out


def check_bundle(raw, width: int | None = None, height: int | None = None) -> tuple[dict | None, str | None]:
    """``(normalised bundle, None)`` or ``(None, error)`` — never raises."""
    try:
        return parse_bundle(raw, width, height), None
    except DealError as e:
        return None, str(e)


def parse_deal_id(v, legacy: bool = False) -> str:
    """``"d7"`` (``7`` and ``"7"`` are accepted; with ``legacy`` also ``"t7"``)."""
    if isinstance(v, int) and not isinstance(v, bool):
        return f"d{v}"
    if not isinstance(v, str) or not v.strip():
        raise DealError("deal must be a deal id like \"d7\"")
    s = v.strip()
    if s.isdigit():
        return "d" + s
    if legacy and s[:1] == "t" and s[1:].isdigit():
        return "d" + s[1:]
    return s


def _peace(v) -> int | None:
    if v is None or v is False or v == 0:
        return None
    k = _int(v, "peace")
    if not C.DEAL_PEACE_MIN_TURNS <= k <= C.DEAL_PEACE_MAX_TURNS:
        raise DealError(f"peace must be {C.DEAL_PEACE_MIN_TURNS}..{C.DEAL_PEACE_MAX_TURNS} turns")
    return k


def _expires(v) -> int:
    if v is None:
        return C.DEAL_DEFAULT_EXPIRES_IN
    k = _int(v, "expires_in")
    if not C.DEAL_EXPIRES_MIN <= k <= C.DEAL_EXPIRES_MAX:
        raise DealError(f"expires_in must be {C.DEAL_EXPIRES_MIN}..{C.DEAL_EXPIRES_MAX}")
    return k


def _player_id(v, what: str) -> str:
    if not isinstance(v, str) or not v:
        raise DealError(f"{what} must be a player id like \"p2\"")
    return v


def parse_action(raw, width: int | None = None, height: int | None = None) -> dict:
    """Canonical form of one diplomacy action (structure only, no game
    state). Legacy names are mapped (``offer_trade`` → ``propose`` with
    ``want`` as ``get``; ``accept_trade`` → ``accept``; ``message`` →
    ``say``). The canonical form parses to itself."""
    if not isinstance(raw, dict):
        raise DealError("action must be an object")
    t = raw.get("type")
    if t not in ALL_ACTION_TYPES:
        raise DealError(f"unknown diplomacy action {t!r}; valid: {', '.join(ACTION_TYPES)}")
    if t in ("propose", "offer_trade", "counter"):
        only = t == "offer_trade"
        give = parse_bundle(raw.get("give"), width, height, only, "give")
        get = parse_bundle(raw.get("want") if only else raw.get("get"), width, height, only,
                           "want" if only else "get")
        peace = None if only else _peace(raw.get("peace"))
        if not give and not get and peace is None:
            if only:
                raise DealError("trade must give or want something")
            raise DealError("a deal needs at least one term (give, get or peace)")
        out = {"type": "counter" if t == "counter" else "propose"}
        if t == "counter":
            out["deal"] = parse_deal_id(raw.get("deal", raw.get("id")))
        else:
            out["to"] = _player_id(raw.get("to"), "to")
        out.update(give=give, get=get, peace=peace,
                   message=_text(raw.get("message"), "message", C.DEAL_MESSAGE_MAX_LENGTH),
                   expires_in=_expires(raw.get("expires_in")))
        return out
    if t in ("accept", "accept_trade", "reject", "withdraw"):
        legacy = t == "accept_trade"
        ref = raw.get("deal", raw.get("id", raw.get("deal_id", raw.get("offer_id"))))
        if legacy:
            ref = raw.get("offer_id", raw.get("deal", raw.get("id")))
        out = {"type": "accept" if legacy else t, "deal": parse_deal_id(ref, legacy=True)}
        if t == "reject":
            out["message"] = _text(raw.get("message"), "message", C.DEAL_MESSAGE_MAX_LENGTH)
        return out
    # say / message
    to = raw.get("to", "all")
    return {"type": "say", "to": _player_id(to, "to"),
            "text": _text(raw.get("text"), "text", C.MAX_MESSAGE_LENGTH, required=True)}


# --------------------------------------------------------------------------
# delivery checks (work on a Game or on a JSON view)
# --------------------------------------------------------------------------
def _fmt(res: dict) -> str:
    return ", ".join(f"{v} {r}" for r, v in res.items())


def delivery_problem(giver: str, resources: dict, bundle: dict, tile_info) -> str | None:
    """Why ``giver`` (holding ``resources``) cannot hand over ``bundle`` right
    now, or None. ``tile_info(x, y) -> (owner, is_city, is_relic)``. Only
    immediate terms are checked (contracts are paid later, turn by turn)."""
    short = {r: bundle[r] - max(0, resources.get(r, 0)) for r in C.TRADABLE
             if bundle.get(r, 0) > resources.get(r, 0)}
    if short:
        return f"{giver} is short of {_fmt(short)}"
    for x, y in bundle.get("tiles", ()):
        owner, is_city, is_relic = tile_info(x, y)
        if owner != giver:
            return f"tile [{x}, {y}] is not owned by {giver}"
        if is_city:
            return f"tile [{x}, {y}] is a city (cities cannot be traded)"
        if is_relic:
            return f"tile [{x}, {y}] is a relic (relics cannot be traded)"
    return None


def tiles_received(log, turn: int, pid: str) -> int:
    """Tiles ``pid`` received by deals executed on ``turn`` (``log`` = the
    executed-deal log, oldest first: ``Game.deal_log`` or ``view['deals']['log']``)."""
    n = 0
    for e in reversed(log or ()):
        if e.get("turn") != turn:
            break
        if e.get("to") == pid:
            n += len((e.get("give") or {}).get("tiles", ()))
        if e.get("from") == pid:
            n += len((e.get("get") or {}).get("tiles", ()))
    return n


def land_problem(receiver: str, tiles, owner_at, units_at=None, leaving=(),
                 received: int | None = None) -> str | None:
    """Why ``tiles`` cannot pass to ``receiver`` (None = they can).

    * every tile must be 4-adjacent to ``receiver``'s land (not counting the
      tiles in ``leaving``, which it hands over in the same deal) or to
      another tile of ``tiles`` that is — traded land extends a border, so
      it cannot be used to plant a third party's territory (impassable for
      that party's treaty partners) next to somebody else's city;
    * ``units_at(x, y)`` (players with units there): no units but the
      receiver's may stand on a traded tile (they would capture it back, or
      block the new owner while a treaty lasts);
    * ``received`` (tiles already received by deals this turn): at most
      ``DEAL_MAX_TILES_RECEIVED_PER_TURN`` per player per turn.
    ``owner_at(x, y)`` returns the owner (None off the map)."""
    tiles = [(int(t[0]), int(t[1])) for t in tiles or ()]
    if not tiles:
        return None
    gone = {(int(t[0]), int(t[1])) for t in leaving or ()}
    pending, reached = set(tiles), set()
    grown = True
    while grown:
        grown = False
        for x, y in sorted(pending - reached):
            for n in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if n in reached or (n not in gone and n not in pending and owner_at(*n) == receiver):
                    reached.add((x, y))
                    grown = True
                    break
    for x, y in tiles:
        if (x, y) not in reached:
            return (f"tile [{x}, {y}] does not touch {receiver}'s land (traded tiles must be 4-adjacent to "
                    f"the receiver's territory or to another tile it receives in the same deal)")
    if units_at is not None:
        for x, y in tiles:
            others = sorted(q for q in units_at(x, y) if q != receiver)
            if others:
                return (f"tile [{x}, {y}] holds units of {others[0]} (a traded tile may hold only units of "
                        f"its new owner {receiver})")
    cap = C.DEAL_MAX_TILES_RECEIVED_PER_TURN
    if received is not None and received + len(tiles) > cap:
        return f"{receiver} may receive at most {cap} tiles by deals per turn ({received} already this turn)"
    return None


def _view_tile_info(view: dict):
    m = view.get("map") or {}
    owner = m.get("owner") or []
    cities = {(c["x"], c["y"]) for c in view.get("cities", [])}
    relics = {(r["x"], r["y"]) for r in m.get("relics", [])}

    def info(x, y):
        try:
            o = owner[y][x]
        except (IndexError, TypeError):
            o = None
        return o, (x, y) in cities, (x, y) in relics
    return info


def _view_owner_at(view: dict):
    owner = (view.get("map") or {}).get("owner") or []

    def at(x, y):
        if x < 0 or y < 0:
            return None
        try:
            return owner[y][x]
        except (IndexError, TypeError):
            return None
    return at


def _view_units_at(view: dict):
    here: dict = {}
    for a in view.get("armies", ()) or ():
        if any((a.get("units") or {}).values()):
            here.setdefault((a.get("x"), a.get("y")), set()).add(a.get("owner"))
    return lambda x, y: here.get((x, y), ())


def view_delivery_problem(view: dict, pid: str, bundle, receiver: str | None = None,
                          leaving=()) -> str | None:
    """Can player ``pid`` deliver ``bundle`` (raw or normalised) according
    to the JSON state ``view``? Returns None or a reason. With ``receiver``
    the tiles are also checked for :func:`land_problem` (``leaving``: tiles
    the receiver hands over in the same deal)."""
    b, err = check_bundle(bundle)
    if err:
        return err
    res = next((p.get("resources", {}) for p in view.get("players", []) if p.get("id") == pid), None)
    if res is None:
        return f"unknown player {pid!r}"
    err = delivery_problem(pid, res, b, _view_tile_info(view))
    if err or receiver is None or not b.get("tiles"):
        return err
    received = tiles_received((view.get("deals") or {}).get("log"), view.get("turn"), receiver)
    return land_problem(receiver, b["tiles"], _view_owner_at(view), _view_units_at(view), leaving, received)


def view_deal_problem(view: dict, deal: dict) -> str | None:
    """Would ``deal`` (from ``view['deals']['open']``) settle right now?"""
    give, get = deal.get("give") or {}, deal.get("get") or {}
    return (view_delivery_problem(view, deal["from"], give, deal["to"], get.get("tiles", ()))
            or view_delivery_problem(view, deal["to"], get, deal["from"], give.get("tiles", ())))


def bundle_value(bundle: dict, prices: dict | None = None, tile_value: float = 0.0,
                 discount: float = 1.0) -> float:
    """Rough gold value of a bundle: resources at market ``prices`` (gold =
    1; defaults to ``view['market']['prices']`` shape), each tile at
    ``tile_value``, contract instalments summed with a per-turn ``discount``."""
    prices = dict(prices or {})
    prices.setdefault("gold", 1.0)
    for r, (a, g) in C.MARKET_POOLS_PER_PLAYER.items():
        prices.setdefault(r, g / a)
    val = sum(bundle.get(r, 0) * prices[r] for r in C.TRADABLE)
    val += tile_value * len(bundle.get("tiles", ()))
    per = bundle.get("per_turn") or {}
    if per:
        inst = sum(v * prices[r] for r, v in per.items())
        val += sum(inst * discount ** k for k in range(int(bundle.get("turns", 0))))
    return val


# ==========================================================================
# engine logic
# ==========================================================================
def _tile_info(g: "Game"):
    def info(x, y):
        i = g.idx(x, y)
        return g.owner[i], i in g.cities, i in g.relic_set
    return info


def _owner_at(g: "Game"):
    def at(x, y):
        if 0 <= x < g.width and 0 <= y < g.height:
            return g.owner[g.idx(x, y)]
        return None
    return at


def _units_at(g: "Game"):
    return lambda x, y: tuple(g.armies.get(g.idx(x, y), ()))


def _counts(g: "Game", pid: str) -> list:
    if g._dip_counts_turn != g.turn:
        g._dip_counts = {}
        g._dip_counts_turn = g.turn
    return g._dip_counts.setdefault(pid, [0, 0])


def _emit(g: "Game", etype: str, vis: list | None, **fields) -> dict:
    g.diplomacy_seq += 1
    ev = g._emit(etype, vis, seq=g.diplomacy_seq, **jcopy(fields))
    g._dip_feed.append(ev)
    if len(g._dip_feed) > C.DIPLOMACY_FEED_MAX:
        del g._dip_feed[: len(g._dip_feed) - C.DIPLOMACY_FEED_MAX]
    return ev


def _ref_error(g: "Game", pid: str, action: dict) -> str | None:
    """Checks for actions on an existing deal. Deals of other players are
    indistinguishable from non-existent ones (no probing)."""
    did = action["deal"]
    d = g.deals.get(did)
    if d is None or pid not in (d["from"], d["to"]):
        return f"no open deal {did!r} involving you"
    if d["status"] != "open":
        return f"deal {did} is no longer open ({d['status']})"
    t = action["type"]
    if t == "withdraw":
        if d["from"] != pid:
            return f"only the proposer ({d['from']}) can withdraw deal {did}; reject it instead"
    elif d["to"] != pid:
        return f"only the recipient ({d['to']}) can {t} deal {did}"
    return None


def _terms_error(g: "Game", frm: str, to: str, action: dict, extra_open: int = 0) -> str | None:
    tp = g.player(to)
    if tp is None:
        return f"unknown player {to!r}"
    if to == frm:
        return "cannot make a deal with yourself"
    if not tp.alive:
        return f"player {to} is eliminated"
    w, h = g.width, g.height
    for side, owner in (("give", frm), ("get", to)):
        for x, y in action[side].get("tiles", ()):
            if not (0 <= x < w and 0 <= y < h):
                return f"{side}: tile [{x}, {y}] is off the map"
            i = g.idx(x, y)
            if g.owner[i] != owner:
                return f"{side}: tile [{x}, {y}] is not owned by {owner}"
            if i in g.cities:
                return f"{side}: tile [{x}, {y}] is a city (cities cannot be traded)"
            if i in g.relic_set:
                return f"{side}: tile [{x}, {y}] is a relic (relics cannot be traded)"
    for side, receiver, other in (("give", to, "get"), ("get", frm, "give")):
        err = land_problem(receiver, action[side].get("tiles"), _owner_at(g),
                           leaving=action[other].get("tiles", ()))
        if err:
            return f"{side}: {err}"
    opened = sum(1 for d in g.open_deals.values() if d["from"] == frm) + extra_open
    if opened >= C.DEAL_MAX_OPEN_PER_PLAYER:
        return f"you already have {C.DEAL_MAX_OPEN_PER_PLAYER} open proposals (withdraw one first)"
    return None


def check(g: "Game", pid: str, action: dict, extra_open: int = 0) -> str | None:
    """State checks for a canonical action by ``pid`` (no mutation).
    ``extra_open``: proposals already queued (order pre-validation)."""
    t = action["type"]
    if t == "say":
        to = action["to"]
        if to == "all":
            return None
        tp = g.player(to)
        if tp is None:
            return f"to: unknown player {to!r}"
        if to == pid:
            return "to: cannot target yourself"
        if not tp.alive:
            return f"to: player {to} is eliminated"
        return None
    if t == "propose":
        return _terms_error(g, pid, action["to"], action, extra_open)
    err = _ref_error(g, pid, action)
    if err or t != "counter":
        return err
    return _terms_error(g, pid, g.deals[action["deal"]]["from"], action, extra_open)


def run_actions(g: "Game", pid: str, actions, via: str = "channel") -> list:
    """``Game.diplomacy``: apply ``actions`` immediately, in order."""
    if isinstance(actions, dict):
        actions = actions["actions"] if "actions" in actions else [actions]
    if not isinstance(actions, (list, tuple)):
        return [{"index": -1, "ok": False, "error": "actions must be a list"}]
    p = g.player(pid)
    err = None
    if p is None:
        err = f"unknown player {pid!r}"
    elif g.status != "running":
        err = f"game is not running (status {g.status})"
    elif not p.alive:
        err = "you have been eliminated"
    if err:
        return [{"index": -1, "ok": False, "error": err}]
    out = []
    for i, raw in enumerate(actions):
        if i >= C.MAX_ACTIONS_PER_CALL:
            res = {"ok": False, "error": f"too many actions in one call (max {C.MAX_ACTIONS_PER_CALL})"}
        else:
            try:
                res = apply_action(g, pid, raw, via)
            except Exception as e:  # pragma: no cover - defensive: never raise
                res = {"ok": False, "error": f"internal error ({type(e).__name__})"}
        out.append(dict(index=i, **res))
    return out


def apply_action(g: "Game", pid: str, raw, via: str = "channel") -> dict:
    """Validate and apply one action. Returns ``{"ok": True, ...}`` or
    ``{"ok": False, "error": ...}``. Applied actions (including an accept
    whose settlement failed) count toward the per-turn limit and are logged
    in ``g.diplomacy_log``."""
    try:
        action = parse_action(raw, g.width, g.height)
    except DealError as e:
        return {"ok": False, "error": str(e)}
    cnt = _counts(g, pid)
    if cnt[0] >= C.DIPLOMACY_ACTIONS_PER_TURN:
        return {"ok": False, "error": f"at most {C.DIPLOMACY_ACTIONS_PER_TURN} diplomacy actions per turn"}
    if action["type"] == "say" and cnt[1] >= C.SAY_PER_TURN:
        return {"ok": False, "error": f"at most {C.SAY_PER_TURN} messages per turn"}
    err = check(g, pid, action)
    if err:
        return {"ok": False, "error": err}
    cnt[0] += 1
    if action["type"] == "say":
        cnt[1] += 1
    g.diplomacy_seq += 1
    g.diplomacy_log.append({"turn": g.turn, "seq": g.diplomacy_seq, "pid": pid,
                            "action": jcopy(action), "via": via})
    res = _PERFORM[action["type"]](g, pid, action)
    g._invalidate()
    return res


# --------------------------------------------------------------- performers
def deal_view(d: dict, reason: bool = False) -> dict:
    out = {k: jcopy(d[k]) for k in ("id", "thread", "from", "to", "give", "get", "peace", "message",
                                             "turn", "expires_turn", "status")}
    if reason:
        out["reason"] = d.get("reason")
        out["closed_turn"] = d.get("closed_turn")
    return out


def _new_deal(g: "Game", frm: str, to: str, action: dict, thread: str | None) -> dict:
    g._deal_counter += 1
    did = f"d{g._deal_counter}"
    d = {"id": did, "thread": thread or did, "from": frm, "to": to,
         "give": jcopy(action["give"]), "get": jcopy(action["get"]),
         "peace": action["peace"], "message": action["message"], "turn": g.turn,
         "expires_turn": g.turn + action["expires_in"], "status": "open",
         "reason": None, "closed_turn": None}
    g.deals[did] = d
    g.open_deals[did] = d
    return d


def _close(g: "Game", d: dict, status: str, reason: str | None) -> None:
    d["status"] = status
    d["reason"] = reason
    d["closed_turn"] = g.turn
    g.open_deals.pop(d["id"], None)
    for pid in (d["from"], d["to"]):
        lst = g._recent_deals.setdefault(pid, [])
        lst.append(d)
        if len(lst) > C.DEALS_RECENT_IN_VIEW:
            del lst[0]
    g._recent_all.append(d)
    if len(g._recent_all) > C.DEALS_RECENT_IN_FULL_VIEW:
        del g._recent_all[0]


def _do_propose(g, pid, a):
    d = _new_deal(g, pid, a["to"], a, None)
    _emit(g, "deal_proposed", [pid, a["to"]], by=pid, deal=deal_view(d), **{"from": pid, "to": a["to"]})
    return {"ok": True, "deal": d["id"]}


def _do_counter(g, pid, a):
    old = g.deals[a["deal"]]
    new = _new_deal(g, pid, old["from"], a, old["thread"])
    _close(g, old, "countered", f"countered by {pid} with {new['id']}")
    _emit(g, "deal_countered", [pid, old["from"]], by=pid, deal=old["id"], new=deal_view(new),
          **{"from": old["from"], "to": pid})
    return {"ok": True, "deal": new["id"], "countered": old["id"]}


def _do_reject(g, pid, a):
    d = g.deals[a["deal"]]
    _close(g, d, "rejected", a["message"] or f"rejected by {pid}")
    _emit(g, "deal_rejected", [d["from"], d["to"]], by=pid, deal=d["id"], message=a["message"],
          **{"from": d["from"], "to": d["to"]})
    return {"ok": True, "deal": d["id"]}


def _do_withdraw(g, pid, a):
    d = g.deals[a["deal"]]
    _close(g, d, "withdrawn", f"withdrawn by {pid}")
    _emit(g, "deal_withdrawn", [d["from"], d["to"]], by=pid, deal=d["id"], reason=d["reason"],
          **{"from": d["from"], "to": d["to"]})
    return {"ok": True, "deal": d["id"]}


def _do_say(g, pid, a):
    to = a["to"]
    g.messages.append({"turn": g.turn, "from": pid, "to": to, "text": a["text"]})
    _emit(g, "say", None if to == "all" else [pid, to], by=pid, text=a["text"], **{"from": pid, "to": to})
    return {"ok": True}


def settle_problem(g: "Game", d: dict) -> str | None:
    """Why deal ``d`` cannot be settled right now (None = it can)."""
    fp, tp = g.player(d["from"]), g.player(d["to"])
    if fp is None or tp is None or not fp.alive or not tp.alive:
        return "a party is no longer in the game"
    info, owner_at, units_at = _tile_info(g), _owner_at(g), _units_at(g)
    for giver, receiver, bundle, back in ((fp, tp, d["give"], d["get"]), (tp, fp, d["get"], d["give"])):
        err = delivery_problem(giver.id, giver.resources, bundle, info)
        if err:
            return err
        if bundle.get("tiles"):
            err = land_problem(receiver.id, bundle["tiles"], owner_at, units_at, back.get("tiles", ()),
                               tiles_received(g.deal_log, g.turn, receiver.id))
            if err:
                return err
    return None


def _do_accept(g, pid, a):
    d = g.deals[a["deal"]]
    problem = settle_problem(g, d)
    if problem:
        _close(g, d, "failed", problem)
        _emit(g, "deal_failed", [d["from"], d["to"]], by=pid, deal=d["id"], reason=problem,
              **{"from": d["from"], "to": d["to"]})
        return {"ok": False, "error": f"deal {d['id']} failed: {problem}", "deal": d["id"], "status": "failed"}
    _execute(g, d)
    return {"ok": True, "deal": d["id"], "status": "accepted"}


def _execute(g: "Game", d: dict) -> None:
    fp, tp = g.player(d["from"]), g.player(d["to"])
    for giver, receiver, bundle in ((fp, tp, d["give"]), (tp, fp, d["get"])):
        for r in C.TRADABLE:
            v = bundle.get(r, 0)
            if v:
                giver.resources[r] -= v
                receiver.resources[r] += v
        for x, y in bundle.get("tiles", ()):
            g._set_owner(g.idx(x, y), receiver.id)   # improvements/deposits stay with the tile
    _close(g, d, "accepted", None)
    contracts = []
    for giver, receiver, bundle in ((fp, tp, d["give"]), (tp, fp, d["get"])):
        if bundle.get("per_turn"):
            g._contract_counter += 1
            c = {"id": f"c{g._contract_counter}", "payer": giver.id, "payee": receiver.id,
                 "per_turn": dict(bundle["per_turn"]), "turns_left": bundle["turns"], "deal": d["id"]}
            g.contracts.append(c)
            contracts.append(c["id"])
    fp.deals += 1
    tp.deals += 1
    entry = {"id": d["id"], "turn": g.turn, "from": d["from"], "to": d["to"],
             "give": jcopy(d["give"]), "get": jcopy(d["get"]), "peace": d["peace"]}
    g.deal_log.append(entry)
    _emit(g, "deal_executed", None, by=d["to"], deal=d["id"], thread=d["thread"], give=d["give"],
          get=d["get"], peace=d["peace"], contracts=contracts, **{"from": d["from"], "to": d["to"]})
    if d["peace"]:
        key = g._pair(d["from"], d["to"])
        until = max(g.turn + d["peace"], g.treaties.get(key, -1))
        g.treaties[key] = until
        g._emit("treaty_signed", a=d["from"], b=d["to"], until_turn=until, deal=d["id"])
    g._invalidate()


_PERFORM = {"propose": _do_propose, "counter": _do_counter, "accept": _do_accept,
            "reject": _do_reject, "withdraw": _do_withdraw, "say": _do_say}


# --------------------------------------------------------------- turn hooks
def default_penalty(per_turn: dict, turns_left: int) -> int:
    """Influence penalty for defaulting with ``turns_left`` instalments of
    ``per_turn`` unpaid: ``CONTRACT_DEFAULT_PENALTY``, or 1 per
    ``CONTRACT_DEFAULT_OWED_PER_INFLUENCE`` units still owed if that is more."""
    owed = sum(per_turn.values()) * max(0, turns_left)
    return max(C.CONTRACT_DEFAULT_PENALTY, -(-owed // C.CONTRACT_DEFAULT_OWED_PER_INFLUENCE))


def pay_contracts(g: "Game") -> None:
    """Phase 7, after yields and before upkeep: every contract (in creation
    order) pays its full instalment or defaults."""
    for p in g.players:                       # unpaid default penalties first
        if p.alive and p.influence_debt > 0:
            take = min(p.influence_debt, max(0, p.resources["influence"]))
            p.resources["influence"] -= take
            p.influence_debt -= take
    keep = []
    for c in g.contracts:
        payer, payee = g.player(c["payer"]), g.player(c["payee"])
        if not payer.alive or not payee.alive:   # pragma: no cover - removed on elimination
            continue
        per = c["per_turn"]
        if all(payer.resources.get(r, 0) >= v for r, v in per.items()):
            for r, v in per.items():
                payer.resources[r] -= v
                payee.resources[r] += v
            c["turns_left"] -= 1
            _emit(g, "contract_paid", [payer.id, payee.id], contract=c["id"], payer=payer.id,
                  payee=payee.id, paid=per, turns_left=c["turns_left"])
            if c["turns_left"] <= 0:
                payer.contracts_honoured += 1
                _emit(g, "contract_completed", [payer.id, payee.id], contract=c["id"], payer=payer.id,
                      payee=payee.id, deal=c["deal"])
                continue
            keep.append(c)
        else:
            penalty = default_penalty(per, c["turns_left"])
            taken = min(penalty, max(0, payer.resources["influence"]))
            payer.resources["influence"] -= taken
            payer.influence_debt += penalty - taken      # paid from future influence
            payer.defaults += 1
            _emit(g, "contract_default", None, contract=c["id"], payer=payer.id, payee=payee.id,
                  per_turn=per, turns_left=c["turns_left"], penalty=penalty, debt=penalty - taken,
                  deal=c["deal"])
    g.contracts = keep
    g._invalidate()


def expire_deals(g: "Game") -> None:
    """Phase 8: deals whose ``expires_turn`` is this turn close as expired."""
    t = g.turn
    for d in [d for d in g.open_deals.values() if d["expires_turn"] <= t]:
        _close(g, d, "expired", f"expired at the end of turn {d['expires_turn']}")
        _emit(g, "deal_expired", [d["from"], d["to"]], deal=d["id"], **{"from": d["from"], "to": d["to"]})


def on_eliminated(g: "Game", pid: str) -> None:
    for d in [d for d in g.open_deals.values() if pid in (d["from"], d["to"])]:
        _close(g, d, "withdrawn", f"{pid} was eliminated")
        _emit(g, "deal_withdrawn", [d["from"], d["to"]], by=None, deal=d["id"], reason=d["reason"],
              **{"from": d["from"], "to": d["to"]})
    g.contracts = [c for c in g.contracts if pid not in (c["payer"], c["payee"])]


# --------------------------------------------------------------- views
def view_part(g: "Game", viewer: str | None, omniscient: bool) -> dict:
    """``deals``, ``contracts`` and ``diplomacy_seq`` for a state view."""
    if omniscient:
        open_ = list(g.open_deals.values())
        recent = list(g._recent_all)
    elif viewer is not None:
        open_ = [d for d in g.open_deals.values() if viewer in (d["from"], d["to"])]
        recent = list(g._recent_deals.get(viewer, ()))
    else:
        open_, recent = [], []
    opened = []
    for d in open_:
        v = deal_view(d)
        v["problem"] = settle_problem(g, d)
        v["deliverable"] = v["problem"] is None
        opened.append(v)
    return {
        "deals": {
            "open": opened,
            "recent": [deal_view(d, reason=True) for d in reversed(recent)],
            "log": jcopy(g.deal_log[-C.DEALS_LOG_IN_VIEW:]),
        },
        "contracts": [jcopy(c) for c in g.contracts],
        # the token-less spectator of a running game must not learn how much
        # private negotiation is going on (§12): the counter is withheld
        "diplomacy_seq": g.diplomacy_seq if (omniscient or viewer is not None) else None,
    }


def legacy_trade_offers(g: "Game", viewer: str | None, omniscient: bool) -> list:
    """The pre-§13 ``trade_offers`` view field: open resource-only deals
    visible to the viewer (``want`` = ``get``)."""
    out = []
    for d in g.open_deals.values():
        if not (omniscient or (viewer is not None and viewer in (d["from"], d["to"]))):
            continue
        if d["peace"] or any(k not in C.TRADABLE for k in list(d["give"]) + list(d["get"])):
            continue
        out.append({"id": d["id"], "from": d["from"], "to": d["to"], "give": dict(d["give"]),
                    "want": dict(d["get"]), "turn": d["turn"], "expires_turn": d["expires_turn"]})
    return out


def inbox(g: "Game", pid: str, since: int = 0) -> dict:
    """Diplomacy events visible to ``pid`` with ``seq > since`` that were not
    caused by ``pid`` itself (for long-polling agents)."""
    items = []
    for ev in g._dip_feed:
        if ev["seq"] <= since:
            continue
        vis = ev.get("_vis")
        if vis is not None and pid not in vis:
            continue
        if ev.get("by") == pid:
            continue
        items.append({k: jcopy(v) for k, v in ev.items() if k != "_vis"})
    return {"seq": g.diplomacy_seq, "items": items}
