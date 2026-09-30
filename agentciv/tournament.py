"""Run bot-vs-bot tournaments in-process (no HTTP) and measure skill.

Usage::

    python -m agentciv.tournament --bots strategist,economist,rusher,turtle,random,random \\
        --games 40 --players 6 --seed 1 [--max-turns 150] [--jobs N] [--rounds 3] [--json out.json]

Every game gets its own seed (derived from ``--seed``), a fresh set of bots
and a seeded shuffle of the seats (on top of the engine's own seeded start
shuffle), so start positions do not bias the results. When more bots than
seats are given, each game draws a seeded sample of them; when fewer, the
list is repeated. Duplicate bot names get suffixes (``random#1``,
``random#2``).

Every turn, before the bots' ``act``, there are ``--rounds`` (default 3,
docs/DESIGN.md §13.6) **negotiation rounds**: in each round every living
bot, in a seat order rotating by turn and round, gets a fresh player view
and returns diplomacy actions from ``Bot.negotiate``, which are applied at
once through ``Game.diplomacy``.

Reported per bot: games, wins, win rate, average placement, OpenSkill
rating (``agentciv.ratings``; display rating = mu - 3·sigma), wins by
condition, pre-validation errors, think time (``act`` and ``negotiate``)
and trade statistics (proposals/counters sent, deals accepted, deals
executed as either party, contracts as payer/payee, defaults, instalments
paid/received, peace deals). Overall: distribution of ending conditions,
median/average game length, per-game timing, win rates by seat and by start
slot (map position, for fairness checks), and deals per game by kind (the
``[kind]`` tag the built-in bots put in their deal messages).

``run_game(bot_specs, seed, max_turns)`` is the reusable single-game API.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import Counter, defaultdict

from agentciv import ratings
from agentciv.bots import get_bot
from agentciv.engine import Game, GameConfig
from agentciv.engine import constants as C

CONDITIONS = ("conquest", "wonder", "relics", "influence", "economic", "score")


def label_bots(names: list) -> list:
    """Give duplicate bot names suffixes: [a, b, b] -> [a, b#1, b#2]."""
    counts = Counter(names)
    seen: Counter = Counter()
    out = []
    for n in names:
        if counts[n] > 1:
            seen[n] += 1
            out.append(f"{n}#{seen[n]}")
        else:
            out.append(n)
    return out


def _spec(entry) -> tuple:
    """Normalise a bot spec: "name" or (label, name) -> (label, name)."""
    if isinstance(entry, (list, tuple)):
        return str(entry[0]), str(entry[1])
    return str(entry), str(entry).split("#")[0]


NEGOTIATION_ROUNDS = 3
TRADE_KEYS = ("proposed", "countered", "accepted", "rejected", "withdrawn", "failed_accepts",
              "diplomacy_errors", "deals", "peace_deals", "contracts_payer", "contracts_payee",
              "defaults", "instalments_paid", "instalments_received", "gold_paid", "gold_received",
              "net_value")
# base market prices (gold per unit) used to value what changes hands in deals
BASE_PRICE = {r: g / a for r, (a, g) in C.MARKET_POOLS_PER_PLAYER.items()}
BASE_PRICE["gold"] = 1.0


def _bundle_now_value(b: dict) -> float:
    return sum(int(b.get(r, 0) or 0) * BASE_PRICE.get(r, 1.0) for r in C.TRADABLE)


def deal_kind(message) -> str:
    """The ``[kind]`` tag of a built-in bot's deal message (else "other")."""
    m = str(message or "")
    if m.startswith("[") and "]" in m:
        return m[1:m.index("]")] or "other"
    return "other"


def _count_actions(stats: dict, actions: list, results: list) -> None:
    for a, r in zip(actions, results):
        t = a.get("type") if isinstance(a, dict) else None
        if not r.get("ok"):
            if t == "accept" and r.get("status") == "failed":
                stats["failed_accepts"] += 1
            else:
                stats["diplomacy_errors"] += 1
            continue
        key = {"propose": "proposed", "offer_trade": "proposed", "counter": "countered",
               "accept": "accepted", "accept_trade": "accepted", "reject": "rejected",
               "withdraw": "withdrawn"}.get(t)
        if key:
            stats[key] += 1


def _count_events(g: Game, trade: dict, kinds: Counter, events: list) -> None:
    for e in events:
        t = e.get("type")
        if t == "deal_executed":
            a, b = e.get("from"), e.get("to")
            for pid in (a, b):
                if pid in trade:
                    trade[pid]["deals"] += 1
                    if e.get("peace"):
                        trade[pid]["peace_deals"] += 1
            for giver, taker, bundle in ((a, b, e.get("give") or {}), (b, a, e.get("get") or {})):
                if bundle.get("per_turn"):
                    if giver in trade:
                        trade[giver]["contracts_payer"] += 1
                    if taker in trade:
                        trade[taker]["contracts_payee"] += 1
            for pid, inn, out in ((a, e.get("get") or {}, e.get("give") or {}),
                                  (b, e.get("give") or {}, e.get("get") or {})):
                if pid in trade:
                    trade[pid]["net_value"] += _bundle_now_value(inn) - _bundle_now_value(out)
            d = g.deals.get(e.get("deal")) or {}
            kinds[deal_kind(d.get("message"))] += 1
        elif t == "contract_paid":
            payer, payee, paid = e.get("payer"), e.get("payee"), e.get("paid") or {}
            val = _bundle_now_value(paid)
            if payer in trade:
                trade[payer]["instalments_paid"] += 1
                trade[payer]["gold_paid"] += int(paid.get("gold", 0))
                trade[payer]["net_value"] -= val
            if payee in trade:
                trade[payee]["instalments_received"] += 1
                trade[payee]["gold_received"] += int(paid.get("gold", 0))
                trade[payee]["net_value"] += val
        elif t == "contract_default":
            if e.get("payer") in trade:
                trade[e["payer"]]["defaults"] += 1


def run_game(bot_specs: list, seed: int, max_turns: int = C.DEFAULT_MAX_TURNS,
             game_id: str | None = None, record_views: bool = False,
             rounds: int = NEGOTIATION_ROUNDS, fog: bool = False) -> dict:
    """Play one full game between built-in bots, in seat order.

    ``bot_specs`` is a list of bot names (labels are derived with
    :func:`label_bots`) or of ``(label, bot_name)`` pairs. Each turn starts
    with ``rounds`` negotiation rounds (see the module doc; 0 = none).
    Returns a result dict with the engine result translated to labels plus
    diagnostics and trade statistics. ``fog``: play a fog-of-war game (the
    result then also has ``fog_events``: battle and espionage event counts).
    """
    if bot_specs and all(isinstance(b, str) for b in bot_specs):
        labels = label_bots([str(b) for b in bot_specs])
        specs = [(lab, b.split("#")[0]) for lab, b in zip(labels, bot_specs)]
    else:
        specs = [_spec(b) for b in bot_specs]
    t_start = time.perf_counter()
    g = Game(GameConfig(seed=seed, max_turns=max_turns, game_id=game_id or f"t{seed}", fog=fog))
    pid_label = {}
    bots = {}
    for k, (label, name) in enumerate(specs):
        pid = g.add_player(label)
        pid_label[pid] = label
        bots[pid] = get_bot(name, seed=seed * 131 + k)
    g.start()
    think = defaultdict(float)
    think_max = defaultdict(float)
    neg = defaultdict(float)
    neg_max = defaultdict(float)
    neg_calls = defaultdict(int)
    errors = defaultdict(int)
    orders_n = defaultdict(int)
    bot_errors = {}
    frames = []
    trade = {pid: dict.fromkeys(TRADE_KEYS, 0) for pid in bots}
    kinds: Counter = Counter()
    fog_events: Counter = Counter()
    while not g.finished:
        alive = g.alive_players()
        for rnd in range(max(0, int(rounds))):
            k = (g.turn + rnd) % len(alive) if alive else 0
            for pid in alive[k:] + alive[:k]:
                view = g.player_view(pid)
                t0 = time.perf_counter()
                try:
                    actions = bots[pid].negotiate(view)
                except Exception as e:  # a bot must never raise
                    bot_errors.setdefault(pid_label[pid], f"negotiate: {type(e).__name__}: {e}")
                    actions = []
                dt = time.perf_counter() - t0
                lab = pid_label[pid]
                neg[lab] += dt
                neg_max[lab] = max(neg_max[lab], dt)
                neg_calls[lab] += 1
                if actions:
                    if not isinstance(actions, list):
                        actions = [actions]
                    _count_actions(trade[pid], actions, g.diplomacy(pid, actions))
        for pid in g.alive_players():
            view = g.player_view(pid)
            t0 = time.perf_counter()
            try:
                orders = bots[pid].act(view)
            except Exception as e:  # a bot must never raise; count it and move on
                bot_errors.setdefault(pid_label[pid], f"{type(e).__name__}: {e}")
                orders = []
            dt = time.perf_counter() - t0
            lab = pid_label[pid]
            think[lab] += dt
            think_max[lab] = max(think_max[lab], dt)
            errs = g.submit_orders(pid, orders)
            errors[lab] += len(errs)
            orders_n[lab] += len(orders) if isinstance(orders, list) else 0
        step_events = g.step()
        _count_events(g, trade, kinds, step_events)
        if fog:
            fog_events.update(e["type"] for e in step_events
                              if e["type"] in ("battle", "spy_report", "spy_incident", "counterintel"))
        if record_views:
            frames.append(g.spectator_view(full=True))  # offline: omniscient
    res = g.result or {}
    turns = res.get("turn", g.turn) + 1
    lab = pid_label
    slot_of = start_slots(g)
    out = {
        "seed": seed,
        "seats": [{"pid": pid, "label": lab[pid], "bot": name, "slot": slot_of.get(pid)}
                  for pid, (_, name) in zip(pid_label, specs)],
        "winner": lab.get(res.get("winner")),
        "condition": res.get("condition"),
        "turns": turns,
        "placements": [lab[p] for p in res.get("placements", [])],
        "scores": {lab[p]: s for p, s in (res.get("scores") or {}).items()},
        "prevalidation_errors": dict(errors),
        "orders": dict(orders_n),
        "think_seconds": {k: round(v, 4) for k, v in think.items()},
        "think_ms_per_turn": {k: round(1000 * v / turns, 3) for k, v in think.items()},
        "think_ms_max": {k: round(1000 * v, 2) for k, v in think_max.items()},
        "negotiate_ms_per_turn": {k: round(1000 * v / turns, 3) for k, v in neg.items()},
        "negotiate_ms_per_call": {k: round(1000 * v / max(1, neg_calls[k]), 3) for k, v in neg.items()},
        "negotiate_ms_max": {k: round(1000 * v, 2) for k, v in neg_max.items()},
        "trade": {lab[pid]: dict(t, net_value=round(t["net_value"], 1), betrayals=g.player(pid).betrayals,
                                 contracts_honoured=g.player(pid).contracts_honoured)
                  for pid, t in trade.items()},
        "deal_kinds": dict(kinds),
        "deals_executed": len(g.deal_log),
        "bot_exceptions": bot_errors,
        "last_errors": {lab[pid]: b.last_error for pid, b in bots.items() if getattr(b, "last_error", None)},
        "seconds": round(time.perf_counter() - t_start, 3),
    }
    if record_views:
        out["frames"] = frames
    if fog:
        out["fog"] = True
        out["fog_events"] = dict(fog_events)
    return out


def start_slots(g: Game) -> dict:
    """pid -> index of the player's start position in the (seed-independent)
    start layout, to measure positional fairness."""
    return dict(getattr(g, "start_slots", {}) or {})


def _run_one(args: tuple) -> dict:
    specs, seed, max_turns, index = args[:4]
    rounds = args[4] if len(args) > 4 else NEGOTIATION_ROUNDS
    fog = bool(args[5]) if len(args) > 5 else False
    r = run_game(specs, seed, max_turns, game_id=f"tour{index}", rounds=rounds, fog=fog)
    r["index"] = index
    return r


def schedule(bots: list, games: int, players: int, seed: int) -> list:
    """List of (specs, game_seed) per game with seeded, rotated seats."""
    names = [str(b) for b in bots]
    if len(names) < players:
        names = [names[k % len(names)] for k in range(players)]
    labels = label_bots(names)
    pool = [(lab, name.split("#")[0]) for lab, name in zip(labels, names)]
    rng = random.Random(seed)
    out = []
    for gi in range(games):
        chosen = rng.sample(pool, players) if len(pool) > players else list(pool)
        rng.shuffle(chosen)
        # rotate so that over consecutive games every bot visits every seat
        k = gi % players
        chosen = chosen[k:] + chosen[:k]
        out.append((chosen, seed * 10007 + gi))
    return out


def run_tournament(bots: list, games: int = 40, players: int | None = None, seed: int = 1,
                   max_turns: int = C.DEFAULT_MAX_TURNS, jobs: int = 1, progress=None,
                   rounds: int = NEGOTIATION_ROUNDS, fog: bool = False) -> dict:
    """Run a tournament and return the summary dict (see module doc)."""
    players = players or len(bots)
    plan = schedule(bots, games, players, seed)
    tasks = [(specs, gseed, max_turns, gi, rounds, fog) for gi, (specs, gseed) in enumerate(plan)]
    results = []
    t0 = time.perf_counter()
    if jobs and jobs > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            for r in ex.map(_run_one, tasks):
                results.append(r)
                if progress:
                    progress(r)
    else:
        for t in tasks:
            r = _run_one(t)
            results.append(r)
            if progress:
                progress(r)
    results.sort(key=lambda r: r["index"])
    return summarize(results, time.perf_counter() - t0)


def summarize(results: list, wall_seconds: float = 0.0) -> dict:
    table: dict = {}
    per = defaultdict(lambda: {"games": 0, "wins": 0, "place_sum": 0, "conditions": Counter(),
                               "errors": 0, "orders": 0, "think_ms": 0.0, "think_ms_max": 0.0,
                               "score_sum": 0, "exceptions": 0, "neg_ms": 0.0, "neg_call_ms": 0.0,
                               "neg_ms_max": 0.0, "trade": Counter()})
    kinds: Counter = Counter()
    deals_total = 0
    conds: Counter = Counter()
    lengths = []
    seconds = []
    seat_stats: dict = defaultdict(lambda: [0, 0, 0])      # pid -> [games, wins, place_sum]
    slot_stats: dict = defaultdict(lambda: [0, 0, 0])      # start slot -> [games, wins, place_sum]
    for r in results:
        place_of = {lab: k for k, lab in enumerate(r["placements"], start=1)}
        for seat in r.get("seats", []):
            k = place_of.get(seat["label"])
            if k is None:
                continue
            for key, table_ in ((seat["pid"], seat_stats), (seat.get("slot"), slot_stats)):
                if key is None:
                    continue
                row = table_[key]
                row[0] += 1
                row[1] += k == 1
                row[2] += k
        places = r["placements"]
        if len(places) >= 2:
            ratings.update(table, places)
        conds[r["condition"]] += 1
        lengths.append(r["turns"])
        seconds.append(r["seconds"])
        for k, lab in enumerate(places, start=1):
            s = per[lab]
            s["games"] += 1
            s["place_sum"] += k
            s["score_sum"] += r["scores"].get(lab, 0)
            if k == 1:
                s["wins"] += 1
                s["conditions"][r["condition"]] += 1
            s["errors"] += r["prevalidation_errors"].get(lab, 0)
            s["orders"] += r["orders"].get(lab, 0)
            s["think_ms"] += r["think_ms_per_turn"].get(lab, 0.0)
            s["think_ms_max"] = max(s["think_ms_max"], r["think_ms_max"].get(lab, 0.0))
            if lab in r.get("bot_exceptions", {}):
                s["exceptions"] += 1
            s["neg_ms"] += r.get("negotiate_ms_per_turn", {}).get(lab, 0.0)
            s["neg_call_ms"] += r.get("negotiate_ms_per_call", {}).get(lab, 0.0)
            s["neg_ms_max"] = max(s["neg_ms_max"], r.get("negotiate_ms_max", {}).get(lab, 0.0))
            s["trade"].update(r.get("trade", {}).get(lab, {}))
        kinds.update(r.get("deal_kinds", {}))
        deals_total += r.get("deals_executed", 0)
    rating_rows = {row["name"]: row for row in ratings.leaderboard(table)}
    bots = []
    for lab, s in per.items():
        g = s["games"]
        row = rating_rows.get(lab, {})
        bots.append({
            "bot": lab,
            "games": g,
            "wins": s["wins"],
            "win_rate": round(s["wins"] / g, 3) if g else 0.0,
            "avg_place": round(s["place_sum"] / g, 2) if g else None,
            "avg_score": round(s["score_sum"] / g, 1) if g else None,
            "rating": row.get("rating"),
            "mu": row.get("mu"),
            "sigma": row.get("sigma"),
            "win_conditions": dict(s["conditions"]),
            "prevalidation_errors_per_game": round(s["errors"] / g, 2) if g else 0,
            "orders_per_game": round(s["orders"] / g, 1) if g else 0,
            "think_ms_per_turn": round(s["think_ms"] / g, 2) if g else 0,
            "think_ms_max": round(s["think_ms_max"], 1),
            "negotiate_ms_per_turn": round(s["neg_ms"] / g, 2) if g else 0,
            "negotiate_ms_per_call": round(s["neg_call_ms"] / g, 3) if g else 0,
            "negotiate_ms_max": round(s["neg_ms_max"], 1),
            "exceptions": s["exceptions"],
            "trade_per_game": {k: round(s["trade"].get(k, 0) / g, 2) if g else 0
                               for k in TRADE_KEYS + ("betrayals", "contracts_honoured")},
        })
    bots.sort(key=lambda b: (-(b["rating"] if b["rating"] is not None else -1e9), b["avg_place"] or 99))
    n = len(results)
    srt = sorted(lengths)
    median = (srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2) if n else 0

    def seat_rows(tab, key_fn):
        return [{"seat": k, "games": v[0], "wins": v[1],
                 "win_rate": round(v[1] / v[0], 3) if v[0] else 0.0,
                 "avg_place": round(v[2] / v[0], 2) if v[0] else None}
                for k, v in sorted(tab.items(), key=lambda kv: key_fn(kv[0]))]
    fog_total: Counter = Counter()
    for r in results:
        fog_total.update(r.get("fog_events") or {})
    extra = {}
    if any(r.get("fog") for r in results):
        extra["fog_events_per_game"] = {k: round(v / n, 2) for k, v in sorted(fog_total.items())}
    return {
        **extra,
        "games": n,
        "bots": bots,
        "conditions": {c: conds.get(c, 0) for c in CONDITIONS if conds.get(c)},
        "avg_turns": round(sum(lengths) / n, 1) if n else 0,
        "median_turns": median,
        "seats": seat_rows(seat_stats, lambda pid: int(str(pid)[1:]) if str(pid)[1:].isdigit() else 0),
        "start_slots": seat_rows(slot_stats, int),
        "min_turns": min(lengths) if lengths else 0,
        "max_turns": max(lengths) if lengths else 0,
        "avg_game_seconds": round(sum(seconds) / n, 3) if n else 0,
        "max_game_seconds": round(max(seconds), 3) if seconds else 0,
        "wall_seconds": round(wall_seconds, 2),
        "deals_per_game": round(deals_total / n, 2) if n else 0,
        "deal_kinds_per_game": {k: round(v / n, 2) for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])} if n else {},
        "results": [{k: v for k, v in r.items() if k != "frames"} for r in results],
    }


def format_summary(s: dict) -> str:
    lines = []
    hdr = f"{'bot':<16}{'games':>6}{'wins':>6}{'win%':>7}{'place':>7}{'score':>7}{'rating':>8}{'err/g':>7}{'ms/turn':>9}  wins by condition"
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for b in s["bots"]:
        wc = ", ".join(f"{k}:{v}" for k, v in sorted(b["win_conditions"].items(), key=lambda kv: -kv[1]))
        lines.append(f"{b['bot']:<16}{b['games']:>6}{b['wins']:>6}{100 * b['win_rate']:>6.1f}%"
                     f"{b['avg_place']:>7.2f}{b['avg_score']:>7.0f}{(b['rating'] or 0):>8.2f}"
                     f"{b['prevalidation_errors_per_game']:>7.1f}{b['think_ms_per_turn']:>9.2f}  {wc}")
    lines.append("")
    total = s["games"] or 1
    lines.append("ending conditions: " + ", ".join(f"{c} {k} ({100 * k / total:.0f}%)"
                                                for c, k in sorted(s["conditions"].items(), key=lambda kv: -kv[1])))
    lines.append(f"game length: median {s.get('median_turns')}, avg {s['avg_turns']} turns (min {s['min_turns']}, max {s['max_turns']}); "
                 f"time per game: avg {s['avg_game_seconds']} s, max {s['max_game_seconds']} s; "
                 f"wall {s['wall_seconds']} s for {s['games']} games")
    if s.get("start_slots"):
        lines.append("win% by start slot: " + ", ".join(
            f"{r['seat']}:{100 * r['win_rate']:.0f}%" for r in s["start_slots"])
            + " | by seat: " + ", ".join(f"{r['seat']}:{100 * r['win_rate']:.0f}%" for r in s["seats"]))
    if s.get("deals_per_game") or any(b.get("trade_per_game", {}).get("proposed") for b in s["bots"]):
        lines.append("")
        lines.append(f"deals per game: {s.get('deals_per_game', 0)}"
                     + (" (" + ", ".join(f"{k} {v}" for k, v in s.get("deal_kinds_per_game", {}).items()) + ")"
                        if s.get("deal_kinds_per_game") else ""))
        th = (f"{'per game':<16}{'prop':>6}{'ctr':>6}{'acc':>6}{'deals':>7}{'peace':>7}{'c-pay':>7}{'c-get':>7}"
              f"{'dflt':>6}{'gold out':>9}{'gold in':>9}{'net val':>9}{'neg ms':>8}")
        lines.append(th)
        for b in s["bots"]:
            t = b.get("trade_per_game", {})
            lines.append(f"{b['bot']:<16}{t.get('proposed', 0):>6.1f}{t.get('countered', 0):>6.1f}"
                         f"{t.get('accepted', 0):>6.1f}{t.get('deals', 0):>7.1f}{t.get('peace_deals', 0):>7.2f}"
                         f"{t.get('contracts_payer', 0):>7.2f}{t.get('contracts_payee', 0):>7.2f}"
                         f"{t.get('defaults', 0):>6.2f}{t.get('gold_paid', 0):>9.0f}{t.get('gold_received', 0):>9.0f}"
                         f"{t.get('net_value', 0):>9.0f}{b.get('negotiate_ms_per_call', 0):>8.2f}")
    if "fog_events_per_game" in s:
        lines.append("fog games; per game: " + (", ".join(f"{k} {v}" for k, v in s["fog_events_per_game"].items())
                                                or "no battles or espionage"))
    exc = [b["bot"] for b in s["bots"] if b["exceptions"]]
    if exc:
        lines.append("bots that raised: " + ", ".join(exc))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="AgentCiv in-process bot tournament")
    ap.add_argument("--bots", default="strategist,economist,rusher,turtle,random,random",
                    help="comma-separated bot names (see agentciv.bots.BOT_NAMES)")
    ap.add_argument("--games", type=int, default=20)
    ap.add_argument("--players", type=int, default=None, help="seats per game (default: number of bots)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-turns", type=int, default=C.DEFAULT_MAX_TURNS)
    ap.add_argument("--jobs", type=int, default=1, help="parallel worker processes")
    ap.add_argument("--rounds", type=int, default=NEGOTIATION_ROUNDS,
                    help="negotiation rounds per turn before the bots act (0 = no barter)")
    ap.add_argument("--fog", action="store_true", help="fog-of-war games (rules §14)")
    ap.add_argument("--json", default=None, help="write the full summary (incl. per-game results) here")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    bots = [b.strip() for b in a.bots.split(",") if b.strip()]
    players = a.players or len(bots)
    if not 1 <= players <= C.MAX_PLAYERS:
        ap.error(f"--players must be 1..{C.MAX_PLAYERS}")

    def progress(r):
        if not a.quiet:
            print(f"game {r['index'] + 1:>3}: {r['winner']:<14} by {r['condition']:<9} turn {r['turns']:>3}"
                  f"  ({r['seconds']:.1f}s)  order: {' > '.join(r['placements'])}", file=sys.stderr)

    s = run_tournament(bots, a.games, players, a.seed, a.max_turns, a.jobs, progress, rounds=a.rounds, fog=a.fog)
    print(format_summary(s))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(s, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
