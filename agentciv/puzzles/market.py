"""Market-funded construction: raise the stone for three buildings from the
market, paying with the one surplus you can spare (docs/PUZZLES.md).

Position (one player, turn 6, the first turn of summer): a capital in a
closed valley with seven farmed plains and a forest with a lumber mill. Food
pours in (45 a turn in summer, 30 in autumn, against 12 food of upkeep) and
the stock is at the 300 cap, so unsold food is lost. The valley has no hills:
stone comes only from the capital (1 a turn) and the market. Wood is about
what the target needs. Gold is nearly zero.

Score (at the end of turn 13, the horizon)::

    P = cost of the target buildings standing / cost of the whole target
    lost = max(0, 12 - infantry); K = max(0, 1 - 2 * lost / 12)
    score = round(100 * K * P)

The target is a market hall and walls level 2 in the capital; a level's
cost counts every unit of resource it costs (market hall 80, walls 1: 60,
walls 2: 120; 260 in all). The target needs 100 wood (the valley has 70
and makes about 37 by turn 13) and 160 stone (30 in stock, 1 a turn), so
about 125 stone has to be bought with gold raised by selling food. A
constant-product pool punishes big batches and recovers only a quarter of
the way each turn. Doing nothing builds nothing (0); selling food in one
batch, or the food income only, or saving the gold for the end, or selling
wood, or building the market hall last all fall short of walls 2 (23-54);
the reference solution builds the whole target (100).
"""
from __future__ import annotations

from ..bots.base import Bot
from ..engine.rules import building_cost
from .base import SOLVER, Puzzle, paint, set_player

ARMY = 12
TARGET = (("market_hall", 1), ("walls", 1), ("walls", 2))
CAPITAL = (7, 7)
VALLEY = [          # top-left (6, 6)
    "...",
    ".C.",
    "..f",
]
FARMS = [(6, 6), (7, 6), (8, 6), (6, 7), (8, 7), (6, 8), (7, 8)]
MILL = (8, 8)


def _level_cost(building: str, level: int) -> int:
    return sum(building_cost(building, level).values())


TARGET_COST = sum(_level_cost(b, lv) for b, lv in TARGET)


class MarketConstruction(Puzzle):
    id = "market"
    title = "Market-funded construction"
    seed = 2
    start_turn = 6
    horizon = 8              # turns 6..13 (summer 6-11, autumn 12-13)
    roles = ((SOLVER, "Builder"),)
    objective = ("By the end of turn 13, have a market hall and walls level 2 in your capital, "
                 f"with all {ARMY} infantry still alive. You are alone in a closed valley; the market is the "
                 "only other source of resources.")
    scoring = (f"P = resource cost of the target buildings standing at the end / {TARGET_COST} "
               "(market hall = 80, walls 1 = 60, walls 2 = 120); "
               f"lost = {ARMY} - infantry at the end; K = max(0, 1 - 2 * lost / {ARMY}); "
               "score = round(100 * K * P).")

    def build(self, g, pids) -> None:
        me = pids[SOLVER]
        paint(g, CAPITAL[0] - 1, CAPITAL[1] - 1, VALLEY)
        g.add_city(*CAPITAL, me, capital=True)
        g.player(me).capital = g.idx(*CAPITAL)
        for x, y in FARMS:
            g.improvement[g.idx(x, y)] = "farm"
        g.improvement[g.idx(*MILL)] = "lumber_mill"
        g.place_units(*CAPITAL, me, {"infantry": ARMY})
        set_player(g, me, food=290, wood=70, stone=30, gold=10, influence=5)

    def score(self, g) -> tuple[int, str]:
        me = self.solver_pid()
        inf = g.units_of(me)["infantry"] if g.player(me).alive else 0
        lost = max(0, ARMY - inf)
        k = max(0.0, 1 - 2 * lost / ARMY)
        cap = g.cities.get(g.idx(*CAPITAL))
        built, done = 0, []
        for b, lv in TARGET:
            if cap is not None and cap.owner == me and cap.building_level(b) >= lv:
                built += _level_cost(b, lv)
                done.append(f"{b} {lv}")
        p = built / TARGET_COST
        score = round(100 * k * p)
        why = (f"target built: {', '.join(done) or 'nothing'} ({built}/{TARGET_COST} of its cost); "
               f"{inf}/{ARMY} infantry -> 100 x {k:.2f} x {p:.2f} = {score}")
        return score, why

    def solution(self) -> Bot:
        return MarketSolution()


class MarketSolution(Bot):
    """Reference solution: every turn sell up to 100 food (the largest
    single order the 400-food pool takes), buy as much stone as the gold
    on hand pays for at the current price plus the fee, and order the
    market hall, walls and walls again (each executes once affordable;
    the market hall comes first for its lower fee and +5 gold a turn)."""

    name = "market-solution"

    def act(self, view: dict) -> list:
        res = view["you"]["resources"]
        out = []
        if res.get("food", 0) > 0:
            out.append({"type": "market", "side": "sell", "resource": "food", "qty": min(100, res["food"])})
        price = view["market"]["prices"]["stone"] * (1 + view["market"]["fee"])
        qty = min(100, int(res.get("gold", 0) / price))
        if qty > 0:
            out.append({"type": "market", "side": "buy", "resource": "stone", "qty": qty})
        for b in ("market_hall", "walls", "walls"):
            out.append({"type": "build", "at": list(CAPITAL), "building": b})
        return out
