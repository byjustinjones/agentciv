"""A configurable planning bot: the shared economic and military "muscle"
used by the economist, rusher, turtle and strategist bots.

:class:`PlannerBot` splits a turn into small behaviours (``sell``,
``expand``, ``develop``, ``defend``, ``offense``, ``garrison_moves``, ...)
that write into a budgeted :class:`~agentciv.bots.common.Plan`. Subclasses
choose which behaviours run, in which priority order, and tune them with
class-level knobs. All numbers come from the engine constants.
"""
from __future__ import annotations

import math

from agentciv.engine import combat as CB
from agentciv.engine import constants as C
from agentciv.engine.rules import claim_cost, settle_cost

from .common import (CAPPED, Plan, SafeBot, World, add_units, bank_limit, base_price,
                     best_counter, best_improvement, buy_price, food_projection,
                     raw_income, raw_strength, season_mods, simulate_attack,
                     threat_to, tile_yield, total_units,
                     treaty_proposals_to_me, value_of, rivals_by_power)
from .trading import Trader


class PlannerBot(Trader, SafeBot):
    """Base class with reusable behaviours. See subclasses for strategies."""

    name = "planner"

    # ---- knobs (override in subclasses) ---------------------------------
    INFLUENCE_WEIGHT = 3.0        # value of 1 influence in "gold" units
    INFLUENCE_RESERVE = 0         # influence never spent on claims
    TREATY_PLEDGE = 30            # bank gold pledged on each treaty we sign (if free)
    ALLOW_TEMPLES = True
    TEMPLE_BIAS = 1.0             # multiplier on temple ROI
    MAX_CITIES = 8
    SETTLE_RADIUS = 6             # how far (path) to look for city sites
    RELIC_INTEREST = 0.0          # bonus value per relic for sites/claims
    DEFENSE_REACH = 2             # turns of warning considered for threats
    DEFENSE_MARGIN = 1.0          # defence must beat threat * margin
    ASSIGN_THREATS = True         # each army only threatens our nearest city
    GARRISON_THREAT_SHARE = 1.0   # share of units inside their own city counted as a threat
    MIN_GARRISON = 1              # units kept in the capital at all times
    CITY_GARRISON = 0             # units kept in other cities
    SELL_FLOOR = 0.65             # never sell below this fraction of base price
    SELL_SURPLUS = True           # sell stock above reserves (not only overflow)
    MIN_ROI = 1 / 45.0            # improvements must repay within ~45 turns
    FOOD_BUFFER_TURNS = 14        # food projection horizon
    MARKET_HALL = True
    WAREHOUSE = True
    BUY_FOR_IMPROVEMENTS = False  # buy missing stone/wood for improvements

    def decide(self, view: dict) -> list:
        w = World(view)
        p = self.new_plan(w)
        self.w, self.p = w, p
        self.prepare()
        for step in self.pipeline() + [self.bank_step]:
            try:
                step()
            except Exception as e:  # keep going with the other behaviours
                self.last_error = f"{getattr(step, '__name__', step)}: {type(e).__name__}: {e}"
        self.memory["last_orders"] = (w.turn, list(p.orders))
        return p.orders

    BANK_KEEP = 40                # gold left on hand after banking

    def bank_wanted(self) -> bool:
        """Move gold into the bank this turn (economic victory, rules §5)?"""
        return False

    def bank_step(self) -> None:
        """Last step: bank what is left, up to this turn's limit, keeping
        BANK_KEEP gold and this turn's contract instalments on hand."""
        if not self.bank_wanted() or self.p.full():
            return
        p = self.p
        amt = min(bank_limit(self.w), int(p.budget.get("gold", 0)) - self.BANK_KEEP - self.contract_gold)
        if amt >= 1:
            p.budget["gold"] -= amt
            p.orders.append({"type": "bank", "gold": amt})

    def note_contested(self) -> None:
        """Claims/settles that failed because a rival tried the same spot get
        a random back-off (1-4 turns): two players retrying the same tile
        every turn would both fail forever."""
        w = self.w
        back = self.memory.setdefault("backoff", {})
        last = self.memory.get("last_orders")
        if not last or last[0] != w.turn - 1:
            return
        orders = last[1]
        for e in w.events:
            if e.get("type") != "order_failed" or e.get("player") != w.me:
                continue
            if not str(e.get("reason", "")).startswith("contested"):
                continue
            k = e.get("index")
            if isinstance(k, int) and 0 <= k < len(orders):
                at = orders[k].get("at")
                if isinstance(at, list) and len(at) == 2:
                    back[w.idx(at[0], at[1])] = w.turn + self.rng.randint(1, 4)
        for i in [i for i, t in back.items() if t < w.turn]:
            del back[i]

    def backed_off(self, i: int) -> bool:
        return self.memory.get("backoff", {}).get(i, -1) >= self.w.turn

    def pipeline(self) -> list:
        return [self.diplomacy, self.food_safety, self.plan_site, self.defend, self.counter_relics,
                self.sell, self.expand, self.develop, self.garrison_moves]

    # ------------------------------------------------------------------
    # preparation
    # ------------------------------------------------------------------
    def prepare(self) -> None:
        w = self.w
        self.wts = self.weights()
        self.raw = raw_income(w)
        self.food_short = False
        self.reserved: dict = {}
        self.locked: dict = {}             # tile -> units that must stay
        self.site = None
        self.site_path: list = []
        rem = max(1, w.max_turns - w.turn)
        self.remaining = rem
        self.reserve_contracts()
        self.contract_gold = self.reserved.get("gold", 0)
        if self.bank_wanted():
            # earlier steps must leave this turn's bank allowance
            self.reserved["gold"] = self.contract_gold + bank_limit(w)
        self.note_contested()
        # per-game random tie-breaker per tile: breaking ties by tile index
        # would favour one map direction (and so some start positions)
        salt = self.memory.get("salt")
        if salt is None or len(salt) != w.n_tiles:
            salt = self.memory["salt"] = [self.rng.random() for _ in range(w.n_tiles)]
        self.salt = salt

    # ------------------------------------------------------------------
    # contracts (§13.3): keep what the instalments due this turn need
    # ------------------------------------------------------------------
    def honour_contract(self, c: dict) -> bool:
        """Reserve this turn's instalment of contract ``c`` (we are the
        payer)? Default: always — a default costs influence, reputation, the
        bank's gold up to the remaining obligation and the economic streak."""
        return True

    def reserve_contracts(self) -> None:
        """Instalments are paid in phase 7 after yields: keep what this
        turn's income will not cover."""
        w = self.w
        due: dict = {}
        self.contract_due = due
        for c in w.view.get("contracts", []) or []:
            if c.get("payer") != w.me or not self.honour_contract(c):
                continue
            for r, v in (c.get("per_turn") or {}).items():
                due[r] = due.get(r, 0) + int(v)
        self.contract_due = due
        if not due:
            return
        mods = season_mods(w.turn)
        for r, v in due.items():
            inc = math.floor(self.raw.get(r, 0) * mods.get(r, 1.0))
            if r == "food":
                inc -= w.upkeep
            keep = v - int(0.8 * max(0, inc))
            if keep > 0:
                self.reserved[r] = self.reserved.get(r, 0) + keep

    # ------------------------------------------------------------------
    # negotiation (§13) — see agentciv.bots.trading.Trader
    # ------------------------------------------------------------------
    def trade_setup(self, w: World) -> None:
        self.w = w
        self.p = Plan(w)
        self.raw = raw_income(w)
        self.wts = self.weights()
        self.reserved = {}
        self.locked = {}
        self.food_short = False
        self.remaining = max(1, w.max_turns - w.turn)
        self.__dict__.pop("_threat_cache", None)

    def trade_needs(self) -> tuple:
        """Stock we want to keep: the keep levels, a winter food buffer and
        the next instalments of our contracts."""
        w = self.w
        needs = {r: self.keep(r) for r in CAPPED}
        needs["food"] += 4 * w.upkeep
        food_raw = self.raw.get("food", 0)
        if food_projection(w, food_raw, w.upkeep, self.FOOD_BUFFER_TURNS) < 20:
            needs["food"] += 60
        gold = 0
        inv = self.memory.get("invest_short")
        if inv and inv[0] >= w.turn - 1 and inv[1] > 0:
            # good investments are waiting for gold: gold now is worth more
            gold = int(w.res.get("gold", 0)) + inv[1]
        from .common import contract_obligations
        for r, v in contract_obligations(w).items():
            if r == "gold":
                gold += 2 * v
            elif r in needs:
                needs[r] += 2 * v
        return needs, gold

    def peace_bias(self) -> dict:
        # no peace with relic runners: we may have to hit their relics
        return {q: -400.0 for q in self.w.rivals if self.relic_runner(q)}

    def weights(self) -> dict:
        w = self.w
        out = {}
        for r in CAPPED:
            pool = w.pools.get(r)
            price = pool[1] / pool[0] if pool and pool[0] > 0 else base_price(r)
            out[r] = max(0.4 * base_price(r), min(1.6 * base_price(r), price))
        out["gold"] = 1.25
        out["influence"] = self.INFLUENCE_WEIGHT
        return out

    def reserve(self, r: str) -> int:
        return self.reserved.get(r, 0)

    def add_reserve(self, cost: dict) -> None:
        for r, v in cost.items():
            self.reserved[r] = self.reserved.get(r, 0) + v

    # ------------------------------------------------------------------
    # diplomacy
    # ------------------------------------------------------------------
    def pledge(self) -> int:
        w = self.w
        room = w.bond_free(w.me) - w.bond_required(w.me) - self.p.pledged
        return max(0, min(self.TREATY_PLEDGE, room))

    def diplomacy(self) -> None:
        """Default: accept every treaty proposal."""
        for pr in treaty_proposals_to_me(self.w):
            self.p.accept_treaty(pr["from"], self.pledge())

    def accept_all_and_propose(self, turns: int = 50, only_neighbors: bool = False) -> None:
        w, p = self.w, self.p
        for pr in treaty_proposals_to_me(w):
            if not self.relic_runner(pr["from"]):
                p.accept_treaty(pr["from"], self.pledge())
        for q in rivals_by_power(w):
            if q not in w.treaties and (w.turn + hash_pid(q)) % 3 == 0 and not self.relic_runner(q):
                p.propose(q, turns, self.pledge())

    def relic_runner(self, q: str) -> bool:
        """Is ``q`` holding (nearly) enough guarded relics for the relic
        victory? Such a player gets no treaty: we may have to hit a relic."""
        w = self.w
        need = w.thresholds.get("relics_needed", 99)
        pl = w.players.get(q, {})
        return pl.get("relics_guarded", 0) >= need - 1 or pl.get("relic_streak", 0) > 0

    # ------------------------------------------------------------------
    # stopping a relic victory
    # ------------------------------------------------------------------
    COUNTER_RELICS = True          # attack a rival's relic guard when its streak runs
    COUNTER_RELIC_STREAK = 3       # ...once the streak is this long

    def counter_relics(self) -> None:
        """If a hostile rival is on a relic streak, raise a strike force
        against its weakest guarded relic and take it (a relic changes hands
        when our units are alone on it, which resets the rival's streak)."""
        w, p = self.w, self.p
        self.relic_target = None
        if not self.COUNTER_RELICS or not w.my_cities:
            return
        need = w.thresholds.get("relics_needed", 99)
        hold = w.thresholds.get("relic_turns", C.RELIC_VICTORY_TURNS)
        worst = None
        for q in w.rivals:
            pl = w.players.get(q, {})
            if pl.get("relics_guarded", 0) < need or pl.get("relic_streak", 0) < self.COUNTER_RELIC_STREAK:
                continue
            if w.at_peace(w.me, q):
                continue
            left = hold - pl.get("relic_streak", 0)
            if worst is None or left < worst[0]:
                worst = (left, q)
        if worst is None:
            return
        q = worst[1]
        enter = w.can_enter_fn(w.me)
        srcs = list(w.my_armies) + list(w.my_cities)
        dist = w.bfs(srcs, enter)
        best = None
        for r in w.relics:
            if w.owner[r] != q or q not in w.armies.get(r, {}) or r not in dist:
                continue
            force = self.min_force(r)
            size = sum(force.values()) + dist[r] / 3.0
            if best is None or size < best[0]:
                best = (size, r, force)
        if best is None:
            return
        _, tgt, force = best
        if best[0] > 3 * worst[0] + 12:
            return      # too late / too strong to stop
        self.relic_target = tgt
        # recruit what is missing in the city closest to the target
        tdist = w.bfs([tgt], enter)
        city = min(w.my_cities, key=lambda c: (tdist.get(c, 999), c))
        have: dict = {}
        for i in w.my_armies:
            if tdist.get(i, 999) <= 12:
                have = add_units(have, self.free_units(i))
        have = add_units(have, p.recruited.get(city, {}))
        room = int(self.raw.get("food", 0) * 0.9 + p.budget.get("food", 0) / 12 - w.upkeep)
        for t, k in sorted(force.items()):
            miss = k - have.get(t, 0)
            if miss > 0 and room > 0:
                got = p.recruit(city, t, min(miss, room), {"food": 20})
                room -= got * C.UNITS[t]["upkeep"]
        self.offense(tgt, min_ratio=1.15)

    def min_force(self, tgt: int, ratio: float = 1.3) -> dict:
        """Smallest force of the best counter type that wins the simulated
        assault on ``tgt`` (with the defenders' neighbours) with ``ratio``."""
        w = self.w
        owner = w.cities[tgt]["owner"] if tgt in w.cities else w.owner[tgt]
        defenders = {q: dict(u) for q, u in w.armies.get(tgt, {}).items() if q != w.me}
        if owner:
            for j in w.nb[tgt]:
                u = w.armies.get(j, {}).get(owner)
                if u:
                    defenders[owner] = add_units(defenders.get(owner, {}), u)
        enemy: dict = {}
        for u in defenders.values():
            enemy = add_units(enemy, u)
        force: dict = {}
        siege = self.siege_needed(tgt)
        if siege:
            force["siege"] = siege
        t = best_counter(enemy or {"infantry": 1}, allowed=("infantry", "cavalry", "archer"))
        k = 1
        while k <= 60:
            force[t] = k
            win, _, r = simulate_attack(w, w.me, force, tgt, defenders_override=defenders, assume_war=True)
            if win and r >= ratio:
                break
            k += 1 if k < 10 else 3
        return force

    # ------------------------------------------------------------------
    # food
    # ------------------------------------------------------------------
    def food_safety(self) -> None:
        """Make sure the army can be fed through the next winter: flag a
        shortage (farms get priority, food is not sold), buy food, and as a
        last resort disband units instead of starving."""
        w, p = self.w, self.p
        food_raw = self.raw.get("food", 0)
        low = food_projection(w, food_raw, w.upkeep, self.FOOD_BUFFER_TURNS)
        self.food_low = low
        if low < 10:
            self.food_short = True
            need = 10 - low
            if w.res.get("gold", 0) > 30:
                p.buy("food", min(need, max(0, w.res["gold"] - 20)), max_price=1.6 * base_price("food"))
        # imminent starvation this turn: disband the cheapest-to-lose units
        mod = season_mods(w.turn).get("food", 1.0)
        this_turn = w.res.get("food", 0) + p.bought.get("food", 0) + math.floor(food_raw * mod) - w.upkeep
        if this_turn < 0:
            deficit = -this_turn
            for t in ("siege", "cavalry", "archer", "infantry"):
                for i, u in sorted(w.my_armies.items()):
                    if deficit <= 0:
                        break
                    have = p.available(i).get(t, 0)
                    if have <= 0:
                        continue
                    k = min(have, int(math.ceil(deficit / C.UNITS[t]["upkeep"])))
                    if p.disband(i, {t: k}):
                        deficit -= k * C.UNITS[t]["upkeep"]
        # reserve food for the leanest point ahead
        self.add_reserve({"food": max(0, 20 - min(0, low))})

    # ------------------------------------------------------------------
    # market
    # ------------------------------------------------------------------
    def sell(self) -> None:
        """Sell what would overflow the storage cap and (optionally) the
        surplus above reserves, never below ``SELL_FLOOR`` × base price
        (except to avoid overflow waste)."""
        w, p = self.w, self.p
        mods = season_mods(w.turn)
        for r in CAPPED:
            if r == "food" and self.food_short:
                continue
            stock = p.budget.get(r, 0)
            inc = math.floor(self.raw.get(r, 0) * mods.get(r, 1.0))
            if r == "food":
                inc -= w.upkeep
            cap = w.caps.get(r, C.STORAGE_BASE)
            overflow = stock + inc - cap
            sold = 0
            if self.SELL_SURPLUS:
                surplus = stock - self.reserve(r) - self.keep(r)
                if surplus >= 5:
                    sold = p.sell(r, surplus, self.SELL_FLOOR * base_price(r))
            if overflow - sold >= 3:
                # whatever would be wasted at the cap goes at almost any price
                p.sell(r, overflow - sold, 0.2 * base_price(r))

    def keep(self, r: str) -> int:
        """Stock of resource ``r`` to keep on hand (beyond reserves)."""
        return {"food": 40, "wood": 60, "stone": 40}.get(r, 0)

    # ------------------------------------------------------------------
    # expansion
    # ------------------------------------------------------------------
    def tile_value(self, i: int) -> float:
        w = self.w
        v = value_of(tile_yield(w, i, None), self.wts)
        b, gain, _ = best_improvement(w, i, self.wts, self.ALLOW_TEMPLES)
        if b:
            v += 0.5 * gain
        if i in w.relic_set:
            v += self.RELIC_INTEREST
        return v

    def find_site(self) -> tuple:
        """Best city site reachable through unowned/own land: returns
        (tile, path of tiles to claim) or (None, [])."""
        w = self.w
        me = w.me
        if not w.my_tiles or len(w.my_cities) + len(self.p.settles) >= self.MAX_CITIES:
            return None, []
        owner, terrain = w.owner, w.terrain

        def ok(i):
            return terrain[i] in C.PASSABLE and owner[i] in (None, me) and not w.hostile_units_on(me, i)
        # BFS from my territory; my tiles are distance 0
        dist = {}
        parent = {}
        from collections import deque
        dq = deque()
        for i in w.my_tiles:
            dist[i] = 0
            dq.append(i)
        while dq:
            i = dq.popleft()
            d = dist[i]
            if d >= self.SETTLE_RADIUS:
                continue
            for j in w.nb[i]:
                if j not in dist and ok(j):
                    dist[j] = d + 1
                    parent[j] = i
                    dq.append(j)
        enemy_cities = [c for c, cc in w.cities.items() if cc["owner"] != me]
        cap = w.capital if w.capital is not None else (w.my_cities[0] if w.my_cities else None)
        best, best_s = None, 0.0
        ccost = claim_cost(len(w.my_tiles))
        for s, d in dist.items():
            if s in w.relic_set or s in w.cities:
                continue
            if any(w.cheb(s, c) < C.CITY_MIN_DISTANCE for c in w.cities):
                continue
            gain = 6.0
            for j in w.radius(s, 1):
                if j == s or terrain[j] not in C.PASSABLE:
                    continue
                o = owner[j]
                if o is None:
                    gain += self.tile_value(j)
                    if j in w.relic_set:
                        gain += 3 * self.RELIC_INTEREST
            claims = max(0, d - 1)
            score = gain - claims * ccost * self.INFLUENCE_WEIGHT * 0.6
            # safety: stay away from rival cities, stay reasonably compact
            if enemy_cities:
                de = min(w.cheb(s, c) for c in enemy_cities)
                if de < 6:
                    score -= (6 - de) * 4.0 * self.site_danger_weight()
            if cap is not None:
                score -= max(0, w.manhattan(s, cap) - 8) * 1.0
            if score > best_s + 1e-9 or (best is not None and abs(score - best_s) <= 1e-9
                                         and self.salt[s] > self.salt[best]):
                best, best_s = s, score
        if best is None:
            return None, []
        path = []
        j = best
        while dist.get(j, 0) > 0:
            if owner[j] is None:
                path.append(j)
            j = parent[j]
        path.reverse()
        return best, path

    def site_danger_weight(self) -> float:
        return 1.0

    def expand(self) -> None:
        self.settle_step()
        self.claim_step()

    def plan_site(self) -> None:
        """Pick the next city site and reserve its cost (runs before selling
        so that the market step does not sell the settlers' resources)."""
        site, path = self.find_site()
        self.site, self.site_path = site, path
        if site is None:
            return
        cost = settle_cost(self.p.cities)
        claims_left = sum(1 for j in path if j != site)
        if claims_left <= 2:
            self.add_reserve(cost)
        else:
            self.add_reserve({r: v // 2 for r, v in cost.items()})

    def settle_step(self) -> None:
        w, p = self.w, self.p
        if self.site is None and not self.site_path:
            self.plan_site()
        site, path = self.site, self.site_path
        if site is None:
            return
        cost = settle_cost(p.cities)
        if self.backed_off(site):
            return
        if p.can_settle_at(site):
            if p.can(cost):
                p.settle(site)
                for r, v in cost.items():
                    self.reserved[r] = max(0, self.reserved.get(r, 0) - v)
                self.site = None
            return
        # claim along the chain (excluding the site itself)
        for j in path:
            if j == site:
                continue
            if w.owner[j] is None and j not in p.claimed:
                if self.backed_off(j):
                    break
                if p.budget.get("influence", 0) - self.INFLUENCE_RESERVE >= claim_cost(p.tiles):
                    p.claim(j)
                break

    def claim_candidates(self) -> list:
        w, p = self.w, self.p
        seen = set()
        out = []
        for i in list(w.my_tiles) + list(p.claimed):
            for j in w.nb[i]:
                if j in seen or w.owner[j] is not None or j in p.claimed or w.terrain[j] not in C.PASSABLE:
                    continue
                seen.add(j)
                if w.hostile_units_on(w.me, j) or self.backed_off(j):
                    continue
                v = self.tile_value(j)
                # opening more land is worth a little
                v += 0.15 * sum(1 for k in w.nb[j] if w.owner[k] is None and w.terrain[k] in C.PASSABLE)
                out.append((v, j))
        out.sort(key=lambda t: (-round(t[0], 9), self.salt[t[1]]))
        return out

    def claim_step(self, max_claims: int = 4) -> None:
        w, p = self.w, self.p
        n = 0
        for v, j in self.claim_candidates():
            if n >= max_claims:
                break
            cost = claim_cost(p.tiles)
            if p.budget.get("influence", 0) - self.INFLUENCE_RESERVE < cost:
                break
            # claim if the tile repays its influence within the remaining game
            if v * min(40, self.remaining) < cost * self.INFLUENCE_WEIGHT * 3:
                continue
            if p.claim(j):
                n += 1

    # ------------------------------------------------------------------
    # development
    # ------------------------------------------------------------------
    def develop(self) -> None:
        self.city_buildings()
        self.improvements()

    def city_buildings(self) -> None:
        w, p = self.w, self.p
        cap = w.capital if w.capital in w.my_cities else (w.my_cities[0] if w.my_cities else None)
        if cap is None:
            return
        if self.MARKET_HALL and not any(w.cities[c]["buildings"].get("market_hall") for c in w.my_cities):
            if w.turn >= 4:
                p.build_city(cap, "market_hall", self.reserved)
        if self.WAREHOUSE and not any(w.cities[c]["buildings"].get("warehouse") for c in w.my_cities):
            near = any(p.budget.get(r, 0) + self.raw.get(r, 0) * 1.5 >= w.caps.get(r, 300) * 0.85 for r in CAPPED)
            if near:
                p.build_city(cap, "warehouse", self.reserved)

    def improvement_weights(self) -> dict:
        wts = dict(self.wts)
        if self.food_short:
            wts["food"] = wts["food"] * 3
        return wts

    def improvements(self, limit: int = 12) -> None:
        w, p = self.w, self.p
        wts = self.improvement_weights()
        cands = []
        for i in list(w.my_tiles) + list(p.claimed):
            if i in w.cities or i in w.improvement or i in p.improved:
                continue
            b, gain, cost = best_improvement(w, i, wts, self.ALLOW_TEMPLES)
            if b is None:
                continue
            roi = gain / cost if cost else 0
            if b == "temple":
                roi *= self.TEMPLE_BIAS
            if roi * min(self.remaining, 60) < 1.0 or roi < self.MIN_ROI:
                continue
            cands.append((roi, i, b))
        cands.sort(key=lambda t: (-round(t[0], 9), self.salt[t[1]]))
        n = 0
        short = 0.0         # value of good investments we could not afford (for loans, §13)
        skipped = 0
        for roi, i, b in cands:
            if n >= limit:
                break
            cost = C.IMPROVEMENTS[b]["cost"]
            if not p.can(cost, self.reserved) and self.BUY_FOR_IMPROVEMENTS:
                self.buy_missing(cost)
            if p.can(cost, self.reserved) and p.improve(i, b):
                n += 1
            elif skipped < 4 and roi >= 1 / 30.0:
                skipped += 1
                short += value_of(cost, self.wts) / max(0.1, self.wts.get("gold", 1.0))
        self.memory["invest_short"] = (w.turn, int(short))

    def buy_missing(self, cost: dict, max_mult: float = 1.3) -> bool:
        """Buy the stone/wood/food missing for ``cost`` (beyond reserves) if
        the gold budget covers the purchase and the gold part of the cost."""
        w, p = self.w, self.p
        short = {r: cost[r] - (p.budget.get(r, 0) - self.reserve(r)) for r in cost
                 if r in C.MARKET_RESOURCES and cost[r] > p.budget.get(r, 0) - self.reserve(r)}
        if not short:
            return True
        gold = cost.get("gold", 0) + self.reserve("gold")
        for r, q in short.items():
            price = buy_price(w, r, q + p.bought.get(r, 0))
            if price > max_mult * base_price(r):
                return False
            gold += q * price * (1 + w.fee) * 1.1 + 2
        if p.budget.get("gold", 0) < gold:
            return False
        return all(p.buy(r, q, max_price=max_mult * base_price(r)) >= q for r, q in short.items())

    # ------------------------------------------------------------------
    # defence
    # ------------------------------------------------------------------
    def city_threat(self, c: int, reach: int | None = None) -> dict:
        """Hostile units that could reach city ``c`` within ``reach`` turns
        and have no other of our cities closer (an army attacks one city at
        a time, so each city only prepares for the armies nearest to it)."""
        reach = reach or self.DEFENSE_REACH
        key = (c, reach)
        cache = self.__dict__.setdefault("_threat_cache", {})
        if cache.get("turn") != self.w.turn or cache.get("me") is not self.w:
            cache.clear()
            cache["turn"], cache["me"] = self.w.turn, self.w
        if key not in cache:
            cache[key] = self._assigned_threat(c, reach)
        return cache[key]

    def _assigned_threat(self, c: int, reach: int) -> dict:
        w = self.w
        full = threat_to(w, c, reach=reach)
        if not full or len(w.my_cities) <= 1 or not self.ASSIGN_THREATS:
            return full
        near = w.bfs([c], max_dist=2 * reach)
        others = [o for o in w.my_cities if o != c]
        odist = w.bfs(others, max_dist=2 * reach) if others else {}
        out: dict = {}
        for i, per in w.armies.items():
            d = near.get(i)
            if d is None:
                continue
            if odist.get(i, 999) < d:
                continue            # another of our cities is closer to this army
            home = w.cities.get(i)
            for q, u in per.items():
                if q == w.me or not w.hostile(w.me, q):
                    continue
                got = {t: k for t, k in u.items()
                       if d <= reach * (C.UNITS[t]["move"] if t in C.UNITS else 1)}
                if got and home is not None and home["owner"] == q and self.GARRISON_THREAT_SHARE < 1:
                    # units sitting in their own city are mostly its garrison
                    got = {t: int(math.ceil(k * self.GARRISON_THREAT_SHARE)) for t, k in got.items()}
                if got:
                    out[q] = add_units(out.get(q, {}), got)
        return out

    def defenders_at(self, c: int) -> dict:
        """My units that will be at city c this turn (staying + queued recruits)."""
        p = self.p
        stay = p.available(c)
        return add_units(stay, p.recruited.get(c, {}))

    def defended(self, c: int, threat: dict, margin: float, units: dict | None = None) -> bool:
        """Would city ``c`` hold against each hostile player's nearby units
        (× margin)? ``units`` defaults to everything that will be there."""
        w = self.w
        mine = self.defenders_at(c) if units is None else units
        for q, enemy in threat.items():
            scaled = {u: int(math.ceil(k * margin)) for u, k in enemy.items()}
            win, _, _ = simulate_attack(w, q, scaled, c, defenders_override={w.me: mine})
            if win:
                return False
        return True

    def defend(self) -> None:
        """Recruit counter units in threatened cities until the strongest
        nearby hostile army (× margin) would lose an assault."""
        w, p = self.w, self.p
        for c in sorted(w.my_cities, key=lambda c: (0 if w.cities[c].get("capital") else 1, c)):
            threat = self.city_threat(c)
            if threat:
                self.reinforce(c, threat, self.DEFENSE_MARGIN)
            self.lock_garrison(c)

    DEFENSIVE_WALLS = True        # raise walls in a threatened city
    DEFENSE_BUY = True            # buy food/wood on the market to recruit defenders
    DEFENSE_BUY_FRACTION = 1.0    # ...spending at most this share of our gold per turn

    def reinforce(self, c: int, threat: dict, margin: float, max_rounds: int = 12) -> None:
        w, p = self.w, self.p
        if self.defended(c, threat, margin):
            return
        combined: dict = {}
        for u in threat.values():
            combined = add_units(combined, u)
        # walls multiply the whole defence (units and garrison) and are cheap
        if self.DEFENSIVE_WALLS and raw_strength(combined) >= 60:
            level = w.cities[c]["buildings"].get("walls", 0) + p.city_builds.get((c, "walls"), 0)
            if level < C.CITY_BUILDINGS["walls"]["max"]:
                if not p.build_city(c, "walls") and self.DEFENSE_BUY:
                    self.build_with_market(c, "walls", max_mult=2.0)
        t = best_counter(combined, allowed=("infantry", "archer"))
        other = "archer" if t == "infantry" else "infantry"
        bought = False
        for _ in range(max_rounds):
            if self.defended(c, threat, margin):
                return
            k = max(1, total_units(combined) // 4)
            if p.recruit(c, t, k) == 0 and p.recruit(c, other, k) == 0:
                if bought or not self.DEFENSE_BUY:
                    return
                # out of food/wood: buy a batch with gold, then retry
                bought = True
                cost = {r: v * 2 * k for r, v in C.UNITS[t]["cost"].items() if r in C.MARKET_RESOURCES}
                spend = w.res.get("gold", 0) * self.DEFENSE_BUY_FRACTION
                for r, v in cost.items():
                    short = v - p.budget.get(r, 0)
                    if short > 0 and p.budget.get("gold", 0) > 40 and spend > 0:
                        price = max(0.1, buy_price(w, r, short))
                        short = min(short, int(spend / (price * (1 + w.fee) * 1.1)))
                        if short > 0:
                            got = p.buy(r, short, max_price=2.0 * base_price(r))
                            spend -= got * price * (1 + w.fee) * 1.1

    def lock_garrison(self, c: int, threat: dict | None = None, margin: float | None = None,
                      minimum: bool = True) -> None:
        """Keep units at city c: the minimum garrison, plus (when a hostile
        army is near) just enough units to hold with the defence margin.
        ``threat``/``margin`` default to :meth:`city_threat` / DEFENSE_MARGIN;
        units already locked there count."""
        w, p = self.w, self.p
        margin = self.DEFENSE_MARGIN if margin is None else margin
        prev = self.locked.get(c, {})
        avail = {u: k - prev.get(u, 0) for u, k in p.available(c).items()}
        avail = {u: k for u, k in avail.items() if k > 0}
        if not avail:
            return
        is_cap = w.cities[c].get("capital")
        need = (self.MIN_GARRISON if is_cap else self.CITY_GARRISON) if minimum else 0
        keep: dict = {}
        left = need
        for t in ("archer", "infantry", "cavalry", "siege"):
            k = min(left, avail.get(t, 0))
            if k > 0:
                keep[t] = k
                left -= k
        if threat is None:
            threat = self.city_threat(c)
        if threat:
            queued = add_units(p.recruited.get(c, {}), prev)
            combined: dict = {}
            for u in threat.values():
                combined = add_units(combined, u)

            def dval(t):
                v = C.UNITS[t]["strength"] * CB.counter_multiplier(t, combined)
                return v * (C.ARCHER_CITY_DEFENSE if t == "archer" else 1.0)
            order = sorted(("archer", "infantry", "cavalry", "siege"), key=lambda t: -dval(t))
            step = 1
            guard = 0
            while not self.defended(c, threat, margin, add_units(keep, queued)) and guard < 60:
                guard += 1
                added = False
                for t in order:
                    room = avail.get(t, 0) - keep.get(t, 0)
                    if room > 0:
                        keep[t] = keep.get(t, 0) + min(room, step)
                        added = True
                        break
                if not added:
                    break
                step = min(step + 1, 5)
        if keep:
            self.locked[c] = add_units(self.locked.get(c, {}), keep)

    def free_units(self, i: int) -> dict:
        """Units on tile i that are neither moved nor locked."""
        avail = self.p.available(i)
        lock = self.locked.get(i, {})
        out = {u: c - lock.get(u, 0) for u, c in avail.items()}
        return {u: c for u, c in out.items() if c > 0}

    def garrison_moves(self) -> None:
        """Send free units that are outside cities back to the nearest own
        city that is short of defenders (or just the nearest one)."""
        w, p = self.w, self.p
        if not w.my_cities:
            return
        enter = w.can_enter_fn(w.me)
        dist = w.bfs(w.my_cities, enter)
        for i in sorted(w.my_armies):
            if i in w.cities and w.cities[i]["owner"] == w.me:
                continue
            free = self.free_units(i)
            if not free:
                continue
            nxt = w.step_towards(i, dist, enter)
            if nxt is not None:
                self.safe_move(i, nxt, free)

    # ------------------------------------------------------------------
    # military helpers
    # ------------------------------------------------------------------
    def safe_move(self, src: int, dst: int, units: dict, allow_fight: bool = True) -> bool:
        """Move ``units`` one step unless that walks into a losing battle."""
        w, p = self.w, self.p
        if w.hostile_units_on(w.me, dst) or (dst in w.cities and w.hostile(w.me, w.cities[dst]["owner"])):
            if not allow_fight:
                return False
            win, _, _ = simulate_attack(w, w.me, units, dst)
            if not win:
                return False
        return p.move(src, [dst], units)

    def army_power(self, units: dict) -> int:
        return raw_strength(units)

    def offense(self, target: int, min_ratio: float = 1.15, reserve_units: int = 0,
                max_dist: int | None = None) -> bool:
        """Drive all free units toward enemy city ``target`` and assault it
        when the combined force adjacent to it wins the simulated battle with
        ``min_ratio`` to spare. Returns True if an assault was ordered."""
        w, p = self.w, self.p
        me = w.me
        enter = w.can_enter_fn(me)
        dist = w.bfs([target], enter)
        # enemy units that might reinforce the target this turn
        owner = w.cities[target]["owner"] if target in w.cities else w.owner[target]
        extra: dict = {}
        if owner:
            for j in w.nb[target]:
                u = w.armies.get(j, {}).get(owner)
                if u:
                    extra = add_units(extra, u)
        # forces able to hit this turn
        strikers = []
        total: dict = {}
        for i in sorted(w.my_armies):
            d = dist.get(i)
            if d is None or (max_dist is not None and d > max_dist):
                continue
            free = self.free_units(i)
            if not free:
                continue
            if d == 1:
                strikers.append((i, [target], free))
                total = add_units(total, free)
            elif d == 2 and free.get("cavalry"):
                mid = None
                for j in w.nb[i]:
                    if dist.get(j) == 1 and enter(j) and not w.hostile_units_on(me, j):
                        mid = j
                        break
                if mid is not None:
                    cav = {"cavalry": free["cavalry"]}
                    strikers.append((i, [mid, target], cav))
                    total = add_units(total, cav)
        attacked = False
        if total:
            win, surv, ratio = simulate_attack(w, me, total, target,
                                               extra_defenders={owner: extra} if extra and owner else None)
            if win and ratio >= min_ratio:
                for i, path, units in strikers:
                    p.move(i, path, units)
                attacked = True
        # everyone else marches toward the target and gathers next to it
        for i in sorted(w.my_armies):
            d = dist.get(i)
            if d is None or d <= 1 or (max_dist is not None and d > max_dist):
                continue
            free = self.free_units(i)
            if not free:
                continue
            cav = free.get("cavalry", 0)
            others = {u: c for u, c in free.items() if u != "cavalry"}
            nxt = w.step_towards(i, dist, enter)
            if nxt is None:
                continue
            if others:
                if not self.safe_move(i, nxt, free):
                    self.locked[i] = add_units(self.locked.get(i, {}), free)
            elif cav:
                # pure cavalry: 2 steps if the first is clear and we don't overshoot
                nxt2 = w.step_towards(nxt, dist, enter)
                if d > 2 and nxt2 is not None and not w.hostile_units_on(me, nxt) and dist.get(nxt2, 99) >= 1 \
                        and not w.hostile_units_on(me, nxt2):
                    if not p.move(i, [nxt, nxt2], {"cavalry": cav}):
                        self.safe_move(i, nxt, {"cavalry": cav})
                else:
                    self.safe_move(i, nxt, {"cavalry": cav})
        # units already in position wait for the others (do not send them home)
        for i in sorted(w.my_armies):
            d = dist.get(i)
            if d is not None and d <= 2:
                rest = self.free_units(i)
                if rest:
                    self.locked[i] = add_units(self.locked.get(i, {}), rest)
        return attacked

    def build_with_market(self, city: int, building: str, reserve: dict | None = None,
                          max_mult: float = 1.6) -> bool:
        """Build the next level of ``building`` in ``city``, buying the
        missing stone/wood/food on the market this turn (the market resolves
        before actions, and storage caps only apply at the end of the turn,
        so a stage can cost more than the cap). Only buys when the whole
        purchase + build is affordable; returns True if the build was ordered."""
        w, p = self.w, self.p
        cost = p.city_build_cost(city, building)
        if cost is None:
            return False
        keep = dict(reserve or {})
        for r, v in getattr(self, "contract_due", {}).items():   # instalments due this turn
            keep[r] = max(keep.get(r, 0), v)
        short = {r: cost[r] - (p.budget.get(r, 0) - keep.get(r, 0)) for r in cost
                 if r in C.MARKET_RESOURCES and cost[r] > p.budget.get(r, 0) - keep.get(r, 0)}
        gold = cost.get("gold", 0) + keep.get("gold", 0)
        for r, q in short.items():
            price = buy_price(w, r, q + p.bought.get(r, 0))
            if price > max_mult * base_price(r):
                return False
            gold += q * price * (1 + w.fee) * 1.1 + 2
        if p.budget.get("gold", 0) < gold:
            return False
        for r, q in short.items():
            if p.buy(r, q, max_price=max_mult * base_price(r)) < q:
                return False
        return p.build_city(city, building, keep)

    def siege_needed(self, target: int) -> int:
        c = self.w.cities.get(target)
        if not c:
            return 0
        return C.SIEGE_PER_WALL_LEVEL * c["buildings"].get("walls", 0)

    def recruit_army(self, city: int, enemy_units: dict, budget_units: int, siege: int = 0,
                     mix: tuple = ("infantry", "cavalry")) -> int:
        """Recruit up to ``budget_units`` attackers suited against ``enemy_units``."""
        p = self.p
        made = 0
        have_siege = self.w.my_units.get("siege", 0) + sum(q.get("siege", 0) for q in p.recruited.values())
        if siege > have_siege:
            made += p.recruit(city, "siege", min(siege - have_siege, budget_units), self.reserved)
        left = budget_units - made
        if left <= 0:
            return made
        t = best_counter(enemy_units or {"archer": 1, "infantry": 1}, allowed=mix)
        made += p.recruit(city, t, left, self.reserved)
        return made


def hash_pid(pid: str) -> int:
    """Small deterministic hash of a player id."""
    return sum(ord(ch) for ch in str(pid))
