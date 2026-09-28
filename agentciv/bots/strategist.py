"""Strategist: the strongest built-in bot.

It plays a strong economy like the economist, but reads the whole board
every turn and adapts:

* **Victory ETA model.** For itself and every rival it estimates how many
  turns each victory condition is away (economic and influence from the
  observed per-turn growth of gold/influence, wonder from the stage pace,
  relics from the streak, conquest from capitals held). Its own path is the
  one with the lowest ETA; the rival with the lowest ETA is the *leader*.
* **Blocking.** When the leader would win before it, it acts against the
  leader's path: captures the leader's relic tiles (which resets the relic
  streak), assaults a wonder city (destroys the wonder) or the leader's
  capital (plunders half of its gold), breaking a treaty if necessary.
* **Relic control.** Relics near the map centre are the fastest path, so it
  prices relic tiles highly, settles next to them, claims them, and keeps a
  mobile army near them that guards its own relics and takes enemy ones.
* **Threat assessment.** For each city it collects every hostile unit that
  could arrive within a few turns and recruits the best counter until the
  engine-exact battle simulation says the city holds with a safety margin.
* **Economy.** Improvements by ROI with market-price-based values, early
  market hall, warehouse before hitting caps, season-aware food planning,
  and market sales that are split so the batch price stays near the spot
  price (selling before overflow, never dumping below a floor).
* **Diplomacy.** Proposes treaties to distant players (so only neighbours
  need watching), accepts proposals from players who are neither its target
  nor close to winning, and never signs with the leader.
"""
from __future__ import annotations

import math

from agentciv.engine import combat as CB
from agentciv.engine import constants as C
from agentciv.engine.rules import building_cost

from .common import (CAPPED, add_units, base_price, best_counter, season_mods,
                     sell_price, simulate_attack, threat_to,
                     treaty_proposals_to_me, wonder_city)
from .planner import PlannerBot

INF = float("inf")


class StrategistBot(PlannerBot):
    name = "strategist"

    INFLUENCE_WEIGHT = 3.0
    ALLOW_TEMPLES = True
    TEMPLE_BIAS = 0.9
    MAX_CITIES = 7
    SETTLE_RADIUS = 8
    RELIC_INTEREST = 14.0
    DEFENSE_REACH = 3
    DEFENSE_MARGIN = 1.25
    MIN_GARRISON = 2
    CITY_GARRISON = 1
    SELL_FLOOR = 0.7
    HISTORY = 6
    COMMIT_HORIZON = 30           # only divert resources to a victory path this close
    USE_PREY = True               # conquer weakly defended rival capitals
    USE_BLOCK = True              # attack the rival about to win
    BLOCK_FEASIBILITY = True      # ...but only if we can get there in time
    EARLY_TREATY_TURNS = 6        # accept any harmless treaty this early
    CONQUEST_SLACK = 1.3          # conquest ETAs are rough: block even if a bit late
    RIVAL_POTENTIAL = 0.0         # share of a rival's production assumed sellable
    #                               for gold (0 = observed growth only; higher values
    #                               raised false alarms in tournaments)
    TRAVEL_FACTOR = 1.0           # approach speed assumed for blocking plans (tiles/turn)

    def pipeline(self):
        return [self.observe, self.diplomacy, self.food_safety, self.plan_site, self.defend,
                self.plan_goal_spending, self.relic_ops, self.block, self.sell, self.goal_spending,
                self.field_army, self.expand, self.develop, self.garrison_moves]

    # ------------------------------------------------------------------
    # observation & ETA model
    # ------------------------------------------------------------------
    def observe(self) -> None:
        w = self.w
        hist = self.memory.setdefault("hist", {})
        for q, pl in w.players.items():
            h = hist.setdefault(q, [])
            if not h or h[-1][0] != w.turn:
                r = pl.get("resources", {})
                h.append((w.turn, r.get("gold", 0), r.get("influence", 0), pl.get("wonder_stage", 0)))
                if len(h) > self.HISTORY + 1:
                    del h[0]
        self.etas = {q: self.eta(q) for q in w.alive}
        mine = dict(self.etas.get(w.me, {}))
        # hysteresis: stick to the current path unless another is clearly faster
        cur = self.memory.get("path")
        best = min(mine, key=lambda k: (mine[k], k)) if mine else "score"
        if cur in mine and mine[cur] < INF and mine[best] > 0.75 * mine[cur] - 2:
            best = cur
        self.memory["path"] = best
        self.my_path = best
        self.my_eta = mine.get(best, INF)
        leader, lpath, leta = None, None, INF
        for q in w.rivals:
            for k, v in sorted(self.etas.get(q, {}).items()):
                if v < leta:
                    leader, lpath, leta = q, k, v
        self.leader, self.leader_path, self.leader_eta = leader, lpath, leta
        left = w.max_turns - w.turn
        # the leader is a danger if it would win before us (or before the end)
        self.danger = leader is not None and leta < left and leta <= self.my_eta * 1.15 + 2

    def rate(self, q: str, k: int) -> float:
        h = self.memory.get("hist", {}).get(q, [])
        if len(h) < 2:
            return 0.0
        dt = h[-1][0] - h[0][0]
        return (h[-1][k] - h[0][k]) / dt if dt > 0 else 0.0

    def price(self, r: str) -> float:
        pool = self.w.pools.get(r)
        return pool[1] / pool[0] if pool and pool[0] > 0 else base_price(r)

    def production_value(self, q: str) -> float:
        """Gold-equivalent value of a player's public per-turn income."""
        inc = self.w.players[q].get("income", {}) or {}
        v = inc.get("gold", 0)
        for r in CAPPED:
            amt = inc.get(r, 0) - (self.w.players[q].get("upkeep", 0) if r == "food" else 0)
            v += max(0, amt) * self.price(r)
        return v

    def wonder_cost_value(self, stage: int, stock: dict | None = None) -> float:
        """Gold-equivalent value of the remaining wonder stages (minus stock)."""
        need = {}
        for k in range(stage + 1, C.WONDER_VICTORY_STAGE + 1):
            for r, v in building_cost("wonder", k).items():
                need[r] = need.get(r, 0) + v
        if stock:
            need = {r: max(0, v - stock.get(r, 0)) for r, v in need.items()}
        return sum(v * (1.0 if r == "gold" else self.price(r)) for r, v in need.items())

    def eta(self, q: str) -> dict:
        """Estimated turns until ``q`` meets each victory condition."""
        w = self.w
        pl = w.players[q]
        res = pl.get("resources", {})
        inc = pl.get("income", {}) or {}
        th = w.thresholds
        me = q == w.me
        out = {}
        # economic
        need = th.get("economic_gold", C.ECONOMIC_VICTORY_GOLD) - res.get("gold", 0)
        r = max(self.rate(q, 1), inc.get("gold", 0))
        if me:
            r = max(r, self.potential_gold_rate())
        else:
            # rivals could start selling everything at any time
            r = max(r, self.RIVAL_POTENTIAL * self.production_value(q))
        out["economic"] = 0 if need <= 0 else (need / r if r > 0.5 else INF)
        # influence
        need = th.get("influence", C.INFLUENCE_VICTORY) - res.get("influence", 0)
        r = max(self.rate(q, 2), inc.get("influence", 0) * (1.0 if me else 0.8))
        out["influence"] = 0 if need <= 0 else (need / r if r > 0.2 else INF)
        # wonder
        stage = pl.get("wonder_stage", 0)
        left = C.WONDER_VICTORY_STAGE - stage
        pv = self.potential_gold_rate() if me else self.production_value(q)
        value_eta = max(left, self.wonder_cost_value(stage, res) / pv) if pv > 1 else INF
        if me:
            out["wonder"] = value_eta + (3 if stage == 0 else 0)
        elif stage > 0:
            h = self.memory.get("hist", {}).get(q, [])
            pace_eta = INF
            if len(h) >= 2 and h[-1][3] > h[0][3]:
                pace = max(1.0, (h[-1][0] - h[0][0]) / (h[-1][3] - h[0][3]))
                pace_eta = left * pace
            out["wonder"] = min(pace_eta, value_eta)
        else:
            out["wonder"] = INF
        # relics
        streak = pl.get("relic_streak", 0)
        need_r = th.get("relics_needed", 99)
        held = pl.get("relics_held", 0)
        if held >= need_r:
            out["relics"] = C.RELIC_VICTORY_TURNS - streak
        elif me:
            out["relics"] = self.relic_acquire_turns(need_r - held) + C.RELIC_VICTORY_TURNS
        elif held == need_r - 1:
            out["relics"] = C.RELIC_VICTORY_TURNS + 4
        else:
            out["relics"] = INF
        # conquest
        caps = pl.get("capitals_held", 0)
        need_c = th.get("conquest_capitals", 99)
        out["conquest"] = 8 * (need_c - caps) + 2 if caps >= max(2, need_c - 1) else INF
        if caps >= need_c:
            out["conquest"] = 0
        return out

    def relic_acquire_turns(self, missing: int) -> float:
        """Rough turns needed to get ``missing`` more relics: walking/claiming
        distance from our land, plus a fight for relics others hold."""
        w = self.w
        if missing <= 0:
            return 0.0
        if not w.my_tiles:
            return INF
        dist = w.bfs(w.my_tiles, max_dist=20)
        costs = []
        for r in w.relics:
            o = w.owner[r]
            if o == w.me or r not in dist:
                continue
            c = dist[r] * 1.2
            if o is not None:
                c += 8 + (6 if w.at_peace(w.me, o) else 0)
            costs.append(c)
        if len(costs) < missing:
            return INF
        costs.sort()
        return costs[missing - 1]

    def potential_gold_rate(self) -> float:
        """Gold per turn if all surplus production were sold at spot prices."""
        w = self.w
        g = self.raw.get("gold", 0)
        for r in CAPPED:
            amt = self.raw.get(r, 0) - (w.upkeep if r == "food" else 0)
            if amt > 0:
                pool = w.pools.get(r)
                price = pool[1] / pool[0] if pool else base_price(r)
                g += amt * price * (1 - w.fee) * 0.8
        return g

    # ------------------------------------------------------------------
    # diplomacy
    # ------------------------------------------------------------------
    def diplomacy(self) -> None:
        """Accept treaties from harmless players, propose them to distant or
        militarily strong ones; never to the leader or our target."""
        w, p = self.w, self.p
        cap = self.home()
        dist = w.bfs([cap]) if cap is not None else {}
        need_r = w.thresholds.get("relics_needed", 99)

        def far(q):
            c = w.capital_of(q) or next(iter(w.cities_of(q)), None)
            return c is None or dist.get(c, 99) >= 14

        def risky(q):
            pl = w.players.get(q, {})
            if q == self.memory.get("block_owner") or (self.danger and q == self.leader):
                return True
            if min(self.etas.get(q, {}).values(), default=INF) < 30:
                return True
            return pl.get("wonder_stage", 0) > 0 or pl.get("relics_held", 0) >= need_r - 1

        mine = max(1, w.players[w.me].get("military_power", 0))

        def useful(q):
            # a treaty protects us from strong armies and costs little with
            # far-away players; others we keep free to attack if needed
            return far(q) or w.players[q].get("military_power", 0) > 1.3 * mine or w.turn < self.EARLY_TREATY_TURNS

        for pr in treaty_proposals_to_me(w):
            q = pr["from"]
            if not risky(q) and useful(q) and q != self.memory.get("prey_owner"):
                p.accept_treaty(q)
        if w.turn % 5 == 1:
            for q in w.rivals:
                if q in w.treaties or risky(q):
                    continue
                if far(q) or w.players[q].get("military_power", 0) > 1.5 * mine:
                    p.propose(q, 20)

    def home(self):
        w = self.w
        if w.capital in w.my_cities:
            return w.capital
        return w.my_cities[0] if w.my_cities else None

    # ------------------------------------------------------------------
    # goal-specific spending
    # ------------------------------------------------------------------
    def committed(self) -> bool:
        w = self.w
        return self.my_eta <= self.COMMIT_HORIZON or self.my_eta >= w.max_turns - w.turn - 5 and w.turn > 40

    def plan_goal_spending(self) -> None:
        w = self.w
        self.goal = self.my_path if self.committed() else "grow"
        if self.goal in ("economic", "wonder", "influence") and self.my_eta < 20:
            # investments only count until we expect to win
            self.remaining = max(1, min(self.remaining, int(self.my_eta) + 8))
        # wonder: reserve the next stage when it is our path
        wc = wonder_city(w)
        if self.goal == "wonder" or (wc is not None and w.cities[wc].get("wonder_stage", 0) >= 3):
            home = wc or self.home()
            if home is not None:
                st = w.cities[home].get("wonder_stage", 0)
                if st < C.WONDER_VICTORY_STAGE:
                    self.add_reserve(building_cost("wonder", st + 1))
        # per-turn knob adjustments start from the class defaults
        self.SELL_FLOOR = type(self).SELL_FLOOR
        self.MIN_ROI = type(self).MIN_ROI
        if self.goal == "wonder":
            self.SELL_FLOOR = 0.5
        if self.goal == "economic" and self.my_eta < 25:
            # stop investing gold: keep almost all of it
            self.reserved["gold"] = max(self.reserved.get("gold", 0), int(w.res.get("gold", 0) * 0.85))
            self.MIN_ROI = 1 / 12.0
        if self.goal == "influence":
            self.TEMPLE_BIAS = 2.0
            if self.my_eta < 30:
                self.INFLUENCE_RESERVE = int(w.res.get("influence", 0) * 0.8)
        else:
            self.TEMPLE_BIAS = type(self).TEMPLE_BIAS
            self.INFLUENCE_RESERVE = 0

    def goal_spending(self) -> None:
        w, p = self.w, self.p
        wc = wonder_city(w)
        if self.goal == "wonder" or (wc is not None and w.cities[wc].get("wonder_stage", 0) >= 3):
            home = wc or self.home()
            if home is None:
                return
            st = w.cities[home].get("wonder_stage", 0)
            if st >= C.WONDER_VICTORY_STAGE:
                return
            cost = building_cost("wonder", st + 1)
            for r in ("stone", "wood"):
                short = cost.get(r, 0) - p.budget.get(r, 0)
                if short > 0 and p.budget.get("gold", 0) > cost.get("gold", 0) + 30:
                    p.buy(r, short, max_price=1.5 * base_price(r))
            if p.can(cost):
                # the winning path outranks every other reservation
                if p.build_city(home, "wonder"):
                    for r, v in cost.items():
                        self.reserved[r] = max(0, self.reserved.get(r, 0) - v)

    # ------------------------------------------------------------------
    # market
    # ------------------------------------------------------------------
    def sell(self) -> None:
        """Sell before overflow and sell surplus, splitting volume so the
        batch price stays within ~8% of spot (the rest is sold next turn)."""
        w, p = self.w, self.p
        mods = season_mods(w.turn)
        for r in CAPPED:
            if r == "food" and self.food_short:
                continue
            stock = p.budget.get(r, 0)
            inc = math.floor(self.raw.get(r, 0) * mods.get(r, 1.0)) - (w.upkeep if r == "food" else 0)
            cap = w.caps.get(r, C.STORAGE_BASE)
            overflow = stock + inc - cap
            surplus = stock - self.reserve(r) - self.keep(r)
            pool = w.pools.get(r)
            spot = pool[1] / pool[0] if pool else base_price(r)
            floor = self.SELL_FLOOR * base_price(r)
            sold = 0
            qty = surplus
            if qty >= 5 and spot >= floor:
                # limit price impact on voluntary sales; the rest waits a turn
                while qty > 5 and sell_price(w, r, qty) < spot * 0.92:
                    qty = int(qty * 0.75)
                sold = p.sell(r, qty, max(floor, spot * 0.85))
            if overflow - sold >= 3:
                p.sell(r, overflow - sold, 0.2 * base_price(r))

    def keep(self, r: str) -> int:
        return {"food": 30, "wood": 45, "stone": 30}.get(r, 0)

    # ------------------------------------------------------------------
    # expansion tweaks
    # ------------------------------------------------------------------
    def site_danger_weight(self) -> float:
        return 0.6

    def settle_step(self) -> None:
        # a new city pays off slowly: skip it when we are about to win
        if getattr(self, "goal", "grow") in ("economic", "wonder", "influence") and self.my_eta < 15:
            return
        super().settle_step()

    def plan_site(self) -> None:
        if self.my_path in ("economic", "wonder", "influence") and self.my_eta < 15:
            self.site, self.site_path = None, []
            return
        super().plan_site()

    # ------------------------------------------------------------------
    # relics
    # ------------------------------------------------------------------
    def relic_ops(self) -> None:
        """Claim reachable relics; keep a guard on held relics when a hostile
        army is near."""
        w, p = self.w, self.p
        for r in w.relics:
            if w.owner[r] is None and w.adjacent_to(w.me, r, p.claimed) and not w.hostile_units_on(w.me, r):
                p.claim(r)
        self.relic_guard_targets = []
        for r in w.relics:
            if w.owner[r] != w.me:
                continue
            th = threat_to(w, r, reach=2)
            if th:
                self.relic_guard_targets.append((r, th))

    # ------------------------------------------------------------------
    # blocking & field army
    # ------------------------------------------------------------------
    def block(self) -> None:
        """Pick a military objective against a rival that threatens to win
        before us and record it for :meth:`field_army`. The objective is kept
        across turns while that rival stays dangerous (no flip-flopping
        between rivals)."""
        w = self.w
        self.objective = None
        self.objective_ratio = 1.15
        rival, path = None, None
        prev = self.memory.get("block")
        if prev:
            q, pth = prev
            eta = self.etas.get(q, {}).get(pth, INF)
            if q in w.rivals and eta < INF and eta <= 1.6 * self.leader_eta + 3 \
                    and eta <= self.my_eta * 1.3 + 3:
                rival, path = q, pth
        if not self.USE_BLOCK:
            rival = None
        elif rival is None and self.danger and self.leader is not None:
            rival, path = self.leader, self.leader_path
        if rival is None:
            self.memory["block"] = None
            self.memory["block_owner"] = None
            return
        tgt = self.block_target(rival, path)
        if tgt is not None and self.BLOCK_FEASIBILITY and not self.block_feasible(tgt, rival, path):
            tgt = None
        if tgt is None:
            self.memory["block"] = None
            self.memory["block_owner"] = None
            return
        self.memory["block"] = (rival, path)
        self.memory["block_owner"] = rival
        self.objective = tgt
        self.reserve_for_force(tgt, plain=w.at_peace(w.me, rival))
        if w.at_peace(w.me, rival):
            # keep enough influence to break the treaty when ready
            self.INFLUENCE_RESERVE = max(self.INFLUENCE_RESERVE, C.TREATY_BREAK_COST + 2)

    def block_feasible(self, tgt: int, rival: str, path: str) -> bool:
        """Can we assemble and deliver the strike force before ``rival``
        wins? (Otherwise racing is the better use of our resources.)"""
        w = self.w
        eta = self.etas.get(rival, {}).get(path, INF)
        plain = w.at_peace(w.me, rival)
        cost = self.force_cost(self.missing_force(tgt, plain))
        prod = max(1.0, self.potential_gold_rate())
        stock = sum(w.res.get(r, 0) * (1.0 if r == "gold" else self.price(r)) for r in C.TRADABLE)
        t_build = max(0.0, cost - 0.5 * stock) / prod
        srcs = list(w.my_armies) or list(w.my_cities)
        dist = w.bfs([tgt])
        travel = min((dist.get(i, 99) for i in srcs), default=99) * self.TRAVEL_FACTOR
        # conquest ETAs are rough guesses: allow more slack
        slack = self.CONQUEST_SLACK if path == "conquest" else 1.1
        return t_build + travel + 1 <= eta * slack + 2

    def block_target(self, q: str, path: str):
        """The tile to hit to stop ``q`` winning by ``path``."""
        w = self.w
        if path == "relics":
            return self.closest_to_army([r for r in w.relics if w.owner[r] == q])
        if path == "wonder":
            return self.closest_to_army([c for c in w.cities_of(q) if w.cities[c].get("wonder_stage", 0) > 0])
        cands = [w.capital_of(q)]
        if path == "influence":
            cands += [r for r in w.relics if w.owner[r] == q] + list(w.cities_of(q))
        elif path == "conquest":
            # any capital it holds will do: pick the cheapest one to take
            caps = [c for c in w.cities_of(q) if w.cities[c].get("capital")]
            return self.cheapest_target(caps, q)
        return self.closest_to_army(cands)

    def cheapest_target(self, cands: list, owner: str):
        """Candidate minimising (turns to build the missing force + travel)."""
        w = self.w
        cands = [c for c in cands if c is not None]
        if not cands:
            return None
        srcs = list(w.my_armies) or list(w.my_cities)
        if not srcs:
            return None
        dist = w.bfs(srcs)
        prod = max(1.0, self.potential_gold_rate())
        plain = w.at_peace(w.me, owner)
        best, best_t = None, INF
        for c in cands:
            d = dist.get(c)
            if d is None:
                continue
            t = d + self.force_cost(self.missing_force(c, plain)) / prod
            if t < best_t:
                best, best_t = c, t
        return best

    def closest_to_army(self, cands: list):
        """Candidate tile closest (plain walking distance) to our forces."""
        w = self.w
        cands = [c for c in cands if c is not None]
        if not cands:
            return None
        srcs = list(w.my_armies) or list(w.my_cities)
        if not srcs:
            return None
        dist = w.bfs(srcs)
        reach = [(dist.get(c, 999), c) for c in cands]
        reach.sort()
        return reach[0][1] if reach[0][0] < 999 else None

    def field_army(self) -> None:
        """Recruit and move the mobile army: guard relics, pursue the block
        objective (staging at the border and breaking the treaty first if the
        target is a treaty partner), or take a weakly defended rival
        city/relic nearby."""
        w, p = self.w, self.p
        if self.home() is None:
            return
        for r, th in getattr(self, "relic_guard_targets", []):
            self.guard_tile(r, th)
        tgt = self.objective
        if tgt is not None:
            owner = w.cities[tgt]["owner"] if tgt in w.cities else w.owner[tgt]
            if owner is not None and w.at_peace(w.me, owner):
                self.ensure_army_for(tgt, plain=True)
                self.stage_against(tgt, owner)
                return
            self.ensure_army_for(tgt)
            self.offense(tgt, min_ratio=self.objective_ratio)
            return
        tgt = self.opportunity()
        if tgt is not None:
            self.offense(tgt, min_ratio=1.3, max_dist=6)
            return
        prey = self.prey() if self.USE_PREY else None
        if prey is not None:
            self.ensure_army_for(prey)
            self.offense(prey, min_ratio=1.25)
            return
        self.position_army()

    def force_cost(self, force: dict) -> float:
        tot = 0.0
        for t, k in force.items():
            for r, v in C.UNITS[t]["cost"].items():
                tot += k * v * (1.0 if r == "gold" else self.price(r))
        return tot

    def prey(self):
        """A weakly defended rival capital worth conquering: plunder, 25
        score, conquest progress and one competitor less. Chosen when the
        estimated strike force costs less than what the capture is worth."""
        w = self.w
        if w.turn < 8 or not w.my_cities:
            return None
        memo = self.memory.get("prey")
        if memo is not None:
            c = w.cities.get(memo)
            if c is not None and c["owner"] != w.me and not w.at_peace(w.me, c["owner"]) \
                    and self.memory.get("prey_until", 0) >= w.turn:
                return memo
        if w.turn % 3:
            return None
        dist = w.bfs(w.my_tiles, w.can_enter_fn(w.me), max_dist=12)
        caps_needed = w.thresholds.get("conquest_capitals", 99) - w.players[w.me].get("capitals_held", 0)
        best, best_v = None, 0.0
        for c, cc in w.cities.items():
            o = cc["owner"]
            if o == w.me or w.at_peace(w.me, o) or c not in dist:
                continue
            if not cc.get("capital"):
                continue
            force = self.strike_force(c)
            if sum(force.values()) > 30:
                continue
            res = w.players[o].get("resources", {})
            plunder = 0.0
            if cc.get("original_owner") == o:
                plunder = sum(res.get(r, 0) * C.PLUNDER_FRACTION * (1.0 if r == "gold" else self.price(r))
                              for r in C.TRADABLE)
            value = 25 * 8 + plunder + (400 if caps_needed <= 1 else 0)
            cost = self.force_cost(force) * (1 + dist[c] / 10.0)
            v = value / max(1.0, cost)
            if v > 1.0 and v > best_v:
                best, best_v = c, v
        if best is not None:
            self.memory["prey"] = best
            self.memory["prey_owner"] = w.cities[best]["owner"]
            self.memory["prey_until"] = w.turn + 25
        return best

    def stage_against(self, tgt: int, owner: str) -> None:
        """March toward a treaty partner's tile as far as the treaty allows;
        break the treaty once the gathered force can win the assault."""
        w, p = self.w, self.p
        plain = w.bfs([tgt])
        enter = w.can_enter_fn(w.me)
        near: dict = {}
        for i in sorted(w.my_armies):
            free = self.free_units(i)
            d = plain.get(i)
            if not free or d is None:
                continue
            if d <= 7:
                near = add_units(near, free)
            nxt = w.step_towards(i, plain, enter)
            moved = nxt is not None and plain.get(nxt, 99) < d and self.safe_move(i, nxt, free, allow_fight=False)
            if not moved and d <= 7:
                # staged at the border: hold position
                self.locked[i] = add_units(self.locked.get(i, {}), free)
        if not near or w.res.get("influence", 0) < C.TREATY_BREAK_COST:
            return
        win, _, ratio = simulate_attack(w, w.me, near, tgt, assume_war=True)
        if win and ratio >= 1.4 and self.memory.get("broke") != w.turn:
            p.orders.append({"type": "break_treaty", "with": owner})
            self.memory["broke"] = w.turn

    def guard_tile(self, tile: int, threat: dict) -> None:
        """Bring free units next to/onto a threatened relic tile."""
        w, p = self.w, self.p
        mine_here = p.available(tile)
        for q, units in threat.items():
            win, _, _ = simulate_attack(w, q, units, tile, defenders_override={w.me: mine_here})
            if not win:
                continue
            enter = w.can_enter_fn(w.me)
            dist = w.bfs([tile], enter, max_dist=4)
            for i in sorted(w.my_armies):
                if i == tile or dist.get(i) is None:
                    continue
                free = self.free_units(i)
                if not free:
                    continue
                nxt = tile if dist[i] == 1 else w.step_towards(i, dist, enter)
                if nxt is not None:
                    self.safe_move(i, nxt, free)
            # keep the ones already there
            self.locked[tile] = add_units(self.locked.get(tile, {}), mine_here)
            break

    def opportunity(self):
        """A rival relic tile or city we can take cheaply with the units we
        already have nearby (never a treaty partner)."""
        w = self.w
        if not w.my_armies:
            return None
        best, best_key = None, None
        enter = w.can_enter_fn(w.me)
        srcs = [i for i in w.my_armies if self.free_units(i)]
        if not srcs:
            return None
        dist = w.bfs(srcs, enter, max_dist=6)
        free_total = {}
        for i in srcs:
            free_total = add_units(free_total, self.free_units(i))
        need_r = w.thresholds.get("relics_needed", 99)
        for t in list(w.relics) + list(w.cities):
            o = w.owner[t]
            if o is None or o == w.me or w.at_peace(w.me, o):
                continue
            d = dist.get(t)
            if d is None:
                continue
            win, surv, ratio = simulate_attack(w, w.me, free_total, t)
            if not win or ratio < 1.4:
                continue
            val = 0.0
            if t in w.relic_set:
                val += 10 + 10 * (w.players[o].get("relics_held", 0) >= need_r - 1)
            if t in w.cities:
                c = w.cities[t]
                val += 25 + (40 if c.get("capital") else 0) + 20 * c.get("wonder_stage", 0)
            key = (-(val / (1 + d)), t)
            if best_key is None or key < best_key:
                best, best_key = t, key
        return best

    def strike_force(self, tgt: int) -> dict:
        """Smallest force (siege to cancel the walls + the best counter to
        the defenders) that wins the simulated assault on ``tgt`` with a 1.35
        power ratio, counting defenders that could step in from next door."""
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
        force = {}
        siege = self.siege_needed(tgt)
        if siege:
            force["siege"] = siege
        t = best_counter(enemy or {"infantry": 1}, allowed=("infantry", "cavalry", "archer"))
        k = 1
        while k <= 80:
            force[t] = k
            win, _, ratio = simulate_attack(w, w.me, force, tgt, defenders_override=defenders, assume_war=True)
            if win and ratio >= 1.35:
                return force
            k += 1 if k < 10 else 3
        return force

    def army_near(self, tgt: int, plain: bool = False, radius: int = 10) -> dict:
        w = self.w
        dist = w.bfs([tgt]) if plain else w.bfs([tgt], w.can_enter_fn(w.me))
        have: dict = {}
        for i in w.my_armies:
            if dist.get(i, 999) <= radius:
                have = add_units(have, self.free_units(i))
        return have

    def missing_force(self, tgt: int, plain: bool = False) -> dict:
        need = self.strike_force(tgt)
        have = self.army_near(tgt, plain)
        return {t: k - have.get(t, 0) for t, k in need.items() if k > have.get(t, 0)}

    def reserve_for_force(self, tgt: int, plain: bool = False) -> None:
        """Keep the resources the missing strike force will need (so they are
        not sold or spent on buildings first)."""
        miss = self.missing_force(tgt, plain)
        cost: dict = {}
        for t, k in miss.items():
            for r, v in C.UNITS[t]["cost"].items():
                cost[r] = cost.get(r, 0) + v * min(k, 15)
        w = self.w
        self.add_reserve({r: min(v, w.res.get(r, 0)) for r, v in cost.items()})

    def pick_unit(self, enemy: dict, allowed=("infantry", "cavalry", "archer")) -> str:
        """Attacker type giving the most power for what we can afford now."""
        p = self.p
        best, best_v = allowed[0], -1.0
        for t in allowed:
            cost = C.UNITS[t]["cost"]
            n = min(int(p.budget.get(r, 0) // v) for r, v in cost.items() if v)
            mult = CB.counter_multiplier(t, enemy) if enemy else 1.0
            v = n * C.UNITS[t]["strength"] * mult + 0.01 * C.UNITS[t]["strength"] * mult
            if v > best_v:
                best, best_v = t, v
        return best

    def ensure_army_for(self, tgt: int, plain: bool = False) -> None:
        """Recruit, in the city closest to the objective and within food
        limits, until the gathered force wins the simulated assault with a
        1.35 ratio (siege first against walls). When a rival is about to
        win this outranks our own goal reservations."""
        w, p = self.w, self.p
        if not w.my_cities:
            return
        dist = w.bfs([tgt]) if plain else w.bfs([tgt], w.can_enter_fn(w.me))
        city = min(w.my_cities, key=lambda c: (dist.get(c, 999), c))
        have = add_units(self.army_near(tgt, plain), p.recruited.get(city, {}))
        room = int(self.raw.get("food", 0) * 0.9 + p.budget.get("food", 0) / 12 - w.upkeep)
        if room <= 0:
            return
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
        reserve = {"food": 20} if self.danger else {r: v for r, v in self.reserved.items() if r == "food"}
        siege_need = self.siege_needed(tgt) - have.get("siege", 0)
        if siege_need > 0:
            k = p.recruit(city, "siege", min(siege_need, room), reserve)
            have = add_units(have, {"siege": k})
            room -= 2 * k
        for _ in range(12):
            if room <= 0:
                break
            win, _, ratio = simulate_attack(w, w.me, have, tgt, defenders_override=defenders,
                                            assume_war=True) if have else (False, {}, 0.0)
            if win and ratio >= 1.35:
                break
            t = self.pick_unit(enemy)
            k = p.recruit(city, t, min(3, room), reserve)
            if k == 0:
                break
            have = add_units(have, {t: k})
            room -= k * C.UNITS[t]["upkeep"]

    def position_army(self) -> None:
        """Idle free units drift toward the relic nearest to home (or stay home)."""
        w, p = self.w, self.p
        mine = [r for r in w.relics if w.owner[r] == w.me]
        if not mine:
            return
        enter = w.can_enter_fn(w.me)
        dist = w.bfs(mine, enter)
        for i in sorted(w.my_armies):
            free = self.free_units(i)
            if free and dist.get(i) == 0:
                self.locked[i] = add_units(self.locked.get(i, {}), free)
                continue
            if not free or dist.get(i) is None:
                continue
            if dist[i] > 6:
                continue
            nxt = w.step_towards(i, dist, enter)
            if nxt is not None:
                self.safe_move(i, nxt, free, allow_fight=False)
