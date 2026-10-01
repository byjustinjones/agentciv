"""Offline puzzle runs: one solver bot in the puzzle seat, the puzzle's own
opponents in the others, resolved with the engine directly.

Per turn, in the order the server effectively applies them:

1. the solver's ``negotiate`` (``NEGOTIATION_ROUNDS`` rounds, if it has one),
   each round on a fresh view, actions applied at once;
2. every opponent's ``act`` on a fresh view (after the solver's diplomacy:
   on the server an opponent party to a deal that executes mid-turn
   recomputes its orders, which gives the same result);
3. the solver's ``act``; then the turn resolves.
"""
from __future__ import annotations

from ..bots.base import Bot
from . import SOLVER, get_puzzle, new_game

NEGOTIATION_ROUNDS = 3   # as the server's house-bot rounds


def _negotiates(bot) -> bool:
    fn = getattr(type(bot), "negotiate", None)
    return callable(fn) and fn is not Bot.negotiate


def run_puzzle(puzzle_id: str, solver, solver_name: str | None = None) -> dict:
    """Play ``solver`` (a :class:`~agentciv.bots.base.Bot` or any object with
    ``act(view)``) through the puzzle. Returns ``{"puzzle", "score",
    "explanation", "turns", "result", "game"}`` (``game``: the finished
    :class:`~.base.PuzzleGame`)."""
    pz = get_puzzle(puzzle_id)
    g = new_game(pz.id, solver_name=solver_name)
    pids = pz.pids()
    me = pids[SOLVER]
    opponents = {pids[role]: pz.opponent(role) for role, _ in pz.roles if role != SOLVER}
    talks = _negotiates(solver)
    while g.status == "running":
        if talks and g.player(me).alive:
            for _ in range(NEGOTIATION_ROUNDS):
                actions = solver.negotiate(g.player_view(me))
                if actions:
                    g.diplomacy(me, actions)
        for pid, bot in opponents.items():
            if g.player(pid).alive:
                g.submit_orders(pid, bot.act(g.player_view(pid)) or [])
        if g.player(me).alive:
            g.submit_orders(me, solver.act(g.player_view(me)) or [])
        g.step()
    oc = g.puzzle_outcome or {"score": 0, "explanation": "the game ended without a score"}
    return {"puzzle": pz.id, "score": oc["score"], "explanation": oc["explanation"],
            "turns": (g.result or {}).get("turn", g.turn) - pz.start_turn + 1,
            "result": g.result, "game": g}
