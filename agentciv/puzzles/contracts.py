"""Contract valuation: six simultaneous offers; accept the set worth the
most (docs/PUZZLES.md).

Position (five players, turn 2): the solver holds a small capital with 20
infantry, 220 food (food income 10 a turn in spring, 15 in summer, against
20 food of upkeep), 70 stone (+3 a turn) and a rent contract from earlier:
it pays Quarry (p4) 20 stone a turn for 4 more turns. Four traders have
sent six offers, all open until the end of turn 4:

==========  =======  =====================================  =====================================
deal        from     the solver gives / gets                why it is worth what it is worth
==========  =======  =====================================  =====================================
O1 = d3     Miller   gives 100 food, gets 120 gold          above market; the stock can spare it
O2 = d4     Vault    gives 100 gold, gets 15 gold x 8       a loan to a rich payer that always pays
O3 = d5     Miller   gives 100 gold, gets 40 wood           wood at 2.5 gold (start price 1.5)
O4 = d6     Drifter  gives 150 gold, gets 25 gold x 8       Drifter has 10 gold, 2 gold income and
                                                            already owes Quarry 30 gold a turn: it
                                                            pays three instalments, then defaults
O5 = d7     Quarry   gives 60 stone, gets 150 gold          above market, but the stone left can't
                                                            pay the rent: default, fine, bank seized
O6 = d8     Vault    gives 100 food, gets 110 gold          fine alone; with O1 the army starves
==========  =======  =====================================  =====================================

(d1 and d2 are the earlier deals behind the two running contracts.)

Valuation (gold equivalent, start prices of rules §7 as for contract
defaults)::

    V = gold + bank + food + 1.5 * wood + 2 * stone
        + 35 * infantry                 (an infantry's recruit cost at those prices)
        + 2 * (influence - influence_debt)   (2 gold per influence, the default-fine rate)

V(S) is the solver's V at the end of turn 9 when the set S is accepted on
turn 2 (in the order it was accepted in the game; an offer that cannot be
delivered at that point is skipped) and the solver does nothing else, the
traders following their scripts: eight turns, the length of the longest
contract. Only which offers were accepted counts; counters, rejections and
everything else the solver did are not scored. ::

    score = round(100 * (V(S) - V({})) / (V(best) - V({})))   clamped to 0..100

where best is the subset with the highest V (O1 + O2). Accepting nothing
scores 0; accepting every offer that looks profitable on its face scores 0.
"""
from __future__ import annotations

import itertools
import threading

from ..bots.base import Bot
from ..engine import deals as D
from ..engine.game import Game
from .base import SOLVER, Puzzle, PuzzleGame, ScriptedBot, paint, set_player

WINDOW = 8              # turns each accepted set is played forward for (the longest contract)
BEST = ("O1", "O2")     # the subset with the highest V (tests check it against ContractValuation.best())
UNIT_VALUE = 35         # infantry: 15 food + 10 wood x 1.5 + 5 gold
INFLUENCE_VALUE = 2     # gold per influence (CONTRACT_DEFAULT_GOLD_PER_INFLUENCE)

# (label, proposer role, proposer gives, proposer gets, message)
OFFERS = (
    ("O1", "miller", {"gold": 120}, {"food": 100}, "Buying 100 food at 1.2 gold each."),
    ("O2", "vault", {"per_turn": {"gold": 15}, "turns": 8}, {"gold": 100},
     "Loan request: 100 gold now, 15 gold a turn for 8 turns."),
    ("O3", "miller", {"wood": 40}, {"gold": 100}, "40 wood for 100 gold."),
    ("O4", "drifter", {"per_turn": {"gold": 25}, "turns": 8}, {"gold": 150},
     "Loan request: 150 gold now, 25 gold a turn for 8 turns."),
    ("O5", "quarry", {"gold": 150}, {"stone": 60}, "Buying 60 stone at 2.5 gold each."),
    ("O6", "vault", {"gold": 110}, {"food": 100}, "Buying 100 food at 1.1 gold each."),
)

CAPITALS = {"miller": (4, 4), "vault": (17, 4), "drifter": (4, 17), "quarry": (17, 17), SOLVER: (11, 11)}
HOME = [            # around the solver's capital, top-left (10, 10)
    "f.h",
    ".C.",
    "g.f",
]


class ContractValuation(Puzzle):
    id = "contracts"
    title = "Contract valuation"
    seed = 3
    start_turn = 2
    horizon = 3             # turns 2..4: the offers' lifetime
    roles = (("miller", "Miller"), ("vault", "Vault"), ("drifter", "Drifter"), ("quarry", "Quarry"),
             (SOLVER, "Treasurer"))
    objective = ("Six trade offers to you are open until the end of turn 4 (deals.open). Accept the set of them "
                 "that leaves you best off, as valued below. Nothing else you do is scored.")
    scoring = ("V = gold + bank + food + 1.5*wood + 2*stone + 35*infantry + 2*(influence - influence_debt). "
               f"V(S) = your V after {WINDOW} turns (the end of turn {start_turn + WINDOW - 1}) when the offers in "
               "S are accepted on turn 2 in the order you accepted them (one that cannot be delivered then is "
               "skipped), you do nothing else and the other players follow their fixed scripts. "
               "score = round(100 * (V(S) - V(none)) / (V(best) - V(none))), clamped to 0..100, where S is the "
               "set of these offers you accepted and best is the subset with the highest V.")

    _cache: dict = {}
    _lock = threading.Lock()

    # ------------------------------------------------------------ position
    def build(self, g, pids) -> None:
        for role, (x, y) in CAPITALS.items():
            paint(g, x - 1, y - 1, ["...", "...", "..."])
        paint(g, 10, 10, HOME)
        for role, (x, y) in CAPITALS.items():
            g.add_city(x, y, pids[role], capital=True)
            g.player(pids[role]).capital = g.idx(x, y)
        me = pids[SOLVER]
        g.place_units(*CAPITALS[SOLVER], me, {"infantry": 20})
        set_player(g, me, food=220, wood=60, stone=70, gold=160, influence=10, bank=200)
        set_player(g, pids["miller"], food=150, wood=140, stone=60, gold=300, influence=10)
        set_player(g, pids["vault"], food=100, wood=100, stone=100, gold=400, influence=40, bank=500,
                   contracts_honoured=5, deals=5)
        set_player(g, pids["drifter"], food=60, wood=40, stone=20, gold=10, influence=0, defaults=2,
                   influence_debt=40, deals=3)
        set_player(g, pids["quarry"], food=120, wood=80, stone=150, gold=300, influence=10)
        # contracts from earlier turns (made by hand: they belong to turn 0)
        _old_contract(g, payer=me, payee=pids["quarry"], per_turn={"stone": 20}, turns=4,
                      back={"gold": 150}, turn=0)
        _old_contract(g, payer=pids["drifter"], payee=pids["quarry"], per_turn={"gold": 30}, turns=8,
                      back={"gold": 200}, turn=1)
        # the offers
        for label, role, give, get, msg in OFFERS:
            res = g.diplomacy(pids[role], [{"type": "propose", "to": me, "give": give, "get": get,
                                            "message": msg, "expires_in": self.horizon - 1}])
            if not res[0].get("ok"):  # pragma: no cover - a broken position is a bug
                raise RuntimeError(f"offer {label} could not be made: {res}")
            g.puzzle_state.setdefault("offers", {})[res[0]["deal"]] = label
        g._events = []   # the proposals are in deals.open; they are not this turn's news

    def opponent(self, role: str) -> Bot:
        return ScriptedBot()   # the traders only hold their offers open and pay what they owe

    # ------------------------------------------------------------ scoring
    def accepted(self, g: PuzzleGame) -> list[str]:
        """Labels of the puzzle's offers that executed, in execution order."""
        offers = g.puzzle_state.get("offers", {})
        return [offers[e["id"]] for e in g.deal_log if e["id"] in offers]

    def value(self, labels: tuple) -> int:
        """V of accepting ``labels`` (in that order) on the first turn."""
        key = tuple(labels)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        g = PuzzleGame(self.id, game_id="valuation")
        for _, name in self.roles:
            g.add_player(name)
        g.start()
        g.max_turns = self.start_turn + WINDOW
        me = self.solver_pid()
        ids = {v: k for k, v in g.puzzle_state["offers"].items()}
        for lab in key:
            g.diplomacy(me, [{"type": "accept", "deal": ids[lab]}])
        while g.status == "running":
            Game.step(g)   # the engine's step: a valuation run is not scored itself
        v = valuation(g, me)
        with self._lock:
            self._cache[key] = v
        return v

    def best(self) -> tuple[tuple, int]:
        labels = [o[0] for o in OFFERS]
        best = ((), self.value(()))
        for k in range(1, len(labels) + 1):
            for combo in itertools.combinations(labels, k):
                v = self.value(combo)
                if v > best[1]:
                    best = (combo, v)
        return best

    def score(self, g) -> tuple[int, str]:
        got = tuple(self.accepted(g))
        v0 = self.value(())
        v = self.value(got)
        best = BEST
        vb = self.value(best)
        score = 100 * (v - v0) / (vb - v0)
        s = max(0, min(100, round(score)))
        ids = {lab: did for did, lab in g.puzzle_state.get("offers", {}).items()}

        def names(labels):
            return " + ".join(ids.get(lab, lab) for lab in labels) or "nothing"
        why = (f"accepted {names(got)}: V = {v}, against {v0} for accepting nothing and {vb} for the best set "
               f"({names(best)}) -> 100 x ({v} - {v0}) / ({vb} - {v0}) = {round(score)}"
               + (f", clamped to {s}" if s != round(score) else ""))
        return s, why

    def solution(self) -> Bot:
        return ContractSolution()


def valuation(g, pid: str) -> int:
    p = g.player(pid)
    r = p.resources
    units = g.units_of(pid)["infantry"] if p.alive else 0
    v = (r["gold"] + p.bank + r["food"] + 1.5 * r["wood"] + 2 * r["stone"] + UNIT_VALUE * units
         + INFLUENCE_VALUE * (r["influence"] - p.influence_debt))
    return int(round(v))


def _old_contract(g, payer: str, payee: str, per_turn: dict, turns: int, back: dict, turn: int) -> None:
    """An executed deal of an earlier turn and the contract it created
    (``payer`` received ``back`` and pays ``per_turn`` for ``turns`` more turns)."""
    g._deal_counter += 1
    did = f"d{g._deal_counter}"
    give = {"per_turn": dict(per_turn), "turns": turns}
    d = {"id": did, "thread": did, "from": payer, "to": payee, "give": give, "get": dict(back),
         "peace": None, "message": None, "turn": turn, "expires_turn": turn + 2, "status": "accepted",
         "reason": None, "closed_turn": turn}
    g.deals[did] = d
    for pid in (payer, payee):
        g._recent_deals.setdefault(pid, []).append(d)
        g.player(pid).deals += 1
    g._recent_all.append(d)
    g.deal_log.append({"id": did, "turn": turn, "from": payer, "to": payee, "give": D.jcopy(give),
                       "get": dict(back), "peace": None})
    g._contract_counter += 1
    g.contracts.append({"id": f"c{g._contract_counter}", "payer": payer, "payee": payee,
                        "per_turn": dict(per_turn), "turns_left": turns, "deal": did})


class ContractSolution(Bot):
    """Reference solution: accept O1 (sell 100 food for 120 gold) and O2
    (lend 100 gold for 15 a turn over 8 turns) on the first turn."""

    name = "contracts-solution"
    ACCEPT = [({"gold": 120}, {"food": 100}),
              ({"per_turn": {"gold": 15}, "turns": 8}, {"gold": 100})]

    def negotiate(self, view: dict) -> list:
        return [{"type": "accept", "deal": d["id"]} for d in view["deals"]["open"]
                if d["to"] == view["you"]["id"] and (d["give"], d["get"]) in self.ACCEPT]

    def act(self, view: dict) -> list:
        return []
