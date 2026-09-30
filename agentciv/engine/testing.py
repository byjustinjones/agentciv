"""Helpers for building hand-made game situations (used by the tests; handy
for bot authors too). Nothing here is used by the engine itself."""
from __future__ import annotations

from . import constants as C
from .game import Game, GameConfig


def new_game(n: int = 2, seed: int = 1, max_turns: int = C.DEFAULT_MAX_TURNS, start: bool = True,
             fog: bool = False) -> Game:
    """A normal game with ``n`` players named P1..Pn (``fog``: a fog-of-war game)."""
    g = Game(GameConfig(seed=seed, max_turns=max_turns, game_id=f"test{seed}", fog=fog))
    for i in range(n):
        g.add_player(f"P{i + 1}")
    if start:
        g.start()
    return g


def sandbox(n: int = 2, seed: int = 1, max_turns: int = C.DEFAULT_MAX_TURNS, terrain: str = ".",
            fog: bool = False) -> Game:
    """A started game whose map is wiped: uniform ``terrain``, no owners,
    cities or armies (relic positions are kept). Add what you need with
    :meth:`Game.add_city`, :meth:`Game.place_units`, :meth:`Game.set_owner`
    and :func:`set_terrain`. Players without a city are eliminated at the end
    of the next step, so give every player a city."""
    g = new_game(n, seed, max_turns, fog=fog)
    size = g.width * g.height
    g.terrain = [terrain] * size
    g.owner = [None] * size
    g.improvement = [None] * size
    g.deposits = [C.DEPOSITS[terrain][1] if terrain in C.DEPOSITS else 0] * size
    for i in g.relics:
        g.terrain[i] = "."
        g.deposits[i] = 0
    g.cities.clear()
    g.armies.clear()
    for p in g.players:
        p.tiles = 0
        p.capital = None
        p.city_counter = 0
    g._invalidate()
    return g


def set_terrain(g: Game, x: int, y: int, ch: str) -> None:
    i = g.idx(x, y)
    g.terrain[i] = ch
    g.deposits[i] = C.DEPOSITS[ch][1] if ch in C.DEPOSITS else 0
    g._invalidate()


def run_turn(g: Game, orders: dict | None = None, check: bool = True) -> list:
    """Submit ``{pid: [orders]}`` and step. With ``check`` the submission must
    produce no pre-validation errors. Returns the step's events."""
    for pid, lst in (orders or {}).items():
        errs = g.submit_orders(pid, lst)
        if check and errs:
            raise AssertionError(f"order errors for {pid}: {errs}")
    return g.step()


def events_of(events: list, etype: str) -> list:
    return [e for e in events if e["type"] == etype]
