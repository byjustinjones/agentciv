"""Random: a weak baseline that issues random, mostly-legal orders.

Each turn it picks a handful of random actions: claim a random adjacent
tile, build a random fitting improvement or city building, recruit a random
unit, move random stacks one step in a random direction, and now and then
trade on the market or propose a treaty. It never plans, so it wastes
resources and wanders its army around.

In negotiations (§13) it now and then proposes a random resource-for-gold
swap at a random price and accepts (25%) or rejects (25%) incoming deals
at random — the only built-in bot that takes losing deals, within limits
(it fills seats in rated games, so it must not be a free buffet): it never
hands over land or a contract, at most half of any stock, at most 1.5x the
market value it receives, and it accepts at most one deal per turn.
"""
from __future__ import annotations

import random

from agentciv.engine import constants as C
from agentciv.engine.deals import bundle_value

from .common import Plan, SafeBot, World, spot_prices


class RandomBot(SafeBot):
    name = "random"

    def decide(self, view: dict) -> list:
        w = World(view)
        p = self.new_plan(w)
        rng = self.rng
        actions = [self._claim, self._improve, self._city_build, self._recruit, self._market,
                   self._settle, self._diplomacy]
        for _ in range(rng.randint(1, 5)):
            rng.choice(actions)(w, p)
        self._moves(w, p)
        return p.orders

    def decide_deals(self, view: dict) -> list:
        rng = self.__dict__.get("_nrng")
        if rng is None:
            rng = self.__dict__["_nrng"] = random.Random(f"negotiate:{self.name}:{self.seed}")
        me = view["you"]["id"]
        res = view["you"].get("resources", {}) or {}
        out = []
        turn = view.get("turn", 0)
        stamp = [view.get("game_id"), turn]
        for d in (view.get("deals") or {}).get("open") or []:
            if d.get("to") != me or d.get("status", "open") != "open":
                continue
            x = rng.random()
            if x < 0.25 and d.get("deliverable", True) and self.memory.get("accepted") != stamp \
                    and self.tolerable(view, d, res):
                self.memory["accepted"] = stamp
                out.append({"type": "accept", "deal": d["id"]})
                break                  # one per turn
            if x < 0.5:
                out.append({"type": "reject", "deal": d["id"], "message": "no"})
        if self.memory.get("neg_turn") == turn:
            return out
        self.memory["neg_turn"] = turn
        rivals = [p["id"] for p in view.get("players", []) if p.get("alive") and p["id"] != me]
        if not rivals or rng.random() >= 0.15:
            return out
        w = World(view)
        prices = spot_prices(w)
        to = rng.choice(rivals)
        r = rng.choice(C.MARKET_RESOURCES)
        if rng.random() < 0.5:
            qty = rng.randint(5, max(5, res.get(r, 0) // 3))
            if qty > res.get(r, 0):
                return out
            gold = max(1, int(qty * prices[r] * rng.uniform(0.6, 1.5)))
            give, get = {r: qty}, {"gold": gold}
        else:
            qty = rng.randint(5, 60)
            gold = max(1, int(qty * prices[r] * rng.uniform(0.6, 1.5)))
            if gold > res.get("gold", 0):
                return out
            give, get = {"gold": gold}, {r: qty}
        out.append({"type": "propose", "to": to, "give": give, "get": get, "expires_in": 1,
                    "message": "[random] how about this?"})
        return out

    @staticmethod
    def tolerable(view: dict, deal: dict, res: dict) -> bool:
        """A bad deal random may take, but not one that guts it: no land or
        contract out, at most half of any stock, at most 1.5x the market
        value it gets (received land counts nothing, contracts discounted)."""
        out, inn = deal.get("get") or {}, deal.get("give") or {}
        if out.get("tiles") or out.get("per_turn"):
            return False
        if any(int(out.get(r, 0) or 0) > int(res.get(r, 0) or 0) // 2 for r in C.TRADABLE):
            return False
        prices = (view.get("market") or {}).get("prices") or {}
        return bundle_value(out, prices) <= 1.5 * bundle_value(inn, prices, discount=0.9)

    def _claim(self, w: World, p: Plan) -> None:
        cands = sorted({j for i in w.my_tiles for j in w.nb[i]
                        if w.owner[j] is None and w.passable(j)})
        if cands:
            p.claim(self.rng.choice(cands))

    def _improve(self, w: World, p: Plan) -> None:
        tiles = [i for i in w.my_tiles if i not in w.cities and i not in w.improvement]
        if not tiles:
            return
        i = self.rng.choice(tiles)
        opts = [b for b, s in C.IMPROVEMENTS.items() if w.terrain[i] in s["terrain"]]
        if opts:
            p.improve(i, self.rng.choice(opts))

    def _city_build(self, w: World, p: Plan) -> None:
        if w.my_cities:
            p.build_city(self.rng.choice(sorted(w.my_cities)), self.rng.choice(sorted(C.CITY_BUILDINGS)))

    def _recruit(self, w: World, p: Plan) -> None:
        if w.my_cities:
            p.recruit(self.rng.choice(sorted(w.my_cities)), self.rng.choice(C.UNIT_TYPES), self.rng.randint(1, 3))

    def _market(self, w: World, p: Plan) -> None:
        r = self.rng.choice(C.MARKET_RESOURCES)
        qty = self.rng.randint(5, 40)
        if self.rng.random() < 0.5:
            p.sell(r, qty)
        else:
            p.buy(r, qty)

    def _settle(self, w: World, p: Plan) -> None:
        cands = sorted({j for i in w.my_tiles for j in w.nb[i] if w.passable(j)} | set(w.my_tiles))
        self.rng.shuffle(cands)
        for j in cands[:10]:
            if p.can_settle_at(j):
                p.settle(j)
                return

    def _diplomacy(self, w: World, p: Plan) -> None:
        for pr in w.view.get("treaty_proposals", []):
            if pr.get("to") == w.me and self.rng.random() < 0.5:
                p.accept_treaty(pr["from"])
        if w.rivals and self.rng.random() < 0.3:
            p.propose(self.rng.choice(w.rivals), self.rng.randint(C.TREATY_MIN_TURNS, C.TREATY_MAX_TURNS))

    def _moves(self, w: World, p: Plan) -> None:
        for i in sorted(w.my_armies):
            if self.rng.random() < 0.4:
                opts = [j for j in w.nb[i] if w.passable(j)]
                if opts:
                    j = self.rng.choice(opts)
                    units = p.available(i)
                    # move a random part of the stack
                    part = {u: self.rng.randint(0, c) for u, c in units.items()}
                    p.move(i, [j], part if any(part.values()) else None)
