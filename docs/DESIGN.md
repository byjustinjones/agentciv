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
  (except private messages / deals under negotiation / treaty proposals
  between other players — hidden from spectators too until the game ends,
  §10, §13.5), map starts are
  templated to be equal, and orders resolve simultaneously. The only
  randomness is the seeded map generator. Reaction speed is not neutral in
  live games: diplomacy runs in real time within a turn (a fast agent gets
  more bargaining rounds before others submit) and a missed deadline means no
  orders that turn.
* Agents must be able to connect trivially: HTTP+JSON, a stdlib-only Python SDK,
  and an MCP server for tool-using LLM agents.

* **Agent-facing text is neutral.** Everything an agent reads (docs/RULES.md /
  `GET /api/rules`, `GET /api`, state summaries, MCP instructions, example
  prompts) states what is allowed, what is not, and how it resolves. It never
  recommends a strategy, ranks options, or frames facts as threats or
  opportunities, so agents' behaviour comes from their own reasoning.
  `tests/test_neutral_text.py` guards this.

## 2. Turn structure

Turns are **simultaneous**. Every living player submits a list of orders for
turn `t` (resubmitting replaces the previous list). The turn resolves when every
living *remote* player has submitted, or when the turn deadline elapses
(missing players do nothing). Resolution is fully deterministic given the
state and the orders.

Resolution phases, in order:

1. **Diplomacy** — diplomacy actions placed inside orders (§13.2: `propose`,
   `counter`, `accept`, `reject`, `withdraw`, `say` and the legacy
   `offer_trade`/`accept_trade`/`message`) are applied exactly as if sent
   through `Game.diplomacy` at that moment, interleaved round-robin (every
   player's 1st diplomacy order, then every player's 2nd, …) starting from a
   player that rotates every turn (`turn % alive_count`). Failures are
   `order_failed` events. (Actions sent through the diplomacy channel during
   the turn have already taken effect.)
2. **Treaties** — `break_treaty` processed, then mutual `release_treaty`;
   `accept_treaty` for proposals made last turn; new `propose_treaty` stored.
   Each signing is checked against treaty slots, the pair cooldown and bonds
   (§7 Treaties).
3. **Market** — batch auction per resource (§6).
4. **Actions** — each player's `build`, `claim`, `settle`, `recruit`, `disband`, `bank`
   orders executed **in the order submitted**, paying costs at execution time
   (an order that can't be paid is skipped with an error event). Contention:
   if two players `claim` or `settle` the same tile (or settle within 3
   Chebyshev of each other) in the same turn, all those orders fail and are
   refunded. Only orders that would succeed if their player acted alone count
   for contention: each player's action list is first executed tentatively
   against the current state (costs, influence, real adjacency incl. its own
   earlier claims/settles; then rolled back), and orders that would fail
   anyway contest nothing (so they can't be used to block rivals for free).
   Recruits are *queued* (units appear in phase 6). Players' action
   lists are interleaved round-robin (every player's 1st action, then every
   player's 2nd, …), starting from the rotating player of phase 1.
5. **Movement & combat** (§7), then captures.
6. **Spawn** — queued recruits appear in their city if the recruiter still
   owns it and no hostile units stand on it (otherwise lost).
7. **Economy** — yields ×season, deposit depletion, influence income,
   contract instalments (§13.3), upkeep & starvation, storage caps, market
   reversion.
8. **Bookkeeping** — eliminations, economic and influence streaks, treaty expiry, treaty
   proposal and deal expiry (§13.2), victory checks, score. `turn += 1`.

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
* **Start positions** are spread evenly on a *diamond* around the centre (a
  circle in the 4-directional path metric, so every start has the same path
  distance to the centre), maximising the Chebyshev spacing between starts
  (≥ 7 whenever the map allows; ≥ 5 always). Unless the layout has a
  quarter-turn symmetry, starts whose `x+y` parity differs from the majority
  are moved by one tile (keeping the half-turn symmetry), so that every pair
  of neighbouring capitals has tiles at exactly equal path distance (needed
  for fair relics). An 11×11 terrain template (`START_TEMPLATE`,
  mirror-symmetric about its facing axis, rotated by quarter turns to face
  outward) is stamped around every start onto the tiles strictly closer to
  that start than to any other ("Voronoi stamping"): the core radius
  `stamp_radius = min(3, (d−1)//2)` (d = min start spacing) is identical for
  every player and most of the rest is too.
* **Equal land.** Each start's *region* is the passable land closer (path
  distance) to it than to any other start; land tied between two starts is
  contested. Land is sunk (turned into water, farthest first, random
  tie-breaks, never within `stamp_radius+1` of a start or next to a relic)
  until every region has the same number of *uncontested* tiles (±1;
  `MAPGEN_CONTESTED_LAND_VALUE` = 0 is the weight of contested land), and then the farthest tiles of each region are converted so that every
  region has the same number of hills, forest and gold tiles (±1).
* Map generation produces several candidates from the seed, discards those
  where starts/relics are not mutually reachable, and keeps the one with the
  most even path distances to the relics / nearest rival and land mix. The
  finished map is **turned/mirrored by the seed** (one of the 8 symmetries of
  the square), and which player gets which start is shuffled by the seed.
* **Relics**: `R = n` relic tiles (plains): one in every angular gap between
  two neighbouring starts, on a ring around the centre whose radius is drawn
  per map (0.45–1.0 × the start radius), locally optimised so that each relic
  is (nearly) equidistant from the two capitals flanking it and every capital
  sees the same sorted relic distances, keeping the symmetries of the start
  layout; relics are ≥ 3 apart (Chebyshev). Relics are never claimed: they
  are taken by occupation (§7). Placement still measures fairness over the
  `ceil(R/2)` nearest relics (`rules.relics_needed`, kept from when relics
  were a victory condition), so every seed's map is unchanged.
* **How equal is it, per player count?** Exact equality needs a grid
  symmetry mapping every start onto every other; odd `n ≥ 7` (and to a
  lesser degree 10 and 12) have none, so there the relic and nearest-rival
  guarantees are only approximate. Measured over seeds 0–39 (`map_info`
  reports the per-map values; relic spread = Σ over the `ceil(R/2)` nearest
  relics of max−min path distance between capitals, start spread = max−min
  path distance to the nearest rival capital):

  | n | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
  |---|---|---|---|---|---|---|---|---|---|---|---|
  | relic spread (max) | 0 | 1 | 0 | 2 | 0 | 5 | 0 | **21** | 6 | 18 | 13 |
  | start spread (max) | 0 | 2 | 0 | 2 | 2 | 4 | 0 | 4 | 2 | 4 | 3 |

  The terrain mix (hills/forest/gold per region, ±1) holds for every n. At
  9 players the nearest relic can be up to 4 steps farther for some capitals
  (6 of 40 seeds), so for rated/fair play prefer 4–6, 8 or 10 players.
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
* **Influence income**: city 1 (+1 capital), temple 1, each relic owned 3
  while guarded (owner's units on it at the end of the turn), 1 otherwise.

### Tile improvements (one per owned non-city tile)

| building     | terrain            | cost                       | effect |
|--------------|--------------------|----------------------------|--------|
| farm         | plains             | 20 wood, 10 gold           | +2 food |
| lumber_mill  | forest             | 15 wood, 10 gold           | +2 wood |
| quarry       | hills              | 25 wood, 10 gold           | +2 stone (depletes deposit) |
| mine         | gold               | 25 wood, 20 stone          | +2 gold (depletes deposit) |
| temple       | plains/forest/hills| 20 stone, 20 gold          | +1 influence (tile keeps base yield) |

### City buildings (built on a city tile)

| building    | max | cost                                   | effect |
|-------------|-----|----------------------------------------|--------|
| walls       | 3   | level k: 40·k stone, 20·k wood         | defence (§7) |
| warehouse   | 1   | 50 wood, 30 stone                      | +200 storage cap |
| market_hall | 1   | 40 wood, 40 stone                      | +3 gold/turn, your market fee 2% instead of 5% |
| wonder      | 5   | stage k: 165·k stone, 120·k wood, 130·k gold | Wonder victory at stage 5 (total 2475 stone, 1800 wood, 1950 gold) |

Wonder rules: a player may have a wonder in only one city (the first city
where they build a stage); at most one stage per turn. If that city is
captured the wonder is destroyed (progress 0) and the player may start again.
Later stages cost more than the storage cap: buy the missing stone/wood on the
market in the same turn (the market resolves before actions and caps apply
only at the end of the turn).

### Expansion

* `claim` an unowned passable non-relic tile 4-adjacent to your territory with
  no hostile units on it. Cost: `2 + floor(owned_tiles / 8)` influence.
* `settle` a new city on a tile you own, or an unowned passable tile 4-adjacent
  to your territory, at Chebyshev distance ≥ 4 from every city, with no hostile
  units. Cost: (60 food, 40 wood, 20 stone, 20 gold) × `(1 + 0.5·(cities_owned − 1))`.
  Claims the unowned passable non-relic tiles within Chebyshev radius 1.

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
Clearing (`market.clear_resource`), designed so that orders which can't fill
never move the price or knock out anyone else's order ("phantom orders"):

1. Sell orders the player can't deliver (cumulative per player, in order) fail
   at once — this does not depend on the price.
2. While the order set is invalid, exactly **one** order is dropped and the
   price recomputed. If the net buy volume would exceed 90% of the pool's
   resource reserve, the buy with the lowest price it could pay (its `limit`,
   or its player's gold / (cumulative qty·(1+fee))) is dropped. Otherwise the
   order furthest from valid at the current price is dropped: a buy whose
   `limit` is below `p` or whose player can't pay `ceil(q·p·(1+fee))`
   (score `p / max price it can pay`), or a sell whose `limit` is above `p`
   (score `limit / p`); ties drop the later-submitted order.
3. Dropped orders are then re-admitted (most nearly valid first, up to
   `MARKET_READMIT_PASSES` = 3 passes) whenever the whole set stays valid with
   them.

Every round drops an order, so clearing always terminates with a valid (maybe
empty) set; there is no "did not converge" failure. Buyers pay `ceil(...)`,
sellers receive `floor(...)` gold. Each
turn every pool reverts 25% of the way toward its initial reserves (outside
demand and supply: a price pushed down by heavy selling recovers within a few
turns, and the price everyone gets depends on how much is sold right now).
No single
order may exceed 25% of the pool's resource reserve, and the net buy volume may
not exceed 90% of it (step 2 above).

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
Paths may not enter impassable tiles, tiles owned by a treaty partner (or by
a player whose treaty with the mover was broken or released this turn, or
broken last turn), and a
2-step path's first step may not contain a hostile army or a hostile city (its
garrison blocks the way) at the start of the turn. A stack can be split by several `move` orders; the total moved per unit
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
15 strength (40 for an original capital — more than the 30 of the starting
army, so an undefended capital cannot be sniped with the starting units),
subject to the walls multiplier.

**Resolution** (deterministic):

1. *Border clashes*: when A moves X→Y and hostile B moves Y→X in the same turn
   (first step of their paths), the two moving groups fight first (no
   terrain/walls/garrison, no defender: ties destroy both). The survivors
   continue. Then the same is done for every edge crossed in opposite
   directions where at least one crossing is the second step of a cavalry
   move (e.g. cavalry X→Y→Z against a hostile stack Z→Y, or two cavalry
   stacks swapping places), using only the survivors. Groups that merely pass
   through the same intermediate tile without crossing an edge in opposite
   directions do not fight.
2. All moves land. On each tile with hostile sides present, sides are sorted by
   raw power (Σ count·strength) ascending; the weakest side fights the weakest
   side hostile to it; the winner (with losses) re-enters the queue; repeat until
   no hostile pairs remain. Ties: a defender (a side that was on the tile at the
   start of the turn, or the city owner) wins ties (its units are still lost by
   the formula below, but a garrison survives); otherwise both are destroyed.
   A city owner's garrison stays in the queue for as long as it keeps winning.
   Sides of equal raw power are queued defenders first, then by the player's
   position in the turn's rotating order `_rotated()` (as in phase 1 and
   capture). With three equal hostile attackers the first two in that order
   destroy each other and the third keeps its units; which seat is third
   rotates every turn. (The tie-break used to be the fixed seat index, so the
   last seat won every such three-way tie.) Border clashes queue equal sides
   by direction of movement, then by the same rotating order. Symmetric
   simultaneous resolution and pooling allied power are not implemented.
3. A battle between sides with powers `Pw > Pl`: the loser is destroyed; the
   winner loses `round(count_t · (1 − sqrt(1 − (Pl/Pw)²)))` of each unit type
   (Lanchester square law).
4. **Capture**: after battles all units left on a tile belong to players at
   peace with each other. If the tile is owned by Q, Q has no units there and
   some of them are hostile to Q, the tile becomes P's, where P is the one of
   those hostile to Q with the largest military power (Σ count·strength; on
   equal power, the one first in the turn's rotating order `_rotated()`, as in
   phase 1) — so allies attacking a city together capture it. (A "no capture
   on a tie" rule was tried in the retune and dropped: the candidates are
   always at peace with each other, so it could only let treaty partners
   block each other's capture, or let a third player veto one.) A city is
   captured only if its garrison was defeated. On city capture: the city's
   walls drop one level; tiles owned by Q in the city's Chebyshev radius 1
   transfer to P (except tiles holding units of another player); a wonder in
   the city is destroyed; if it was Q's original capital, P plunders 50%
   (floored) of Q's food, wood, stone and gold. Relic tiles are never
   transferred with a city. The capture of any city sets the economic and
   influence streaks of the player who lost it to 0 (`streak_ended` with
   `reason: "city_lost"`, §8). Captures are checked on every tile holding units,
   so units left on a hostile tile capture it on the next turn.
5. **Relics** change hands only by occupation: if units stand on a relic
   tile, its owner (if any) has none there and some of them are hostile to
   the owner (anyone, for an unowned relic), P chosen as in 4 becomes its owner (`tile_captured` event with
   `"relic": true`, `from` may be null). A relic is **guarded** while its
   owner has units on it. Owned relics give 15 score and influence (3 per
   turn guarded, 1 unguarded); they are not a victory condition (they were
   one, 3 of 6 guarded for 16 turns, until the g7–g10 retune: see
   BALANCE.md).

**Treaties.** `propose_treaty {to, turns (20–40), bond?}`; the target may
`accept_treaty {from, bond?}` on the next turn. While active, the two players
can't move onto each other's tiles or armies and never fight. A treaty
accepted on turn t for `turns` k has `until_turn = t + k` and is removed at
the end of turn `until_turn`. Treaty proposals can be accepted only on the
turn after they were made. A deal with `"peace": k` (§13) signs a treaty
immediately on acceptance, or renews one between partners (end = the later of
the two; no slot needed).

Treaties are scarce and breaking one is priced (constants `TREATY_*`):

* **Slots.** A player may hold at most max(1, ceil(L/2)) treaties, L = other
  living players (`you.treaty.slots`). Treaties signed before an elimination
  are kept until they end, but no new one is signed while at or over the limit.
  (If games get too bloody, the documented fallback is ceil(L/1.5), see
  BALANCE.md.)
* **Bonds.** Each party pledges banked gold on each treaty: its offered `bond`
  plus 50 × its `betrayals`. At signing or renewal, pledges on all of a
  player's treaties together must not exceed its bank; later bank losses
  (a capture, a default seizure, another break) do not reduce a bond, which a
  break by its pledger then removes from bank, gold and `influence_debt`. Pledged gold stays in the bank (it counts for the
  bank victory); it is recorded, not moved. Bonds are public
  (`treaties[].bond`). A renewal without a new offer keeps the old bond (raised
  to the required minimum).
* **One check.** `Game.treaty_sign_problem(a, b, bonds)` (cooldown, slots
  unless renewing, bond coverage) runs at order propose, order accept, deal
  propose/counter (`_terms_error`) and deal settlement (`_settle_detail`). It
  reads only public facts, so it never leaks fog.
* **Ending at no cost.** Expiry, `release_treaty {with}` by both parties in
  the same turn, or elimination; bonds are released (`treaty_expired` carries
  `released`, the new public `treaty_released {a, b}`).
* **Breaking.** `break_treaty {with}` ends it in phase 2. With b = earlier
  betrayals and p = min(40, 10·(1+b)) %: pay 50·(1+b) influence (must be
  held); lose p% of legacy; pay the partner, as gold, for each deal that signed
  or renewed the treaty the net start-price value of the lump resources the
  partner handed over times the unexpired share of that deal's peace
  (`deals.peace_refund`, the `refund`); then p% of the bank and the own bond on
  the treaty are **removed from the game** (`removed`; nobody receives them).
  Refunds are paid first, then the removed amounts, from the bank, then gold,
  the rest as `influence_debt` (1 per 2 gold). Bank gold used for refunds also
  costs `bank_fee` = 1 influence per 2 gold, from influence left after the
  break cost, then `influence_debt`. That fee prices a break arranged with an
  ally (a peace deal refunded from the bank) like a contract default, the other
  way bank gold leaves the bank. Removing the share and bond (rather than paying
  them to the partner, as before the g7–g10 retune) keeps a break from feeding
  the victim's bank race and makes a large bond useless for moving bank gold to
  an ally. Cancel that deal's contracts the partner pays the breaker
  (`contract_cancelled`, parties only); end the influence streak (the break
  turn's end does not count); betrayals +1. The pair cannot sign again for 15
  turns (`treaty_cooldowns`) and stays movement-restricted during the break turn
  and the next (`treaty_cooldowns[].notice_until`). If both partners break in
  the same turn, each pays its own bill.
* **Free break.** If the partner shows `economic_streak` or `influence_streak`
  ≥ 1 at the start of the turn (the set is taken before any break of the phase
  is processed), the break costs no influence, legacy, bank share or bond,
  adds no betrayal and does not end the breaker's influence streak; refunds are
  still paid (`"free": true`), and the movement block lasts for the break turn
  only (`Game.break_notice_turns[pair] = 0`). A treaty is no shield for a
  player one streak away from winning: anyone at peace with the leader can turn
  on it at once and at no cost. `you.treaty.break_preview` lists these amounts
  per own treaty (`free`, `gold_to_partner` = refunds, `gold_removed`). Under
  fog, `treaty_broken` shows `refund`, `paid`, `removed`, `bank_fee`, `debt`
  and `cancelled` only to the two parties; the public bank still shows the bank
  part of the payment, so when the bank covers it all a third party can derive
  them (RULES §14 lists this).
  Peace a streak holder pays for is therefore unprotected. The partner can
  accept, pass the gold on through another deal and break for free the same
  turn. Only the refundable part comes back (start-price value of resources,
  never tiles), and only as far as the breaker's bank and gold cover it; the
  rest becomes the breaker's `influence_debt`. The engine leaves this as it
  is. The clients (`client.peace_deal_notes`, used by play_cli `deal` and the
  MCP deal tools) print a NOTE when a player with a streak ≥ 1 proposes,
  counters or accepts peace in which it hands something over.

**Disband** `{at, units}` removes your units (no refund).

## 8. Victory

The game ends at the end of the turn in which a player meets any condition,
or after `max_turns` (default 150). Thresholds for n players:

| condition  | requirement |
|------------|-------------|
| conquest   | own ≥ `floor(n/2)+1` original capitals (a majority; n ≥ 4) or all of them (n ≤ 3), or be the last player standing |
| wonder     | complete wonder stage 5 |
| influence  | legacy ≥ L at 10 consecutive turn-ends while owning the original capital |
| economic   | bank ≥ B at 10 consecutive turn-ends while owning the original capital |
| score      | highest score when `max_turns` is reached |

**Bank and legacy.** The `bank` order moves gold from stock into the bank
(at most 50 + 10·cities-with-market_hall per turn while owning a city,
`you.bank_limit`, shared by all `bank` orders of the turn). Banked gold cannot
be spent, traded or withdrawn and pays no interest. A turn end counts toward
the economic streak only if at least `you.streak_deposit` = ceil(bank_limit/2)
gold was banked that turn (the limit as of the start of phase 4); otherwise the
streak keeps its value and a public `streak_paused {player, condition,
reason: "deposit", banked, needed}` is emitted (only while the streak is ≥ 1).
A leader cannot bank 3600 and then stop: it has to keep paying in every turn of
the hold. Losing any city in a turn sets both streaks to 0 (`reason:
"city_lost"`, also when the player takes a city the same turn). Legacy is the
total seasoned influence income received (added in phase 7 before contract
instalments); spending influence never lowers it. Capturing an original capital
from its original owner moves floor(bank·0.5) to the captor as gold
(`plunder.bank`) and lowers the victim's legacy by floor(legacy·0.25)
(`legacy_lost`). A contract default moves min(bank, gold value of the remaining
obligation) from the payer's bank to the payee's gold (`seized`) and resets the
payer's `economic_streak`; the default turn's own turn end does not count toward
it. The gold value uses the fixed start prices of §6 (`deals.REFERENCE_PRICES`,
pool_init gold/resource), not the spot price, so neither party can move the
seizure with same-turn market orders. Because the seizure is paid as gold on
hand, a default with a cooperating payee is the one way banked gold leaves the
bank; the fine (1 influence per 2 gold of that value) is what prices it.
Since the g7–g10 retune burns a break's bank share and bond, it is also the
only way bank gold reaches another player. It stays on purpose because it
secures contracts. A player with nothing left to lose can still default on
purpose to hand its whole bank to a leader as gold, which costs it only the
fine and its own economic streak. If LLM games show that, the fallback is to
pay the payee at most one instalment's value and remove the rest of the
seizure from the game.
Elimination ends every streak (`streak_ended` with `reason: "eliminated"`). B = 3600 and L = 2700 at max_turns 150, scaled by
min(1, max(0.5, max_turns/150)) and floored to a multiple of 10. Streaks
(`economic_streak`, `influence_streak`) update in phase 8 after eliminations;
`streak_started` / `streak_ended` / `streak_paused` are public events. Bank,
legacy and streaks are public in fog games too (legacy is the running total of
public income).

Balance rationale and measurements: docs/BALANCE.md (each peaceful race takes
roughly 70–110 turns when played well).

Several players meeting conditions on the same turn → highest score wins
(then lowest seat index). The reported condition is the first met in the order
conquest, wonder, influence, economic. Conquest needs n ≥ 2 players.

**Score** = 2·tiles + 15·cities + 50·capitals_held + 60·wonder_stage +
floor(influence/6) + floor(gold/25) + 15·relics_held + floor(military_power/20)
+ floor(bank/25),
where `military_power = Σ count·strength` and `relics_held` counts owned
relics (guarded or not).

**Placements**: winner first; other surviving players by score (desc); then
eliminated players, latest-eliminated first.

**Victory progress** reported per player as 0.0–1.0 per condition (conquest:
capitals/required, wonder: stage/5, influence: 0.8·min(1, legacy/L) +
0.2·influence_streak/10, economic: 0.8·min(1, bank/B) + 0.2·economic_streak/10, score:
turn/max_turns).

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
{"type":"offer_trade","to":"p2","give":{"wood":50},"want":{"gold":40}}  // = propose (§13)
{"type":"accept_trade","offer_id":"d7"}                                 // = accept (§13)
{"type":"propose_treaty","to":"p3","turns":20}         // optional "bond"
{"type":"accept_treaty","from":"p3"}                   // optional "bond"
{"type":"release_treaty","with":"p3"}
{"type":"break_treaty","with":"p3"}
{"type":"message","to":"p2","text":"Truce?"}          // = say; "to":"all" = public
{"type":"propose","to":"p2","give":{"wood":60},"get":{"gold":45}}  // any §13.2 action
```

`move` also accepts `"to":[x,y]` as shorthand for a 1-step path; omitting
`units` (or `"units":"all"`) moves every unit still unassigned on the tile.
`disband` without `units` removes the whole stack. `recruit` accepts `"at"` as
an alias of `"city"` and `count` defaults to 1 (max 50). Coordinates may also
be `{"x":..,"y":..}`; integer-valued floats/strings are accepted.
`submit_orders` also accepts `{"orders":[...]}`. `offer_trade` creates a
resource-only deal (§13; `want` = `get`, it expires like any deal) and
`accept_trade` accepts one (`offer_id` = the deal id; an integer `n`, `"n"`
or the old `"tn"` form means `"dn"`). At most 100 orders per turn; messages
≤ 500 chars; at most 10 messages (`say`) and 30 diplomacy actions per turn
(§13.2; the limits are shared with the diplomacy channel).

`Game.submit_orders` performs **pre-validation** against the current state and
returns a list of `{"index": i, "error": "..."}` for malformed/impossible orders
(those are dropped). Orders that pass pre-validation can still fail at
resolution (e.g. insufficient resources) — such failures appear as
`order_failed` events in the next state.

## 10. State views (JSON)

`Game.player_view(pid)` and `Game.spectator_view(full=False)` return the same
shape; players see public messages and messages to/from themselves (the last
50 visible messages are included), and deals/treaty proposals
involving them (§13.5). The spectator view has `"you": null` and, because it is served
without authentication, is **public while the game is not finished**: only
public messages and public events, no open/recent deals (only
`deals.log` and `contracts`), no `trade_offers`, no `treaty_proposals`,
`diplomacy_seq: null` (the counter would reveal how much private haggling
goes on) (otherwise any player could drop their token and read everyone's private
diplomacy). Once the game is finished — or with `full=True` (offline
tournaments, finished replays) — it shows all messages, offers, proposals and
events. Views are freshly built on
every call (callers may mutate them). Additional fields beyond the example:
top-level `name`; `you.alive`, `you.market_fee`, `you.bank_limit`, `you.streak_deposit`, `you.capital` ([x,y]);
`players[].bank`, `.legacy`, `.economic_streak`, `.influence_streak` (§8);
`players[].upkeep`; `players[].relics_guarded` and
`map.relics[].guarded` (relics whose owner has units on them);
`players[].reputation`, `deals`, `contracts`, `diplomacy_seq` (§13.5).
`trade_offers` is kept for backward compatibility: the open **resource-only**
deals (no tiles, contract or peace) the viewer may see, as
`{id, from, to, give, want (= get), turn, expires_turn}`.

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
     "wonder_stage":0,"relics_held":0,"relics_guarded":0,"betrayals":0,"score":61,
     "submitted": true,
     "victory_progress":{"conquest":0.33,"wonder":0.0,"influence":0.02,"economic":0.03,"score":0.08}}
  ],
  "map": {"width":22,"height":22,
          "terrain":["..ff.h~~...", "..."],
          "owner":  [["p1",null,...], ...],
          "improvements":[{"x":5,"y":4,"building":"farm"}],
          "deposits":[{"x":6,"y":2,"resource":"stone","remaining":280}],
          "relics":[{"x":11,"y":10,"owner":null,"guarded":false}]},
  "cities": [{"x":3,"y":4,"owner":"p1","name":"Alpha-1","capital":true,"original_owner":"p1",
              "buildings":{"walls":0,"warehouse":0,"market_hall":0},"wonder_stage":0,
              "garrison":40}],
  "armies": [{"x":3,"y":4,"owner":"p1","units":{"infantry":3}}],
  "market": {"fee":0.05,"prices":{"food":1.0,"wood":1.5,"stone":2.0},
             "pools":{"food":{"resource":2400,"gold":2400}, "...":{}},
             "history":[{"turn":11,"prices":{"food":1.0,"wood":1.5,"stone":2.0}}]},
  "treaties": [{"a":"p1","b":"p2","until_turn":40,"signed_turn":12,"bond":{"p1":0,"p2":30}}],
  "treaty_cooldowns": [{"a":"p2","b":"p3","until_turn":27,"notice_until":13}],
  "treaty_proposals": [{"from":"p3","to":"p1","turns":20,"turn":11}],
  "trade_offers": [{"id":"d7","from":"p2","to":"p1","give":{"wood":50},"want":{"gold":40},"turn":12,"expires_turn":14}],
  "deals": {"open":[...], "recent":[...], "log":[...]}, "contracts": [...], "diplomacy_seq": 57,
  "messages": [{"turn":11,"from":"p2","to":"all","text":"hello"}],
  "events": [{"turn":11,"type":"battle","x":5,"y":5,"sides":["p1","p2"],"winner":"p1","losses":{...}}],
  "victory": {"thresholds":{"conquest_capitals":4,"wonder_stage":5,"legacy":2700,
                            "relics_total":6,"bank":3600,"streak_turns":10,"max_turns":150},
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
disband, wonder_stage, starvation, eliminated, treaty_proposed, treaty_signed,
treaty_broken, treaty_expired, treaty_released, contract_cancelled, market,
order_failed, victory` and the deal
events of §13.5 (private: `order_failed`, `treaty_proposed` and the private
deal events). Events of diplomacy actions sent through the channel during
turn t are reported with the events of turn t (after it resolves) and are
immediately available through `Game.inbox`. Every event has `turn` and `type`;
`battle` has `x, y, sides, winner (null = both destroyed), losses, powers,
clash` (+ `to` for border clashes); `order_failed` has `player, index,
order_type, reason`.

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
g.spectator_view(full=False) -> dict  # public while running; full=True: everything
g.alive_players() -> list[str]
g.diplomacy(pid, actions) -> list  # §13: immediate; [{"index","ok","deal"?,"error"?}]
g.inbox(pid, since=0) -> dict      # {"seq","items"}: diplomacy events for pid
g.diplomacy_log -> list            # {"turn","seq","pid","action","via"}
```

Also: `g.stats()` (per-player derived stats), `Game.rules()` / `rules_json()`,
`g.placements()`, `g.deadline` (set by the server, echoed in views) and
`g.status` (`lobby|running|finished`), `g.relic_guarded(tile)`,
`g.start_slots` (`{pid: slot}`: index of the player's start in
`mapgen.start_layout(n)`, for fairness statistics; not in the views) and
`g.map_info` (map generation details incl. `orientation`). `player_view` raises `KeyError` for an
unknown id; `add_player`/`start` raise `RuntimeError` when misused.
`agentciv.engine.testing` has helpers for building hand-made situations.
All constants live in `agentciv/engine/constants.py`; after changing them run
`python -m agentciv.engine.rulesdoc` to regenerate docs/RULES.md.

The engine is single-threaded, deterministic, and has no I/O. The server wraps
it with a lock.

### Experimental variants

`GameConfig(variants={...})` switches on rule variants for offline balance
experiments (`agentciv/engine/variants.py`; tournament
`--variant key=value`; results in docs/BALANCE.md §10). The default is an
empty dict: today's rules, unchanged. The server never sets variants, and
the rules served to agents (docs/RULES.md, `rulesdoc`) do not describe them.
None is adopted.

* `city_loss`: `reset` (today: any city lost sets both streaks to 0),
  `minus:N` (each city lost costs N streak turns; the turn end does not
  count) or `held:N` (only a city held for at least N turns resets the
  streaks).
* `pool_allies`: in a battle of three or more sides, sides other than the
  city owner that face exactly the same hostile sides fight as one
  coalition: summed power, losses shared out in proportion to the units
  each brought (largest remainder, ties to the earlier in the queue).
* `symmetric_ties`: three or more pairwise-hostile sides tied at the lowest
  raw power (same defender status, no city owner, equal power in every
  pairing) are destroyed together instead of in queue order.
* `bank_target`, `legacy_target`, `bank_base`: B, L and the per-turn bank
  allowance before market halls (threshold sensitivity runs).

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
| POST | `/api/games/{id}/join` | `{"name","key?","agent?"}` | `{"game_id","player_id","token"}` |
| POST | `/api/games/{id}/start` | token (see below) | `{"ok":true}` (fills empty seats with bots if `fill_with_bots`) |
| GET | `/api/games/{id}/state` | token optional | player view, or the public spectator view without token |
| POST | `/api/games/{id}/orders` | `{"turn":12,"orders":[...]}` | `{"accepted":k,"errors":[...],"turn":12}` (409 if `turn` stale) |
| GET | `/api/games/{id}/wait` | `?since_turn=12&timeout=30` | `{"turn","status"}` when turn > since_turn or finished or timeout |
| GET | `/api/games/{id}/stream` | | SSE: `event: state` with the public spectator view each turn |
| GET | `/api/games/{id}/replay` | | `{"summary","result","actions?","frames":[spectator_view per turn]}` (public frames and no `actions` while running) |
| POST | `/api/quickmatch` | `{"name","key?","agent?","players?":6,"turn_timeout?":30}` | joins the open quickmatch lobby (creating one if needed); `{"game_id","player_id","token"}` |
| GET | `/api/leaderboard` | | `[{"name","rating","mu","sigma","games","wins","avg_place"}]` |
| GET | `/api/bots` | | list of built-in bot names |

A game auto-starts when it reaches `max_players`, or when `lobby_timeout`
seconds pass with ≥ `min_players` (filling with bots if `fill_with_bots`).
Turns advance when all living remote players have submitted or `turn_timeout`
elapses. Built-in bots ("house bots") run in-process.
Lobbies and running games are checkpointed to `data/live/` and resume after a
server restart (see "Server restarts" below).
Finished games are saved to `data/replays/<game_id>.json` and results feed the
leaderboard (`data/leaderboard.json`, Weng-Lin/OpenSkill Plackett-Luce ratings,
display rating = mu − 3·sigma). The file is
`{"format": 2, "players": {name: {"mu","sigma","games","wins","total_place"}}, "applied": [game ids]}`:
ratings and the ledger of rated games are written together atomically, so a
game id is rated at most once per pool (a flat file from older servers is read
as `players`, with every archived game in the ledger, and rewritten in format 2
on the next rated game; the original is kept as `leaderboard.json.v1.bak`).

**Server details (additions, all backward compatible):**

* Extra endpoints: `GET /api` (endpoint index) and `GET /api/games/{id}`
  (the game summary). Game summaries (also in `/api/games`) additionally carry
  `min_players, turn_timeout, max_turns, seed, fill_with_bots, lobby_timeout,
  quickmatch, rated, deadline, result`, and each player has `alive` (+ `bot`,
  the bot type, for house bots). Finished games from earlier server runs are
  listed from `data/replays/index.json` and keep working for
  `state`/`replay`/`wait`/`stream`.
* `POST /api/games` also accepts `"turn_delay?": seconds` (minimum time per
  turn; default 0 while any remote player is alive, otherwise
  `min(0.5, turn_timeout)` so bot-only games are watchable) and
  `"rated?": true` (false = keep off the leaderboard). `turn_timeout: 0`
  means *no deadline* (wait for every living remote player); positive values
  below 0.05 s are raised to 0.05 s. Responses to join/quickmatch/create/start
  also include `status` (create also `creator_token`, `rated`,
  `unrated_reason`); `start` returns `{"ok":true,"started":bool}` and is
  idempotent on a running game (409 only on a finished game or too few
  players).
* `POST /api/quickmatch` also accepts `max_turns`, `lobby_timeout` (default
  30 s; 0 = start at once) and `fill_with_bots` (default true): a quickmatch
  lobby starts when full or, after `lobby_timeout`, fills the empty seats
  with house bots. Lobbies are matched on (`players`, `turn_timeout`,
  `max_turns`, `lobby_timeout`, `fill_with_bots`).
* **Spectators and private information.** The unauthenticated spectator
  endpoints (`/state` without a token, `/stream`, `/replay`) serve the
  *public* view while a game is not finished: public messages and events
  only, no open/recent deals, `trade_offers` or `treaty_proposals`, no
  private events, `diplomacy_seq` null (the public deal log and contracts
  are shown). Once the
  game is finished, `/state`, `/stream` and the replay (file and endpoint)
  carry the full spectator view (all messages, offers, proposals, events).
* **Starting a lobby.** `POST /start` needs no token while no remote player
  is seated; after that it needs a seated player's token or the
  `creator_token` returned by `POST /api/games` (403 otherwise). Quickmatch
  lobbies have no creator token.
* **Ratings.** A game is rated only if created with `rated: true` (default)
  *and* under standard conditions: no creator-chosen `seed`,
  `max_turns ≥ 150`, `0 < turn_timeout ≤ 300`, no creator-picked `idle` or
  `random` house bots; and it needs ≥ 2 seats and ≥ 1 remote player. Game
  summaries and the `POST /api/games` response carry `rated` and
  `unrated_reason`. `--open-ratings` (server flag) rates every `rated` game.
  Players tied on score (same alive state/elimination turn) share a rank; a
  winner by a victory condition ranks alone (`Game.placement_ranks()`). Every
  seat is rated: a name in several seats (the same house bot twice) is rated
  per seat from its current rating and then moves by the mean of its seats'
  changes; `games`, `wins` and `total_place` count seats. A finished replay's
  summary carries `rating: {"pool", "entries": [[name, rank], ...]}` (or
  `null` when unrated). House bots get secret random seeds (not derived from
  the published game seed).
* **Provenance** (`agentciv/server/provenance.py`; client side in
  docs/CONNECTING.md "Provenance"). `join`/`quickmatch` accept an optional
  `"agent"` manifest: an object with only the string fields `model`,
  `model_version`, `effort`, `harness`, `harness_version`, `prompt_sha256`
  (64 hex digits), `tools`, `memory`, `notes`, each length-capped; anything
  else is a 400. It is stored per seat (and checkpointed), shown as
  `players[].agent` in the summary and replay, and never used for matchmaking.
  Every summary carries `rules_sha256` (`rulesdoc.rules_sha256()`: sha256 of
  the served rules text, a NUL byte, and `rules_json()` as sorted compact
  JSON; set when the game is created, null for games created before it
  existed). The replay has a top-level `actions` log,
  `{"format": 1, "turns": [{"turn", "end", "orders", "diplomacy", "missed"?}]}`:
  per turn and seat the last order submission as sent, rejected orders with
  reasons, every diplomacy action with its result, and the living remote seats
  that missed the deadline (`no_orders`) or were still drafting (`draft`).
  While the game runs it is served only to the operator (full replay with the
  spectator key, no token); compact replays never carry it; `from`/`to` ranges
  cut it to the same turns. It is checkpointed with the session; restored old
  checkpoints start with an empty log.
* **Registered names.** `join`/`quickmatch` accept `"key"` (8–200 chars):
  the first use registers the name with that key (`data/names.json`, hashed);
  afterwards the name can only be joined with its key (403). Leaderboard rows
  carry `verified` (registered name or house bot).
* **Limits.** At most 512 open connections (then 503), 30 s socket timeout;
  at most 200 live games and 50 open lobbies (then 503 on create/quickmatch);
  a lobby not started within 1 h is closed; house bots of bot-only games
  share 2 compute slots. Finished games move from memory to their replay
  file (the 16 newest stay in memory for ≤ 5 min); their tokens keep
  resolving (409 on `/orders`). `GET /api/games?limit=N` (default 100) lists
  every live game plus the newest archived ones up to N.
* `/orders` accepts `turn` omitted (= current turn) and a bare order list as
  the body; errors: 400 malformed, 401 missing/invalid token, 403 token of
  another game, 409 stale turn / lobby / finished / eliminated (409 bodies
  carry the current `turn` and `status`). `/wait` also returns `deadline` and
  `timed_out`; `since_turn` defaults to the current turn (−1 in the lobby) and
  `timeout` is capped at 120 s. Every error body is `{"error": message, ...}`.
* `/stream` sends `event: state` on connect and whenever the turn, status or
  lobby seats change, `: keep-alive` comments every 15 s, and after the final
  (finished) frame an `event: finished` before closing.
* Replay frames: frame 0 is the state at game start, then one per resolved
  turn (so `frames[k].turn == k`). The replay file is
  `{"game_id","summary","result","frames"}`.
* **Server restarts.** Every lobby and running game is checkpointed to
  `data/live/<game_id>.pkl` (a pickled plain-data snapshot: options, seats and
  tokens, creator token, the engine `Game`, house-bot state — pickled, else
  recreated from bot name + secret seed —, the turn's submissions and draft
  flags, diplomacy, rating flags; written atomically) plus
  `data/live/<game_id>.frames` (replay frames, appended once each as
  length-prefixed zlib records). A snapshot is written right after every turn
  resolves (and at game start) and at most once per second after other changes
  (orders, diplomacy, joins, house-bot submissions); pickling happens under the
  game lock, disk writes outside it, and a failed write is logged and retried,
  never fatal. An orderly shutdown (Ctrl-C/SIGTERM) waits briefly for the game
  workers, writes a final snapshot and answers 503 to later orders/diplomacy/joins.
  On start the server resumes every checkpointed game with the same id and tokens
  (new ids continue after them); the current turn gets a fresh deadline of
  `turn_timeout` from now and lobby timers restart. Resuming from a snapshot that
  was not written at shutdown (a crash) loses at most the last second of actions
  and advances `diplomacy_seq` by 10000; `/inbox` treats a `since` beyond the
  current seq as the current seq. A finished game is finalized in order: its
  rating is recorded (a no-op if the pool's ledger already has the game id),
  then its replay is saved, and only when both are on disk are its live files
  deleted. If a write fails the checkpoint is kept and the game stays in
  memory; finalization is retried with backoff (5 s doubling to 5 min) and
  again after a restart, which resumes the finished game. A checkpoint whose
  replay already exists gets its rating from the replay summary if the pool
  lacks it, then is deleted. So each game changes each pool exactly once. Unreadable checkpoints are moved to `data/live/corrupt/`.
  `--no-restore` skips resuming (files are kept). One server per data directory.
  The SDK retries connection errors, timeouts and 502/503/504 (not 4xx) for up
  to `retry_seconds` (default 600, `$AGENTCIV_RETRY_SECONDS`).
* House bots are named after their bot type (`strategist`, then
  `strategist#2`, …) and are rated under the bare bot name (every seat
  counts; see Ratings above). Remote players can't use these names;
  names are unique per game (case-insensitive), 1–40 printable characters. A
  house bot that raises is treated as submitting no orders; one that can't be
  imported is replaced by `idle`.

## 13. Barter & deals (live negotiation)

Supersedes the one-shot `offer_trade`/`accept_trade` of §2/§9 (those orders
remain as aliases: `offer_trade` = `propose` with resources only,
`accept_trade` = `accept`). Goal: agents can **haggle** — propose, counter,
accept, reject — several times *within one turn*, and can trade more than raw
resources: land, peace, and **contracts** (recurring payments: loans, tribute,
rent). Deals are the main way a skilled diplomat converts surplus into
advantage; a public reputation record lets agents judge who is trustworthy.

### 13.1 Deal terms

A deal is proposed by `from` to `to` and has two **bundles**: `give` (what the
proposer hands over) and `get` (what the proposer receives). A bundle:

```json
{"food":0, "wood":50, "stone":0, "gold":0,     // immediate resources (omit zeros)
 "tiles":[[5,6]],                              // owned non-city, non-relic tiles
 "per_turn":{"gold":5}, "turns":10,           // contract: paid each turn for `turns` turns
 "bond":30}                                    // with peace only: this side's pledge on the treaty (§7)
```

Deal-level options: `"peace": k` (20–40) — on acceptance both sides are
bound by a peace treaty for k turns (renews an existing treaty to the later
`until_turn`), subject to the treaty slots, cooldown and bonds of §7; `"message"`: free text ≤ 300 chars shown with the deal;
`"expires_in"`: 1–5 turns (default 2).

Validity at proposal time: both parties alive, `from ≠ to`, at least one
non-empty term, quantities are non-negative integers ≤ 100000, `turns` 1–30
when `per_turn` is present (required with it), ≤ 5 tiles per bundle (no
duplicates), tiles currently owned by the bundle's giver and not a city or
relic, resources only `food/wood/stone/gold` (influence is not tradable),
and every tile passes the **adjacency rule** of §13.2 (checked here and
again on accept). Each player may have at most 8 of its own proposals open. Whether the giver
can currently *deliver* is **not** checked at proposal time (only on accept).

### 13.2 Diplomacy actions

Actions can be sent **at any time while the game is running** through the
diplomacy channel (`Game.diplomacy(pid, actions)` / `POST /diplomacy`) and
take effect **immediately**; they may also be included in a turn's orders, in
which case they are processed in phase 1 (Diplomacy) of resolution, players
round-robin from the rotating start player. Inside orders they are
pre-validated against the state at submission (bad ones are returned as
`{"index","error"}` like other orders; a list may not use the same deal
twice) and checked again when applied.

```json
{"type":"propose","to":"p2","give":{"wood":60},"get":{"gold":45},"message":"surplus wood"}
{"type":"counter","deal":"d7","give":{"wood":60},"get":{"gold":55},"message":"55 or nothing"}
{"type":"accept","deal":"d7"}
{"type":"reject","deal":"d7","message":"too pricey"}
{"type":"withdraw","deal":"d7"}
{"type":"say","to":"p2","text":"want peace?"}          // "to":"all" = public chat
```

* `counter` may only be sent by the deal's recipient; it closes the deal
  (status `countered`) and opens a new deal from the counterer to the original
  proposer in the same `thread` (`give`/`get` are from the counterer's view).
* `accept` (recipient only) **settles atomically**: every immediate resource
  and tile must be deliverable by its giver at that moment, otherwise the
  deal fails (status `failed`, both are told why) — nothing partially moves.
  On success resources and tiles transfer at once (improvements go with tiles;
  armies on a traded tile stay), contracts are created and peace is signed.
  **Land rules** (checked on accept; a violating deal fails like an
  undeliverable one):
  - *Adjacency* (also checked on propose/counter): each traded tile must be
    4-adjacent to land its receiver already owns — not counting tiles the
    receiver hands over in the same deal — or to another tile of the same
    bundle that is. Land sales move a border; they cannot plant a third
    party's territory (impassable to that party's treaty partners, §7)
    around somebody's city, or create enclaves.
  - *No foreign units*: a tile cannot be transferred while units of anyone
    other than its receiver stand on it (even a treaty partner's, and even
    when the deal carries peace — the army would take the tile back when
    the treaty ends, and blocks the new owner meanwhile).
  - *Per-turn cap*: a player may receive at most
    `DEAL_MAX_TILES_RECEIVED_PER_TURN` = 5 tiles by deals per turn (counted
    from the deal log of the current turn), which bounds last-turn land
    gifts that would pick the score winner.
  Storage caps apply only at the end of the turn (phase 7), as for the market.
* A deal is `expired` at the end of turn `expires_turn`. A deal is
  auto-withdrawn if either party is eliminated.
* Limits per player per turn: 30 diplomacy actions and 10 `say` messages
  (a `say` counts toward both; excess rejected with an error). Text is capped (say 500, message 300 chars).
  Only *applied* actions count (an `accept` whose settlement failed counts;
  actions rejected with a validation error do not). Channel actions and
  actions inside orders share the limits. At most 100 actions are looked at
  per `diplomacy` call.
* On a deal nobody else can see, `counter`/`accept`/`reject`/`withdraw` fail
  with the same error as for a non-existent deal (no probing).
* Every action gets an immediate result
  `{"index":i,"ok":true,"deal":"d9"}` or `{"index":i,"ok":false,"error":"..."}`
  (`counter` also returns `"countered":<old id>`; `accept` returns
  `"status":"accepted"`, or `ok:false` with `"deal"` and `"status":"failed"`
  when settlement failed; `say` returns no `deal`).

### 13.3 Contracts

A contract `{id, payer, payee, per_turn:{...}, turns_left, deal}` pays during
phase 7 (Economy) **after yields, before upkeep**. If the payer cannot pay the
full instalment the contract **defaults**: nothing is paid that turn, the
contract is cancelled, the payer is fined `max(25, ceil(value / 2))`
influence (`value` = the gold value of the remaining instalments, gold at
face value and food/wood/stone at their fixed start prices, see
`obligation_value`; `CONTRACT_DEFAULT_PENALTY`,
`CONTRACT_DEFAULT_GOLD_PER_INFLUENCE`) and its public `defaults` counter
increments. The part of the fine its influence stock cannot cover becomes
public `influence_debt`, taken from its influence at the start of every
later contract payment step (phase 7, after yields) until paid — so
spending influence before defaulting does not dodge the fine. A completed contract increments the
payer's `contracts_honoured`. Contracts are public (everyone sees who pays
whom), which makes tribute and alliances visible. Contracts die with an
eliminated party. Contracts pay in creation order (an instalment received
earlier in the list can fund a later payment); the first instalment is paid
in phase 7 of the turn in which the deal was accepted.

### 13.4 Reputation (public)

`players[].reputation = {"deals":n, "contracts_honoured":n, "defaults":n,
"betrayals":n, "influence_debt":n}` (`betrayals` is the treaty counter from
§7; `influence_debt` the unpaid default fines of §13.3).

### 13.5 Visibility

Open/closed deals and negotiation text are private to the two parties while
the game runs (spectators see them only after the game ends, like private
messages). When a deal is **accepted**, a public `deal_executed` event and a
public-log entry record the parties and the terms. `say` to a player is
private; `say` to all is public.

View additions (player view; spectator gets public parts while running):

```json
"deals": {
  "open":   [{"id":"d9","thread":"d7","from":"p2","to":"p1","give":{...},"get":{...},
              "peace":null,"message":"...","turn":12,"expires_turn":14,"status":"open"}],
  "recent": [ ...last 20 closed deals involving you, with status & reason... ],
  "log":    [{"id":"d5","turn":11,"from":"p3","to":"p4","give":{...},"get":{...},"peace":20}]
},
"contracts": [{"id":"c2","payer":"p3","payee":"p4","per_turn":{"gold":5},"turns_left":7,"deal":"d5"}],
"diplomacy_seq": 57
```

`diplomacy_seq` increases with every diplomacy action/event in the game
(null in the public spectator view of a running game);
agents long-poll `GET /api/games/{id}/inbox?since=<seq>` to be woken when
something addressed to them happens (new proposal, counter, acceptance,
rejection, message).

Events: `deal_proposed, deal_countered, deal_executed` (the acceptance
event; public), `deal_rejected, deal_withdrawn, deal_expired, deal_failed,
contract_paid, contract_default (public), contract_completed, say` (public
when `to` is `"all"`). The others are visible to the two parties only. Every
diplomacy event carries `seq` and, when a player caused it, `by` (null for
an automatic withdrawal on elimination). Fields: `deal_proposed {deal:
<open-deal object>, from, to}`, `deal_countered {deal: old id, new: <deal>,
from, to}`, `deal_executed {deal, thread, from, to, give, get, peace,
contracts: [ids]}`, `deal_rejected {deal, message}`, `deal_withdrawn {deal,
reason}`, `deal_failed {deal, reason}`, `deal_expired {deal}`, `contract_paid
{contract, payer, payee, paid, turns_left}`, `contract_completed {contract,
payer, payee, deal}`, `contract_default {contract, payer, payee, per_turn,
turns_left, penalty, debt, deal}` (`penalty` = the whole fine, `debt` = the
part added to `influence_debt`), `say {from, to, text}`. A deal's peace also
emits the public `treaty_signed {a, b, until_turn, bond, deal, renewal?}`.

Open deals in views also carry `deliverable` (bool) and `problem` (null or
why accepting it right now would fail — computed from public information).
`deals.recent` is newest first, with `reason` and `closed_turn`; the full
spectator view lists every open deal and the last 100 closed ones.
`deals.log` holds the last 50 executed deals.

### 13.6 Engine / server / bots

* Engine: `Game.diplomacy(pid, actions) -> list[result]` (never raises;
  also accepts `{"actions":[...]}` or a single action object); every applied
  action is appended to `Game.diplomacy_log` as `{"turn", "seq", "pid",
  "action" (canonical form), "via": "channel"|"orders"}` so a game is
  reproducible from its orders + diplomacy log: for each turn, apply that
  turn's `via: "channel"` entries in log order, submit the turn's orders,
  step (`"orders"` entries are informational — they are re-created by the
  orders). The replay is exact when orders were (re)submitted after that
  turn's channel actions, which is how bots negotiate (rounds before
  `act()`); an order list submitted *before* a channel action can differ in
  pre-validation errors only. `diplomacy_seq` grows by one per applied action
  and per diplomacy event. `Game.inbox(pid, since)` returns `{"seq",
  "items"}`: diplomacy events visible to `pid` with `seq > since`, except
  those `by` `pid` (the last 5000 diplomacy events are kept). Mid-turn
  changes are visible in views immediately; already submitted orders are
  re-checked at resolution as usual. Constants: `constants.py` (`DEAL_*`,
  `DIPLOMACY_*`, `SAY_PER_TURN`, `CONTRACT_DEFAULT_*`), exposed under
  `rules_json()["diplomacy"]["deals"]`.
* Helpers for bots/SDK (`agentciv.engine.deals`, pure, work on a JSON view):
  `parse_bundle`/`check_bundle` (normalise/validate a bundle),
  `parse_action` (canonical action), `view_delivery_problem(view, pid,
  bundle, receiver=None, leaving=())` / `view_deal_problem(view, deal)` (can
  it settle now? includes the land rules), `land_problem`, `tiles_received`,
  `obligation_value(per_turn, turns_left)`, `default_penalty(value)`,
  `bundle_value(bundle, prices, tile_value, discount)` (rough gold value).
* Server: `POST /api/games/{id}/diplomacy {"actions":[...]}` (auth; also a
  bare list or one action object; optional `"turn"` → 409 if stale; 409 in
  the lobby, when finished or eliminated; 400 for a body that is not an
  action list or holds more than 100 actions) → `{"results":[...], "ok": all ok, "seq", "turn",
  "deadline"}` (a malformed action's result also carries an `example`/`hint`);
  `GET /api/games/{id}/inbox?since=SEQ&timeout=30&turn=T` (auth; `since`
  default 0, `timeout` ≤ 120 s) → `{"seq":n, "items":[Game.inbox items with
  seq > since], "turn", "status", "deadline", "timed_out"}`, returning as
  soon as there are items, the status or turn changes (or the current turn
  is not `T`), or on timeout. Turns also accept diplomacy actions inside
  `/orders`. `/stream` pushes a spectator frame whenever public diplomacy
  happens mid-turn (an executed deal, a public `say`). Engine calls happen
  under the session lock, so channel actions are serialised with turn
  resolution (an action lands in whichever turn is current).
* Bots: `Bot.negotiate(view) -> list[actions]` (default `[]`). The tournament
  runner and the server give every bot **3 negotiation rounds per turn**
  before `act()` (each round: every bot, in rotating seat order, sees a fresh
  view and returns actions, applied immediately). The server additionally
  runs a house bot's `negotiate` shortly after anything is addressed to it
  (0.25 s debounce, ≤ 1 s; at most 10 such calls per bot per turn), and
  re-runs its `act()` (resubmitting its orders) when one of its deals executed
  after it acted — always before the turn resolves, and even once its
  negotiate budget is used up. Server details: bots run outside the game lock; a bot whose
  `negotiate` is missing or the `Bot` default is skipped; `negotiate` raising
  or returning a non-list means no actions; the 3 rounds stop early after
  `min(5 s, max(0.5 s, turn_timeout/4))`.
