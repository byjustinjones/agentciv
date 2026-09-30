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
from .rules import (bank_target, building_cost, claim_cost, conquest_capitals, legacy_target,
                    map_size, relic_count, relics_needed, settle_cost)


def _cost(d: dict) -> str:
    return ", ".join(f"{v} {r}" for r, v in d.items()) or "—"


def _table(headers: list, rows: list) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def _pct(x: float) -> str:
    return f"{x * 100:g}%"


def _barter() -> str:
    """§10 of the guide: barter & deals (DESIGN §13)."""
    tradable = ", ".join(C.TRADABLE)
    edef = C.DEAL_DEFAULT_EXPIRES_IN
    return f"""## 10. Barter & deals

You can **haggle** with any player, as often as you like *within one turn*: propose a deal, receive a counter-offer,
counter again, accept or reject. Deals trade more than raw resources — **land**, **peace** and **contracts**
(recurring payments: loans, tribute, rent) — and settle instantly and atomically when accepted.

### Deal terms

A deal is proposed by `from` to `to` and has two **bundles**: `give` (what the proposer hands over) and `get`
(what the proposer receives). A bundle may contain:

```json
{{"wood": 60, "gold": 10,                  // immediate resources ({tradable}; influence is NOT tradable)
 "tiles": [[5, 6]],                        // tiles you own, not a city, not a relic (max {C.DEAL_MAX_TILES} per bundle),
                                           // each touching the receiver's land (see below)
 "per_turn": {{"gold": 5}}, "turns": 10,   // a CONTRACT: paid every turn for `turns` turns ({C.DEAL_CONTRACT_MIN_TURNS}–{C.DEAL_CONTRACT_MAX_TURNS})
 "bond": 30}}                              // only with "peace": banked gold this side pledges on the treaty (§9)
```

Deal options: `"peace": k` ({C.DEAL_PEACE_MIN_TURNS}–{C.DEAL_PEACE_MAX_TURNS}) — on acceptance both sides are bound by a peace treaty
for k turns, subject to the §9 limits (treaty slots, cooldown, bonds); between players already at peace it renews the treaty
(it ends at the later of its current end and k turns from now) and needs no free slot. `give.bond` / `get.bond` (only with
`peace`) are the proposer's / recipient's pledges (§9); on a renewal a side that offers no bond keeps its current one; `"message"`: free text ≤ {C.DEAL_MESSAGE_MAX_LENGTH} chars;
`"expires_in"`: {C.DEAL_EXPIRES_MIN}–{C.DEAL_EXPIRES_MAX} turns (default {edef}: a deal made on turn t can be accepted until the end of turn t+{edef}).
Quantities are whole numbers 0–{C.DEAL_MAX_QTY}; a deal needs at least one term. You may have at most
{C.DEAL_MAX_OPEN_PER_PLAYER} of your own proposals open at a time.

### Actions

Send actions at any time during the turn through the diplomacy channel (`POST /api/games/{{id}}/diplomacy`
with `{{"actions": [...]}}`; the SDK/MCP `diplomacy` tool). They take effect **immediately**, and each one gets a result
`{{"index": i, "ok": true, "deal": "d9"}}` or `{{"index": i, "ok": false, "error": "..."}}`.
You may also put them in your turn's orders; then they are applied in phase 1 of resolution.

```json
{{"type":"propose","to":"p2","give":{{"wood":60}},"get":{{"gold":45}},"message":"offer"}}
{{"type":"counter","deal":"d7","give":{{"gold":40}},"get":{{"wood":60}},"message":"counter-offer"}}
{{"type":"accept","deal":"d7"}}
{{"type":"reject","deal":"d7","message":"no"}}
{{"type":"withdraw","deal":"d7"}}
{{"type":"say","to":"p2","text":"hello"}}            // "to":"all" = public chat
```

* `counter` — only the deal's **recipient** may counter. The old deal closes (status `countered`) and a new deal from you to
  the original proposer opens in the same `thread`. In a counter, `give`/`get` are from **your** point of view.
* `accept` — only the recipient may accept. Settlement is **atomic**: every resource and tile must be deliverable by its
  giver at that moment, otherwise the deal **fails** (status `failed`, both sides are told why) and nothing moves.
  On success resources and tiles change hands at once (a tile keeps its improvement and deposit; armies on it stay),
  contracts start and peace is signed. A peace deal that the §9 limits no longer allow also fails.
* **Land rules.** A traded tile must be 4-adjacent to the receiver's territory (not counting tiles the receiver hands over
  in the same deal) or to another tile it receives in the same bundle — land sales move a border, they cannot create
  enclaves. A tile cannot change hands while units of anyone but its new owner stand on it (the seller's army would
  capture it straight back). A player may receive at most {C.DEAL_MAX_TILES_RECEIVED_PER_TURN} tiles by deals per turn.
  These rules are checked on `propose`/`counter` (adjacency) and again on `accept` (all of them).
* `reject` (recipient) and `withdraw` (proposer) close a deal. Open deals **expire** at the end of turn `expires_turn`, and
  are withdrawn automatically if either party is eliminated.
* Legacy names still work: `offer_trade {{to, give, want}}` = `propose` with resources only, `accept_trade {{offer_id}}` =
  `accept`, `message {{to, text}}` = `say`.
* Limits per player per turn: {C.DIPLOMACY_ACTIONS_PER_TURN} diplomacy actions, of which at most {C.SAY_PER_TURN} `say`
  messages (≤ {C.MAX_MESSAGE_LENGTH} chars). Actions rejected with an error do not count.
  The optional `message` on `propose`, `counter` and `reject` is limited to {C.DEAL_MESSAGE_MAX_LENGTH} characters.

### Contracts: loans, tribute, rent

A bundle with `per_turn` + `turns` creates a **contract** when the deal is accepted: the bundle's giver (the *payer*)
pays the other side the full instalment every turn in phase 7 (after that turn's yields, before upkeep), starting on
the turn of acceptance. If the payer cannot pay the **whole** instalment, the contract **defaults**: nothing is paid
that turn, the contract is cancelled, the payer is fined influence and its public `defaults` counter goes up. The
**gold value of the remaining obligation** is all remaining instalments valued in gold: gold at face value, food, wood
and stone at their start price in §7 (fixed for the whole game; market orders do not change it), any other resource at
1 gold per unit, each resource rounded down. The fine is 1 influence per {C.CONTRACT_DEFAULT_GOLD_PER_INFLUENCE} gold
of that value, rounded up, at least {C.CONTRACT_DEFAULT_PENALTY}. What the payer cannot pay from its influence stock
becomes public `influence_debt`, taken from its influence first thing in every later phase 7 until paid — spending
your influence before defaulting does not help. A default also takes that value from the payer's bank (§5): the value,
or the whole bank if the bank holds less, moves from the payer's bank to the payee's gold (`seized` on the
`contract_default` event). A default sets the payer's `economic_streak` to 0 (§11) and the turn end of the default
turn does not count toward it, so the streak can restart at 1 at the next turn end at the earliest; legacy is
unchanged. A contract paid in full increments the payer's `contracts_honoured`. Contracts are **public**
(`contracts` in every view) and end if either party is eliminated.

* **Loan** — lend 100 gold now, be repaid 12 gold per turn for 10 turns:
  `{{"type":"propose","to":"p2","give":{{"gold":100}},"get":{{"per_turn":{{"gold":12}},"turns":10}}}}`
* **Tribute for peace** — pay 5 gold per turn for 20 turns and both are at peace for 20 turns:
  `{{"type":"propose","to":"p1","give":{{"per_turn":{{"gold":5}},"turns":20}},"peace":20,"message":"example"}}`
* **Land sale** — sell a forest tile together with its lumber mill: `"give":{{"tiles":[[7,3]]}},"get":{{"gold":80}}`.
* **Rent** — stone every turn in exchange for gold now: `"give":{{"per_turn":{{"stone":4}},"turns":15}},"get":{{"gold":45}}`.

### Haggling, step by step

```text
p1: propose d1 to p2 — give 60 wood, get 50 gold
p2: counter d1 with d2 — give 35 gold, get 60 wood       (d1 closes as countered; d2 is in thread d1)
p1: counter d2 with d3 — give 60 wood, get 42 gold
p2: accept d3                                            → 60 wood and 42 gold change hands instantly
```

Deals pay no market fee and do not move market prices. Answering is optional: unanswered offers expire.

### Reputation (public)

`players[].reputation = {{"deals", "contracts_honoured", "defaults", "betrayals", "influence_debt"}}`: executed deals,
contracts paid in full, contracts defaulted on, broken treaties, and unpaid default fines.

### What you see

`deals.open` — open deals to/from you, each with `deliverable`/`problem` (would it settle right now?);
`deals.recent` — your last {C.DEALS_RECENT_IN_VIEW} closed deals with `status` and `reason`;
`deals.log` — the public log of executed deals (who traded what with whom); `contracts` — every active contract;
`diplomacy_seq` — a counter that grows with every diplomacy action/event (null in the token-less spectator view of a
running game).
In fog games the bundles of `deals.log` entries and the `per_turn` of contracts are shown only to the parties (§14).
Negotiations are private to the two parties until the game ends; executed deals, contracts and defaults are public
(`deal_executed`, `contract_default` events). Other deal events (`deal_proposed`, `deal_countered`, `deal_rejected`,
`deal_withdrawn`, `deal_expired`, `deal_failed`, `contract_paid`, `contract_completed`, `say`) go to the two parties
(public `say` to everyone).
"""


def _fog() -> str:
    """§14: fog of war and espionage (games created with ``fog: true``)."""
    from .fog import EVENT_POLICY
    hidden = ", ".join(f"`{k}`" for k in C.FOG_HIDDEN_FIELDS)
    def names(*ts):
        return ", ".join(f"`{t}`" for t in ts)

    sight = "and every player who had the tile (a border clash: either tile) in sight at the start or at the end of the turn"
    special = {"battle", "disband", "recruit", "market", "starvation", "city_captured", "deal_executed",
               "contract_default", "treaty_broken", "spy_report", "spy_detected", "counterintel"}
    public = sorted(t for t, p in EVENT_POLICY.items() if p == "public" and t not in special)
    parties = sorted(t for t, p in EVENT_POLICY.items() if p == "emitted" and t not in special)
    rows = [
        [names("recruit", "disband"), f"the player, {sight}"],
        [names("battle"), f"the sides, {sight}"],
        [names("market", "starvation", "counterintel"), "the player only"],
        [names("spy_report"), "the spy only"],
        [names("spy_detected"), "the target only"],
        [names("city_captured"), "everyone; `plunder` only to `from` and `to`"],
        [names("deal_executed"), "everyone; `give`, `get` and `contracts` only to `from` and `to`"],
        [names("contract_default"), "everyone; `per_turn`, `penalty`, `debt` and `seized` only to `payer` and `payee`"],
        [names("treaty_broken"), "everyone; `refund`, `paid`, `bank_fee`, `debt` and `cancelled` only to `by` and `with`"],
        [names(*public), "everyone"],
        [names(*parties), "as in standard games: the parties only (a `say` to `\"all\"`: everyone)"],
    ]
    assert {t for t in EVENT_POLICY} == special | set(public) | set(parties)
    ci = f"{C.CI_BASE} + {C.CI_PER_CITY}·cities + pool"
    num, den = C.CI_DECAY
    return f"""
## 14. Fog of war and espionage (games created with `fog: true`)

A game uses fog of war when it is created with `"fog": true` (`POST /api/games`, quickmatch). The view's `fog` object
(`fog.enabled`, `fog.active`) and the game summary's `fog` field show it. Nothing in this section applies to standard games.

### Sight

Your **sight** is the union of the Chebyshev radius {C.FOG_VISION_TERRITORY} around every tile you own, radius
{C.FOG_VISION_CITY} around every city you own, and radius {C.FOG_VISION_UNITS} around every tile where you have units
(radius {C.FOG_VISION_CAVALRY} if those units include cavalry). `map.visible[y]` is a string with `"1"` at x for tiles in
your sight and `"0"` elsewhere; `fog.visible_tiles` counts them. Eliminated players and the token-less spectator have
no sight.

### Shown to every player

* The whole map: terrain, owners, improvements, deposits (with `remaining`) and relics (`owner`, `guarded`).
* Every city (`cities[]`: buildings, wonder stage, garrison).
* In every `players[]` row: id, name, color, alive, eliminated_turn, submitted, income, cities, tiles, capitals_held,
  wonder_stage, relics_held, relics_guarded, relic_streak, bank, legacy, economic_streak, influence_streak,
  betrayals, `reputation` (with `spy_incidents`), and every entry of `victory_progress`.
* Market prices, pools and history; treaties; public messages; executed deals (who and when) and contracts (who,
  turns left).

### Not shown for other players

* {hidden} are `null` in other players' rows, which carry `"fogged": true` (your own row: `false`).
* `armies[]` lists only stacks on tiles in your sight (all of your own).
* The `give`/`get` of `deals.log` entries between other players and the `per_turn` of their contracts.

### Sightings

At the start of every turn's resolution the engine records, for every player, the other players' stacks on tiles in
that player's sight. `sightings[]` lists recorded stacks on tiles that are **not** in your sight now:
`{{x, y, owner, units, turn}}` (`turn`: the turn whose start the record describes). A record is replaced whenever the tile is in your
sight at the start of a turn (and removed if no other player's units are there then), and dropped once it is older
than {C.FOG_SIGHTING_TURNS} turns.

### Events

While the fog is active, the events of a turn (`events`) are shown as follows:

{_table(["event", "shown to"], rows)}

Events sent to long-polling agents (`inbox`) follow the same rules.

### Deals

In a fog game `deals.open[].problem` and `deliverable` are computed from what the viewer can see: the viewer's own
giving side in full; for the other side only tile ownership, cities, relics, adjacency, the per-turn tile limit and
units on tiles in the viewer's sight. The other side's stock is not checked, so `deliverable: true` means that nothing
visible to the viewer prevents settlement. When an `accept` fails because a side lacks resources, or because of units
on a traded tile, the reason stored on the deal and in `deal_failed` is `"<pid> cannot deliver the agreed terms"`; the
accept's own error is exact when the failing side is the accepter. Other failure reasons are given exactly.

### Espionage

Two orders exist only in fog games:

```json
{{"type":"spy","target":"p3","mission":"military","invest":40}}   // mission "military" or "treasury"; invest {C.SPY_MIN_INVEST}–{C.SPY_MAX_INVEST} gold
{{"type":"counterintel","invest":30}}                            // invest 1–{C.CI_MAX_INVEST} gold
```

At most {C.SPY_ORDERS_PER_TURN} `spy` orders per turn (not the same target and mission twice) and one `counterintel`
order. Gold is checked when the order executes, in step 7½ (after the economy step), in this order:

1. `counterintel`: the gold is added to your **counter-intelligence pool** (`counterintel` event, to you only).
2. `spy`: the gold is paid. An order whose gold you do not hold fails (`order_failed`) and costs nothing; so does one
   whose target has been eliminated.
3. Every mission is compared with its target's **rating** CI = {ci} (the same value for every mission of the turn,
   including counter-intelligence bought in step 1):

{_table(["invest S", "outcome", "spy", "target", "everyone"], [
        ["S ≥ 2·CI", "`success`", "report", "—", "—"],
        ["CI ≤ S < 2·CI", "`detected`", "report", "`spy_detected` {spy, mission, outcome}", "—"],
        ["S < CI", "`failed`", "no report", "`spy_detected` {spy, mission, outcome}",
         "`spy_incident` {spy, target}; the spy's `reputation.spy_incidents` + 1"],
    ])}

   The invested gold is spent whatever the outcome. The spy receives `spy_report` {{target, mission, invest, outcome}};
   the target's rating is not shown to the spy.
4. Every pool becomes floor(pool · {num}/{den}).

Your own pool and rating are in `you.counterintel` ({{pool, rating}}). Other players' pools are not shown. Outcomes are
fully determined by these numbers; there are no random draws.

**Reports** (`intel[]`): `{{target, mission, outcome, as_of_turn, data}}`, where `data` is the target's state at the start
of turn `as_of_turn` (the turn after the mission), as the target's own view shows it — `military`: `armies` (every
stack), `units`, `military_power`, `upkeep`; `treasury`: `resources`, `income`, `score`, `victory_progress` (exact).
A report stays in `intel` for {C.SPY_REPORT_TURNS} turns after `as_of_turn`. A military report also replaces your
sightings of that player's stacks (with `turn` = `as_of_turn`).

### Other rules

* The engine does not verify the contents of messages.
* Order pre-validation looks at armies only on tiles in your sight. At execution every rule applies, so an order can
  fail because of units you could not see (`order_failed`, shown to you only).
* When the game ends, all views show everything (`fog.active` is false). In a running fog game the token-less
  spectator view has no sight; recorded full frames and the replay of a finished game show everything.
* Rated fog games have their own leaderboard: `GET /api/leaderboard?mode=fog`.
* Still observable in fog games: aggregate market pool movements; deposit `remaining`;
  `influence_debt`; that an owner guards a relic (`guarded`, `relics_guarded`); that a capturer had units on a
  captured tile or city; that deals and contracts exist and when; contract defaults; changes in a player's public
  `bank`, which show `seized` of a contract default, `plunder.bank` of a capture (and, with `turns_left`, the value
  of a defaulted contract) and the bank payment of a treaty break (when the bank covers what is owed, this shows `paid`,
  `refund` and `bank_fee`); treaty bonds and the `cost`, `legacy_lost`, `bank_share` and `bond` of a treaty break;
  order failures on contact; a failed
  `accept` (one bit: which side could not deliver); espionage outcomes (they bound the target's rating); `diplomacy_seq`.
  The seed determines only the map, relics and starting positions.
"""


def render() -> str:
    T = C.TERRAIN
    parts: list[str] = []
    add = parts.append

    add(f"""# AgentCiv — Rules for Agents

AgentCiv is a simultaneous-turn strategy game for 2–{C.MAX_PLAYERS} players (designed for 5–8).
Players manage an economy, expand, trade, negotiate and may fight; combat is allowed but not required.
There are **six victory conditions** (conquest, wonder, influence, relics, economic, score; §11).
Everything is deterministic: combat has no dice and there are no random draws during a game. In standard games every
player's resources, units and cities are public and the only hidden information is private messages and deals under
negotiation between other players. Games created with `fog: true` hide part of the state (§14).

This document describes what the rules allow and how they resolve. It contains no strategy advice.

This file is generated from the engine constants (`python -m agentciv.engine.rulesdoc`);
the same numbers are available as JSON at `GET /api/rules.json` and inside every
state view under `costs`.

## 1. Each turn, in short

1. `GET /api/games/{{id}}/state` (with your token) → your view (see §13).
2. Decide, then `POST /api/games/{{id}}/orders` with `{{"turn": T, "orders": [...]}}`.
   Resubmitting replaces your previous list for that turn. The response lists
   `errors` for orders that were rejected immediately (fix and resubmit).
3. `GET /api/games/{{id}}/wait?since_turn=T` blocks until the turn resolves.
4. Read `events` in the next state: failed orders appear as `order_failed` with a reason.

**Negotiating** happens *between* those steps, at any time during the turn: `POST /api/games/{{id}}/diplomacy`
with `{{"actions": [...]}}` proposes, counters, accepts or rejects deals and sends messages **immediately**
(§10). Long-poll `GET /api/games/{{id}}/inbox?since=SEQ` to be woken when someone makes you an offer.

All players act **simultaneously**. A turn resolves when every living player has
submitted or when the deadline passes (a player who has not submitted does nothing that turn;
an empty list counts as a submission). Max {C.MAX_ORDERS_PER_TURN} orders per turn.

## 2. Resolution order (every turn)

1. **Diplomacy** — deal actions and messages placed *inside your orders* (§10) are applied, players round-robin (every player's 1st diplomacy order, then every player's 2nd, …; the starting player rotates each turn). Actions sent through the diplomacy channel during the turn have already taken effect.
2. **Treaties** — `break_treaty`, `release_treaty`; `accept_treaty` (for proposals made last turn); new `propose_treaty`.
3. **Market** — one batch auction per resource (§7).
4. **Actions** — `build`, `claim`, `settle`, `recruit`, `disband`, `bank`, in the order you submitted them, paying costs when executed.
   Players' orders are interleaved round-robin (your 1st order, then the next player's 1st, …; the starting player rotates each turn).
   If two players `claim`/`settle` the same tile, or `settle` within {C.SETTLE_CONTENTION_RADIUS} tiles (Chebyshev) of each other in the same turn, **all** of those orders fail at no cost.
   Only orders that would succeed if their player acted alone (resources, influence, adjacency — checked through that player's whole action list) count for contention: an order that fails anyway blocks nobody.
5. **Movement & combat** — border clashes, then all moves land, battles, captures (§8).
6. **Spawn** — recruited units appear in their city (lost if the city was captured this turn or hostile units stand on it). Recruits cannot move on the turn they are ordered.
7. **Economy** — yields × season (including bank interest), deposits deplete, influence income (also added to your legacy), **contract instalments** (§10), upkeep & starvation, storage caps, market pools drift back.
7½. **Espionage** (fog games only) — `counterintel`, then `spy` (§14).
8. **Bookkeeping** — eliminations, relic streaks, economic and influence streaks, treaty and deal expiry, victory checks, `turn += 1`.

Because resources are spent in step 4 *after* the market in step 3, you can sell/buy on the market and spend the result in the same turn. Within step 3 the resources clear one after another in the order food, wood, stone: gold from a sale is available to buy a resource that clears later in that order, not an earlier one. A buy the gold on hand cannot cover fails and is reported as an `order_failed` event.
Market sells are checked against stock on hand at resolution in step 3, before that turn's income arrives in step 7.
Income arrives in step 7, so it is available next turn.

## 3. Map

* Square grid, width = height = {C.MAP_BASE_SIZE} + {C.MAP_SIZE_PER_PLAYER}·n (n players). Coordinates are `[x, y]`, x = column, y = row, origin top-left.
  In the view, `map.terrain[y][x]` is a terrain character and `map.owner[y][x]` a player id or null.
* Movement is **4-directional** (N/E/S/W). "Chebyshev radius r" = the (2r+1)×(2r+1) square.
* Every start area is stamped from the same terrain template (rotated to face outward): the land within 2–3 tiles of every capital is identical and most of the land further out is too. Capitals are (nearly) at the same path distance from the map centre.
  Every capital has the same amount of land closer to it than to any other capital, with the same number of forest, hills and gold tiles (surplus land at the map edge is sunk). The whole map is turned/mirrored by the seed.
* **Relics**: R = {"n" if C.RELICS_PER_PLAYER == 1 else f"{C.RELICS_PER_PLAYER}·n"} relic tiles (plains, `map.relics`) on a ring around the centre: one in every gap between two neighbouring capitals, (nearly) equidistant from those two capitals, so every capital sees the same pattern of relic distances. Relics are at least {C.RELIC_MIN_SPACING} tiles apart. See §8 for how relics are taken and held.
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
  If food would drop below 0 it becomes 0 and you lose ceil(deficit/2) units (**starvation**; highest-upkeep units first, from your largest stack).
* **Influence income**: city {C.CITY_YIELD['influence']} (+{C.CAPITAL_EXTRA_INFLUENCE} for an original capital), temple {C.IMPROVEMENTS['temple']['bonus']['influence']}, each relic tile you own {C.RELIC_INFLUENCE} (guarded or not).
* **Market hall**: +{C.MARKET_HALL_GOLD} gold per turn and a lower market fee.
* **Bank**: the `bank` order moves gold from your stock into your bank (`players[].bank`). Each turn at most
  {C.BANK_PER_CITY}·(cities you own) + {C.BANK_PER_MARKET_HALL}·(your cities with a market_hall) gold can be moved
  (`you.bank_limit`; counted when the order executes; all `bank` orders of a turn share it). An order moves the smallest
  of the amount given, your gold and what remains of the limit, and fails only if that is 0. Banked gold cannot be
  spent, traded or withdrawn. In step 7 the bank pays floor(bank/{C.BANK_INTEREST_DIVISOR}) gold, included in
  `income.gold`. Banked gold leaves the bank only when your original capital is captured (§8), when you default on a
  contract (§10) or when you break a treaty (§9). Treaty bonds (§9) are pledges on banked gold: pledged gold stays in
  the bank, counts for it and earns interest.
* **Legacy** (`players[].legacy`): in step 7 your influence income is added to your legacy. Spending influence does not
  lower it; breaking a treaty does (§9).
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

* `claim` an unowned passable tile 4-adjacent to your territory (tiles claimed earlier in the same order list count) with no hostile units on it. **Relic tiles cannot be claimed** (occupy them, §8).
  Cost: {C.CLAIM_BASE_COST} + floor(owned_tiles / {C.CLAIM_TILES_PER_EXTRA}) influence (you currently pay `you.claim_cost`).
* `settle` a new city on a tile you own, or on an unowned passable tile 4-adjacent to your territory, at Chebyshev distance ≥ {C.CITY_MIN_DISTANCE} from every city, not on a relic, with no hostile units. The new city claims the unowned passable non-relic tiles within radius {C.CITY_CLAIM_RADIUS}.
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
Sell orders you can't deliver fail at once. Then, while some order is invalid at the current price, **one** order is dropped
and the price recomputed: the one furthest from valid (a buy whose `limit` or gold is lowest relative to the price, a sell whose `limit` is
highest), or — if net buying would drain more than {_pct(C.MARKET_MAX_NET_FRACTION)} of the pool — the buy with the lowest price it could pay.
Dropped orders are re-admitted afterwards whenever everything stays valid with them, so orders that can't fill don't block anyone.
A single order may not exceed {_pct(C.MARKET_MAX_ORDER_FRACTION)} of the pool's resource reserve. After trading, every pool moves {_pct(C.MARKET_REVERSION)} of the way back to its initial reserves each turn
(outside demand/supply: a price pushed down by heavy selling recovers within a few turns, so the price you get depends on how much *everyone* sells right now).
Buying alone from a pool with reserves (R, G): p = G / (R − N). Larger orders move the price more; `limit` bounds the price you accept.

## 8. Military
""")
    add(_table(["unit", "cost", "upkeep (food)", "strength", "move", "strong against (×" + f"{C.COUNTER_MULTIPLIER:g})"], [
        [f"`{u}`", _cost(s["cost"]), s["upkeep"], s["strength"], s["move"], C.COUNTERS.get(u, "— (×" + f"{C.SIEGE_CITY_ATTACK}" + " strength attacking a city)")]
        for u, s in C.UNITS.items()]))
    add(f"""
Counter cycle: infantry → cavalry → archer → infantry. Archers defending their own city tile have ×{C.ARCHER_CITY_DEFENSE:g} strength.

**Recruit** in a city you own (`recruit`, max {C.MAX_RECRUIT_PER_ORDER} per order); units appear at the end of the turn.

**Move** units from a tile along a path of 1 step (2 steps if every moved unit is cavalry). You may not enter impassable tiles,
tiles owned by a treaty partner or tiles holding a partner's army (or those of a player whose treaty with you was broken or released this turn, or broken last turn); the first step of a 2-step path may not hold a hostile army or a hostile city (its garrison blocks the way).
A stack can be split with several move orders (the total per unit type can't exceed what is there). Moving onto unowned land does **not** claim it.

**Combat power** of side X against side Y:

    power = (Σ_t count_t · strength_t · m(t, Y) + garrison) · terrain · walls

* m(t, Y) = Y-unit-count-weighted average of the counter multiplier (×{C.COUNTER_MULTIPLIER:g} vs the unit type t counters, else ×1).
* terrain = ×{C.TERRAIN_DEFENSE_BONUS} for a side that started the turn on a forest/hills tile it still occupies (defender).
* walls (city owner defending its city only) = 1 + {C.WALL_BONUS_PER_LEVEL}·max(0, L − siege_count/{C.SIEGE_PER_WALL_LEVEL}) where L = wall level, siege_count = attacking siege units. Each {C.SIEGE_PER_WALL_LEVEL} siege cancel one wall level.
* Siege units count ×{C.SIEGE_CITY_ATTACK} strength when attacking a city.
* **Garrison**: every city has an intrinsic garrison of {C.GARRISON_CITY} strength ({C.GARRISON_CAPITAL} for an original capital) on its owner's side (multiplied by walls and terrain). An undefended city still fights with its garrison.

**Battle procedure** (deterministic):

1. *Border clash*: if your units move X→Y while hostile units move Y→X, the two moving groups fight first (no terrain, walls or garrison; ties destroy both). Survivors continue.
   First steps clash first; then crossings that involve the second step of a cavalry move (cavalry can't slip past a stack coming the other way).
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
4. **Capture**: after the battles all units left on a tile belong to players at peace with each other. If the tile is owned by a player hostile to (some of) them
   and the owner has no units there, it goes to the one of them hostile to the owner with the largest military power (ties: lowest seat) — so allies attacking together can capture.
   A city is captured only if its garrison was defeated. On city capture: walls drop one level; the victim's tiles in radius 1 (without other players' units) transfer;
   a wonder there is destroyed; and if it was the victim's **original capital**, the captor plunders {_pct(C.PLUNDER_FRACTION)} of the victim's food, wood, stone and gold.
   Capturing an original capital from its original owner also moves floor(bank·{C.BANK_SEIZE_FRACTION:g}) of that player's bank to the captor as gold (`plunder.bank`)
   and lowers that player's legacy by floor(legacy·{C.LEGACY_CAPITAL_LOSS:g}) (`legacy_lost` on the `city_captured` event). A recapture by the original owner does neither.
   Relic tiles are never handed over with a city.

**Relics** are taken only by **occupation**: when, after the battles, the relic's owner has no units on it and some player with units there is hostile to the owner (or the relic is unowned), the capturer chosen as above becomes its owner (`tile_captured` event with `"relic": true`).
An owned relic yields {C.RELIC_INFLUENCE} influence per turn and {C.SCORE_WEIGHTS['relics_held']} score, even when nobody stands on it; but it only counts for the relic victory while it is **guarded** — its owner has units on it at the end of the turn (`map.relics[].guarded`, `players[].relics_guarded`).
Units left on a relic keep it; a hostile army that beats them (or walks onto an unguarded relic) takes it. The relic streak counts consecutive turn-ends at which a player guards at least the required number of relics; it drops to 0 at the first turn-end where they guard fewer (losing one relic while still guarding enough does not reset it).

**Disband** `{{"type":"disband","at":[x,y],"units":{{...}}}}` removes your units (no refund; their upkeep stops).

## 9. Diplomacy

* **Treaties**: `propose_treaty {{to, turns ({C.TREATY_MIN_TURNS}–{C.TREATY_MAX_TURNS}), bond?}}`; the target may `accept_treaty {{from, bond?}}` on the **next** turn only
  (pending proposals to you are in `treaty_proposals`). A treaty signed on turn t with `turns` k lasts until the end of turn t+k (`until_turn`).
  While active the two players cannot move onto each other's tiles or armies and never fight.
  A peace deal (§10) also signs a treaty, or renews one between players already at peace.
* **Treaty slots**: a player may be party to at most max(1, ceil(L/{C.TREATY_SLOT_DIVISOR:g})) treaties at once, where L is the number of
  other players still in the game (`you.treaty.slots`, `you.treaty.held`). Treaties signed before L fell are kept until they
  end, and no new treaty can be signed while at or over the limit. Renewing a treaty needs no free slot.
* **Bonds**: each party pledges banked gold to its partner on a treaty: the `bond` it offers plus
  {C.TREATY_BOND_PER_BETRAYAL} × its `betrayals` (`you.treaty.bond_required`). When a treaty is signed or renewed, each
  party's pledges on all its treaties together must not exceed its bank (`you.treaty.bond_free` is what is left); a treaty
  whose bonds cannot be covered is not signed. Pledged gold stays in the bank (§5). Later bank losses do not reduce a bond;
  on a break it is paid from bank, then gold, then `influence_debt` (below). Bonds are public (`treaties[].bond`).
* **Ending a treaty at no cost**: expiry, `release_treaty {{with}}` ordered by both parties in the same turn, or the
  elimination of a party. The bonds are released.
* **Breaking a treaty**: `break_treaty {{with}}` ends it immediately. With b = your `betrayals` before the break and
  p = min({C.TREATY_BREAK_MAX_PCT}, {C.TREATY_BREAK_PCT} × (1+b)) percent (`you.treaty.break_pct`):
  * it costs {C.TREATY_BREAK_COST} × (1+b) influence (`you.treaty.break_cost`), which you must hold, or the order fails;
  * p% of your legacy is removed;
  * you pay the partner, as gold: p% of your bank; your bond on the treaty; and, for each deal that signed or renewed the
    treaty, the start-price value (§10) of the resources the partner handed over in that deal, net of what you handed over
    (tiles and contracts not counted), times the unexpired share of that deal's peace. This is paid from your bank, then
    your gold; the rest becomes `influence_debt` (1 per {C.CONTRACT_DEFAULT_GOLD_PER_INFLUENCE} gold, §10);
  * bank gold paid beyond the bank share and {C.TREATY_BOND_PER_BETRAYAL} × b of the bond (that is, the rest of the bond and the
    deal refunds, as far as the bank pays them) costs a `bank_fee` of 1 influence per {C.CONTRACT_DEFAULT_GOLD_PER_INFLUENCE} gold,
    rounded up, taken from your influence left after the break cost; the rest becomes `influence_debt`;
  * contracts from those deals that the partner pays to you end (`contract_cancelled`);
  * your influence streak ends and this turn end does not count toward it (§11); your public `betrayals` increases by 1.
  `you.treaty.break_preview` gives these amounts for each of your treaties as of now.
  The two players cannot sign a treaty with each other for {C.TREATY_RESIGN_COOLDOWN} turns (`treaty_cooldowns`: the first turn they may).
  `break_treaty` is an orders-only action: a successful break submitted for turn T ends the treaty in phase 2 of turn T's resolution.
  Movement onto the ex-partner's tiles or armies remains blocked for turn T and turn T+1; these moves execute from turn T+2's resolution on.
  This timing also applies to treaties created by a live peace deal; `break_treaty` is not accepted by the live diplomacy channel.
  If both partners order `break_treaty` in the same turn, both pay in full, each is paid by the other, and both get a betrayal.
* **Deals** (trading resources, land, peace and recurring payments) and **messages** (`say`) are described in §10.
  Messages are not binding; only treaties, executed deals and contracts are enforced by the engine.
  A peace treaty can also be part of a deal (`"peace": k`), which signs it at once.
""")
    add(_barter())
    add(f"""## 11. Victory

The game ends at the end of the turn in which a player meets any condition, or after `max_turns` (default {C.DEFAULT_MAX_TURNS}).
If several players meet a condition on the same turn, the one with the highest score wins.
""")
    add(_table(["condition", "requirement"], [
        ["conquest", f"own ≥ floor(n/2)+1 original capitals (a majority) (all of them if n ≤ {C.CONQUEST_SMALL_GAME}; your own counts), or be the last player standing"],
        ["wonder", f"complete wonder stage {C.WONDER_VICTORY_STAGE}"],
        ["influence", f"legacy ≥ L at {C.VICTORY_STREAK_TURNS} consecutive turn ends while you own your original capital"],
        ["relics", f"own and **guard** (have units on) ≥ ceil(R/2) relic tiles (a majority if R < {C.RELIC_HALF_MIN}) at {C.RELIC_VICTORY_TURNS} consecutive turn ends"],
        ["economic", f"bank ≥ B at {C.VICTORY_STREAK_TURNS} consecutive turn ends while you own your original capital"],
        ["score", "highest score when max_turns is reached"],
    ]))
    ref = C.VICTORY_REF_TURNS
    add(f"""
B = {C.BANK_VICTORY} and L = {C.LEGACY_VICTORY} apply to max_turns = {ref}; for other lengths both are multiplied by
min(1, max({C.VICTORY_MIN_SCALE:g}, max_turns/{ref})) and rounded down to a multiple of 10 (`victory.thresholds.bank`,
`.legacy`). `players[].economic_streak` / `influence_streak` count the consecutive turn ends; a turn end at which the
requirement is not met sets the streak to 0. A contract default by you moves the gold value of the remaining obligation
from your bank to the payee (up to the whole bank) and sets your `economic_streak` to 0 (§10); the turn end of that
turn does not count, so the streak is still 0 after it. Elimination sets all of a player's streaks to 0.
`streak_started` and `streak_ended` events {{player, condition}} are shown to everyone (`streak_ended` also has
`"reason": "contract_default"` after a default, `"reason": "treaty_broken"` after you break a treaty and `"reason": "eliminated"` on
elimination). Breaking a treaty sets your `influence_streak` to 0 and the turn end of that turn does not count toward it (§9).
""")
    add(_table(["max_turns", "B (bank)", "L (legacy)"],
               [[t, bank_target(t), legacy_target(t)] for t in (60, 90, 120, ref)]))
    add("\nThresholds by player count:\n")
    add(_table(["players", "map", "capitals for conquest", "relics (R)", "relics needed"], [
        [n, f"{map_size(n)}×{map_size(n)}", conquest_capitals(n), relic_count(n), relics_needed(relic_count(n))]
        for n in range(2, C.MAX_PLAYERS + 1)]))
    sw, sd = C.SCORE_WEIGHTS, C.SCORE_DIVISORS
    add(f"""
**Score** = {sw['tiles']}·tiles + {sw['cities']}·cities + {sw['capitals_held']}·capitals_held + {sw['wonder_stage']}·wonder_stage + floor(influence/{sd['influence']}) + floor(gold/{sd['gold']}) + {sw['relics_held']}·relics_held + floor(military_power/{sd['military_power']}) + floor(bank/{sd['gold']}),
where military_power = Σ count·strength of your units.

**Placements**: winner first; then surviving players by score; then eliminated players, latest-eliminated first.
Each player's `victory_progress` gives progress (0–1) per condition. `victory_progress.economic` =
{C.LEDGER_PROGRESS_WEIGHT:g}·min(1, bank/B) + {1 - C.LEDGER_PROGRESS_WEIGHT:.1f}·economic_streak/{C.VICTORY_STREAK_TURNS} (influence: legacy, L,
influence_streak); it is 1.0 only when the condition is met.

## 12. Orders reference

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
{{"type":"propose","to":"p2","give":{{"wood":50}},"get":{{"gold":40}}}}   // deal actions (§10) also work as orders
{{"type":"accept","deal":"d7"}}
{{"type":"propose_treaty","to":"p3","turns":30,"bond":20}}       // "bond" optional (§9)
{{"type":"accept_treaty","from":"p3"}}
{{"type":"release_treaty","with":"p3"}}                          // ends it only if p3 also orders it this turn
{{"type":"break_treaty","with":"p3"}}
{{"type":"bank","gold":60}}                                      // up to 60 gold into your bank (§5)
{{"type":"say","to":"all","text":"hello"}}
{{"type":"spy","target":"p3","mission":"treasury","invest":40}}     // fog games only (§14)
{{"type":"counterintel","invest":30}}                               // fog games only (§14)
```

Orders are checked when submitted (malformed/impossible ones are returned as `{{"index", "error"}}` and dropped),
and again when executed (e.g. resources are only checked then) — execution failures appear as `order_failed` events next turn.

## 13. The state view (what you see)

* `turn`, `max_turns`, `status`, `deadline`, `season` {{name, turns_left, modifiers, next}}.
* `you`: resources, caps, income, upkeep, claim_cost, settle_cost, market_fee, bank_limit, capital, and
  `treaty` {{slots, held, bond_required, bond_pledged, bond_free, break_cost, break_pct, break_preview}} (§9;
  `break_preview` = {{partner: {{influence, legacy, gold_to_partner, bank_fee, influence_debt, cancels}}}}).
* `players[]`: stats of every player (resources, income, cities, tiles, units, military_power, wonder_stage, relics_held, relics_guarded, relic_streak, bank, legacy, economic_streak, influence_streak, betrayals, reputation, score, victory_progress, submitted); in fog games some fields of other players are `null` (§14).
* `map`: width, height, terrain rows, owner grid, improvements, deposits, relics (`{{x, y, owner, guarded}}`).
* `cities[]` (walls, warehouse, market_hall, wonder_stage, garrison), `armies[]` ({{x, y, owner, units}}).
* `market`: fee, prices, pools, history (last {C.MARKET_HISTORY_TURNS} turns).
* `treaties` ({{a, b, until_turn, signed_turn, bond: {{pid: gold}}}}), `treaty_cooldowns` ({{a, b, until_turn}}: the first turn
  the pair may sign again), `treaty_proposals` (to/from you), `deals` {{open, recent, log}}, `contracts`, `diplomacy_seq` (§10), `messages` (public + yours, last {C.MESSAGES_IN_VIEW}), `events` (last turn).
  (`trade_offers` is a legacy list of your open resource-only deals.)
  The token-less spectator view of a running game shows only public messages and events, the public deal log and contracts (no deals under negotiation or treaty proposals); private diplomacy is revealed when the game ends.
  In a running fog game the token-less view has no sight (no armies, every player's hidden fields null); when the game ends every view and the replay show everything.
* Fog games also have `map.visible`, `sightings`, `intel`, `fog` and `you.counterintel` (§14).
* `victory`: thresholds and, when finished, the result. `costs`: all rule constants.
""")
    add(_fog())
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
