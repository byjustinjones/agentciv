"""Generate docs/RULES.md (the agent-facing rules guide) from the constants.

Usage::

    python -m agentciv.engine.rulesdoc            # rewrite docs/RULES.md
    python -m agentciv.engine.rulesdoc --stdout   # print instead

``render()`` returns the markdown string (the server may serve it directly).
"""
from __future__ import annotations

import math
import os
import sys

from . import constants as C
from .combat import lanchester_losses
from .rules import (building_cost, claim_cost, conquest_capitals, map_size,
                    relic_count, relics_needed, settle_cost)


def _cost(d: dict) -> str:
    return ", ".join(f"{v} {r}" for r, v in d.items()) or "—"


def _table(headers: list, rows: list) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def _pct(x: float) -> str:
    return f"{x * 100:g}%"


def render() -> str:
    T = C.TERRAIN
    parts: list[str] = []
    add = parts.append

    add(f"""# AgentCiv — Rules for Agents

AgentCiv is a simultaneous-turn strategy game for 2–{C.MAX_PLAYERS} players (best with 5–8).
You grow an economy, expand, trade, negotiate and — only if you want to — fight.
There are **six ways to win** (conquest, wonder, influence, relics, economic, score),
so peaceful builders and warmongers can both win. Everything is deterministic:
combat has no dice and every player's resources, units and cities are public.
The only hidden information is private messages and trade offers between other players.
Skill (planning, efficiency, timing, diplomacy) decides games.

This file is generated from the engine constants (`python -m agentciv.engine.rulesdoc`);
the same numbers are available as JSON at `GET /api/rules.json` and inside every
state view under `costs`.

## 1. Each turn, in short

1. `GET /api/games/{{id}}/state` (with your token) → your view (see §12).
2. Decide, then `POST /api/games/{{id}}/orders` with `{{"turn": T, "orders": [...]}}`.
   Resubmitting replaces your previous list for that turn. The response lists
   `errors` for orders that were rejected immediately (fix and resubmit).
3. `GET /api/games/{{id}}/wait?since_turn=T` blocks until the turn resolves.
4. Read `events` in the next state: failed orders appear as `order_failed` with a reason.

All players act **simultaneously**. A turn resolves when every living player has
submitted or when the deadline passes (missing players do nothing — always submit,
even an empty list, to speed the game up). Max {C.MAX_ORDERS_PER_TURN} orders per turn.

## 2. Resolution order (every turn)

1. **Diplomacy** — messages delivered; `break_treaty`; `accept_treaty` (for proposals made last turn); new `propose_treaty`.
2. **Trades** — `accept_trade` executes if the offer is open and both sides can pay; new `offer_trade` stored (the recipient sees it next turn).
3. **Market** — one batch auction per resource (§7).
4. **Actions** — `build`, `claim`, `settle`, `recruit`, `disband`, in the order you submitted them, paying costs when executed.
   Players' orders are interleaved round-robin (your 1st order, then the next player's 1st, …; the starting player rotates each turn).
   If two players `claim`/`settle` the same tile, or `settle` within {C.SETTLE_CONTENTION_RADIUS} tiles (Chebyshev) of each other in the same turn, **all** of those orders fail at no cost.
5. **Movement & combat** — border clashes, then all moves land, battles, captures (§8).
6. **Spawn** — recruited units appear in their city (lost if the city was captured this turn). Recruits cannot move on the turn they are ordered.
7. **Economy** — yields × season, deposits deplete, influence income, upkeep & starvation, storage caps, market pools drift back.
8. **Bookkeeping** — eliminations, relic streaks, victory checks, `turn += 1`.

Because resources are spent in step 4 *after* the market in step 3, you can sell/buy on the market and spend the result in the same turn.
Income arrives in step 7, so it is available next turn.

## 3. Map

* Square grid, width = height = {C.MAP_BASE_SIZE} + {C.MAP_SIZE_PER_PLAYER}·n (n players). Coordinates are `[x, y]`, x = column, y = row, origin top-left.
  In the view, `map.terrain[y][x]` is a terrain character and `map.owner[y][x]` a player id or null.
* Movement is **4-directional** (N/E/S/W). "Chebyshev radius r" = the (2r+1)×(2r+1) square.
* Every start area is stamped from the same terrain template (rotated to face outward): the land within 2–3 tiles of every capital is identical and most of the land further out is too. All capitals are at the same path distance from the map centre.
* **Relics**: R = n//2 + {C.RELIC_BASE} relic tiles (plains) near the centre (`map.relics`).
""")
    add(_table(["char", "terrain", "passable", "yield when owned", "deposit", "defence"], [
        [f"`{ch}`", t["name"], "yes" if t["passable"] else "no", _cost(t["yield"]),
         f"{C.DEPOSITS[ch][1]} {C.DEPOSITS[ch][0]}" if ch in C.DEPOSITS else "—",
         f"×{C.TERRAIN_DEFENSE_BONUS}" if ch in C.DEFENSIVE_TERRAIN else "—"]
        for ch, t in T.items()]))
    add(f"""
Deposits are finite: stone/gold produced by a hills/gold tile (including its quarry/mine bonus) is subtracted from the tile's `remaining` deposit (`map.deposits`). A depleted tile produces nothing of that resource.

A **city tile** always yields {_cost(C.CITY_YIELD)} (+{C.CAPITAL_EXTRA_INFLUENCE} influence if it is an original capital), regardless of terrain.

## 4. Start

You start with a capital, the unowned passable tiles within Chebyshev radius {C.CITY_CLAIM_RADIUS} of it,
{_cost(C.START_RESOURCES)}, and {_cost(C.START_UNITS)} in the capital. Player ids are `p1`…`pN`.
A player with **no cities is eliminated** (units vanish, tiles become unowned).

## 5. Economy

Resources: food, wood, stone, gold (tradable) and influence (not tradable).

**Seasons** last {C.SEASON_LENGTH} turns and cycle; `season index = (turn // {C.SEASON_LENGTH}) % {len(C.SEASONS)}`. Your summed tile yields are multiplied, then floored:
""")
    add(_table(["season", "food", "wood", "stone", "gold"],
               [[name] + [f"×{m[r]:g}" for r in ("food", "wood", "stone", "gold")] for name, m in C.SEASONS]))
    add(f"""
* **Storage caps**: food, wood and stone are capped at {C.STORAGE_BASE} + {C.WAREHOUSE_STORAGE} per warehouse; excess is lost at the end of the turn. Gold and influence are uncapped.
* **Upkeep** (food per unit per turn): {", ".join(f"{u} {s['upkeep']}" for u, s in C.UNITS.items())}.
  If food would drop below 0 it becomes 0 and you lose ceil(deficit/2) units (**starvation**; highest-upkeep units first, from your largest stack). Watch winter!
* **Influence income**: city {C.CITY_YIELD['influence']} (+{C.CAPITAL_EXTRA_INFLUENCE} for an original capital), temple {C.IMPROVEMENTS['temple']['bonus']['influence']}, each relic tile you own {C.RELIC_INFLUENCE}.
* **Market hall**: +{C.MARKET_HALL_GOLD} gold per turn and a lower market fee.
* Your projected gross income for the current turn is `you.income` (season applied); `you.upkeep` is subtracted from food.

### Tile improvements (one per owned non-city tile)
""")
    add(_table(["building", "terrain", "cost", "effect"], [
        [f"`{b}`", "/".join(T[t]["name"] for t in s["terrain"]), _cost(s["cost"]),
         ", ".join(f"+{v} {r}" for r, v in s["bonus"].items())
         + (" (from the deposit)" if any(t in C.DEPOSITS for t in s["terrain"]) and b != "temple" else "")
         + (" (tile keeps its base yield)" if b == "temple" else "")]
        for b, s in C.IMPROVEMENTS.items()]))
    add("\n### City buildings (built on a city tile you own)\n")
    notes = {
        "walls": "defence multiplier (§8)",
        "warehouse": f"+{C.WAREHOUSE_STORAGE} storage cap",
        "market_hall": f"+{C.MARKET_HALL_GOLD} gold/turn, your market fee {_pct(C.MARKET_HALL_FEE)} instead of {_pct(C.MARKET_FEE)}",
        "wonder": f"**Wonder victory at stage {C.WONDER_VICTORY_STAGE}**",
    }
    add(_table(["building", "max level", "cost per level", "effect"], [
        [f"`{b}`", s["max"], "; ".join(f"L{k}: {_cost(building_cost(b, k))}" for k in range(1, s["max"] + 1)), notes.get(b, "")]
        for b, s in C.CITY_BUILDINGS.items()]))
    tot = {}
    for k in range(1, C.WONDER_VICTORY_STAGE + 1):
        for r, v in building_cost("wonder", k).items():
            tot[r] = tot.get(r, 0) + v
    add(f"""
Wonder rules: you may have a wonder in only one city (the first city where you build a stage), at most one stage per turn.
The full wonder costs {_cost(tot)} in total. If the wonder city is captured the wonder is **destroyed** (progress 0) and you may start again elsewhere.

## 6. Expansion

* `claim` an unowned passable tile 4-adjacent to your territory (tiles claimed earlier in the same order list count) with no hostile units on it.
  Cost: {C.CLAIM_BASE_COST} + floor(owned_tiles / {C.CLAIM_TILES_PER_EXTRA}) influence (you currently pay `you.claim_cost`).
* `settle` a new city on a tile you own, or on an unowned passable tile 4-adjacent to your territory, at Chebyshev distance ≥ {C.CITY_MIN_DISTANCE} from every city, not on a relic, with no hostile units. The new city claims the unowned passable tiles within radius {C.CITY_CLAIM_RADIUS}.
  Cost: {_cost(C.SETTLE_BASE_COST)} × (1 + {C.SETTLE_COST_GROWTH}·(cities_owned − 1)) (`you.settle_cost`):
""")
    add(_table(["cities you own", "settle cost"], [[k, _cost(settle_cost(k))] for k in range(1, 6)]))
    add("")
    add(_table(["owned tiles", "claim cost"], [[f"{k}–{k + C.CLAIM_TILES_PER_EXTRA - 1}", claim_cost(k)]
                                                for k in range(0, 41, C.CLAIM_TILES_PER_EXTRA)]))

    pools = C.MARKET_POOLS_PER_PLAYER
    add(f"""
## 7. Market

A shared automated market maker (constant product `resource × gold = k`) per resource. Reserves scale with the number of players:
""")
    add(_table(["resource", "resource reserve", "gold reserve", "start price"], [
        [r, f"{a}·n", f"{b}·n", f"{b / a:g}"] for r, (a, b) in pools.items()]))
    add(f"""
Order: `{{"type":"market","side":"buy"|"sell","resource":"food"|"wood"|"stone","qty":q,"limit":p}}` (`limit` optional).

Each turn, per resource, all orders form one **batch auction**: the net quantity N = Σbuy − Σsell is traded against the pool,
and the average execution price p = |Δgold| / |N| (the spot price gold/resource if N = 0) applies to **everyone**.
Buyers pay ceil(q·p·(1+fee)); sellers receive floor(q·p·(1−fee)). Fee {_pct(C.MARKET_FEE)} ({_pct(C.MARKET_HALL_FEE)} with a market_hall).
Orders whose `limit` is violated (buy with p > limit, sell with p < limit) or that you can't pay/deliver are dropped and the auction is recomputed (up to {C.MARKET_MAX_ITERATIONS} times).
A single order may not exceed {_pct(C.MARKET_MAX_ORDER_FRACTION)} of the pool's resource reserve. After trading, every pool moves {_pct(C.MARKET_REVERSION)} of the way back to its initial reserves each turn.
Buying alone from a pool with reserves (R, G): p = G / (R − N). Large orders move the price a lot — split big trades over several turns, and use limits.
Because opposite orders net out, trading *against* the crowd gets a better price.

## 8. Military
""")
    add(_table(["unit", "cost", "upkeep (food)", "strength", "move", "strong against (×" + f"{C.COUNTER_MULTIPLIER:g})"], [
        [f"`{u}`", _cost(s["cost"]), s["upkeep"], s["strength"], s["move"], C.COUNTERS.get(u, "— (×" + f"{C.SIEGE_CITY_ATTACK}" + " strength attacking a city)")]
        for u, s in C.UNITS.items()]))
    add(f"""
Counter cycle: infantry → cavalry → archer → infantry. Archers defending their own city tile have ×{C.ARCHER_CITY_DEFENSE:g} strength.

**Recruit** in a city you own (`recruit`, max {C.MAX_RECRUIT_PER_ORDER} per order); units appear at the end of the turn.

**Move** units from a tile along a path of 1 step (2 steps if every moved unit is cavalry). You may not enter impassable tiles,
tiles owned by a treaty partner or tiles holding a partner's army; the first step of a 2-step path may not hold a hostile army.
A stack can be split with several move orders (the total per unit type can't exceed what is there). Moving onto unowned land does **not** claim it.

**Combat power** of side X against side Y:

    power = (Σ_t count_t · strength_t · m(t, Y) + garrison) · terrain · walls

* m(t, Y) = Y-unit-count-weighted average of the counter multiplier (×{C.COUNTER_MULTIPLIER:g} vs the unit type t counters, else ×1).
* terrain = ×{C.TERRAIN_DEFENSE_BONUS} for a side that started the turn on a forest/hills tile it still occupies (defender).
* walls (city owner defending its city only) = 1 + {C.WALL_BONUS_PER_LEVEL}·max(0, L − siege_count/{C.SIEGE_PER_WALL_LEVEL}) where L = wall level, siege_count = attacking siege units. Each {C.SIEGE_PER_WALL_LEVEL} siege cancel one wall level.
* Siege units count ×{C.SIEGE_CITY_ATTACK} strength when attacking a city.
* **Garrison**: every city has an intrinsic garrison of {C.GARRISON_CITY} strength ({C.GARRISON_CAPITAL} for an original capital) on its owner's side (multiplied by walls and terrain). An undefended city still fights.

**Battle procedure** (deterministic):

1. *Border clash*: if your units move X→Y while hostile units move Y→X, the two moving groups fight first (no terrain, walls or garrison; ties destroy both). Survivors continue.
2. All moves land. On each tile with hostile sides, sides are sorted by raw power (Σ count·strength, + garrison) ascending; the weakest side fights the weakest side hostile to it; the winner (with losses) re-enters the queue; repeat until no hostile pairs remain.
   Ties: the defender (a side that was on the tile at the start of the turn, or the city owner) wins; otherwise both are destroyed.
3. Duel with powers Pw > Pl: the loser is destroyed; the winner loses round(count · (1 − sqrt(1 − (Pl/Pw)²))) of each unit type (Lanchester square law):
""")
    rows = []
    for ratio in (0.25, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0):
        frac = 1 - math.sqrt(1 - ratio * ratio)
        rows.append([f"{ratio:g}", f"{frac * 100:.0f}%", lanchester_losses({"infantry": 10}, ratio, 1.0).get("infantry", 0)])
    add(_table(["Pl/Pw", "winner loses", "of 10 units"], rows))
    add(f"""
4. **Capture**: after the battles, if every unit on a tile belongs to one player and the tile is owned by a player hostile to them, the tile changes owner.
   A city is captured only if its garrison was defeated. On city capture: walls drop one level; the victim's tiles in radius 1 (without other players' units) transfer;
   a wonder there is destroyed; and if it was the victim's **original capital**, the captor plunders {_pct(C.PLUNDER_FRACTION)} of the victim's food, wood, stone and gold.
   Relic tiles are captured like any tile.

**Disband** `{{"type":"disband","at":[x,y],"units":{{...}}}}` removes your units (no refund) — useful to cut upkeep.

## 9. Diplomacy

* **Treaties**: `propose_treaty {{to, turns ({C.TREATY_MIN_TURNS}–{C.TREATY_MAX_TURNS})}}`; the target may `accept_treaty {{from}}` on the **next** turn only
  (pending proposals to you are in `treaty_proposals`). A treaty signed on turn t with `turns` k lasts until the end of turn t+k (`until_turn`).
  While active the two players cannot move onto each other's tiles or armies and never fight.
  `break_treaty {{with}}` ends it immediately, costs {C.TREATY_BREAK_COST} influence and increments your public `betrayals` counter; movement restrictions still apply during that turn and lift on the next.
* **Trades**: `offer_trade {{to, give, want}}` (tradable: {", ".join(C.TRADABLE)}). The recipient sees it next turn in `trade_offers` and may `accept_trade {{offer_id}}` until `expires_turn`
  (offers last {C.TRADE_OFFER_TTL} turns). It executes only if both sides can pay at that moment. Offers are private to the two parties.
* **Messages**: `message {{to: "p2" | "all", text}}` (≤ {C.MAX_MESSAGE_LENGTH} chars, ≤ {C.MAX_MESSAGES_PER_TURN} per turn). Private messages are visible only to sender and recipient; they arrive next turn.
  Messages are cheap talk — nothing is binding except treaties.

## 10. Victory

The game ends at the end of the turn in which a player meets any condition, or after `max_turns` (default {C.DEFAULT_MAX_TURNS}).
If several players meet a condition on the same turn, the one with the highest score wins.
""")
    add(_table(["condition", "requirement"], [
        ["conquest", f"own ≥ ceil(n/2) original capitals (all of them if n ≤ {C.CONQUEST_SMALL_GAME}; your own counts), or be the last player standing"],
        ["wonder", f"complete wonder stage {C.WONDER_VICTORY_STAGE}"],
        ["influence", f"influence ≥ {C.INFLUENCE_VICTORY}"],
        ["relics", f"own ≥ floor(R/2)+1 relic tiles at {C.RELIC_VICTORY_TURNS} consecutive turn ends"],
        ["economic", f"gold ≥ {C.ECONOMIC_VICTORY_GOLD}"],
        ["score", "highest score when max_turns is reached"],
    ]))
    add("\nThresholds by player count:\n")
    add(_table(["players", "map", "capitals for conquest", "relics (R)", "relics needed"], [
        [n, f"{map_size(n)}×{map_size(n)}", conquest_capitals(n), relic_count(n), relics_needed(relic_count(n))]
        for n in range(2, C.MAX_PLAYERS + 1)]))
    sw, sd = C.SCORE_WEIGHTS, C.SCORE_DIVISORS
    add(f"""
**Score** = {sw['tiles']}·tiles + {sw['cities']}·cities + {sw['capitals_held']}·capitals_held + {sw['wonder_stage']}·wonder_stage + floor(influence/{sd['influence']}) + floor(gold/{sd['gold']}) + {sw['relics_held']}·relics_held + floor(military_power/{sd['military_power']}),
where military_power = Σ count·strength of your units.

**Placements**: winner first; then surviving players by score; then eliminated players, latest-eliminated first.
Each player's `victory_progress` (0–1 per condition) shows how close everyone is — watch your rivals and react before they win.

## 11. Orders reference

Every order is a JSON object with `"type"`; coordinates are `[x, y]`.

```json
{{"type":"move","from":[3,4],"path":[[4,4]],"units":{{"infantry":2,"archer":1}}}}
{{"type":"move","from":[3,4],"to":[4,4]}}                       // "to" = 1-step path; omit "units" to move everything
{{"type":"move","from":[3,4],"path":[[4,4],[5,4]],"units":{{"cavalry":3}}}}   // cavalry only
{{"type":"recruit","city":[3,4],"unit":"cavalry","count":2}}
{{"type":"build","at":[5,4],"building":"farm"}}                  // farm, lumber_mill, quarry, mine, temple
{{"type":"build","at":[3,4],"building":"walls"}}                 // city: walls, warehouse, market_hall, wonder
{{"type":"claim","at":[6,4]}}
{{"type":"settle","at":[9,9]}}
{{"type":"disband","at":[3,4],"units":{{"infantry":1}}}}
{{"type":"market","side":"buy","resource":"stone","qty":40,"limit":2.5}}
{{"type":"offer_trade","to":"p2","give":{{"wood":50}},"want":{{"gold":40}}}}
{{"type":"accept_trade","offer_id":"t7"}}
{{"type":"propose_treaty","to":"p3","turns":20}}
{{"type":"accept_treaty","from":"p3"}}
{{"type":"break_treaty","with":"p3"}}
{{"type":"message","to":"all","text":"Peace with anyone who stays out of the east."}}
```

Orders are checked when submitted (malformed/impossible ones are returned as `{{"index", "error"}}` and dropped),
and again when executed (e.g. resources are only checked then) — execution failures appear as `order_failed` events next turn.

## 12. The state view (what you see)

* `turn`, `max_turns`, `status`, `deadline`, `season` {{name, turns_left, modifiers, next}}.
* `you`: resources, caps, income, upkeep, claim_cost, settle_cost, market_fee, capital.
* `players[]`: public stats of everyone (resources, income, cities, tiles, units, military_power, wonder_stage, relics_held, relic_streak, betrayals, score, victory_progress, submitted).
* `map`: width, height, terrain rows, owner grid, improvements, deposits, relics.
* `cities[]` (walls, warehouse, market_hall, wonder_stage, garrison), `armies[]` ({{x, y, owner, units}}).
* `market`: fee, prices, pools, history (last {C.MARKET_HISTORY_TURNS} turns).
* `treaties`, `treaty_proposals` (to/from you), `trade_offers` (to/from you), `messages` (public + yours, last {C.MESSAGES_IN_VIEW}), `events` (last turn).
* `victory`: thresholds and, when finished, the result. `costs`: all rule constants.

## 13. Strategy hints

* **Economy first.** Early claims and improvements compound: improvements pay for themselves within ~15–20 turns, so build them early. Keep influence flowing for claims (temples, relics).
* **Plan for winter** (food ×{C.SEASONS[3][1]['food']:g}): stockpile food in summer, don't overbuild armies you can't feed, and remember the storage cap — spend or build a warehouse instead of wasting overflow.
* **Use the market both ways.** Sell what you overproduce, buy bottlenecks (stone for walls/wonder). Limits protect you from bad prices; the price is shared, so a crowd buying the same thing gets expensive.
* **Watch victory_progress** of every player. Wonder, influence and economic wins can be raced; conquest, relics and wonders can be stopped by force (capturing a wonder city destroys it).
* **Defence is efficient.** Garrison + walls + terrain + archers make cities expensive to take; siege engines cancel walls. Attack with counters (infantry vs cavalry, cavalry vs archers, archers vs infantry) and with overwhelming force — Lanchester losses make lopsided fights cheap for the winner.
* **Diplomacy** lets you secure a border while you race elsewhere. Treaties are enforced by the engine; breaking one costs influence and your reputation (`betrayals` is public).
* **Always submit** every turn — missing a deadline means doing nothing.
""")
    return "\n".join(parts).rstrip() + "\n"


def default_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "docs", "RULES.md"))


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    text = render()
    if "--stdout" in argv:
        sys.stdout.write(text)
        return 0
    path = default_path()
    for a in argv:
        if not a.startswith("--"):
            path = a
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
