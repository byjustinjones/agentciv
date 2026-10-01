"""Banker: the economic race as the LLM players ran it in g8-g10.

Modelled on what the agents did, not on the economist: they reached the
bank target while fourth on score, banked no more than the streak needed
once there, stacked archers in the capital and held the 10-turn streak.

Strategy
--------
* **Market halls first.** The bank allowance is 50 + 10 per city with a
  market hall (rules §5), so every city gets one as soon as it can be
  afforded; the capital's comes before anything else.
* **The allowance every turn.** From ``BANK_START`` the turn's allowance is
  reserved before any other spending and banked at the end of the turn.
  Once the bank holds the target (B), it banks exactly the streak deposit
  (half the allowance, rounded up) and spends the rest on defence.
* **Stops expanding near the target.** No new city sites once the bank is
  at ``SETTLE_STOP`` of B (a new city is one more city to defend, and it
  pays off too late).
* **Holds every city** (``GarrisonMixin``): an archer stack in the capital
  that grows with the bank (``CAPITAL_ARCHERS`` at the target), walls in
  the capital from 40% of B, and ``STREAK_CITY_GARRISON`` defenders in every
  other city once near the target. Threatened cities still recruit counters
  as every planner bot does.
* **Diplomacy.** Accepts every treaty and proposes treaties to everyone, as
  the agents did. (A treaty does not protect a streak holder: breaking a
  treaty with one is free, rules §9.)
* **Trading.** Sells its surplus at a fair price; lends nothing (gold out on
  loan is gold not in the bank).
"""
from __future__ import annotations

from agentciv.engine import constants as C
from agentciv.engine.rules import streak_deposit

from .common import bank_limit, bank_of
from .garrison import GarrisonMixin
from .planner import PlannerBot


class BankerBot(GarrisonMixin, PlannerBot):
    name = "banker"

    INFLUENCE_WEIGHT = 2.5
    ALLOW_TEMPLES = False
    MAX_CITIES = 4
    DEFENSE_REACH = 3
    DEFENSE_MARGIN = 1.2
    MIN_GARRISON = 2
    CITY_GARRISON = 1
    STREAK_CITY_GARRISON = 2
    STREAK_GUARD_SHARE = 0.7
    SELL_FLOOR = 0.55
    DEFENSIVE_WALLS = True
    BANK_START = 6
    BANK_KEEP = 0
    SETTLE_STOP = 0.5             # no new city sites once the bank is at this share of B
    CAPITAL_ARCHERS = 14          # archers in the capital when the bank is at B
    WALLS_AT = ((0.4, 1), (0.8, 2))   # (bank share, capital wall level)

    # trading
    DISCOUNT = 0.99
    ACCEPT_FRACTION = 0.02

    def pipeline(self):
        return [self.diplomacy, self.food_safety, self.plan_site, self.defend, self.counter_relics,
                self.market_halls, self.sell, self.fortify, self.retake_capital, self.expand, self.develop, self.garrison_moves]

    # ---- the bank -------------------------------------------------------
    def target(self) -> int:
        return int(self.w.thresholds.get("bank", C.BANK_VICTORY))

    def share(self) -> float:
        return bank_of(self.w) / max(1, self.target())

    def bank_wanted(self) -> bool:
        return self.w.turn >= self.BANK_START

    def deposit(self) -> int:
        """Gold to bank this turn: the allowance, or once at the target only
        the streak deposit (rules §11)."""
        w = self.w
        lim = bank_limit(w)
        if bank_of(w) >= self.target():
            return int(w.you.get("streak_deposit") or streak_deposit(lim))
        return lim

    def prepare(self) -> None:
        super().prepare()
        if self.bank_wanted():
            # the deposit comes before every other use of gold
            self.reserved["gold"] = self.contract_gold + self.deposit()

    def bank_step(self) -> None:
        if not self.bank_wanted() or self.p.full():
            return
        p = self.p
        amt = min(self.deposit(), int(p.budget.get("gold", 0)) - self.BANK_KEEP - self.contract_gold)
        if amt >= 1:
            p.budget["gold"] -= amt
            p.orders.append({"type": "bank", "gold": amt})

    # ---- buildings ------------------------------------------------------
    def market_halls(self) -> None:
        """A market hall in every city (+10 allowance each), capital first."""
        w, p = self.w, self.p
        for c in sorted(w.my_cities, key=lambda c: (c != w.capital, c)):
            if not w.cities[c]["buildings"].get("market_hall"):
                if not p.build_city(c, "market_hall", self.reserved) and c == w.capital:
                    self.build_with_market(c, "market_hall", self.reserved, max_mult=1.4)

    def city_buildings(self) -> None:
        w, p = self.w, self.p
        cap = w.capital if w.capital in w.my_cities else (w.my_cities[0] if w.my_cities else None)
        if cap is None:
            return
        if not any(w.cities[c]["buildings"].get("warehouse") for c in w.my_cities):
            near = any(p.budget.get(r, 0) + self.raw.get(r, 0) * 1.5 >= w.caps.get(r, 300) * 0.85
                       for r in ("food", "wood", "stone"))
            if near:
                p.build_city(cap, "warehouse", self.reserved)

    # ---- expansion ------------------------------------------------------
    def plan_site(self) -> None:
        if self.share() >= self.SETTLE_STOP:
            self.site, self.site_path = None, []
            return
        super().plan_site()

    def settle_step(self) -> None:
        if self.share() >= self.SETTLE_STOP:
            return
        super().settle_step()

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

    def diplomacy(self) -> None:
        self.accept_all_and_propose(turns=30)

    def trade_proposals(self) -> list:
        return self.sale_offers(share=0.5)

    def keep(self, r: str) -> int:
        return {"food": 40, "wood": 50, "stone": 40}.get(r, 0)
