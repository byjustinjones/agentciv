"""Deterministic combat math (docs/DESIGN.md §7).

A *side* is one player's force in one battle. :func:`resolve` runs the
multi-side procedure on one tile (or one border clash): the weakest side fights
the weakest side hostile to it, the winner re-enters the queue, until no
hostile pair remains. Each duel uses the Lanchester square law for the
winner's losses.

Two experimental switches (``agentciv.engine.variants``, off by default and
not part of the served rules): ``pool`` fights allied sides as one
coalition, ``symmetric`` resolves equal-power free-for-alls at once. With
both off, :func:`resolve` is exactly the procedure above.
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
    members: list | None = None      # a coalition (variant ``pool_allies``): the sides pooled in it

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
    """Combat power of ``side`` against ``enemy`` (§7). A coalition's power
    is the sum of its members' powers against ``enemy``."""
    if side.members:
        return sum(power(m, enemy, walls) for m in side.members)
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


def _coalition_duel(a: Side, b: Side, walls: int = 0) -> dict:
    """:func:`duel` where either side may be a coalition: the coalitions'
    summed powers decide, a losing coalition loses every member's units, and
    a winning one takes the Lanchester losses of its pooled units, shared
    out over its members (:func:`share_losses`). The record lists every
    member in ``sides``, ``powers`` and ``losses``; ``winner`` is the
    winning coalition's lead (first member) and ``coalitions`` lists the
    members of each coalition."""
    pa = round(power(a, b, walls), 6)
    pb = round(power(b, a, walls), 6)
    if abs(pa - pb) <= _EPS:
        winner = (a if a.defender else b) if a.defender != b.defender else None
    else:
        winner = a if pa > pb else b
    losses: dict = {}
    powers: dict = {}
    for s, enemy, p in ((a, b, pa), (b, a, pb)):
        for m in (s.members or [s]):
            losses.setdefault(m.pid, {})
            powers[m.pid] = round(powers.get(m.pid, 0.0) + (power(m, enemy, walls) if s.members else p), 6)
    for s in (a, b):
        if s is winner:
            continue
        for m in (s.members or [s]):
            losses[m.pid] = _merge(losses[m.pid], dict(m.units))
            m.units = {}
            m.defeated = True
        s.units = {}
        s.defeated = True
    if winner is not None:
        pw, pl = (pa, pb) if winner is a else (pb, pa)
        wl = lanchester_losses(winner.units, pl, pw)
        if winner.members:
            for m, ml in zip(winner.members, share_losses(winner.members, wl)):
                losses[m.pid] = _merge(losses[m.pid], ml)
                m.units = {u: c - ml.get(u, 0) for u, c in m.units.items() if c - ml.get(u, 0) > 0}
            winner.units = _pooled(winner.members)
        else:
            losses[winner.pid] = _merge(losses[winner.pid], wl)
            winner.units = {u: c - wl.get(u, 0) for u, c in winner.units.items() if c - wl.get(u, 0) > 0}
    rec = {
        "sides": [m.pid for s in (a, b) for m in (s.members or [s])],
        "powers": powers,
        "winner": (winner.members or [winner])[0].pid if winner is not None else None,
        "losses": losses,
    }
    if a.members or b.members:
        rec["coalitions"] = [[m.pid for m in s.members] for s in (a, b) if s.members]
    return rec


def share_losses(members: list, losses: dict) -> list:
    """Split a coalition's losses over its members, unit type by unit type,
    in proportion to the units each member brought: everyone loses the
    floor of its exact share, and the units left over go one each to the
    members with the largest remainders (ties: the member listed first, i.e.
    the earlier in the battle queue). Integer arithmetic, so the members
    together lose exactly ``losses``. Returns one loss dict per member."""
    out = [{} for _ in members]
    for u, lost in losses.items():
        have = [m.units.get(u, 0) for m in members]
        total = sum(have)
        if lost <= 0 or total <= 0:
            continue
        base = [lost * h // total for h in have]
        rest = lost - sum(base)
        order = sorted(range(len(members)), key=lambda k: (-(lost * have[k] % total), k))
        for k in order[:rest]:
            base[k] += 1
        for k, n in enumerate(base):
            if n > 0:
                out[k][u] = n
    return out


def _pooled(sides: list) -> dict:
    units: dict = {}
    for s in sides:
        for u, c in s.units.items():
            if c > 0:
                units[u] = units.get(u, 0) + c
    return units


def _merge(d: dict, add: dict) -> dict:
    out = dict(d)
    for k, v in add.items():
        out[k] = out.get(k, 0) + v
    return out


def _still_present(s: Side) -> bool:
    return not s.defeated and (s.count() > 0 or s.garrison > 0)


def resolve(sides: list, hostile: Callable[[str, str], bool], walls: int = 0,
            pool: bool = False, symmetric: bool = False) -> list:
    """Run the multi-side battle procedure. Mutates ``sides`` (units,
    ``defeated``, ``alive``) and returns the list of duel records.

    ``pool`` and ``symmetric`` are experimental variants (module doc):

    * ``pool``: with three or more sides present, the sides other than the
      city owner are grouped by the set of sides hostile to them on this
      tile. Sides with the same, non-empty hostile set are pairwise at peace
      (a side is never hostile to itself) and face exactly the same enemies,
      so they fight as one coalition: summed raw power for the queue, summed
      combat power (each member with its own terrain bonus and counters
      against the enemy's pooled units) in duels, a defender if any member
      is one, queued by its first member's tie-break key. A beaten coalition
      loses everything; a winning one shares the Lanchester losses of its
      pooled units (:func:`share_losses`). The city owner (garrison, walls,
      archer bonus) is never pooled. With two sides nothing changes.
    * ``symmetric``: when the weakest side that has an enemy is tied on raw
      power with other sides, and all the tied sides (three or more) are
      pairwise hostile, of the same defender status, not a city owner, and
      equal in combat power against each other in every pairing, they are
      destroyed together in one record (``simultaneous``) instead of the
      first two annihilating each other and the third keeping its units.
      Any other tie is resolved in queue order as usual.
    """
    if pool:
        present = [s for s in sides if _still_present(s)]
        if len(present) >= 3:
            return _resolve_pooled(sides, present, hostile, walls, symmetric)
    return _resolve(sides, [s for s in sides if _still_present(s)], hostile, walls, symmetric)


def _resolve(sides: list, active: list, hostile, walls: int, symmetric: bool,
             fight: Callable = duel) -> list:
    records = []
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
        tied = _symmetric_tie(queue, pair[0], hostile, walls) if symmetric else None
        if tied:
            records.append(_annihilate(tied, walls))
        else:
            records.append(fight(pair[0], pair[1], walls))
        active = [s for s in active if _still_present(s)]
    for s in sides:
        s.alive = _still_present(s)
    return records


def _symmetric_tie(queue: list, first: Side, hostile, walls: int) -> list | None:
    """The sides of a symmetric equal-power free-for-all led by ``first``
    (see :func:`resolve`), or None."""
    level = round(raw_power(first), 6)
    tied = [s for s in queue if round(raw_power(s), 6) == level]
    if len(tied) < 3:
        return None
    if any(s.city_owner or s.defender != first.defender for s in tied):
        return None
    for k, s in enumerate(tied):
        for o in tied[k + 1:]:
            if not hostile(s.pid, o.pid):
                return None
            if abs(round(power(s, o, walls), 6) - round(power(o, s, walls), 6)) > _EPS:
                return None
    return tied


def _annihilate(tied: list, walls: int) -> dict:
    losses: dict = {}
    for s in tied:
        for m in (s.members or [s]):
            losses[m.pid] = _merge(losses.get(m.pid, {}), dict(m.units))
            m.units = {}
            m.defeated = True
        s.units = {}
        s.defeated = True
    rec = {"sides": [m.pid for s in tied for m in (s.members or [s])],
           "powers": {m.pid: round(raw_power(m), 6) for s in tied for m in (s.members or [s])},
           "winner": None, "losses": losses, "simultaneous": True}
    if any(s.members for s in tied):
        rec["coalitions"] = [[m.pid for m in s.members] for s in tied if s.members]
    return rec


def _resolve_pooled(sides: list, present: list, hostile, walls: int, symmetric: bool) -> list:
    groups: dict = {}
    for s in present:
        enemies = frozenset(o.pid for o in present if o is not s and hostile(s.pid, o.pid))
        key = ("own", id(s)) if s.city_owner or not enemies else enemies
        groups.setdefault(key, []).append(s)
    if all(len(g) == 1 for g in groups.values()):
        return _resolve(sides, present, hostile, walls, symmetric)
    units = []
    for g in groups.values():
        if len(g) == 1:
            units.append(g[0])
            continue
        g.sort(key=lambda s: s.order)
        units.append(Side(pid=g[0].pid, units=_pooled(g), defender=any(s.defender for s in g),
                          order=g[0].order, members=g))
    records = _resolve(units, units, hostile, walls, symmetric, fight=_coalition_duel)
    for u in units:
        for m in (u.members or ()):
            m.defeated = m.defeated or u.defeated
    for s in sides:
        s.alive = _still_present(s)
    return records
