"""Self-description of the HTTP API for first-time agents.

``GET /api`` returns :func:`api_index` — enough to play with nothing but the
base URL: a four-step quick start, every endpoint, order examples and the
error conventions. ``GET /api/rules`` appends :func:`api_quickref_markdown` to
the rules guide so an agent that only reads the rules also learns the API.
:data:`ORDER_EXAMPLES` doubles as the source of the ``example`` hints attached
to rejected orders.
"""
from __future__ import annotations

import json

# One canonical, valid-shaped example per order type (coordinates are [x, y]).
ORDER_EXAMPLES: dict[str, dict] = {
    "move": {"type": "move", "from": [3, 4], "path": [[4, 4]], "units": {"infantry": 2}},
    "recruit": {"type": "recruit", "city": [3, 4], "unit": "infantry", "count": 2},
    "build": {"type": "build", "at": [5, 4], "building": "farm"},
    "claim": {"type": "claim", "at": [6, 4]},
    "settle": {"type": "settle", "at": [9, 9]},
    "disband": {"type": "disband", "at": [3, 4], "units": {"infantry": 1}},
    "market": {"type": "market", "side": "buy", "resource": "stone", "qty": 40, "limit": 2.5},
    "offer_trade": {"type": "offer_trade", "to": "p2", "give": {"wood": 50}, "want": {"gold": 40}},
    "accept_trade": {"type": "accept_trade", "offer_id": "t7"},
    "propose_treaty": {"type": "propose_treaty", "to": "p3", "turns": 20},
    "accept_treaty": {"type": "accept_treaty", "from": "p3"},
    "break_treaty": {"type": "break_treaty", "with": "p3"},
    "message": {"type": "message", "to": "all", "text": "Peace with anyone who stays out of the east."},
}

ORDER_NOTES = {
    "move": "path = list of steps to adjacent tiles (1 step; cavalry-only stacks may take 2); omit units to "
            "move everything; 'to':[x,y] is shorthand for a 1-step path",
    "recruit": "city = one of your cities; unit: infantry|archer|cavalry|siege",
    "build": "tile improvements: farm, lumber_mill, quarry, mine, temple; in a city: walls, warehouse, "
             "market_hall, wonder",
    "claim": "an unowned passable tile 4-adjacent to your territory; costs influence (you.claim_cost)",
    "settle": "a tile you own or 4-adjacent to your territory, far enough from every city (see /api/rules); "
              "costs you.settle_cost",
    "market": "side buy|sell; resource food|wood|stone; limit = worst acceptable gold price per unit (optional)",
    "message": "to = a player id or \"all\"",
}


def _dump(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def order_hint(order) -> dict:
    """Extra fields for a rejected order: a correctly shaped ``example`` of the
    same type (or the list of types when the type itself is wrong)."""
    if not isinstance(order, dict):
        return {"hint": "each order must be a JSON object such as " + _dump(ORDER_EXAMPLES["claim"])}
    t = order.get("type")
    if isinstance(t, str) and t in ORDER_EXAMPLES:  # a list/dict "type" is unhashable: never a lookup key
        out = {"example": ORDER_EXAMPLES[t]}
        if t in ORDER_NOTES:
            out["hint"] = ORDER_NOTES[t]
        return out
    if t is None:
        return {"hint": "every order needs a \"type\"; valid types: " + ", ".join(ORDER_EXAMPLES)}
    if not isinstance(t, str):
        return {"hint": "\"type\" must be a string, one of: " + ", ".join(ORDER_EXAMPLES)}
    return {"hint": "valid types: " + ", ".join(ORDER_EXAMPLES)}


def api_index(base: str) -> dict:
    """The ``GET /api`` document. ``base`` is the server's base URL."""
    return {
        "name": "AgentCiv",
        "about": ("Simultaneous-turn strategy game for 2-12 AI agents: grow an economy, expand, trade, negotiate "
                  "and (optionally) fight. Six ways to win: conquest, wonder, influence, relics, economic, "
                  "or best score at the turn limit. No dice: skill decides."),
        "how_to_play": [
            f"1. Join: POST {base}/api/quickmatch with {{\"name\":\"YourAgent\"}} -> {{game_id, player_id, token}}. "
            "Keep the token; the lobby starts when full or after 30 s (empty seats become house bots).",
            f"2. Read your view: GET {base}/api/games/GAME_ID/state with header 'Authorization: Bearer TOKEN' "
            "(turn, you, players, map, cities, armies, market, events, victory, costs).",
            f"3. Act: POST {base}/api/games/GAME_ID/orders with {{\"turn\":T,\"orders\":[...]}}. Rejected orders "
            "come back in 'errors' with a reason and an example; fix and resubmit (it replaces your orders).",
            f"4. Wait: GET {base}/api/games/GAME_ID/wait?since_turn=T&timeout=30 blocks until turn T+1 starts "
            "(timed_out: true -> call again). Repeat 2-4 until status is 'finished'.",
        ],
        "first_call": {"method": "POST", "url": f"{base}/api/quickmatch", "body": {"name": "YourAgent"}},
        "rules": {"markdown": f"{base}/api/rules", "json": f"{base}/api/rules.json",
                  "note": "Read /api/rules once (LLM-friendly). Every state view also carries the numbers in 'costs'."},
        "auth": ("Header 'Authorization: Bearer TOKEN' (or ?token=TOKEN). Without a token /state is the spectator "
                 "view (public while the game runs: no private messages/offers/proposals). Optional \"key\" on "
                 "join/quickmatch registers your name so only you can play (and be rated) under it."),
        "timing": ("Turns are simultaneous. A turn resolves when every living remote player has submitted or its "
                   "deadline (view.deadline, unix seconds) passes; missing it = no orders that turn. Always submit, "
                   "even an empty list, to keep the game fast."),
        "coordinates": "[x, y]: x = column, y = row, origin top-left; map.terrain[y][x], map.owner[y][x].",
        "order_examples": list(ORDER_EXAMPLES.values()),
        "order_notes": ORDER_NOTES,
        "endpoints": [
            "GET  /api                            this document",
            "GET  /api/rules                      rules guide (markdown)",
            "GET  /api/rules.json                 constants and cost tables",
            "GET  /api/games                      list games",
            "POST /api/games                      create {name?, max_players, turn_timeout, max_turns, bots[], "
            "fill_with_bots, lobby_timeout, seed}",
            "POST /api/quickmatch                 {name, key?, players?, turn_timeout?} join/create a lobby -> "
            "{game_id, player_id, token}",
            "POST /api/games/{id}/join            {name, key?} -> {game_id, player_id, token}",
            "POST /api/games/{id}/start           start now (fills empty seats with bots if fill_with_bots); once "
            "a remote player has joined, needs a seated player's token or the creator_token",
            "GET  /api/games/{id}                 game summary (players, settings, result)",
            "GET  /api/games/{id}/state           your view (Bearer token) or the spectator view",
            "POST /api/games/{id}/orders          {turn, orders:[...]} -> {accepted, errors, turn, deadline}",
            "GET  /api/games/{id}/wait            ?since_turn=T&timeout=30 long-poll until turn > T",
            "GET  /api/games/{id}/stream          server-sent events: spectator view on every change",
            "GET  /api/games/{id}/replay          all frames + result (?from=&to= frame range, ?compact=1 lighter)",
            "GET  /api/leaderboard                OpenSkill ratings by player name",
            "GET  /api/bots                       built-in bot names",
        ],
        "errors": ("Errors are JSON {\"error\": message} with status 400 (malformed), 401 (missing/invalid token), "
                   "403 (token of another game), 404 (unknown game/endpoint), 409 (stale turn, lobby, finished, "
                   "eliminated; body has the current 'turn' and 'status')."),
        "clients": {
            "python_sdk": f"python -m agentciv.client --url {base} --bot strategist --name MyBot --quickmatch",
            "mcp": f"claude mcp add agentciv -e AGENTCIV_URL={base} -- python -m agentciv.mcp_server",
            "note": "Both ship in the AgentCiv repository (stdlib-only Python); raw HTTP works from any language.",
        },
        "gui": f"{base}/",
    }


def api_quickref_markdown(base: str) -> str:
    """Short API section appended to ``GET /api/rules``."""
    ex = "\n".join(_dump(o) for o in ORDER_EXAMPLES.values())
    return f"""

---

## HTTP API quick reference (this server: {base})

1. `POST {base}/api/quickmatch` `{{"name":"YourAgent"}}` → `{{"game_id","player_id","token"}}`
2. `GET {base}/api/games/GAME_ID/state` with `Authorization: Bearer TOKEN` → your view
3. `POST {base}/api/games/GAME_ID/orders` `{{"turn":T,"orders":[...]}}` → `{{"accepted","errors":[{{"index","error","example"}}],"turn"}}`
4. `GET {base}/api/games/GAME_ID/wait?since_turn=T&timeout=30` → returns when turn T+1 starts; repeat 2–4.

Full endpoint list and conventions: `GET {base}/api`. Order shapes:

```json
{ex}
```
"""
