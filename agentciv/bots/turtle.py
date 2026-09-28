"""Turtle: walls, archers, temples and a wonder.

Strategy
--------
* Proposes long (50-turn) treaties to everyone and accepts every proposal,
  so most rivals cannot even walk onto its land.
* Fortifies: walls in the capital early (up to level 3 as the game goes on),
  archers (×1.5 in their own city, strong against infantry) in every city,
  more when an army approaches.
* Builds temples on a large share of its tiles (influence) and puts
  everything else into a wonder in the capital, buying stone on the market
  when it is the bottleneck. Wins by wonder, or by influence if the wonder is
  destroyed or influence gets there first.
"""
from __future__ import annotations

from agentciv.engine import constants as C
from agentciv.engine.rules import building_cost

from .common import base_price, wonder_city
from .planner import PlannerBot


class TurtleBot(PlannerBot):
    name = "turtle"

    INFLUENCE_WEIGHT = 4.0
    ALLOW_TEMPLES = True
    TEMPLE_BIAS = 1.6
    MAX_CITIES = 4
    DEFENSE_REACH = 3
    DEFENSE_MARGIN = 1.3
    MIN_GARRISON = 3
    CITY_GARRISON = 1
    SELL_FLOOR = 0.7
    WONDER_START = 18

    def pipeline(self):
        return [self.diplomacy, self.food_safety, self.plan_site, self.defend, self.fortify,
                self.plan_wonder, self.sell, self.wonder, self.expand, self.develop,
                self.garrison_moves]

    def diplomacy(self) -> None:
        w, p = self.w, self.p
        for pr in self.proposals():
            p.accept_treaty(pr["from"])
        for q in w.rivals:
            if q not in w.treaties:
                p.propose(q, C.TREATY_MAX_TURNS)

    def proposals(self):
        from .common import treaty_proposals_to_me
        return treaty_proposals_to_me(self.w)

    def home(self):
        w = self.w
        if w.capital in w.my_cities:
            return w.capital
        return w.my_cities[0] if w.my_cities else None

    def fortify(self) -> None:
        """Walls by game phase, a standing archer garrison."""
        w, p = self.w, self.p
        home = self.home()
        if home is None:
            return
        target_walls = 1 if w.turn < 30 else (2 if w.turn < 70 else 3)
        c = w.cities[home]
        if c["buildings"].get("walls", 0) < target_walls:
            p.build_city(home, "walls", self.reserved)
        have = w.my_armies.get(home, {}).get("archer", 0) + p.recruited.get(home, {}).get("archer", 0)
        want = 2 + w.turn // 25
        if have < want and w.upkeep < self.raw.get("food", 0) * 0.5:
            p.recruit(home, "archer", want - have, self.reserved)
        for ci in w.my_cities:
            if ci == home:
                continue
            if not w.my_armies.get(ci) and not p.recruited.get(ci):
                p.recruit(ci, "archer", 1, self.reserved)

    def plan_wonder(self) -> None:
        """Reserve the next wonder stage so it isn't sold off."""
        w = self.w
        home = wonder_city(w) or self.home()
        self.wonder_home = home
        if home is None or w.turn < self.WONDER_START:
            return
        stage = w.cities[home].get("wonder_stage", 0)
        if stage >= C.WONDER_VICTORY_STAGE:
            return
        self.add_reserve(building_cost("wonder", stage + 1))

    def keep(self, r: str) -> int:
        return {"food": 40, "wood": 40, "stone": 0}.get(r, 0)

    def wonder(self) -> None:
        w, p = self.w, self.p
        home = self.wonder_home
        if home is None or w.turn < self.WONDER_START:
            return
        stage = w.cities[home].get("wonder_stage", 0)
        if stage >= C.WONDER_VICTORY_STAGE:
            return
        cost = building_cost("wonder", stage + 1)
        # buy the missing stone/wood if gold allows
        for r in ("stone", "wood"):
            short = cost.get(r, 0) - p.budget.get(r, 0)
            if short > 0 and p.budget.get("gold", 0) > cost.get("gold", 0) + 20:
                p.buy(r, short, max_price=1.6 * base_price(r))
        if p.can(cost):
            reserve_minus = {r: max(0, v - cost.get(r, 0)) for r, v in self.reserved.items()}
            if p.build_city(home, "wonder", reserve_minus):
                for r, v in cost.items():
                    self.reserved[r] = max(0, self.reserved.get(r, 0) - v)
