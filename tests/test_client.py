"""SDK tests: text helpers on engine views, run_bot, and the CLI."""
from __future__ import annotations

import json
import threading

import pytest

from agentciv.bots.base import IdleBot
from agentciv.client import AgentCivClient, ApiError, ascii_map, main, run_bot, summarize_view
from agentciv.engine.testing import new_game
from agentciv.server import create_server


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    yield srv
    srv.stop()


def test_summarize_view_and_ascii_map_on_engine_views():
    g = new_game(6, seed=3)
    for _ in range(3):
        g.step()
    view = g.player_view("p2")
    text = summarize_view(view)
    assert "YOU: p2" in text and "Resources" in text and "Victory thresholds" in text
    for p in g.players:
        assert p.id in text
    spec = summarize_view(g.spectator_view())  # no "you": still works
    assert "Players" in spec and "YOU:" not in spec
    m = ascii_map(view)
    rows = m.splitlines()
    assert len(rows) == 2 + g.height + 2
    assert "@2" in m  # p2's capital
    assert "(you)" in m
    json.dumps(view)  # untouched by the helpers


def test_summary_mentions_threats_and_proposals():
    g = new_game(2, seed=5)
    cap = g.player("p1").capital
    x, y = g.xy(cap)
    g.place_units(x + 1 if x + 1 < g.width else x - 1, y, "p2", {"cavalry": 2})
    g.treaty_proposals.append({"from": "p2", "to": "p1", "turns": 20, "turn": g.turn - 1})
    text = summarize_view(g.player_view("p1"))
    assert "Threats near your cities:" in text and "p2 at" in text
    assert 'accept_treaty' in text


def test_ascii_map_lobby():
    g = new_game(2, start=False)
    assert "not started" in ascii_map(g.spectator_view())


def test_client_errors(server):
    c = AgentCivClient(server.url)
    with pytest.raises(ApiError) as ei:
        c.join("g404", "A")
    assert ei.value.status == 404
    with pytest.raises(ValueError):
        c.state()  # no game joined
    assert "idle" in c.bots()
    assert "units" in c.rules_json()
    assert c.rules().startswith("#")


def test_run_bot_with_bot_instance_and_callable(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=3, max_turns=5, bots=["idle"], turn_timeout=0.05, turn_delay=0)
    out = {}
    t = threading.Thread(target=lambda: out.update(a=run_bot(IdleBot(), server.url, game_id=gid, name="InstanceBot")))
    t.start()
    out["b"] = run_bot(lambda view: [{"type": "message", "to": "all", "text": "hi"}], server.url,
                       game_id=gid, name="CallableBot")
    t.join(30)
    assert out["a"]["result"] == out["b"]["result"]
    assert out["a"]["result"]["turn"] == 4
    assert {out["a"]["place"], out["b"]["place"]} <= {1, 2, 3}
    msgs = c.state(gid)["messages"]
    assert any(m["text"] == "hi" for m in msgs)


def test_run_bot_quickmatch_with_builtin_name(server):
    c = AgentCivClient(server.url)
    c.quickmatch("QM", players=2, lobby_timeout=0.1, max_turns=3, turn_timeout=0.05)
    res = run_bot("idle", server.url, client=c)
    assert res["result"]["turn"] == 2 and res["name"] == "idle-remote"


def test_run_bot_requires_a_game(server):
    with pytest.raises(ValueError):
        run_bot("idle", server.url)


def test_cli_runs_a_builtin_bot(server, capsys):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, max_turns=3, bots=["idle"], turn_timeout=0.05, turn_delay=0)
    assert main(["--url", server.url, "--bot", "idle", "--name", "CliBot", "--game", gid, "--quiet"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["game_id"] == gid and res["name"] == "CliBot" and res["result"]["turn"] == 2
    assert main(["--url", server.url, "--bot", "idle", "--game", "g999", "--quiet"]) == 1
