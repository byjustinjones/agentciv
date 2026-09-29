"""Bot interface shared by house bots (in-process), the tournament runner
and remote SDK bots.

A bot receives the player view dict (docs/DESIGN.md §10) and returns a list
of order dicts (§9) from ``act``; before that it gets a few negotiation
rounds per turn (``negotiate``, §13) returning diplomacy actions. Bots must be deterministic given (seed, view) and must
not do I/O.
"""
from __future__ import annotations


class Bot:
    name = "base"

    def __init__(self, seed: int = 0):
        self.seed = seed

    def act(self, view: dict) -> list[dict]:
        raise NotImplementedError

    def negotiate(self, view: dict) -> list[dict]:
        """Diplomacy actions (§13.2: propose/counter/accept/reject/withdraw/
        say) for one negotiation round. The tournament runner and the server
        call it several times per turn before :meth:`act`, each time with a
        fresh view; the actions take effect immediately. Default: none."""
        return []


class IdleBot(Bot):
    """Submits nothing. Useful as a baseline and for tests."""

    name = "idle"

    def act(self, view: dict) -> list[dict]:
        return []
