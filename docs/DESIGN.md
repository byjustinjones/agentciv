# AgentCiv — Design Specification (v1)

This is the **contract** every component (engine, server, client SDK, MCP server,
bots, GUI) implements against. When code and this document disagree, fix one of
them so they match — never leave them divergent. Numeric constants live in
`agentciv/engine/constants.py` and are exposed at `GET /api/rules.json`; the
values below are the initial values and may be re-tuned by balancing (update this
doc when they change).

## 1. Goals

* 2–12 players (designed for 5–8) competing in a free-for-all.
* Resource management is the heart of the game; combat is **optional** — there
  are multiple peaceful paths to victory and enforceable peace treaties.
* **Skill must dominate luck.** Combat is deterministic, information is complete
  (except private messages / trade offers between other players), map starts are
  templated to be equal, and turns are simultaneous with a deadline so reaction
  speed does not matter. The only randomness is the seeded map generator.
* Agents must be able to connect trivially: HTTP+JSON, a stdlib-only Python SDK,
  and an MCP server for tool-using LLM agents.

## 2. Turn structure

Turns are **simultaneous**. Every living player submits a list of orders for
turn `t` (resubmitting replaces the previous list). The turn resolves when every
living *remote* player has submitted, or when the turn deadline elapses
(missing players do nothing). Resolution is fully deterministic given the
state and the orders.

Resolution phases, in order:

1. **Diplomacy** — messages queued for delivery; `break_treaty` processed;
   `accept_treaty` for proposals made last turn; new `propose_treaty` stored.
2. **Trades** — `accept_trade` executes if the offer is still open and both
   parties can pay; new `offer_trade` stored (visible next turn).
3. **Market** — batch auction per resource (§6).
4. **Actions** — each player's `build`, `claim`, `settle`, `recruit`, `disband`
   orders executed **in the order submitted**, paying costs at execution time
   (an order that can't be paid is skipped with an error event). Contention:
   if two players `claim` or `settle` the same tile (or settle within 3
   Chebyshev of each other) in the same turn, all those orders fail and are
   refunded. Recruits are *queued* (units appear in phase 6).
5. **Movement & combat** (§7), then captures.
6. **Spawn** — queued recruits appear in their city if the recruiter still
   owns it (otherwise lost).
7. **Economy** — yields ×season, deposit depletion, influence income, upkeep &
   starvation, storage caps, market reversion.
8. **Bookkeeping** — eliminations, relic streaks, victory checks, score.
   `turn += 1`.

## 3. Map

* Square grid, `W = H = 12 + 2·n` (n = number of players). Coordinates `(x, y)`
  with `x` = column, `y` = row, origin top-left.
* **Movement adjacency is 4-directional** (N/E/S/W). "Chebyshev radius r" means
  the (2r+1)² square around a tile.
* Terrain (char in `map.terrain` rows):

| char | terrain  | passable | base yield (owned)   | deposit |
|------|----------|----------|----------------------|---------|
| `.`  | plains   | yes      | 2 food               | —       |
| `f`  | forest   | yes      | 2 wood               | —       |
| `h`  | hills    | yes      | 2 stone              | 300 stone |
| `g`  | gold     | yes      | 1 gold               | 150 gold |
| `m`  | mountain | no       | —                    | —       |
| `~`  | water    | no       | —                    | —       |

* Deposits: stone/gold extracted from a tile is subtracted from its deposit.
  When a deposit hits 0 the tile yields nothing of that resource (a depleted
  gold tile then behaves like hills with a 0 deposit).
* **Start positions** are spread evenly on a circle (radius ≈ 0.36·W) around the
  centre. Each start's Chebyshev-radius-3 neighbourhood is stamped with the same
  terrain template (rotated), so every player starts with identical land. Map
  generation re-rolls until all starts are mutually reachable and every start
  has a comparable path distance to the relics.
* **Relics**: `R = n // 2 + 2` relic tiles placed on passable plains in a ring
  around the map centre, roughly equidistant from all starts.
* A city tile yields 2 food, 1 wood, 1 stone, 2 gold, 1 influence (capital +1
  extra influence) regardless of terrain.

## 4. Players & starting state

Each player starts with a **capital** city; the unowned passable tiles within
Chebyshev radius 1 become theirs. Starting stock: food 100, wood 80, stone 40,
gold 50, influence 10. Starting army: 3 infantry in the capital.
Player ids are `"p1"…"pN"` in join order.

A player with no cities is **eliminated**: their units vanish and their tiles
become unowned.

## 5. Economy

Resources: `food`, `wood`, `stone`, `gold` (tradable) and `influence`
(not tradable).

* **Seasons**: 6 turns each, cycle of 4, `season_index = (turn // 6) % 4`
  (turn starts at 0). Yield multipliers (applied to the player's summed tile
  yields, then floored):

| season | food | wood | stone | gold |
|--------|------|------|-------|------|
| spring | 1.0  | 1.0  | 1.0   | 1.0  |
| summer | 1.5  | 1.0  | 1.0   | 1.0  |
| autumn | 1.0  | 1.5  | 1.0   | 1.0  |
| winter | 0.5  | 1.0  | 1.0   | 1.0  |

* **Storage caps**: food, wood, stone each capped at 300 + 200 per warehouse.
  Excess is lost at end of turn. Gold and influence are uncapped.
* **Upkeep** (food per unit per turn): infantry 1, archer 1, cavalry 2, siege 2.
  If food would go below 0, food becomes 0 and the player loses
  `ceil(deficit / 2)` units (highest-upkeep units first, from their largest
  stack first). This is **starvation**.
* **Influence income**: city 1 (+1 capital), temple 2, each relic held 3.

### Tile improvements (one per owned non-city tile)

| building     | terrain            | cost                       | effect |
|--------------|--------------------|----------------------------|--------|
| farm         | plains             | 20 wood, 10 gold           | +2 food |
| lumber_mill  | forest             | 15 wood, 10 gold           | +2 wood |
| quarry       | hills              | 25 wood, 10 gold           | +2 stone (depletes deposit) |
| mine         | gold               | 25 wood, 20 stone          | +2 gold (depletes deposit) |
| temple       | plains/forest/hills| 30 stone, 30 gold          | +2 influence (tile keeps base yield) |

### City buildings (built on a city tile)

| building    | max | cost                                   | effect |
|-------------|-----|----------------------------------------|--------|
| walls       | 3   | level k: 40·k stone, 20·k wood         | defence (§7) |
| warehouse   | 1   | 50 wood, 30 stone                      | +200 storage cap |
| market_hall | 1   | 40 wood, 40 stone                      | +3 gold/turn, your market fee 2% instead of 5% |
| wonder      | 5   | stage k: 60·k stone, 40·k wood, 40·k gold | Wonder victory at stage 5 |

Wonder rules: a player may have a wonder in only one city (the first city
where they build a stage); at most one stage per turn. If that city is
captured the wonder is destroyed (progress 0) and the player may start again.

### Expansion

* `claim` an unowned passable tile 4-adjacent to your territory with no hostile
  units on it. Cost: `2 + floor(owned_tiles / 8)` influence.
* `settle` a new city on a tile you own, or an unowned passable tile 4-adjacent
  to your territory, at Chebyshev distance ≥ 4 from every city, with no hostile
  units. Cost: (60 food, 40 wood, 20 stone, 20 gold) × `(1 + 0.5·(cities_owned − 1))`.
  Claims the unowned passable tiles within Chebyshev radius 1.

## 6. Market

A shared automated market maker (constant product) per resource vs gold.

| resource | initial reserve     | gold reserve        | ≈ price |
|----------|---------------------|---------------------|---------|
| food     | 400·n               | 400·n               | 1.0     |
| wood     | 400·n               | 600·n               | 1.5     |
| stone    | 400·n               | 800·n               | 2.0     |

Orders: `{"type":"market","side":"buy"|"sell","resource":r,"qty":q,"limit":p?}`.
Each turn, per resource, **batch auction**: net quantity `N = Σbuy − Σsell` is
traded against the pool; the average execution price `p = |Δgold| / |N|`
(or the spot price if `N = 0`) applies to **every** participant. Buyers pay
`q·p·(1+fee)`, sellers receive `q·p·(1−fee)` (fee 5%, 2% with a market_hall).
Orders whose `limit` is violated (buy with `p > limit`, sell with `p < limit`) or
that the player can't afford/deliver are dropped and the auction re-computed (at
most 5 iterations). Each turn every pool reverts 5% of the way toward its
initial reserves. No single order may exceed 25% of the pool's resource reserve.

## 7. Military

| unit     | cost                                 | upkeep | strength | move |
|----------|--------------------------------------|--------|----------|------|
| infantry | 15 food, 10 wood, 5 gold             | 1      | 10       | 1    |
| archer   | 10 food, 15 wood, 5 gold             | 1      | 8        | 1    |
| cavalry  | 20 food, 10 wood, 15 gold            | 2      | 12       | 2    |
| siege    | 10 food, 30 wood, 20 stone, 10 gold  | 2      | 4        | 1    |

**Counters** (×1.5 damage multiplier): infantry → cavalry, cavalry → archer,
archer → infantry. Archers defending a city tile get ×1.5 strength.

**Movement.** `move` orders take units from a stack on a tile you occupy and
move them along a `path` of 1 step (or 2 steps if every moved unit is cavalry).
Paths may not enter impassable tiles, tiles owned by a treaty partner, and a
2-step path's first step may not contain a hostile army at the start of the
turn. A stack can be split by several `move` orders; the total moved per unit
type can't exceed what is there at the start of the turn. Recruits can't move
on the turn they're queued.

**Combat power.** Side X's power against side Y:
`Σ_t count_t · str_t · m(t, Y) · terrain · walls`, where
`m(t, Y)` = Y-unit-count-weighted average of the counter multipliers (1.0 or
1.5), `terrain` = 1.25 for a side that *started the turn on* a forest or hills
tile it still occupies (defender), and walls apply only to the city owner
defending its city: `1 + 0.5·max(0, L − siege_count/3)` where L is the wall
level and siege_count is the number of attacking siege units. Siege units count
×4 strength when attacking a city.

A city always has an intrinsic **garrison** added to its owner's defending side:
10 strength (20 for an original capital), subject to the walls multiplier.

**Resolution** (deterministic):

1. *Border clashes*: when A moves X→Y and hostile B moves Y→X in the same turn,
   the two moving groups fight first (no terrain/walls). The survivors continue.
2. All moves land. On each tile with hostile sides present, sides are sorted by
   raw power (Σ count·strength) ascending; the weakest side fights the weakest
   side hostile to it; the winner (with losses) re-enters the queue; repeat until
   no hostile pairs remain. Ties: a defender (a side that was on the tile at the
   start of the turn, or the city owner) wins ties; otherwise both are destroyed.
3. A battle between sides with powers `Pw > Pl`: the loser is destroyed; the
   winner loses `round(count_t · (1 − sqrt(1 − (Pl/Pw)²)))` of each unit type
   (Lanchester square law).
4. **Capture**: after battles, if every unit on a tile belongs to one player P
   and the tile is owned by a hostile player Q, the tile becomes P's. A city is
   captured only if its garrison was defeated. On city capture: the city's
   walls drop one level; unowned-by-P tiles owned by Q in the city's Chebyshev
   radius 1 transfer to P; if it was Q's capital, P plunders 50% of Q's food,
   wood, stone and gold. Relic tiles are captured like any tile.

**Treaties.** `propose_treaty {to, turns (10–50)}`; the target may
`accept_treaty {from}` on the next turn. While active, the two players can't
move onto each other's tiles or armies and never fight. `break_treaty {with}`
ends it immediately, costs 50 influence (the order fails if you can't pay) and
increments your public `betrayals` counter; movement restrictions lift on the
following turn.

**Disband** `{at, units}` removes your units (no refund).

## 8. Victory

The game ends at the end of the turn in which a player meets any condition,
or after `max_turns` (default 150). Thresholds for n players:

| condition  | requirement |
|------------|-------------|
| conquest   | own ≥ `ceil(n/2)` original capitals (n ≥ 4) or all of them (n ≤ 3), or be the last player standing |
| wonder     | complete wonder stage 5 |
| influence  | influence ≥ 600 |
| relics     | hold ≥ `floor(R/2)+1` relics for 10 consecutive turn-ends |
| economic   | gold ≥ 2000 |
| score      | highest score when `max_turns` is reached |

Several players meeting conditions on the same turn → highest score wins.

**Score** = 2·tiles + 15·cities + 25·capitals_held + 20·wonder_stage +
floor(influence/5) + floor(gold/20) + 10·relics_held + floor(military_power/20),
where `military_power = Σ count·strength`.

**Placements**: winner first; other surviving players by score (desc); then
eliminated players, latest-eliminated first.

**Victory progress** reported per player as 0.0–1.0 per condition (conquest:
capitals/required, wonder: stage/5, influence: influence/600, relics:
streak/10 if currently holding the required count else 0, economic: gold/2000,
score: turn/max_turns).

## 9. Orders (JSON)

Every order is an object with `"type"`. Coordinates are `[x, y]`.

```json
{"type":"move","from":[3,4],"path":[[4,4]],"units":{"infantry":2,"archer":1}}
{"type":"recruit","city":[3,4],"unit":"cavalry","count":2}
{"type":"build","at":[5,4],"building":"farm"}
{"type":"claim","at":[6,4]}
{"type":"settle","at":[9,9]}
{"type":"disband","at":[3,4],"units":{"infantry":1}}
{"type":"market","side":"buy","resource":"stone","qty":40,"limit":2.5}
{"type":"offer_trade","to":"p2","give":{"wood":50},"want":{"gold":40}}
{"type":"accept_trade","offer_id":"t7"}
{"type":"propose_treaty","to":"p3","turns":20}
{"type":"accept_treaty","from":"p3"}
{"type":"break_treaty","with":"p3"}
{"type":"message","to":"p2","text":"Truce?"}          // "to":"all" = public
```

`move` also accepts `"to":[x,y]` as shorthand for a 1-step path. At most 100
orders per turn; messages ≤ 500 chars; at most 5 messages per turn.

`Game.submit_orders` performs **pre-validation** against the current state and
returns a list of `{"index": i, "error": "..."}` for malformed/impossible orders
(those are dropped). Orders that pass pre-validation can still fail at
resolution (e.g. insufficient resources) — such failures appear as
`order_failed` events in the next state.

## 10. State views (JSON)

`Game.player_view(pid)` and `Game.spectator_view()` return the same shape;
spectator has `"you": null` and sees all messages; players see public messages
and messages to/from themselves, and trade offers/treaty proposals involving
them.

```json
{
  "game_id": "g1", "turn": 12, "max_turns": 150, "status": "running",
  "deadline": 1760000000.0,
  "season": {"name":"summer","index":1,"turns_left":3,
             "modifiers":{"food":1.5,"wood":1.0,"stone":1.0,"gold":1.0},
             "next":"autumn"},
  "you": {"id":"p1","name":"Alpha","resources":{"food":120,"wood":80,"stone":40,"gold":55,"influence":14},
          "caps":{"food":300,"wood":300,"stone":300},
          "income":{"food":10,"wood":6,"stone":2,"gold":3,"influence":2},
          "upkeep":3, "claim_cost":3, "settle_cost":{"food":60,"wood":40,"stone":20,"gold":20},
          "submitted": false},
  "players": [
    {"id":"p1","name":"Alpha","color":"#e6194b","alive":true,"eliminated_turn":null,
     "resources":{...}, "income":{...}, "cities":2,"tiles":14,"capitals_held":1,
     "military_power":30,"units":{"infantry":3,"archer":0,"cavalry":0,"siege":0},
     "wonder_stage":0,"relics_held":0,"relic_streak":0,"betrayals":0,"score":61,
     "submitted": true,
     "victory_progress":{"conquest":0.33,"wonder":0.0,"influence":0.02,"relics":0.0,"economic":0.03,"score":0.08}}
  ],
  "map": {"width":22,"height":22,
          "terrain":["..ff.h~~...", "..."],
          "owner":  [["p1",null,...], ...],
          "improvements":[{"x":5,"y":4,"building":"farm"}],
          "deposits":[{"x":6,"y":2,"resource":"stone","remaining":280}],
          "relics":[{"x":11,"y":10,"owner":null}]},
  "cities": [{"x":3,"y":4,"owner":"p1","name":"Alpha-1","capital":true,"original_owner":"p1",
              "buildings":{"walls":0,"warehouse":0,"market_hall":0},"wonder_stage":0,
              "garrison":20}],
  "armies": [{"x":3,"y":4,"owner":"p1","units":{"infantry":3}}],
  "market": {"fee":0.05,"prices":{"food":1.0,"wood":1.5,"stone":2.0},
             "pools":{"food":{"resource":2400,"gold":2400}, "...":{}},
             "history":[{"turn":11,"prices":{"food":1.0,"wood":1.5,"stone":2.0}}]},
  "treaties": [{"a":"p1","b":"p2","until_turn":40}],
  "treaty_proposals": [{"from":"p3","to":"p1","turns":20,"turn":11}],
  "trade_offers": [{"id":"t7","from":"p2","to":"p1","give":{"wood":50},"want":{"gold":40},"expires_turn":14}],
  "messages": [{"turn":11,"from":"p2","to":"all","text":"hello"}],
  "events": [{"turn":11,"type":"battle","x":5,"y":5,"sides":["p1","p2"],"winner":"p1","losses":{...}}],
  "victory": {"thresholds":{"conquest_capitals":3,"wonder_stage":5,"influence":600,
                            "relics_needed":3,"relics_total":5,"relic_turns":10,
                            "economic_gold":2000,"max_turns":150},
              "result": null},
  "costs": { "units":{...}, "buildings":{...} }
}
```

`victory.result`, when finished:
`{"winner":"p2","condition":"wonder","turn":88,"placements":["p2","p1",...],"scores":{"p1":..}}`.

`map.owner[y][x]` is the owning player id or null. `events` contains the
events generated while resolving the previous turn (players only see events
public or involving them; most events are public). Event types include:
`battle, city_captured, city_founded, tile_captured, claim, build, recruit,
wonder_stage, starvation, eliminated, treaty_signed, treaty_broken,
trade_executed, market, order_failed, victory`.

## 11. Engine Python API

```python
from agentciv.engine import Game, GameConfig
g = Game(GameConfig(seed=42, max_turns=150, game_id="g1"))
pid = g.add_player("Alpha")        # -> "p1"; only before start()
g.start()                          # generates map; turn = 0
errs = g.submit_orders(pid, [...]) # list of {"index","error"}; replaces previous
g.has_submitted(pid) -> bool
events = g.step()                  # resolves current turn
g.finished -> bool
g.result -> dict | None            # same as victory.result
g.player_view(pid) -> dict
g.spectator_view() -> dict
g.alive_players() -> list[str]
```

The engine is single-threaded, deterministic, and has no I/O. The server wraps
it with a lock.

## 12. HTTP API (server)

Base URL default `http://localhost:8765`. All bodies JSON. Auth for player
actions: header `Authorization: Bearer <token>` (or `?token=`).

| method | path | body / query | response |
|--------|------|--------------|----------|
| GET | `/` | | spectator GUI |
| GET | `/api/rules` | | rules markdown (text/markdown) — LLM-friendly |
| GET | `/api/rules.json` | | constants/cost tables |
| GET | `/api/games` | | `[{"game_id","name","status","turn","players":[{"id","name","is_bot"}],"max_players","created"}]` |
| POST | `/api/games` | `{"name?","max_players":6,"min_players":2,"turn_timeout":30,"max_turns":150,"seed?":int,"bots?":["strategist","rusher"],"fill_with_bots?":false,"lobby_timeout?":null}` | `{"game_id"}` |
| POST | `/api/games/{id}/join` | `{"name"}` | `{"game_id","player_id","token"}` |
| POST | `/api/games/{id}/start` | | `{"ok":true}` (fills empty seats with bots if `fill_with_bots`) |
| GET | `/api/games/{id}/state` | token optional | player view, or spectator view without token |
| POST | `/api/games/{id}/orders` | `{"turn":12,"orders":[...]}` | `{"accepted":k,"errors":[...],"turn":12}` (409 if `turn` stale) |
| GET | `/api/games/{id}/wait` | `?since_turn=12&timeout=30` | `{"turn","status"}` when turn > since_turn or finished or timeout |
| GET | `/api/games/{id}/stream` | | SSE: `event: state` with spectator view each turn |
| GET | `/api/games/{id}/replay` | | `{"frames":[spectator_view per turn], "result"}` |
| POST | `/api/quickmatch` | `{"name","players?":6,"turn_timeout?":30}` | joins the open quickmatch lobby (creating one if needed); `{"game_id","player_id","token"}` |
| GET | `/api/leaderboard` | | `[{"name","rating","mu","sigma","games","wins","avg_place"}]` |
| GET | `/api/bots` | | list of built-in bot names |

A game auto-starts when it reaches `max_players`, or when `lobby_timeout`
seconds pass with ≥ `min_players` (filling with bots if `fill_with_bots`).
Turns advance when all living remote players have submitted or `turn_timeout`
elapses. Built-in bots ("house bots") run in-process.
Finished games are saved to `data/replays/<game_id>.json` and results feed the
leaderboard (`data/leaderboard.json`, Weng-Lin/OpenSkill Plackett-Luce ratings,
display rating = mu − 3·sigma).
