"""Order parsing and pre-validation (docs/DESIGN.md §9).

:func:`prevalidate` turns the raw JSON orders an agent submitted into
canonical order dicts (coordinates become tile indices) and a list of
``{"index": i, "error": "..."}`` for orders that are malformed or impossible
against the current state. It never raises on bad input.

Canonical orders always carry ``"type"`` and ``"index"`` (position in the
submitted list). Orders that pass can still fail at resolution time.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import constants as C
from . import deals as D
from . import fog as F

if TYPE_CHECKING:  # pragma: no cover
    from .game import Game

ORDER_TYPES = (
    "move", "recruit", "build", "claim", "settle", "disband", "market",
    "offer_trade", "accept_trade", "propose_treaty", "accept_treaty",
    "break_treaty", "message",
    # diplomacy actions (§13), processed in phase 1; the three legacy names
    # above (offer_trade, accept_trade, message) are aliases of these
    "propose", "counter", "accept", "reject", "withdraw", "say",
)
# accepted only in games created with fog: true (docs/RULES.md §14)
FOG_ORDER_TYPES = C.FOG_ORDER_TYPES


class OrderError(Exception):
    """Raised internally for an invalid order; message goes to the agent."""


# --------------------------------------------------------------------------
# primitive parsers
# --------------------------------------------------------------------------
def as_int(v, what: str = "value") -> int:
    if isinstance(v, bool):
        raise OrderError(f"{what} must be an integer")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if s.isascii() and s.lstrip("-").isdigit() and len(s) <= 12:
            return int(s)
    raise OrderError(f"{what} must be an integer")


def as_number(v, what: str = "value") -> float:
    if isinstance(v, bool):
        raise OrderError(f"{what} must be a number")
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            raise OrderError(f"{what} must be a number") from None
    else:
        raise OrderError(f"{what} must be a number")
    if f != f or f in (float("inf"), float("-inf")):
        raise OrderError(f"{what} must be finite")
    return f


def as_str(v, what: str) -> str:
    if not isinstance(v, str):
        raise OrderError(f"{what} must be a string")
    return v


class _Ctx:
    """Per-submission scratch state for cumulative checks."""

    def __init__(self):
        self.moved: dict = {}          # (tile, unit) -> count requested so far
        self.disbanded: dict = {}      # (tile, unit) -> count
        self.virtual: set = set()      # tiles claimed/settled earlier in this list
        self.claimed: set = set()
        self.settles: list = []        # tile indices
        self.improved: set = set()
        self.city_builds: dict = {}    # (tile, building) -> count
        self.wonder = False
        self.messages = 0
        self.dip_actions = 0
        self.proposals = 0
        self.deal_refs: set = set()     # deals acted on earlier in this list
        self.treaty_targets: set = set()
        self.accepted_treaties: set = set()
        self.broken: set = set()
        self.spies: set = set()         # (target, mission) of earlier spy orders
        self.counterintel = False


class Validator:
    """Pre-validates one player's order list against the game state."""

    def __init__(self, game: "Game", pid: str):
        self.g = game
        self.pid = pid
        self.ctx = _Ctx()
        self._sight: frozenset | None = None

    @property
    def sight(self) -> frozenset:
        """The player's sight (fog games only; computed on first use)."""
        if self._sight is None:
            self._sight = F.vision(self.g, self.pid)
        return self._sight

    def armies_at(self, i: int) -> dict:
        """Armies on tile ``i`` as far as pre-validation may look: in a fog
        game only tiles in sight (resolution still applies every rule)."""
        if self.g.fog and i not in self.sight:
            return {}
        return self.g.armies.get(i, {})

    # ---------------------------------------------------------------- helpers
    def tile(self, v, what: str = "coordinate") -> int:
        g = self.g
        if isinstance(v, dict):
            if "x" not in v or "y" not in v:
                raise OrderError(f"{what} must be [x, y]")
            x, y = v["x"], v["y"]
        elif isinstance(v, (list, tuple)) and len(v) == 2:
            x, y = v
        else:
            raise OrderError(f"{what} must be [x, y]")
        x = as_int(x, f"{what} x")
        y = as_int(y, f"{what} y")
        if not (0 <= x < g.width and 0 <= y < g.height):
            raise OrderError(f"{what} [{x}, {y}] is off the map")
        return y * g.width + x

    def xy(self, i: int) -> str:
        return f"[{i % self.g.width}, {i // self.g.width}]"

    def units(self, v, allow_none: bool = True) -> dict | None:
        if v is None or v == "all":
            if allow_none:
                return None
            raise OrderError("units required")
        if not isinstance(v, dict):
            raise OrderError('units must be an object like {"infantry": 2}')
        out = {}
        for u, c in v.items():
            if u not in C.UNITS:
                raise OrderError(f"unknown unit type {u!r}")
            n = as_int(c, f"units.{u}")
            if n < 0:
                raise OrderError(f"units.{u} must be >= 0")
            if n > 0:
                out[u] = n
        if not out:
            raise OrderError("no units given")
        return out

    def resources(self, v, what: str) -> dict:
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise OrderError(f"{what} must be an object like {{\"wood\": 50}}")
        out = {}
        for r, amt in v.items():
            if r not in C.TRADABLE:
                raise OrderError(f"{what}: {r!r} is not tradable (tradable: {', '.join(C.TRADABLE)})")
            n = as_int(amt, f"{what}.{r}")
            if n < 0:
                raise OrderError(f"{what}.{r} must be >= 0")
            if n > 0:
                out[r] = n
        return out

    def other_player(self, v, what: str) -> str:
        pid = as_str(v, what)
        p = self.g.player(pid)
        if p is None:
            raise OrderError(f"{what}: unknown player {pid!r}")
        if pid == self.pid:
            raise OrderError(f"{what}: cannot target yourself")
        if not p.alive:
            raise OrderError(f"{what}: player {pid} is eliminated")
        return pid

    def my_units_at(self, i: int) -> dict:
        return self.g.armies.get(i, {}).get(self.pid, {})

    def adjacent_to_territory(self, i: int) -> bool:
        g = self.g
        for j in g.neighbors[i]:
            if g.owner[j] == self.pid or j in self.ctx.virtual:
                return True
        return False

    # --------------------------------------------------------------- orders
    def validate(self, raw) -> dict:
        if not isinstance(raw, dict):
            raise OrderError("order must be an object")
        t = raw.get("type")
        fog = self.g.config.fog
        if t not in ORDER_TYPES and not (fog and t in FOG_ORDER_TYPES):
            valid = ORDER_TYPES + FOG_ORDER_TYPES if fog else ORDER_TYPES
            raise OrderError(f"unknown order type {t!r}; valid: {', '.join(valid)}")
        return getattr(self, "v_" + t)(raw)

    def v_move(self, o: dict) -> dict:
        g = self.g
        src = self.tile(o.get("from"), "from")
        have = self.my_units_at(src)
        if not have:
            raise OrderError(f"you have no units at {self.xy(src)}")
        if "path" in o and o["path"] is not None:
            p = o["path"]
            if not isinstance(p, (list, tuple)):
                raise OrderError("path must be a list of [x, y]")
            path = [self.tile(s, "path step") for s in p]
        elif "to" in o:
            path = [self.tile(o["to"], "to")]
        else:
            raise OrderError("move needs 'path' or 'to'")
        if not 1 <= len(path) <= 2:
            raise OrderError("path must have 1 or 2 steps")
        req = self.units(o.get("units"))
        if req is None:  # all remaining units at the tile
            req = {u: c - self.ctx.moved.get((src, u), 0) for u, c in have.items()}
            req = {u: c for u, c in req.items() if c > 0}
            if not req:
                raise OrderError(f"all your units at {self.xy(src)} are already moving")
        for u, c in req.items():
            used = self.ctx.moved.get((src, u), 0)
            if used + c > have.get(u, 0):
                raise OrderError(f"only {have.get(u, 0) - used} {u} available at {self.xy(src)}")
        if len(path) == 2 and any(u != "cavalry" for u in req):
            raise OrderError("2-step moves are only allowed when every moved unit is cavalry")
        prev = src
        for k, step in enumerate(path):
            if step not in g.neighbors[prev]:
                raise OrderError(f"path step {self.xy(step)} is not 4-adjacent to {self.xy(prev)}")
            if g.terrain[step] not in C.PASSABLE:
                raise OrderError(f"{self.xy(step)} is impassable ({C.TERRAIN[g.terrain[step]]['name']})")
            owner = g.owner[step]
            if owner is not None and owner != self.pid and g.treaty(self.pid, owner):
                raise OrderError(f"{self.xy(step)} belongs to treaty partner {owner}")
            for other in self.armies_at(step):
                if other != self.pid and g.treaty(self.pid, other):
                    raise OrderError(f"{self.xy(step)} holds an army of treaty partner {other}")
            if len(path) == 2 and k == 0:
                if any(other != self.pid and g.hostile(self.pid, other) for other in self.armies_at(step)):
                    raise OrderError(f"cannot move through {self.xy(step)}: hostile army there")
                city = g.cities.get(step)
                if city is not None and city.owner != self.pid and g.hostile(self.pid, city.owner):
                    raise OrderError(f"cannot move through {self.xy(step)}: hostile city (its garrison blocks the way)")
            prev = step
        for u, c in req.items():
            self.ctx.moved[(src, u)] = self.ctx.moved.get((src, u), 0) + c
        return {"type": "move", "from": src, "path": path, "units": req}

    def v_recruit(self, o: dict) -> dict:
        city = self.tile(o.get("city", o.get("at")), "city")
        c = self.g.cities.get(city)
        if c is None or c.owner != self.pid:
            raise OrderError(f"you do not own a city at {self.xy(city)}")
        unit = o.get("unit")
        if unit not in C.UNITS:
            raise OrderError(f"unknown unit {unit!r}; valid: {', '.join(C.UNIT_TYPES)}")
        count = as_int(o.get("count", 1), "count")
        if not 1 <= count <= C.MAX_RECRUIT_PER_ORDER:
            raise OrderError(f"count must be 1..{C.MAX_RECRUIT_PER_ORDER}")
        return {"type": "recruit", "city": city, "unit": unit, "count": count}

    def v_build(self, o: dict) -> dict:
        g = self.g
        at = self.tile(o.get("at"), "at")
        b = o.get("building")
        if b in C.IMPROVEMENTS:
            if g.owner[at] != self.pid and at not in self.ctx.virtual:
                raise OrderError(f"you do not own {self.xy(at)}")
            if at in g.cities:
                raise OrderError("tile improvements cannot be built on a city tile")
            if g.terrain[at] not in C.IMPROVEMENTS[b]["terrain"]:
                allowed = ", ".join(C.TERRAIN[t]["name"] for t in C.IMPROVEMENTS[b]["terrain"])
                raise OrderError(f"{b} requires terrain: {allowed}")
            if g.improvement[at] is not None or at in self.ctx.improved:
                raise OrderError(f"{self.xy(at)} already has an improvement")
            self.ctx.improved.add(at)
        elif b in C.CITY_BUILDINGS:
            c = g.cities.get(at)
            if c is None or c.owner != self.pid:
                raise OrderError(f"you do not own a city at {self.xy(at)}")
            if b == "wonder":
                if self.ctx.wonder:
                    raise OrderError("only one wonder stage per turn")
                p = g.player(self.pid)
                if p.wonder_city is not None and p.wonder_city != at and g.cities.get(p.wonder_city) is not None \
                        and g.cities[p.wonder_city].owner == self.pid:
                    raise OrderError(f"your wonder is in {g.cities[p.wonder_city].name}; build it there")
                self.ctx.wonder = True
            key = (at, b)
            level = c.building_level(b) + self.ctx.city_builds.get(key, 0)
            if level >= C.CITY_BUILDINGS[b]["max"]:
                raise OrderError(f"{b} is already at max level {C.CITY_BUILDINGS[b]['max']}")
            self.ctx.city_builds[key] = self.ctx.city_builds.get(key, 0) + 1
        else:
            valid = list(C.IMPROVEMENTS) + list(C.CITY_BUILDINGS)
            raise OrderError(f"unknown building {b!r}; valid: {', '.join(valid)}")
        return {"type": "build", "at": at, "building": b}

    def v_claim(self, o: dict) -> dict:
        g = self.g
        at = self.tile(o.get("at"), "at")
        if g.terrain[at] not in C.PASSABLE:
            raise OrderError(f"{self.xy(at)} is impassable")
        if at in g.relic_set:
            raise OrderError(f"{self.xy(at)} is a relic: relics cannot be claimed, occupy them with units")
        if g.owner[at] is not None:
            raise OrderError(f"{self.xy(at)} is already owned by {g.owner[at]}")
        if at in self.ctx.virtual:
            raise OrderError(f"{self.xy(at)} is already claimed by an earlier order")
        if not self.adjacent_to_territory(at):
            raise OrderError(f"{self.xy(at)} is not 4-adjacent to your territory")
        self.ctx.virtual.add(at)
        return {"type": "claim", "at": at}

    def v_settle(self, o: dict) -> dict:
        g = self.g
        at = self.tile(o.get("at"), "at")
        if g.terrain[at] not in C.PASSABLE:
            raise OrderError(f"{self.xy(at)} is impassable")
        if at in g.relic_set:
            raise OrderError("cannot settle on a relic")
        if at in g.cities:
            raise OrderError("there is already a city there")
        owner = g.owner[at]
        if owner is None:
            if not self.adjacent_to_territory(at) and at not in self.ctx.virtual:
                raise OrderError(f"{self.xy(at)} is not yours and not 4-adjacent to your territory")
        elif owner != self.pid:
            raise OrderError(f"{self.xy(at)} is owned by {owner}")
        near = g.nearest_city_distance(at)
        if near < C.CITY_MIN_DISTANCE:
            raise OrderError(f"too close to another city (distance {near} < {C.CITY_MIN_DISTANCE})")
        for s in self.ctx.settles:
            if g.cheb(s, at) < C.CITY_MIN_DISTANCE:
                raise OrderError("too close to another settle order")
        self.ctx.settles.append(at)
        self.ctx.virtual.add(at)
        return {"type": "settle", "at": at}

    def v_disband(self, o: dict) -> dict:
        at = self.tile(o.get("at"), "at")
        have = self.my_units_at(at)
        if not have:
            raise OrderError(f"you have no units at {self.xy(at)}")
        req = self.units(o.get("units"))
        if req is None:
            req = dict(have)
        for u, c in req.items():
            used = self.ctx.disbanded.get((at, u), 0)
            if used + c > have.get(u, 0):
                raise OrderError(f"only {have.get(u, 0) - used} {u} at {self.xy(at)}")
        for u, c in req.items():
            self.ctx.disbanded[(at, u)] = self.ctx.disbanded.get((at, u), 0) + c
        return {"type": "disband", "at": at, "units": req}

    def v_market(self, o: dict) -> dict:
        side = o.get("side")
        if side not in ("buy", "sell"):
            raise OrderError("side must be 'buy' or 'sell'")
        r = o.get("resource")
        if r not in C.MARKET_RESOURCES:
            raise OrderError(f"resource must be one of {', '.join(C.MARKET_RESOURCES)}")
        qty = as_int(o.get("qty"), "qty")
        if qty < 1:
            raise OrderError("qty must be >= 1")
        cap = int(self.g.pools[r][0] * C.MARKET_MAX_ORDER_FRACTION)
        if qty > cap:
            raise OrderError(f"qty exceeds {int(C.MARKET_MAX_ORDER_FRACTION * 100)}% of the pool reserve ({cap})")
        limit = o.get("limit")
        if limit is not None:
            limit = as_number(limit, "limit")
            if limit <= 0:
                raise OrderError("limit must be > 0")
        return {"type": "market", "side": side, "resource": r, "qty": qty, "limit": limit}

    def v_diplomacy(self, o: dict) -> dict:
        """Any diplomacy action (§13). Checked against the current state
        now and again when it is applied in phase 1."""
        try:
            a = D.parse_action(o, self.g.width, self.g.height)
        except D.DealError as e:
            raise OrderError(str(e)) from None
        ctx = self.ctx
        if ctx.dip_actions >= C.DIPLOMACY_ACTIONS_PER_TURN:
            raise OrderError(f"at most {C.DIPLOMACY_ACTIONS_PER_TURN} diplomacy actions per turn")
        if a["type"] == "say" and ctx.messages >= C.SAY_PER_TURN:
            raise OrderError(f"at most {C.SAY_PER_TURN} messages per turn")
        if "deal" in a and a["deal"] in ctx.deal_refs:
            raise OrderError(f"deal {a['deal']} is already used by an earlier order")
        err = D.check(self.g, self.pid, a, extra_open=ctx.proposals)
        if err:
            raise OrderError(err)
        ctx.dip_actions += 1
        if a["type"] == "say":
            ctx.messages += 1
        if a["type"] in ("propose", "counter"):
            ctx.proposals += 1
        if "deal" in a:
            ctx.deal_refs.add(a["deal"])
        return {"type": o["type"], "action": a}

    v_propose = v_counter = v_accept = v_reject = v_withdraw = v_say = v_diplomacy
    v_offer_trade = v_accept_trade = v_message = v_diplomacy

    def v_propose_treaty(self, o: dict) -> dict:
        to = self.other_player(o.get("to"), "to")
        turns = as_int(o.get("turns"), "turns")
        if not C.TREATY_MIN_TURNS <= turns <= C.TREATY_MAX_TURNS:
            raise OrderError(f"turns must be {C.TREATY_MIN_TURNS}..{C.TREATY_MAX_TURNS}")
        if self.g.treaty(self.pid, to):
            raise OrderError(f"you already have a treaty with {to}")
        if to in self.ctx.treaty_targets:
            raise OrderError(f"duplicate treaty proposal to {to}")
        self.ctx.treaty_targets.add(to)
        return {"type": "propose_treaty", "to": to, "turns": turns}

    def v_accept_treaty(self, o: dict) -> dict:
        frm = self.other_player(o.get("from"), "from")
        if self.g.treaty_proposal(frm, self.pid) is None:
            raise OrderError(f"no treaty proposal from {frm} to accept (proposals can only be accepted on the next turn)")
        if self.g.treaty(self.pid, frm):
            raise OrderError(f"you already have a treaty with {frm}")
        if frm in self.ctx.accepted_treaties:
            raise OrderError("duplicate accept_treaty")
        self.ctx.accepted_treaties.add(frm)
        return {"type": "accept_treaty", "from": frm}

    def v_break_treaty(self, o: dict) -> dict:
        w = as_str(o.get("with"), "with")
        if not self.g.treaty(self.pid, w):
            raise OrderError(f"you have no treaty with {w!r}")
        if w in self.ctx.broken:
            raise OrderError("duplicate break_treaty")
        self.ctx.broken.add(w)
        return {"type": "break_treaty", "with": w}

    # ------------------------------------------------------ fog games (§14)
    def v_spy(self, o: dict) -> dict:
        target = self.other_player(o.get("target"), "target")
        mission = o.get("mission")
        if mission not in C.SPY_MISSIONS:
            raise OrderError(f"mission must be one of {', '.join(C.SPY_MISSIONS)}")
        invest = as_int(o.get("invest"), "invest")
        if not C.SPY_MIN_INVEST <= invest <= C.SPY_MAX_INVEST:
            raise OrderError(f"invest must be {C.SPY_MIN_INVEST}..{C.SPY_MAX_INVEST} gold")
        if len(self.ctx.spies) >= C.SPY_ORDERS_PER_TURN:
            raise OrderError(f"at most {C.SPY_ORDERS_PER_TURN} spy orders per turn")
        if (target, mission) in self.ctx.spies:
            raise OrderError(f"duplicate spy order ({target}, {mission})")
        self.ctx.spies.add((target, mission))
        return {"type": "spy", "target": target, "mission": mission, "invest": invest}

    def v_counterintel(self, o: dict) -> dict:
        invest = as_int(o.get("invest"), "invest")
        if not 1 <= invest <= C.CI_MAX_INVEST:
            raise OrderError(f"invest must be 1..{C.CI_MAX_INVEST} gold")
        if self.ctx.counterintel:
            raise OrderError("at most one counterintel order per turn")
        self.ctx.counterintel = True
        return {"type": "counterintel", "invest": invest}


def prevalidate(game: "Game", pid: str, raw_orders) -> tuple[list, list]:
    """Return ``(accepted canonical orders, errors)``. Never raises."""
    errors: list = []
    accepted: list = []
    if isinstance(raw_orders, dict) and "orders" in raw_orders:
        raw_orders = raw_orders["orders"]
    if not isinstance(raw_orders, (list, tuple)):
        return [], [{"index": -1, "error": "orders must be a list"}]
    v = Validator(game, pid)
    for i, raw in enumerate(raw_orders):
        if i >= C.MAX_ORDERS_PER_TURN:
            errors.append({"index": i, "error": f"too many orders (max {C.MAX_ORDERS_PER_TURN} per turn)"})
            continue
        try:
            order = v.validate(raw)
        except OrderError as e:
            errors.append({"index": i, "error": str(e)})
            continue
        except Exception as e:  # defensive: malformed input must never crash
            errors.append({"index": i, "error": f"invalid order ({type(e).__name__})"})
            continue
        order["index"] = i
        accepted.append(order)
    return accepted, errors
