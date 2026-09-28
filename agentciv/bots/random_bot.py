"""Random: a weak baseline that issues random, mostly-legal orders.

Each turn it picks a handful of random actions: claim a random adjacent
tile, build a random fitting improvement or city building, recruit a random
unit, move random stacks one step in a random direction, and now and then
trade on the market or propose a treaty. It never plans, so it wastes
resources and wanders its army around.
"""
from __future__ import annotations

from agentciv.engine import constants as C

from .common import Plan, SafeBot, World


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
