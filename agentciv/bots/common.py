"""Shared helpers for the built-in bots.

Everything here works on the JSON player view (docs/DESIGN.md §10) and uses
the engine's own constants and combat math, so the bots' estimates match the
real rules exactly.

Main pieces:

* :class:`World` – an index-based snapshot of a view (flat tile arrays, city
  and army lookups, relations) plus geometry helpers.
* Path finding – :func:`World.bfs` / :meth:`World.step_towards` over tiles a
  player may enter (respects impassable terrain and treaty partners).
* Combat – :func:`simulate_attack` runs the engine's deterministic battle
  procedure on copies of the armies; :func:`threat_to` estimates the worst
  attack a city can face soon.
* Economy – tile values, improvement ROI, food projection, market price
  estimation (:func:`sell_price`, :func:`buy_price`).
* :class:`Plan` – an order list with a resource budget, so bots never emit
  orders they cannot pay for.
* :class:`SafeBot` – a Bot base class whose ``act`` never raises.
"""
from __future__ import annotations

import math
import random
from collections import deque

from agentciv.engine import combat as CB
from agentciv.engine import constants as C
from agentciv.engine import market as MK
from agentciv.engine.rules import building_cost, claim_cost, settle_cost, unit_cost

from .base import Bot

TRADABLE = C.TRADABLE
CAPPED = C.CAPPED_RESOURCES
UNIT_TYPES = C.UNIT_TYPES
PASSABLE = C.PASSABLE
DEFENSIVE = C.DEFENSIVE_TERRAIN

# Which unit type beats which (attacker -> countered) and the reverse lookup:
# "what should I field against enemy unit X".
COUNTERS = dict(C.COUNTERS)
COUNTERED_BY = {v: k for k, v in COUNTERS.items()}   # cavalry -> infantry, ...

_NEIGHBOR_CACHE: dict = {}


def neighbors_for(w: int, h: int) -> list:
    """4-neighbour tuples for every tile of a ``w`` x ``h`` map (cached)."""
    key = (w, h)
    nb = _NEIGHBOR_CACHE.get(key)
    if nb is None:
        nb = []
        for i in range(w * h):
            x, y = i % w, i // w
            out = []
            for nx, ny in ((x, y - 1), (x + 1, y), (x, y + 1), (x - 1, y)):
                if 0 <= nx < w and 0 <= ny < h:
                    out.append(ny * w + nx)
            nb.append(tuple(out))
        _NEIGHBOR_CACHE[key] = nb
    return nb


def can_pay(res: dict, cost: dict) -> bool:
    return all(res.get(r, 0) >= v for r, v in cost.items())


def total_units(units: dict | None) -> int:
    return sum(units.values()) if units else 0


def raw_strength(units: dict | None) -> int:
    """Σ count·strength (the engine's ``military_power``)."""
    if not units:
        return 0
    return sum(c * C.UNITS[u]["strength"] for u, c in units.items() if u in C.UNITS)


def add_units(a: dict, b: dict) -> dict:
    out = dict(a)
    for u, c in b.items():
        out[u] = out.get(u, 0) + c
    return {u: c for u, c in out.items() if c > 0}


def upkeep_of(units: dict) -> int:
    return sum(c * C.UNITS[u]["upkeep"] for u, c in units.items() if u in C.UNITS)


# ---------------------------------------------------------------------------
# World snapshot
# ---------------------------------------------------------------------------
class World:
    """Index-based snapshot of a player view with geometry helpers.

    Tile indices are ``y * width + x`` as in the engine.
    """

    def __init__(self, view: dict):
        self.view = view
        self.turn = int(view.get("turn", 0))
        self.max_turns = int(view.get("max_turns", C.DEFAULT_MAX_TURNS))
        you = view.get("you") or {}
        self.me = you.get("id")
        self.you = you
        m = view.get("map") or {}
        self.w = int(m.get("width", 0))
        self.h = int(m.get("height", 0))
        w = self.w
        self.n_tiles = self.w * self.h
        self.terrain = "".join(m.get("terrain", []))
        self.owner = [o for row in m.get("owner", []) for o in row]
        self.nb = neighbors_for(self.w, self.h) if self.n_tiles else []
        self.improvement = {d["y"] * w + d["x"]: d["building"] for d in m.get("improvements", [])}
        self.deposit = {d["y"] * w + d["x"]: d["remaining"] for d in m.get("deposits", [])}
        self.relics = [d["y"] * w + d["x"] for d in m.get("relics", [])]
        self.relic_set = frozenset(self.relics)
        self.cities = {c["y"] * w + c["x"]: c for c in view.get("cities", [])}
        self.armies: dict = {}
        for a in view.get("armies", []):
            self.armies.setdefault(a["y"] * w + a["x"], {})[a["owner"]] = dict(a["units"])
        self.players = {p["id"]: p for p in view.get("players", [])}
        self.alive = [p["id"] for p in view.get("players", []) if p.get("alive")]
        self.n_players = len(self.players)
        self.rivals = [q for q in self.alive if q != self.me]
        self.treaties: dict = {}          # other pid -> until_turn (treaties involving me)
        self.treaty_pairs = set()
        for t in view.get("treaties", []):
            a, b = t["a"], t["b"]
            self.treaty_pairs.add((a, b) if a < b else (b, a))
            if a == self.me:
                self.treaties[b] = t["until_turn"]
            elif b == self.me:
                self.treaties[a] = t["until_turn"]
        self.res = dict(you.get("resources", {}))
        self.caps = dict(you.get("caps", {r: C.STORAGE_BASE for r in CAPPED}))
        self.income = dict(you.get("income", {}))
        self.upkeep = int(you.get("upkeep", 0))
        self.fee = float(you.get("market_fee", C.MARKET_FEE))
        cap = you.get("capital")
        self.capital = cap[1] * w + cap[0] if cap else None
        self.season = view.get("season") or {}
        mk = view.get("market") or {}
        self.pools = {r: [float(p["resource"]), float(p["gold"])] for r, p in (mk.get("pools") or {}).items()}
        vt = (view.get("victory") or {}).get("thresholds") or {}
        self.thresholds = vt
        self.events = view.get("events", [])
        # derived
        self.my_cities = [i for i, c in self.cities.items() if c["owner"] == self.me]
        self.my_tiles = [i for i, o in enumerate(self.owner) if o == self.me]
        self.my_tile_set = set(self.my_tiles)
        self.my_armies = {i: per[self.me] for i, per in self.armies.items() if self.me in per}
        self.my_units = {u: 0 for u in UNIT_TYPES}
        for u in self.my_armies.values():
            for k, c in u.items():
                self.my_units[k] = self.my_units.get(k, 0) + c

    # -- geometry ---------------------------------------------------------
    def xy(self, i: int) -> list:
        return [i % self.w, i // self.w]

    def idx(self, x: int, y: int) -> int:
        return y * self.w + x

    def cheb(self, a: int, b: int) -> int:
        w = self.w
        return max(abs(a % w - b % w), abs(a // w - b // w))

    def manhattan(self, a: int, b: int) -> int:
        w = self.w
        return abs(a % w - b % w) + abs(a // w - b // w)

    def radius(self, i: int, r: int) -> list:
        w, h = self.w, self.h
        x, y = i % w, i // w
        return [yy * w + xx
                for yy in range(max(0, y - r), min(h, y + r + 1))
                for xx in range(max(0, x - r), min(w, x + r + 1))]

    def passable(self, i: int) -> bool:
        return self.terrain[i] in PASSABLE

    # -- relations --------------------------------------------------------
    def at_peace(self, a: str, b: str) -> bool:
        if a == b:
            return True
        return ((a, b) if a < b else (b, a)) in self.treaty_pairs

    def hostile(self, a: str, b: str) -> bool:
        return a != b and not self.at_peace(a, b)

    def hostile_units_on(self, pid: str, i: int) -> bool:
        return any(self.hostile(pid, q) for q in self.armies.get(i, {}))

    def enemy_units_at(self, i: int, pid: str | None = None) -> dict:
        """Units of players hostile to ``pid`` (default: me) on tile ``i``."""
        pid = pid or self.me
        out: dict = {}
        for q, u in self.armies.get(i, {}).items():
            if self.hostile(pid, q):
                out = add_units(out, u)
        return out

    def adjacent_to(self, pid: str, i: int, extra: set | None = None) -> bool:
        own = self.owner
        for j in self.nb[i]:
            if own[j] == pid or (extra is not None and j in extra):
                return True
        return False

    def can_enter_fn(self, pid: str):
        """Predicate: may ``pid`` move units onto tile i (ignoring combat)."""
        terrain, owner, armies = self.terrain, self.owner, self.armies
        partners = {q for q in self.players if q != pid and self.at_peace(pid, q)}

        def ok(i: int) -> bool:
            if terrain[i] not in PASSABLE:
                return False
            if partners:
                o = owner[i]
                if o in partners:
                    return False
                per = armies.get(i)
                if per and any(q in partners for q in per):
                    return False
            return True
        return ok

    # -- path finding -----------------------------------------------------
    def bfs(self, sources, can_enter=None, max_dist: int | None = None, pass_sources: bool = True) -> dict:
        """Multi-source BFS distances (4-directional) over enterable tiles."""
        if can_enter is None:
            terrain = self.terrain

            def can_enter(i):
                return terrain[i] in PASSABLE
        dist = {}
        dq = deque()
        for s in sources:
            if s not in dist:
                dist[s] = 0
                dq.append(s)
        nb = self.nb
        while dq:
            i = dq.popleft()
            d = dist[i] + 1
            if max_dist is not None and d > max_dist:
                continue
            for j in nb[i]:
                if j not in dist and can_enter(j):
                    dist[j] = d
                    dq.append(j)
        return dist

    def step_towards(self, src: int, dist: dict, can_enter=None) -> int | None:
        """Neighbour of ``src`` that is one step closer in ``dist`` (a BFS
        map computed *from the target*). None if no progress possible."""
        here = dist.get(src)
        best = None
        for j in self.nb[src]:
            d = dist.get(j)
            if d is None or (can_enter is not None and not can_enter(j)):
                continue
            if (here is None or d < here) and (best is None or d < dist[best] or (d == dist[best] and j < best)):
                best = j
        return best

    # -- per player summaries --------------------------------------------
    def cities_of(self, pid: str) -> list:
        return [i for i, c in self.cities.items() if c["owner"] == pid]

    def capital_of(self, pid: str) -> int | None:
        """Tile of ``pid``'s original capital if they still hold it."""
        for i, c in self.cities.items():
            if c.get("capital") and c.get("original_owner") == pid and c["owner"] == pid:
                return i
        return None

    def armies_of(self, pid: str) -> dict:
        return {i: per[pid] for i, per in self.armies.items() if pid in per}

    def progress(self, pid: str) -> dict:
        return (self.players.get(pid) or {}).get("victory_progress", {}) or {}


# ---------------------------------------------------------------------------
# Combat estimation (mirrors agentciv.engine.combat exactly)
# ---------------------------------------------------------------------------
def simulate_attack(world: World, attacker: str, att_units: dict, tile: int,
                    extra_defenders: dict | None = None, defenders_override: dict | None = None,
                    assume_war: bool = False) -> tuple:
    """Resolve an attack by ``att_units`` of ``attacker`` on ``tile`` with the
    engine's battle procedure (defenders = everything hostile to the attacker
    currently on the tile, plus the city garrison/walls/archer bonus and
    terrain). ``defenders_override`` ({player: units}) replaces the units
    currently on the tile. Returns ``(win, survivors, ratio)`` where ``ratio``
    is attacker power / weakest-margin defender power (>1: attacker stronger).
    With ``assume_war`` treaty partners are treated as hostile (to evaluate
    an attack after breaking a treaty)."""
    def hostile_to_attacker(q):
        return q != attacker and (assume_war or world.hostile(attacker, q))

    city = world.cities.get(tile)
    cown = city["owner"] if city else None
    defenders: dict = {}
    source = world.armies.get(tile, {}) if defenders_override is None else defenders_override
    for q, u in source.items():
        if hostile_to_attacker(q):
            defenders[q] = dict(u)
    if extra_defenders:
        for q, u in extra_defenders.items():
            defenders[q] = add_units(defenders.get(q, {}), u)
    if cown is not None and hostile_to_attacker(cown):
        defenders.setdefault(cown, {})
    if not defenders:
        return True, dict(att_units), float("inf")
    defensive = world.terrain[tile] in DEFENSIVE
    walls = city["buildings"].get("walls", 0) if city else 0
    sides = []
    for k, (q, u) in enumerate(sorted(defenders.items())):
        is_owner = q == cown
        sides.append(CB.Side(pid=q, units={a: c for a, c in u.items() if c > 0}, defender=True,
                             city_owner=is_owner,
                             garrison=float(city.get("garrison", 0)) if is_owner and city else 0.0,
                             terrain_bonus=defensive, order=(0, k)))
    att = CB.Side(pid=attacker, units={a: c for a, c in att_units.items() if c > 0}, order=(1, 0))
    ratio = float("inf")
    for s in sides:
        if s.count() > 0 or s.garrison > 0:
            pa = CB.power(att, s, walls)
            pd = CB.power(s, att, walls)
            ratio = min(ratio, pa / pd if pd > 0 else float("inf"))
    sides.append(att)

    def hostile(a, b):
        if a == b:
            return False
        if a == attacker or b == attacker:
            return True
        return world.hostile(a, b)
    CB.resolve(sides, hostile, walls=walls)
    win = att.alive and not any(s.alive and s.pid != attacker for s in sides[:-1])
    return win, dict(att.units), ratio


def defense_power(world: World, tile: int, owner: str, units: dict, vs: dict) -> float:
    """Engine power of ``owner``'s defence on ``tile`` against units ``vs``."""
    city = world.cities.get(tile)
    is_owner = city is not None and city["owner"] == owner
    side = CB.Side(pid=owner, units=dict(units), defender=True, city_owner=is_owner,
                   garrison=float(city.get("garrison", 0)) if is_owner else 0.0,
                   terrain_bonus=world.terrain[tile] in DEFENSIVE)
    enemy = CB.Side(pid="x", units=dict(vs) or {"infantry": 1})
    walls = city["buildings"].get("walls", 0) if city else 0
    return CB.power(side, enemy, walls)


def threat_to(world: World, tile: int, pid: str | None = None, reach: int = 3) -> dict:
    """Enemy units (per hostile player) that could reach ``tile`` within
    ``reach`` turns (cavalry move 2 per turn). Returns {player: units}."""
    pid = pid or world.me
    out: dict = {}
    dist = world.bfs([tile], max_dist=2 * reach)
    for i, per in world.armies.items():
        d = dist.get(i)
        if d is None:
            continue
        for q, u in per.items():
            if q == pid or not world.hostile(pid, q):
                continue
            got = {}
            for t, c in u.items():
                mv = C.UNITS[t]["move"] if t in C.UNITS else 1
                if d <= reach * mv:
                    got[t] = c
            if got:
                out[q] = add_units(out.get(q, {}), got)
    return out


def best_counter(enemy_units: dict, allowed=("infantry", "archer", "cavalry")) -> str:
    """Unit type that maximises power per resource against ``enemy_units``."""
    best, best_v = "infantry", -1.0
    for t in allowed:
        m = CB.counter_multiplier(t, enemy_units) if enemy_units else 1.0
        cost = sum(unit_cost(t).values())
        v = C.UNITS[t]["strength"] * m / cost
        # vulnerability: how strongly the enemy counters us
        worse = 1.0
        tot = total_units(enemy_units)
        if tot:
            k = enemy_units.get(COUNTERED_BY.get(t, ""), 0)
            worse = 1.0 + 0.5 * k / tot
        v /= worse
        if v > best_v:
            best, best_v = t, v
    return best


# ---------------------------------------------------------------------------
# Economy helpers
# ---------------------------------------------------------------------------
def tile_yield(world: World, i: int, improvement: str | None = "__current__") -> dict:
    """Per-turn (un-seasoned) yield of tile ``i`` if owned (optionally with a
    hypothetical improvement)."""
    if i in world.cities:
        y = dict(C.CITY_YIELD)
        c = world.cities[i]
        if c.get("capital") and c.get("original_owner") == c.get("owner"):
            pass
        return y
    t = world.terrain[i]
    spec = C.TERRAIN.get(t)
    if spec is None or not spec["passable"]:
        return {}
    y = dict(spec["yield"])
    b = world.improvement.get(i) if improvement == "__current__" else improvement
    if b:
        for r, v in C.IMPROVEMENTS[b]["bonus"].items():
            y[r] = y.get(r, 0) + v
    dep = C.DEPOSITS.get(t)
    if dep is not None:
        rem = world.deposit.get(i, dep[1])
        if rem <= 0:
            y[dep[0]] = 0
    if i in world.relic_set:
        y["influence"] = y.get("influence", 0) + C.RELIC_INFLUENCE
    return {r: v for r, v in y.items() if v}


def deposit_turns(world: World, i: int, per_turn: int) -> float:
    dep = C.DEPOSITS.get(world.terrain[i])
    if dep is None or per_turn <= 0:
        return float("inf")
    return world.deposit.get(i, dep[1]) / per_turn


def value_of(amounts: dict, weights: dict) -> float:
    return sum(v * weights.get(r, 1.0) for r, v in amounts.items())


def best_improvement(world: World, i: int, weights: dict, allow_temple: bool = True,
                     horizon: float = 60.0) -> tuple:
    """(building, gain value per turn, cost value) of the best improvement
    for an owned, unimproved tile. Deposits shorten the useful life of
    quarries/mines, which is accounted for over ``horizon`` turns."""
    t = world.terrain[i]
    best = (None, 0.0, 1.0)
    best_roi = 0.0
    for b, spec in C.IMPROVEMENTS.items():
        if t not in spec["terrain"] or (b == "temple" and not allow_temple):
            continue
        bonus = spec["bonus"]
        gain = value_of(bonus, weights)
        dep = C.DEPOSITS.get(t)
        if dep is not None and dep[0] in bonus:
            rem = world.deposit.get(i, dep[1])
            base = C.TERRAIN[t]["yield"].get(dep[0], 0)
            life_with = rem / max(1, base + bonus[dep[0]])
            # extra output over the horizon relative to not improving
            extra = min(rem, (base + bonus[dep[0]]) * min(horizon, life_with)) - min(rem, base * horizon)
            gain = max(0.0, extra / horizon) * weights.get(dep[0], 1.0)
        cost = value_of(spec["cost"], weights)
        roi = gain / cost if cost else 0.0
        if roi > best_roi:
            best, best_roi = (b, gain, cost), roi
    return best


def raw_income(world: World, pid: str | None = None) -> dict:
    """Un-seasoned per-turn income of ``pid`` from its tiles (engine rules)."""
    pid = pid or world.me
    inc = {r: 0 for r in C.RESOURCES}
    for i, o in enumerate(world.owner):
        if o != pid:
            continue
        city = world.cities.get(i)
        if city is not None:
            for r, v in C.CITY_YIELD.items():
                inc[r] += v
            if city.get("capital"):
                inc["influence"] += C.CAPITAL_EXTRA_INFLUENCE
            if city["buildings"].get("market_hall"):
                inc["gold"] += C.MARKET_HALL_GOLD
            continue
        for r, v in tile_yield(world, i).items():
            inc[r] = inc.get(r, 0) + v
    return inc


def season_mods(turn: int) -> dict:
    idx = (turn // C.SEASON_LENGTH) % len(C.SEASONS)
    return C.SEASONS[idx][1]


def food_projection(world: World, raw_food: int, upkeep: int, turns: int, start: int | None = None) -> int:
    """Minimum food stock over the next ``turns`` turns (no spending)."""
    food = world.res.get("food", 0) if start is None else start
    cap = world.caps.get("food", C.STORAGE_BASE)
    low = food
    for k in range(turns):
        mod = season_mods(world.turn + k).get("food", 1.0)
        food = min(cap, food + int(math.floor(raw_food * mod + 1e-9)) - upkeep)
        low = min(low, food)
    return low


def sell_price(world: World, resource: str, qty: int) -> float:
    """Average execution price if we alone sell ``qty`` this turn."""
    pool = world.pools.get(resource)
    if not pool or qty <= 0:
        return pool[1] / pool[0] if pool else 0.0
    return MK.auction_price(pool[0], pool[1], -qty)


def buy_price(world: World, resource: str, qty: int) -> float:
    pool = world.pools.get(resource)
    if not pool:
        return float("inf")
    if qty >= pool[0] * C.MARKET_MAX_NET_FRACTION:
        return float("inf")
    return MK.auction_price(pool[0], pool[1], qty)


def base_price(resource: str) -> float:
    a, g = C.MARKET_POOLS_PER_PLAYER[resource]
    return g / a


def max_order(world: World, resource: str) -> int:
    pool = world.pools.get(resource)
    return int(pool[0] * C.MARKET_MAX_ORDER_FRACTION) if pool else 0


# ---------------------------------------------------------------------------
# Plan: orders + budget
# ---------------------------------------------------------------------------
class Plan:
    """Accumulates orders while tracking the resources they will consume.

    Only actions that pass the engine's pre-validation rules are emitted
    (the checks mirror agentciv.engine.orders)."""

    def __init__(self, world: World):
        self.w = world
        self.orders: list = []
        self.budget = dict(world.res)
        self.claimed: set = set()          # tiles claimed this turn
        self.improved: set = set()
        self.city_builds: dict = {}         # (tile, building) -> count this turn
        self.wonder = False
        self.settles: list = []
        self.moved: dict = {}               # (tile, unit) -> count moved
        self.recruited: dict = {}           # tile -> units queued
        self.proposed: set = set()
        self.accepted: set = set()
        self.messages = 0
        self.tiles = len(world.my_tiles)    # owned tiles incl. planned claims
        self.cities = len(world.my_cities)
        self.market_gold = 0                # gold expected from sells
        self.sold: dict = {}
        self.bought: dict = {}

    # -- budget -----------------------------------------------------------
    def can(self, cost: dict, reserve: dict | None = None) -> bool:
        b = self.budget
        for r, v in cost.items():
            keep = reserve.get(r, 0) if reserve else 0
            if b.get(r, 0) - keep < v:
                return False
        return True

    def pay(self, cost: dict) -> None:
        for r, v in cost.items():
            self.budget[r] = self.budget.get(r, 0) - v

    def full(self) -> bool:
        return len(self.orders) >= C.MAX_ORDERS_PER_TURN - 2

    # -- actions ------------------------------------------------------------
    def owns(self, i: int) -> bool:
        return self.w.owner[i] == self.w.me or i in self.claimed

    def claim(self, i: int) -> bool:
        w = self.w
        if self.full() or i in self.claimed or i in self.settles or not w.passable(i) or w.owner[i] is not None:
            return False
        if not w.adjacent_to(w.me, i, self.claimed):
            return False
        if w.hostile_units_on(w.me, i):
            return False
        cost = {"influence": claim_cost(self.tiles)}
        if not self.can(cost):
            return False
        self.pay(cost)
        self.claimed.add(i)
        self.tiles += 1
        self.orders.append({"type": "claim", "at": w.xy(i)})
        return True

    def improve(self, i: int, building: str) -> bool:
        w = self.w
        spec = C.IMPROVEMENTS.get(building)
        if self.full() or spec is None or not self.owns(i) or i in w.cities:
            return False
        if w.terrain[i] not in spec["terrain"] or i in w.improvement or i in self.improved:
            return False
        if not self.can(spec["cost"]):
            return False
        self.pay(spec["cost"])
        self.improved.add(i)
        self.orders.append({"type": "build", "at": w.xy(i), "building": building})
        return True

    def city_level(self, i: int, building: str) -> int:
        c = self.w.cities.get(i)
        if c is None:
            return 0
        base = c.get("wonder_stage", 0) if building == "wonder" else c["buildings"].get(building, 0)
        return base + self.city_builds.get((i, building), 0)

    def city_build_cost(self, i: int, building: str) -> dict | None:
        lvl = self.city_level(i, building)
        if lvl >= C.CITY_BUILDINGS[building]["max"]:
            return None
        return building_cost(building, lvl + 1)

    def build_city(self, i: int, building: str, reserve: dict | None = None) -> bool:
        w = self.w
        c = w.cities.get(i)
        if self.full() or c is None or c["owner"] != w.me or building not in C.CITY_BUILDINGS:
            return False
        if building == "wonder":
            if self.wonder:
                return False
            wc = wonder_city(w)
            if wc is not None and wc != i:
                return False
        cost = self.city_build_cost(i, building)
        if cost is None or not self.can(cost, reserve):
            return False
        self.pay(cost)
        self.city_builds[(i, building)] = self.city_builds.get((i, building), 0) + 1
        if building == "wonder":
            self.wonder = True
        self.orders.append({"type": "build", "at": w.xy(i), "building": building})
        return True

    def can_settle_at(self, i: int) -> bool:
        w = self.w
        if not w.passable(i) or i in w.relic_set or i in w.cities:
            return False
        o = w.owner[i]
        if o is None:
            if not (w.adjacent_to(w.me, i, self.claimed) or i in self.claimed):
                return False
        elif o != w.me:
            return False
        for c in w.cities:
            if w.cheb(i, c) < C.CITY_MIN_DISTANCE:
                return False
        for s in self.settles:
            if w.cheb(i, s) < C.CITY_MIN_DISTANCE:
                return False
        if w.hostile_units_on(w.me, i):
            return False
        return True

    def settle(self, i: int) -> bool:
        if self.full() or not self.can_settle_at(i):
            return False
        cost = settle_cost(self.cities)
        if not self.can(cost):
            return False
        self.pay(cost)
        self.settles.append(i)
        self.cities += 1
        # the new city takes the unowned tiles around it: later claims this
        # turn will cost more
        w = self.w
        self.tiles += sum(1 for j in w.radius(i, C.CITY_CLAIM_RADIUS)
                          if (w.owner[j] is None and j not in self.claimed and w.passable(j)))
        self.orders.append({"type": "settle", "at": self.w.xy(i)})
        return True

    def recruit(self, i: int, unit: str, count: int = 1, reserve: dict | None = None) -> int:
        """Recruit up to ``count`` units (as many as affordable). Returns the
        number actually ordered."""
        w = self.w
        c = w.cities.get(i)
        if self.full() or c is None or c["owner"] != w.me or unit not in C.UNITS or count <= 0:
            return 0
        per = unit_cost(unit)
        n = min(count, C.MAX_RECRUIT_PER_ORDER)
        for r, v in per.items():
            avail = self.budget.get(r, 0) - (reserve.get(r, 0) if reserve else 0)
            n = min(n, int(avail // v) if v else n)
        if n <= 0:
            return 0
        self.pay(unit_cost(unit, n))
        q = self.recruited.setdefault(i, {})
        q[unit] = q.get(unit, 0) + n
        self.orders.append({"type": "recruit", "city": w.xy(i), "unit": unit, "count": n})
        return n

    def disband(self, i: int, units: dict) -> bool:
        have = self.w.my_armies.get(i, {})
        req = {u: min(c, have.get(u, 0) - self.moved.get((i, u), 0)) for u, c in units.items()}
        req = {u: c for u, c in req.items() if c > 0}
        if not req or self.full():
            return False
        for u, c in req.items():
            self.moved[(i, u)] = self.moved.get((i, u), 0) + c
        self.orders.append({"type": "disband", "at": self.w.xy(i), "units": req})
        return True

    # -- movement ---------------------------------------------------------
    def available(self, i: int) -> dict:
        """My units on tile i not yet given a move/disband order this turn."""
        have = self.w.my_armies.get(i, {})
        out = {u: c - self.moved.get((i, u), 0) for u, c in have.items()}
        return {u: c for u, c in out.items() if c > 0}

    def move(self, src: int, path: list, units: dict | None = None) -> bool:
        """Move ``units`` (default: all available) from ``src`` along ``path``
        (1 step, or 2 steps for pure cavalry). Validates like the engine."""
        w = self.w
        if self.full() or not path:
            return False
        avail = self.available(src)
        req = dict(avail) if units is None else {u: min(c, avail.get(u, 0)) for u, c in units.items()}
        req = {u: c for u, c in req.items() if c > 0}
        if not req:
            return False
        if len(path) > 2 or (len(path) == 2 and any(u != "cavalry" for u in req)):
            return False
        enter = w.can_enter_fn(w.me)
        prev = src
        for k, step in enumerate(path):
            if step not in w.nb[prev] or not enter(step):
                return False
            if len(path) == 2 and k == 0 and w.hostile_units_on(w.me, step):
                return False
            prev = step
        for u, c in req.items():
            self.moved[(src, u)] = self.moved.get((src, u), 0) + c
        self.orders.append({"type": "move", "from": w.xy(src), "path": [w.xy(s) for s in path], "units": req})
        return True

    # -- market -----------------------------------------------------------
    def sell(self, resource: str, qty: int, limit: float | None = None) -> int:
        """Sell up to ``qty`` (bounded by stock and the per-order cap). Adds
        the conservatively estimated proceeds to the gold budget."""
        w = self.w
        qty = min(int(qty), int(self.budget.get(resource, 0)), max_order(w, resource) - self.sold.get(resource, 0))
        if qty < 1 or self.full():
            return 0
        price = sell_price(w, resource, qty + self.sold.get(resource, 0))
        order = {"type": "market", "side": "sell", "resource": resource, "qty": qty}
        if limit is not None:
            if price < limit:
                return 0
            order["limit"] = round(limit, 4)
        gold = int(qty * price * (1 - w.fee) * 0.97)
        self.budget[resource] -= qty
        self.budget["gold"] = self.budget.get("gold", 0) + gold
        self.market_gold += gold
        self.sold[resource] = self.sold.get(resource, 0) + qty
        self.orders.append(order)
        return qty

    def buy(self, resource: str, qty: int, max_price: float | None = None) -> int:
        """Buy up to ``qty`` if affordable at (a padded) estimated price."""
        w = self.w
        qty = min(int(qty), max_order(w, resource) - self.bought.get(resource, 0))
        if qty < 1 or self.full():
            return 0
        price = buy_price(w, resource, qty + self.bought.get(resource, 0))
        while qty > 0 and (price * (1 + w.fee) * 1.08 * qty > self.budget.get("gold", 0)
                           or (max_price is not None and price > max_price)):
            qty = int(qty * 0.7)
            price = buy_price(w, resource, qty + self.bought.get(resource, 0))
        if qty < 1:
            return 0
        limit = price * 1.08
        if max_price is not None:
            limit = min(limit, max_price)
        cost = int(math.ceil(qty * limit * (1 + w.fee)))
        self.budget["gold"] = self.budget.get("gold", 0) - cost
        self.budget[resource] = self.budget.get(resource, 0) + qty
        self.bought[resource] = self.bought.get(resource, 0) + qty
        self.orders.append({"type": "market", "side": "buy", "resource": resource, "qty": qty,
                            "limit": round(limit, 4)})
        return qty

    # -- diplomacy ----------------------------------------------------------
    def propose(self, to: str, turns: int) -> bool:
        w = self.w
        if self.full() or to in self.proposed or to == w.me or to not in w.alive or to in w.treaties:
            return False
        for pr in w.view.get("treaty_proposals", []):
            # a proposal either way made last turn may be accepted right now
            if {pr.get("from"), pr.get("to")} == {w.me, to}:
                return False
        turns = max(C.TREATY_MIN_TURNS, min(C.TREATY_MAX_TURNS, int(turns)))
        self.proposed.add(to)
        self.orders.append({"type": "propose_treaty", "to": to, "turns": turns})
        return True

    def accept_treaty(self, frm: str) -> bool:
        if self.full() or frm in self.accepted or frm in self.w.treaties:
            return False
        self.accepted.add(frm)
        self.orders.append({"type": "accept_treaty", "from": frm})
        return True

    def message(self, to: str, text: str) -> bool:
        if self.full() or self.messages >= C.MAX_MESSAGES_PER_TURN or not text:
            return False
        self.messages += 1
        self.orders.append({"type": "message", "to": to, "text": text[:C.MAX_MESSAGE_LENGTH]})
        return True


def wonder_city(world: World) -> int | None:
    """Tile of my city hosting a wonder in progress (if any)."""
    for i in world.my_cities:
        if world.cities[i].get("wonder_stage", 0) > 0:
            return i
    return None


def treaty_proposals_to_me(world: World) -> list:
    return [p for p in world.view.get("treaty_proposals", []) if p.get("to") == world.me]


# ---------------------------------------------------------------------------
# Safe bot base
# ---------------------------------------------------------------------------
class SafeBot(Bot):
    """Bot whose :meth:`act` never raises: subclasses implement
    :meth:`decide`; any exception yields the orders gathered so far (or an
    empty list). A per-bot ``random.Random`` seeded from ``seed`` is
    available as ``self.rng``."""

    name = "safe"

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        self.rng = random.Random(f"{self.name}:{seed}")
        self.memory: dict = {}
        self.last_error: str | None = None
        self._plan: Plan | None = None

    def act(self, view: dict) -> list:
        self._plan = None
        try:
            if not view or not view.get("you") or not view["you"].get("alive", True):
                return []
            if view.get("status") not in (None, "running"):
                return []
            out = self.decide(view)
            return list(out or [])[:C.MAX_ORDERS_PER_TURN]
        except Exception as e:  # never let a bot crash the game
            self.last_error = f"{type(e).__name__}: {e}"
            if self._plan is not None:
                return list(self._plan.orders)[:C.MAX_ORDERS_PER_TURN]
            return []

    def new_plan(self, world: World) -> Plan:
        self._plan = Plan(world)
        return self._plan

    def decide(self, view: dict) -> list:  # pragma: no cover - abstract
        raise NotImplementedError
