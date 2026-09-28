"""Deterministic combat math (docs/DESIGN.md §7).

A *side* is one player's force in one battle. :func:`resolve` runs the
multi-side procedure on one tile (or one border clash): the weakest side fights
the weakest side hostile to it, the winner re-enters the queue, until no
hostile pair remains. Each duel uses the Lanchester square law for the
winner's losses.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from . import constants as C

_EPS = 1e-9


@dataclass
class Side:
    pid: str
    units: dict                      # unit type -> count (> 0)
    defender: bool = False           # started the turn on the tile, or owns the city
    city_owner: bool = False         # owns the city on this tile (garrison, walls, archer bonus)
    garrison: float = 0.0            # intrinsic garrison strength (city owner only)
    terrain_bonus: bool = False      # started on forest/hills and still there
    order: tuple = ()                # deterministic tie-break key
    payload: object = None           # caller data (e.g. move groups)
    defeated: bool = False           # lost a duel (for garrisons: city can be captured)
    alive: bool = field(default=True)

    def count(self) -> int:
        return sum(self.units.values())


def raw_power(side: Side) -> float:
    """Unmodified power used for ordering the battle queue."""
    return sum(c * C.UNITS[u]["strength"] for u, c in side.units.items()) + side.garrison


def military_power(units: dict) -> int:
    return sum(c * C.UNITS[u]["strength"] for u, c in units.items())


def counter_multiplier(unit: str, enemy_units: dict) -> float:
    """Enemy-count-weighted average of the counter multiplier for ``unit``."""
    total = sum(enemy_units.values())
    target = C.COUNTERS.get(unit)
    if total <= 0 or target is None:
        return 1.0
    k = enemy_units.get(target, 0)
    return (k * C.COUNTER_MULTIPLIER + (total - k)) / total


def wall_multiplier(walls: int, enemy_siege: int) -> float:
    return 1.0 + C.WALL_BONUS_PER_LEVEL * max(0.0, walls - enemy_siege / C.SIEGE_PER_WALL_LEVEL)


def power(side: Side, enemy: Side, walls: int = 0) -> float:
    """Combat power of ``side`` against ``enemy`` (§7)."""
    p = 0.0
    for u, c in side.units.items():
        if c <= 0:
            continue
        s = C.UNITS[u]["strength"]
        if side.city_owner and u == "archer":
            s *= C.ARCHER_CITY_DEFENSE
        if enemy.city_owner and u == "siege":
            s *= C.SIEGE_CITY_ATTACK
        p += c * s * counter_multiplier(u, enemy.units)
    p += side.garrison
    if side.terrain_bonus:
        p *= C.TERRAIN_DEFENSE_BONUS
    if side.city_owner:
        p *= wall_multiplier(walls, enemy.units.get("siege", 0))
    return p


def lanchester_losses(units: dict, p_loser: float, p_winner: float) -> dict:
    """Losses of the winning side: round(count * (1 - sqrt(1 - (Pl/Pw)^2)))."""
    ratio = 1.0 if p_winner <= 0 else min(1.0, p_loser / p_winner)
    frac = 1.0 - math.sqrt(max(0.0, 1.0 - ratio * ratio))
    out = {}
    for u, c in units.items():
        lost = min(c, int(math.floor(c * frac + 0.5)))
        if lost > 0:
            out[u] = lost
    return out


def duel(a: Side, b: Side, walls: int = 0) -> dict:
    """Fight two sides; mutates their units. Returns a battle record."""
    pa = round(power(a, b, walls), 6)
    pb = round(power(b, a, walls), 6)
    if abs(pa - pb) <= _EPS:
        if a.defender != b.defender:
            winner = a if a.defender else b
        else:
            winner = None
    else:
        winner = a if pa > pb else b
    losses = {a.pid: {}, b.pid: {}} if a.pid != b.pid else {a.pid: {}}
    if winner is None:
        for s in (a, b):
            losses[s.pid] = _merge(losses.get(s.pid, {}), dict(s.units))
            s.units = {}
            s.defeated = True
    else:
        loser = b if winner is a else a
        pw, pl = (pa, pb) if winner is a else (pb, pa)
        wl = lanchester_losses(winner.units, pl, pw)
        losses[loser.pid] = _merge(losses.get(loser.pid, {}), dict(loser.units))
        losses[winner.pid] = _merge(losses.get(winner.pid, {}), wl)
        loser.units = {}
        loser.defeated = True
        for u, c in wl.items():
            winner.units[u] -= c
        winner.units = {u: c for u, c in winner.units.items() if c > 0}
    return {
        "sides": [a.pid, b.pid],
        "powers": {a.pid: pa, b.pid: pb},
        "winner": winner.pid if winner is not None else None,
        "losses": losses,
    }


def _merge(d: dict, add: dict) -> dict:
    out = dict(d)
    for k, v in add.items():
        out[k] = out.get(k, 0) + v
    return out


def _still_present(s: Side) -> bool:
    return not s.defeated and (s.count() > 0 or s.garrison > 0)


def resolve(sides: list, hostile: Callable[[str, str], bool], walls: int = 0) -> list:
    """Run the multi-side battle procedure. Mutates ``sides`` (units,
    ``defeated``, ``alive``) and returns the list of duel records."""
    records = []
    active = [s for s in sides if _still_present(s)]
    guard = 0
    while guard < 1000:
        guard += 1
        queue = sorted(active, key=lambda s: (round(raw_power(s), 6), s.order))
        pair = None
        for s in queue:
            for o in queue:
                if o is not s and hostile(s.pid, o.pid):
                    pair = (s, o)
                    break
            if pair:
                break
        if pair is None:
            break
        rec = duel(pair[0], pair[1], walls)
        records.append(rec)
        active = [s for s in active if _still_present(s)]
    for s in sides:
        s.alive = _still_present(s)
    return records
