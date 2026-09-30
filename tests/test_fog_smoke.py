"""Fog-of-war variants of the bot, tournament and warfare smoke tests
(rules §14): the built-in bots play fogged views without internal errors,
and fighting still happens when armies are only partly visible."""
from __future__ import annotations

import json
import random

import pytest

from agentciv import tournament as T
from agentciv.bots import BOT_NAMES, get_bot
from agentciv.bots.fogfill import fill
from agentciv.engine import Game, GameConfig
from agentciv.engine.testing import new_game

from test_engine_fuzz import aggressive_orders, plausible_orders

BUILTIN = [b for b in BOT_NAMES if b != "idle"]


def play_fog(bot_names, seed=1, turns=60):
    g = Game(GameConfig(seed=seed, max_turns=150, fog=True))
    bots = {}
    for k, name in enumerate(bot_names):
        bots[g.add_player(f"{name}{k}")] = get_bot(name, seed=seed * 100 + k)
    g.start()
    stats = {pid: {"orders": 0, "errors": []} for pid in bots}
    while not g.finished and g.turn < turns:
        for pid in g.alive_players():
            view = g.player_view(pid)
            for _ in range(2):
                actions = bots[pid].negotiate(view)
                if actions:
                    g.diplomacy(pid, actions)
                view = g.player_view(pid)
            orders = bots[pid].act(view)
            assert isinstance(orders, list)
            stats[pid]["orders"] += len(orders)
            stats[pid]["errors"].extend(e["error"] for e in g.submit_orders(pid, orders))
        g.step()
    return g, bots, stats


@pytest.mark.parametrize("name", BUILTIN)
def test_bot_survives_fog_game(name):
    field = [name, "economist", "rusher", "turtle", "random", "strategist"]
    g, bots, stats, = play_fog(field, seed=3, turns=60)
    for pid, b in bots.items():
        assert getattr(b, "last_error", None) is None, (pid, b.name, b.last_error)
    st = stats["p1"]
    assert len(st["errors"]) <= max(3, st["orders"] // 40), st["errors"][:5]


def test_fogfill_is_identity_without_active_fog():
    g = new_game(3)
    v = g.player_view("p1")
    assert fill(v) is v
    g = new_game(3, fog=True)
    v = g.player_view("p1")
    f = fill(v)
    assert f is not v and v["players"][1]["resources"] is None      # the original is not modified
    rival = f["players"][1]
    gold_income = v["players"][1]["income"]["gold"]
    assert rival["estimated"] and rival["units"]["infantry"] == 0
    assert rival["resources"]["gold"] == min(1000, 5 * gold_income) > 0     # bank/legacy are public; gold is guessed
    assert isinstance(rival["score"], int) and rival["military_power"] == 0


def test_tournament_fog_smoke():
    r = T.run_game(["strategist", "economist", "rusher", "turtle", "random", "random"], seed=3, max_turns=20,
                   fog=True)
    assert r["fog"] is True and not r["bot_exceptions"] and r["turns"] <= 20
    assert sum(r["prevalidation_errors"].values()) <= 5
    json.dumps(r)
    s = T.run_tournament(["rusher", "random", "turtle"], games=2, players=3, seed=2, max_turns=10, fog=True)
    assert "fog_events_per_game" in s and "fog games" in T.format_summary(s)
    assert "fog_events_per_game" not in T.run_tournament(["random", "random"], games=1, players=2, max_turns=3)


def test_fog_warfare_battles_still_happen():
    seen = set()
    for seed in (11, 12):
        g = Game(GameConfig(seed=seed, max_turns=120, fog=True))
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
    assert {"battle", "tile_battle", "city_captured", "tile_captured", "eliminated"} <= seen, seen
