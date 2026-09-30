"""View shapes and JSON-serialisability; rules_json."""
import json

from agentciv.engine import Game, GameConfig, rules_json
from agentciv.engine import constants as C
from agentciv.engine.testing import new_game, run_turn

TOP_KEYS = {"game_id", "turn", "max_turns", "status", "deadline", "season", "you", "players", "map",
            "cities", "armies", "market", "treaties", "treaty_proposals", "trade_offers", "messages",
            "events", "victory", "costs", "deals", "contracts", "diplomacy_seq"}
PLAYER_KEYS = {"id", "name", "color", "alive", "eliminated_turn", "resources", "income", "cities", "tiles",
               "capitals_held", "military_power", "units", "wonder_stage", "relics_held", "relics_guarded",
               "relic_streak", "bank", "legacy", "economic_streak", "influence_streak",
               "betrayals", "reputation", "score", "submitted", "victory_progress"}
YOU_KEYS = {"id", "name", "resources", "caps", "income", "upkeep", "claim_cost", "settle_cost", "submitted",
            "bank_limit"}


def test_player_view_shape():
    g = new_game(5, seed=3)
    v = g.player_view("p2")
    json.dumps(v)
    assert TOP_KEYS <= set(v)
    assert v["game_id"] == "test3" and v["turn"] == 0 and v["status"] == "running"
    assert YOU_KEYS <= set(v["you"]) and v["you"]["id"] == "p2"
    assert v["you"]["claim_cost"] == 3
    assert v["season"] == {"name": "spring", "index": 0, "turns_left": 6,
                           "modifiers": {"food": 1.0, "wood": 1.0, "stone": 1.0, "gold": 1.0}, "next": "summer"}
    assert len(v["players"]) == 5 and all(PLAYER_KEYS <= set(p) for p in v["players"])
    m = v["map"]
    assert m["width"] == m["height"] == 22
    assert len(m["terrain"]) == 22 and all(isinstance(r, str) and len(r) == 22 for r in m["terrain"])
    assert len(m["owner"]) == 22 and all(isinstance(r, list) and len(r) == 22 for r in m["owner"])
    assert set("".join(m["terrain"])) <= set(C.TERRAIN)
    cap = v["cities"][[c["owner"] for c in v["cities"]].index("p2")]
    assert m["owner"][cap["y"]][cap["x"]] == "p2"
    assert cap["capital"] and cap["garrison"] == C.GARRISON_CAPITAL
    assert cap["buildings"] == {"walls": 0, "warehouse": 0, "market_hall": 0}
    assert {"x", "y", "owner", "units"} == set(v["armies"][0])
    assert len(m["relics"]) == 5 and {"x", "y", "owner", "guarded"} == set(m["relics"][0])
    assert all({"x", "y", "resource", "remaining"} == set(d) for d in m["deposits"])
    assert set(v["market"]["prices"]) == {"food", "wood", "stone"}
    assert v["market"]["prices"] == {"food": 1.0, "wood": 1.5, "stone": 2.0}
    assert v["market"]["pools"]["food"] == {"resource": 2000.0, "gold": 2000.0}
    assert v["victory"]["result"] is None
    assert "units" in v["costs"] and "buildings" in v["costs"]


def test_spectator_view_and_submitted_flags():
    g = new_game(3)
    g.submit_orders("p1", [])
    v = g.spectator_view()
    json.dumps(v)
    assert v["you"] is None
    assert [p["submitted"] for p in v["players"]] == [True, False, False]
    assert g.player_view("p1")["you"]["submitted"] is True


def test_events_last_turn_only_and_private_filtering():
    g = new_game(3)
    cap = g.player_view("p1")["you"]["capital"]
    run_turn(g, {"p1": [{"type": "recruit", "city": cap, "unit": "infantry", "count": 50}]})
    ev1 = g.player_view("p1")["events"]
    assert any(e["type"] == "order_failed" for e in ev1)
    assert not any(e["type"] == "order_failed" for e in g.player_view("p2")["events"])
    assert not any(e["type"] == "order_failed" for e in g.spectator_view()["events"])  # private while running
    assert any(e["type"] == "order_failed" for e in g.spectator_view(full=True)["events"])
    run_turn(g)
    assert not any(e["type"] == "order_failed" for e in g.player_view("p1")["events"])
    assert all(e["turn"] == 1 for e in g.spectator_view()["events"])


def test_views_are_independent_copies():
    g = new_game(2)
    v = g.player_view("p1")
    v["map"]["owner"][0][0] = "hacked"
    v["you"]["resources"]["gold"] = 10 ** 6
    v["costs"]["units"]["infantry"]["strength"] = 999
    v2 = g.player_view("p1")
    assert v2["map"]["owner"][0][0] != "hacked"
    assert v2["you"]["resources"]["gold"] == 50
    assert v2["costs"]["units"]["infantry"]["strength"] == 10


def test_lobby_view():
    g = Game(GameConfig(game_id="lobby"))
    g.add_player("A")
    v = g.spectator_view()
    json.dumps(v)
    assert v["status"] == "lobby" and v["map"]["terrain"] == []
    json.dumps(g.player_view("p1"))


def test_finished_view_has_result():
    g = new_game(2, max_turns=1)
    g.step()
    v = g.spectator_view()
    json.dumps(v)
    assert v["status"] == "finished" and v["victory"]["result"]["condition"] == "score"
    assert set(v["victory"]["result"]) == {"winner", "condition", "turn", "placements", "scores"}


def test_rules_json():
    r = rules_json()
    json.dumps(r)
    assert r["units"]["cavalry"]["move"] == 2
    assert r["buildings"]["city"]["walls"]["costs"][1] == {"stone": 80, "wood": 40}
    assert r["buildings"]["improvements"]["farm"]["cost"] == {"wood": 20, "gold": 10}
    assert r["market"]["fee"] == C.MARKET_FEE
    assert Game.rules() == r


def test_add_player_rules():
    g = Game(GameConfig())
    assert [g.add_player(f"n{i}") for i in range(3)] == ["p1", "p2", "p3"]
    g.start()
    try:
        g.add_player("late")
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def test_rules_md_in_sync_with_constants():
    """docs/RULES.md is generated; regenerate with `python -m agentciv.engine.rulesdoc`."""
    import os

    from agentciv.engine import rulesdoc
    path = rulesdoc.default_path()
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        assert f.read() == rulesdoc.render(), "docs/RULES.md is stale: run python -m agentciv.engine.rulesdoc"
