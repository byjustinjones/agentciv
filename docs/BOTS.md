# AgentCiv built-in bots

Built-in bots serve as house players on the server, sparring partners for
new agents, and a way to measure skill. They are ordinary Python classes that
implement the bot interface (`agentciv/bots/base.py`). Each one takes the
player view (docs/DESIGN.md §10) and returns a list of orders (§9).

```python
from agentciv.bots import get_bot, BOT_NAMES
bot = get_bot("strategist", seed=42)
orders = bot.act(game.player_view("p1"))
```

| name | style | usual win condition | strength |
|------|-------|---------------------|----------|
| `idle` | submits nothing | – | baseline |
| `random` | random, mostly legal orders | – | weakest |
| `economist` | peaceful builder, sells surplus, hoards gold, weak defence | economic | medium |
| `rusher` | infantry/cavalry rush on the nearest weak capital | conquest | medium (feast or famine) |
| `turtle` | walls, archers, treaties; wonder or temples | wonder / influence | medium |
| `strategist` | adaptive: picks the fastest race, raids, relic control, threat response | whatever is fastest | strongest |
| `strategist_lite` | handicapped strategist (for the skill ladder) | economic | between |

Every bot is deterministic for a given `seed` and sequence of views. None of
them does I/O, none raises (see `SafeBot` below), and each takes a few
milliseconds per turn (the strategist averages about 7 ms per turn at 6
players).

## Measured strength

The results below use the current constants and
`python -m agentciv.tournament --jobs 4`. Seats are shuffled and rotated, and
every game uses its own map seed. Full tables, the rule changes behind them
and more fields: docs/BALANCE.md.

| field (6 players, 240 games, seed 1) | strategist win% / avg place | next best |
|------|------|------|
| A: `strategist,economist,rusher,turtle,random,random` | 52.9% / 1.87 | economist 21.7% / 2.64 (turtle place 2.50) |
| B: `strategist,economist,rusher,turtle,economist,turtle` | 54.2% / 2.38 | turtle 15.0% / 3.10 |

The random bots place last (5.36–5.37 in field A).

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
python -m agentciv.tournament --bots strategist,rusher,rusher,rusher,rusher,rusher --games 60 --seed 3 --jobs 4
```

## The bots

### random (`random_bot.py`)
Each turn it takes 1–5 random actions: claim an adjacent tile, build a
fitting improvement or city building, recruit random units, trade a random
amount on the market, settle, or accept or propose treaties at random. It also
moves random parts of its stacks one step in random directions. It never
plans, so it wastes resources and scatters its army.

### economist (`economist.py`)
* **Diplomacy:** accepts every treaty and proposes 25-turn treaties to
  everyone — except to a player on (or one relic short of) a relic streak.
* **Expansion:** picks city sites by the value of the tiles they would claim.
  It claims a path of tiles toward a site, settles it, and reserves the
  settlers' resources ahead of time so the market step doesn't sell them.
* **Improvements:** builds them in order of return on investment. Resources
  are valued at the current market prices.
* **Buildings:** a market hall early, and a warehouse when stock nears the
  caps.
* **Market:** sells everything above small reserves every turn, and dumps at
  almost any price what would otherwise overflow the storage cap.
* **Economic push:** from turn 55, or once it holds 700 gold, it keeps its
  gold and only makes investments that pay back quickly.
* **Defence:** its weak spot. It keeps 2 units in the capital, never raises
  walls, and when an army comes within 2 turns it recruits the best counter
  from its stock, spending at most 15% of its gold per turn on emergency
  food/wood purchases. A rich economist is a juicy target: capturing its
  capital plunders half its gold.
* **Relic streaks:** like every planner bot it attacks the weakest guarded
  relic of a hostile player whose relic streak runs (see `counter_relics`).

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

### strategist (`strategist.py`)
It uses the economist's economy, then adds these behaviours on top:

1. **Victory ETA model.** Every turn it estimates, for itself and every
   rival, how many turns each condition is away.
   * **Economic and influence:** from how fast gold and influence grew over
     the last 6 turns. For itself it uses potential income: all surplus sold
     at the current pool prices.
   * **Wonder:** the remaining stage costs valued at market prices divided
     by production value, and (for rivals) the value of the stages built
     per turn since their first stage.
   * **Relics:** the streak of guarded relics; for itself the walking
     distance to the missing relics plus a stall penalty when a campaign
     makes no progress.
   * **Conquest:** capitals held.

   Its own race is the lowest ETA among economic, wonder and influence, plus
   relics when that ETA ×1.5 is still the lowest (relic streaks are
   contested), with hysteresis. Once the ETA is within 30 turns (45 for
   influence, whose temples pay late) it commits:
   * **Wonder:** reserves the next stage (stone/wood up to the storage cap),
     buys the rest on the market in the build turn.
   * **Economic:** keeps its gold and raises the bar for investments.
   * **Influence:** temples everywhere (buying their stone), influence kept.
   * **Relics:** a campaign (see 3).
2. **Raids.** Every other turn it looks for a rival original capital or
   wonder city whose capture is worth at least twice the cost of the strike
   force (value: plunder = half the owner's resources, conquest progress,
   wonder denial, and a large bonus when the owner would otherwise win
   first). The force (siege against walls plus the cheapest of cavalry,
   infantry, archers or a mix) must beat the defenders, their neighbours and
   one turn of emergency recruiting (the owner's stock plus 20% of its gold).
   It gathers out of sight (3 tiles, 5 for an all-cavalry force), breaks a
   treaty first if needed, then strikes; a raid that stalls is abandoned and
   that rival left alone for 15 turns. Raids are expensive, so they stay rare.
3. **Relics.** Relics are taken by occupation. In a campaign it keeps a guard
   on every held relic (at least 4 units, enough to hold against armies 5
   turns away), marches detachments onto the cheapest missing relics, fights
   for guarded ones and recruits what the guards need. Outside a campaign it
   parks one unit on free relics next to its army.
4. **Threat assessment.** Each hostile army is assigned to the city it is
   closest to (an army attacks one city at a time); units sitting in their
   own city count 40%. The threatened city raises walls first, then recruits
   the best counter (buying food/wood if needed) until the simulated assault
   fails with a 1.1 margin.
5. **Diplomacy.** It proposes and accepts 50-turn treaties with militarily
   stronger players (and far-away ones that are not hoarding gold), never
   with a player close to winning, running a wonder or a relic streak, or
   with its raid target.
6. **Market.** It sells surplus before it overflows the caps. Voluntary sales
   are split so that the batch price stays within about 8% of the spot price;
   the rest waits a turn.

`strategist_lite` is a handicapped strategist (fixed economic race, no raids,
no relic campaigns, short treaties) used for the skill ladder in
docs/BALANCE.md.

## Building blocks (`common.py`, `planner.py`)

Both modules are useful if you write your own in-process bot.

* `World(view)` is an index-based snapshot of a view, with tile index
  `y * width + x`:
  * flat `terrain` and `owner` arrays, plus `cities`, `armies` and `players`
    looked up by tile or id;
  * relations: `at_peace`, `hostile`, `can_enter_fn(pid)`;
  * geometry: `cheb`, `manhattan`, `radius`, `nb` (4-neighbours);
  * `bfs(sources, can_enter, max_dist)` gives BFS distances, and
    `step_towards(src, dist)` gives the next step.
* `simulate_attack(world, attacker, units, tile, ...)` runs the engine's own
  `combat.resolve` on copies of the armies. It includes garrison, walls,
  siege, terrain, archer city bonus and counters, and returns
  `(win, survivors, power_ratio)`.
  * `assume_war=True` treats treaty partners as enemies, to evaluate an
    attack after breaking a treaty.
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
    `buy`, `propose`, `accept_treaty`, `message`, …
  * Each method emits an order only if it passes the same checks as the
    engine's pre-validation and can be paid for. It also tracks what was
    already planned this turn: claimed tiles, moved units and the tile count
    for claim costs.
* `SafeBot` is a `Bot` base class. Its `act()` never raises (on an error it
  returns the orders planned so far) and it provides a seeded `self.rng` and
  `self.memory` across turns.
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
  `DEFENSE_BUY_FRACTION`, `BUY_FOR_IMPROVEMENTS`, `COUNTER_RELICS`. The
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
For full control, subclass `SafeBot` and implement `decide(view) -> list`.
Or subclass the plain `Bot` and implement `act(view)`, but then you must never
raise.

**Remote** (any language): implement the same view → orders function
yourself and play over HTTP (docs/DESIGN.md §12) or with the Python client
SDK (`agentciv/client.py`). A few tips from building these bots:

* **Always submit**, even an empty list.
* **Relics are occupied, not claimed**: stand on them, and keep units there
  (only guarded relics count for the relic victory).
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
  gather at the border first, then `break_treaty` (50 influence). You can
  fight that turn but only walk in on the next.

## Tournament runner

```
python -m agentciv.tournament --bots strategist,economist,rusher,turtle,random,random \
    --games 40 --players 6 --seed 1 [--max-turns 150] [--jobs 4] [--json out.json]
```

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
* wins by condition;
* pre-validation errors per game and think time per turn.

It also shows overall stats: the distribution of ending conditions, game
length (median and average) and time per game, and the win rate by **start
slot** (the index of the start position in `mapgen.start_layout(n)`, i.e. the
map position independent of the seed's orientation) and by seat (`p1`…).
Six identical bots measure positional fairness by start slot. `--json` writes
the full summary, including per-game results (seats with their start slot,
placements, scores, errors, timings).

For programmatic use: `run_game(bot_specs, seed, max_turns)` returns one
game's result dict. `run_tournament(...)` returns the summary, and
`format_summary(...)` renders it.
