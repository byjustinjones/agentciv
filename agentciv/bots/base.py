"""Bot interface shared by house bots (in-process), the tournament runner
and remote SDK bots.

A bot receives the player view dict (docs/DESIGN.md §10) and returns a list
of order dicts (§9). Bots must be deterministic given (seed, view) and must
not do I/O.
"""
from __future__ import annotations


class Bot:
    name = "base"

    def __init__(self, seed: int = 0):
        self.seed = seed

    def act(self, view: dict) -> list[dict]:
        raise NotImplementedError


class IdleBot(Bot):
    """Submits nothing. Useful as a baseline and for tests."""

    name = "idle"

    def act(self, view: dict) -> list[dict]:
        return []
