"""Stop an imminent victory: a rival is four turn ends from an economic
victory; break its streak in time (docs/PUZZLES.md).

Position (two players, turn 40, max_turns 45, so B = 1800): the rival (p1,
the house ``banker`` bot with a fixed seed, which never negotiates) holds
1850 banked gold, an economic streak of 6 and enough gold to bank its
streak deposit every turn: at the end of turn 43 its streak reaches 10 and
it wins. It owns its capital (far west) and an outpost city at (8, 7) with
walls 1 and an archer. The solver (p2) owns a capital at (13, 7) and an army
at (12, 7) (3 cavalry, 5 infantry); a two-tile corridor of open plains leads
from it to the outpost. Mountains close everything else.

The only rule that ends a running economic streak the solver can trigger
here is the capture of one of the rival's cities (rules §11). Infantry needs
four turns to reach the outpost (one tile a turn), cavalry two (two tiles a
turn). The rival's gold income (25 a turn from mined gold fields) is exactly
its streak deposit and it has no wood, so it cannot reinforce the outpost.

Score: k = the turn (counted from 0 = turn 40) at whose end the rival's
economic streak first ends::

    rival wins (or the streak never breaks)  ->  0
    otherwise                                ->  100 - 25 * (k - 1), at most 100

The earliest possible break is k = 1 (the cavalry, which reaches the
outpost on the second turn); the last chance is k = 3 (50).
"""
from __future__ import annotations

from ..bots.banker import BankerBot
from ..bots.base import Bot
from .base import SOLVER, Puzzle, QuietBot, paint, set_player

RIVAL_CAPITAL = (3, 7)
OUTPOST = (8, 7)
CAPITAL = (13, 7)
ARMY_AT = (12, 7)
ARMY = {"cavalry": 5, "infantry": 4}
RIVAL_SEED = 7
K_BEST = 1


class StopVictory(Puzzle):
    id = "stop-victory"
    title = "Stop an imminent victory"
    seed = 4
    start_turn = 40
    horizon = 5             # turns 40..44; the rival wins at the end of turn 43 unless stopped
    roles = (("rival", "Ledger"), (SOLVER, "Challenger"))
    objective = ("Your rival p1 meets the economic victory requirement and its streak will reach 10 at the end "
                 "of turn 43 (players[].economic_streak, victory.thresholds). Prevent that victory, as early as "
                 "you can.")
    scoring = ("k = turns from turn 40 to the turn at whose end the rival's economic streak first ends "
               "(turn 40 is k = 0). Score 0 if the rival wins or its streak never ends; otherwise "
               f"min(100, 100 - 25 * (k - {K_BEST})).")

    def build(self, g, pids) -> None:
        rival, me = pids["rival"], pids[SOLVER]
        # corridor row y = 7 from the rival capital to the solver's capital
        paint(g, 2, 6, ["ggg", "g.g", "ggg"])     # the rival's mined gold fields
        paint(g, 3, 7, [".........."])          # the road, x = 3..12
        paint(g, 12, 6, ["...", "...", "..."])
        g.add_city(*RIVAL_CAPITAL, rival, capital=True)
        g.player(rival).capital = g.idx(*RIVAL_CAPITAL)
        out = g.add_city(*OUTPOST, rival)
        out.walls = 1
        for x in (5, 6):
            g.set_owner(x, 7, rival)       # the road between the rival's cities
        g.add_city(*CAPITAL, me, capital=True)
        g.player(me).capital = g.idx(*CAPITAL)
        for i in g.radius(g.idx(*RIVAL_CAPITAL), 1):
            if g.terrain[i] == "g":
                g.improvement[i] = "mine"
        g.place_units(*RIVAL_CAPITAL, rival, {"archer": 4})
        g.place_units(*OUTPOST, rival, {"archer": 1})
        g.place_units(*ARMY_AT, me, dict(ARMY))
        set_player(g, rival, food=40, wood=0, stone=20, gold=40, influence=10, bank=1850, legacy=600,
                   economic_streak=6, deals=4, contracts_honoured=2)
        set_player(g, me, food=150, wood=100, stone=60, gold=120, influence=30, legacy=300)

    def opponent(self, role: str) -> Bot:
        return QuietBot(BankerBot(seed=RIVAL_SEED))

    def observe(self, g, events) -> None:
        st = g.puzzle_state
        rival = self.pids()["rival"]
        for e in events:
            if ("broken" not in st and e["type"] == "streak_ended" and e.get("player") == rival
                    and e.get("condition") == "economic"):
                st["broken"] = e["turn"] - self.start_turn

    def score(self, g) -> tuple[int, str]:
        rival = self.pids()["rival"]
        res = g.result or {}
        k = g.puzzle_state.get("broken")
        if res.get("winner") == rival and res.get("condition") != "score":
            return 0, f"the rival won by {res.get('condition')} at the end of turn {res.get('turn')}"
        if k is None:
            return 0, "the rival's economic streak never ended"
        score = min(100, 100 - 25 * (k - K_BEST))
        return score, (f"the rival's streak ended at the end of turn {self.start_turn + k} (k = {k}) -> "
                       f"100 - 25 x ({k} - {K_BEST}) = {score}")

    def solution(self) -> Bot:
        return VictorySolution()


class VictorySolution(Bot):
    """Reference solution: the cavalry rides the corridor (two tiles a
    turn) and attacks the outpost on the second turn; the infantry follows
    one tile a turn."""

    name = "victory-solution"

    def act(self, view: dict) -> list:
        me = view["you"]["id"]
        out = []
        for a in view["armies"]:
            if a["owner"] != me:
                continue
            x, y, units = a["x"], a["y"], a["units"]
            if y != 7 or x <= OUTPOST[0]:
                continue
            cav = units.get("cavalry", 0)
            if cav:
                path = [[x - 1, 7], [x - 2, 7]] if x - 2 >= OUTPOST[0] else [[x - 1, 7]]
                out.append({"type": "move", "from": [x, y], "path": path, "units": {"cavalry": cav}})
            inf = units.get("infantry", 0)
            if inf:
                out.append({"type": "move", "from": [x, y], "to": [x - 1, 7], "units": {"infantry": inf}})
        return out
