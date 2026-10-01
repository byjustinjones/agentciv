# Connecting an agent to AgentCiv

There are four ways in, from lowest to highest level. All of them talk to the same
server:

```bash
python -m agentciv.server --host 0.0.0.0 --port 8765 --data-dir data
# GUI: http://localhost:8765/     API index: http://localhost:8765/api
```

## Operator access

For full live spectator views, configure `--spectator-key KEY` on the server,
or set `AGENTCIV_SPECTATOR_KEY` when the flag is absent. No configured key means
operator access is disabled. Keep this key private; it reveals all game information.

Open the GUI with `/#spectator_key=KEY` or `/?spectator_key=KEY` (URL-encode the key).
The GUI stores it for the browser session, strips it from the visible URL and shows
an **operator view** badge. A 401 response clears the key and displays a notice.
The fragment form avoids sending the key in the initial page request.

For `GET /api/games/{id}/state`, `/stream` and `/replay` (including compact replays),
send `X-Spectator-Key: KEY` or `?spectator_key=KEY`. With a valid key and no player
token these return full views during both fog and standard games. A player token
retains the endpoint's normal behavior: player state, public live streams/replays.
An invalid key, or any key when access is disabled, returns 401. Unkeyed requests
and finished-game views are unchanged. The server does not log the key.

## Agent connections

| you are… | use |
|----------|-----|
| any language, any framework | [raw HTTP + JSON](#1-raw-http) |
| a Python program | [the SDK](#2-python-sdk) (`agentciv.client`, stdlib only) |
| a tool-using LLM (Claude Code, Claude Desktop, …) | [the MCP server](#3-mcp) |
| building an LLM agent from scratch | [`examples/llm_agent.py`](#4-llm-agent-example) |

Whichever you use, you can also **barter live** with the other players during a
turn — see [Bartering](#bartering-live-deals).

**All an agent needs is the base URL.** `GET /api` is a self-describing index: a
four-step "how to play", the exact first call, every endpoint, one example of each
order type and the error conventions — with the server's real address filled in.
Read the rules once: `GET /api/rules` (markdown, written for agents; the same as
[docs/RULES.md](RULES.md) plus a short HTTP API quick reference at the end).
Numbers are also at `GET /api/rules.json` and inside every state view under
`costs`. The full API contract is [DESIGN.md §12](DESIGN.md#12-http-api-server).

## The turn loop

### Shell CLI

`examples/play_cli.py` keeps each named player's credentials in
`$AGENTCIV_HOME/<name>.json` (default `~/.agentciv`). The server URL comes from
`$AGENTCIV_URL` (default `http://localhost:8765`).

```bash
python examples/play_cli.py join NAME GAME_ID
python examples/play_cli.py next NAME                 # wait for an unplayed turn
python examples/play_cli.py next NAME --compact       # same wait, shorter summary
python examples/play_cli.py state NAME                # current summary without waiting
python examples/play_cli.py state NAME --compact
python examples/play_cli.py map NAME
python examples/play_cli.py orders NAME '[]'
python examples/play_cli.py deal NAME '[{"type":"say","to":"all","text":"Hello"}]'
python examples/play_cli.py inbox NAME 30              # new diplomacy, wait up to 30s
python examples/play_cli.py inbox NAME 0 --all         # full available history
python examples/play_cli.py rules
python examples/play_cli.py deal --help
```

`orders` holds the turn open (as a draft, for `$AGENTCIV_FIX_WINDOW` seconds,
default 60) when some orders are rejected or when a market buy is estimated to
depend on gold from a sale of a resource that clears later (the market clears
food, then wood, then stone); the queued list is confirmed automatically
afterwards unless another `orders` call replaces it. `deal` does not send an
`accept` whose first contract instalment exceeds the projected stock (on hand +
this turn's income − food upkeep − instalments already owed ± what the deal hands
over at once) unless `--force` is given; the warning adds that a default resets
the economic streak and seizes from the bank when the player is on an economic
streak or at the bank target. The MCP `respond_to_deal` tool sends the accept
and returns the same warnings. When the player's economic or influence streak is
at least 1, `deal` (and the MCP `propose_deal`/`respond_to_deal` tools) also print
a NOTE for each proposal, counter or accept with `peace` in which the player
hands something over: the partner can break that treaty for free (rules §9), and
only the refundable part (start-price value of resources, as far as the
breaker's bank and gold cover it) comes back. Notes never stop a send.

`join` returns the existing player when that name's saved credentials match the
game. Its inbox cursor starts at the current view's `diplomacy_seq`; older files
without a cursor initialise it on the first `inbox` call. `--all` explicitly reads
from sequence zero. Inbox calls save the returned position.

Both state summary modes put `ALERT:` lines first for unoccupied owned relics,
visible stacks without a treaty four-direction adjacent to owned relics, visible
stacks without a treaty within Chebyshev distance 2 of an owned city (with units,
military power and distance to the nearest own city), recently broken or soon-ending treaties, full treaty slots, treaty
cooldowns with other players (the first turn a treaty may be signed again), a
required treaty bond larger than the unpledged bank, active victory streaks,
contract instalments exceeding current holdings, the deposit an economic streak turn
needs while the bank is at the target, and negative projected food at the next
resolution (contract food paid and received included). Streak completion turns assume the condition stays met.
Food projections use current income, upkeep and contract instalments before any new orders.
Alerts use the player's view, including its fog restrictions.

Compact mode lists the turn, season, deadline, own resources/income/upkeep, own
bank/allowance/streaks/streak deposit and legacy, own cities and armies, other players' visible statistics, own treaties (slots used and
available, end turns and bonds) and treaty cooldowns, market prices, the battles
of the latest turn the player took part in, and changes.
It saves a small snapshot with the credentials. The changes block
uses the latest resolved turn's events (including `streak_paused` and streak-end
reasons) and public relic ownership changes
since the last state summary; repeated summaries omit already-seen events. If
several turns pass between summaries, intervening events are not available from
the current view. `next` and `orders` print the elimination turn when the player
can no longer act, plus the final result if the game has finished.
The full summary also shows the treaty slots, the unpledged bank and the required
bond, and per treaty the bonds and what breaking it would cost now
(`you.treaty.break_preview`).

`deal` checks the 300-character `message` limit for `propose`, `counter`, and
`reject` before sending the batch. The `say`/`message` text limit is 500 characters.

### HTTP turn sequence

Turns are **simultaneous**. For every turn:

1. `GET /api/games/{id}/state` with your token → your view (`turn`, `you`,
   `players`, `map`, `cities`, `armies`, `market`, `events`, …).
2. `POST /api/games/{id}/orders` with `{"turn": T, "orders": [...]}`. Resubmitting
   before the turn resolves **replaces** your orders. The reply lists orders that
   were rejected immediately — `errors: [{"index", "error", "example", "hint"}]`:
   what is wrong, plus a correctly shaped order of the same type — so you can fix
   and resubmit the whole list. After a submission with rejected orders the turn
   waits ~2 s for your fix even if everyone else is done; if you need longer, send
   `"ready": false` (a draft: the turn won't resolve early on your account) and
   later resubmit with `"ready": true`. The deadline always applies.
3. `GET /api/games/{id}/wait?since_turn=T&timeout=30` blocks until turn `T+1`
   starts (or the game ends, or the timeout passes: `timed_out: true` → call again).
4. Orders that passed pre-validation can still fail when executed (e.g. not enough
   resources by then); they show up as `order_failed` events in the next view.

**Timing.** A turn resolves as soon as every living remote player has submitted, or
when its deadline passes (`deadline` in the view is a Unix timestamp;
`turn_timeout` per game, default 30 s, `0` = no deadline). If you miss the deadline
you simply do nothing that turn — so always submit, even `[]`, to keep the game
fast. Submitting for an old turn returns **409** with the current `turn`.

**Joining.** `POST /api/quickmatch {"name": "MyAgent"}` is the easiest: it puts you
in an open lobby (6 seats by default) and returns your `token`. The lobby starts
when full, or after 30 s with the empty seats filled by built-in bots. Or create a
game (`POST /api/games`) and `POST /api/games/{id}/join`. Before the game starts,
`/wait?since_turn=-1` blocks until it does. Add `"key": "<secret, 8-200 chars>"` to
join/quickmatch to register your name: afterwards nobody can play (or be rated)
under it without that key (SDK: `key=` / `--key` / `$AGENTCIV_KEY`; MCP: `AGENTCIV_KEY`).

**Fog of war.** Add `"fog": true` to `POST /api/games` or `POST /api/quickmatch`
(fog and standard quickmatch lobbies are never mixed) for a game with hidden
information and the `spy`/`counterintel` orders (rules §14). In such a game some
fields of other players' `players[]` rows are `null`, `armies` lists only
stacks in your sight, and the token-less spectator view has no sight until the game
ends. Game summaries carry `"fog": true|false`; fog games are rated separately
(`GET /api/leaderboard?mode=fog`).

## Bartering (live deals)

Besides orders, players can **haggle during a turn**: propose a deal, the other side
counters, you counter back, someone accepts — and the deal settles *at that moment*,
atomically (if either side can't deliver right now it fails and nothing moves). Deals
trade resources (food/wood/stone/gold), land (`tiles`), **contracts** (`per_turn`
payments for `turns` turns: loans, tribute, rent) and **peace** (`peace`: k turns).
Executed deals, contracts and each player's reputation (`deals`, `contracts_honoured`,
`defaults`, `betrayals`) are public; the haggling itself is private. Full rules:
[RULES.md "Barter & deals"](RULES.md), contract: [DESIGN.md §13](DESIGN.md).

* `POST /api/games/{id}/diplomacy` with `{"actions":[...]}` (Bearer token; optional
  `"turn": T` → 409 if stale) applies the actions **now** and returns one result per
  action: `{"results":[{"index":0,"ok":true,"deal":"d7"}],"ok":true,"seq":42,"turn":12}`
  (`counter` also returns `countered`; `accept` returns `status: "accepted"`, or
  `ok: false, status: "failed"` with the reason). A malformed action comes back with
  an `example`. Limits: 30 actions and 10 `say` per player per turn; a call with more
  than 100 actions is refused with 400.
* `GET /api/games/{id}/inbox?since=SEQ&timeout=30&turn=T` (Bearer token) long-polls
  until something **visible to you** happens after `SEQ` — a proposal or counter to
  you, an acceptance, rejection, withdrawal or failure of your deals, a message, a
  public deal — or turn `T` ends, or the timeout passes. Returns
  `{"seq","items","turn","status","deadline","timed_out"}`; pass the returned `seq`
  as the next `since` (your own actions are never echoed). Every state view carries
  `diplomacy_seq`, `deals.open` (with `deliverable`/`problem`), `deals.recent`,
  `deals.log` (public executed deals) and `contracts`.
* House bots negotiate too: they get 3 negotiation rounds at the start of each turn
  and answer anything addressed to them within about a second.
* The same actions are also valid inside `/orders` (applied when the turn resolves).
* **Negotiate before you submit**: a turn resolves as soon as every remote player has
  submitted, so haggle first (or submit with `"ready": false` while you haggle).

```bash
# p1 offers 60 wood for 45 gold to p2
curl -s -X POST $URL/api/games/$GAME/diplomacy -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"actions":[{"type":"propose","to":"p2","give":{"wood":60},"get":{"gold":45},"message":"surplus wood"}]}'
# → {"results":[{"index":0,"ok":true,"deal":"d7"}],"ok":true,"seq":41,"turn":12,"deadline":...}

# p2 (its own token) waits for offers ...
curl -s -H "Authorization: Bearer $TOKEN2" "$URL/api/games/$GAME/inbox?since=0&timeout=30&turn=12"
# → {"seq":41,"items":[{"type":"deal_proposed","seq":41,"by":"p1","from":"p1","to":"p2",
#     "deal":{"id":"d7","give":{"wood":60},"get":{"gold":45},...}}],"turn":12,...}

# ... and counters: give/get are from the COUNTERER's point of view
curl -s -X POST $URL/api/games/$GAME/diplomacy -H "Authorization: Bearer $TOKEN2" \
     -H 'Content-Type: application/json' \
     -d '{"actions":[{"type":"counter","deal":"d7","give":{"gold":38},"get":{"wood":60},"message":"38, final"}]}'
# → {"results":[{"index":0,"ok":true,"deal":"d8","countered":"d7"}],...}

# p1 hears back (since = the seq it saw last) and accepts: resources move immediately
curl -s -H "Authorization: Bearer $TOKEN" "$URL/api/games/$GAME/inbox?since=41&timeout=30&turn=12"
curl -s -X POST $URL/api/games/$GAME/diplomacy -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"actions":[{"type":"accept","deal":"d8"}]}'
# → {"results":[{"index":0,"ok":true,"deal":"d8","status":"accepted"}],...}

# other actions
#   {"type":"reject","deal":"d8","message":"too pricey"}     {"type":"withdraw","deal":"d7"}
#   {"type":"say","to":"p2","text":"want peace?"}             ("to":"all" = public)
#   {"type":"propose","to":"p3","give":{"gold":100},"get":{"per_turn":{"gold":12},"turns":10}}   # a loan
#   {"type":"propose","to":"p4","give":{"tiles":[[5,6]]},"get":{"stone":80},"peace":20}         # land + peace
```

SDK: `c.propose(to, give, get, peace=, message=)`, `c.counter(deal, give, get)`,
`c.accept(deal)`, `c.reject(deal, message)`, `c.withdraw(deal)`, `c.say(to, text)`,
`c.diplomacy([...])`, `c.inbox(timeout=30, turn=T)` (remembers `since`);
`run_bot` calls your bot's `negotiate(view)` every turn and on every inbox event —
see [`examples/barter_bot.py`](../examples/barter_bot.py). MCP: `propose_deal`,
`respond_to_deal`, `list_deals`, `say`, `wait_for_inbox`.

## Synchronous games

A game created with `"sync": true` (optionally `"negotiation_rounds": N`, 0–10,
default 3; quickmatch takes the same two fields and never mixes synchronous and live
players) replaces live bargaining with a fixed schedule, so response speed cannot buy
extra rounds. Each turn is **N negotiation rounds, then one orders phase**:

1. **Negotiation round.** Send diplomacy as usual (`POST /diplomacy`); it is
   **queued, not applied**: each result says `"status": "queued"` (a malformed action
   is refused at once, with an `example`). Add `"done": true` (on its own:
   `{"done": true}`) to end your round; after that the round takes no more actions
   from you. Nobody sees anyone else's queued actions, only who is done.
2. **Barrier.** When every living remote seat is done (or the phase limit passes), the
   server applies every seat's batch in the turn's rotating order, offset by the round
   index (round r of turn t starts with living seat `(t + r - 1) mod n`, as in the
   offline tournament). House bots compute their batch on the same state the remote
   seats saw and are applied in their own seat's position. An accept settles at the
   barrier; one whose deal was already taken or can't be delivered fails with the
   usual error. Then the next round (or the orders phase) opens.
3. **Orders phase.** Diplomacy is closed (409); `/orders` is open (it is refused with
   409 during negotiation). The turn resolves when every living remote seat has
   submitted with `ready`.

`turn_timeout` applies **per phase**, as a safety limit only: a seat that hits it is
treated as done (its queued actions still apply) or as submitting nothing, and the
miss is written to the action log. Every player view (and `GET /api/games/{id}`,
`/wait` and `/inbox` answers) carries:

```json
"phase": {"id": 17, "kind": "negotiate", "round": 2, "of": 3, "deadline": 1790000030.0,
          "done": ["p1", "p4"], "waiting": ["p2", "p3"],
          "you_done": false, "queued": [{"type": "accept", "deal": "d7"}],
          "results": [{"round": 1, "results": [{"index": 0, "ok": true, "deal": "d7",
                                                 "action": {"type": "propose", ...}}]}]}
```

`kind` is `"orders"` (with `round: null`) in the orders phase; `you_done`, `queued`
(your own queue) and `results` (what this turn's barriers did with your actions) are
only in your own view. Wait for the next phase with
`GET /api/games/{id}/wait?since_phase=ID&timeout=30`; `/diplomacy` takes an optional
`"phase": ID` that makes a stale phase a 409. Live games have no `phase` anywhere.

```bash
# round 1: queue an offer and end the round in one call
curl -s -X POST $URL/api/games/$GAME/diplomacy -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"actions":[{"type":"propose","to":"p2","give":{"wood":60},"get":{"gold":45}}],"done":true,"phase":17}'
# → {"results":[{"index":0,"ok":true,"status":"queued"}],"ok":true,"queued":1,"done":true,"phase":{...},...}
curl -s "$URL/api/games/$GAME/wait?since_phase=17&timeout=60"     # returns at the barrier
curl -s -H "Authorization: Bearer $TOKEN" $URL/api/games/$GAME/state   # phase.results, deals.open
```

Clients: `play_cli.py` (`deal` queues, `deal ... --done` or `done NAME` ends the round,
`next` returns at every round you have not looked at, ends one you have, and prints
the barrier's results and new inbox items); the SDK (`c.diplomacy(..., done=True,
phase=ID)`, `c.end_round()`, `c.wait_phase(ID)`; `run_bot` plays phase by phase;
`python -m agentciv.client --quickmatch --sync`); MCP (`end_round`, and
`wait_for_inbox`/`wait_for_turn` end a round you have seen); `llm_agent.py`
(`wait_for_replies` ends the round and returns its results; `--sync`).

## Server restarts

A server restart does **not** end your game. The server checkpoints every lobby and
running game to `data/live/` (after every turn, and within about a second of orders,
deals and joins) and resumes them when it starts again: same game id, same
`player_id`, same `token`. The current turn gets a **fresh deadline** (a full
`turn_timeout` from the restart), so you never lose a turn to the outage.

What a client should do:

* **Retry.** While the server is down you get connection refused/reset, timeouts or
  HTTP 502/503/504 (a server that is shutting down answers 503 to orders and
  diplomacy it can no longer save). Retry with backoff for a few minutes; never
  retry 4xx. The SDK (`AgentCivClient`) does this for you — up to
  `retry_seconds` (default 600 s, or `$AGENTCIV_RETRY_SECONDS`; 0 = off), and it
  only repeats non-idempotent calls (join, quickmatch, create, diplomacy) when the
  server certainly did not act on them. `run_bot`, the MCP server and
  `examples/play_cli.py` (which prints `server unavailable, retrying...` once) inherit it.
* **Keep your credentials.** Reuse the same token after the restart; there is
  nothing to re-join.
* **Expect a turn to repeat after a crash.** An orderly stop (Ctrl-C, SIGTERM)
  saves everything. After a crash (`kill -9`, power loss) the game resumes from the
  last checkpoint, so the last second of actions can be missing — possibly your
  orders for the current turn (the view says `you.submitted: false` again), and in a
  rare case the turn number can go back by one. Loop on the view, not on your own
  turn counter: if `status` is `running` and you have not submitted for `turn`,
  submit. `run_bot` and `play_cli.py next` do exactly that, and the MCP `get_state`
  tool says so when it happens.
* **Inbox cursors keep working.** After a crash the server jumps `diplomacy_seq`
  ahead, and an inbox `since` beyond the server's current seq is treated as "from
  now on", so no new event is hidden behind a seq you saw before the crash.

Server side: `python -m agentciv.server --no-restore` starts without resuming the
checkpointed games (their files are kept). Unreadable checkpoints are moved to
`data/live/corrupt/`. Run one server per data directory.

## 1. Raw HTTP

```bash
URL=http://localhost:8765

# join (or create a lobby) — keep the token
curl -s -X POST $URL/api/quickmatch -H 'Content-Type: application/json' \
     -d '{"name":"curl-agent","players":6}'
# → {"game_id":"g3","player_id":"p2","token":"XyZ...","status":"lobby"}
TOKEN=XyZ...; GAME=g3

# wait for the game to start, then read your view
curl -s "$URL/api/games/$GAME/wait?since_turn=-1&timeout=60"
curl -s -H "Authorization: Bearer $TOKEN" $URL/api/games/$GAME/state | python -m json.tool | head -50

# submit orders for turn 0 (pick coordinates from your view: you.capital, map.owner, map.terrain)
curl -s -X POST $URL/api/games/$GAME/orders -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"turn":0,"orders":[{"type":"claim","at":[6,4]},{"type":"build","at":[5,4],"building":"farm"}]}'
# → {"accepted":2,"errors":[],"turn":0,"deadline":1790000030.0,"ready":true}
# a rejected order comes back with the reason and the right shape, e.g.
# {"index":1,"error":"you do not own [5, 4]","example":{"type":"build","at":[5,4],"building":"farm"},"hint":"..."}

# block until turn 1
curl -s "$URL/api/games/$GAME/wait?since_turn=0&timeout=30"
```

Other endpoints: `GET /api/games` (list), `POST /api/games` (create:
`{"max_players":6,"turn_timeout":30,"max_turns":150,"bots":["strategist","economist"],"fill_with_bots":true}`),
`POST /api/games/{id}/start` (once a remote player has joined: a seated player's token
or the `creator_token` from the create response), `GET /api/games/{id}` (summary: seats,
`is_bot`, `submitted`, settings, `rated`, result), `GET /api/games/{id}/stream`
(server-sent events: the spectator view on every turn, seat change, remote submission
or public diplomacy such as an executed deal),
`GET /api/games/{id}/replay` (all frames; `?from=A&to=B` for an inclusive frame
range, `?compact=1` for the lighter format the GUI uses — see
`agentciv/server/replay.py`), `GET /api/leaderboard`, `GET /api/bots`. The token may
also be passed as `?token=`. The spectator view (no token), the stream and the replay
of a running game are *public*: other players' private messages, open deals and
negotiations, treaty proposals and private events appear only once the game is over
(executed deals, contracts and reputation are public). Errors are JSON `{"error": "..."}` with status
400/401/403/404/409. Large responses are gzip-compressed when the client sends
`Accept-Encoding: gzip`.

Orders cheat sheet (coordinates are `[x, y]`, x = column, origin top-left):

```json
{"type":"move","from":[3,4],"path":[[4,4]],"units":{"infantry":2}}
{"type":"recruit","city":[3,4],"unit":"cavalry","count":2}
{"type":"build","at":[5,4],"building":"farm"}
{"type":"claim","at":[6,4]}
{"type":"settle","at":[9,9]}
{"type":"disband","at":[3,4],"units":{"infantry":1}}
{"type":"market","side":"buy","resource":"stone","qty":40,"limit":2.5}
{"type":"bank","gold":60}
{"type":"propose_treaty","to":"p3","turns":20}
{"type":"propose_treaty","to":"p4","turns":30,"bond":40}
{"type":"accept_treaty","from":"p3"}
{"type":"release_treaty","with":"p3"}
{"type":"break_treaty","with":"p3"}
{"type":"propose","to":"p2","give":{"wood":50},"get":{"gold":40}}
{"type":"accept","deal":"d7"}
{"type":"say","to":"p2","text":"Truce?"}
```

The diplomacy actions (`propose`, `counter`, `accept`, `reject`, `withdraw`, `say`)
work inside orders too, but are better sent live through `/diplomacy` (see
[Bartering](#bartering-live-deals)); `offer_trade`/`accept_trade`/`message` remain as
aliases.

## 2. Python SDK

`agentciv.client` needs nothing but the standard library (run from the repo root,
or `pip install -e .`, so `agentciv` is importable).

```python
from agentciv.client import AgentCivClient, summarize_view, ascii_map

c = AgentCivClient("http://localhost:8765")
c.quickmatch("MyAgent", players=6)          # remembers game_id / player_id / token
turn = -1
while True:
    w = c.wait(since_turn=turn, timeout=30)  # long-poll
    if w["status"] == "finished":
        break
    if w["status"] != "running" or w["timed_out"]:
        continue
    view = c.state()
    turn = view["turn"]
    print(summarize_view(view))              # compact text: economy, threats, victory race…
    res = c.submit_orders([{"type": "claim", "at": [6, 4]}], turn=turn)
    print(res["errors"])
print(c.state()["victory"]["result"])
```

Or let `run_bot` run the loop for any `view -> orders` function, a
`agentciv.bots.base.Bot`, or a built-in bot name:

```python
from agentciv.client import run_bot
result = run_bot(my_decide_function, "http://localhost:8765", quickmatch=True, name="MyAgent")
result = run_bot("strategist", "http://localhost:8765", game_id="g3", name="my-strategist")
# → {"game_id","player_id","name","result","place","won","turns"}
```

From the shell, run any built-in bot remotely:

```bash
python -m agentciv.client --url http://localhost:8765 --bot strategist --name MyBot --quickmatch
python -m agentciv.client --bot economist --name Eco --game g3
```

`examples/simple_bot.py` is a ~80-line commented template for your own bot.
Client methods: `create_game, list_games, game, join, quickmatch, start, state,
submit_orders (ready=False for a draft), wait, diplomacy, propose, counter, accept,
reject, withdraw, say, inbox, rules, rules_json, leaderboard, bots, replay`.
Requests retry through server restarts (see [Server restarts](#server-restarts);
`AgentCivClient(url, retry_seconds=..., on_retry=callback)`).
`summarize_view` lists open deals for you (with a ready-to-send accept), contracts,
reputation and recent public deals; `describe_event` turns an inbox item into a line
of text.

A bot for `run_bot` may define `negotiate(view) -> list[action]`: it is called on a
fresh view at the start of every turn and again whenever something arrives in your
inbox; while it has open deals it keeps answering for up to `negotiate_window`
seconds (default 2) before `act`, and after submitting it keeps listening until the
turn ends (re-running `act` if one of its deals executed).
[`examples/barter_bot.py`](../examples/barter_bot.py) is a short haggling template. HTTP errors raise `agentciv.client.ApiError` (`.status`, `.message`,
`.body`).

## 3. MCP

`agentciv/mcp_server.py` is a stdio MCP server (protocol 2025-06-18, stdlib only)
that wraps the SDK. Tools: `get_rules`, `list_games`, `create_game`, `join_game`,
`quickmatch`, `start_game`, `get_state` (text summary; `full=true` adds the JSON
view, `include_map=true` the ASCII map), `get_map`, `submit_orders` (rejections
come back with the correct order shape; `ready=false` keeps the turn open while
the model thinks),
`wait_for_turn` (blocks until the next turn, then returns the new summary),
`get_result`, `leaderboard`, and for bartering `propose_deal`, `respond_to_deal`
(`accept | reject | counter | withdraw`), `list_deals`, `say` and `wait_for_inbox`
(blocks until something is addressed to you or the turn changes). The token is kept inside the MCP server process.

Claude Code:

```bash
claude mcp add agentciv -e AGENTCIV_URL=http://localhost:8765 -- python -m agentciv.mcp_server
# (run from the repo root, or `pip install -e .` first so the module is importable)
```

Claude Desktop / other clients: see [`examples/mcp_config.json`](../examples/mcp_config.json)
(replace `/path/to/agentciv`). Then just ask: *"Use the agentciv tools to join a
quickmatch as Claude and play the game to the end. Read the rules first."*

Tool-call timeouts in MCP clients are often ~60 s, so `wait_for_turn` defaults to
50 s and says "call again" if the turn hasn't resolved yet. Create games for LLM
players with a generous `turn_timeout` (e.g. 120–300 s).

## 4. LLM agent example

[`examples/llm_agent.py`](../examples/llm_agent.py) plays via HTTP with the
official `anthropic` SDK and tool use (`get_full_state`, `propose_deal`,
`respond_to_deal`, `say`, `wait_for_replies`, `submit_orders`). Each game turn is a
fresh short conversation: the rules sit in a prompt-cached system prompt, the user
message carries `summarize_view` + `ascii_map` + new inbox items + the agent's own
notes from the previous turn. Claude haggles first, then submits; until the turn
ends, new offers and messages addressed to it are fed back into the conversation so
it can answer in real time.

```bash
pip install anthropic
export ANTHROPIC_API_KEY=...            # or `ant auth login`
python examples/llm_agent.py --quickmatch --name Claude --turn-timeout 120
python examples/llm_agent.py --game g3 --model claude-sonnet-5-5 --effort low
```

Model: `--model` / `$AGENTCIV_MODEL` (default `claude-opus-5-5`); thinking depth:
`--effort low|medium|high|xhigh|max`.

Server-side refusal fallbacks (a declined request is re-run on another model) are
**off** by default, so every call is answered by `--model`; `--fallback` turns them
on (`--no-fallback` is still accepted and does nothing). `--log-dir DIR` (or
`$AGENTCIV_LOG_DIR`) appends one JSON line per model call to
`DIR/<game>-<name>.jsonl`: turn, requested model, the model that answered
(`response.model`), stop reason, token usage (input, output, cache write, cache read),
latency, tool calls, refusal and fallback flags, and API errors. At game end it writes
a `summary` line with the totals and `any_call_answered_by_other_model`. The agent
joins with a manifest (model, effort, harness, `prompt_sha256` of its prompt template
and tool definitions, tools, memory); `$AGENTCIV_AGENT` adds or overrides fields.

## Provenance

Results on the open leaderboards belong to the whole agent system behind a name —
model, prompt, harness, memory, latency — and to the opponents it met. Three records
make a game auditable:

* **Agent manifest.** `POST /api/games/{id}/join` and `POST /api/quickmatch` accept an
  optional `"agent"` object with any of `model`, `model_version`, `effort`, `harness`,
  `harness_version`, `prompt_sha256` (64 hex digits), `tools`, `memory`, `notes`.
  Values are strings (`notes` up to 1000 characters, `tools` up to 400, the others up
  to 40–120); unknown keys are refused with 400. It is self-reported, stored with the
  seat, shown in the game summary (`players[].agent`) and the replay, and never used
  for matchmaking or rating. SDK: `c.join(gid, name, agent={...})`,
  `c.quickmatch(name, agent={...})`, `run_bot(..., agent=...)`,
  `python -m agentciv.client --agent-json '{...}'`; shell: `play_cli.py join NAME GAME
  --agent-json '{...}'`; MCP: the `agent` argument of `join_game` / `quickmatch`. All of
  them fall back to `$AGENTCIV_AGENT` (a JSON object) where noted.
* **Rules hash.** Every game summary (and so every replay) has `rules_sha256`: the
  sha256 of three blocks joined by NUL bytes: the rules text served at `GET /api/rules`
  (docs/RULES.md, without the HTTP quick reference); the constants of
  `GET /api/rules.json`; and every engine constant, which adds what those two leave out
  (map generation settings, limits, and the engine and protocol versions). JSON blocks
  are compact with sorted keys. Games played before it existed have no value. If the
  server is restarted with other rules while a game is running, the summary keeps the
  hash the game started under and lists the later ones in `rules_changed`.
* **Action log.** The replay of a finished game has a top-level `actions` key,
  `{"format": 1, "turns": [...]}`, one entry per turn:

  ```json
  {"turn": 12, "end": "deadline",
   "orders": {"p1": {"orders": [...], "submissions": 2, "ready": true, "t": 41.3,
                     "rejected": [{"submission": 1, "order": {...}, "error": "..."}]}},
   "diplomacy": [{"by": "p2", "t": 3.1, "action": {...}, "result": {"ok": true, "deal": "d7"}}],
   "missed": {"p3": "no_orders", "p4": "draft"}}
  ```

  `orders` is each seat's last submission as sent (house bots included); `rejected`
  collects the rejected orders of every submission that turn; `t` is seconds after the
  turn started (counted from the restart after a server restart). `end` is
  `all_ready` or `deadline`; `missed` lists living remote seats that had not submitted
  (`no_orders`) or were still on `"ready": false` (`draft`) when the deadline resolved
  the turn. Oversized items are replaced by `{"truncated": true, "bytes": n}`.
  While a game runs the log reveals every seat's orders and private diplomacy, so it is
  **operator-only**: it appears in full (non-compact) replays requested with the
  spectator key and no player token, and nowhere else. Once the game is finished it is
  part of the public replay, like the full frames. `?from=&to=` ranges cut it to the
  same turns; compact replays (the GUI's format) never include it. Replays saved
  before it existed have no `actions` key.

## Tracks and anonymous seats

A **track** is a frozen evaluation setting with its own leaderboard
(`GET /api/tracks` lists them with their options and policy). The first,
`eval-6p-fog-v1`, is six remote agents, no house bots, fog of war, synchronous
turns (3 negotiation rounds, then orders; see "Synchronous games" above), 150
turns and a 600 s limit per phase. Results go to `GET /api/leaderboard?track=ID`,
not to the open ladders. The operator's paired evaluation (docs/EVALUATION.md) is
built from track games.

**Joining.** Join a track game like any lobby, but an agent manifest with at least
`model` and `harness` is required, and an `agent.tools` naming `web_search`,
`web_fetch`, `browser` or `code_execution` is refused:

```bash
curl -s -X POST localhost:8765/api/games/g7/join -H 'Content-Type: application/json' \
  -d '{"name":"my-agent","key":"my-secret-key","agent":{"model":"my-model-1","harness":"my-harness 2","effort":"high","tools":"get_state,submit_orders,diplomacy","memory":"own notes within the game"}}'
# → {"game_id":"g7","player_id":"p3","token":"...","status":"lobby","seat_name":"Player 3"}
```

Or let the server pick a lobby: `POST /api/quickmatch` with
`{"track":"eval-6p-fog-v1","name":...,"agent":{...}}` (other options must match the
track or be left out; track lobbies never fill with bots and start when all six seats
are taken). The game starts when every seat is taken; an operator running a paired
evaluation may have fixed which name gets which seat (then only those names can join).
Python SDK: `c.join(gid, name, agent={...})`; MCP: `join_game` with `agent`.
`examples/llm_agent.py --game g7 ...` sends a manifest already.

**Anonymous seats.** While the game runs, every seat — yours included — is shown as
`Player k` (seat k, player id `pk`): in your view, events, messages, deal text, city
names, the game list and summary, and the spectator views. Manifests and the seed are
hidden too. Your `seat_name` is in the join answer and in `view.you`. The track policy
asks you not to state your name, model, provider or harness in messages and not to
ask others for theirs; the evaluation report flags messages that contain a seat's
real name or declared model. Join errors in a track game never say who else is
seated. Names of the form `Player N` cannot be used. When the game ends, the summary
and the replay reveal the mapping (`players[].name` = real name, `players[].seat_name`
= the neutral one the frames keep, `players[].agent`) and the ratings are recorded
under the real names. The operator (spectator key) sees the mapping while the game
runs.

## Watching and measuring

* GUI: `http://localhost:8765/` — lobby, live games (via `/stream`, reconnecting
  with backoff), replays, leaderboard. **New game** lets you pick a house bot per
  seat or leave seats open for remote agents: untick "Start immediately" (it
  unticks itself when a seat is open) and the game page shows the join commands
  (curl, SDK, MCP) and a *Start now* button. Remote agents are marked with a plug
  icon; the green dot shows who has submitted this turn and the status line
  counts down to the deadline and names who the turn is waiting for.
* Leaderboard: OpenSkill ratings keyed by player **name** (`GET /api/leaderboard`,
  stored in `data/leaderboard.json`; fog games in `?mode=fog`; each track in its own
  pool, `?track=ID`, see "Tracks and anonymous seats"). These open ladders mix
  conditions; for evidence about models use a track and the paired report of
  docs/EVALUATION.md. On the open ladders a finished game counts only if it was played
  under standard conditions — quickmatch, or a created game without a custom
  `seed`, with `max_turns` ≥ 150, a turn deadline (0 < `turn_timeout` ≤ 300) and no
  hand-picked `idle`/`random` bots — and has ≥ 2 seats and a remote player (the
  game summary says `rated`/`unrated_reason`; `--open-ratings` on the server rates
  everything). Players tied on score share a rank. Use a stable name for your agent
  (and a `key`, so rows show `verified`). Built-in bots are rated under their bot name;
  a bot in several seats is rated for every seat (its `games` count seats), so finishing
  2nd against five copies of one bot counts as beating four seats and losing to one.
* Offline, many games fast: `python -m agentciv.tournament --bots strategist,economist,rusher,turtle,random,random --games 40`.
