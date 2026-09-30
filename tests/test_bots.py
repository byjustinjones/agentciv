"""Built-in bots: robustness, order validity, determinism, speed, and the
shared helpers in agentciv.bots.common."""
from __future__ import annotations

import time

import pytest

from agentciv.bots import BOT_NAMES, get_bot
from agentciv.bots.common import (Plan, World, best_counter, simulate_attack,
                                  threat_to)
from agentciv.engine import Game, GameConfig
from agentciv.engine import constants as C
from agentciv.engine.testing import new_game, run_turn, sandbox

BUILTIN = [b for b in BOT_NAMES if b != "idle"]


def play(bot_names, seed=1, turns=60):
    """Play up to ``turns`` turns; return (game, bots, stats)."""
    g = Game(GameConfig(seed=seed, max_turns=150))
    bots = {}
    for k, name in enumerate(bot_names):
        pid = g.add_player(f"{name}{k}")
        bots[pid] = get_bot(name, seed=seed * 100 + k)
    g.start()
    stats = {pid: {"orders": 0, "errors": [], "think": 0.0, "turns": 0} for pid in bots}
    history = []
    while not g.finished and g.turn < turns:
        for pid in g.alive_players():
            view = g.player_view(pid)
            t0 = time.perf_counter()
            orders = bots[pid].act(view)
            stats[pid]["think"] += time.perf_counter() - t0
            stats[pid]["turns"] += 1
            assert isinstance(orders, list)
            for o in orders:
                assert isinstance(o, dict) and isinstance(o.get("type"), str)
            stats[pid]["orders"] += len(orders)
            errs = g.submit_orders(pid, orders)
            stats[pid]["errors"].extend(e["error"] for e in errs)
            history.append((g.turn, pid, orders))
        g.step()
    return g, bots, stats, history


@pytest.mark.parametrize("name", BUILTIN)
def test_bot_survives_and_emits_valid_orders(name):
    """Every bot plays a full mixed game segment without internal errors and
    with (almost) no pre-validation errors."""
    field = [name, "economist", "rusher", "turtle", "random", "strategist"]
    for seed in (1, 2):
        g, bots, stats, _ = play(field, seed=seed, turns=60)
        pid = "p1"
        assert bots[pid].last_error is None, bots[pid].last_error
        st = stats[pid]
        assert len(st["errors"]) <= max(2, st["orders"] // 50), st["errors"][:5]
        for p, b in bots.items():
            assert getattr(b, "last_error", None) is None, (p, b.name, b.last_error)


def test_all_bots_long_game_no_exceptions():
    g, bots, stats, _ = play(["strategist", "economist", "rusher", "turtle", "random", "random"], seed=5, turns=150)
    for pid, b in bots.items():
        assert b.last_error is None, (b.name, b.last_error)
    total_orders = sum(s["orders"] for s in stats.values())
    total_errors = sum(len(s["errors"]) for s in stats.values())
    assert total_errors <= max(5, total_orders // 100)


@pytest.mark.parametrize("name", BUILTIN)
def test_bot_deterministic(name):
    field = [name, "economist", "turtle", "rusher", "random"]
    _, _, _, h1 = play(field, seed=7, turns=25)
    _, _, _, h2 = play(field, seed=7, turns=25)
    assert h1 == h2


@pytest.mark.parametrize("name", BUILTIN)
def test_bot_is_fast(name):
    field = [name, "economist", "rusher", "turtle", "random", "strategist"]
    _, _, stats, _ = play(field, seed=3, turns=40)
    st = stats["p1"]
    per_turn_ms = 1000 * st["think"] / max(1, st["turns"])
    assert per_turn_ms < 40, per_turn_ms   # typical is well under 20 ms


@pytest.mark.parametrize("name", BUILTIN)
def test_bot_handles_odd_views(name):
    bot = get_bot(name, seed=1)
    assert bot.act({}) == []
    assert bot.act({"you": None}) == []
    g = new_game(3, seed=2)
    v = g.player_view("p1")
    v["you"]["alive"] = False
    assert bot.act(v) == []
    # a finished game
    v = g.player_view("p1")
    v["status"] = "finished"
    assert bot.act(v) == []
    # garbage inside a valid-looking view must not raise
    v = g.player_view("p1")
    v["armies"] = [{"x": 0, "y": 0, "owner": "p9", "units": {"infantry": 1}}]
    v["players"].append({"id": "p9", "alive": True})
    out = bot.act(v)
    assert isinstance(out, list)


def test_registry_names():
    for name in ("random", "economist", "rusher", "turtle", "strategist", "idle"):
        assert name in BOT_NAMES
        assert get_bot(name).act is not None
    with pytest.raises(ValueError):
        get_bot("nope")


# ---------------------------------------------------------------------------
# common helpers
# ---------------------------------------------------------------------------
def test_simulate_attack_matches_engine():
    """The bots' battle simulation reproduces the engine's result."""
    cases = [
        ({"infantry": 6}, {"archer": 2}, 0),
        ({"cavalry": 5}, {"archer": 4}, 1),
        ({"infantry": 8, "siege": 3}, {"infantry": 3}, 2),
        ({"infantry": 3}, {}, 0),
        ({"archer": 4}, {"infantry": 4}, 0),
    ]
    for att, dfn, walls in cases:
        g = sandbox(2, seed=1)
        g.add_city(5, 5, "p2", capital=True)
        g.add_city(12, 12, "p1", capital=True)
        city = g.cities[g.idx(5, 5)]
        city.walls = walls
        if dfn:
            g.place_units(5, 5, "p2", dfn)
        g.place_units(4, 5, "p1", att)
        w = World(g.player_view("p1"))
        win, surv, ratio = simulate_attack(w, "p1", att, w.idx(5, 5))
        run_turn(g, {"p1": [{"type": "move", "from": [4, 5], "to": [5, 5]}]})
        captured = g.cities[g.idx(5, 5)].owner == "p1"
        assert win == captured, (att, dfn, walls, win, captured)
        if captured:
            assert g.armies[g.idx(5, 5)]["p1"] == surv


def test_threat_and_counter():
    g = sandbox(2, seed=1)
    g.add_city(5, 5, "p1", capital=True)
    g.add_city(15, 15, "p2", capital=True)
    g.place_units(8, 5, "p2", {"cavalry": 4})
    g.place_units(5, 12, "p2", {"infantry": 4})
    w = World(g.player_view("p1"))
    th = threat_to(w, w.idx(5, 5), reach=2)
    assert th == {"p2": {"cavalry": 4}}          # infantry 7 steps away is too slow
    assert best_counter({"cavalry": 5}) == "infantry"
    assert best_counter({"infantry": 5}, allowed=("infantry", "archer")) == "archer"


def test_plan_budget_and_validation():
    g = new_game(2, seed=1)
    w = World(g.player_view("p1"))
    p = Plan(w)
    cap = w.capital
    # can't afford 50 cavalry: recruits as many as the budget allows
    n = p.recruit(cap, "cavalry", 50)
    assert 0 < n < 50
    assert p.budget["food"] >= 0 and p.budget["gold"] >= 0
    # claims must be adjacent and unowned
    far = w.idx(0, 0)
    assert not p.claim(far)
    errs = g.submit_orders("p1", p.orders)
    assert errs == []


def test_plan_never_claims_relics_and_bots_occupy_them():
    g = sandbox(2, seed=1)
    g.add_city(3, 3, "p1", capital=True)
    g.add_city(14, 14, "p2", capital=True)
    r = g.relics[0]
    rx, ry = r % g.width, r // g.width
    g.set_owner(rx - 1, ry, "p1")
    w = World(g.player_view("p1"))
    assert not Plan(w).claim(r)


def test_build_with_market_buys_stone_above_the_cap():
    from agentciv.bots.planner import PlannerBot
    from agentciv.engine.rules import building_cost
    g = sandbox(2, seed=1)
    g.add_city(3, 3, "p1", capital=True)
    g.add_city(14, 14, "p2", capital=True)
    city = g.cities[g.idx(3, 3)]
    city.wonder_stage = 2
    g.player("p1").wonder_city = city.idx
    cost = building_cost("wonder", 3)
    g.player("p1").resources.update(stone=C.STORAGE_BASE, wood=cost["wood"], gold=cost["gold"] + 3000)
    assert cost["stone"] > C.STORAGE_BASE
    bot = PlannerBot(seed=1)
    w = World(g.player_view("p1"))
    bot.w, bot.p = w, bot.new_plan(w)
    assert bot.build_with_market(w.idx(3, 3), "wonder")
    run_turn(g, {"p1": bot.p.orders})
    assert city.wonder_stage == 3


def test_contested_claim_backoff():
    """Two bots that keep claiming the same tile back off at random."""
    from agentciv.bots.planner import PlannerBot
    bot = PlannerBot(seed=3)
    g = sandbox(2, seed=1)
    g.add_city(3, 3, "p1", capital=True)
    g.add_city(14, 14, "p2", capital=True)
    g.player("p1").resources["influence"] = 50
    g.player("p2").resources["influence"] = 50
    g.set_owner(8, 7, "p1")
    g.set_owner(8, 9, "p2")
    claim = {"type": "claim", "at": [8, 8]}
    run_turn(g, {"p1": [claim], "p2": [claim]})
    view = g.player_view("p1")
    bot.memory["last_orders"] = (g.turn - 1, [claim])
    bot.act(view)
    assert bot.backed_off(g.idx(8, 8))


def test_world_follows_treaty_limits_and_the_break_notice():
    g = sandbox(4)
    for pid, xy in zip(("p1", "p2", "p3", "p4"), ((2, 2), (12, 2), (2, 12), (12, 12))):
        g.add_city(*xy, pid, capital=True)
    run_turn(g, {"p1": [{"type": "propose_treaty", "to": "p2", "turns": 20}]})
    run_turn(g, {"p2": [{"type": "accept_treaty", "from": "p1"}]})
    g.set_owner(7, 2, "p2")
    w = World(g.player_view("p1"))
    assert w.treaty_slots("p1") == 2 and w.treaties_held("p1") == 1 and w.sign_problem("p1", "p3") is None
    assert not w.can_enter_fn("p1")(g.idx(7, 2))
    g.player("p1").resources["influence"] = 100
    run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    w = World(g.player_view("p1"))
    # the turn after the break: still restricted, a cooldown, and a bond now required
    assert w.break_notice("p1", "p2") and not w.can_enter_fn("p1")(g.idx(7, 2))
    assert w.sign_problem("p1", "p2") == "cooldown" and w.sign_problem("p1", "p3") == "bond"
    assert w.break_influence() == 2 * C.TREATY_BREAK_COST
    run_turn(g)
    w = World(g.player_view("p1"))
    assert not w.break_notice("p1", "p2") and w.can_enter_fn("p1")(g.idx(7, 2))
    p = Plan(w)
    assert not p.propose("p2", 20) and not p.propose("p3", 20)     # cooldown; no bank for the bond
