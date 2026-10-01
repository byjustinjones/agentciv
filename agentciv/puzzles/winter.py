"""Winter planning: keep an army fed through two winters while finishing a
build target (docs/PUZZLES.md).

Position (one player, turn 22, the last two turns of a winter): a capital in
a valley closed off by mountains, its eight surrounding tiles (four plains,
two forests, a hills tile and a gold tile), 18 infantry and small stocks.
Winter halves food yields; the army eats 18 food a turn, which the bare
valley never produces (10 food a turn, 5 in winter). The horizon ends with
the four winter turns 42-45. Stone (3 a turn) and wood (5) are short for the
target too, so what is built first matters.

Score (both parts measured at the end of turn 45)::

    lost = max(0, 18 - infantry)                  starved and disbanded units count alike
    K = max(0, 1 - 2 * lost / 18)                 each lost unit costs 1/9 of K; losing half the army, all of it
    P = cost of the target levels standing / cost of the whole target
    score = round(K * (30 + 70 * P))

The target is walls level 2 and a market hall in the capital; a level's
cost counts every unit of resource it costs (walls 1: 60, walls 2: 120,
market hall: 80; 260 in all). Doing nothing starves 8 of the 18 infantry
(K = 0.11, score 3); keeping the army but building nothing scores 30; the
reference solution keeps every unit and builds the whole target (100).
"""
from __future__ import annotations

from ..bots.base import Bot
from ..engine import constants as C
from ..engine.rules import building_cost
from .base import SOLVER, Puzzle, paint, set_player

ARMY = 18
TARGET = (("walls", 1), ("walls", 2), ("market_hall", 1))
CAPITAL = (7, 7)
VALLEY = [          # top-left (6, 6); C = the capital (plains)
    "f.h",
    ".C.",
    "g.f",
]


def _level_cost(building: str, level: int) -> int:
    return sum(building_cost(building, level).values())


TARGET_COST = sum(_level_cost(b, lv) for b, lv in TARGET)


class WinterPlanning(Puzzle):
    id = "winter"
    title = "Winter planning"
    seed = 1
    start_turn = 22
    horizon = 24            # turns 22..45: winter 22-23, then spring, summer, autumn, winter 42-45
    roles = ((SOLVER, "Steward"),)
    objective = (f"Reach the end of turn 45 with all {ARMY} infantry still alive and with walls level 2 and a "
                 "market hall in your capital. You are alone in a closed valley; turns 22-23 and 42-45 are winter.")
    scoring = (f"lost = {ARMY} - infantry at the end (starved, disbanded or killed alike); "
               f"K = max(0, 1 - 2 * lost / {ARMY}); "
               f"P = resource cost of the target levels built / {TARGET_COST} (walls 1 = 60, walls 2 = 120, "
               "market hall = 80); score = round(K * (30 + 70 * P)).")

    def build(self, g, pids) -> None:
        me = pids[SOLVER]
        paint(g, CAPITAL[0] - 1, CAPITAL[1] - 1, VALLEY)
        g.add_city(*CAPITAL, me, capital=True)
        g.player(me).capital = g.idx(*CAPITAL)
        g.place_units(*CAPITAL, me, {"infantry": ARMY})
        set_player(g, me, food=40, wood=60, stone=30, gold=40, influence=5)

    def observe(self, g, events) -> None:
        st = g.puzzle_state
        for e in events:
            if e["type"] == "starvation":
                st["starved"] = st.get("starved", 0) + sum(e.get("lost", {}).values())

    def score(self, g) -> tuple[int, str]:
        me = self.solver_pid()
        inf = g.units_of(me)["infantry"] if g.player(me).alive else 0
        lost = max(0, ARMY - inf)
        k = max(0.0, 1 - 2 * lost / ARMY)
        cap = g.cities.get(g.idx(*CAPITAL))
        built = 0
        done = []
        for b, lv in TARGET:
            if cap is not None and cap.owner == me and cap.building_level(b) >= lv:
                built += _level_cost(b, lv)
                done.append(f"{b} {lv}")
        p = built / TARGET_COST
        score = round(k * (30 + 70 * p))
        starved = g.puzzle_state.get("starved", 0)
        why = (f"{inf}/{ARMY} infantry at the end ({starved} starved); target built: "
               f"{', '.join(done) or 'nothing'} ({built}/{TARGET_COST} of its cost) -> "
               f"{k:.2f} x (30 + 70 x {p:.2f}) = {score}")
        return score, why

    def solution(self) -> Bot:
        return WinterSolution()


class WinterSolution(Bot):
    """Reference solution: a fixed build order, each item built as soon as
    it is affordable and nothing later in the list before it: three farms,
    a quarry, a lumber mill, the fourth farm, then walls 1, the market hall
    and walls 2. From turn 30 a stone shortfall of the next item is bought
    on the market when the gold covers it."""

    name = "winter-solution"
    ORDER = ("farm", "farm", "farm", "quarry", "lumber_mill", "farm", "walls", "market_hall", "walls")
    SPOTS = {"farm": [(7, 6), (6, 7), (8, 7), (7, 8)], "quarry": [(8, 6)], "lumber_mill": [(6, 6)]}
    BUY_FROM = 30

    def act(self, view: dict) -> list:
        me = view["you"]["id"]
        res = dict(view["you"]["resources"])
        have = {(i["x"], i["y"]) for i in view["map"]["improvements"]}
        city = next(c for c in view["cities"] if c["owner"] == me and (c["x"], c["y"]) == CAPITAL)
        levels = dict(city["buildings"])
        used: dict = {}
        out = []
        for b in self.ORDER:
            if b in self.SPOTS:
                k = used[b] = used.get(b, -1) + 1
                at = self.SPOTS[b][k]
                if at in have:
                    continue
                cost = C.IMPROVEMENTS[b]["cost"]
            else:
                at = CAPITAL
                want = used[b] = used.get(b, 0) + 1     # the level this entry stands for
                if levels.get(b, 0) >= want:
                    continue
                cost = building_cost(b, levels.get(b, 0) + 1)
            short = {r: q - res.get(r, 0) for r, q in cost.items() if res.get(r, 0) < q}
            if short and set(short) == {"stone"} and view["turn"] >= self.BUY_FROM:
                qty = short["stone"]
                gold = int(qty * view["market"]["prices"]["stone"] * 1.3) + 1
                if res.get("gold", 0) >= gold + 5:
                    out.append({"type": "market", "side": "buy", "resource": "stone", "qty": qty})
                    res["gold"] -= gold
                    res["stone"] += qty
                    short = {}
            if short:
                break
            for r, q in cost.items():
                res[r] -= q
            if b not in self.SPOTS:
                levels[b] = levels.get(b, 0) + 1
            out.append({"type": "build", "at": list(at), "building": b})
        return out
