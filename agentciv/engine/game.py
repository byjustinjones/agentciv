"""The AgentCiv game engine: state and deterministic turn resolution.

See docs/DESIGN.md for the rules. Public API (§11)::

    g = Game(GameConfig(seed=42, max_turns=150, game_id="g1"))
    pid = g.add_player("Alpha")
    g.start()
    errors = g.submit_orders(pid, [...])
    results = g.diplomacy(pid, [...])   # live barter (§13), any time
    events = g.step()
    g.player_view(pid); g.spectator_view(); g.finished; g.result

The engine is single-threaded, deterministic and performs no I/O. The only
randomness is the seeded map generator.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import combat
from . import constants as C
from . import deals as D
from . import fog as F
from . import market as M
from . import views
from .mapgen import generate_map
from .model import City, Player
from .orders import prevalidate
from .rules import (building_cost, claim_cost, relics_needed,
                    rules_json, season, settle_cost, storage_cap, thresholds,
                    unit_cost)

ACTION_TYPES = ("build", "claim", "settle", "recruit", "disband")


@dataclass
class GameConfig:
    seed: int = 0
    max_turns: int = C.DEFAULT_MAX_TURNS
    game_id: str = "g1"
    name: str | None = None
    max_players: int = C.MAX_PLAYERS
    fog: bool = False          # fog of war and espionage (docs/RULES.md §14)


class _Group:
    """A group of units moving along a path this turn."""

    __slots__ = ("pid", "src", "path", "units", "order")

    def __init__(self, pid, src, path, units, order):
        self.pid = pid
        self.src = src
        self.path = path
        self.units = units
        self.order = order


def _can_pay(res: dict, cost: dict) -> bool:
    return all(res.get(r, 0) >= v for r, v in cost.items())


def _pay(res: dict, cost: dict) -> None:
    for r, v in cost.items():
        res[r] = res.get(r, 0) - v


def _fmt_cost(cost: dict) -> str:
    return ", ".join(f"{v} {r}" for r, v in cost.items())


class Game:
    """One game of AgentCiv."""

    def __init__(self, config: GameConfig | None = None):
        self.config = config or GameConfig()
        self.game_id = self.config.game_id
        self.max_turns = int(self.config.max_turns)
        self.status = "lobby"
        self.turn = 0
        self.deadline: float | None = None
        self.players: list[Player] = []
        self._by_id: dict[str, Player] = {}
        # map
        self.width = self.height = 0
        self.terrain: list = []
        self.owner: list = []
        self.improvement: list = []
        self.deposits: list = []
        self.neighbors: list = []
        self.relics: list = []
        self.relic_set: frozenset = frozenset()
        self.map_info: dict = {}
        # entities
        self.cities: dict[int, City] = {}
        self.armies: dict[int, dict[str, dict[str, int]]] = {}
        # diplomacy
        self.treaties: dict[tuple, int] = {}
        self.treaty_proposals: list = []
        self.messages: list = []
        # barter & deals (§13, agentciv.engine.deals)
        self.deals: dict[str, dict] = {}        # every deal ever made, by id
        self.open_deals: dict[str, dict] = {}   # open ones (same dicts)
        self.deal_log: list = []                # public log of executed deals
        self.contracts: list = []               # active contracts, creation order
        self.diplomacy_seq = 0
        self.diplomacy_log: list = []           # {"turn","seq","pid","action","via"}
        self._deal_counter = 0
        self._contract_counter = 0
        self._recent_deals: dict[str, list] = {}
        self._recent_all: list = []
        self._dip_feed: list = []
        self._dip_counts: dict[str, list] = {}
        self._dip_counts_turn = -1
        # market
        self.pools: dict[str, list] = {}
        self.pool_init: dict[str, tuple] = {}
        self.market_history: list = []
        # turn bookkeeping
        self._orders: dict[str, list] = {}
        self._submitted: dict[str, bool] = {}
        self._recruit_queue: list = []
        self._events: list = []
        self._move_restricted: set = set()
        self.last_events: list = []
        self.result: dict | None = None
        self._stats: dict | None = None
        # fog of war (config.fog only; agentciv.engine.fog)
        self.sightings: dict[str, dict[int, dict]] = {}   # pid -> {tile: {"turn", "armies": {owner: units}}}
        self.intel_reports: dict[str, list] = {}           # pid -> espionage reports
        self._pending_intel: list = []                     # (spy, target, mission, outcome) this turn

    # ==================================================================
    # public API
    # ==================================================================
    @property
    def finished(self) -> bool:
        return self.status == "finished"

    @property
    def fog(self) -> bool:
        """True while a fog game is running (views hide part of the state)."""
        return self.config.fog and self.status == "running"

    def add_player(self, name: str) -> str:
        if self.status != "lobby":
            raise RuntimeError("players can only join before the game starts")
        if len(self.players) >= min(self.config.max_players, C.MAX_PLAYERS):
            raise RuntimeError("game is full")
        k = len(self.players)
        pid = f"p{k + 1}"
        name = str(name or f"Player {k + 1}").strip()[:40] or f"Player {k + 1}"
        p = Player(pid, name, k, C.PLAYER_COLORS[k % len(C.PLAYER_COLORS)])
        self.players.append(p)
        self._by_id[pid] = p
        return pid

    def start(self) -> None:
        if self.status != "lobby":
            raise RuntimeError("game already started")
        n = len(self.players)
        if n < C.MIN_PLAYERS:
            raise RuntimeError("no players")
        m = generate_map(n, self.config.seed)
        self.width, self.height = m.width, m.height
        w, h = self.width, self.height
        self.terrain = list(m.terrain)
        self.deposits = list(m.deposits)
        self.owner = [None] * (w * h)
        self.improvement = [None] * (w * h)
        self.relics = list(m.relics)
        self.relic_set = frozenset(self.relics)
        self.map_info = dict(m.fairness, stamp_radius=m.stamp_radius)
        # start slot (index in mapgen.start_layout) per player, for fairness stats
        self.start_slots = {p.id: m.slots[k] for k, p in enumerate(self.players)} if m.slots else {}
        self.neighbors = []
        for i in range(w * h):
            x, y = i % w, i // w
            nb = []
            for nx, ny in ((x, y - 1), (x + 1, y), (x, y + 1), (x - 1, y)):
                if 0 <= nx < w and 0 <= ny < h:
                    nb.append(ny * w + nx)
            self.neighbors.append(tuple(nb))
        for p, s in zip(self.players, m.starts):
            p.capital = s
            self._found_city(p, s, capital=True)
            self.armies.setdefault(s, {})[p.id] = dict(C.START_UNITS)
        for r, (a, g) in C.MARKET_POOLS_PER_PLAYER.items():
            self.pool_init[r] = (float(a * n), float(g * n))
            self.pools[r] = [float(a * n), float(g * n)]
        self.market_history = [{"turn": -1, "prices": self._prices()}]
        self.status = "running"
        self.turn = 0
        self._submitted = {p.id: False for p in self.players}
        self._invalidate()

    def player(self, pid) -> Player | None:
        return self._by_id.get(pid) if isinstance(pid, str) else None

    def alive_players(self) -> list:
        return [p.id for p in self.players if p.alive]

    def has_submitted(self, pid: str) -> bool:
        return bool(self._submitted.get(pid, False))

    def submit_orders(self, pid: str, orders) -> list:
        """Pre-validate and store ``orders`` for the current turn (replacing
        any earlier submission). Returns ``[{"index", "error"}]``."""
        try:
            p = self.player(pid)
            if p is None:
                return [{"index": -1, "error": f"unknown player {pid!r}"}]
            if self.status != "running":
                return [{"index": -1, "error": f"game is not running (status {self.status})"}]
            if not p.alive:
                return [{"index": -1, "error": "you have been eliminated"}]
            accepted, errors = prevalidate(self, pid, orders)
        except Exception as e:  # pragma: no cover - defensive
            return [{"index": -1, "error": f"could not process orders ({type(e).__name__})"}]
        self._orders[pid] = accepted
        self._submitted[pid] = True
        return errors

    def diplomacy(self, pid: str, actions) -> list:
        """Apply diplomacy actions (propose, counter, accept, reject,
        withdraw, say — §13) immediately. Returns one result per action,
        ``{"index", "ok", "deal"?, "error"?}``. Never raises."""
        try:
            return D.run_actions(self, pid, actions)
        except Exception as e:  # pragma: no cover - defensive
            return [{"index": -1, "ok": False, "error": f"could not process diplomacy ({type(e).__name__})"}]

    def inbox(self, pid: str, since: int = 0) -> dict:
        """``{"seq", "items"}``: diplomacy events visible to ``pid`` with
        ``seq > since`` (excluding its own actions)."""
        try:
            since = int(since)
        except (TypeError, ValueError):
            since = 0
        return D.inbox(self, pid, since)

    def player_view(self, pid: str) -> dict:
        if self.player(pid) is None:
            raise KeyError(f"unknown player {pid!r}")
        return views.build_view(self, pid)

    def spectator_view(self, full: bool = False) -> dict:
        """Public view (no private messages/offers/proposals/events while the
        game runs); ``full=True`` or a finished game: everything."""
        return views.build_view(self, None, full)

    @staticmethod
    def rules() -> dict:
        return rules_json()

    # ==================================================================
    # geometry / relations helpers
    # ==================================================================
    def idx(self, x: int, y: int) -> int:
        return y * self.width + x

    def xy(self, i: int) -> tuple[int, int]:
        return i % self.width, i // self.width

    def cheb(self, a: int, b: int) -> int:
        w = self.width
        return max(abs(a % w - b % w), abs(a // w - b // w))

    def radius(self, i: int, r: int) -> list:
        """Tile indices in the Chebyshev radius ``r`` square around ``i``."""
        w, h = self.width, self.height
        x, y = i % w, i // w
        return [yy * w + xx
                for yy in range(max(0, y - r), min(h, y + r + 1))
                for xx in range(max(0, x - r), min(w, x + r + 1))]

    def nearest_city_distance(self, i: int) -> int:
        return min((self.cheb(i, c) for c in self.cities), default=10 ** 9)

    @staticmethod
    def _pair(a: str, b: str) -> tuple:
        return (a, b) if a < b else (b, a)

    def treaty(self, a: str, b: str) -> bool:
        return a != b and self._pair(a, b) in self.treaties

    def hostile(self, a: str, b: str) -> bool:
        return a != b and self._pair(a, b) not in self.treaties

    def treaty_proposal(self, frm: str, to: str) -> dict | None:
        for pr in self.treaty_proposals:
            if pr["from"] == frm and pr["to"] == to and pr["turn"] == self.turn - 1:
                return pr
        return None

    def owned_cities(self, pid: str) -> list:
        return [c for c in self.cities.values() if c.owner == pid]

    def has_market_hall(self, pid: str) -> bool:
        return any(c.market_hall for c in self.cities.values() if c.owner == pid)

    def warehouses(self, pid: str) -> int:
        return sum(c.warehouse for c in self.cities.values() if c.owner == pid)

    def caps(self, pid: str) -> dict:
        cap = storage_cap(self.warehouses(pid))
        return {r: cap for r in C.CAPPED_RESOURCES}

    def relic_guarded(self, i: int) -> bool:
        """True if the owner of relic tile ``i`` has units standing on it."""
        o = self.owner[i]
        return o is not None and o in self.armies.get(i, {})

    def wonder_stage(self, pid: str) -> int:
        p = self._by_id[pid]
        c = self.cities.get(p.wonder_city) if p.wonder_city is not None else None
        return c.wonder_stage if c is not None and c.owner == pid else 0

    def _alive_in_order(self) -> list:
        return [p for p in self.players if p.alive]

    def _set_owner(self, i: int, pid: str | None) -> None:
        old = self.owner[i]
        if old == pid:
            return
        if old is not None:
            self._by_id[old].tiles -= 1
        if pid is not None:
            self._by_id[pid].tiles += 1
        self.owner[i] = pid
        self._stats = None

    def _invalidate(self) -> None:
        self._stats = None

    # ==================================================================
    # state helpers (also used by tests to construct situations)
    # ==================================================================
    def _found_city(self, p: Player, i: int, capital: bool = False) -> City:
        p.city_counter += 1
        x, y = self.xy(i)
        city = City(i, x, y, p.id, f"{p.name}-{p.city_counter}", capital, self.turn)
        self.cities[i] = city
        self.improvement[i] = None
        self._set_owner(i, p.id)
        for j in self.radius(i, C.CITY_CLAIM_RADIUS):
            if self.owner[j] is None and self.terrain[j] in C.PASSABLE and j not in self.relic_set:
                self._set_owner(j, p.id)
        return city

    def set_owner(self, x: int, y: int, pid: str | None) -> None:
        """Test/debug helper: set a tile's owner."""
        self._set_owner(self.idx(x, y), pid)
        self._invalidate()

    def place_units(self, x: int, y: int, pid: str, units: dict) -> None:
        """Test/debug helper: put units on a tile (adds to existing)."""
        per = self.armies.setdefault(self.idx(x, y), {}).setdefault(pid, {})
        for u, c in units.items():
            per[u] = per.get(u, 0) + c
            if per[u] <= 0:
                del per[u]
        self._clean_armies()
        self._invalidate()

    def add_city(self, x: int, y: int, pid: str, capital: bool = False) -> City:
        """Test/debug helper: found a city for ``pid`` at (x, y)."""
        c = self._found_city(self._by_id[pid], self.idx(x, y), capital)
        self._invalidate()
        return c

    def _clean_armies(self) -> None:
        for i in list(self.armies):
            per = self.armies[i]
            for pid in list(per):
                per[pid] = {u: c for u, c in per[pid].items() if c > 0}
                if not per[pid]:
                    del per[pid]
            if not per:
                del self.armies[i]

    def units_of(self, pid: str) -> dict:
        tot = {u: 0 for u in C.UNIT_TYPES}
        for per in self.armies.values():
            u = per.get(pid)
            if u:
                for k, c in u.items():
                    tot[k] += c
        return tot

    # ==================================================================
    # events
    # ==================================================================
    def _emit(self, etype: str, vis: list | None = None, **fields) -> dict:
        ev = {"turn": self.turn, "type": etype}
        ev.update(fields)
        if vis is not None:
            ev["_vis"] = sorted(set(vis))
        self._events.append(ev)
        return ev

    def _fail(self, pid: str, order: dict, reason: str) -> None:
        self._emit("order_failed", vis=[pid], player=pid, index=order.get("index"),
                   order_type=order.get("type"), reason=reason)

    def _xy_fields(self, i: int) -> dict:
        return {"x": i % self.width, "y": i // self.width}

    # ==================================================================
    # turn resolution
    # ==================================================================
    def step(self) -> list:
        """Resolve the current turn. Returns the events generated."""
        if self.status != "running":
            return []
        # self._events already holds the events of diplomacy actions sent
        # through the channel during this turn (they belong to this turn)
        orders = {p.id: list(self._orders.get(p.id, [])) for p in self.players if p.alive}
        fog = self.config.fog
        if fog:
            pre = F.vision_all(self)
            F.record_sightings(self, pre)
        self._phase_diplomacy(orders)
        self._phase_treaties(orders)
        self._phase_market(orders)
        self._phase_actions(orders)
        self._phase_movement(orders)
        self._phase_spawn()
        self._phase_economy()
        if fog:
            self._phase_intel(orders)
        self._phase_bookkeeping()
        if fog:
            F.after_step(self, pre)
        self.last_events = self._events
        self._events = []
        return [{k: v for k, v in e.items() if k not in ("_vis", "_fog")} for e in self.last_events]

    # ---------------------------------------------------------------- 1
    def _rotated(self) -> list:
        """Living players, starting with a player that rotates every turn."""
        alive = self._alive_in_order()
        if not alive:
            return alive
        k = self.turn % len(alive)
        return alive[k:] + alive[:k]

    def _phase_diplomacy(self, orders: dict) -> None:
        """Diplomacy actions inside orders (propose/counter/accept/reject/
        withdraw/say and the legacy offer_trade/accept_trade/message),
        round-robin from the rotating player, exactly as if sent through
        ``diplomacy()`` at that moment."""
        order = self._rotated()
        lists = {p.id: [o for o in orders[p.id] if "action" in o] for p in order}
        longest = max((len(v) for v in lists.values()), default=0)
        for k in range(longest):
            for p in order:
                lst = lists[p.id]
                if k >= len(lst) or not p.alive:
                    continue
                o = lst[k]
                res = D.apply_action(self, p.id, o["action"], via="orders")
                if not res["ok"]:
                    self._fail(p.id, o, res["error"])

    # ---------------------------------------------------------------- 2
    def _phase_treaties(self, orders: dict) -> None:
        t = self.turn
        broken = set()
        for p in self._alive_in_order():
            for o in orders[p.id]:
                if o["type"] != "break_treaty":
                    continue
                other = o["with"]
                key = self._pair(p.id, other)
                # a treaty broken earlier in this phase by the partner still
                # counts: both players who ordered the break pay (no seat bias)
                if key not in self.treaties and key not in broken:
                    self._fail(p.id, o, f"no treaty with {other}")
                elif p.resources["influence"] < C.TREATY_BREAK_COST:
                    self._fail(p.id, o, f"breaking a treaty costs {C.TREATY_BREAK_COST} influence")
                else:
                    p.resources["influence"] -= C.TREATY_BREAK_COST
                    self.treaties.pop(key, None)
                    p.betrayals += 1
                    broken.add(key)
                    self._emit("treaty_broken", by=p.id, **{"with": other})
        for p in self._alive_in_order():
            for o in orders[p.id]:
                if o["type"] != "accept_treaty":
                    continue
                frm = o["from"]
                prop = self.treaty_proposal(frm, p.id)
                fp = self._by_id.get(frm)
                if prop is None or fp is None or not fp.alive:
                    self._fail(p.id, o, f"no valid treaty proposal from {frm}")
                elif self.treaty(p.id, frm):
                    self._fail(p.id, o, f"already at peace with {frm}")
                else:
                    until = t + prop["turns"]
                    self.treaties[self._pair(p.id, frm)] = until
                    self.treaty_proposals.remove(prop)
                    self._emit("treaty_signed", a=frm, b=p.id, until_turn=until)
        for p in self._alive_in_order():
            for o in orders[p.id]:
                if o["type"] != "propose_treaty":
                    continue
                to = o["to"]
                tp = self._by_id.get(to)
                if tp is None or not tp.alive:
                    self._fail(p.id, o, f"{to} is not in the game")
                elif self.treaty(p.id, to):
                    self._fail(p.id, o, f"already at peace with {to}")
                else:
                    self.treaty_proposals.append({"from": p.id, "to": to, "turns": o["turns"], "turn": t})
                    self._emit("treaty_proposed", vis=[p.id, to], **{"from": p.id, "to": to, "turns": o["turns"]})
        self._move_restricted = set(self.treaties) | broken

    # ---------------------------------------------------------------- 3
    def _phase_market(self, orders: dict) -> None:
        fees = {p.id: (C.MARKET_HALL_FEE if self.has_market_hall(p.id) else C.MARKET_FEE)
                for p in self._alive_in_order()}
        balances = {p.id: p.resources for p in self._alive_in_order()}
        for r in C.MARKET_RESOURCES:
            pool = self.pools[r]
            cap = int(pool[0] * C.MARKET_MAX_ORDER_FRACTION)
            mos = []
            for p in self._alive_in_order():
                for o in orders[p.id]:
                    if o["type"] != "market" or o["resource"] != r:
                        continue
                    if o["qty"] > cap:
                        self._fail(p.id, o, f"market: qty exceeds {cap}")
                        continue
                    mos.append(M.MarketOrder(p.id, o["side"], r, o["qty"], o["limit"], o["index"]))
            if not mos:
                continue
            fills, failures, price = M.clear_resource(pool, mos, balances, fees)
            for mo, reason in failures:
                self._emit("order_failed", vis=[mo.pid], player=mo.pid, index=mo.index,
                           order_type="market", reason=reason)
            for mo, gold in fills:
                self._emit("market", player=mo.pid, resource=r, side=mo.side, qty=mo.qty,
                           price=round(price, 4), gold=gold)

    # ---------------------------------------------------------------- 4
    def _phase_actions(self, orders: dict) -> None:
        alive = self._alive_in_order()
        if not alive:
            return
        lists = {p.id: [o for o in orders[p.id] if o["type"] in ACTION_TYPES] for p in alive}
        self._wonder_built: set = set()
        contested = self._contested(lists, self._viable_expansions(lists))
        order = self._rotated()
        longest = max(len(v) for v in lists.values())
        for k in range(longest):
            for p in order:
                lst = lists[p.id]
                if k >= len(lst):
                    continue
                o = lst[k]
                if id(o) in contested:
                    self._fail(p.id, o, "contested: another player claimed/settled the same spot this turn (refunded)")
                    continue
                try:
                    reason = getattr(self, "_act_" + o["type"])(p, o)
                except Exception as e:  # pragma: no cover - defensive; checks precede payment
                    reason = f"internal error ({type(e).__name__})"
                if reason:
                    self._fail(p.id, o, reason)

    def _viable_expansions(self, lists: dict) -> set:
        """ids of the claim/settle orders that would succeed if their player
        acted alone this turn (their whole action list, in order, against the
        current state). Only these can contest other players' orders: an
        order that fails anyway (no influence/resources, not adjacent, ...)
        must not block anybody."""
        viable: set = set()
        for pid, lst in lists.items():
            if not any(o["type"] in ("claim", "settle") for o in lst):
                continue
            p = self._by_id[pid]
            snap = self._snapshot(p)
            try:
                for o in lst:
                    if o["type"] == "disband":  # free, never affects expansions
                        continue
                    try:
                        reason = getattr(self, "_act_" + o["type"])(p, o)
                    except Exception:  # pragma: no cover - defensive
                        reason = "error"
                    if reason is None and o["type"] in ("claim", "settle"):
                        viable.add(id(o))
            finally:
                self._restore(p, snap)
        return viable

    def _snapshot(self, p: Player) -> tuple:
        cities = {i: (c.owner, c.walls, c.warehouse, c.market_hall, c.wonder_stage) for i, c in self.cities.items()}
        return (list(self.owner), list(self.improvement), {q.id: q.tiles for q in self.players},
                dict(p.resources), p.wonder_city, p.city_counter, cities,
                len(self._recruit_queue), len(self._events), set(self._wonder_built))

    def _restore(self, p: Player, snap: tuple) -> None:
        owner, imp, tiles, res, wcity, counter, cities, nq, nev, wb = snap
        self.owner[:] = owner
        self.improvement[:] = imp
        for q in self.players:
            q.tiles = tiles[q.id]
        p.resources.clear()
        p.resources.update(res)
        p.wonder_city = wcity
        p.city_counter = counter
        for i in [i for i in self.cities if i not in cities]:
            del self.cities[i]
        for i, (own, walls, wh, mh, ws) in cities.items():
            c = self.cities[i]
            c.owner, c.walls, c.warehouse, c.market_hall, c.wonder_stage = own, walls, wh, mh, ws
        del self._recruit_queue[nq:]
        del self._events[nev:]
        self._wonder_built = wb
        self._invalidate()

    def _contested(self, lists: dict, viable: set | None = None) -> set:
        by_tile: dict = {}
        settles = []
        for pid, lst in lists.items():
            for o in lst:
                if o["type"] in ("claim", "settle") and (viable is None or id(o) in viable):
                    by_tile.setdefault(o["at"], []).append((pid, o))
                    if o["type"] == "settle":
                        settles.append((pid, o))
        out = set()
        for entries in by_tile.values():
            if len({pid for pid, _ in entries}) > 1:
                out.update(id(o) for _, o in entries)
        for a in range(len(settles)):
            pa, oa = settles[a]
            for b in range(a + 1, len(settles)):
                pb, ob = settles[b]
                if pa != pb and self.cheb(oa["at"], ob["at"]) <= C.SETTLE_CONTENTION_RADIUS:
                    out.add(id(oa))
                    out.add(id(ob))
        return out

    def _hostile_units_on(self, pid: str, i: int) -> bool:
        return any(q != pid and self.hostile(pid, q) for q in self.armies.get(i, {}))

    def _adjacent_to(self, pid: str, i: int) -> bool:
        return any(self.owner[j] == pid for j in self.neighbors[i])

    def _act_build(self, p: Player, o: dict):
        i, b = o["at"], o["building"]
        if b in C.IMPROVEMENTS:
            spec = C.IMPROVEMENTS[b]
            if self.owner[i] != p.id:
                return "you do not own that tile"
            if i in self.cities:
                return "cannot build an improvement on a city tile"
            if self.terrain[i] not in spec["terrain"]:
                return f"{b} cannot be built on {C.TERRAIN[self.terrain[i]]['name']}"
            if self.improvement[i] is not None:
                return "tile already has an improvement"
            cost = spec["cost"]
            if not _can_pay(p.resources, cost):
                return f"cannot afford {b} ({_fmt_cost(cost)})"
            _pay(p.resources, cost)
            self.improvement[i] = b
            self._emit("build", player=p.id, building=b, level=1, **self._xy_fields(i))
            return None
        city = self.cities.get(i)
        if city is None or city.owner != p.id:
            return "you do not own a city there"
        spec = C.CITY_BUILDINGS[b]
        level = city.building_level(b)
        if level >= spec["max"]:
            return f"{b} already at max level"
        if b == "wonder":
            if p.id in self._wonder_built:
                return "only one wonder stage per turn"
            wc = self.cities.get(p.wonder_city) if p.wonder_city is not None else None
            if wc is not None and wc.owner == p.id and wc.idx != i:
                return f"your wonder is in {wc.name}"
        cost = building_cost(b, level + 1)
        if not _can_pay(p.resources, cost):
            return f"cannot afford {b} level {level + 1} ({_fmt_cost(cost)})"
        _pay(p.resources, cost)
        if b == "wonder":
            city.wonder_stage += 1
            p.wonder_city = i
            self._wonder_built.add(p.id)
            self._emit("wonder_stage", player=p.id, stage=city.wonder_stage, city=city.name, **self._xy_fields(i))
        else:
            setattr(city, b, level + 1)
            self._emit("build", player=p.id, building=b, level=level + 1, **self._xy_fields(i))
        return None

    def _act_claim(self, p: Player, o: dict):
        i = o["at"]
        if i in self.relic_set:
            return "relics cannot be claimed: occupy them with units"
        if self.owner[i] is not None:
            return f"tile already owned by {self.owner[i]}"
        if self.terrain[i] not in C.PASSABLE:
            return "tile is impassable"
        if not self._adjacent_to(p.id, i):
            return "tile is not 4-adjacent to your territory"
        if self._hostile_units_on(p.id, i):
            return "hostile units on the tile"
        cost = claim_cost(p.tiles)
        if p.resources["influence"] < cost:
            return f"claim costs {cost} influence"
        p.resources["influence"] -= cost
        self._set_owner(i, p.id)
        self._emit("claim", player=p.id, cost=cost, **self._xy_fields(i))
        return None

    def _act_settle(self, p: Player, o: dict):
        i = o["at"]
        if self.terrain[i] not in C.PASSABLE:
            return "tile is impassable"
        if i in self.relic_set:
            return "cannot settle on a relic"
        if i in self.cities:
            return "there is already a city there"
        own = self.owner[i]
        if own is None:
            if not self._adjacent_to(p.id, i):
                return "tile is not yours and not 4-adjacent to your territory"
        elif own != p.id:
            return f"tile owned by {own}"
        if self.nearest_city_distance(i) < C.CITY_MIN_DISTANCE:
            return "too close to another city"
        if self._hostile_units_on(p.id, i):
            return "hostile units on the tile"
        cost = settle_cost(len(self.owned_cities(p.id)))
        if not _can_pay(p.resources, cost):
            return f"cannot afford settle ({_fmt_cost(cost)})"
        _pay(p.resources, cost)
        city = self._found_city(p, i)
        self._emit("city_founded", player=p.id, name=city.name, **self._xy_fields(i))
        return None

    def _act_recruit(self, p: Player, o: dict):
        i = o["city"]
        city = self.cities.get(i)
        if city is None or city.owner != p.id:
            return "you do not own that city"
        cost = unit_cost(o["unit"], o["count"])
        if not _can_pay(p.resources, cost):
            return f"cannot afford {o['count']} {o['unit']} ({_fmt_cost(cost)})"
        _pay(p.resources, cost)
        self._recruit_queue.append((p.id, i, o["unit"], o["count"], o))
        self._emit("recruit", player=p.id, unit=o["unit"], count=o["count"], **self._xy_fields(i))
        return None

    def _act_disband(self, p: Player, o: dict):
        i = o["at"]
        have = self.armies.get(i, {}).get(p.id)
        if not have:
            return "no units there"
        removed = {}
        for u, c in o["units"].items():
            k = min(c, have.get(u, 0))
            if k > 0:
                have[u] -= k
                removed[u] = k
        if not removed:
            return "no such units there"
        self._clean_armies()
        self._emit("disband", player=p.id, units=removed, **self._xy_fields(i))
        return None

    # ---------------------------------------------------------------- 5
    def _check_path(self, pid: str, src: int, path: list, units: dict, start: dict) -> str | None:
        restricted = self._move_restricted
        if len(path) == 2 and any(u != "cavalry" for u in units):
            return "2-step moves require all moved units to be cavalry"
        prev = src
        for k, step in enumerate(path):
            if step not in self.neighbors[prev]:
                return "path is not 4-adjacent"
            if self.terrain[step] not in C.PASSABLE:
                return "path crosses impassable terrain"
            own = self.owner[step]
            if own is not None and own != pid and self._pair(pid, own) in restricted:
                return f"cannot enter territory of treaty partner {own}"
            here = start.get(step, {})
            for q in here:
                if q != pid and self._pair(pid, q) in restricted:
                    return f"cannot move onto an army of treaty partner {q}"
            if len(path) == 2 and k == 0:
                if any(q != pid and self.hostile(pid, q) for q in here):
                    return "cannot move through a tile with a hostile army"
                city = self.cities.get(step)
                if city is not None and city.owner != pid and self.hostile(pid, city.owner):
                    return "cannot move through a hostile city"
            prev = step
        return None

    def _phase_movement(self, orders: dict) -> None:
        start = {i: {q: dict(u) for q, u in per.items()} for i, per in self.armies.items()}
        remaining = {i: {q: dict(u) for q, u in per.items()} for i, per in self.armies.items()}
        groups: list[_Group] = []
        for p in self._alive_in_order():
            for o in orders[p.id]:
                if o["type"] != "move":
                    continue
                src = o["from"]
                avail = remaining.get(src, {}).get(p.id)
                if not avail:
                    self._fail(p.id, o, "no units at the source tile")
                    continue
                take = {}
                for u, c in o["units"].items():
                    k = min(c, avail.get(u, 0))
                    if k > 0:
                        take[u] = k
                if not take:
                    self._fail(p.id, o, "requested units are no longer there")
                    continue
                reason = self._check_path(p.id, src, o["path"], take, start)
                if reason:
                    self._fail(p.id, o, reason)
                    continue
                for u, k in take.items():
                    avail[u] -= k
                groups.append(_Group(p.id, src, list(o["path"]), take, o))

        self._border_clashes(groups)

        # land every move
        new: dict = {}
        stationary = set()
        for i in sorted(remaining):
            for q, u in remaining[i].items():
                u = {k: c for k, c in u.items() if c > 0}
                if u:
                    new.setdefault(i, {})[q] = u
                    stationary.add((i, q))
        for g in groups:
            if not g.units:
                continue
            tgt = new.setdefault(g.path[-1], {}).setdefault(g.pid, {})
            for u, c in g.units.items():
                tgt[u] = tgt.get(u, 0) + c

        # battles
        defeated_garrisons = set()
        for i in sorted(new):
            per = new[i]
            city = self.cities.get(i)
            cown = city.owner if city is not None else None
            parts = list(per)
            if cown is not None and cown not in per and any(self.hostile(cown, q) for q in parts):
                parts.insert(0, cown)
            if not any(self.hostile(a, b) for ai, a in enumerate(parts) for b in parts[ai + 1:]):
                continue
            defensive = self.terrain[i] in C.DEFENSIVE_TERRAIN
            sides = []
            for q in parts:
                is_owner = q == cown
                defender = is_owner or (i, q) in stationary
                sides.append(combat.Side(
                    pid=q, units=dict(per.get(q, {})), defender=defender, city_owner=is_owner,
                    garrison=float(city.garrison) if is_owner else 0.0,
                    terrain_bonus=defender and defensive,
                    order=(0 if defender else 1, self._by_id[q].index)))
            records = combat.resolve(sides, self.hostile, walls=city.walls if city is not None else 0)
            for rec in records:
                self._emit("battle", **self._xy_fields(i), clash=False, **rec)
            new_per = {}
            for s in sides:
                if s.units:
                    new_per[s.pid] = s.units
                if s.city_owner and s.defeated:
                    defeated_garrisons.add(i)
            if new_per:
                new[i] = new_per
            else:
                del new[i]

        self.armies = new
        # captures: cities first, then plain tiles
        for i in sorted(new):
            if i not in self.cities or i not in defeated_garrisons:
                continue
            q = self._capturer(i, new.get(i, {}))
            if q is not None:
                self._capture_city(i, q)
        for i in sorted(new):
            if i in self.cities:
                continue
            own = self.owner[i]
            relic = i in self.relic_set
            if own is None and not relic:
                continue
            # relic tiles are also taken when unowned: occupation is the only
            # way to acquire a relic
            q = self._capturer(i, new.get(i, {}))
            if q is not None:
                self._set_owner(i, q)
                self._emit("tile_captured", **self._xy_fields(i), **{"from": own, "to": q}, relic=relic)

    def _capturer(self, i: int, per: dict) -> str | None:
        """Who captures tile ``i`` given the units on it after the battles
        (all of them belong to players at peace with each other): among the
        players hostile to the tile's owner (any player for an unowned tile),
        the one with the largest raw power, then the lowest seat. None if the
        owner itself (or nobody hostile to it) is there."""
        own = self.owner[i]
        if not per or own in per:
            return None
        cands = [q for q in per if own is None or self.hostile(q, own)]
        if not cands:
            return None
        return min(cands, key=lambda q: (-combat.military_power(per[q]), self._by_id[q].index))

    def _border_clashes(self, groups: list) -> None:
        """Hostile groups crossing the same edge in opposite directions fight
        there: first steps against first steps, then every crossing that
        involves the second step of a cavalry move (survivors only)."""
        by_edge: dict = {}
        for g in groups:
            by_edge.setdefault((g.src, g.path[0]), []).append(g)
        self._clash_edges(by_edge)
        by_edge = {}
        for g in groups:
            if not g.units:
                continue
            prev = g.src
            for step in g.path:
                lst = by_edge.setdefault((prev, step), [])
                if g not in lst and g not in by_edge.get((step, prev), ()):
                    lst.append(g)
                prev = step
        self._clash_edges(by_edge)

    def _clash_edges(self, by_edge: dict) -> None:
        for (a, b) in sorted(by_edge):
            if a > b or (b, a) not in by_edge:
                continue
            sides = []
            for d, key in enumerate(((a, b), (b, a))):
                per: dict = {}
                for g in by_edge[key]:
                    if not g.units:
                        continue
                    per.setdefault(g.pid, []).append(g)
                for q, gs in per.items():
                    units: dict = {}
                    for g in gs:
                        for u, c in g.units.items():
                            units[u] = units.get(u, 0) + c
                    sides.append(combat.Side(pid=q, units=units, order=(d, self._by_id[q].index), payload=gs))
            fwd = [s for s in sides if s.order[0] == 0]
            back = [s for s in sides if s.order[0] == 1]
            if not any(self.hostile(s.pid, r.pid) for s in fwd for r in back):
                continue
            records = combat.resolve(sides, self.hostile)
            ax, ay = self.xy(a)
            bx, by = self.xy(b)
            for rec in records:
                self._emit("battle", x=ax, y=ay, to=[bx, by], clash=True, **rec)
            for s in sides:
                left = dict(s.units)
                for g in s.payload:
                    kept = {}
                    for u, c in g.units.items():
                        k = min(c, left.get(u, 0))
                        if k > 0:
                            kept[u] = k
                            left[u] -= k
                    g.units = kept

    def _capture_city(self, i: int, pid: str) -> None:
        city = self.cities[i]
        old = city.owner
        victim = self._by_id[old]
        p = self._by_id[pid]
        city.owner = pid
        self._set_owner(i, pid)
        city.walls = max(0, city.walls - 1)
        wonder_lost = city.wonder_stage
        city.wonder_stage = 0
        if victim.wonder_city == i:
            victim.wonder_city = None
        transferred = 0
        for j in self.radius(i, 1):
            if j == i or self.owner[j] != old or j in self.cities or j in self.relic_set:
                continue
            if any(q != pid for q in self.armies.get(j, {})):
                continue
            self._set_owner(j, pid)
            transferred += 1
        plunder = {}
        if city.capital and city.original_owner == old:
            for r in C.TRADABLE:
                amt = int(victim.resources.get(r, 0) * C.PLUNDER_FRACTION)
                if amt > 0:
                    victim.resources[r] -= amt
                    p.resources[r] += amt
                    plunder[r] = amt
        self._emit("city_captured", **self._xy_fields(i), city=city.name, **{"from": old, "to": pid},
                   capital=city.capital, plunder=plunder, wonder_destroyed=wonder_lost, tiles=transferred)

    # ---------------------------------------------------------------- 6
    def _phase_spawn(self) -> None:
        for pid, i, unit, count, o in self._recruit_queue:
            city = self.cities.get(i)
            if city is None or city.owner != pid or not self._by_id[pid].alive:
                self._fail(pid, o, "city lost before the recruits were ready; recruits lost")
                continue
            if self._hostile_units_on(pid, i):
                self._fail(pid, o, "hostile units hold the city; recruits lost")
                continue
            per = self.armies.setdefault(i, {}).setdefault(pid, {})
            per[unit] = per.get(unit, 0) + count
        self._recruit_queue = []

    # ---------------------------------------------------------------- 7
    def _raw_income(self) -> tuple[dict, list]:
        """Per-player un-seasoned yields and deposit extraction list."""
        inc = {p.id: {r: 0 for r in C.RESOURCES} for p in self.players}
        extraction = []
        terrain, owner, imp, dep = self.terrain, self.owner, self.improvement, self.deposits
        cities, relics = self.cities, self.relic_set
        for i, o in enumerate(owner):
            if o is None:
                continue
            acc = inc[o]
            city = cities.get(i)
            if city is not None:
                for r, v in C.CITY_YIELD.items():
                    acc[r] += v
                if city.capital:
                    acc["influence"] += C.CAPITAL_EXTRA_INFLUENCE
                if city.market_hall:
                    acc["gold"] += C.MARKET_HALL_GOLD
                continue
            t = terrain[i]
            y = C.TERRAIN[t]["yield"]
            b = imp[i]
            d = C.DEPOSITS.get(t)
            if b is None and d is None:
                for r, v in y.items():
                    acc[r] += v
            else:
                amounts = dict(y)
                if b is not None:
                    for r, v in C.IMPROVEMENTS[b]["bonus"].items():
                        amounts[r] = amounts.get(r, 0) + v
                if d is not None:
                    res = d[0]
                    take = min(amounts.get(res, 0), dep[i])
                    amounts[res] = take
                    if take > 0:
                        extraction.append((i, take))
                for r, v in amounts.items():
                    acc[r] += v
            if i in relics:
                acc["influence"] += C.RELIC_INFLUENCE
        return inc, extraction

    def _seasoned(self, raw: dict, turn: int) -> dict:
        _, _, mods = season(turn)
        return {r: (int(math.floor(v * mods[r] + 1e-9)) if r in mods else v) for r, v in raw.items()}

    def _upkeep(self, pid: str, units: dict | None = None) -> int:
        units = units if units is not None else self.units_of(pid)
        return sum(c * C.UNITS[u]["upkeep"] for u, c in units.items())

    def _phase_economy(self) -> None:
        raw, extraction = self._raw_income()
        for i, take in extraction:
            self.deposits[i] = max(0, self.deposits[i] - take)
        for p in self._alive_in_order():
            inc = self._seasoned(raw[p.id], self.turn)
            for r, v in inc.items():
                p.resources[r] += v
        D.pay_contracts(self)          # after yields, before upkeep (§13.3)
        for p in self._alive_in_order():
            upkeep = self._upkeep(p.id)
            p.resources["food"] -= upkeep
            if p.resources["food"] < 0:
                deficit = -p.resources["food"]
                p.resources["food"] = 0
                lost = self._starve(p.id, int(math.ceil(deficit / 2)))
                self._emit("starvation", player=p.id, deficit=deficit, lost=lost)
            for r, cap in self.caps(p.id).items():
                if p.resources[r] > cap:
                    p.resources[r] = cap
        for r in C.MARKET_RESOURCES:
            M.revert(self.pools[r], self.pool_init[r])

    def _starve(self, pid: str, n: int) -> dict:
        lost: dict = {}
        stacks = {i: per[pid] for i, per in self.armies.items() if pid in per}
        for _ in range(n):
            best = None
            for u in C.STARVATION_ORDER:
                cands = [(c.get(u, 0), -i) for i, c in stacks.items() if c.get(u, 0) > 0]
                if cands:
                    if best is None or C.UNITS[u]["upkeep"] > C.UNITS[best[0]]["upkeep"]:
                        best = (u, -max(cands)[1])
            if best is None:
                break
            u, i = best
            stacks[i][u] -= 1
            if stacks[i][u] == 0:
                del stacks[i][u]
            lost[u] = lost.get(u, 0) + 1
        self._clean_armies()
        return lost

    # ---------------------------------------------------------------- 7½
    def _phase_intel(self, orders: dict) -> None:
        """Espionage (fog games): counterintel purchases, then spy payments,
        then every mission is compared with its target's counter-intelligence
        rating (the same for all missions this turn), then pools decay."""
        for p in self._rotated():
            for o in orders.get(p.id, ()):
                if o["type"] != "counterintel":
                    continue
                inv = o["invest"]
                if p.resources["gold"] < inv:
                    self._fail(p.id, o, f"cannot afford counterintel ({inv} gold)")
                    continue
                p.resources["gold"] -= inv
                p.ci_pool += inv
                self._emit("counterintel", vis=[p.id], player=p.id, invest=inv, pool=p.ci_pool)
        missions = []
        for p in self._rotated():
            for o in orders.get(p.id, ()):
                if o["type"] != "spy":
                    continue
                tgt = self._by_id.get(o["target"])
                inv = o["invest"]
                if tgt is None or not tgt.alive:
                    self._fail(p.id, o, "target eliminated; nothing spent")
                elif p.resources["gold"] < inv:
                    self._fail(p.id, o, f"cannot afford spy ({inv} gold)")
                else:
                    p.resources["gold"] -= inv
                    missions.append((p, tgt, o))
        ci = {q.id: F.ci_rating(self, q.id) for q in self.players}
        for p, tgt, o in missions:
            inv, rating, mission = o["invest"], ci[tgt.id], o["mission"]
            if inv >= 2 * rating:
                outcome = "success"
            elif inv >= rating:
                outcome = "detected"
            else:
                outcome = "failed"
            self._emit("spy_report", vis=[p.id], player=p.id, target=tgt.id, mission=mission,
                       invest=inv, outcome=outcome)
            if outcome != "success":
                self._emit("spy_detected", vis=[tgt.id], player=tgt.id, spy=p.id, mission=mission,
                           outcome=outcome)
            if outcome == "failed":
                p.spy_incidents += 1
                self._emit("spy_incident", spy=p.id, target=tgt.id)
            else:
                self._pending_intel.append((p.id, tgt.id, mission, outcome))
        num, den = C.CI_DECAY
        for q in self.players:
            q.ci_pool = q.ci_pool * num // den
        self._invalidate()

    # ---------------------------------------------------------------- 8
    def _phase_bookkeeping(self) -> None:
        t = self.turn
        # eliminations
        for p in self._alive_in_order():
            if not any(c.owner == p.id for c in self.cities.values()):
                self._eliminate(p)
        # relic streaks
        need = relics_needed(len(self.relics))
        for p in self._alive_in_order():
            held = sum(1 for r in self.relics if self.owner[r] == p.id and self.relic_guarded(r))
            p.relic_streak = p.relic_streak + 1 if held >= need else 0
        # expiries
        for key in sorted(self.treaties):
            if self.treaties[key] <= t:
                del self.treaties[key]
                self._emit("treaty_expired", a=key[0], b=key[1])
        self.treaty_proposals = [pr for pr in self.treaty_proposals if pr["turn"] >= t]
        D.expire_deals(self)
        self.market_history.append({"turn": t, "prices": self._prices()})
        if len(self.market_history) > C.MARKET_HISTORY_TURNS:
            self.market_history = self.market_history[-C.MARKET_HISTORY_TURNS:]
        self._invalidate()
        self._check_victory()
        self.turn += 1
        self._orders = {}
        self._submitted = {p.id: False for p in self.players}
        self._invalidate()

    def _eliminate(self, p: Player) -> None:
        p.alive = False
        p.eliminated_turn = self.turn
        for i in list(self.armies):
            self.armies[i].pop(p.id, None)
        self._clean_armies()
        for i, o in enumerate(self.owner):
            if o == p.id:
                self._set_owner(i, None)
        for key in [k for k in self.treaties if p.id in k]:
            del self.treaties[key]
        self.treaty_proposals = [pr for pr in self.treaty_proposals if p.id not in (pr["from"], pr["to"])]
        D.on_eliminated(self, p.id)
        p.wonder_city = None
        self._invalidate()
        p.final_score = self.stats()[p.id]["score"]
        self._emit("eliminated", player=p.id)

    def _prices(self) -> dict:
        return {r: round(M.spot_price(self.pools[r]), 4) for r in C.MARKET_RESOURCES}

    # ==================================================================
    # stats, score, victory
    # ==================================================================
    def stats(self) -> dict:
        """Per-player derived stats (cached until the state changes)."""
        if self._stats is not None:
            return self._stats
        n = len(self.players)
        raw, _ = self._raw_income() if self.status != "lobby" else ({p.id: {r: 0 for r in C.RESOURCES} for p in self.players}, [])
        units = {p.id: {u: 0 for u in C.UNIT_TYPES} for p in self.players}
        for per in self.armies.values():
            for q, u in per.items():
                tot = units[q]
                for k, c in u.items():
                    tot[k] += c
        cities = {p.id: 0 for p in self.players}
        capitals = {p.id: 0 for p in self.players}
        for c in self.cities.values():
            cities[c.owner] += 1
            if c.capital:
                capitals[c.owner] += 1
        relics = {p.id: 0 for p in self.players}
        guarded = {p.id: 0 for p in self.players}
        for r in self.relics:
            o = self.owner[r]
            if o is not None:
                relics[o] += 1
                if self.relic_guarded(r):
                    guarded[o] += 1
        thr = thresholds(n, self.max_turns) if n else {}
        alive_count = sum(1 for p in self.players if p.alive)
        out = {}
        for p in self.players:
            mp = combat.military_power(units[p.id])
            ws = self.wonder_stage(p.id) if self.status != "lobby" else 0
            res = p.resources
            sw, sd = C.SCORE_WEIGHTS, C.SCORE_DIVISORS
            score = (sw["tiles"] * p.tiles + sw["cities"] * cities[p.id]
                     + sw["capitals_held"] * capitals[p.id] + sw["wonder_stage"] * ws
                     + res["influence"] // sd["influence"] + res["gold"] // sd["gold"]
                     + sw["relics_held"] * relics[p.id] + mp // sd["military_power"])
            if not p.alive and p.final_score is not None:
                score = p.final_score
            if thr:
                need_cap = thr["conquest_capitals"]
                conquest = min(1.0, capitals[p.id] / need_cap) if need_cap else 0.0
                if p.alive and n >= 2 and alive_count == 1:
                    conquest = 1.0
                progress = {
                    "conquest": round(conquest, 3),
                    "wonder": round(min(1.0, ws / C.WONDER_VICTORY_STAGE), 3),
                    "influence": round(min(1.0, max(0, res["influence"]) / C.INFLUENCE_VICTORY), 3),
                    "relics": round(min(1.0, p.relic_streak / C.RELIC_VICTORY_TURNS)
                                    if guarded[p.id] >= thr["relics_needed"] else 0.0, 3),
                    "economic": round(min(1.0, max(0, res["gold"]) / C.ECONOMIC_VICTORY_GOLD), 3),
                    "score": round(min(1.0, self.turn / self.max_turns) if self.max_turns else 1.0, 3),
                }
            else:
                progress = {}
            if not p.alive:
                progress = {k: 0.0 for k in progress}
            out[p.id] = {
                "tiles": p.tiles,
                "cities": cities[p.id],
                "capitals_held": capitals[p.id],
                "military_power": mp,
                "units": units[p.id],
                "wonder_stage": ws,
                "relics_held": relics[p.id],
                "relics_guarded": guarded[p.id],
                "score": int(score),
                "income": self._seasoned(raw[p.id], self.turn) if p.alive else {r: 0 for r in C.RESOURCES},
                "upkeep": self._upkeep(p.id, units[p.id]),
                "victory_progress": progress,
            }
        self._stats = out
        return out

    def _check_victory(self) -> None:
        n = len(self.players)
        thr = thresholds(n, self.max_turns)
        st = self.stats()
        alive = self._alive_in_order()
        met: dict = {}
        for p in alive:
            s = st[p.id]
            conds = set()
            if n >= 2 and (s["capitals_held"] >= thr["conquest_capitals"] or len(alive) == 1):
                conds.add("conquest")
            if s["wonder_stage"] >= C.WONDER_VICTORY_STAGE:
                conds.add("wonder")
            if p.relic_streak >= C.RELIC_VICTORY_TURNS:
                conds.add("relics")
            if p.resources["influence"] >= C.INFLUENCE_VICTORY:
                conds.add("influence")
            if p.resources["gold"] >= C.ECONOMIC_VICTORY_GOLD:
                conds.add("economic")
            if conds:
                met[p.id] = next(c for c in C.VICTORY_CONDITIONS if c in conds)
        if met:
            winner = max(met, key=lambda q: (st[q]["score"], -self._by_id[q].index))
            self._finish(winner, met[winner])
        elif not alive:
            self._finish(None, "score")
        elif self.turn + 1 >= self.max_turns:
            winner = max((p.id for p in alive), key=lambda q: (st[q]["score"], -self._by_id[q].index))
            self._finish(winner, "score")

    def placements(self, winner: str | None = None) -> list:
        st = self.stats()
        alive = sorted((p for p in self.players if p.alive and p.id != winner),
                       key=lambda p: (-st[p.id]["score"], p.index))
        dead = sorted((p for p in self.players if not p.alive and p.id != winner),
                      key=lambda p: (-(p.eliminated_turn or 0), -st[p.id]["score"], p.index))
        out = [q.id for q in alive + dead]
        return ([winner] + out) if winner is not None else out

    def _finish(self, winner: str | None, condition: str) -> None:
        places = self.placements(winner)
        if winner is None and places:
            winner = places[0]
        st = self.stats()
        self.result = {
            "winner": winner,
            "condition": condition,
            "turn": self.turn,
            "placements": places,
            "scores": {p.id: st[p.id]["score"] for p in self.players},
        }
        self.status = "finished"
        self._emit("victory", winner=winner, condition=condition)
