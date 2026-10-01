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
* Deals – :class:`DealValuer` values §13 deal terms (resources by need and
  market prices, tiles, contracts by reliability, peace by threat) for any
  player; :func:`danger` / :meth:`DealValuer.helps_winner` implement the
  "never help a player close to winning" guard.
* :class:`SafeBot` – a Bot base class whose ``act`` and ``negotiate`` never
  raise.
"""
from __future__ import annotations

import math
import random
from collections import deque

from agentciv.engine import combat as CB
from agentciv.engine import constants as C
from agentciv.engine import market as MK
from agentciv.engine.rules import bank_limit as _bank_limit
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
        self.treaty_bonds = {}            # pair -> {pid: bond}
        for t in view.get("treaties", []):
            a, b = t["a"], t["b"]
            self.treaty_bonds[(a, b) if a < b else (b, a)] = dict(t.get("bond") or {})
        self.cooldowns = {}               # pair -> first turn the pair may sign again
        self.notice_until = {}            # pair -> last movement-restricted turn after a break
        for c in view.get("treaty_cooldowns", []) or []:
            a, b = c["a"], c["b"]
            self.cooldowns[(a, b) if a < b else (b, a)] = c["until_turn"]
            if c.get("notice_until") is not None:
                self.notice_until[(a, b) if a < b else (b, a)] = c["notice_until"]
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

    def break_notice(self, a: str, b: str) -> bool:
        """Still movement-restricted after a treaty break (rules §9)?"""
        key = (a, b) if a < b else (b, a)
        if key in self.notice_until:
            return self.turn <= self.notice_until[key]
        until = self.cooldowns.get(key)
        return until is not None and self.turn - (until - C.TREATY_RESIGN_COOLDOWN) <= C.TREATY_BREAK_NOTICE

    def hostile(self, a: str, b: str) -> bool:
        return a != b and not self.at_peace(a, b)

    # -- treaty limits (RULES §9) ------------------------------------------
    def treaties_held(self, q: str) -> int:
        return sum(1 for k in self.treaty_pairs if q in k)

    def treaty_slots(self, q: str) -> int:
        return max(1, math.ceil((len(self.alive) - 1) / C.TREATY_SLOT_DIVISOR))

    def betrayals(self, q: str) -> int:
        return int((self.players.get(q) or {}).get("betrayals", 0) or 0)

    def streaking(self, q: str) -> bool:
        """Is ``q`` on an economic or influence streak (public)? A treaty with
        such a partner can be broken for free (rules §9)."""
        pl = self.players.get(q) or {}
        return bool(pl.get("economic_streak") or pl.get("influence_streak"))

    def break_influence(self, q: str | None = None, partner: str | None = None) -> int:
        if partner is not None and self.streaking(partner):
            return 0
        return C.TREATY_BREAK_COST * (1 + self.betrayals(q or self.me))

    def bond_required(self, q: str) -> int:
        return C.TREATY_BOND_PER_BETRAYAL * self.betrayals(q)

    def bond_free(self, q: str) -> int:
        bank = int((self.players.get(q) or {}).get("bank", 0) or 0)
        return max(0, bank - sum(b.get(q, 0) for k, b in self.treaty_bonds.items() if q in k))

    def sign_problem(self, a: str, b: str, extra: dict | None = None) -> str | None:
        key = (a, b) if a < b else (b, a)
        if self.cooldowns.get(key, -1) > self.turn:
            return "cooldown"
        renewal = key in self.treaty_pairs
        for q in (a, b):
            if not renewal and self.treaties_held(q) >= self.treaty_slots(q):
                return "slots"
            if self.bond_required(q) + (extra or {}).get(q, 0) > self.bond_free(q):
                return "bond"
        return None

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
        partners = {q for q in self.players if q != pid and (self.at_peace(pid, q) or self.break_notice(pid, q))}

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
        y["influence"] = y.get("influence", 0) + C.RELIC_INFLUENCE      # guarded rate
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
        self.pledged = 0                    # bank pledged on treaties ordered this turn
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
        if i in w.relic_set:            # relics are taken by occupation only
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
            if len(path) == 2 and k == 0:
                city = w.cities.get(step)
                if w.hostile_units_on(w.me, step) or (city is not None and w.hostile(w.me, city["owner"])):
                    return False  # hostile army or hostile city: no passing through
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
    def propose(self, to: str, turns: int, bond: int = 0) -> bool:
        w = self.w
        if self.full() or to in self.proposed or to == w.me or to not in w.alive or to in w.treaties:
            return False
        for pr in w.view.get("treaty_proposals", []):
            # a proposal either way made last turn may be accepted right now
            if {pr.get("from"), pr.get("to")} == {w.me, to}:
                return False
        if w.sign_problem(w.me, to, {w.me: bond + self.pledged}):
            return False
        if w.treaties_held(w.me) + len(self.proposed) + len(self.accepted) >= w.treaty_slots(w.me):
            return False
        turns = max(C.TREATY_MIN_TURNS, min(C.TREATY_MAX_TURNS, int(turns)))
        self.proposed.add(to)
        o = {"type": "propose_treaty", "to": to, "turns": turns}
        if bond > 0:
            o["bond"] = int(bond)
            self.pledged += int(bond)
        self.orders.append(o)
        return True

    def accept_treaty(self, frm: str, bond: int = 0) -> bool:
        w = self.w
        if self.full() or frm in self.accepted or frm in w.treaties:
            return False
        if w.sign_problem(w.me, frm, {w.me: bond + self.pledged}):
            return False
        if w.treaties_held(w.me) + len(self.accepted) >= w.treaty_slots(w.me):
            return False
        self.accepted.add(frm)
        o = {"type": "accept_treaty", "from": frm}
        if bond > 0:
            o["bond"] = int(bond)
            self.pledged += int(bond)
        self.orders.append(o)
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
    """Proposals to me, strongest proposer (then largest bond) first: with
    few treaty slots the most dangerous neighbours get them."""
    out = [p for p in world.view.get("treaty_proposals", []) if p.get("to") == world.me]
    mp = lambda q: float((world.players.get(q) or {}).get("military_power", 0) or 0)
    return sorted(out, key=lambda p: (-mp(p.get("from")), -int(p.get("bond", 0) or 0), str(p.get("from"))))


def rivals_by_power(world: World) -> list:
    mp = lambda q: float((world.players.get(q) or {}).get("military_power", 0) or 0)
    return sorted(world.rivals, key=lambda q: (-mp(q), q))


def break_cost(world: World, q: str) -> tuple:
    """(influence needed, gold-equivalent cost) of breaking our treaty with
    ``q``: the influence, the legacy loss (dear on the influence path), our
    bond on the treaty, the tribute ``q`` still owes us under contracts that
    the break cancels, the bank fee on an offered bond, and dearer treaties
    afterwards."""
    w = world
    me = w.players.get(w.me) or {}
    if w.streaking(q):
        return 0, 0.0          # free break: the partner is on a victory streak
    b = w.betrayals(w.me)
    infl = C.TREATY_BREAK_COST * (1 + b)
    pct = min(C.TREATY_BREAK_MAX_PCT, C.TREATY_BREAK_PCT * (1 + b))
    legacy = int(me.get("legacy", 0) or 0) * pct // 100
    lw = 1.0 + 4.0 * float(w.progress(w.me).get("influence", 0) or 0)
    key = (w.me, q) if w.me < q else (q, w.me)
    bond = int((w.treaty_bonds.get(key) or {}).get(w.me, 0) or 0)
    # the offered part of the bond leaves the bank for 1 influence per 2 gold (§9)
    fee = -(-max(0, bond - C.TREATY_BOND_PER_BETRAYAL * b) // C.CONTRACT_DEFAULT_GOLD_PER_INFLUENCE)
    tribute = 0.0
    for c in w.view.get("contracts", []) or []:
        if c.get("payer") == q and c.get("payee") == w.me:
            tribute += sum(v * (1.0 if r == "gold" else 0.8) for r, v in (c.get("per_turn") or {}).items()) \
                * int(c.get("turns_left", 0) or 0)
    later = 2 * C.TREATY_BOND_PER_BETRAYAL  # dearer treaties afterwards
    bank = int(me.get("bank", 0) or 0) * pct // 100
    bw = 1.0 + 2.0 * float(w.progress(w.me).get("economic", 0) or 0)
    return infl, 2.0 * (infl + fee) + lw * legacy + bw * bank + bond + tribute + later


# ---------------------------------------------------------------------------
# Deal valuation (docs/DESIGN.md §13)
# ---------------------------------------------------------------------------
PROGRESS_KEYS = ("conquest", "wonder", "influence", "relics", "economic")
GOLD_NEED_PREMIUM = 0.15        # gold we need for a plan/investment is worth more than 1
THREAT_REACH = 3                # turns of marching considered by peace valuation
CREDIT_BASE = 150.0             # credit limit (gold PV) of a payer with no contract history
COLLATERAL_WEIGHT = 0.5         # share of a payer's bank added to its credit limit (seized on default)
SAFE_PEACE_TURNS = 10           # a received tile near a partner's army is safe for this long a treaty


def spot_prices(world: World) -> dict:
    """Current market spot price of every tradable resource (gold = 1)."""
    out = {"gold": 1.0}
    for r in C.MARKET_RESOURCES:
        pool = world.pools.get(r)
        out[r] = pool[1] / pool[0] if pool and pool[0] > 0 else base_price(r)
    return out


def annuity(discount: float, turns: int) -> float:
    """Σ discount^k for k < turns (present value of 1 per turn)."""
    if turns <= 0:
        return 0.0
    if discount >= 0.99999:
        return float(turns)
    return (1.0 - discount ** turns) / (1.0 - discount)


def danger(world: World, q: str) -> float:
    """How close ``q`` is to winning: its highest public victory progress
    (0..1) over the real conditions (not the turn-limit score). Conquest
    counts only once a rival capital has been taken (everyone starts with
    its own)."""
    prog = world.progress(q)
    out = max((float(prog.get(k, 0.0) or 0.0) for k in PROGRESS_KEYS if k != "conquest"), default=0.0)
    if int((world.players.get(q) or {}).get("capitals_held", 0) or 0) >= 2:
        out = max(out, float(prog.get("conquest", 0.0) or 0.0))
    return out


def bank_limit(world: World, pid: str | None = None) -> int:
    """Gold ``pid`` (default: me) may bank per turn: ``you.bank_limit`` for
    myself, else computed from its cities and market halls."""
    pid = pid or world.me
    if pid == world.me and world.you.get("bank_limit") is not None:
        return int(world.you["bank_limit"])
    n = h = 0
    for c in world.cities.values():
        if c.get("owner") == pid:
            n += 1
            if (c.get("buildings") or {}).get("market_hall"):
                h += 1
    return _bank_limit(n, h)


def bank_of(world: World, pid: str | None = None) -> int:
    """Public bank of ``pid`` (default: me)."""
    return int((world.players.get(pid or world.me) or {}).get("bank", 0) or 0)


def contract_obligations(world: World, pid: str | None = None) -> dict:
    """Per-turn amounts ``pid`` (default: me) pays under running contracts."""
    pid = pid or world.me
    out: dict = {}
    for c in world.view.get("contracts", []) or []:
        if c.get("payer") == pid:
            for r, v in (c.get("per_turn") or {}).items():
                out[r] = out.get(r, 0) + int(v)
    return out


def contract_income(world: World, pid: str | None = None) -> dict:
    """Per-turn amounts ``pid`` (default: me) receives under running contracts."""
    pid = pid or world.me
    out: dict = {}
    for c in world.view.get("contracts", []) or []:
        if c.get("payee") == pid:
            for r, v in (c.get("per_turn") or {}).items():
                out[r] = out.get(r, 0) + int(v)
    return out


class DealValuer:
    """Gold-equivalent value of §13 deal terms for *any* player, from public
    information (resources, income, caps, armies, reputation, contracts).

    * **Resources** have a marginal value: the part a player *needs* (its
      stock is below its target) is worth what buying it on the market would
      cost (spot + slippage + fee); the rest is worth what selling it would
      bring (spot − slippage − fee). Giving away surplus therefore costs
      little, giving away what you need costs a lot — the room in between is
      what makes a trade good for both sides.
    * **Tiles**: yield over (part of) the remaining game.
    * **Contracts**: instalments discounted per turn and, for the receiver,
      weighted by the payer's reliability (reputation, ability to pay).
      ``horizon`` truncates *our own* payments (after an expected victory
      nothing is owed any more).
    * **Peace**: the threat the other side poses (its army within reach of
      our cities against our defence) plus a small base value; bots add a
      ``peace_bias`` per player (negative = we want to be free to attack).

    ``needs`` / ``gold_need`` describe our own targets (planner knowledge);
    other players' needs are estimated (keep levels, food deficit, the next
    wonder stage of a wonder builder)."""

    def __init__(self, world: World, needs: dict | None = None, gold_need: int = 0,
                 discount: float = 0.97, horizon: int | None = None,
                 peace_bias: dict | None = None, peace_scale: float = 1.0):
        self.w = world
        self.me = world.me
        self.prices = spot_prices(world)
        self.my_needs = needs
        self.my_gold_need = gold_need
        self.discount = discount
        self.horizon = horizon
        self.peace_bias = peace_bias or {}
        self.peace_scale = peace_scale
        self.remaining = max(1, world.max_turns - world.turn)
        self._threat: dict = {}
        self._city_dist: dict = {}
        self._needs: dict = {}

    # -- public state -----------------------------------------------------
    def stock(self, q: str) -> dict:
        if q == self.me:
            return self.w.res
        return (self.w.players.get(q) or {}).get("resources") or {}

    def income(self, q: str) -> dict:
        if q == self.me:
            return self.w.income
        return (self.w.players.get(q) or {}).get("income", {}) or {}

    def fee(self, q: str) -> float:
        if q == self.me:
            return self.w.fee
        hall = any(c["owner"] == q and c["buildings"].get("market_hall") for c in self.w.cities.values())
        return C.MARKET_HALL_FEE if hall else C.MARKET_FEE

    def caps(self, q: str) -> dict:
        if q == self.me:
            return self.w.caps
        wh = sum(1 for c in self.w.cities.values() if c["owner"] == q and c["buildings"].get("warehouse"))
        return {r: C.STORAGE_BASE + C.WAREHOUSE_STORAGE * wh for r in CAPPED}

    def needs(self, q: str) -> dict:
        """Stock of each capped resource ``q`` wants to keep on hand."""
        if q == self.me and self.my_needs is not None:
            return self.my_needs
        got = self._needs.get(q)
        if got is not None:
            return got
        pl = self.w.players.get(q) or {}
        upkeep = int(pl.get("upkeep", 0) or 0)
        inc = self.income(q)
        need = {"food": 30 + 4 * upkeep, "wood": 40, "stone": 30}
        if inc.get("food", 0) < upkeep:
            need["food"] += 8 * (upkeep - inc.get("food", 0))
        stage = int(pl.get("wonder_stage", 0) or 0)
        if 1 <= stage < C.WONDER_VICTORY_STAGE:
            # a wonder builder wants the next stage's stone/wood; above its
            # storage cap only when it can build this turn (else it is lost)
            cost = building_cost("wonder", stage + 1)
            stock = self.stock(q)
            short = sum(max(0, cost[r] - stock.get(r, 0)) * self.prices[r] for r in ("stone", "wood"))
            now = stock.get("gold", 0) >= cost["gold"] + short
            caps = self.caps(q)
            for r in ("stone", "wood"):
                need[r] = max(need[r], cost[r] if now else min(cost[r], caps[r]))
        self._needs[q] = need
        return need

    def gold_need(self, q: str) -> int:
        if q == self.me:
            return self.my_gold_need
        stage = int((self.w.players.get(q) or {}).get("wonder_stage", 0) or 0)
        if 1 <= stage < C.WONDER_VICTORY_STAGE:
            return building_cost("wonder", stage + 1)["gold"]
        return 0

    # -- resources --------------------------------------------------------
    def buy_unit(self, q: str, r: str, qty: int) -> float:
        """Gold per unit to buy ``qty`` of ``r`` on the market (with fee)."""
        p = buy_price(self.w, r, max(1, qty))
        if p == float("inf"):
            p = 3.0 * self.prices[r]
        return min(p, 3.0 * self.prices[r]) * (1 + self.fee(q))

    def sell_unit(self, q: str, r: str, qty: int) -> float:
        return sell_price(self.w, r, max(1, qty)) * (1 - self.fee(q))

    def recv_value(self, q: str, r: str, x: int) -> float:
        """Value for ``q`` of receiving ``x`` of resource ``r`` now."""
        if x <= 0:
            return 0.0
        have = self.stock(q).get(r, 0)
        if r == "gold":
            need = max(0, self.gold_need(q) - have)
            a = min(x, need)
            return a * (1 + GOLD_NEED_PREMIUM) + (x - a)
        need = max(0, self.needs(q).get(r, 0) - have)
        a = min(x, need)
        b = x - a
        v = a * self.buy_unit(q, r, a) if a else 0.0
        if b:
            v += b * self.sell_unit(q, r, b) * 0.97
        return v

    def give_cost(self, q: str, r: str, x: int) -> float:
        """Cost for ``q`` of handing over ``x`` of resource ``r`` now."""
        if x <= 0:
            return 0.0
        have = self.stock(q).get(r, 0)
        if r == "gold":
            free = max(0, have - self.gold_need(q))
            a = min(x, free)
            return a + (x - a) * (1 + GOLD_NEED_PREMIUM)
        free = max(0, have - self.needs(q).get(r, 0))
        a = min(x, free)
        b = x - a
        v = a * self.sell_unit(q, r, a) if a else 0.0
        if b:
            v += b * self.buy_unit(q, r, b) * 1.05
        return v

    # -- tiles --------------------------------------------------------------
    def tile_exposed(self, q: str, i: int, other: str | None = None, peace: int = 0) -> bool:
        """Could another player's army take tile ``i`` from ``q`` soon? Any
        army of a player not bound to ``q`` by a treaty (or by the deal's own
        ``peace`` with ``other``) for another ``SAFE_PEACE_TURNS`` turns that
        can march onto the tile within ``THREAT_REACH`` turns (§7.4:
        undefended land is captured by moving onto it)."""
        w = self.w
        dist = w.bfs([i], max_dist=THREAT_REACH * 2)
        for j, d in dist.items():
            for owner, units in w.armies.get(j, {}).items():
                if owner == q or not any(units.values()):
                    continue
                reach = THREAT_REACH * max((C.UNITS[u]["move"] for u, k in units.items() if k and u in C.UNITS),
                                           default=1)
                if d > reach:
                    continue
                if owner == other and peace >= SAFE_PEACE_TURNS:
                    continue
                until = w.treaties.get(owner) if q == w.me else (w.treaties.get(q) if owner == w.me else None)
                if until is not None and until >= w.turn + SAFE_PEACE_TURNS:
                    continue
                if until is None and q != w.me and owner != w.me and w.at_peace(q, owner):
                    continue                  # other players' treaties: end turn unknown here
                return True
        return False

    def tile_value(self, q: str, xy, giving: bool, other: str | None = None, peace: int = 0) -> float:
        w = self.w
        try:
            i = w.idx(int(xy[0]), int(xy[1]))
            y = tile_yield(w, i)
        except (TypeError, ValueError, IndexError):
            return 0.0
        if not giving and self.tile_exposed(q, i, other, peace):
            return 0.0                # the giver (or another army) could just take it back
        per_turn = sum(v * (3.0 if r == "influence" else self.prices.get(r, 1.0)) for r, v in y.items())
        v = per_turn * min(self.remaining, 40) * 0.6 + 6.0 * C.SCORE_WEIGHTS["tiles"]
        if giving:
            v *= 1.3
            if any(w.cities.get(j, {}).get("owner") == q for j in w.radius(i, 1)):
                v += 40.0          # land next to our city: armies could stand there
        return v

    # -- contracts ------------------------------------------------------------
    def installment(self, per_turn: dict) -> float:
        return sum(v * (1.0 if r == "gold" else 0.95 * self.prices.get(r, 1.0)) for r, v in per_turn.items())

    def reliability(self, payer: str, per_turn: dict, turns: int) -> float:
        """Probability-like weight that ``payer`` pays every instalment."""
        pl = self.w.players.get(payer) or {}
        if not pl.get("alive", True):
            return 0.0
        rep = pl.get("reputation") or {}
        rel = 0.93 * (0.55 ** int(rep.get("defaults", 0) or 0)) * (0.85 ** int(rep.get("betrayals", 0) or 0))
        rel += 0.01 * min(5, int(rep.get("contracts_honoured", 0) or 0))
        inc = self.income(payer)
        owed = contract_obligations(self.w, payer)
        stock = self.stock(payer)
        upkeep = int(pl.get("upkeep", 0) or 0)
        # gold can also come from selling production on the market
        sellable = sum(max(0, inc.get(r, 0) - (upkeep if r == "food" else 0)) * self.prices[r] * 0.7
                       for r in CAPPED)
        for r, v in per_turn.items():
            if v <= 0:
                continue
            spare = inc.get(r, 0) - owed.get(r, 0) + stock.get(r, 0) / max(1, turns)
            if r == "food":
                spare -= upkeep
            elif r == "gold":
                spare += sellable - sum(owed.get(x, 0) * self.prices[x] for x in CAPPED)
            if spare < v:
                rel *= max(0.1, spare / v)
        return max(0.02, min(0.98, rel))

    def credit_limit(self, payer: str) -> float:
        """Most future instalments (present value) we accept as payment for
        goods handed over now: grows with contracts ``payer`` honoured,
        none after a default."""
        rep = (self.w.players.get(payer) or {}).get("reputation") or {}
        # a default takes the remaining obligation from the payer's bank:
        # part of the bank backs a loan whatever the payer's record
        collateral = COLLATERAL_WEIGHT * bank_of(self.w, payer)
        if int(rep.get("defaults", 0) or 0):
            return collateral
        return CREDIT_BASE * (1 + min(4, int(rep.get("contracts_honoured", 0) or 0))) + collateral

    def contract_value(self, viewer: str, bundle: dict, payer: str, payee: str) -> float:
        """+PV for the payee, −cost for the payer (from ``viewer``'s side)."""
        per = bundle.get("per_turn") or {}
        turns = int(bundle.get("turns", 0) or 0)
        if not per or turns <= 0:
            return 0.0
        inst = self.installment(per)
        mine = viewer == self.me
        disc = self.discount if mine else 0.97
        t = min(turns, self.remaining)
        owed = inst * t                                   # what a default would leave unpaid (at most)
        bank = bank_of(self.w, payer)
        if viewer == payer:
            if mine and self.horizon is not None:
                t = min(t, max(1, self.horizon))
            cost = inst * annuity(disc, t)
            if self.reliability(payer, per, turns) < 0.5:
                # we would likely default: influence fine, and the bank pays the rest
                cost += C.CONTRACT_DEFAULT_PENALTY * 3.0 + 0.5 * min(bank, owed)
            return -cost
        rel = self.reliability(payer, per, turns)
        # on a default the payee receives part of the rest from the payer's bank
        recovery = (1 - rel) * min(bank, 0.5 * owed)
        return inst * annuity(disc, t) * rel + recovery

    # -- peace ----------------------------------------------------------------
    def _dist_from(self, city: int) -> dict:
        d = self._city_dist.get(city)
        if d is None:
            d = self._city_dist[city] = self.w.bfs([city], max_dist=2 * THREAT_REACH)
        return d

    def threat(self, attacker: str, victim: str) -> float:
        """Raw strength of ``attacker``'s units that could reach one of
        ``victim``'s cities within a few turns (worst city)."""
        key = (attacker, victim)
        got = self._threat.get(key)
        if got is not None:
            return got
        w = self.w
        worst = 0.0
        armies = w.armies_of(attacker)
        if armies:
            for c in w.cities_of(victim):
                dist = self._dist_from(c)
                tot = 0.0
                for i, units in armies.items():
                    d = dist.get(i)
                    if d is None:
                        continue
                    for t, k in units.items():
                        if t in C.UNITS and d <= THREAT_REACH * C.UNITS[t]["move"]:
                            tot += k * C.UNITS[t]["strength"]
                worst = max(worst, tot)
        self._threat[key] = worst
        return worst

    def defense(self, q: str) -> float:
        w = self.w
        cities = w.cities_of(q)
        if not cities:
            return 0.0
        own = 0.0
        for c in cities:
            cc = w.cities[c]
            mult = 1 + 0.5 * cc["buildings"].get("walls", 0)
            units = w.armies.get(c, {}).get(q, {})
            own = max(own, (raw_strength(units) + float(cc.get("garrison", 0))) * mult)
        return own + 0.3 * raw_strength((w.players.get(q) or {}).get("units"))

    def peace_value(self, q: str, other: str, turns: int) -> float:
        """Value for ``q`` of ``turns`` turns of peace with ``other``."""
        if not turns:
            return 0.0
        w = self.w
        until = None
        if q == self.me:
            until = w.treaties.get(other)
        elif other == self.me:
            until = w.treaties.get(q)
        if until is not None and until >= w.turn + turns:
            return 0.0                                # nothing new
        mp = float((w.players.get(other) or {}).get("military_power", 0) or 0)
        t = self.threat(other, q)
        v = 5.0 + 0.03 * mp + 4.0 * max(0.0, t - 0.6 * self.defense(q))
        v = min(800.0, v) * min(1.0, turns / 20.0)
        # a partner with betrayals is less likely to keep the peace; what it
        # must pledge (§9) is paid to us if it breaks
        rel = 0.85 ** w.betrayals(other)
        v = v * rel + w.bond_required(other) * (1.0 - rel)
        if q == self.me:
            v = v * self.peace_scale + self.peace_bias.get(other, 0.0)
        return v

    # -- bundles & deals --------------------------------------------------------
    def bundle_in(self, pid: str, b: dict, other: str, peace: int = 0) -> float:
        v = sum(self.recv_value(pid, r, int(b.get(r, 0) or 0)) for r in C.TRADABLE)
        v += sum(self.tile_value(pid, t, False, other, peace) for t in b.get("tiles", ()) or ())
        v += self.contract_value(pid, b, other, pid)
        return v

    def bundle_out(self, pid: str, b: dict, other: str) -> float:
        v = sum(self.give_cost(pid, r, int(b.get(r, 0) or 0)) for r in C.TRADABLE)
        v += sum(self.tile_value(pid, t, True) for t in b.get("tiles", ()) or ())
        v -= self.contract_value(pid, b, pid, other)
        return v

    def deal_gain(self, deal: dict, pid: str | None = None) -> float:
        """Net value of ``deal`` for ``pid`` (default: me), either party."""
        pid = pid or self.me
        if pid == deal.get("from"):
            out, inn, other = deal.get("give") or {}, deal.get("get") or {}, deal.get("to")
        else:
            out, inn, other = deal.get("get") or {}, deal.get("give") or {}, deal.get("from")
        g = self.bundle_in(pid, inn, other, int(deal.get("peace") or 0)) - self.bundle_out(pid, out, other)
        if pid == self.me and inn.get("per_turn") and (out.get("tiles") or any(out.get(r) for r in C.TRADABLE)):
            # lending (goods now against instalments later): trust a payer
            # only up to its credit limit (a default costs it influence, but
            # the goods are gone)
            pv = self.contract_value(pid, inn, other, pid)
            g -= max(0.0, pv - self.credit_limit(other))
        if deal.get("peace"):
            g += self.peace_value(pid, other, int(deal["peace"]))
        return g

    def size(self, deal: dict) -> float:
        """Market value of everything the deal moves (for relative margins)."""
        tot = 0.0
        for key in ("give", "get"):
            b = deal.get(key) or {}
            tot += sum(int(b.get(r, 0) or 0) * self.prices.get(r, 1.0) for r in C.TRADABLE)
            per = b.get("per_turn") or {}
            if per:
                tot += self.installment(per) * min(int(b.get("turns", 0) or 0), self.remaining)
            tot += 30.0 * len(b.get("tiles", ()) or ())
        return tot

    # -- "never help a player who is close to winning" -------------------------
    def danger(self, q: str) -> float:
        return danger(self.w, q)

    def projected_danger(self, q: str, bundle: dict, bundle_from: dict | None = None) -> float:
        """``q``'s victory progress if it received ``bundle`` and handed over
        ``bundle_from`` (economic: the bank plus the net gold value, contract
        payments of the next 10 turns included, capped at 10 turns of its
        bank limit; wonder: the next stage becomes affordable)."""
        w = self.w
        d = danger(w, q)
        if not bundle:
            return d
        out = bundle_from or {}
        stock = self.stock(q)
        pl = w.players.get(q) or {}

        def value(b):
            v = sum(int(b.get(r, 0) or 0) * self.prices.get(r, 1.0) for r in C.TRADABLE)
            per = b.get("per_turn") or {}
            if per:
                v += self.installment(per) * min(10, int(b.get("turns", 0) or 0))
            return v
        # economic: net gold can only reach the bank at bank_limit per turn
        target = w.thresholds.get("bank", C.BANK_VICTORY)
        extra = max(0.0, min(value(bundle) - value(out), 10 * bank_limit(w, q)))
        d = max(d, C.LEDGER_PROGRESS_WEIGHT * min(1.0, (bank_of(w, q) + extra) / max(1, target)))
        stage = int(pl.get("wonder_stage", 0) or 0)
        if 1 <= stage < C.WONDER_VICTORY_STAGE:
            cost = building_cost("wonder", stage + 1)
            short = 0.0
            for r, v in cost.items():
                have = stock.get(r, 0) + int(bundle.get(r, 0) or 0) - int(out.get(r, 0) or 0)
                if r != "gold" and have < v:
                    short += (v - have) * self.prices.get(r, 1.0) * 1.2
            gold = stock.get("gold", 0) + int(bundle.get("gold", 0) or 0) - int(out.get("gold", 0) or 0)
            if gold >= cost["gold"] + short:
                d = max(d, (stage + 1) / C.WONDER_VICTORY_STAGE)
        return d

    def helps_winner(self, q: str, bundle_to_q: dict, threshold: float = 0.7,
                     bundle_from_q: dict | None = None) -> bool:
        """True if ``q`` is close to winning (progress ≥ ``threshold``) or
        receiving ``bundle_to_q`` (for ``bundle_from_q``) would bring it
        there."""
        return self.projected_danger(q, bundle_to_q or {}, bundle_from_q) >= threshold


# ---------------------------------------------------------------------------
# Safe bot base
# ---------------------------------------------------------------------------
def _fogfill(view: dict) -> dict:
    """Estimates for the fields hidden in fog games (no-op otherwise)."""
    from .fogfill import fill
    return fill(view)


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
            out = self.decide(_fogfill(view))
            return list(out or [])[:C.MAX_ORDERS_PER_TURN]
        except Exception as e:  # never let a bot crash the game
            self.last_error = f"{type(e).__name__}: {e}"
            if self._plan is not None:
                return list(self._plan.orders)[:C.MAX_ORDERS_PER_TURN]
            return []

    TRADE = True        # False: never negotiates (ablation / pure market play)

    def negotiate(self, view: dict) -> list:
        """One §13 negotiation round; never raises. Subclasses implement
        :meth:`decide_deals`."""
        try:
            if not self.TRADE or not view or not view.get("you") or not view["you"].get("alive", True):
                return []
            if view.get("status") not in (None, "running"):
                return []
            out = self.decide_deals(_fogfill(view))
            return list(out or [])[:C.DIPLOMACY_ACTIONS_PER_TURN]
        except Exception as e:  # never let a bot crash the game
            self.last_error = f"negotiate: {type(e).__name__}: {e}"
            return []

    def decide_deals(self, view: dict) -> list:
        return []

    def new_plan(self, world: World) -> Plan:
        self._plan = Plan(world)
        return self._plan

    def decide(self, view: dict) -> list:  # pragma: no cover - abstract
        raise NotImplementedError
