"""Robustness (garbage orders), determinism and performance."""
import json
import random
import time

from agentciv.engine import Game, GameConfig
from agentciv.engine import constants as C
from agentciv.engine.orders import ORDER_TYPES

KEYS = ["type", "from", "to", "path", "units", "city", "unit", "count", "at", "building", "side",
        "resource", "qty", "limit", "give", "want", "offer_id", "turns", "with", "text", "id", "x", "y"]


def garbage(rng, depth=0):
    k = rng.randrange(12 if depth < 3 else 8)
    if k == 0:
        return None
    if k == 1:
        return rng.choice([True, False])
    if k == 2:
        return rng.randint(-5, 40)
    if k == 3:
        return rng.choice([10 ** 12, -10 ** 12, 1.5, float("nan"), float("inf"), -0.0, 3.0])
    if k == 4:
        return rng.choice(["", "all", "p1", "p2", "p99", "t1", "infantry", "farm", "buy", "x" * 600, "3"])
    if k == 5:
        return [rng.randint(-2, 30), rng.randint(-2, 30)]
    if k == 6:
        return rng.choice(list(C.UNITS) + list(C.IMPROVEMENTS) + list(C.CITY_BUILDINGS) + list(C.TRADABLE))
    if k == 7:
        return rng.choice(ORDER_TYPES)
    if k in (8, 9):
        return [garbage(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {rng.choice(KEYS + list(C.UNITS) + list(C.TRADABLE)): garbage(rng, depth + 1)
            for _ in range(rng.randrange(5))}


def garbage_order(rng):
    o = {"type": rng.choice(ORDER_TYPES + ("junk",))}
    for _ in range(rng.randrange(6)):
        o[rng.choice(KEYS)] = garbage(rng)
    return o if rng.random() < 0.95 else garbage(rng)


def plausible_orders(view, rng, n=20):
    """Semi-sensible random orders built from the view (exercise resolution)."""
    me = view["you"]["id"]
    w, h = view["map"]["width"], view["map"]["height"]
    owner = view["map"]["owner"]
    mine = [(x, y) for y in range(h) for x in range(w) if owner[y][x] == me]
    cities = [(c["x"], c["y"]) for c in view["cities"] if c["owner"] == me]
    armies = [a for a in view["armies"] if a["owner"] == me]
    others = [p["id"] for p in view["players"] if p["id"] != me and p["alive"]]
    out = []
    for _ in range(n):
        k = rng.randrange(12)
        if k <= 2 and armies:
            a = rng.choice(armies)
            dx, dy = rng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
            units = {u: rng.randint(1, c) for u, c in a["units"].items() if rng.random() < 0.7} or None
            path = [[a["x"] + dx, a["y"] + dy]]
            if units and set(units) == {"cavalry"} and rng.random() < 0.5:
                path.append([a["x"] + 2 * dx, a["y"] + 2 * dy])
            out.append({"type": "move", "from": [a["x"], a["y"]], "path": path, "units": units})
        elif k == 3 and mine:
            x, y = rng.choice(mine)
            dx, dy = rng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
            out.append({"type": "claim", "at": [x + dx, y + dy]})
        elif k == 4 and cities:
            out.append({"type": "recruit", "city": list(rng.choice(cities)),
                        "unit": rng.choice(C.UNIT_TYPES), "count": rng.randint(1, 3)})
        elif k == 5 and mine:
            out.append({"type": "build", "at": list(rng.choice(mine)),
                        "building": rng.choice(list(C.IMPROVEMENTS) + list(C.CITY_BUILDINGS))})
        elif k == 6 and mine:
            x, y = rng.choice(mine)
            out.append({"type": "settle", "at": [x + rng.randint(-3, 3), y + rng.randint(-3, 3)]})
        elif k == 7:
            out.append({"type": "market", "side": rng.choice(["buy", "sell"]),
                        "resource": rng.choice(C.MARKET_RESOURCES), "qty": rng.randint(1, 60),
                        "limit": rng.choice([None, 0.5, 1.5, 3.0])})
        elif k == 8 and others:
            out.append({"type": "offer_trade", "to": rng.choice(others),
                        "give": {rng.choice(C.TRADABLE): rng.randint(1, 40)},
                        "want": {rng.choice(C.TRADABLE): rng.randint(1, 40)}})
        elif k == 9:
            for o in view["trade_offers"]:
                if o["to"] == me:
                    out.append({"type": "accept_trade", "offer_id": o["id"]})
            for pr in view["treaty_proposals"]:
                if pr["to"] == me:
                    out.append({"type": "accept_treaty", "from": pr["from"]})
        elif k == 10 and others:
            if rng.random() < 0.7:
                out.append({"type": "propose_treaty", "to": rng.choice(others), "turns": rng.randint(10, 20)})
            else:
                for t in view["treaties"]:
                    if me in (t["a"], t["b"]):
                        out.append({"type": "break_treaty", "with": t["b"] if t["a"] == me else t["a"]})
                        break
        elif k == 11 and armies:
            a = rng.choice(armies)
            out.append({"type": "disband", "at": [a["x"], a["y"]], "units": {next(iter(a["units"])): 1}})
        else:
            out.append({"type": "message", "to": rng.choice(others + ["all"]) if others else "all", "text": "hi"})
    return out


def aggressive_orders(view, rng):
    """Recruit and march every army toward the nearest enemy city (exercises combat)."""
    me = view["you"]["id"]
    out = []
    enemy = [(c["x"], c["y"]) for c in view["cities"] if c["owner"] != me]
    for c in view["cities"]:
        if c["owner"] == me:
            out.append({"type": "recruit", "city": [c["x"], c["y"]],
                        "unit": rng.choice(C.UNIT_TYPES), "count": rng.randint(1, 2)})
    if not enemy:
        return out
    terrain = view["map"]["terrain"]
    for a in view["armies"]:
        if a["owner"] != me:
            continue
        tx, ty = min(enemy, key=lambda e: abs(e[0] - a["x"]) + abs(e[1] - a["y"]))
        steps = []
        if tx != a["x"]:
            steps.append((a["x"] + (1 if tx > a["x"] else -1), a["y"]))
        if ty != a["y"]:
            steps.append((a["x"], a["y"] + (1 if ty > a["y"] else -1)))
        steps = [s for s in steps if terrain[s[1]][s[0]] in "._fhg"] or steps
        if steps:
            out.append({"type": "move", "from": [a["x"], a["y"]], "to": list(rng.choice(steps))})
    return out


def play(seed, n_players, turns, garbage_ratio=0.0, orders_per_turn=20, aggressive=0):
    g = Game(GameConfig(seed=seed, max_turns=turns, game_id=f"f{seed}"))
    for i in range(n_players):
        g.add_player(f"Bot{i}")
    g.start()
    rng = random.Random(seed)
    trace = []
    while not g.finished:
        for pid in g.alive_players():
            view = g.player_view(pid)
            orders = plausible_orders(view, rng, orders_per_turn)
            if g.player(pid).index < aggressive:
                orders = aggressive_orders(view, rng) + orders[: orders_per_turn // 4]
            if garbage_ratio:
                orders += [garbage_order(rng) for _ in range(int(orders_per_turn * garbage_ratio))]
                rng.shuffle(orders)
            errs = g.submit_orders(pid, orders)
            assert isinstance(errs, list)
        g.step()
        trace.append(json.dumps(g.spectator_view(), sort_keys=True))
    return g, trace


def test_fuzz_garbage_orders_never_crash():
    rng = random.Random(1234)
    g = Game(GameConfig(seed=9, max_turns=60))
    for i in range(6):
        g.add_player(f"G{i}")
    g.start()
    while not g.finished:
        for pid in g.alive_players():
            orders = [garbage_order(rng) for _ in range(rng.randrange(30))]
            errs = g.submit_orders(pid, orders)
            assert all(isinstance(e["index"], int) and isinstance(e["error"], str) for e in errs)
            g.submit_orders(pid, garbage(rng))
            g.submit_orders(pid, orders)
            json.dumps(g.player_view(pid))
        g.step()
    json.dumps(g.spectator_view())


def test_fuzz_mixed_orders_full_games():
    for seed in (1, 2, 3):
        g, _ = play(seed, n_players=5 + seed, turns=80, garbage_ratio=0.3, orders_per_turn=15, aggressive=2)
        assert g.finished and g.result is not None
        assert sorted(g.result["placements"]) == sorted(p.id for p in g.players)


def test_fuzz_warfare():
    """Aggressive players march on their neighbours: battles, clashes, captures."""
    seen = set()
    for seed in (11, 12):
        g = Game(GameConfig(seed=seed, max_turns=120))
        for i in range(6):
            g.add_player(f"W{i}")
        g.start()
        rng = random.Random(seed)
        while not g.finished:
            for pid in g.alive_players():
                view = g.player_view(pid)
                g.submit_orders(pid, aggressive_orders(view, rng) + plausible_orders(view, rng, 3))
            for e in g.step():
                seen.add(e["type"])
                if e["type"] == "battle":
                    seen.add("clash" if e["clash"] else "tile_battle")
            json.dumps(g.spectator_view())
        assert g.finished
    assert {"battle", "clash", "tile_battle", "city_captured", "tile_captured", "eliminated"} <= seen, seen


def test_determinism():
    _, t1 = play(77, n_players=6, turns=40, aggressive=3)
    _, t2 = play(77, n_players=6, turns=40, aggressive=3)
    assert t1 == t2
    _, t3 = play(78, n_players=6, turns=40, aggressive=3)
    assert t1 != t3


def test_performance_8_players_150_turns():
    """8 players x 150 turns x ~20 orders/turn must run well under 10 s
    (engine time only: orders are generated up front from the views)."""
    g = Game(GameConfig(seed=5, max_turns=150))
    for i in range(8):
        g.add_player(f"P{i}")
    g.start()
    rng = random.Random(5)
    engine_time = 0.0
    while not g.finished:
        batch = {}
        for pid in g.alive_players():
            t0 = time.perf_counter()
            view = g.player_view(pid)
            engine_time += time.perf_counter() - t0
            batch[pid] = plausible_orders(view, rng, 20)
        t0 = time.perf_counter()
        for pid, orders in batch.items():
            g.submit_orders(pid, orders)
        g.step()
        g.spectator_view()
        engine_time += time.perf_counter() - t0
    assert g.turn == 150 or g.finished
    assert engine_time < 10.0, engine_time
