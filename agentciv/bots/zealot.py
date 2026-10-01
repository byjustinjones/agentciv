"""Zealot: a temple rush for the influence victory.

The influence race the LLM players ran: temples on every tile that can
hold one from the first turns, land claimed for more temples, relics
occupied for their 3 influence a turn, and just enough army to keep every
city (the loss of any city resets the influence streak, rules §11).

Strategy
--------
* **Temples first.** Influence is valued high from turn 0, temples get a
  large ROI bias, and the stone they need is bought on the market.
  Claims spend influence but not legacy (only income counts), so it claims
  freely to open temple tiles. Up to ``MAX_CITIES`` cities (+1 influence
  each, and more land).
* **Relics.** Occupies up to ``MAX_RELICS`` relics within ``RELIC_REACH``
  steps of its cities with ``RELIC_GUARD`` infantry each (a guarded relic
  pays 3 influence a turn), and only where no hostile army is near.
* **Minimum army** (``GarrisonMixin``): a capital archer stack growing with
  legacy, ``STREAK_CITY_GARRISON`` defenders in every other city near the
  target, capital walls from 60% of L. Threatened cities recruit counters.
* **Never breaks a treaty** (a break ends its own influence streak);
  accepts every treaty and proposes them to everyone.
* No bank, no wonder, no raids. Trading: sells its surplus, buys peace from
  armies that threaten it.
"""
from __future__ import annotations

from agentciv.engine import constants as C

from .common import add_units, threat_to
from .garrison import GarrisonMixin
from .planner import PlannerBot


class ZealotBot(GarrisonMixin, PlannerBot):
    name = "zealot"

    INFLUENCE_WEIGHT = 6.0
    ALLOW_TEMPLES = True
    TEMPLE_BIAS = 3.0
    BUY_FOR_IMPROVEMENTS = True
    MAX_CITIES = 6
    SETTLE_RADIUS = 7
    RELIC_INTEREST = 8.0
    DEFENSE_REACH = 3
    DEFENSE_MARGIN = 1.2
    MIN_GARRISON = 2
    CITY_GARRISON = 1
    STREAK_CITY_GARRISON = 2
    STREAK_GUARD_SHARE = 0.75
    SELL_FLOOR = 0.55
    TREATY_TURNS = 30
    CAPITAL_ARCHERS = 10          # archers in the capital when legacy is at L
    WALLS_AT = ((0.6, 1), (0.9, 2))   # (legacy share, capital wall level)
    MAX_RELICS = 2
    RELIC_REACH = 6
    RELIC_GUARD = 2

    # trading
    PEACE_SCALE = 1.3

    def pipeline(self):
        return [self.diplomacy, self.food_safety, self.plan_site, self.defend, self.hold_relics,
                self.sell, self.fortify, self.retake_capital, self.expand, self.develop, self.garrison_moves]

    def share(self) -> float:
        w = self.w
        me = w.players.get(w.me) or {}
        return int(me.get("legacy") or 0) / max(1, int(w.thresholds.get("legacy", C.LEGACY_VICTORY)))

    def diplomacy(self) -> None:
        self.accept_all_and_propose(turns=self.TREATY_TURNS)

    def trade_proposals(self) -> list:
        return self.peace_offers(turns=self.TREATY_TURNS) + self.sale_offers(resources=("food", "wood"), share=0.5)

    def keep(self, r: str) -> int:
        # stone for the next temples is kept, not sold
        return {"food": 40, "wood": 45, "stone": 60}.get(r, 0)

    def claim_step(self, max_claims: int = 5) -> None:
        super().claim_step(max_claims)

    # ---- defence --------------------------------------------------------
    def fortify(self) -> None:
        s = min(1.0, self.share())
        archers = self.MIN_GARRISON + int(round((self.CAPITAL_ARCHERS - self.MIN_GARRISON) * s))
        others = self.STREAK_CITY_GARRISON if self.streak_guard() else self.CITY_GARRISON
        walls = 0
        for at, level in self.WALLS_AT:
            if s >= at:
                walls = level
        self.keep_garrisons(archers, others, walls)

    # ---- relics ---------------------------------------------------------
    def hold_relics(self) -> None:
        """Keep the guards on our relics; send a squad of infantry to the
        nearest free relic while we hold fewer than MAX_RELICS."""
        w, p = self.w, self.p
        if not w.my_cities:
            return
        held = []
        for r in w.relics:
            if w.owner[r] == w.me and w.me in w.armies.get(r, {}):
                held.append(r)
                here = self.free_units(r)
                if here:
                    self.locked[r] = add_units(self.locked.get(r, {}), here)
        if len(held) >= self.MAX_RELICS:
            return
        enter = w.can_enter_fn(w.me)
        dist = w.bfs(w.my_cities, enter, max_dist=self.RELIC_REACH)
        cands = []
        for r in w.relics:
            if r in held or r not in dist or not enter(r):
                continue
            if any(q != w.me for q in w.armies.get(r, {})):
                continue            # guarded by someone: not worth a fight
            if threat_to(w, r, reach=2):
                continue
            cands.append((dist[r], self.salt[r], r))
        if not cands:
            return
        _, _, tgt = min(cands)
        to = w.bfs([tgt], enter, max_dist=self.RELIC_REACH + 2)
        need = self.RELIC_GUARD
        # infantry already on the way (outside our cities) first, then from cities
        for i in sorted(w.my_armies, key=lambda i: (to.get(i, 99), i)):
            if need <= 0 or i not in to:
                break
            inf = self.free_units(i).get("infantry", 0)
            if i in w.cities and w.cities[i]["owner"] == w.me:
                # never strip a city below its garrison
                inf = min(inf, max(0, sum(p.available(i).values()) - self.city_minimum(i)))
            k = min(inf, need)
            if k <= 0:
                continue
            if i == tgt:
                self.locked[i] = add_units(self.locked.get(i, {}), {"infantry": k})
            else:
                nxt = w.step_towards(i, to, enter)
                if nxt is None or not self.safe_move(i, nxt, {"infantry": k}, allow_fight=False):
                    continue
            need -= k
        if need > 0:
            city = min(w.my_cities, key=lambda c: (to.get(c, 99), c))
            room = self.food_room()
            if room > 0 and not p.recruited.get(city, {}).get("infantry"):
                p.recruit(city, "infantry", min(need, room), self.reserved)

    def city_minimum(self, c: int) -> int:
        w = self.w
        if w.cities[c].get("capital"):
            return self.MIN_GARRISON
        if self.streak_guard():
            return max(self.CITY_GARRISON, self.STREAK_CITY_GARRISON)
        return self.CITY_GARRISON
