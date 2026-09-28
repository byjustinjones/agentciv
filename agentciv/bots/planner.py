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

from .common import (CAPPED, SafeBot, World, add_units, base_price,
                     best_counter, best_improvement, food_projection,
                     raw_income, raw_strength, season_mods, simulate_attack,
                     threat_to, tile_yield, total_units,
                     treaty_proposals_to_me, value_of)


class PlannerBot(SafeBot):
    """Base class with reusable behaviours. See subclasses for strategies."""

    name = "planner"

    # ---- knobs (override in subclasses) ---------------------------------
    INFLUENCE_WEIGHT = 3.0        # value of 1 influence in "gold" units
    INFLUENCE_RESERVE = 0         # influence never spent on claims
    ALLOW_TEMPLES = True
    TEMPLE_BIAS = 1.0             # multiplier on temple ROI
    MAX_CITIES = 8
    SETTLE_RADIUS = 6             # how far (path) to look for city sites
    RELIC_INTEREST = 0.0          # bonus value per relic for sites/claims
    DEFENSE_REACH = 2             # turns of warning considered for threats
    DEFENSE_MARGIN = 1.0          # defence must beat threat * margin
    MIN_GARRISON = 1              # units kept in the capital at all times
    CITY_GARRISON = 0             # units kept in other cities
    SELL_FLOOR = 0.65             # never sell below this fraction of base price
    SELL_SURPLUS = True           # sell stock above reserves (not only overflow)
    MIN_ROI = 1 / 45.0            # improvements must repay within ~45 turns
    FOOD_BUFFER_TURNS = 14        # food projection horizon
    MARKET_HALL = True
    WAREHOUSE = True

    def decide(self, view: dict) -> list:
        w = World(view)
        p = self.new_plan(w)
        self.w, self.p = w, p
        self.prepare()
        for step in self.pipeline():
            try:
                step()
            except Exception as e:  # keep going with the other behaviours
                self.last_error = f"{getattr(step, '__name__', step)}: {type(e).__name__}: {e}"
        return p.orders

    def pipeline(self) -> list:
        return [self.diplomacy, self.food_safety, self.plan_site, self.defend, self.sell,
                self.expand, self.develop, self.garrison_moves]

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
    def diplomacy(self) -> None:
        """Default: accept every treaty proposal."""
        for pr in treaty_proposals_to_me(self.w):
            self.p.accept_treaty(pr["from"])

    def accept_all_and_propose(self, turns: int = 50, only_neighbors: bool = False) -> None:
        w, p = self.w, self.p
        for pr in treaty_proposals_to_me(w):
            p.accept_treaty(pr["from"])
        for q in w.rivals:
            if q not in w.treaties and (w.turn + hash_pid(q)) % 3 == 0:
                p.propose(q, turns)

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
            if score > best_s:
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
                if w.hostile_units_on(w.me, j):
                    continue
                v = self.tile_value(j)
                # opening more land is worth a little
                v += 0.15 * sum(1 for k in w.nb[j] if w.owner[k] is None and w.terrain[k] in C.PASSABLE)
                out.append((v, j))
        out.sort(key=lambda t: (-t[0], t[1]))
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
        cands.sort(key=lambda t: (-t[0], t[1]))
        n = 0
        for roi, i, b in cands:
            if n >= limit:
                break
            if p.can(C.IMPROVEMENTS[b]["cost"], self.reserved) and p.improve(i, b):
                n += 1

    # ------------------------------------------------------------------
    # defence
    # ------------------------------------------------------------------
    def city_threat(self, c: int, reach: int | None = None) -> dict:
        return threat_to(self.w, c, reach=reach or self.DEFENSE_REACH)

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

    def reinforce(self, c: int, threat: dict, margin: float, max_rounds: int = 12) -> None:
        w, p = self.w, self.p
        allowed = ("infantry", "archer", "cavalry")
        for _ in range(max_rounds):
            if self.defended(c, threat, margin):
                return
            combined: dict = {}
            for u in threat.values():
                combined = add_units(combined, u)
            t = best_counter(combined, allowed=("infantry", "archer"))
            k = max(1, total_units(combined) // 4)
            if p.recruit(c, t, k) == 0:
                # try the other cheap type
                other = "archer" if t == "infantry" else "infantry"
                if p.recruit(c, other, k) == 0:
                    return

    def lock_garrison(self, c: int) -> None:
        """Keep units at city c: the minimum garrison, plus (when a hostile
        army is near) just enough units to hold with the defence margin."""
        w, p = self.w, self.p
        avail = p.available(c)
        if not avail:
            return
        is_cap = w.cities[c].get("capital")
        need = self.MIN_GARRISON if is_cap else self.CITY_GARRISON
        keep: dict = {}
        left = need
        for t in ("archer", "infantry", "cavalry", "siege"):
            k = min(left, avail.get(t, 0))
            if k > 0:
                keep[t] = k
                left -= k
        threat = self.city_threat(c)
        if threat:
            queued = p.recruited.get(c, {})
            combined: dict = {}
            for u in threat.values():
                combined = add_units(combined, u)

            def dval(t):
                v = C.UNITS[t]["strength"] * CB.counter_multiplier(t, combined)
                return v * (C.ARCHER_CITY_DEFENSE if t == "archer" else 1.0)
            order = sorted(("archer", "infantry", "cavalry", "siege"), key=lambda t: -dval(t))
            step = 1
            guard = 0
            while not self.defended(c, threat, self.DEFENSE_MARGIN, add_units(keep, queued)) and guard < 60:
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
