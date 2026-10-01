"""Diagnostic positions ("puzzles"): short saved-position challenges with an
objective score (docs/PUZZLES.md).

A full game's placement is a noisy, opponent-dependent signal of how well
an agent reasons. A puzzle is a small hand-made position, a fixed horizon of
a handful of turns, deterministic opponents and a 0-100 score with a
one-line explanation, so the same play always gets the same score.

* ``PUZZLES`` — id -> :class:`~.base.Puzzle` instance.
* :func:`new_game` — a started :class:`~.base.PuzzleGame` (offline runs).
* :func:`run_puzzle` (in :mod:`.runner`) — play one solver bot through a
  puzzle offline; ``python -m agentciv.puzzles list|run`` is the CLI.
* On the server, ``POST /api/games {"puzzle": id}`` creates an unrated game
  of the puzzle that starts as soon as its one remote seat is taken.
"""
from __future__ import annotations

from .base import SOLVER, Puzzle, PuzzleGame
from .contracts import ContractValuation
from .market import MarketConstruction
from .victory import StopVictory
from .winter import WinterPlanning

PUZZLES: dict[str, Puzzle] = {p.id: p for p in (WinterPlanning(), MarketConstruction(),
                                                 ContractValuation(), StopVictory())}

BOT_PREFIX = "puzzle:"


def get_puzzle(puzzle_id: str) -> Puzzle:
    try:
        return PUZZLES[puzzle_id]
    except (KeyError, TypeError):
        raise ValueError(f"unknown puzzle {puzzle_id!r}; choose from {list(PUZZLES)}") from None


def house_seats(puzzle_id: str) -> list[tuple[str, str, object]]:
    """``(player name, bot name, bot)`` for every house seat, in seat order
    (the solver's seat, always last, is not included)."""
    pz = get_puzzle(puzzle_id)
    return [(name, f"{BOT_PREFIX}{pz.id}:{role}", pz.opponent(role))
            for role, name in pz.roles if role != SOLVER]


def make_bot(bot_name: str):
    """Recreate a house seat's bot from its bot name (``puzzle:<id>:<role>``)."""
    _, pid, role = bot_name.split(":", 2)
    pz = get_puzzle(pid)
    if role not in dict(pz.roles) or role == SOLVER:
        raise ValueError(f"puzzle {pid} has no house seat {role!r}")
    return pz.opponent(role)


def new_game(puzzle_id: str, solver_name: str | None = None, game_id: str = "puzzle") -> PuzzleGame:
    """A started puzzle game, seats added in the server's order."""
    pz = get_puzzle(puzzle_id)
    g = PuzzleGame(pz.id, game_id=game_id)
    for role, name in pz.roles:
        g.add_player(solver_name or name if role == SOLVER else name)
    g.start()
    return g


__all__ = ["PUZZLES", "SOLVER", "Puzzle", "PuzzleGame", "get_puzzle", "house_seats", "make_bot", "new_game"]
