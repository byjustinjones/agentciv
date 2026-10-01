# AgentCiv — Balance notes

This document records **why** the rules have their current numbers and
**how well** they work, measured with the built-in bots. The binding rules are
docs/DESIGN.md (contract) and docs/RULES.md (generated from
`agentciv/engine/constants.py`). Every table below can be reproduced with the
commands listed in §4 (Python 3.11, `--jobs 4`, results are deterministic for
a given seed).

## 1. Problems we started from (6 players)

Measured with the mixed field `strategist,economist,rusher,turtle,random,random`:

* Games ended after ~20–28 turns (average 27.7 in a 24-game run).
* Relics decided 54–82% of games. At 6 players 3 of the 5 relics clustered
  around the centre ((11,11), (13,11), (10,13)) and 2 sat in corners, the
  same layout in every game; a single city claimed two relics with influence.
* The economic victory (2000 gold) was reached by turn ~29 just by selling
  production; food fell to ~0.35 gold because every pool drifted back only
  5% per turn.
* The influence victory was almost unreachable (1–2% of games); the wonder
  (turtle, turn ~40–45) took over as soon as relics/economic were harder.
* The 3 starting infantry (30 strength) beat an undefended capital's
  garrison (20), so turn-5 capital snipes decided games.

## 2. Rule changes and why

### Relics (placement, control, victory)

(The relic victory below was removed in the g7–g10 retune, §9; placement and
control are unchanged, income is now 3 guarded / 1 unguarded.)

| | before | now |
|---|---|---|
| count | `n//2 + 2` | `n` — one relic in every gap between two neighbouring capitals |
| placement | fixed cluster near the centre + corners | ring around the centre; each relic (nearly) equidistant from the two capitals flanking it; symmetric under the start layout's symmetries; ≥ 3 tiles apart; ring radius and map orientation vary by seed |
| acquiring | `claim` with influence (and city founding) | **occupation only**: units alone on the tile at the end of movement take it (also when unowned); cities never claim or hand over relics |
| victory | own `floor(R/2)+1` for 10 turn-ends | own **and guard** (units on the tile) `ceil(R/2)` (a majority if R < 4) for **16** consecutive turn-ends |
| income | 3 influence per relic | 2 influence per owned relic |

Why: interleaving relics between neighbours makes every relic a contested
border objective instead of a free prize for whoever is nearest to a cluster.
Occupation makes relic control military and visible (armies on the map), so a
streak can be answered by taking one guarded relic (which resets it). With
R = n the "majority" rule (4 of 6) was almost never achieved, so the victory
needs half the relics; 16 turns is the shortest hold that keeps relics from
dominating while still ending ~9% of games (15 turns: 17–27% of field-A
games; 18 turns: ≤ 3%).

**Equidistance needs matching parities.** Two tiles can be at the same
4-directional path distance from some tile only if their `x+y` parities
agree. At 6 players two of the diamond starts had the other parity, so the
relic between them and a neighbour was always one step closer to one side —
that class of starts won 30–35% of 6-strategist games. `start_layout` now
moves minority-parity starts by one tile (keeping the half-turn symmetry;
layouts with a quarter-turn symmetry keep mixed parities because all their
starts are equivalent anyway). At 6 players every capital now has relics at
path distances (6, 6, 10) for the three relics a victory needs.

### Map fairness (all player counts)

* The start template is now **mirror-symmetric** about its facing axis, so
  mirrored starts get identical neighbourhoods.
* **Equal land**: land that makes a start's uncontested region (tiles closer
  to it than to any other start) larger than the smallest one is sunk
  (farthest first, random tie-breaks), then every region gets the same
  number of hills, forest and gold tiles. On a square map some starts used to
  own a whole corner (+10% land); tied (contested) land now does not count
  toward the equalised amount.
* The finished map is **turned/mirrored by seed**, so no start position is
  tied to a fixed map direction (bots — and agents — that break ties by tile
  index otherwise favour one direction).
* Result: 6 identical strategists win 9–23% from every start slot over 240
  games (target 1/6 ± 0.08), and 15–21% from every seat.

### Early game: garrisons

Capital garrison 20 → **40**, city garrison 10 → **15**. The starting army
(30) can no longer take an undefended capital; a real rush (5+ infantry, or
fewer with cavalry against archers) still can, and rushers still win 12% of
field-A games by conquest. Threatened bots now raise walls (cheap, and a wall
level multiplies the whole defence) before recruiting.

### Conquest

`ceil(n/2)` → **a majority** (`floor(n/2)+1`, 4 of 6) of the original
capitals. With two weak players in a game, capturing their two capitals used
to end the game at turn ~22.

### Market

Pools revert **25%** (was 5%) of the way to their initial reserves each turn
— an outside demand/supply that makes prices recover within a few turns.
Food now trades around 0.4–0.85 (base 1.0; wood ~0.9–1.3, stone ~1.5–2.0) instead of sitting at 0.35,
while heavy simultaneous selling still pushes prices down (six economists
selling everything never reach the economic goal in 150 turns). We did not
add a separate NPC buyer: stronger reversion is that buyer, with one number.

### Victory thresholds and costs (targets: ~70–110 turns per peaceful race)

| | before | now |
|---|---|---|
| economic | 2000 gold | 13500 gold (replaced by a banked target, §7) |
| influence | 600 | 3350 (replaced by a legacy target, §7) |
| wonder stage k | 60·k stone, 40·k wood, 40·k gold | **165·k stone, 120·k wood, 130·k gold** (total 2475 / 1800 / 1950) |
| temple | 30 stone + 30 gold, +2 influence | **20 stone + 20 gold, +1 influence** |
| relic influence | 3 | **2** |
| score | 25/capital, 20/wonder stage, influence/5, gold/20, 10/relic | 50/capital, **60/wonder stage**, influence/6, gold/25, 15/relic |

Selling production alone is no longer a fast win: an economist needs a grown
economy and ~75 turns even with nobody competing on the market. The later
wonder stages cost more than the storage cap, so the missing stone/wood is
bought on the market in the build turn (the market resolves before actions
and caps apply at the end of the turn) — a visible, contestable race. The
temple was halved (with a lower cost) because +2 influence per tile made the
influence race four times faster than any other once a player had 40 tiles.
Score weights were rebalanced so that progress on every path counts at the
turn limit (a 5-stage wonder was worth less than 2000 hoarded gold).

Race lengths of a single well-played racer with **nobody competing**
(5 idle opponents, 40 games each, median turn of victory):

| racer | median | p25–p75 |
|---|---|---|
| economist (economic) | 75 | 74–76 |
| turtle (wonder) | 79 | 77–80 |
| turtle forced to influence | 76 | 75–78 |
| strategist forced to economic | 72 | 71–74 |
| strategist forced to wonder | 60.5 | 60–62 |
| strategist relics (nobody contests) | 31 | 30–36 |

In contested fields the median victory turns are: field A — wonder 76,
economic 84, conquest 71, influence 104.5, relics 32.5; field B — wonder 89,
economic 95.5, influence 96, conquest 92, relics 31.5.

## 3. Bot changes (so they play the new rules well)

All planner bots:
* never claim relics; contested claims/settles get a random 1–4 turn back-off
  (two bots retrying the same tile failed forever); ties between equally good
  tiles are broken by a per-game random salt instead of tile index;
* each hostile army only threatens the own city it is closest to (armies in
  their own cities count partly for the strategist); threatened cities raise
  walls first and may buy food/wood for defenders;
* `build_with_market` buys the stone/wood a wonder stage needs in the build
  turn; `counter_relics` attacks the weakest guarded relic of a hostile player
  on a relic streak; no treaties with relic runners.

Specialists: the **economist** is the vulnerable hoarder (no walls, at most 15%
of its gold per turn on emergency defence, 25-turn treaties); the **turtle**
chooses wonder or influence (temples everywhere, buying their stone) by ETA,
preferring the wonder; the **rusher** is unchanged in spirit.

**Strategist**: picks the fastest of economic / wonder / influence (relics only
when ×1.5 faster), commits earlier to influence, occupies and guards relics in
a relic campaign, raids (plunder/wonder denial) only when the value is ≥ 2×
the cost of a strike force that beats one turn of emergency recruiting,
gathers out of sight and strikes at once, and signs 50-turn treaties with
strong armies. Raids are rare by design: in tests, more aggressive raiding
lowered its win rate (every raid is expensive and invites retaliation).
`strategist_lite` (fixed economic race, no raids/relics, short treaties) is
the handicapped version used for the ladder.

## 4. Results (current constants)

These tables were measured **before barter** (docs/DESIGN.md §13). Since
then every tournament turn starts with 3 negotiation rounds; §5 has fields A
and B with barter, the same fields without it (`--rounds 0`, current bots)
and the trading ablation. The other tables in this section (5 copies,
fairness, other player counts, ladder) have not been re-run with barter.

Commands (all 6 players unless noted; `--jobs 4`):

```
python -m agentciv.tournament --bots strategist,economist,rusher,turtle,random,random --games 240 --seed 1 --jobs 4
python -m agentciv.tournament --bots strategist,economist,rusher,turtle,economist,turtle --games 240 --seed 1 --jobs 4
python -m agentciv.tournament --bots strategist,economist,economist,economist,economist,economist --games 60 --seed 3 --jobs 4   # also rusher/turtle/random
python -m agentciv.tournament --bots strategist,strategist,strategist,strategist,strategist,strategist --games 240 --seed 1 --jobs 4
python -m agentciv.tournament --bots strategist,strategist,strategist,strategist,strategist --games 100 --seed 1 --jobs 4
python -m agentciv.tournament --bots strategist,strategist,strategist,strategist,strategist,strategist,strategist,strategist --games 96 --seed 1 --jobs 4
python -m agentciv.tournament --bots strategist,economist,rusher,turtle,random --games 100 --seed 2 --jobs 4
python -m agentciv.tournament --bots strategist,economist,rusher,turtle,economist,turtle,random,random --games 96 --seed 2 --jobs 4
python -m agentciv.tournament --bots strategist,strategist_lite,economist,turtle,rusher,random --games 120 --seed 5 --jobs 4
```

### Field A — `strategist,economist,rusher,turtle,random,random` (240 games)

| bot | win% | avg place | rating | wins by condition |
|---|---|---|---|---|
| strategist | **52.9%** | **1.87** | 41.6 | wonder 78, relics 36, conquest 10, influence 3 |
| economist | 21.7% | 2.64 | 27.7 | economic 52 |
| turtle | 13.8% | 2.50 | 31.9 | wonder 28, influence 5 |
| rusher | 11.7% | 3.25 | 21.9 | conquest 27, economic 1 |
| random ×2 | 0% | 5.36–5.37 | −6.4 / −6.9 | – |

Endings: wonder 44%, economic 22%, conquest 15%, relics 15%, influence 3%,
turn limit 0%. Game length: median 78.5 (min 29, max 119).

### Field B — `strategist,economist,rusher,turtle,economist,turtle` (240 games)

| bot | win% | avg place | rating | wins by condition |
|---|---|---|---|---|
| strategist | **54.2%** | **2.38** | 27.8 | wonder 61, influence 60, relics 6, conquest 2, economic 1 |
| turtle #1 / #2 | 9.2% / 15.0% | 3.03 / 3.10 | 20.6 / 19.6 | wonder 42, influence 16 |
| economist #1 / #2 | 8.7% / 11.3% | 4.08 / 3.94 | 12.8 / 15.6 | economic 48 |
| rusher | 1.7% | 4.48 | 11.3 | conquest 3, economic 1 |

Endings: wonder 43%, influence 32%, economic 21%, relics 2%, conquest 2%,
turn limit 0%. Game length: median 93 (min 30, max 115).

Across A+B (480 games): wonder 43.5%, economic 21.5%, influence 17.5%,
conquest 8.8%, relics 8.8% — every condition ≥ 4%.

### Strategist against 5 copies of one bot (60 games each, seed 3)

| field | strategist win% | avg place | endings |
|---|---|---|---|
| 5 × economist | 100% | 1.00 | wonder 63%, influence 37% |
| 5 × turtle | 86.7% | 1.15 | economic 70%, influence 25% |
| 5 × rusher | 80.0% | 1.20 | relics 72%, conquest 20% |
| 5 × random | 100% | 1.00 | relics 87%, wonder 13% |

### Positional fairness: identical strategists

| players | games | win% by start slot | win% by seat | endings |
|---|---|---|---|---|
| 6 | 240 | 17, 11, 22, 19, 9, 23 | 16, 18, 21, 15, 15, 16 | relics 49%, influence 35%, wonder 12% |
| 5 | 100 | 17, 12, 18, 35, 18 | 29, 14, 14, 17, 26 | relics 50%, influence 36%, conquest 13% |
| 8 | 96 | 17, 5, 12, 7, 15, 22, 12, 12 | 10, 12, 15, 16, 14, 5, 17, 12 | wonder 44%, influence 35%, turn limit 15% |

At 6 players every start slot is within 1/6 ± 0.08 (the two parity-moved
starts, slots 2 and 5, are still slightly favoured: 22–23%). The 5- and
8-player checks are short runs (standard error ≈ 4 and 3.4 points); slot 3 at
5 players (35%) is outside the band and worth a longer look (see §5).

### Other player counts (mixed fields)

| field | games | strategist | endings | median length |
|---|---|---|---|---|
| 5p `strategist,economist,rusher,turtle,random` | 100 | 57.0% / place 1.77 | conquest 46%, wonder 19%, relics 19%, economic 12%, influence 4% | 69.5 |
| 8p `strategist,economist,rusher,turtle,economist,turtle,random,random` | 96 | 79.2% / place 1.56 | wonder 96% | 68 |

### Skill ladder (120 games, seed 5)

`strategist,strategist_lite,economist,turtle,rusher,random`: ratings
strategist 35.4 (53.3%, place 2.01) > economist 23.3 > **strategist_lite 19.9**
(5.8%, place 3.01) > turtle 18.4 > rusher 5.6 > random −15.0. Removing the
strategist's path choice, relics, raids and long treaties costs it most of
its strength; ratings increase with skill random < strategist_lite <
strategist.

### Re-check after the exploit fixes (60 games, field A)

Phantom-proof market clearing (one drop per round + re-admission, no
"did not converge"), contention only among orders that would succeed,
coalition captures (largest force among allies), cavalry crossing clashes,
no 2-step moves through hostile cities, symmetric simultaneous treaty breaks
and the protected-tile floor in the terrain-mix balancing (maps for n = 4,
5, 6, 8 unchanged). `--games 60 --players 6 --jobs 4`:
strategist 53.3% (place 1.80), economist 20.0%, turtle 15.0%, rusher 11.7%,
random 0%; endings wonder 45%, economic 20%, relics 18%, conquest 13%,
influence 3%; median length 78 — within noise of the 240-game table above.

## 5. Barter (§13): does trading keep skill on top?

Every turn the tournament runner gives each bot 3 negotiation rounds before
it acts (rotating seat order, fresh view each round, actions applied at
once). How the bots trade: docs/BOTS.md ("Barter"). Commands (seed 1
unless noted, 240 games each, `--jobs 4`):

```
python -m agentciv.tournament --bots strategist,economist,rusher,turtle,random,random --games 240 --seed 1 --jobs 4
python -m agentciv.tournament --bots strategist,economist,rusher,turtle,economist,turtle --games 240 --seed 1 --jobs 4
python -m agentciv.tournament --bots strategist,economist,rusher,turtle,random,random --games 240 --seed 1 --jobs 4 --rounds 0   # no barter
python -m agentciv.tournament --bots strategist_notrade,economist,rusher,turtle,random,random --games 240 --seed 1 --jobs 4       # ablation
python -m agentciv.tournament --bots strategist,strategist_notrade,economist,rusher,turtle,random --games 240 --seed 1 --jobs 4 # head-to-head
```

### Field A — `strategist,economist,rusher,turtle,random,random`

| | strategist win% / place | economist | turtle | rusher | endings |
|---|---|---|---|---|---|
| **barter** (seed 1) | **51.2% / 1.93** | 24.6% | 15.0% | 9.2% | wonder 44%, economic 25%, relics 17%, conquest 10%, influence 4% |
| barter (seed 2) | 48.3% / 1.92 | 23.7% | 13.3% | 14.6% | wonder 37%, economic 25%, relics 18%, conquest 16%, influence 3% |
| no barter (`--rounds 0`) | 52.9% / 1.88 | 22.1% | 13.3% | 11.7% | wonder 44%, economic 22%, relics 15%, conquest 15%, influence 4% |
| barter, strategist does not trade (seed 1) | 52.5% / 1.92 | 23.3% | 12.1% | 12.1% | wonder 42%, economic 25%, relics 16%, conquest 15%, influence 3% |
| barter, strategist does not trade (seed 2) | 49.2% / 2.02 | 20.0% | 16.3% | 14.6% | wonder 45%, economic 23%, relics 15%, conquest 12%, influence 5% |

With barter the random bots still place last (5.27–5.37). Median game length
78.5 turns (no barter 78). **Deals: 16.7 per game** — bargain 6.9, random
3.1, loan 3.0, sell 1.8, buy 1.5, peace 0.24, tribute 0.19. Per bot and game:
strategist 9.4 deals (mostly bargains from the random bots; 0.6 contracts as
payer, 0.18 defaults), economist 5.7 (3.0 loans out, never a default), turtle 4.0,
rusher 2.1, random 5.7–6.5 (it loses ~400 gold-equivalent per game in
trades at base prices, the strategist gains ~800).

**After the deal-exploit review** (land adjacency / no foreign units / 5
tiles per turn, default fines scaled with the debt and carried as
`influence_debt`, bots' credit limit and exposed-land valuation, a random
bot that can't be milked), 60 games, `--players 6`, default seed:
strategist 55.0% / place 1.90, economist 25.0%, turtle 13.3%, rusher 6.7%,
random 0% (5.35–5.37); endings wonder 42%, economic 25%, relics 15%,
conquest 12%, influence 7%; median 72.5 turns. **Deals: 6.7 per game**
(random 2.6, sell 1.9, buy 1.05, loan 0.8, peace 0.22, tribute 0.07,
bargain 0.07): the strategist's bargains with the random bots are gone
(random no longer takes lopsided deals) and loans shrank to first-time
credit limits (150 gold of instalments, growing with contracts honoured).

Calibration notes: a contract default costs `max(25, ceil(owed/5))`
influence, owed as debt if the payer spent its influence first. With the
bots' credit limit a first-time borrower gets at most ~125 gold from a bot,
so a deliberate default nets about 100 gold for 25–40 influence and a
public default that ends further credit. If loans become an exploit again,
lower the fine's divisor (engine) or `CREDIT_BASE` (bots/common.py).
Since the bank (section 7 below) the fine is `max(25, ceil(value/2))`, where
`value` is the remaining obligation in gold at fixed start prices
(`CONTRACT_DEFAULT_GOLD_PER_INFLUENCE` = 2): that same 100-gold default now
costs 50–63 influence.

### Field B — `strategist,economist,rusher,turtle,economist,turtle`

| | strategist win% / place | turtles | economists | rusher | endings |
|---|---|---|---|---|---|
| **barter** | **53.3% / 2.26** | 6.2% / 8.7% | 12.9% / 17.9% | 0.8% | influence 40%, economic 31%, wonder 25%, relics 3%, conquest 1% |
| no barter (`--rounds 0`) | 57.5% / 2.27 | 7.5% / 7.9% | 12.1% / 14.2% | 0.8% | wonder 36%, influence 33%, economic 26%, relics 2%, conquest 2% |
| barter, strategist does not trade | 53.3% / 2.28 | 5.8% / 7.9% | 12.5% / 19.6% | 0.8% | influence 35%, economic 32%, wonder 28%, relics 2%, conquest 1% |

**Deals: 11.5 per game** — loan 5.5, sell 3.2, buy 2.4, peace 0.26,
tribute 0.13. The turtles borrow (about 2 loans each per game) and buy stone/wood
for their wonder stages, the economists lend and sell, the rusher buys food
while it is not (yet) a big army and collects a little tribute; the
strategist makes only 1.9 deals per game here (no random bots to bargain
with, and it sells nothing but overflow at a premium).

### The strategist stays on top; what trading is worth to it

* **Barter costs the strategist 1–4 points** of win rate against the
  no-barter game (A: 52.9% → 51.2%, B: 57.5% → 53.3%; the BALANCE tables of
  §4, 52.9% / 54.2%, are within 2 points), mostly because the *others* now
  trade among themselves: the economists' loans and fair sales help the
  turtles and themselves (economic wins in B 26% → 31%). It keeps the best
  win rate and average place in every field by a wide margin.
* **Ablation, separate fields.** A strategist that never trades
  (`strategist_notrade`: same code and random tie-breaks, `TRADE = False`)
  in the same barter field does as well within noise: A 52.5% / 49.2% (seed
  1 / 2) vs 51.2% / 48.3% trading, B 53.3% vs 53.3% (average place 2.28 vs
  2.26). One 240-game run has a standard error of about 3.2 points, so an
  effect of a point or two is not measurable this way.
* **Ablation, head-to-head** (both in the same games:
  `strategist,strategist_notrade,economist,rusher,turtle,random`, 240
  games): the trading strategist wins **33.8%** (place **2.58**), the
  non-trading one **24.6%** (place 2.70); economist 25.4%, turtle 9.6%,
  rusher 6.7%. Trading helps the skilled bot when the two meet directly.
* Negotiation is fast: 0.8–1.8 ms per `negotiate` call (the strategist's
  is the slowest; single calls peak at 20–50 ms under `--jobs 4`), so a
  game takes ~4–6 s instead of ~2–3 s (views for 3 rounds dominate).

### What tuning the traders taught us

* **Selling "surplus" can lose games.** A strategist that offered its stock
  above keep levels to needy buyers won 42–47% of field B (vs 53% without
  trading): barter sold the whole surplus at once, while its market sales
  are throttled by price impact, so the stone and wood its wonder needed
  went to rivals (its wonder wins fell from 42 to 16). It now sells only
  what would overflow its storage caps, and only at ≥ 110% of what the
  market would pay.
* **Food is war material.** Economists and turtles selling food "fairly" to
  the rusher pushed its conquest wins from 25 to 39 (field A, seed 2). No
  bot now sells food or wood to a hostile army that can reach it, to a
  conqueror (2+ capitals) or to an army 1.5× the average size.
* **Don't feed the race you are in.** The strategist refuses the leader,
  never gives a contender what its race runs on (gold to an economic racer,
  stone/wood to a wonder builder), never pays interest on loans others push
  on it (the economist's 20% loans fed the economist's own victory), and
  signs no peace with a contender (it may have to block it).
* **Negotiation must not leak into play.** Calling the strategist's victory
  ETA model during negotiation updated its path hysteresis several times a
  turn and quietly changed its play (several points of win rate in field
B). The
  negotiation now restores that state; a test checks that negotiating
  without sending anything plays exactly like no negotiation.
* **Guards.** "Close to winning" uses real progress only (conquest counts
  from the first captured capital; at 2–3 players the own capital alone was
  0.5) and projects the deal (net gold incl. 10 turns of instalments; the
  next wonder stage becoming affordable): 0.7 for every bot, 0.55 for the
  strategist.

## 6. Open issues

* **Thresholds do not scale with the player count.** At 8 players the
  strategist wins 79% of mixed games, almost all by wonder (turn ~68); at 5
  players conquest ends 46% of games (a majority is only 3 capitals). The
  targets were tuned for 6 players.
* **5-player positional fairness** (one short run): slot 3 won 35% of
  5-strategist games. 5 starts on a diamond have only a mirror symmetry;
  relic distances per slot are (5,5,8) / (5,5,9) / (5,6,9).
* **Early relic wins** (resolved by removing the relic victory, §9).
* **Odd player counts ≥ 7** have no start symmetry: at 9 players relic
  distances differ by up to 4 steps between capitals (DESIGN §3 table).
* **Influence and wonder are knife-edge.** Small changes of the influence
  goal or wonder cost move 10–15% of field-B games between the two; the
  current values keep every condition ≤ 45% in field B with a few points of
  margin (wonder 43%).
* **Trading is a small edge.** The strategist's own trading is worth a few
  points head-to-head but is within noise in separate fields; its biggest
  gains come from bargains with the random bots (field A). Loans (its
  largest lever: long loans that are only partly repaid before its victory)
  are rare because lenders refuse loans that make a wonder stage 4+
  affordable. The economist also grants loan *requests* while it is itself
  close to the economic target (it only stops *offering* them).
* **Barter shifts field B toward the economic victory** (26% → 31%) and away
  from the wonder (36% → 25%): the turtles pay the economists interest, and
  the strategist races for influence more often.
* **The rusher's opportunism** (breaking a treaty with a weak partner) also
  plays without barter, so `--rounds 0` differs slightly from the §4 tables
  (field B: strategist 57.5% instead of 54.2%).

## 7. Economic and influence victories: bank, legacy and streaks

(Superseded in part by the g7–g10 retune, §9: allowance 50 + 10 per market
hall, no interest, a deposit needed for every streak turn, city loss resets the
streaks, L = 2700.)

The stock thresholds (13500 gold, 3350 influence) were out of reach for LLM
players (the best reached 13.5% of the gold target) and gave no warning. They
are replaced by held targets (rules §5, §11):

* **Economic:** `bank` orders move up to 10 gold per city + 10 per market hall
  each turn into a bank that cannot be spent; it pays floor(bank/100) interest.
  Win at bank ≥ 3600 for 10 consecutive turn ends while owning the original
  capital.
* **Influence:** legacy = total influence income; win at legacy ≥ 3000 for 10
  consecutive turn ends with the original capital.
* Targets scale with min(1, max(0.5, max_turns/150)). Capturing an original
  capital takes half the bank and a quarter of the legacy; a contract default
  takes the remaining obligation's gold value from the payer's bank and ends
  its economic streak. Market hall gold 3 → 5. Bank and legacy are public in
  fog games too.

Tuning sweep (prototype engine, 240 games per row over six fields A6, B6, S6,
M6, A5, S5; 150 max turns; cell = share of games (median win turn);
B = bank target, L = legacy target, "+div" = +1 influence per active treaty):

| Config | conquest | wonder | relics | influence | economic | score | median game |
|---|---|---|---|---|---|---|---|
| **Baseline, old rules (480 games, seeds 1–6)** | 12% (t71) | 20% (t78.5) | 25% (t47) | 28% (t97) | 14% (t90) | 0 | t84 |
| B3000, L2800, +div | 11% (t65) | 9% (t68) | 25% (t42) | 28% (t88) | 27% (t74) | 0 | t73 |
| Allowance 5 per city + 15 per hall | 12% | 16% | 24% | 46% (t87) | 1% (t122) | 0 | t82 |
| B3300, L3000, +div | 15% | 12% | 26% | 24% (t90) | 23% (t79) | 0 | t78 |
| + a 1.2× legacy lead clause | 16% | 15% | 26% | 10% | 25% | 8% (t150) | t78 |
| B3300, L2800, no div | 14% | 14% | 25% | 26% (t89.5) | 21% (t80) | 0 | t79 |
| same + hall gold 5, seeds 4–6 | 11% | 10% | 26% | 28% (t90) | 26% (t82.5) | 0 | t81.5 |
| B3600, L2800, no div | 15% | 13% | 26% | 31% (t89.5) | 15% (t84) | 0 | t83 |
| same, no interest | 12% | 13% | 26% | 32% (t89) | 16% (t85) | 0 | t83.5 |
| same, hall gold 5 | 12% | 13% | 26% | 32% (t90) | 16% (t84) | 0 | t83 |
| B3600, L3000, no div | 14% | 15% | 27% | 26% (t94) | 18% (t85) | 0 | t83 |
| **Chosen: B3600, L3000, interest, hall gold 5 (480 games, seeds 1–6)** | 12% (t69.5) | 14% (t81) | 26% (t47) | 28% (t94) | 20% (t85) | 0 | t84 |

The shipped implementation (same fields and seeds, 480 games, which adds
the contract-default bank seizure and the bot collateral/recovery valuation)
reproduces the chosen row, with no bot exceptions:

| | conquest | wonder | relics | influence | economic | score | median game |
|---|---|---|---|---|---|---|---|
| implementation, seeds 1–6 | 12% (t69.5) | 15% (t80.5) | 26% (t47) | 29% (t93) | 18% (t85) | 0 | t84 |
| … seeds 1–3 / seeds 4–6 | 12 / 12 | 15 / 14 | 27 / 26 | 28 / 30 | 18 / 18 | 0 / 0 | t83 / t85 |

`python -m agentciv.tournament` with the default field A (60 games) and the
same field with `--fog` (30 games), 150 max turns:

| | conquest | wonder | relics | influence | economic | score | median game |
|---|---|---|---|---|---|---|---|
| field A, standard (60) | 17% (t76) | 32% (t70) | 17% (t35.5) | 7% (t98.5) | 28% (t84) | 0 | t83 |
| field A, fog (30) | 13% (t67.5) | 23% (t73) | 27% (t45) | 7% (t98.5) | 30% (t84) | 0 | t73.5 |

Field A alone has few influence wins (the turtle is the only influence
racer besides the strategist; the prototype's A6 runs gave 2–5%); the
six-field mix is the balance reference.

* Interest and hall gold 5 barely move bot results (the economist is limited
  by the allowance, not by gold, and bots build one market hall); both help
  gold-starved LLM players.
* Streaks are a visible race, but bots seldom attack a streak holder's
  capital (18 of 600 streaks broken in the prototype runs).
* Wonder's share fell from 20% to 14–15%; watch it in LLM games (fixes: a
  larger bank target or cheaper wonder stages).
* The strategist rarely picks the economic path; strategist_lite does bank.
* A default is the one way gold leaves a bank: the seizure is paid to the
  payee as gold on hand, so a payee that gifts it back returns the bank to
  the payer as spendable gold (or keeps it out of a captor's plunder). The
  fine prices this at 1 influence per 2 gold moved (a 3000-gold bank costs
  1500 influence, mostly as `influence_debt`), and the seizure is valued at
  fixed start prices so neither side can inflate or shrink it with
  same-turn market orders. If it is still used, burn the seizure instead of
  paying it out.


## 8. Treaties: slots, bonds and priced breaks

(Since the g7–g10 retune, §9, the bank share and bond of a break are removed
from the game instead of paid to the victim, and a break against a partner on
a victory streak is free.)

Bots signed ~23 treaties per 6-player game and a leader was at peace with
about half the field; breaking cost a flat 50 influence, so the rational
breaker broke often. The treaty rules (rules §9) make treaties scarce and a
break visibly expensive:

* **Slots:** max(1, ceil(L/2)) treaties per player, L = other living players
  (`TREATY_SLOT_DIVISOR = 2`); renewals need no slot, treaties over the limit
  after an elimination are kept until they end.
* **Lengths** 20–40 turns (was 10–50; the lengths alone changed almost
  nothing in the sims).
* **Bonds:** each side pledges banked gold (an optional bond + 50 × its
  betrayals); pledges are recorded, not moved.
* **Break:** 50 × (1+b) influence, p% of legacy and p% of the bank to the
  victim (p = min(40, 10·(1+b))), plus the breaker's bond and a pro-rated
  refund of what the victim paid in the deals behind the treaty; a 15-turn
  re-sign cooldown and one extra turn of movement restriction.
* Rejected in the design runs: a signing fee (it removed every economist
  win), a bank bond for every signer (treaties fell from 18 to 1.5 per game)
  and a renewal window (rules without effect).

48 games per row, 150 max turns, seed 1, field F1 =
`strategist,strategist,rusher,turtle,economist,strategist_lite`
(`python -m agentciv.tournament --bots ... --games 48 --jobs 6 [--fog]`;
design numbers from the prototype runs, "shipped" = this implementation):

| Run | Signed/game | Live/player | Peak/player | Breaks/game | Battles/game | Cities captured/game | Median turns | Victories | Economist wins |
|---|---|---|---|---|---|---|---|---|---|
| F1 old rules | 23.5 | 2.56 | 3.98 | 0.17 | 13.7 | 5.3 | 88 | influence 18, economic 14, wonder 11, relics 4, conquest 1 | 11 |
| F1 design | 18.2 | 1.88 | 2.84 | 0.10 | 13.0 | 5.3 | 89 | influence 23, economic 14, wonder 7, conquest 2, relics 2 | 9 |
| **F1 shipped** | 18.25 | 1.88 | 2.84 | 0.10 | 12.96 | 5.27 | 89 | influence 23, economic 14, wonder 7, conquest 2, relics 2 | 9 |
| F1 fog old rules | 23.4 | 2.50 | 3.43 | 0.48 | 13.6 | 5.2 | 91 | influence 23, economic 13, relics 10, conquest 1, wonder 1 | 9 |
| F1 fog design | 20.6 | 2.08 | 2.90 | 0.35 | 13.8 | 5.0 | 92 | influence 22, economic 16, relics 7, wonder 2, conquest 1 | 9 |
| **F1 fog shipped** | 20.6 | 2.08 | 2.90 | 0.35 | 13.52 | 4.92 | 91.5 | influence 22, economic 16, relics 7, wonder 2, conquest 1 | 9 |

In the design runs with a treaty-exploiting bot (F2) the rational breaker
broke 56–70% less, cities captured doubled (attacks land on players without a
treaty) and the score leader was at peace with at most ~46% of its opponents.
The turtle loses its blanket protection (F1 wins 5 → 1).

* **Watch:** the aggressive field (F3: two rushers, a betrayer) went from 3
  to 16 conquest wins. If LLM games turn too bloody, the documented fallback
  is `TREATY_SLOT_DIVISOR = 1.5` (slots ceil(L/1.5); 10 conquest wins in F3).
* No bot broke a treaty while holding a bank, so the bank share and bond
  forfeits are covered by the tests, not by the sims; deterrence of rich
  breakers has to come from LLM games.
* The cooldown also stops a victim from buying peace from its breaker for 15
  turns. If LLM games show victims trapped, exempt peace proposed by the
  victim.

## 9. The g7–g10 retune: relics, bank allowance, streak resets

Agent games g7–g10 showed two problems. Relic wins came early (T45–T68) and
were hard to contest. Economic wins came to whoever passed the bank target
first and then sat on it, with nothing a rival could do. The retune changes:

* **Relics are no longer a victory condition.** An owned relic gives 15 score
  and 3 influence per turn while guarded, 1 unguarded.
* **Bank:** each turn you may bank at most 50 + 10 per city with a market hall
  (0 without a city). Banked gold earns no interest. B stays 3600.
* **Streaks:** an economic streak turn counts only if at least half the
  allowance (`streak_deposit`) was banked that turn. Otherwise the turn is a
  `streak_paused`. Losing any city sets both streaks to 0 (`city_lost`).
  L = 2700 (was 2400).
* **Treaty breaks:** the bank share and bond are removed from the game
  instead of paid to the victim. A break against a partner whose streak is at
  least 1 is free (no influence, legacy, bank share, bond or betrayal) and
  has no notice turn.
* **Capture ties:** equal top power goes to the first player in the turn's
  rotating order (below). Passage rights for treaty partners are deferred.

### Judge runs (5 fields × 48 games per row)

Fields: A std and fog (`strategist,economist,rusher,turtle,random,random`),
F1 std and fog (`strategist,strategist,rusher,turtle,economist,strategist_lite`),
S fog (`strategist,strategist,strategist,strategist_lite,strategist_lite,strategist_notrade`).
Win columns: economic / influence / wonder / conquest / relics, as win % and
median turn.

| Run (seeds 11–15) | Median end | Wins | Battles on / next to a relic | Streaks broken by a city capture (econ / infl) |
|---|---|---|---|---|
| Baseline 29fdb0a | t85.5 | 25% t85 / 31% t93 / 20% t78 / 9% t73.5 / **15% t50** | 10% / 15% | 0 / 0 |
| bank 4000, L 2400 | t89 | 4% / 68% / 15% / 14% / 0% | 4% / 12% | 2 / 54 |
| bank 3600, L 2400 | t90 | 17% / 53% / 15% / 15% / 0% | 4% / 12% | 9 / 49 |
| **bank 3600, L 2700** | t92.5 | 35% t93 / 28% t96 / 19% t83 / 18% t83 / 0% | 4% / 12% | 21 / 47 |

Confirmation on seeds 21–25: baseline 23 / 28 / 15 / 12 / **21% (t45)**; final
38% t93 / 31% t96 / 15% t80 / 16% t77 / 0%. Battles on / next to a relic went
from 9% / 16% to 3% / 8%. Captures of streak holders went from 35 to 48.

### Verification of the implementation (seeds 31–35, 240 games per column)

| | Base 29fdb0a | Retune |
|---|---|---|
| Median end | t86 | t93 |
| Economic | 20.8% (t86) | 38.3% (t94) |
| Influence | 33.8% (t93) | 27.1% (t95) |
| Wonder | 18.8% (t75) | 19.6% (t77) |
| Conquest | 7.1% (t75) | 15.0% (t76) |
| Relics | 19.6% (t48) | — |
| Battles on / next to a relic | 9.5% / 15.4% | 4.1% / 9.6% |
| Relic owner changes per game | 5.9 | 4.0 |
| Streaks started (econ / infl) | 105 / 243 | 231 / 284 |
| Streaks broken by a city capture (econ / infl) | 0 / 0 | 22 / 29 |
| Economic turns paused (deposit short) | — | 68 |
| Captures of a streak holder's city | 24 | 50 |
| Treaty breaks (free) | 40 (0) | 52 (6) |
| Bot errors / games to t150 | 0 / 0 | 0 / 0 |

Per field (economic / influence / wonder / conquest / relics, %):

| Field | Base | Retune |
|---|---|---|
| A std | 27 / 6 / 38 / 19 / 10 | 19 / 13 / 38 / 31 / — |
| A fog | 23 / 4 / 33 / 15 / 25 | 29 / 17 / 25 / 29 / — |
| F1 std | 21 / 50 / 21 / 2 / 6 | 65 / 13 / 19 / 4 / — |
| F1 fog | 25 / 48 / 2 / 0 / 25 | 48 / 29 / 17 / 6 / — |
| S fog | 8 / 60 / 0 / 0 / 31 | 31 / 65 / 0 / 4 / — |

The bots are only a crash and pacing check here. They build temples
steadily and never stack relics the way the LLMs did, so the LLM games decide
the final numbers.

**Garrison variant (not shipped).** A variant had the economist and the
strategist recruit up to 2 archers in unthreatened non-capital cities while on
or near a streak. On the same seeds, 30 of 240 games played out differently
and 14 had a different outcome, in both directions. Economic wins went from
38.3% to 36.2% and influence from 27.1% to 29.2%. Streaks broken by a city
capture stayed at 51, captures of streak holders at 50 and economist wins at
55. That is noise, and `STREAK_CITY_GARRISON` in `lock_garrison` already
keeps 2 units in those cities.

### Review fixes

* **Capture ties.** The judge's "no capture on a tie" rule was dropped.
  Capture candidates are chosen after the battles, so they are always at
  peace with each other. The rule therefore never settled a contest between
  rivals. It only let two treaty partners with equal stacks sit on a streak
  holder's city without taking it, and that city then never reset the
  holder's streaks. It also let a third player veto a capture by moving in a
  matching stack. A tie now goes to the first in the turn's rotating order
  (RULES §8). A re-run of F1 std (seed 33, 48 games) gave the same result in
  every game: bots never produce a tie.
* The spectator feed no longer reports a fog-redacted break's bank share as
  gold paid to the partner. The full summary lists `streak_paused`. Clients
  print a note when a streak holder proposes, counters or accepts peace that
  it pays for (DESIGN §7, free break).

### Watch in LLM games

* **Uncontested banking.** In F1 std, economic wins rose from 21% to 65%, and
  the economist's seat won 18 of 48 games (base 6). The wins fall in a narrow
  window (median t93–94), because a player who banks the full allowance every
  turn from about T25 and loses no city finishes on a fixed schedule. The
  bot fields have at most one aggressor and no bot raids streak holders on
  purpose, so they do not test the counterplay. Nothing was retuned on bot
  numbers alone. If LLM bankers win unopposed, the fallbacks are a smaller
  allowance, a higher B, or a strategist that raids streak holders, which
  would also make the bot fields test the counterplay.
* **Temple influence** decides about two thirds of all-strategist games
  (S fog 65%, base 60%). This predates the retune, but relic wins no longer
  dilute it. If a temple rush wins before about T70, raise L to 3000.
* **No economic win in 2–3 LLM games:** make a city loss cost 3 streak
  turns, or count only cities held for 10 turns or more. The siege pause is
  the next fallback after that.
* **Contract defaults** are now the only way bank gold reaches another player
  (DESIGN §8). A losing player could default on purpose to give a leader its
  bank.
