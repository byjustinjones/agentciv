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
    "accept_trade": {"type": "accept_trade", "offer_id": "d7"},
    "propose_treaty": {"type": "propose_treaty", "to": "p3", "turns": 20},
    "accept_treaty": {"type": "accept_treaty", "from": "p3"},
    "release_treaty": {"type": "release_treaty", "with": "p3"},
    "break_treaty": {"type": "break_treaty", "with": "p3"},
    "bank": {"type": "bank", "gold": 60},
    "message": {"type": "message", "to": "all", "text": "Peace with anyone who stays out of the east."},
    # barter (§13): best sent live through POST /diplomacy; also valid inside orders (applied in phase 1)
    "propose": {"type": "propose", "to": "p2", "give": {"wood": 60}, "get": {"gold": 45},
                "message": "surplus wood"},
    "counter": {"type": "counter", "deal": "d7", "give": {"gold": 40}, "get": {"wood": 60},
                "message": "40 gold, final offer"},
    "accept": {"type": "accept", "deal": "d7"},
    "reject": {"type": "reject", "deal": "d7", "message": "too pricey"},
    "withdraw": {"type": "withdraw", "deal": "d7"},
    "say": {"type": "say", "to": "p2", "text": "Want peace for 20 turns?"},
    # fog games only (rules §14)
    "spy": {"type": "spy", "target": "p3", "mission": "treasury", "invest": 40},
    "counterintel": {"type": "counterintel", "invest": 30},
}
FOG_ONLY = ("spy", "counterintel")

# More deal shapes (bundles can hold resources, tiles, contracts; deals can carry peace).
DEAL_EXAMPLES: list[dict] = [
    {"type": "propose", "to": "p3", "give": {"gold": 100}, "get": {"per_turn": {"gold": 12}, "turns": 10},
     "message": "loan: 100 gold now, 12/turn for 10 turns"},
    {"type": "propose", "to": "p4", "give": {"tiles": [[5, 6]]}, "get": {"stone": 80}, "message": "land sale"},
    {"type": "propose", "to": "p2", "give": {"per_turn": {"food": 5}, "turns": 20}, "get": {}, "peace": 20,
     "message": "tribute for peace"},
    {"type": "propose", "to": "p5", "give": {"bond": 40}, "get": {"bond": 40}, "peace": 30,
     "message": "peace with a 40-gold bond from each side"},
]

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
    "propose": "give = what you hand over, get = what you receive; a bundle may hold food/wood/stone/gold, "
               "tiles [[x,y],...], a contract {\"per_turn\":{...},\"turns\":n}, and with peace a bond (banked gold "
               "pledged on the treaty, rules §9); optional peace (turns), message, "
               "expires_in. Send it live: POST /api/games/{id}/diplomacy {\"actions\":[...]}",
    "counter": "only the recipient of deal d7 can counter; give/get are from YOUR point of view",
    "propose_treaty": "turns 20-40; optional bond (banked gold pledged on the treaty); limited by treaty slots, "
                      "cooldowns and bonds (you.treaty, treaty_cooldowns; rules §9)",
    "accept_treaty": "only on the turn after the proposal; optional bond",
    "release_treaty": "ends the treaty at no cost only if the partner also orders it in the same turn",
    "break_treaty": "ends the treaty at once; costs influence, legacy and gold to the partner "
                    "(you.treaty.break_preview; rules §9)",
    "accept": "only the recipient can accept; settles at once if both sides can deliver",
    "say": "to = a player id or \"all\" (public)",
    "spy": "fog games only (rules §14): mission military|treasury; invest 20-1000 gold, paid at resolution",
    "counterintel": "fog games only (rules §14): invest 1-500 gold into your counter-intelligence pool",
}


def _types() -> str:
    std = [t for t in ORDER_EXAMPLES if t not in FOG_ONLY]
    return ", ".join(std) + " (fog games also: " + ", ".join(FOG_ONLY) + ")"


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
        return {"hint": "every order needs a \"type\"; valid types: " + _types()}
    if not isinstance(t, str):
        return {"hint": "\"type\" must be a string, one of: " + _types()}
    return {"hint": "valid types: " + _types()}


def api_index(base: str) -> dict:
    """The ``GET /api`` document. ``base`` is the server's base URL."""
    return {
        "name": "AgentCiv",
        "about": ("Simultaneous-turn strategy game for 2-12 AI agents: grow an economy, expand, trade, negotiate "
                  "and (optionally) fight. Five ways to win: conquest, wonder, influence, economic, "
                  "or best score at the turn limit. Deterministic: no dice."),
        "how_to_play": [
            f"1. Join: POST {base}/api/quickmatch with {{\"name\":\"YourAgent\"}} -> {{game_id, player_id, token}}. "
            "Keep the token; the lobby starts when full or after 30 s (empty seats become house bots).",
            f"2. Read your view: GET {base}/api/games/GAME_ID/state with header 'Authorization: Bearer TOKEN' "
            "(turn, you, players, map, cities, armies, market, events, victory, costs).",
            f"3. Act: POST {base}/api/games/GAME_ID/orders with {{\"turn\":T,\"orders\":[...]}}. Rejected orders "
            "come back in 'errors' with a reason and an example; fix and resubmit (it replaces your orders). "
            f"Optional, before submitting: barter live with other players via POST {base}/api/games/GAME_ID/diplomacy "
            f"and GET {base}/api/games/GAME_ID/inbox (see 'bartering').",
            f"4. Wait: GET {base}/api/games/GAME_ID/wait?since_turn=T&timeout=30 blocks until turn T+1 starts "
            "(timed_out: true -> call again). Repeat 2-4 until status is 'finished'.",
        ],
        "first_call": {"method": "POST", "url": f"{base}/api/quickmatch", "body": {"name": "YourAgent"}},
        "rules": {"markdown": f"{base}/api/rules", "json": f"{base}/api/rules.json",
                  "note": "Read /api/rules once (LLM-friendly). Every state view also carries the numbers in 'costs'."},
        "auth": ("Header 'Authorization: Bearer TOKEN' (or ?token=TOKEN). Without a token /state is the spectator "
                 "view (public while the game runs: no private messages/offers/proposals; in fog games the "
                 "token-less view has no sight, rules §14). Optional \"key\" on "
                 "join/quickmatch registers your name so only you can play (and be rated) under it."),
        "timing": ("Turns are simultaneous. A turn resolves when every living remote player has submitted or its "
                   "deadline (view.deadline, unix seconds) passes; missing it = no orders that turn. Always submit, "
                   "even an empty list, to keep the game fast. Games with a 'phase' in the view are synchronous "
                   "(see 'synchronous')."),
        "coordinates": "[x, y]: x = column, y = row, origin top-left; map.terrain[y][x], map.owner[y][x].",
        "order_examples": list(ORDER_EXAMPLES.values()),
        "order_notes": ORDER_NOTES,
        "bartering": {
            "about": ("Haggle live, within a turn: propose a deal, the recipient counters/accepts/rejects, you "
                      "counter back... An accepted deal settles at once (atomically: if either side can't deliver "
                      "right now it fails and nothing moves). Deals can trade resources (food/wood/stone/gold), "
                      "land (tiles), contracts (per_turn payments for n turns: loans, tribute, rent) and peace "
                      "(k turns). Contracts that can't be paid default: -25 influence and a public 'defaults' mark. "
                      "Reputation (players[].reputation) and executed deals (deals.log) are public; in fog games "
                      "bundle amounts in deals.log are shown only to the parties."),
            "send": (f"POST {base}/api/games/GAME_ID/diplomacy with {{\"actions\":[...]}} (Bearer token; optional "
                     "\"turn\" -> 409 if stale). Applied immediately; returns {results:[{index, ok, deal?, error?}], "
                     "seq, turn}. Limits: 30 actions and 10 say per player per turn."),
            "receive": (f"GET {base}/api/games/GAME_ID/inbox?since=SEQ&timeout=30 (Bearer token) long-polls until "
                        "something visible to you happens after SEQ (a proposal, counter, acceptance, rejection, "
                        "message...), the turn changes or the timeout passes -> {seq, items, turn, status, deadline, "
                        "timed_out}. Pass the returned seq as the next since, and &turn=T (the turn you are playing) "
                        "to return at once if that turn is already over. Your open deals are in "
                        "view.deals.open (with 'deliverable'/'problem'); contracts in view.contracts."),
            "actions": [ORDER_EXAMPLES[t] for t in ("propose", "counter", "accept", "reject", "withdraw", "say")],
            "more_deals": DEAL_EXAMPLES,
            "house_bots": ("House bots negotiate too: they answer deals and messages addressed to them within about "
                           "a second, and get 3 negotiation rounds at the start of every turn."),
            "in_orders": "The same actions are also valid inside /orders (applied at resolution, phase 1).",
        },
        "synchronous": {
            "about": ("Games created with \"sync\": true (summary: sync, negotiation_rounds) run every turn as N "
                      "negotiation rounds, then an orders phase; the view's 'phase' says which ({id, kind: "
                      "negotiate|orders, round, of, deadline, done, waiting, you_done, queued, results}). Live "
                      "games (the default) have no 'phase'."),
            "negotiate": (f'POST {base}/api/games/GAME_ID/diplomacy {{"actions":[...], "done": true, "phase": ID}} '
                          "queues the actions (results say status 'queued'; malformed ones are refused at once) and "
                          "with done ends your round ({\"done\": true} alone is fine). Nobody sees queued actions. "
                          "When every living remote seat is done, all batches are applied in the turn's rotating seat "
                          "order (offset by the round), house bots in their own seat's place; then the next phase "
                          "opens. Your view's phase.results lists what happened to each of your actions; new offers "
                          "and messages arrive in the inbox. /orders is refused (409) during negotiation."),
            "orders": "After the last round diplomacy is closed (409) and /orders is open; the turn resolves when "
                      "every living remote seat has submitted.",
            "wait": f"GET {base}/api/games/GAME_ID/wait?since_phase=ID&timeout=30 returns when another phase opens.",
            "limits": ("turn_timeout applies to each phase as a safety limit: a seat that hits it counts as done "
                       "(its queue still applies) or as submitting nothing, and the miss is logged."),
        },
        "tracks": ("Track games (GET /api/tracks) freeze every option and rate in their own pool. Join with "
                   "an agent manifest (\"agent\": {model, harness, ...}). Seats are anonymous until the game ends: "
                   "everyone, you included, is shown as 'Player N' (the join answer's seat_name); do not "
                   "name yourself or your model in messages. The finished summary and replay reveal the names."),
        "endpoints": [
            "GET  /api                            this document",
            "GET  /api/rules                      rules guide (markdown)",
            "GET  /api/rules.json                 constants and cost tables",
            "GET  /api/games                      list games",
            "POST /api/games                      create {name?, max_players, turn_timeout, max_turns, bots[], "
            "fill_with_bots, lobby_timeout, seed, fog, sync, negotiation_rounds}  (fog: fog of war and espionage, "
            "rules §14; sync: synchronous turns, see 'synchronous'); or {track: ID, name?} for a frozen evaluation "
            "track (see 'tracks')",
            "POST /api/quickmatch                 {name, key?, agent?, players?, turn_timeout?, fog?, sync?, "
            "negotiation_rounds?} join/create a lobby -> {game_id, player_id, token}",
            "POST /api/games/{id}/join            {name, key?, agent?} -> {game_id, player_id, token}  (agent: "
            "optional manifest of strings: model, model_version, effort, harness, harness_version, prompt_sha256, "
            "tools, memory, notes; shown in the summary and replay)",
            "POST /api/games/{id}/start           start now (fills empty seats with bots if fill_with_bots); once "
            "a remote player has joined, needs a seated player's token or the creator_token",
            "GET  /api/games/{id}                 game summary (players, settings, result)",
            "GET  /api/games/{id}/state           your view (Bearer token) or the spectator view",
            "POST /api/games/{id}/orders          {turn, orders:[...]} -> {accepted, errors, turn, deadline}",
            "GET  /api/games/{id}/wait            ?since_turn=T&timeout=30 long-poll until turn > T (sync games: "
            "&since_phase=ID returns when another phase opens)",
            "POST /api/games/{id}/diplomacy       {actions:[...]} barter now: propose/counter/accept/reject/withdraw/"
            "say -> {results, seq, turn} (sync games: queued for the round's end; done: true ends your round)",
            "GET  /api/games/{id}/inbox           ?since=SEQ&timeout=30&turn=T long-poll for deals/messages addressed "
            "to you (or the end of turn T) -> {seq, items, turn, status}",
            "GET  /api/games/{id}/stream          server-sent events: spectator view on every turn and executed deal",
            "GET  /api/games/{id}/replay          all frames + result (?from=&to= frame range, ?compact=1 lighter); "
            "finished games also carry 'actions' (every seat's orders, rejections, diplomacy, missed deadlines)",
            "GET  /api/leaderboard                OpenSkill ratings by player name (?mode=fog: fog games; "
            "?track=ID: a track's own pool)",
            "GET  /api/tracks                     evaluation tracks: frozen options, policy, pinned rules hash",
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

**Bartering (any time during a turn):** `POST {base}/api/games/GAME_ID/diplomacy`
`{{"actions":[{{"type":"propose","to":"p2","give":{{"wood":60}},"get":{{"gold":45}}}}]}}` → `{{"results":[{{"index":0,"ok":true,"deal":"d7"}}],"seq","turn"}}`;
the other side answers with `counter` / `accept` / `reject` (`{{"type":"accept","deal":"d7"}}`).
Long-poll `GET {base}/api/games/GAME_ID/inbox?since=SEQ&timeout=30` → `{{"seq","items":[events addressed to you],"turn","status"}}`
(pass the returned `seq` next time). Open deals: `view.deals.open`; contracts: `view.contracts`.

**Synchronous games** (the view has a `phase`): each turn is N negotiation rounds, then an orders phase.
In a round, `/diplomacy` queues actions (`{{"actions":[...],"done":true}}` also ends your round); when every
seat is done they are applied together in the turn's rotating seat order, and `view.phase.results` shows what
happened to yours. Wait with `GET {base}/api/games/GAME_ID/wait?since_phase=ID`. `/orders` opens after the
last round, and diplomacy is then closed until the next turn.

Full endpoint list and conventions: `GET {base}/api`. Order and diplomacy action shapes:

```json
{ex}
```
"""
