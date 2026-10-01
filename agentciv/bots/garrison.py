"""Standing garrisons for bots that race on a streak (rules §11).

The loss of *any* city sets both streaks to 0, so a player on an economic or
influence streak has to hold every city, not only the capital. The house
bots before ``banker`` and ``zealot`` only *kept* units where they happened
to be (``PlannerBot.lock_garrison``); :class:`GarrisonMixin` also recruits
them: an archer stack in the capital that grows with the race's progress,
a fixed number of defenders in every other city, and walls. It also
retakes a lost original capital, without which neither streak can run.
"""
from __future__ import annotations

from agentciv.engine import constants as C

from .common import add_units, best_counter, simulate_attack


class GarrisonMixin:
    """For :class:`~agentciv.bots.planner.PlannerBot` subclasses."""

    GARRISON_UNIT = "archer"      # x1.5 in its own city, x1.5 against infantry
    GARRISON_PER_TURN = 2         # recruits per city per turn (keeps the economy going)
    GARRISON_FOOD_SHARE = 0.6     # share of the food surplus their upkeep may take

    def food_room(self) -> int:
        """Upkeep (food per turn) we can still add without starving."""
        w, p = self.w, self.p
        return int(self.GARRISON_FOOD_SHARE * self.raw.get("food", 0) + p.budget.get("food", 0) / 15 - w.upkeep)

    def present(self, c: int) -> int:
        """Units that will be in city ``c`` at the end of this turn."""
        return sum(add_units(self.p.available(c), self.p.recruited.get(c, {})).values())

    def keep_garrisons(self, capital: int, others: int, walls: int = 0) -> None:
        """Recruit toward ``capital`` units in the capital and ``others`` in
        every other city (at most GARRISON_PER_TURN per city and turn, within
        the food room), and raise the capital's walls to ``walls``."""
        w, p = self.w, self.p
        home = w.capital if w.capital in w.my_cities else None
        room = self.food_room()
        order = sorted(w.my_cities, key=lambda c: (c != home, c))
        for c in order:
            want = capital if c == home else others
            miss = min(want - self.present(c), self.GARRISON_PER_TURN)
            if miss <= 0 or room <= 0:
                continue
            k = p.recruit(c, self.GARRISON_UNIT, min(miss, room), self.reserved)
            room -= k * C.UNITS[self.GARRISON_UNIT]["upkeep"]
        if home is not None and walls:
            level = w.cities[home]["buildings"].get("walls", 0) + p.city_builds.get((home, "walls"), 0)
            if level < min(walls, C.CITY_BUILDINGS["walls"]["max"]):
                p.build_city(home, "walls", self.reserved)

    RETAKE_RATIO = 1.1            # simulated power margin for the assault

    def lost_capital(self):
        """(tile, holder) of our original capital while a rival holds it
        (no economic or influence streak without it, rules §11)."""
        w = self.w
        for i, c in w.cities.items():
            if c.get("capital") and c.get("original_owner") == w.me and c["owner"] != w.me:
                return i, c["owner"]
        return None

    def retake_capital(self) -> None:
        """Raise an army in the city nearest our lost original capital and
        assault it when the simulated battle wins with RETAKE_RATIO."""
        w, p = self.w, self.p
        lost = self.lost_capital()
        if lost is None or not w.my_cities:
            return
        tgt, holder = lost
        if w.at_peace(w.me, holder):
            return
        enter = w.can_enter_fn(w.me)
        dist = w.bfs([tgt], enter)
        city = min(w.my_cities, key=lambda c: (dist.get(c, 999), c))
        if city not in dist:
            return
        have: dict = {}
        for i in w.my_armies:
            if dist.get(i, 99) <= 8:
                have = add_units(have, self.free_units(i))
        have = add_units(have, p.recruited.get(city, {}))
        defenders = {q: dict(u) for q, u in w.armies.get(tgt, {}).items() if q != w.me}
        enemy: dict = {}
        for u in defenders.values():
            enemy = add_units(enemy, u)
        siege = C.SIEGE_PER_WALL_LEVEL * w.cities[tgt]["buildings"].get("walls", 0) - have.get("siege", 0)
        room = self.food_room()
        if siege > 0 and room > 0:
            k = p.recruit(city, "siege", min(siege, max(1, room // 2)), {"food": 20})
            have = add_units(have, {"siege": k})
            room -= 2 * k
        t = best_counter(enemy or {"infantry": 1}, allowed=("infantry", "cavalry", "archer"))
        for _ in range(6):
            win, _, ratio = simulate_attack(w, w.me, have, tgt, assume_war=True) if have else (False, {}, 0.0)
            if (win and ratio >= self.RETAKE_RATIO + 0.2) or room <= 0:
                break
            k = p.recruit(city, t, min(4, room), {"food": 20})
            if k == 0:
                break
            have = add_units(have, {t: k})
            room -= k * C.UNITS[t]["upkeep"]
        self.offense(tgt, min_ratio=self.RETAKE_RATIO, max_dist=10)
