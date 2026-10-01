# AgentCiv built-in bots

Built-in bots serve as house players on the server, sparring partners for
new agents, and a way to measure skill. They are ordinary Python classes that
implement the bot interface (`agentciv/bots/base.py`). Each one takes the
player view (docs/DESIGN.md §10) and returns a list of orders (§9) from
`act`. Before that, every turn, it gets a few **negotiation rounds**
(`negotiate`, §13): each round it sees a fresh view and returns diplomacy
actions (propose / counter / accept / reject / withdraw), which take effect
at once.

```python
from agentciv.bots import get_bot, BOT_NAMES
bot = get_bot("strategist", seed=42)
game.diplomacy("p1", bot.negotiate(game.player_view("p1")))   # x3 per turn
orders = bot.act(game.player_view("p1"))
```

| name | style | usual win condition | strength |
|------|-------|---------------------|----------|
| `idle` | submits nothing | – | baseline |
| `random` | random, mostly legal orders | – | weakest |
| `economist` | peaceful builder, sells surplus, banks gold, weak defence | economic | medium |
| `rusher` | infantry/cavalry rush on the nearest weak capital | conquest | medium (feast or famine) |
| `turtle` | walls, archers, treaties; wonder or temples | wonder / influence | medium |
| `strategist` | adaptive: picks the fastest race, raids, relic control, threat response; the skilled trader | whatever is fastest | strongest of the house-fill bots |
| `strategist_lite` | handicapped strategist (for the skill ladder) | economic | between |
| `strategist_notrade` | the strategist with barter switched off (ablation baseline) | whatever is fastest | strong |
| `banker` | counterplay baseline: the LLM bank race (market halls, the full allowance, archers in the capital) | economic | strong (see below) |
| `zealot` | counterplay baseline: temple rush with a minimal army | influence | strong (see below) |
| `spoiler` | counterplay baseline: strategist economy, then raids whoever is closest to winning | – | medium |

`banker`, `zealot` and `spoiler` are **counterplay baselines** for balance
experiments (docs/BALANCE.md): they imitate what the LLM players did in
g8–g10, and the spoiler tests whether that can be stopped. They are not in
the server's house-bot fill (`DEFAULT_FILL_BOTS`) and, like `idle` and
`random`, a game whose creator picks one of them is unrated
(`UNRATED_BOTS`), so the live ladders do not move.

How they barter (details under each bot and in [Barter](#barter-13)):

| bot | proposes | answers |
|-----|----------|---------|
| `random` | now and then a random resource-for-gold swap at a random price | accepts 25% / rejects 25% of offers at random (the only bot that takes losing deals — within limits, see below) |
| `economist` | its surplus at a fair price; **loans** (gold now, ~20% more back over 15 turns) to solvent players | accepts above a small margin, one fair counter |
| `turtle` | gold for **peace** with armies that threaten it; the stone/wood its wonder stage lacks, in the build turn | accepts above a margin (values peace ×1.5), one fair counter |
| `rusher` | **tribute** (gold per turn + a 20-turn peace) from weaker neighbours its army threatens | greedy counters; peace with its target costs the spoils of conquest; honours contracts and treaties only while that pays |
| `strategist` | exploits needs (sells overflow at a premium, buys what its race lacks), borrows to fund its race, buys peace from armies at its gates, probes for sloppy traders | haggles: anchors high, concedes step by step; refuses the leader and anything that feeds a rival's race |
| `banker` | its surplus at a fair price (no loans) | accepts above a small margin, one fair counter |
| `zealot` | gold for **peace** with armies that threaten it; surplus food/wood | accepts above a margin, one fair counter |
| `spoiler` | as the strategist | as the strategist, but no peace with a rival it may have to stop |

Every bot is deterministic for a given `seed` and sequence of views. None of
them does I/O, none raises (see `SafeBot` below), and each takes a few
milliseconds per turn: the strategist averages about 9 ms per `act` and
under 2 ms per `negotiate` call at 6 players.

## Measured strength

The results below use the current constants and
`python -m agentciv.tournament --jobs 4`. Seats are shuffled and rotated, and
every game uses its own map seed. Full tables, the rule changes behind them
and more fields: docs/BALANCE.md.

| field (6 players, 240 games, seed 1, with barter) | strategist win% / avg place | next best | deals per game |
|------|------|------|------|
| A: `strategist,economist,rusher,turtle,random,random` | 51.2% / 1.93 | economist 24.6% / 2.61 (turtle place 2.48) | 16.7 |
| B: `strategist,economist,rusher,turtle,economist,turtle` | 53.3% / 2.26 | economist 17.9% / 3.83 (turtle place 3.11) | 11.5 |

The random bots place last (5.27–5.37 in field A). Without barter
(`--rounds 0`) the strategist wins 52.9% / 57.5%. Trading ablation: in the
same games (`strategist,strategist_notrade,economist,rusher,turtle,random`)
the trading strategist wins 33.8% (place 2.58), the non-trading one 24.6%
(place 2.70); in separate fields the difference is within noise
(docs/BALANCE.md §5).

The numbers below predate barter:

**Strategist against 5 copies of one bot** (60 games each, seed 3):

| field | strategist wins | avg place |
|-------|-----------------|-----------|
| 5 × economist | 100% | 1.00 |
| 5 × turtle | 86.7% | 1.15 |
| 5 × rusher | 80.0% | 1.20 |
| 5 × random | 100% | 1.00 |

**Skill ladder** (`strategist,strategist_lite,economist,turtle,rusher,random`,
120 games, seed 5): ratings strategist 35.4 > economist 23.3 >
strategist_lite 19.9 > turtle 18.4 > rusher 5.6 > random −15.0.

Reproduce with:

```
python -m agentciv.tournament --games 240 --players 6 --seed 1 --jobs 4
python -m agentciv.tournament --bots strategist,economist,rusher,turtle,economist,turtle --games 240 --seed 1 --jobs 4
python -m agentciv.tournament --bots strategist,strategist_notrade,economist,rusher,turtle,random --games 240 --seed 1 --jobs 4
python -m agentciv.tournament --bots strategist,rusher,rusher,rusher,rusher,rusher --games 60 --seed 3 --jobs 4
```

### Counterplay baselines

`python -m agentciv.tournament --jobs 8 --seed 1`, 48 games per row, std and
fog. Field A' is field A with the new bot in place of one `random`
(`X,strategist,economist,rusher,turtle,random`); field CP is
`banker,spoiler,strategist,strategist,economist,turtle`.

| field | bot | std win% / place | fog win% / place | median win turn (std / fog) |
|------|------|------|------|------|
| A' | `banker` | 62.5% / 2.38 (strategist 18.8%) | 64.6% / 2.44 (strategist 12.5%) | t67 / t68 (earliest t62 / t61) |
| A' | `zealot` | 56.2% / 2.06 (strategist 20.8%) | 64.6% / 1.81 (strategist 16.7%) | t69 / t70 (earliest t65 / t67) |
| A' | `spoiler` | 18.8% / 2.90 (strategist 16.7%) | 12.5% / 3.33 (strategist 25.0%) | t97 / t96 |
| CP | `banker` | 29.2% / 4.50 | 20.8% / 4.88 | t77 / t75 |
| CP | `spoiler` | 8.3% / 3.25 | 18.8% / 2.85 | t95 / t98 |

Like the LLM bankers, the banker wins from behind on score (in field A' std
its median score rank in its wins is 3rd, in field CP 5th of 6) and on a
fixed schedule: 9 turns after reaching the target in the median win; its
streak had been broken before the win in 1 of its 30 wins (fog: 3 of 31).
The zealot wins first on score and before t70 in 14 of its 27 wins (fog: 9
of 31). Neither result was tuned away: they are what docs/BALANCE.md §9
asks the balance experiments to measure.

Counterplay in field CP (resets of the banker's economic streak by a city
capture): std, the banker reached the target in 30 games, its streak was
reset 7 times in total, 6 times by the spoiler in 5 games; fog, 35 games, 16
resets, 13 by the spoiler in 8 games. The spoiler reset 10 (std) and 24
(fog) streaks of all players. In small fields it does better: banker,
spoiler (1v1), banker, spoiler, idle and banker, spoiler, economist, turtle,
seeds 1–6: the spoiler broke a banker streak in 10 of 18 games. It is still a
weak counter in six-player games: it must raise a force that beats a
defender with a gold hoard, walls and an archer stack, and the banker often
wins first.

## The bots

### random (`random_bot.py`)
Each turn it takes 1–5 random actions: claim an adjacent tile, build a
fitting improvement or city building, recruit random units, trade a random
amount on the market, settle, or accept or propose treaties at random. It also
moves random parts of its stacks one step in random directions. It never
plans, so it wastes resources and scatters its army.

In negotiations it proposes a random resource-for-gold swap at 60–150% of
the spot price about once every 7 turns, and accepts a quarter and rejects a
quarter of the offers it gets, at random. Because it fills seats in rated
quickmatch games it cannot be milked: it accepts at most one deal per turn and
never one that hands over land or a contract, more than half of any stock, or
more than 1.5x the market value it receives.

### economist (`economist.py`)
* **Diplomacy:** accepts every treaty and proposes 25-turn treaties to
  everyone — except, under the old relic rules, to a player on (or one relic
  short of) a relic streak.
* **Expansion:** picks city sites by the value of the tiles they would claim.
  It claims a path of tiles toward a site, settles it, and reserves the
  settlers' resources ahead of time so the market step doesn't sell them.
* **Improvements:** builds them in order of return on investment. Resources
  are valued at the current market prices.
* **Buildings:** a market hall early, and a warehouse when stock nears the
  caps.
* **Market:** sells everything above small reserves every turn, and dumps at
  almost any price what would otherwise overflow the storage cap.
* **Banking:** from turn 10 it banks the gold left after the turn's plan (up
  to the bank limit, keeping 40 gold and the turn's contract instalments;
  once the bank is at the target it keeps no reserve, since a streak turn
  counts only with half the allowance banked); earlier steps leave the
  allowance untouched.
* **Defence:** its weak spot. It keeps 2 units in the capital (and, while on
  or within 85% of a bank/legacy target, 2 in every other city, since the loss
  of any city resets the streaks), never raises walls, and when an army comes
  within 2 turns it recruits the best counter from its stock, spending at most
  15% of its gold per turn on emergency food/wood purchases. A rich economist
  is a juicy target: capturing its capital plunders half its gold and half its
  bank, and taking any of its cities ends its streak.
* **Trading:** offers its surplus (stock above its small keep levels) to the
  player who needs it most at a *fair* price (half of the estimated gain
  from the trade each — both save the market fee and slippage). A patient
  lender (it discounts future gold by only 0.5% per turn): from turn 12,
  while its own bank is under half the target, it offers
  **loans** — up to 600 gold, about 22 turns of the borrower's gold income,
  and no more than the borrower's credit limit (below),
  repaid with 20% interest over 15 turns — to one solvent player at a time
  (gold income, no defaults, able to pay, not close to winning) that has a
  use for gold; at most 35% of its gold is out on loan.

### rusher (`rusher.py`)
* **Recruiting:** from turn 0 it turns food, wood and gold into attackers,
  limited by how much upkeep its food income can support.
  * The unit type is the best counter to the target's defenders (for example,
    cavalry against archers).
  * It adds 3 siege engines per wall level of the target.
* **Targets:** it picks the rival city with the best mix of short distance and
  weak defence (original capitals first). If it stands in front of a target
  for 6 turns without attacking, it gives up on it for 15 turns.
* **Attacks:** units gather next to the target and attack together. They only
  go in when the engine's own battle procedure, run on copies of the armies,
  says they win with a 10% power margin.
* **After a capture:** it leaves 1 unit and chains on to the next capital
  until it holds enough capitals for conquest (a majority of them).
* **Economy:** thin. It builds farms and lumber mills, claims tiles, sells
  spare stone, and disbands units rather than let them starve.
* **Treaties:** accepts them only from players it is not targeting, and never
  proposes one.
* **Extortion:** demands tribute — gold per turn for 10 turns plus a 15-turn
  peace — from a weaker rival its army threatens (army within reach ≥ 1.3×
  their defence), but not from its current target unless that assault has
  stalled. Peace with its target is worth minus the expected spoils
  (plunder + 250, scaled by how feasible the assault looks), so buying it
  off costs real money. Counters are greedy (asks 65%, concedes 25%).
* **Opportunist:** reserves gold for its contract instalments only while the
  payee's army is at least 70% of its own (otherwise the army gets the gold
  and the contract may default). It breaks a peace treaty (50 × (1 +
  betrayals) influence) when the partner defaulted on tribute to it, or when
  the partner no longer pays, its army near the partner is ≥ 1.8× the
  partner's defence and the spoils exceed `common.break_cost` (influence,
  legacy, bank share, bond, the bank fee on an offered bond, cancelled tribute
  and dearer treaties later).

### turtle (`turtle.py`)
* **Diplomacy:** proposes 30-turn treaties to everyone and accepts every
  proposal (not from relic runners). Treaty partners cannot enter its land.
* **Defence:** raises capital walls over the game (level 1, then 2 from turn
  30, then 3 from turn 70), keeps a standing archer garrison (archers are
  ×1.5 in their own city and ×1.5 against infantry), and adds archers and
  walls when an army comes within 3 turns.
* **Path:** from turn 15 it commits to one peaceful victory, re-checked every
  5 turns: the **wonder** (it reserves the next stage in the capital and
  builds it as soon as the stage plus the stone/wood it must buy on the
  market that turn is affordable — later stages cost more than the storage
  cap) or **influence** (temples on every tile that can hold one, buying the
  stone for them). It picks influence only when its estimate is clearly
  faster than the wonder (×0.65), and never abandons a wonder at stage 2+.
* **Trading:** **buys peace** — offers gold for 30 turns of peace to the
  rival whose army threatens it most (it values peace 1.5× the shared
  threat estimate) — and pays a tribute demand when the peace is worth
  more. In the turn it can afford the next wonder stage it **bids for the
  missing stone/wood** from players with spare stock (cheaper than the
  market with slippage and fee; never above its storage cap otherwise). It
  sells surplus food.

### strategist (`strategist.py`)
It uses the economist's economy, then adds these behaviours on top:

1. **Victory ETA model.** Every turn it estimates, for itself and every
   rival, how many turns each condition is away.
   * **Economic and influence:** turns to reach the bank/legacy target, from
     how fast bank and legacy grew over the last 6 turns (for its own bank at
     least min(bank limit, potential gold income), for legacy at least the
     influence income), plus the streak turns still missing; infinite while
     the player does not hold its original capital.
   * **Wonder:** the remaining stage costs valued at market prices divided
     by production value, and (for rivals) the value of the stages built
     per turn since their first stage.
   * **Relics** (only in rules with a relic victory, i.e. replays of games
     before the g7–g10 retune): the streak of guarded relics; for itself the
     walking distance to the missing relics plus a stall penalty when a
     campaign makes no progress.
   * **Conquest:** capitals held.

   Its own race is the lowest ETA among economic, wonder and influence (plus,
   under the old rules, relics when that ETA ×1.5 is still the lowest), with
   hysteresis. Once the ETA is within 30 turns (45 for
   influence, whose temples pay late) it commits:
   * **Wonder:** reserves the next stage (stone/wood up to the storage cap),
     buys the rest on the market in the build turn.
   * **Economic:** banks every turn (as the economist) and raises the bar
     for investments.
   * **Influence:** temples everywhere (buying their stone), influence kept.
   * **Relics** (old rules only): a campaign (see 3).
2. **Raids.** Every other turn it looks for a rival original capital or
   wonder city — or any city of a rival on an economic or influence streak,
   whose loss resets that streak — whose capture is worth at least twice the cost of the strike
   force (value: plunder = half the owner's resources, conquest progress,
   wonder denial, and a large bonus when the owner would otherwise win
   first). The force (siege against walls plus the cheapest of cavalry,
   infantry, archers or a mix) must beat the defenders, their neighbours and
   one turn of emergency recruiting (the owner's stock plus 20% of its gold).
   It gathers out of sight (3 tiles, 5 for an all-cavalry force), breaks a
   treaty first if needed (free against a player on a streak), then strikes; a raid that stalls is abandoned and
   that rival left alone for 15 turns. Raids are expensive, so they stay rare.
3. **Relics.** Relics are taken by occupation; they pay 3 influence a turn
   guarded and 1 unguarded. In a campaign (old rules only) it keeps a guard
   on every held relic (at least 4 units, enough to hold against armies 5
   turns away), marches detachments onto the cheapest missing relics, fights
   for guarded ones and recruits what the guards need. Outside a campaign it
   parks one unit on free relics next to its army.
4. **Threat assessment.** Each hostile army is assigned to the city it is
   closest to (an army attacks one city at a time); units sitting in their
   own city count 40%. The threatened city raises walls first, then recruits
   the best counter (buying food/wood if needed) until the simulated assault
   fails with a 1.1 margin.
5. **Diplomacy.** It proposes and accepts 40-turn treaties with militarily
   stronger players (and far-away ones that are not hoarding gold), strongest
   first since treaty slots are limited, never
   with a player close to winning, running a wonder (or, under the old rules,
   a relic streak), or
   with its raid target.
6. **Market.** It sells surplus before it overflows the caps. Voluntary sales
   are split so that the batch price stays within about 8% of the spot price;
   the rest waits a turn.
7. **Trading — the skilled trader.**
   * *Haggling:* it accepts an offer only if it leaves it a share of the
     estimated joint gain that starts at 80% (anchoring) and drops by 12
     points per counter in the thread, to 50% (45% in the last round of a
     turn). Otherwise it counters (up to 3 times per thread), moving the
     gold term to that share but conceding at least 30% of the gap between
     its last offer and theirs, so the positions converge.
   * *Exploiting needs:* it sells only what would overflow its storage caps
     (its stock feeds its own growth; all surplus when racing for gold),
     only to players who need it, at a
     price leaving the buyer just its acceptance margin and at least 10%
     above what the market would pay. It buys the stone/wood its wonder
     needs (up to the storage cap, or all of it in the build turn) from
     players with spare stock.
   * *Never feeding a rival:* it refuses deals with the leader once the
     leader threatens to win first (or has 45% progress), never gives a
     contender (a rival whose victory ETA is not far behind its own) what
     its race runs on (gold for the economic race; stone, wood, gold for the
     wonder; stone, gold for temples), never signs peace with a contender or
     a raid/block target, and sells no food/wood to armies that could march
     on it (or on anyone). Stone for a wonder builder is fine only while it
     does not make stage 3+ affordable (guard 0.55 instead of 0.7).
   * *Funding its race with contracts:* when committed to the wonder or
     influence race (ETA ≤ 35) and short of gold, it borrows (up to 1500,
     half the lender's gold, instalments ≤ 30% of its potential gold
     income) for 30 turns at 25% — from the leader too, whose gold then
     leaves it. Before the next-but-one wonder stage it pre-finances that
     one as well (lenders only check whether the *next* stage becomes
     affordable). Its own instalments only count until its expected victory
     (ETA + 3 turns), so long loans are cheap for it. It never pays interest
     on loans others push on it.
   * *Tribute:* accepts gold for peace only from players it has no plans
     against; buys peace itself from an army at its gates when that is
     cheaper than a war (so it marches on someone else).
   * *Opponent modelling:* it offers to buy partners' spare stock at 40% of
     the spot price. Rational bots refuse (and the offer then waits longer
     each time, 4 up to 16 turns); a partner that accepts gets them again.

`strategist_lite` is a handicapped strategist (fixed economic race, no raids,
no relic campaigns, short treaties) used for the skill ladder in
docs/BALANCE.md. `strategist_notrade` is the strategist with `TRADE = False`
(it never negotiates; same random tie-breaks), the baseline for measuring
what barter is worth.

### banker (`banker.py`)
Modelled on the LLM players of g8–g10, who reached the bank target while
fourth on score, banked no more than the streak needed once there, stacked
archers in the capital and held the 10-turn streak.
* **Bank first:** from turn 6 the turn's allowance (50 + 10 per city with a
  market hall, rules §5) is reserved before any other spending and banked
  at the end of the turn. Once the bank holds B it banks exactly the streak
  deposit (half the allowance) and keeps the rest.
* **Market halls in every city**, the capital's first (buying the stone/wood
  if needed): each adds 10 to the allowance. A warehouse when near the caps.
* **Expansion:** up to 4 cities, no new site once the bank is at half of B.
* **Holding the cities** (`garrison.py`, `GarrisonMixin`): an archer stack in
  the capital growing with the bank (14 at B), walls there from 40% (level
  1) and 80% (level 2) of B, 1 defender in every other city, 2 once near the
  target (`STREAK_CITY_GARRISON`); threatened cities recruit counters and
  raise walls as every planner bot does. A lost original capital is retaken
  (no streak without it).
* **Diplomacy and trade:** accepts every treaty and proposes 30-turn
  treaties to everyone; sells its surplus at a fair price; lends nothing.

### zealot (`zealot.py`)
A temple rush for the influence victory, with the minimum army.
* Influence is valued high from turn 0 (`INFLUENCE_WEIGHT` 6, temple ROI
  ×3); temples go on every plains/forest/hills tile, and the stone they need
  is bought on the market. Claims spend influence but not legacy, so it
  claims freely; up to 6 cities.
* **Relics:** occupies up to 2 relics within 6 steps of its cities with 2
  infantry each (3 influence a turn guarded), only where no hostile army is
  near.
* **Army:** the `GarrisonMixin` again: a capital archer stack growing with
  legacy (10 at L), capital walls from 60% / 90% of L, 1–2 defenders in other
  cities, a lost capital retaken. Never breaks a treaty (that would end its
  own influence streak); accepts and proposes 30-turn treaties.
* **Trade:** buys peace from armies that threaten it, sells surplus
  food/wood. No bank, no wonder, no raids.

### spoiler (`spoiler.py`)
The strategist's economy and opening, then stopping the leader becomes its
main job. A `StrategistBot` subclass; everything not listed here is the
strategist's.
* **Whom:** from turn 20, every turn, the rivals ranked by their estimated
  turns to win (the strategist's ETA model; conquest only counts one capital
  short of it). A rival is a target when it is on a streak, has 45% progress
  in economic, influence or wonder, a wonder at stage 3+, or an ETA within
  30 turns. It looks at the two most dangerous.
* **Where:** on a streak any city of the rival (its loss resets both
  streaks); before the streak the original capital (half the bank is
  plundered, a quarter of the legacy lost); against a wonder the wonder city;
  against conquest a capital. Among those the city whose strike force is
  cheapest to raise and bring, counting the units it already has and half
  its stock; targets it cannot reach before the rival's expected win (+6
  turns) are skipped.
* **Sizing the force (no suicide into walls):** the force must win the
  engine-exact simulation (`simulate_attack`) with a 1.3 margin against the
  garrison, walls (3 siege per wall level), the defenders next door, one
  turn of emergency recruiting from the target's stock and 20% of its gold,
  and 2 turns of recruiting from its income (it sees the force coming). The
  force is what is already within 12 steps plus the cheapest addition of
  cavalry, infantry or both (archers only pay off defending a city); missing
  units are bought with up to half its gold (food/wood/stone on the market)
  in the city nearest the target.
* **How:** it gathers 3 steps away (cavalry 5), marching around the rival's
  other cities, and strikes when the gathered force wins. While gathering it
  switches to another city of the same rival when the units near it would
  win there now, or every 3 turns when that city is much cheaper (a target
  that walls up leaves its other cities open). A raid on the most dangerous
  rival never times out. In fog games it buys a military spy report on the
  target every 4 turns before relying on the armies it can see.
* **Its own race waits** while a rival would win first (no wonder or temple
  reservations); it signs no treaty and no peace deal with a rival at 30%
  progress, within 45 turns of winning or on a streak, and uses the free
  treaty break against streak holders (rules §9).

## Barter (§13)

All planner bots (economist, rusher, turtle, strategist) share the
negotiation machinery of `trading.py` and the valuation of `common.py`;
each bot supplies its needs, its peace preferences and its own proposals.

**Valuation (`DealValuer`, `common.py`).** Every term of a deal is priced in
gold, for any player, from public information:

* **Resources** have a marginal value. The part a player *needs* (stock
  below its target: keep levels, a winter food buffer, the next contract
  instalments, the next wonder stage or temples, investments waiting for
  gold) is worth what buying it on the market would cost (spot + slippage +
  fee); the rest is worth what selling it would bring (spot − slippage −
  fee). The difference is what makes a trade good for both sides. Other
  players' needs are estimated (a wonder builder needs its next stage;
  above its storage cap only if it can build this turn).
* **Tiles:** yield over (part of) the remaining game; more for the giver when
  the tile touches its city. A received tile is worth **nothing** while an
  army of another player that is not bound to us by a treaty (or by the
  deal's own peace) for 10+ more turns could march onto it within 3 turns —
  undefended land is captured by moving onto it, so the seller could simply
  take it back.
* **Contracts:** instalments discounted per turn (0.97; the economist 0.995)
  and, for the receiver, weighted by the payer's reliability: reputation
  (defaults, betrayals, contracts honoured) and ability to pay (income incl.
  sellable production minus existing obligations). A bot may cap its own
  payments at a horizon (the strategist: its expected victory). **Credit
  limit:** when the bot hands over goods now against instalments later (a
  loan), it counts at most 150 gold of instalment value per payer, ×(1 +
  contracts honoured, max 4), and nothing after a default.
* **Peace:** base value plus the threat the other side poses — its army
  within 3 turns of our cities against our defence — scaled by the length;
  plus a bot-specific bias (e.g. −(spoils of conquest) for the rusher's
  target, −400 for relic runners).
* **The guard:** `danger(world, q)` is `q`'s best victory progress
  (conquest only once a rival capital is taken); `helps_winner` also
  projects the deal (economic: bank plus net gold, at most 10 turns of the
  receiver's bank limit; next wonder stage affordable). Contract valuation
  counts the bank: a default takes the rest of the obligation from the
  payer's bank, so part of a payer's bank is collateral in its credit limit. Bots never deal with a player at 0.7+ (strategist 0.55) or
  one the deal would bring there.
* **War supplies:** no bot sells food or wood to a hostile army that can
  reach it, to a conqueror (2+ capitals) or to an army 1.5× the average.

**Negotiation loop (`Trader`, `trading.py`).** One call of
`negotiate(view)`:

1. Incoming deals: rejected if they cannot settle, come from a refused
   partner, would help a near-winner or fail the bot's veto; accepted if the
   gain clears `ACCEPT_MARGIN` (2) + `ACCEPT_FRACTION` (1.5%) of the deal's
   size (or the bot's own threshold); otherwise countered — gold moved to
   the bot's share of the joint surplus, conceding `CONCEDE` of the gap to
   the last offer — while the thread has counters left (`COUNTER_LIMIT`),
   accepted when the remaining gap is crumbs, else rejected. At most one
   acceptance per round (valuations use the stock before the round).
2. Own open proposals that turned bad are withdrawn.
3. New proposals (`trade_proposals`), at most 2–3 per turn and one open deal
   per partner. The kind travels in the message (`"[sell] 60 stone for 120
   gold"`, `[buy]`, `[loan]`, `[peace]`, `[tribute]`, `[bargain]`); a
   rejected or expired kind cools down per partner (4, 8, 12, 16 turns).

Negotiation leaves no traces in `act()` state (tested: a dry run that
evaluates everything but sends nothing plays exactly like no negotiation).
In `act()`, planners reserve the gold (or other resource) their contract
instalments need this turn beyond what this turn's income covers (wonder
builds with market purchases keep it too; emergency defence does not).

## Building blocks (`common.py`, `planner.py`)

Both modules are useful if you write your own in-process bot.

* `World(view)` is an index-based snapshot of a view, with tile index
  `y * width + x`:
  * flat `terrain` and `owner` arrays, plus `cities`, `armies` and `players`
    looked up by tile or id;
  * relations: `at_peace`, `hostile`, `can_enter_fn(pid)`;
  * treaty limits (rules §9): `treaties_held`, `treaty_slots`, `betrayals`,
    `bond_required`, `bond_free`, `break_influence`, and `sign_problem(a, b)`
    (cooldown, slots or bond, from public view data);
  * geometry: `cheb`, `manhattan`, `radius`, `nb` (4-neighbours);
  * `bfs(sources, can_enter, max_dist)` gives BFS distances, and
    `step_towards(src, dist)` gives the next step.
* `simulate_attack(world, attacker, units, tile, ...)` runs the engine's own
  `combat.resolve` on copies of the armies. It includes garrison, walls,
  siege, terrain, archer city bonus and counters, and returns
  `(win, survivors, power_ratio)`.
  * `assume_war=True` treats treaty partners as enemies, to evaluate an
    attack after breaking a treaty; `break_cost(world, q)` estimates the
    influence and gold-equivalent cost of breaking the treaty with `q`.
  * `threat_to(world, tile, reach)` lists hostile units that can reach a tile
    within `reach` turns.
  * `best_counter(enemy_units)` picks the unit type to field against them.
* Economy helpers:
  * `tile_yield`, `raw_income` and `best_improvement` (ROI, including
    depletion of deposits);
  * `food_projection`, which walks through the coming seasons;
  * `sell_price` and `buy_price`, which give the exact batch-auction price
    for a volume.
* `Plan(world)` is an order list with a resource budget.
  * `claim`, `improve`, `build_city`, `settle`, `recruit`, `move`, `sell`,
    `buy`, `propose`, `accept_treaty`, `message`, … (`propose` and
    `accept_treaty` take an optional `bond` and skip partners the treaty
    limits exclude).
  * Each method emits an order only if it passes the same checks as the
    engine's pre-validation and can be paid for. It also tracks what was
    already planned this turn: claimed tiles, moved units and the tile count
    for claim costs.
* `SafeBot` is a `Bot` base class. Its `act()` never raises (on an error it
  returns the orders planned so far), nor does its `negotiate()` (it calls
  `decide_deals(view)`; `TRADE = False` switches barter off), and it
  provides a seeded `self.rng` and `self.memory` across turns.
* `GarrisonMixin` (`garrison.py`), for planner bots on a streak:
  `keep_garrisons(capital, others, walls)` recruits toward a capital stack,
  a number of defenders in every other city and a capital wall level;
  `retake_capital()` raises an army and retakes a lost original capital.
* Barter (see [Barter](#barter-13)): `DealValuer(world, needs, gold_need,
  discount, horizon, peace_bias)` with `deal_gain(deal, pid)`,
  `recv_value`/`give_cost`, `contract_value`, `peace_value`, `threat`,
  `helps_winner`; `danger(world, pid)`; `spot_prices`,
  `contract_obligations`. The `Trader` mixin (`trading.py`) runs the
  negotiation loop; override `trade_setup`, `trade_needs`,
  `trade_horizon`, `peace_bias`, `refuse_partner`, `veto`,
  `accept_threshold`, `counter_share` and `trade_proposals`, and build
  proposals with `priced` (split the joint gain), `sale_offers`,
  `purchase_bids` and `peace_offers`.
* `PlannerBot` (in `planner.py`) is a `SafeBot` whose turn is a pipeline of
  behaviours:
  * `diplomacy`, `food_safety`, `plan_site`, `defend`, `counter_relics`,
    `sell`, `expand`, `develop`, `garrison_moves`;
  * `offense(target)`: gather and assault; `recruit_army`; `min_force`;
  * `build_with_market(city, building)`: build a level, buying the missing
    stone/wood in the same turn (only if the whole purchase is affordable);
  * `city_threat(c)`: hostile units that could reach city `c` and have no
    other of our cities closer; `reinforce` raises walls and recruits
    counters (buying food/wood if `DEFENSE_BUY`);
  * contested claims/settles get a random back-off of 1–4 turns, and ties
    between equally good tiles are broken by a per-game random salt (index
    order would favour one map direction).

  Class-level knobs tune it, for example `INFLUENCE_WEIGHT`,
  `DEFENSE_MARGIN`, `MIN_GARRISON`, `SELL_FLOOR`, `DEFENSIVE_WALLS`,
  `DEFENSE_BUY_FRACTION`, `BUY_FOR_IMPROVEMENTS`, `COUNTER_RELICS`, and for
  barter `ACCEPT_MARGIN`, `ACCEPT_FRACTION`, `COUNTER_LIMIT`,
  `COUNTER_SHARE`, `CONCEDE`, `WIN_GUARD`, `DISCOUNT`, `PEACE_SCALE`,
  `MAX_NEW_PER_TURN`. A `PlannerBot` is also a `Trader`; it reserves its
  contract instalments (`honour_contract(c)` decides per contract). The
  economist, rusher, turtle and strategist are all `PlannerBot` subclasses.

## Writing your own bot

**In-process** (for the server's house bots and the tournament runner):

```python
# agentciv/bots/mybot.py
from agentciv.bots.planner import PlannerBot

class MyBot(PlannerBot):
    name = "mybot"
    DEFENSE_MARGIN = 1.5            # tweak a knob...

    def pipeline(self):             # ...or reorder/replace behaviours
        return [self.diplomacy, self.food_safety, self.plan_site, self.defend,
                self.sell, self.expand, self.develop, self.my_plan, self.garrison_moves]

    def my_plan(self):
        w, p = self.w, self.p       # World snapshot and budgeted Plan
        for c in w.my_cities:
            p.build_city(c, "walls", self.reserved)
```

Register it in `agentciv/bots/__init__.py`:

```python
REGISTRY["mybot"] = "agentciv.bots.mybot:MyBot"
```

Then try it with
`python -m agentciv.tournament --bots mybot,strategist,economist,rusher,turtle,random --games 40`.
For full control, subclass `SafeBot` and implement `decide(view) -> list`
(and `decide_deals(view) -> list` to barter). Or subclass the plain `Bot`
and implement `act(view)` (and optionally `negotiate(view)`), but then you
must never raise.

**Remote** (any language): implement the same view → orders function
yourself and play over HTTP (docs/DESIGN.md §12) or with the Python client
SDK (`agentciv/client.py`). A few tips from building these bots:

* **Always submit**, even an empty list.
* **Relics are occupied, not claimed**: stand on them, and keep units there
  (a guarded relic pays 3 influence a turn, an unguarded one 1).
* **Contested claims fail for everyone**: if a rival keeps claiming the same
  tile, try another one for a turn or two.
* **Check `events`** for `order_failed` entries. Resources are only checked
  at execution time, in the order you submitted.
* **Reserve resources for big purchases before you sell.** The market
  resolves before actions, so gold from sales can be spent the same turn.
* **A settle raises the claim cost** for later claims in the same turn,
  because the new city's tiles count.
* **Recruits appear after combat.** They cannot defend against an attack in
  the same turn, so react to armies 2–3 turns away.
* **Simulate battles exactly.** The rules are deterministic; use
  `agentciv.engine.combat` or copy the formulas.
* **Treaty partners can't enter each other's land.** To stop a partner,
  gather at the border first, then `break_treaty` (see `you.treaty.break_preview`
  for the influence, legacy and gold it costs). You can fight that turn but
  only walk in two turns later.
* **Barter beats the market** by the fee and the slippage on both sides —
  but only sell what you would otherwise sell anyway (the built-in bots
  learned that the hard way: a strategist selling its "surplus" stone and
  wood lost 7 points of win rate), never feed the player about to win, and
  don't sell food to the biggest army around.
* **Contracts outlive games**: instalments after the game has ended are
  never paid, and a default only costs 25 influence and reputation. Lend to
  players with income and a clean record; borrow long when you are close
  to winning.

## Tournament runner

```
python -m agentciv.tournament --bots strategist,economist,rusher,turtle,random,random \
    --games 40 --players 6 --seed 1 [--max-turns 150] [--jobs 4] [--rounds 3] [--json out.json]
```

* **Negotiation:** every turn starts with `--rounds` (default 3, §13.6)
  negotiation rounds. In each round every living bot, in a seat order that
  rotates by turn and round, gets a fresh view and its `negotiate` actions
  are applied at once (`Game.diplomacy`). `--rounds 0` plays without
  barter.

* **Isolation:** games run in-process against the engine, and each game gets
  fresh bots.
* **Map seeds:** game `i` uses map seed `seed*10007 + i`.
* **Seats:** a seeded shuffle, rotated per game, on top of the engine's own
  seeded start shuffle.
* **Duplicate names** get suffixes (`random#1`, `random#2`).
* **More bots than `--players`:** each game samples a subset.
* **Fewer bots than `--players`:** the list repeats.
* **`--jobs N`:** runs games in N processes. The results are identical to a
  serial run.

The report lists per bot:
* games, wins, win rate, average placement and average score;
* OpenSkill rating (`agentciv.ratings`, displayed as mu − 3σ);
  bots tied on score share a rank (`Game.placement_ranks()`, the server's
  rule), so ties count as draws in the rating and as shared places and wins
  in the other columns, never by seat order;
* wins by condition;
* pre-validation errors per game and think time per turn (`act`), plus
  `negotiate` time per turn, per call and the maximum;
* trade statistics per game: proposals and counters sent, deals accepted,
  deals executed (either side), peace deals, contracts as payer / payee,
  defaults, instalments and gold paid / received, betrayals, contracts
  honoured, diplomacy actions rejected by the engine, and the net value
  received at base market prices (`trade_per_game`).

* forced-replanning counters (below): streak resets by a city capture it
  caused (`streak_breaks_by_per_game`) and suffered (`streak_resets_per_game`).

It also shows overall stats: the distribution of ending conditions, game
length (median and average) and time per game, and the win rate by **start
slot** (the index of the start position in `mapgen.start_layout(n)`, i.e. the
map position independent of the seed's orientation) and by seat (`p1`…),
and deals per game in total and by kind (the `[kind]` tag of the built-in
bots' deal messages).
Six identical bots measure positional fairness by start slot. `--json` writes
the full summary, including per-game results (seats with their start slot,
placements, scores, errors, timings).

**Forced replanning** (`replan` per game, `replanning` in the summary,
a block in the text report). These measure whether a race gets interrupted,
not who wins:

* **streaks** per condition: started, paused by reason (`deposit`), ended by
  reason (`city_lost`, `treaty_broken`, `contract_default`, `eliminated`, or
  `unmet` when the requirement simply stopped holding);
* **streak_breaks**: every streak reset by a city capture, with the turn,
  the victim, the conditions and the capturers;
* **share of streak winners whose streak was broken** at least once before
  the win (any `streak_ended` of the winning condition), and by a capture;
* **lead changes** in victory progress: the leader is the player with the
  best economic, influence, wonder or conquest progress (`Game.stats`),
  counted from the first turn that progress reaches 0.25; a new leader must
  be strictly ahead (mean, median, max per game);
* **turns from first reaching the target to the win** (bank for economic,
  legacy for influence wins; 9 is the minimum);
* **winners never attacked after turn 30**: no battle on (or, for a border
  clash, next to) a tile the winner owned at the start of the turn and no
  city captured from it after turn 30 (armies walking onto undefended land
  do not count);
* **win conditions per field** (the multiset of bots in the game), so mixed
  schedules are never only pooled.

For programmatic use: `run_game(bot_specs, seed, max_turns, rounds=3)`
returns one game's result dict (with `ranks` aligned with `placements`, `trade`, `deal_kinds`,
`deals_executed`, `replan`, `negotiate_ms_*`). `run_tournament(...)` returns the summary, and
`format_summary(...)` renders it.
