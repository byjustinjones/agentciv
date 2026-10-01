# Diagnostic positions (puzzles)

A full game's placement is a noisy signal of how well an agent reasons: it
depends on the opponents, the map and a long chain of decisions. A **puzzle**
is a short, saved position with one objective and an objective score:

* a hand-made position built on a wiped map (`agentciv/puzzles/`),
* fixed opponents (scripted bots or house bots with a fixed seed that never
  negotiate),
* a horizon of a few turns (3 to 24),
* a score from 0 to 100 with a one-line explanation, computed when the game
  ends.

Nothing in the rules changes. The solver plays the position as an ordinary
player with the normal state view, orders and diplomacy. The same sequence of
actions always gets the same score: no part of a puzzle reads the clock or
draws a secret random seed.

| id | title | turns | objective (short) | solution | do nothing |
|---|---|---|---|---|---|
| `winter` | Winter planning | 24 (22–45) | keep 18 infantry fed through two winters and build walls 2 + a market hall | 100 | 3 |
| `market` | Market-funded construction | 8 (6–13) | build a market hall and walls 2 from stone bought with the right surplus | 100 | 0 |
| `contracts` | Contract valuation | 3 (2–4) | accept the best subset of six open offers | 100 | 0 |
| `stop-victory` | Stop an imminent victory | 5 (40–44) | break a rival's economic streak before it reaches 10 | 100 | 0 |

## Playing a puzzle

### On the server

```bash
curl -X POST localhost:8000/api/games -d '{"puzzle": "winter", "turn_timeout": 600}'
# -> {"game_id": "g7", "status": "lobby", "rated": false, "unrated_reason": "puzzle", ...}
curl -X POST localhost:8000/api/games/g7/join -d '{"name": "me"}'
# -> token; the game starts at once (its one remote seat is taken)
```

With the Python client: `client.create_game(puzzle="winter")`, then `join` or
`run_bot(..., game_id=...)` as for any game. The clients need no puzzle
support: a puzzle is joined and played like any other game.

* **Options.** A `{"puzzle": id}` body may also carry `name`, `turn_timeout`
  (default **300 s** for puzzle games; `0` waits for the solver forever),
  `turn_delay`, `rated: false` and `fog: false`. Every other option (`bots`,
  `max_players`, `seed`, `max_turns`, `fill_with_bots`, `lobby_timeout`,
  `rated: true`, `fog: true`, ...) would change the position or the seats, so
  it is **refused with 400** rather than silently ignored. An unknown id is a
  400 that lists the puzzles.
* **Seats.** The opponents' seats are created with the game; the solver's
  seat is always the **last** one (`p1` in the one-player puzzles, `p2` in
  `stop-victory`, `p5` in `contracts`). The game starts as soon as the solver
  joins. Opponent seats show `"bot": "puzzle:<id>:<role>"` in the summary.
* **Unrated.** Puzzle games are always unrated (`unrated_reason: "puzzle"`,
  also on a server started with open ratings) and never touch a leaderboard.
* **The `puzzle` block.** Every state view (player and spectator) and the
  game summary carry

  ```json
  "puzzle": {"puzzle": "winter", "title": "Winter planning",
             "objective": "...", "horizon": 24, "start_turn": 22, "last_turn": 45,
             "scoring": "the formula in words", "solver": "p1",
             "score": null, "explanation": null}
  ```

  `score` and `explanation` are filled in when the game ends; the finished
  summary (`GET /api/games/{id}`) and the replay's `summary` and last frame
  carry them. The replay is the same bytes whether served from memory or from
  the saved file.
* **Turns.** A puzzle starts at its own turn (`winter` at turn 22, so that
  the seasons are the real ones of rules §5) and ends after `last_turn`
  (`max_turns = last_turn + 1`). Replay frames therefore start at that turn:
  `frames[k].turn == start_turn + k`; the replay's `?from=&to=` range is a
  frame range as before, and the action log is cut to the matching turns.
* **Checkpoints.** A puzzle game is checkpointed and restored like any game
  (its position, opponents and scoring inputs are part of the pickled game).

### Offline

```bash
python -m agentciv.puzzles list                       # ids, horizons, objectives, formulas (--json)
python -m agentciv.puzzles run winter --solution      # the shipped reference solution
python -m agentciv.puzzles run winter --baseline      # do nothing
python -m agentciv.puzzles run contracts --bot strategist [--seed 0] [--json]
```

`agentciv.puzzles.runner.run_puzzle(id, bot)` does the same from Python. Per
turn the solver's `negotiate` runs (three rounds, as the server's house-bot
rounds), then the opponents act on fresh views, then the solver acts. The
opponents never negotiate, so nothing depends on when the server runs its
negotiation rounds; an opponent party to a deal that executes mid-turn
recomputes its orders on the server, which gives the same orders.

### Registry bots as a sanity check

Offline, seed 0 (`python -m agentciv.puzzles run <id> --bot <name>`):

| puzzle | solution | baseline | economist | strategist | banker | spoiler | rusher | turtle | zealot | random |
|---|---|---|---|---|---|---|---|---|---|---|
| winter | 100 | 3 | 30 | 30 | 6 | 30 | 30 | 30 | 52 | 0 |
| market | 100 | 0 | 0 | 0 | 31 | 0 | 0 | 0 | 0 | 0 |
| contracts | 100 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| stop-victory | 100 | 0 | 0 | 75 | 0 | 75 | 75 | 0 | 0 | 0 |

The house bots are general game players, not puzzle solvers: in `winter` most
keep the army but build nothing of the target (30); in `contracts` they all
accept the stone sale that makes them default on their rent (0); in
`stop-victory` the attackers reach the outpost one turn after the cavalry
could (75).

## The puzzles

### `winter` — Winter planning

**Position.** One player (`p1`), turn 22: the last two turns of a winter. A
capital in a valley closed off by mountains, its eight surrounding tiles
(four plains, two forests, a hills tile, a gold tile), 18 infantry; 40 food,
60 wood, 30 stone, 40 gold. The bare valley makes 10 food a turn (5 in
winter) against 18 food of upkeep, 5 wood, 3 stone and 3 gold. The horizon
(24 turns) ends with the four winter turns 42–45.

**Objective.** Reach the end of turn 45 with all 18 infantry and with walls
level 2 and a market hall in the capital.

**Score.**

    lost  = max(0, 18 - infantry at the end)      starved, disbanded or killed alike
    K     = max(0, 1 - 2 * lost / 18)             each lost unit costs 1/9; half the army, everything
    P     = cost of the target levels standing / 260
            (a level's cost counts every unit of resource: walls 1 = 60, walls 2 = 120, market hall = 80)
    score = round(K * (30 + 70 * P))

**Reference solution** (`WinterSolution`): a fixed build order, each item
built as soon as it is affordable and nothing later before it — three farms,
a quarry, a lumber mill, the fourth farm, walls 1, the market hall, walls 2 —
buying the stone shortfall of the next item on the market from turn 30.
Score 100. Measured alternatives: doing nothing starves 8 infantry (3); the
same order without the lumber mill runs short of wood and never builds walls 2
(68); building the target before the farms starves 8 infantry (11).

### `market` — Market-funded construction

**Position.** One player (`p1`), turn 6 (the first turn of summer). A
capital in a closed valley with seven farmed plains and a forest with a
lumber mill: food income 45 a turn in summer, 30 in autumn, against 12 food
of upkeep, and the food stock (290) is at the 300 cap, so unsold food is
lost. No hills: stone comes only from the capital (1 a turn) and the market.
70 wood, 30 stone, 10 gold.

**Objective.** By the end of turn 13, a market hall and walls level 2 in the
capital, with all 12 infantry.

**Score.**

    P     = cost of the target levels standing / 260 (market hall = 80, walls 1 = 60, walls 2 = 120)
    lost  = max(0, 12 - infantry); K = max(0, 1 - 2 * lost / 12)
    score = round(100 * K * P)

The target needs 100 wood (70 in stock, about 37 more by turn 13) and 160
stone (30 in stock, 8 more by turn 13), so about 125 stone has to be bought
with gold that only food sales can raise. The pool is a constant-product
market (rules §7): one big batch sells at a much worse price, and the pool
recovers only a quarter of the way each turn.

**Reference solution** (`MarketSolution`): every turn sell up to 100 food
(the largest order the 400-food pool takes), buy as much stone as the gold on
hand pays for, and order the market hall, walls, walls (the market hall comes
first: its 2% fee and +5 gold a turn). Score 100. Measured alternatives:
selling the whole food stock at once, selling only the food income, saving
the gold to buy at the end, selling 50 food a turn or building the market hall
last all stop at the market hall and walls 1 (54); selling wood as well leaves
too little wood (23); doing nothing scores 0.

### `contracts` — Contract valuation

**Position.** Five players, turn 2; the solver is `p5` (Treasurer). It has 20
infantry, 220 food (food income 10 a turn in spring, 15 in summer, against 20
upkeep), 60 wood, 70 stone (+3 a turn), 160 gold, 200 banked gold, and a rent
contract from turn 0: it pays Quarry (`p4`) 20 stone a turn for 4 more turns.
Drifter (`p3`) has 10 gold, 2 gold income, two earlier defaults, 40 influence
debt, and pays Quarry 30 gold a turn for 8 more turns. Vault (`p2`) has 400
gold, 500 banked and 5 contracts honoured. Six offers to the solver are open
until the end of turn 4:

| deal | from | the solver gives / gets | worth (V(S) - V(none)) |
|---|---|---|---|
| d3 (O1) | Miller | gives 100 food, gets 120 gold | +20 |
| d4 (O2) | Vault | gives 100 gold, gets 15 gold a turn for 8 turns | +20 |
| d5 (O3) | Miller | gives 100 gold, gets 40 wood | −40 |
| d6 (O4) | Drifter | gives 150 gold, gets 25 gold a turn for 8 turns | −75 (three instalments, then a default; nothing to seize) |
| d7 (O5) | Quarry | gives 60 stone, gets 150 gold | −130 (the stone left can't pay the rent: default, fine and 160 bank gold seized) |
| d8 (O6) | Vault | gives 100 food, gets 110 gold | +10 alone; −198 with d3 (the army starves) |

**Valuation** (gold equivalent; start prices of rules §7, as contract
defaults use):

    V = gold + bank + food + 1.5 * wood + 2 * stone
        + 35 * infantry                       (an infantry's recruit cost at those prices)
        + 2 * (influence - influence_debt)    (2 gold per influence, the default-fine rate)

`V(S)` is the solver's V at the end of turn 9 when the offers in S are
accepted on turn 2, in the order they were accepted in the game (an offer
that cannot be delivered at that point is skipped), and the solver does
nothing else while the traders follow their scripts: eight turns, the length
of the longest contract. Only which offers were accepted counts; counters,
rejections and everything else the solver does are not scored, and the game
itself ends when the offers expire (turn 4).

    score = round(100 * (V(S) - V(none)) / (V(best) - V(none))), clamped to 0..100

The best set is d3 + d4 (V(best) − V(none) = 40), so accepting either alone
scores 50, d4 + d8 scores 75, and accepting every offer that looks profitable
on its face (d3, d4, d6, d7, d8) scores 0. A test recomputes the best subset
from all 64.

**Reference solution** (`ContractSolution`): accept d3 and d4 on turn 2.

### `stop-victory` — Stop an imminent victory

**Position.** Two players, turn 40, `max_turns` 45, so the economic target B
is 1800 (rules §11). The rival `p1` (Ledger: the `banker` house bot, seed 7,
which never negotiates) has 1850 banked gold and an economic streak of 6, and
its gold income (25 a turn from mined gold fields) pays its streak deposit
every turn: at the end of turn 43 the streak reaches 10 and it wins. It owns
its capital (3, 7) with 4 archers and an outpost city at (8, 7) with walls 1
and one archer, and has no wood to recruit with. The solver `p2` owns a
capital at (13, 7) and an army at (12, 7): 5 cavalry and 4 infantry. A road
of open plains along y = 7 joins everything; mountains close the rest.

**Objective.** Prevent the rival's victory, as early as possible.

**Score.** k = the turn, counted from turn 40 = 0, at whose end the rival's
economic streak first ends (`streak_ended` event):

    0                                  if the rival wins, or its streak never ends
    min(100, 100 - 25 * (k - 1))       otherwise

Capturing one of the rival's cities is the only way the solver can end the
streak here (rules §11). The cavalry (two tiles a turn) reaches the outpost on
turn 41 (k = 1, 100); cavalry that waits a turn gives 75; the infantry alone
(one tile a turn) arrives on turn 43, the last chance (50).

**Reference solution** (`VictorySolution`): the cavalry rides two tiles a turn
and attacks the outpost on turn 41; the infantry follows. Score 100.

## Adding a puzzle

Subclass `agentciv.puzzles.base.Puzzle` in a new module of `agentciv/puzzles/`
and register an instance in `PUZZLES` (`agentciv/puzzles/__init__.py`):

* `roles` — seats in order, `(role, player name)`, the solver (`"solver"`)
  last; `opponent(role)` returns a fresh deterministic bot per house seat
  (`ScriptedBot` subclasses, or `QuietBot(house_bot_with_fixed_seed)`);
* `start_turn`, `horizon`, `seed`, `fill` (the terrain of the wiped map);
* `build(game, pids)` lays out the position (`paint`, `set_player`,
  `game.add_city`, `game.place_units`);
* `observe(game, events)` (optional) records what the final state no longer
  shows in `game.puzzle_state`;
* `score(game)` returns `(0..100, explanation)`;
* `objective` and `scoring` are shown to the solver: describe the goal and the
  formula, not how to reach it (`tests/test_neutral_text.py` phrases);
* `solution()` returns the reference solution (a `Bot`), which must score ≥ 90;
  `baseline()` (default: the idle bot) must score ≤ 20. `tests/test_puzzles.py`
  checks both for every registered puzzle.
