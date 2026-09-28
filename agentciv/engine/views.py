"""JSON views of the game state (docs/DESIGN.md §10).

``build_view(game, pid)`` returns the player view for ``pid`` or the
spectator view when ``pid`` is None. Everything returned is freshly built
plain JSON data (safe for callers to mutate).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from . import constants as C
from .rules import claim_cost, rules_json, season, settle_cost, thresholds

if TYPE_CHECKING:  # pragma: no cover
    from .game import Game


def _visible(ev: dict, viewer: str | None) -> bool:
    vis = ev.get("_vis")
    return vis is None or viewer is None or viewer in vis


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


def _map_view(g: "Game") -> dict:
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
    relics = [{"x": i % w, "y": i // w, "owner": g.owner[i]} for i in g.relics]
    return {"width": w, "height": h, "terrain": terrain, "owner": owner,
            "improvements": improvements, "deposits": deposits, "relics": relics}


def build_view(g: "Game", viewer: str | None) -> dict:
    n = len(g.players)
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
            "relic_streak": p.relic_streak,
            "betrayals": p.betrayals,
            "score": s["score"],
            "submitted": g.has_submitted(p.id),
            "victory_progress": dict(s["victory_progress"]),
        })

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

    def involves(*pids) -> bool:
        return viewer is None or viewer in pids

    if g.status == "lobby":
        map_view = {"width": 0, "height": 0, "terrain": [], "owner": [], "improvements": [],
                    "deposits": [], "relics": []}
    else:
        map_view = _map_view(g)

    w = g.width or 1
    armies = []
    for i in sorted(g.armies):
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

    events = [{k: v for k, v in e.items() if k != "_vis"}
              for e in g.last_events if _visible(e, viewer)]

    pools = {r: {"resource": round(pl[0], 2), "gold": round(pl[1], 2)} for r, pl in g.pools.items()}
    prices = {r: round(pl[1] / pl[0], 4) for r, pl in g.pools.items() if pl[0] > 0}

    return {
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
        "trade_offers": [{"id": o["id"], "from": o["from"], "to": o["to"], "give": dict(o["give"]),
                          "want": dict(o["want"]), "turn": o["turn"], "expires_turn": o["expires_turn"]}
                         for o in g.trade_offers if involves(o["from"], o["to"])],
        "messages": messages,
        "events": events,
        "victory": {
            "thresholds": thresholds(n, g.max_turns) if n else {},
            "result": dict(g.result) if g.result else None,
        },
        "costs": rules_json(),
    }
