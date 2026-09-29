"""Economist: a peaceful builder going for the economic victory.

Strategy
--------
* Accepts every treaty and proposes long treaties to everyone.
* Expands steadily (claims, settling new cities), builds improvements by
  return on investment, a market hall early and a warehouse when near caps.
* Sells surplus food/wood/stone on the market every turn.
* After the build-up phase it hoards gold for the economic victory, only
  investing in things that repay quickly.
* Defends minimally: reacts to armies within 2 turns of a city with just
  enough infantry/archers, keeps one unit in the capital.
"""
from __future__ import annotations

from .planner import PlannerBot


class EconomistBot(PlannerBot):
    name = "economist"

    INFLUENCE_WEIGHT = 2.5
    ALLOW_TEMPLES = False
    MAX_CITIES = 6
    DEFENSE_REACH = 2
    DEFENSE_MARGIN = 1.0
    MIN_GARRISON = 2
    SELL_FLOOR = 0.6
    HOARD_TURN = 55            # after this turn, gold is saved for victory
    DEFENSIVE_WALLS = False    # a pure builder: never raises walls and spends
    DEFENSE_BUY_FRACTION = 0.15  # little of its hoard on defenders (its weak spot)

    def pipeline(self):
        return [self.diplomacy, self.food_safety, self.plan_site, self.defend, self.counter_relics,
                self.sell, self.expand, self.develop, self.garrison_moves]

    def diplomacy(self) -> None:
        self.accept_all_and_propose(turns=25)

    def prepare(self) -> None:
        super().prepare()
        w = self.w
        gold = w.res.get("gold", 0)
        self.hoarding = w.turn >= self.HOARD_TURN or gold >= 700
        if self.hoarding:
            # keep gold: stricter return requirement on anything costing gold
            self.MIN_ROI = 1 / 20.0

    def keep(self, r: str) -> int:
        return {"food": 30, "wood": 40, "stone": 30}.get(r, 0)

    def develop(self) -> None:
        if self.hoarding:
            # only gold-free or fast-paying investments
            self.reserved["gold"] = max(self.reserved.get("gold", 0), int(self.w.res.get("gold", 0) * 0.9))
        super().develop()
