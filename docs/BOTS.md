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
| `economist` | peaceful builder, sells surplus, hoards gold | economic | medium |
| `rusher` | early infantry/cavalry rush on the nearest weak capital | conquest | medium (feast or famine) |
| `turtle` | walls, archers, temples, wonder, treaties with everyone | wonder | medium |
| `strategist` | adaptive: economy, relic control, threat response, blocking | whatever is fastest | strongest |

Every bot is deterministic for a given `seed` and sequence of views. None of
them does I/O, none raises (see `SafeBot` below), and each takes a few
milliseconds per turn (the strategist averages about 5 ms and peaks near
15 ms).

## Measured strength

The results below use the current constants and
`python -m agentciv.tournament --jobs 4`. Seats are shuffled and rotated, and
every game uses its own map seed.

**Mixed 6-player field** (`strategist,economist,rusher,turtle,random,random`):

| bot | 40 games (seed 1): win% / avg place | 200 games (seed 7): win% / avg place |
|-----|------|------|
| strategist | 57.5% / 1.77 | 76.5% / 1.38 |
| economist | 17.5% / 2.50 | 13.0% / 2.44 |
| turtle | 5.0% / 2.50 | 6.0% / 2.64 |
| rusher | 20.0% / 3.30 | 4.5% / 3.65 |
| random ×2 | 0% / 5.45–5.47 | 0% / 5.42–5.47 |

The strategist also leads at 3 players (71% of 24 games) and at 8 players
(75% of 24 games).

**Strategist against 5 copies of one bot** (48 games each, seed 3):

| field | strategist wins | avg place |
|-------|-----------------|-----------|
| 5 × economist | 100% | 1.00 |
| 5 × turtle | 83% | 1.27 |
| 5 × rusher | 79% | 1.23 |
| 5 × random | 100% | 1.00 |

Reproduce with:

```
python -m agentciv.tournament --games 40 --players 6 --seed 1 --jobs 4
python -m agentciv.tournament --bots strategist,rusher,rusher,rusher,rusher,rusher --games 48 --seed 3 --jobs 4
```

## The bots

### random (`random_bot.py`)
Each turn it takes 1–5 random actions: claim an adjacent tile, build a
fitting improvement or city building, recruit random units, trade a random
amount on the market, settle, or accept or propose treaties at random. It also
moves random parts of its stacks one step in random directions. It never
plans, so it wastes resources and scatters its army.

### economist (`economist.py`)
* **Diplomacy:** accepts every treaty and proposes 50-turn treaties to
  everyone.
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
* **Defence:** minimal. It keeps 2 units in the capital. When an army comes
  within 2 turns, it recruits just enough of the best counter for the city to
  hold.

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
  until it holds enough capitals for conquest.
* **Economy:** thin. It builds farms and lumber mills, claims tiles, sells
  spare stone, and disbands units rather than let them starve.
* **Treaties:** accepts them only from players it is not targeting, and never
  proposes one.

### turtle (`turtle.py`)
* **Diplomacy:** proposes the longest possible treaty to everyone and accepts
  everything. Treaty partners cannot even enter its land.
* **Defence:** raises capital walls over the game (level 1, then 2 from turn
  30, then 3 from turn 70).
  * It keeps a standing archer garrison. Archers are ×1.5 in their own city
    and ×1.5 against infantry.
  * It adds more archers when an army comes within 3 turns.
* **Temples:** builds them on many tiles (influence).
* **Wonder:** from turn 18 it reserves the cost of the next wonder stage in
  the capital. It buys missing stone and wood on the market.
* **Win conditions:** mainly the wonder, and influence as a by-product.

### strategist (`strategist.py`)
It uses the economist's economy, then adds these behaviours on top:

1. **Victory ETA model.** Every turn it estimates, for itself and every
   rival, how many turns each condition is away.
   * **Economic and influence:** based on how fast gold and influence have
     grown over the last 6 turns. For itself it uses potential income: all
     surplus sold at the current pool prices.
   * **Wonder:** the remaining stage costs valued at market prices, divided by
     production value, and the pace of recent stages.
   * **Relics:** based on the streak. For itself it adds the walking and
     claiming distance to the missing relics.
   * **Conquest:** based on the number of capitals held.

   Its own path is the lowest ETA, with hysteresis so it doesn't flip-flop.
   Once that ETA is within 30 turns it commits:
   * **Wonder:** builds stages in the wonder city every turn, buys materials,
     and accepts a lower selling price to raise gold.
   * **Economic:** keeps its gold and raises the bar for investments.
   * **Influence:** favours temples and saves influence.

   When it is close to winning, it shortens its investment horizon and stops
   founding new cities.
2. **Blocking.** If a rival would win before it, it picks a military
   objective against that rival's path:
   * **Relics:** takes one of the rival's relic tiles. Capturing it resets the
     streak.
   * **Wonder:** assaults the wonder city. A capture destroys the wonder.
   * **Economic:** takes the rival's capital, which plunders half of the
     rival's gold.
   * **Conquest:** retakes whichever of the rival's capitals is cheapest to
     take.

   It computes the smallest strike force that wins the simulated assault with
   a 1.35 power ratio: siege against walls plus the best counter to the
   defenders. It reserves those resources, recruits the force and marches.
   It only blocks if the force can be built and delivered before the rival's
   ETA; otherwise racing is the better use of its resources. If the target
   is a treaty partner, it:
   * stages at the border,
   * keeps 50 influence in reserve,
   * breaks the treaty once the gathered force can win.
3. **Relic control.** It prices relic tiles highly (+3 influence and +10
   score each, and the fastest victory under the current constants).
   * It settles next to relics and claims them.
   * It guards its own relics when hostile units come within 2 turns.
   * It parks idle units on its relic tiles.
4. **Threat assessment.** For each city it collects every hostile unit that
   could arrive within 3 turns (cavalry move 2 per turn) and simulates the
   assault. It recruits the best counter until the city holds with a 1.25
   margin. It keeps in the city only as many units as that requires; the rest
   stay free for operations.
5. **Opportunism and predation.** It attacks rival cities and relics next to
   its army that it can take with a 1.4 power ratio. Every few turns it looks
   for a rival capital whose strike force costs less than the capture is
   worth (plunder, 25 score, conquest progress).
6. **Diplomacy.**
   * It proposes 20-turn treaties to distant players and to players with a
     much stronger army.
   * It accepts proposals only from players that are neither close to winning,
     its current target, nor running a wonder or relic streak. In addition,
     the proposer must be far away or much stronger militarily, or it must
     still be very early in the game.
7. **Market.** It sells surplus before it overflows the caps. Voluntary sales
   are split so that the batch price stays within about 8% of the spot price;
   the rest waits a turn.

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
  * `diplomacy`, `food_safety`, `plan_site`, `defend`, `sell`, `expand`,
    `develop`, `garrison_moves`;
  * `offense(target)`: gather and assault;
  * `recruit_army`.

  Class-level knobs tune it, for example `INFLUENCE_WEIGHT`,
  `DEFENSE_MARGIN`, `MIN_GARRISON` and `SELL_FLOOR`. The economist, rusher,
  turtle and strategist are all `PlannerBot` subclasses.

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
length and time per game. `--json` writes the full summary, including
per-game results (seats, placements, scores, errors, timings).

For programmatic use: `run_game(bot_specs, seed, max_turns)` returns one
game's result dict. `run_tournament(...)` returns the summary, and
`format_summary(...)` renders it.
