"""JSON views of the game state (docs/DESIGN.md §10).

``build_view(game, pid)`` returns the player view for ``pid`` or the
spectator view when ``pid`` is None. Everything returned is freshly built
plain JSON data (safe for callers to mutate).

The spectator view is served without authentication, so while the game is
not finished it is *public*: only public messages and events, no open or
closed deals (only the public log of executed deals and the contracts), no
trade offers or treaty proposals (anyone could otherwise drop their token and read
the other players' private diplomacy). ``full=True`` (or a finished game)
gives the omniscient view.

In a running fog game (docs/RULES.md §14, ``agentciv.engine.fog``) player
views and the token-less spectator view are *fogged*: other players'
stockpiles, units and exact score are hidden, armies are listed only on
tiles in the viewer's sight (none for the spectator) and events are scoped
by sight. The full spectator view and every view of a finished game are not.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import constants as C
from . import deals as D
from . import fog as F
from .rules import claim_cost, rules_json, season, settle_cost, thresholds

if TYPE_CHECKING:  # pragma: no cover
    from .game import Game


def _visible(ev: dict, viewer: str | None, omniscient: bool) -> bool:
    vis = ev.get("_vis")
    return vis is None or omniscient or viewer in vis


def _event_view(ev: dict) -> dict:
    return {k: v for k, v in ev.items() if k not in ("_vis", "_fog")}


def _season_view(turn: int) -> dict:
    idx, name, mods = season(turn)
    nxt = C.SEASONS[(idx + 1) % len(C.SEASONS)][0]
    return {
        "name": name,
        "index": idx,
        "turns_left": C.SEASON_LENGTH - (turn % C.SEASON_LENGTH),
        "modifiers": dict(mods),
        "next": nxt,
    }


def _map_view(g: "Game", sight: frozenset | None = None) -> dict:
    w, h = g.width, g.height
    terrain = ["".join(g.terrain[y * w:(y + 1) * w]) for y in range(h)]
    owner = [g.owner[y * w:(y + 1) * w] for y in range(h)]
    improvements = [{"x": i % w, "y": i // w, "building": b}
                    for i, b in enumerate(g.improvement) if b is not None]
    deposits = []
    for i, t in enumerate(g.terrain):
        d = C.DEPOSITS.get(t)
        if d is not None:
            deposits.append({"x": i % w, "y": i // w, "resource": d[0], "remaining": g.deposits[i]})
    relics = [{"x": i % w, "y": i // w, "owner": g.owner[i], "guarded": g.relic_guarded(i)}
              for i in g.relics]
    out = {"width": w, "height": h, "terrain": terrain, "owner": owner,
           "improvements": improvements, "deposits": deposits, "relics": relics}
    if sight is not None:
        out["visible"] = ["".join("1" if y * w + x in sight else "0" for x in range(w)) for y in range(h)]
    return out


def build_view(g: "Game", viewer: str | None, full: bool = False) -> dict:
    n = len(g.players)
    # the spectator sees private diplomacy only once the game is over (or when
    # explicitly asked for the full view, e.g. offline tournaments)
    omniscient = viewer is None and (full or g.status == "finished")
    fog_game = g.config.fog
    fogged = g.fog and not omniscient
    sight = F.vision(g, viewer) if fogged and viewer is not None else frozenset()
    st = g.stats()
    players = []
    for p in g.players:
        s = st[p.id]
        players.append({
            "id": p.id,
            "name": p.name,
            "color": p.color,
            "alive": p.alive,
            "eliminated_turn": p.eliminated_turn,
            "resources": dict(p.resources),
            "income": dict(s["income"]),
            "cities": s["cities"],
            "tiles": s["tiles"],
            "capitals_held": s["capitals_held"],
            "military_power": s["military_power"],
            "units": dict(s["units"]),
            "upkeep": s["upkeep"],
            "wonder_stage": s["wonder_stage"],
            "relics_held": s["relics_held"],
            "relics_guarded": s["relics_guarded"],
            "relic_streak": p.relic_streak,
            "betrayals": p.betrayals,
            "reputation": {"deals": p.deals, "contracts_honoured": p.contracts_honoured,
                           "defaults": p.defaults, "betrayals": p.betrayals,
                           "influence_debt": p.influence_debt},
            "score": s["score"],
            "submitted": g.has_submitted(p.id),
            "victory_progress": dict(s["victory_progress"]),
        })
        if fog_game:
            row = players[-1]
            row["reputation"]["spy_incidents"] = p.spy_incidents
            row["fogged"] = False
            if fogged and p.id != viewer:
                F.redact_row(row)

    you = None
    if viewer is not None:
        p = g.player(viewer)
        s = st[p.id]
        you = {
            "id": p.id,
            "name": p.name,
            "alive": p.alive,
            "resources": dict(p.resources),
            "caps": g.caps(p.id) if g.status != "lobby" else {r: C.STORAGE_BASE for r in C.CAPPED_RESOURCES},
            "income": dict(s["income"]),
            "upkeep": s["upkeep"],
            "claim_cost": claim_cost(p.tiles),
            "settle_cost": settle_cost(s["cities"]),
            "market_fee": C.MARKET_HALL_FEE if g.status != "lobby" and g.has_market_hall(p.id) else C.MARKET_FEE,
            "capital": list(g.xy(p.capital)) if p.capital is not None else None,
            "submitted": g.has_submitted(p.id),
        }
        if fog_game:
            you["counterintel"] = {"pool": p.ci_pool, "rating": F.ci_rating(g, p.id)}

    def involves(*pids) -> bool:
        return omniscient or (viewer is not None and viewer in pids)

    if g.status == "lobby":
        map_view = {"width": 0, "height": 0, "terrain": [], "owner": [], "improvements": [],
                    "deposits": [], "relics": []}
    else:
        map_view = _map_view(g, sight if fogged and viewer is not None else None)

    w = g.width or 1
    armies = []
    for i in sorted(g.armies):
        if fogged and i not in sight:   # own stacks are always in sight
            continue
        for q, u in g.armies[i].items():
            armies.append({"x": i % w, "y": i // w, "owner": q, "units": dict(u)})
    cities = [g.cities[i].view() for i in sorted(g.cities)]

    messages = []
    for m in reversed(g.messages):
        if m["to"] == "all" or involves(m["from"], m["to"]):
            messages.append(dict(m))
            if len(messages) >= C.MESSAGES_IN_VIEW:
                break
    messages.reverse()

    if fogged:
        events = [F.redact_event(e, viewer) for e in g.last_events
                  if _visible(e, viewer, omniscient) and F.event_visible(e, viewer)]
    else:
        events = [_event_view(e) for e in g.last_events if _visible(e, viewer, omniscient)]

    pools = {r: {"resource": round(pl[0], 2), "gold": round(pl[1], 2)} for r, pl in g.pools.items()}
    prices = {r: round(pl[1] / pl[0], 4) for r, pl in g.pools.items() if pl[0] > 0}

    view = {
        "game_id": g.game_id,
        "name": g.config.name,
        "turn": g.turn,
        "max_turns": g.max_turns,
        "status": g.status,
        "deadline": g.deadline,
        "season": _season_view(g.turn),
        "you": you,
        "players": players,
        "map": map_view,
        "cities": cities,
        "armies": armies,
        "market": {
            "fee": C.MARKET_FEE,
            "prices": prices,
            "pools": pools,
            "history": [{"turn": hst["turn"], "prices": dict(hst["prices"])} for hst in g.market_history],
        },
        "treaties": [{"a": a, "b": b, "until_turn": u} for (a, b), u in sorted(g.treaties.items())],
        "treaty_proposals": [dict(pr) for pr in g.treaty_proposals
                             if pr["turn"] == g.turn - 1 and involves(pr["from"], pr["to"])],
        "trade_offers": D.legacy_trade_offers(g, viewer, omniscient),
        **D.view_part(g, viewer, omniscient, fogged),
        "messages": messages,
        "events": events,
        "victory": {
            "thresholds": thresholds(n, g.max_turns) if n else {},
            "result": dict(g.result) if g.result else None,
        },
        "costs": rules_json(),
    }
    if fog_game:
        view["sightings"] = F.sightings_view(g, viewer, sight) if fogged and viewer is not None else []
        view["intel"] = F.intel_view(g, viewer) if viewer is not None else []
        view["fog"] = {
            "enabled": True,
            "active": fogged,
            "vision": {"territory": C.FOG_VISION_TERRITORY, "city": C.FOG_VISION_CITY,
                       "units": C.FOG_VISION_UNITS, "cavalry": C.FOG_VISION_CAVALRY},
            "progress_step": C.FOG_PROGRESS_STEP,
            "hidden_fields": list(C.FOG_HIDDEN_FIELDS),
            "sighting_turns": C.FOG_SIGHTING_TURNS,
            "visible_tiles": len(sight) if fogged else None,
        }
    return view
