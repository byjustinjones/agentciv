"""Client-side rendering of fog-of-war views (rules §14): text summary, ASCII
map, event lines, MCP schemas, deal helpers and the barter example bot."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from agentciv.bots import get_bot
from agentciv.client import ascii_map, describe_event, order_warnings, summarize_view
from agentciv.engine import constants as C
from agentciv.engine import deals as D
from agentciv.engine import fog as F
from agentciv.engine.testing import new_game, run_turn
from agentciv.mcp_server import ORDER_HELP, TOOLS

ROOT = Path(__file__).resolve().parent.parent


def fog_view():
    """p1's view after a few turns with a sighting, a report and counter-intelligence."""
    g = new_game(4, seed=2, fog=True)
    for p in g.players:
        p.resources["gold"] = 1000
    cap2 = g.player("p2").capital
    x1, y1 = g.xy(g.player("p1").capital)
    g.place_units(x1 + 1, y1, "p1", {"cavalry": 1})
    g.place_units(x1 + 3, y1, "p3", {"infantry": 2})       # seen by the cavalry
    run_turn(g, {"p1": [{"type": "spy", "target": "p2", "mission": "military", "invest": 200},
                        {"type": "spy", "target": "p3", "mission": "treasury", "invest": 200},
                        {"type": "counterintel", "invest": 40},
                        {"type": "move", "from": [x1 + 1, y1], "to": [x1, y1]}]})
    assert g.idx(x1 + 3, y1) not in F.vision(g, "p1") and cap2 is not None
    return g, g.player_view("p1")


def test_summary_of_a_fog_view():
    g, v = fog_view()
    s = summarize_view(v)
    assert "None" not in s
    assert "Fog of war: other players' resources, units, military_power, upkeep and score are hidden" in s
    assert "p2 P2: ? |" in s and "? ? ? ? ?" in s and f"bank 0/{C.BANK_VICTORY} streak 0/10" in s
    assert "bank, legacy and victory progress are shown" in s
    assert "Armies last seen (not in sight now):" in s and "p3 at [" in s
    assert "Intel reports:" in s and "p2 military (success" in s and "p3 treasury (success" in s
    assert "Your counter-intelligence: pool 30, rating" in s
    assert "Relics (units are listed on tiles in your sight):" in s
    assert "units not in sight" in s
    assert "spy incidents 0" in s
    std = summarize_view(new_game(3).player_view("p1"))
    assert "Fog of war" not in std and "spy incidents" not in std and "Relics (units standing" in std


def test_ascii_map_marks_tiles_out_of_sight():
    g, v = fog_view()
    lines = ascii_map(v).splitlines()
    w = v["map"]["width"]
    assert len(lines[2]) == 4 + 3 * w
    row = lines[2 + 0][4:]
    assert all(row[3 * x + 2] in " ?" for x in range(w))
    assert "?" in ascii_map(v) and "3rd char: ? = not in your sight" in lines[-1]
    std = ascii_map(new_game(3).player_view("p1")).splitlines()
    assert len(std[2]) == 4 + 2 * new_game(3).width and "3rd char" not in std[-1]


def test_describe_espionage_events():
    assert describe_event({"type": "spy_report", "player": "p1", "target": "p2", "mission": "treasury",
                           "invest": 40, "outcome": "detected"}, "p1") == \
        "your treasury mission against p2 (40 gold): detected"
    assert "p1 ran a military mission against you: failed (no report)" == describe_event(
        {"type": "spy_detected", "player": "p2", "spy": "p1", "mission": "military", "outcome": "failed"}, "p2")
    assert "failed (public incident)" in describe_event({"type": "spy_incident", "spy": "p1", "target": "p2"})
    assert describe_event({"type": "counterintel", "invest": 30, "pool": 50}) == \
        "counter-intelligence +30 gold (pool 50)"
    ev = describe_event({"type": "deal_executed", "deal": "d1", "from": "p1", "to": "p2", "peace": None})
    assert "terms shown only to the parties" in ev and "nothing" not in ev
    assert "penalty" not in describe_event({"type": "contract_default", "contract": "c1", "payer": "p1",
                                            "payee": "p2"})


def test_order_warnings_for_espionage_gold():
    g = new_game(3, fog=True)
    v = g.player_view("p1")
    w = order_warnings(v, [{"type": "spy", "target": "p2", "mission": "military", "invest": 500}])
    assert any("spy/counterintel orders invest 500 gold" in x for x in w)
    assert not order_warnings(v, [{"type": "counterintel", "invest": 10}])


def test_mcp_and_order_help_mention_fog():
    tools = {t["name"]: t for t in TOOLS}
    for name in ("create_game", "quickmatch"):
        assert tools[name]["inputSchema"]["properties"]["fog"]["type"] == "boolean"
    assert tools["leaderboard"]["inputSchema"]["properties"]["mode"]["enum"] == ["standard", "fog"]
    assert '"type":"spy"' in ORDER_HELP and '"type":"counterintel"' in ORDER_HELP


def test_view_delivery_problem_with_hidden_stock():
    g = new_game(3, fog=True)
    v = g.player_view("p1")
    assert D.view_delivery_problem(v, "p2", {"gold": 10 ** 5}) is None       # unknown, not short
    assert "p1 is short of" in D.view_delivery_problem(v, "p1", {"gold": 10 ** 5})
    assert "not owned by p2" in D.view_delivery_problem(v, "p2", {"tiles": [[0, 0]]})


def _barter_bot():
    spec = importlib.util.spec_from_file_location("barter_bot", ROOT / "examples" / "barter_bot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BarterBot


def test_barter_bot_survives_a_fog_game():
    BarterBot = _barter_bot()
    g = new_game(4, seed=3, max_turns=15, fog=True)
    bots = {p.id: (BarterBot(seed=k) if k == 0 else get_bot("strategist", seed=k)) for k, p in enumerate(g.players)}
    while not g.finished:
        for pid in g.alive_players():
            acts = bots[pid].negotiate(g.player_view(pid))
            if acts:
                g.diplomacy(pid, acts)
            g.submit_orders(pid, bots[pid].act(g.player_view(pid)))
        g.step()
    assert g.finished
