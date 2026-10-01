"""Fog of war and espionage for games created with ``fog: true`` (docs/RULES.md §14).

Everything here is a pure function of the game state: no random draws, no
``hash()``. The engine calls:

* :func:`vision_all` and :func:`record_sightings` at the start of
  ``Game.step`` (sight at the start of the turn),
* :func:`after_step` at the end of ``Game.step`` (scopes the turn's events by
  the sight before and after the turn, turns queued spy missions into
  reports, prunes old memory).

``views.build_view`` calls :func:`vision`, :func:`redact_row`,
:func:`redact_event` and :func:`sightings_view` for player views and the
token-less spectator view of a *running* fog game. Omniscient views (the
full spectator view, recorded full frames, anything after the game ended)
never go through this module.

Visibility of events while the fog is active is stored under the key
``_fog`` (a list of player ids), separately from the engine's ``_vis`` (which
marks genuinely private events such as ``order_failed``), so that when the
game ends every view shows the last turn's events as in a standard game.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import constants as C

if TYPE_CHECKING:  # pragma: no cover
    from .game import Game

# How every event type is shown to players while the fog is active.
#   emitted  visibility as emitted by the engine (party-only ``_vis``, or a public ``say``)
#   public   everyone (the fields in REDACT are stripped for non-parties)
#   player   only ev["player"]
#   local    the players involved, plus every player that had the event's
#            tile(s) in sight at the start or at the end of the turn
EVENT_POLICY = {
    "order_failed": "emitted",
    "treaty_proposed": "emitted",
    "deal_proposed": "emitted",
    "deal_countered": "emitted",
    "deal_rejected": "emitted",
    "deal_withdrawn": "emitted",
    "deal_failed": "emitted",
    "deal_expired": "emitted",
    "contract_paid": "emitted",
    "contract_completed": "emitted",
    "say": "emitted",
    "spy_report": "emitted",
    "spy_detected": "emitted",
    "counterintel": "emitted",
    "bank": "emitted",
    "treaty_broken": "public",
    "treaty_signed": "public",
    "treaty_expired": "public",
    "treaty_released": "public",
    "contract_cancelled": "emitted",
    "eliminated": "public",
    "victory": "public",
    "build": "public",
    "wonder_stage": "public",
    "claim": "public",
    "city_founded": "public",
    "tile_captured": "public",
    "city_captured": "public",
    "deal_executed": "public",
    "contract_default": "public",
    "spy_incident": "public",
    "streak_started": "public",
    "streak_ended": "public",
    "streak_paused": "public",
    "market": "player",
    "starvation": "player",
    "recruit": "local",
    "disband": "local",
    "battle": "local",
}

# event type -> (fields naming the parties, fields stripped for everyone else)
REDACT = {
    "city_captured": (("from", "to"), ("plunder",)),
    "deal_executed": (("from", "to"), ("give", "get", "contracts")),
    "contract_default": (("payer", "payee"), ("per_turn", "penalty", "debt", "seized")),
    "treaty_broken": (("by", "with"), ("refund", "paid", "removed", "bank_fee", "debt", "cancelled")),
}

_HIDDEN_KEYS = ("_vis", "_fog")


# ==========================================================================
# vision
# ==========================================================================
def _vision_sets(g: "Game", pids) -> dict:
    sets = {pid: set() for pid in pids}
    if not sets or not g.width:
        return {pid: frozenset() for pid in sets}
    radius = g.radius
    for i, o in enumerate(g.owner):
        if o in sets:
            sets[o].update(radius(i, C.FOG_VISION_TERRITORY))
    for c in g.cities.values():
        if c.owner in sets:
            sets[c.owner].update(radius(c.idx, C.FOG_VISION_CITY))
    for i, per in g.armies.items():
        for q, u in per.items():
            if q in sets and any(u.values()):
                r = C.FOG_VISION_CAVALRY if u.get("cavalry") else C.FOG_VISION_UNITS
                sets[q].update(radius(i, r))
    return {pid: frozenset(s) for pid, s in sets.items()}


def vision(g: "Game", pid: str | None) -> frozenset:
    """Tiles ``pid`` sees now (empty for the spectator and eliminated players)."""
    p = g.player(pid)
    if p is None or not p.alive or g.status == "lobby":
        return frozenset()
    return _vision_sets(g, [pid])[pid]


def vision_all(g: "Game") -> dict:
    """``{pid: frozenset(tiles)}`` for every living player."""
    return _vision_sets(g, [p.id for p in g.players if p.alive])


def ci_rating(g: "Game", pid: str) -> int:
    """Counter-intelligence rating: CI_BASE + CI_PER_CITY * cities + pool."""
    p = g.player(pid)
    cities = sum(1 for c in g.cities.values() if c.owner == pid)
    return C.CI_BASE + C.CI_PER_CITY * cities + (p.ci_pool if p is not None else 0)


# ==========================================================================
# memory of rival armies
# ==========================================================================
def record_sightings(g: "Game", pre: dict) -> None:
    """Remember the rival stacks every player sees at the start of the turn;
    forget remembered stacks on tiles now in sight without rival units.
    Memory: ``g.sightings[pid][tile] = {owner: {"turn", "units"}}`` (each
    owner's stack keeps its own turn stamp)."""
    for pid, sight in pre.items():
        mem = g.sightings.setdefault(pid, {})
        for i in sight:
            per = g.armies.get(i)
            rivals = {q: {"turn": g.turn, "units": dict(u)} for q, u in per.items()
                      if q != pid and any(u.values())} if per else None
            if rivals:
                mem[i] = rivals
            else:
                mem.pop(i, None)


def _remember_report(g: "Game", spy: str, target: str, armies: list, turn: int) -> None:
    """A military report replaces what ``spy`` remembers of ``target``'s
    stacks (older memories only); other owners' stacks are kept."""
    mem = g.sightings.setdefault(spy, {})
    for i in list(mem):
        e = mem[i]
        if target in e and e[target]["turn"] <= turn:
            del e[target]
            if not e:
                del mem[i]
    for a in armies:
        e = mem.setdefault(g.idx(a["x"], a["y"]), {})
        if target not in e or e[target]["turn"] <= turn:
            e[target] = {"turn": turn, "units": dict(a["units"])}


def sightings_view(g: "Game", viewer: str, sight: frozenset) -> list:
    """Remembered rival stacks on tiles not in sight now: ``[{x, y, owner, units, turn}]``."""
    w = g.width or 1
    out = []
    for i, e in sorted(g.sightings.get(viewer, {}).items()):
        if i in sight:
            continue
        for q in sorted(e):
            out.append({"x": i % w, "y": i // w, "owner": q, "units": dict(e[q]["units"]), "turn": e[q]["turn"]})
    return out


def intel_view(g: "Game", viewer: str) -> list:
    return [_copy(r) for r in g.intel_reports.get(viewer, ())]


def _copy(v):
    if isinstance(v, dict):
        return {k: _copy(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_copy(x) for x in v]
    return v


# ==========================================================================
# espionage reports
# ==========================================================================
def snapshot(g: "Game", target: str, mission: str) -> dict:
    """The part of ``target``'s state a report of ``mission`` shows (as in
    the target's own view)."""
    p = g.player(target)
    st = g.stats()[target]
    if mission == "military":
        w = g.width or 1
        armies = [{"x": i % w, "y": i // w, "units": dict(g.armies[i][target])}
                  for i in sorted(g.armies) if target in g.armies[i]]
        return {"armies": armies, "units": dict(st["units"]), "military_power": st["military_power"],
                "upkeep": st["upkeep"]}
    return {"resources": dict(p.resources), "income": dict(st["income"]), "score": st["score"],
            "victory_progress": dict(st["victory_progress"])}


# ==========================================================================
# events
# ==========================================================================
def scope_events(g: "Game", events: list, pre: dict, post: dict) -> None:
    """Set ``_fog`` on this turn's events per EVENT_POLICY (events that
    already carry ``_vis`` or ``_fog`` are left alone)."""
    w = g.width or 1

    def seers(tiles) -> set:
        out = set()
        for sets in (pre, post):
            for q, sight in sets.items():
                if any(t in sight for t in tiles):
                    out.add(q)
        return out

    for ev in events:
        if "_vis" in ev or "_fog" in ev:
            continue
        pol = EVENT_POLICY.get(ev["type"])
        if pol in ("public", "emitted"):
            continue
        if pol == "local" and "x" in ev and "y" in ev:
            tiles = [ev["y"] * w + ev["x"]]
            to = ev.get("to")
            if isinstance(to, (list, tuple)) and len(to) == 2:
                tiles.append(to[1] * w + to[0])
            who = set(ev.get("sides") or ()) | ({ev["player"]} if ev.get("player") else set())
            ev["_fog"] = sorted(who | seers(tiles))
        else:  # "player", and defensively anything unlisted
            ev["_fog"] = [ev["player"]] if ev.get("player") else []


def event_visible(ev: dict, viewer: str | None) -> bool:
    """Visibility under the fog (``_fog`` only; ``_vis`` is checked by the caller)."""
    vis = ev.get("_fog")
    return vis is None or viewer in vis


def redact_event(ev: dict, viewer: str | None) -> dict:
    """Copy of ``ev`` without the hidden keys, and without the REDACT fields
    unless ``viewer`` is a party."""
    out = {k: v for k, v in ev.items() if k not in _HIDDEN_KEYS}
    spec = REDACT.get(ev.get("type"))
    if spec is not None and (viewer is None or viewer not in [ev.get(f) for f in spec[0]]):
        for f in spec[1]:
            out.pop(f, None)
    return out


def redact_row(row: dict) -> dict:
    """Hide another player's stockpiles, army and exact score in a
    ``players[]`` row (in place). Bank, legacy, streaks and victory progress
    stay exact."""
    for k in C.FOG_HIDDEN_FIELDS:
        row[k] = None
    row["fogged"] = True
    return row


# ==========================================================================
# end of turn
# ==========================================================================
def after_step(g: "Game", pre: dict) -> None:
    """Called by ``Game.step`` after bookkeeping (``g.turn`` is already the
    next turn): scope the events, write queued spy reports, prune memory."""
    post = vision_all(g)
    scope_events(g, g._events, pre, post)
    t = g.turn
    for spy, target, mission, outcome in g._pending_intel:
        data = snapshot(g, target, mission)
        g.intel_reports.setdefault(spy, []).append(
            {"target": target, "mission": mission, "outcome": outcome, "as_of_turn": t, "data": data})
        if mission == "military":
            _remember_report(g, spy, target, data["armies"], t)
    g._pending_intel = []
    for pid, mem in g.sightings.items():
        for i in list(mem):
            e = mem[i]
            for q in [q for q, s in e.items() if t - s["turn"] > C.FOG_SIGHTING_TURNS]:
                del e[q]
            if not e:
                del mem[i]
    for pid, reps in g.intel_reports.items():
        reps[:] = [r for r in reps if t - r["as_of_turn"] <= C.SPY_REPORT_TURNS]
