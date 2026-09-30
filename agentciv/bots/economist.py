"""Economist: a peaceful builder going for the economic victory.

Strategy
--------
* Accepts every treaty and proposes long treaties to everyone.
* Expands steadily (claims, settling new cities), builds improvements by
  return on investment, a market hall early and a warehouse when near caps.
* Sells surplus food/wood/stone on the market every turn.
* From turn 10 it banks what gold is left after each turn's plan, up to the
  bank limit, for the economic victory.
* Defends minimally: reacts to armies within 2 turns of a city with just
  enough infantry/archers, keeps one unit in the capital.

Trading (§13)
-------------
* Offers its surplus (stock above its keep levels) to the players who need
  it most at a **fair** price (half of the estimated joint surplus each —
  both sides save the market fee and slippage), before selling the rest on
  the market.
* A patient lender (discount 0.995/turn): offers **loans** — gold now for
  more gold per turn later (~20% interest over 15 turns) — to solvent
  players (gold income, no defaults, able to pay) that are not close to
  winning, while its own gold race is still far away. At most one loan per
  borrower at a time, no bigger than the borrower's credit limit (150 gold
  of instalments with no history, growing with contracts it honoured).
* Answers offers with the shared valuation: accepts when the gain clears a
  margin, counters once at the fair split, rejects losing deals.
"""
from __future__ import annotations

import math

from agentciv.engine import constants as C

from .common import bank_of, contract_income, contract_obligations
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
    BANK_START = 10            # banks from this turn on
    DEFENSIVE_WALLS = False    # a pure builder: never raises walls and spends
    DEFENSE_BUY_FRACTION = 0.15  # little of its hoard on defenders (its weak spot)

    # trading
    DISCOUNT = 0.995            # a hoarder: gold later is almost as good as gold now
    ACCEPT_FRACTION = 0.02
    LOAN_TURNS = 15
    LOAN_INTEREST = 0.2
    LOAN_MAX = 600
    LOAN_SHARE = 0.35           # at most this share of our gold out on loan
    LOAN_START = 12

    def trade_proposals(self) -> list:
        out = self.sale_offers(share=0.5)
        out += self.loan_offers()
        return out

    def loan_offers(self) -> list:
        """Gold now for more gold per turn later, to a solvent player that
        is not close to winning (and while our own race is far away)."""
        w, v = self.tw, self.tv
        gold = v.stock(w.me).get("gold", 0)
        target = w.thresholds.get("bank", C.BANK_VICTORY)
        if w.turn < self.LOAN_START or gold < 300 or bank_of(w) >= 0.5 * target:
            return []
        if v.remaining < self.LOAN_TURNS + 5:
            return []
        out_now = 0
        for c in w.view.get("contracts", []) or []:
            if c.get("payee") == w.me:
                out_now += int((c.get("per_turn") or {}).get("gold", 0)) * int(c.get("turns_left", 0))
        budget = int(self.LOAN_SHARE * (gold + out_now)) - out_now
        if budget < 100:
            return []
        paying = {c.get("payer") for c in w.view.get("contracts", []) or [] if c.get("payee") == w.me}
        cands = []
        for q in self.partners():
            pl = w.players.get(q) or {}
            rep = pl.get("reputation") or {}
            if q in paying or rep.get("defaults", 0) or rep.get("betrayals", 0) > 1:
                continue
            inc = (pl.get("income") or {}).get("gold", 0) - contract_obligations(w, q).get("gold", 0)
            inc += contract_income(w, q).get("gold", 0)
            if inc < 4:
                continue
            principal = int(min(budget, self.LOAN_MAX, max(150, 22 * inc),
                                v.credit_limit(q) / (1 + self.LOAN_INTEREST)))   # credit grows with history
            if principal < 100:
                continue
            inst = int(math.ceil(principal * (1 + self.LOAN_INTEREST) / self.LOAN_TURNS))
            if v.reliability(q, {"gold": inst}, self.LOAN_TURNS) < 0.7:
                principal = int(min(principal, 0.8 * inc * self.LOAN_TURNS / (1 + self.LOAN_INTEREST)))
                inst = int(math.ceil(principal * (1 + self.LOAN_INTEREST) / self.LOAN_TURNS))
                if principal < 100 or v.reliability(q, {"gold": inst}, self.LOAN_TURNS) < 0.7:
                    continue
            give = {"gold": principal}
            get = {"per_turn": {"gold": inst}, "turns": self.LOAN_TURNS}
            deal = {"from": w.me, "to": q, "give": give, "get": get, "peace": None}
            if v.deal_gain(deal, q) < 0:
                continue          # they would not want it (no use for gold now)
            cands.append((-v.deal_gain(deal), q, give, get))
        out = []
        for g, q, give, get in sorted(cands)[:1]:
            out.append({"to": q, "give": give, "get": get, "kind": "loan", "value": -g,
                        "text": f"loan: {give['gold']} gold now for {get['per_turn']['gold']} gold/turn "
                                f"x{get['turns']}"})
        return out

    def pipeline(self):
        return [self.diplomacy, self.food_safety, self.plan_site, self.defend, self.counter_relics,
                self.sell, self.expand, self.develop, self.garrison_moves]

    def diplomacy(self) -> None:
        self.accept_all_and_propose(turns=25)

    def bank_wanted(self) -> bool:
        return self.w.turn >= self.BANK_START

    def keep(self, r: str) -> int:
        return {"food": 30, "wood": 40, "stone": 30}.get(r, 0)

