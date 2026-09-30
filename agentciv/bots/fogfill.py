"""Estimates for the fields a fog-of-war view hides (docs/RULES.md §14).

The house bots were written for full information. :func:`fill` gives them a
view in which every other player's row has estimated ``units``,
``military_power``, ``upkeep``, ``resources`` and ``score`` (marked
``"estimated": True``), built only from what the view itself contains:

* units: armies in sight plus remembered ``sightings``, or the latest
  military ``intel`` report if it shows more;
* resources: the latest treasury report; otherwise gold and influence from
  the banded victory progress and food/wood/stone from income;
* score: the score formula over the public terms and these estimates.

Remembered stacks are also appended to ``armies`` (with ``"stale": age``).
Views of standard games (and of finished fog games) are returned unchanged.
Stateless and deterministic.
"""
from __future__ import annotations

from ..engine import constants as C
from .common import raw_strength, upkeep_of


def _add(a: dict, b: dict) -> dict:
    out = dict(a)
    for u, c in (b or {}).items():
        out[u] = out.get(u, 0) + c
    return out


def _latest(reports: list, target: str, mission: str) -> dict | None:
    best = None
    for r in reports:
        if r.get("target") == target and r.get("mission") == mission and r.get("data"):
            if best is None or r.get("as_of_turn", 0) >= best.get("as_of_turn", 0):
                best = r
    return best


def _score(row: dict, res: dict, mp: int) -> int:
    sw, sd = C.SCORE_WEIGHTS, C.SCORE_DIVISORS
    return int(sw["tiles"] * (row.get("tiles") or 0) + sw["cities"] * (row.get("cities") or 0)
               + sw["capitals_held"] * (row.get("capitals_held") or 0)
               + sw["wonder_stage"] * (row.get("wonder_stage") or 0)
               + int(res.get("influence", 0)) // sd["influence"] + int(res.get("gold", 0)) // sd["gold"]
               + sw["relics_held"] * (row.get("relics_held") or 0) + mp // sd["military_power"])


def fill(view: dict) -> dict:
    """``view`` itself for a view without active fog; otherwise a shallow
    copy with estimates in the hidden fields of other players' rows."""
    if not isinstance(view, dict) or not (view.get("fog") or {}).get("active"):
        return view
    turn = int(view.get("turn", 0) or 0)
    reports = view.get("intel") or []
    seen: dict = {}
    for a in view.get("armies") or ():
        seen[a["owner"]] = _add(seen.get(a["owner"], {}), a.get("units"))
    stale = []
    for s in view.get("sightings") or ():
        seen[s["owner"]] = _add(seen.get(s["owner"], {}), s.get("units"))
        stale.append({"x": s["x"], "y": s["y"], "owner": s["owner"], "units": dict(s.get("units") or {}),
                      "stale": turn - int(s.get("turn", turn))})
    players = []
    for row in view.get("players") or ():
        if not row.get("fogged"):
            players.append(row)
            continue
        row = dict(row)
        q = row.get("id")
        units = {u: 0 for u in C.UNIT_TYPES}
        units = _add(units, seen.get(q, {}))
        mil = _latest(reports, q, "military")
        if mil is not None:
            rep = mil["data"].get("units") or {}
            units = {u: max(units.get(u, 0), rep.get(u, 0)) for u in C.UNIT_TYPES}
        mp = raw_strength(units)
        tre = _latest(reports, q, "treasury")
        if tre is not None and tre["data"].get("resources"):
            res = dict(tre["data"]["resources"])
        else:
            vp = row.get("victory_progress") or {}
            inc = row.get("income") or {}
            res = {r: min(C.STORAGE_BASE, 3 * max(0, int(inc.get(r, 0)))) for r in C.CAPPED_RESOURCES}
            res["gold"] = int(float(vp.get("economic", 0) or 0) * C.ECONOMIC_VICTORY_GOLD)
            res["influence"] = int(float(vp.get("influence", 0) or 0) * C.INFLUENCE_VICTORY)
            res = {r: res.get(r, 0) for r in C.RESOURCES}
        row.update(units=units, military_power=mp, upkeep=upkeep_of(units), resources=res,
                   score=_score(row, res, mp) if row.get("alive") else 0, estimated=True)
        players.append(row)
    out = dict(view)
    out["players"] = players
    out["armies"] = list(view.get("armies") or ()) + stale
    return out
