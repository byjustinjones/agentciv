"""Turtle: walls, archers and a peaceful victory — wonder or influence.

Strategy
--------
* Proposes long (50-turn) treaties to everyone and accepts every proposal,
  so most rivals cannot even walk onto its land.
* Fortifies: walls in the capital early (up to level 3 as the game goes on),
  archers (x1.5 in their own city, strong against infantry) in every city,
  more when an army approaches.
* From turn ``PATH_START`` it commits to one peaceful victory, re-checked
  every few turns with hysteresis:

  - **wonder**: reserves the next stage in the capital and builds it as soon
    as the stage plus the stone/wood it has to buy on the market (a stage may
    cost more than the storage cap) is affordable;
  - **influence**: temples on every tile that can hold one, claims to open
    more temple tiles, and no wonder.

  The choice compares rough ETAs: remaining wonder cost / production value
  versus the time to build out temples and accumulate the influence target.

Trading (§13)
-------------
* **Buys peace**: offers gold for a 30-turn peace to rivals whose armies
  threaten its cities (it values peace 1.5× the threat-based estimate),
  and accepts tribute demands when peace is worth more than the tribute.
* **Buys stone/wood for its wonder** from players with spare stock in the
  turn it can afford the next stage (cheaper than the market's buy price
  with slippage and fee; nothing above the storage cap otherwise).
* Sells surplus food; answers offers with the shared valuation (accept /
  one fair counter / reject).
"""
from __future__ import annotations

from agentciv.engine import constants as C
from agentciv.engine.rules import building_cost

from .common import CAPPED, wonder_city
from .planner import PlannerBot

INF = float("inf")


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
    SELL_FLOOR = 0.5
    PATH_START = 15
    TREATY_TURNS = 30
    INFLUENCE_SWITCH = 0.65          # go for influence only if clearly faster than the wonder
    INFLUENCE_MODE_WEIGHT = 9.0      # value of 1 influence when going for influence
    BUY_FOR_IMPROVEMENTS = True

    # trading
    PEACE_SCALE = 1.5
    PEACE_TURNS = 30

    def trade_setup(self, w) -> None:
        super().trade_setup(w)
        self.path = self.memory.get("path", "wonder")

    def wonder_order(self):
        """(city, next stage cost) of the wonder we are building, or None."""
        w = self.w
        if self.path != "wonder" or w.turn < self.PATH_START - 3:
            return None
        home = wonder_city(w) or self.home()
        if home is None:
            return None
        stage = w.cities[home].get("wonder_stage", 0)
        if stage >= C.WONDER_VICTORY_STAGE:
            return None
        return home, building_cost("wonder", stage + 1)

    def build_turn_shortfall(self):
        """Stone/wood missing for the next stage if the whole stage (buying
        the rest at spot) is affordable right now, else None."""
        wo = self.wonder_order()
        if wo is None:
            return None
        cost = wo[1]
        v = self.tv
        stock = v.stock(self.w.me)
        miss = {r: max(0, cost[r] - stock.get(r, 0)) for r in ("stone", "wood")}
        need_gold = cost["gold"] + sum(q * v.prices[r] * 1.05 for r, q in miss.items())
        if stock.get("gold", 0) < need_gold:
            return None
        return miss

    def trade_needs(self) -> tuple:
        needs, gold = super().trade_needs()
        wo = self.wonder_order()
        if wo is not None:
            cost = wo[1]
            now = self.build_turn_shortfall() is not None
            for r in ("stone", "wood"):
                needs[r] = max(needs[r], cost[r] if now else min(cost[r], self.w.caps.get(r, C.STORAGE_BASE)))
            gold += cost["gold"]
        return needs, gold

    def trade_proposals(self) -> list:
        out = self.peace_offers()
        miss = self.build_turn_shortfall()
        if miss:
            out += self.purchase_bids(miss, share=0.5)
        out += self.sale_offers(resources=("food",), share=0.5)
        return out

    def peace_offers(self, turns: int = 30, share: float = 0.5, min_ratio: float = 0.5) -> list:
        return super().peace_offers(self.PEACE_TURNS, share, min_ratio)

    def pipeline(self):
        return [self.diplomacy, self.food_safety, self.choose_path, self.plan_site, self.defend,
                self.fortify, self.counter_relics, self.plan_wonder, self.sell, self.wonder,
                self.expand, self.develop, self.garrison_moves]

    def diplomacy(self) -> None:
        w, p = self.w, self.p
        for pr in self.proposals():
            if not self.relic_runner(pr["from"]):
                p.accept_treaty(pr["from"])
        for q in w.rivals:
            if q not in w.treaties and not self.relic_runner(q):
                p.propose(q, self.TREATY_TURNS)

    def proposals(self):
        from .common import treaty_proposals_to_me
        return treaty_proposals_to_me(self.w)

    def home(self):
        w = self.w
        if w.capital in w.my_cities:
            return w.capital
        return w.my_cities[0] if w.my_cities else None

    # ------------------------------------------------------------------
    # path choice
    # ------------------------------------------------------------------
    def price(self, r: str) -> float:
        return self.wts.get(r, 1.0)

    def production_value(self) -> float:
        w = self.w
        v = self.raw.get("gold", 0)
        for r in CAPPED:
            amt = self.raw.get(r, 0) - (w.upkeep if r == "food" else 0)
            v += max(0, amt) * self.price(r) * (1 - w.fee)
        return max(1.0, v)

    def wonder_eta(self) -> float:
        w = self.w
        home = wonder_city(w) or self.home()
        if home is None:
            return INF
        stage = w.cities[home].get("wonder_stage", 0)
        left = C.WONDER_VICTORY_STAGE - stage
        need = 0.0
        for k in range(stage + 1, C.WONDER_VICTORY_STAGE + 1):
            for r, v in building_cost("wonder", k).items():
                need += v * (1.0 if r == "gold" else self.price(r))
        stock = sum(w.res.get(r, 0) * (1.0 if r == "gold" else self.price(r)) for r in ("stone", "wood", "gold"))
        return max(left, (need - 0.5 * stock) / (0.85 * self.production_value()))

    def temple_slots(self) -> int:
        w = self.w
        ok = C.IMPROVEMENTS["temple"]["terrain"]
        return sum(1 for i in w.my_tiles if i not in w.cities and i not in w.improvement and w.terrain[i] in ok)

    def influence_eta(self) -> float:
        w = self.w
        target = w.thresholds.get("influence", C.INFLUENCE_VICTORY)
        have = w.res.get("influence", 0)
        rate = self.raw.get("influence", 0)
        slots = self.temple_slots()
        bonus = C.IMPROVEMENTS["temple"]["bonus"]["influence"]
        tcost = sum(v * (1.0 if r == "gold" else self.price(r)) for r, v in C.IMPROVEMENTS["temple"]["cost"].items())
        t_build = slots * tcost / (0.85 * self.production_value())
        gained = rate * t_build + bonus * slots * t_build / 2
        if have + gained >= target:
            # finishes while building: solve roughly with the average rate
            avg = max(0.5, rate + bonus * slots / 2)
            return max(0.0, (target - have) / avg)
        final = max(0.5, rate + bonus * slots)
        return t_build + (target - have - gained) / final

    def choose_path(self) -> None:
        w = self.w
        path = self.memory.get("path", "wonder")
        if w.turn >= self.PATH_START and (w.turn % 5 == 0 or "path" not in self.memory):
            home = wonder_city(w)
            stage = w.cities[home].get("wonder_stage", 0) if home is not None else 0
            ew, ei = self.wonder_eta(), self.influence_eta()
            if stage >= 2:
                path = "wonder"          # sunk cost: finish it
            elif path == "wonder" and ei < self.INFLUENCE_SWITCH * ew:
                path = "influence"
            elif path == "influence" and ew < 0.8 * ei:
                path = "wonder"
            self.memory["path"] = path
        self.path = path
        if path == "influence" and w.turn >= self.PATH_START:
            self.INFLUENCE_WEIGHT = self.INFLUENCE_MODE_WEIGHT
            self.TEMPLE_BIAS = 3.0
            self.wts = self.weights()
        else:
            self.INFLUENCE_WEIGHT = type(self).INFLUENCE_WEIGHT
            self.TEMPLE_BIAS = type(self).TEMPLE_BIAS

    # ------------------------------------------------------------------
    # defence
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # wonder
    # ------------------------------------------------------------------
    def plan_wonder(self) -> None:
        """Reserve the next wonder stage (stone/wood only up to the storage
        cap: the rest is bought on the build turn) so it isn't sold off."""
        w = self.w
        home = wonder_city(w) or self.home()
        self.wonder_home = home
        if home is None or w.turn < self.PATH_START or self.path != "wonder":
            return
        stage = w.cities[home].get("wonder_stage", 0)
        if stage >= C.WONDER_VICTORY_STAGE:
            return
        cost = building_cost("wonder", stage + 1)
        self.add_reserve({r: min(v, w.caps.get(r, v)) if r in CAPPED else v for r, v in cost.items()})

    def keep(self, r: str) -> int:
        if r == "stone" and getattr(self, "path", "wonder") == "influence":
            return 30 * min(4, self.temple_slots())
        return {"food": 40, "wood": 40, "stone": 0}.get(r, 0)

    def wonder(self) -> None:
        w = self.w
        home = self.wonder_home
        if home is None or w.turn < self.PATH_START or self.path != "wonder":
            return
        stage = w.cities[home].get("wonder_stage", 0)
        if stage >= C.WONDER_VICTORY_STAGE:
            return
        cost = building_cost("wonder", stage + 1)
        others = {r: max(0, v - cost.get(r, 0)) for r, v in self.reserved.items()}
        if self.build_with_market(home, "wonder", others):
            for r, v in cost.items():
                self.reserved[r] = max(0, self.reserved.get(r, 0) - v)
