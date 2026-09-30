"""Server plumbing of fog-of-war games (rules §14): the option, fogged live
spectator endpoints, full replays after the end, quickmatch buckets and the
separate leaderboard pool."""
from __future__ import annotations

import json
import socket

import pytest

from agentciv.client import AgentCivClient
from agentciv.server import create_server

from test_server import FAST, call, wait_until


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    yield srv
    srv.stop()


def _hidden(view: dict) -> bool:
    """No armies and no player's resources in ``view``."""
    return view["armies"] == [] and all(p["resources"] is None for p in view["players"])


def _stream_frame(server, gid) -> dict:
    s = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    s.sendall(f"GET /api/games/{gid}/stream HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    buf = b""
    while b"event: state\n" not in buf or b"\n\n" not in buf.split(b"event: state\n", 1)[1]:
        buf += s.recv(65536)
    s.close()
    return json.loads(buf.split(b"event: state\ndata: ", 1)[1].split(b"\n\n", 1)[0])


def test_fog_option_round_trips(server):
    s, g = call(server, "POST", "/api/games", {"max_players": 3, "fog": True})
    assert s == 200 and g["fog"] is True
    assert call(server, "GET", f"/api/games/{g['game_id']}")[1]["fog"] is True
    s, g = call(server, "POST", "/api/games", {"max_players": 3})
    assert g["fog"] is False
    assert call(server, "POST", "/api/games", {"max_players": 3, "fog": "yes"})[0] == 400
    assert all("fog" in x for x in call(server, "GET", "/api/games")[1])


def test_live_fog_game_hides_state_and_the_finished_replay_does_not(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=3, turn_timeout=0, max_turns=3, fog=True)
    a, b, x = AgentCivClient(server.url), AgentCivClient(server.url), AgentCivClient(server.url)
    a.join(gid, "Alice")
    b.join(gid, "Bob")
    x.join(gid, "Carol")
    assert wait_until(lambda: a.state()["status"] == "running")
    for p in (a, b, x):
        p.submit_orders([], turn=0)
    assert wait_until(lambda: a.state()["turn"] == 1)
    mine = a.state()
    assert mine["fog"]["active"] and mine["you"]["counterintel"]["rating"] > 0
    assert {r["id"] for r in mine["players"] if r["resources"] is None} == {"p2", "p3"}
    assert {q["owner"] for q in mine["armies"]} == {"p1"}          # capitals are far apart
    spec = AgentCivClient(server.url).state(gid, spectator=True)
    assert _hidden(spec) and spec["turn"] == 1
    frame = _stream_frame(server, gid)
    assert frame["turn"] == 1 and _hidden(frame)
    for q in ("", "?compact=1", "?from=1&to=1"):
        s, rep = call(server, "GET", f"/api/games/{gid}/replay{q}")
        assert s == 200 and rep["frames"] and all(_hidden(f) for f in rep["frames"]), q
    for t in (1, 2):
        for p in (a, b, x):
            p.submit_orders([], turn=t)
        assert c.wait(since_turn=t, timeout=10, game_id=gid)
    assert wait_until(lambda: c.game(gid)["status"] == "finished")
    spec = AgentCivClient(server.url).state(gid, spectator=True)
    assert spec["armies"] and all(p["resources"] is not None for p in spec["players"])
    assert not a.state()["fog"]["active"]
    for q in ("", "?compact=1", "?from=1&to=1"):
        s, rep = call(server, "GET", f"/api/games/{gid}/replay{q}")
        assert all(f["armies"] and f["players"][0]["resources"] is not None for f in rep["frames"]), q


def test_quickmatch_keeps_fog_and_standard_lobbies_apart(server):
    a = AgentCivClient(server.url).quickmatch("A", players=4, lobby_timeout=60, fog=True)
    b = AgentCivClient(server.url).quickmatch("B", players=4, lobby_timeout=60)
    c = AgentCivClient(server.url).quickmatch("C", players=4, lobby_timeout=60, fog=True)
    assert a["game_id"] == c["game_id"] != b["game_id"]
    assert call(server, "GET", f"/api/games/{a['game_id']}")[1]["fog"] is True
    assert call(server, "GET", f"/api/games/{b['game_id']}")[1]["fog"] is False


def test_fog_games_are_rated_in_their_own_pool(server, tmp_path):
    server.manager.open_ratings = True
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, max_turns=2, fog=True, **FAST)
    AgentCivClient(server.url).join(gid, "Fox")
    AgentCivClient(server.url).join(gid, "Hound")
    assert c.wait(since_turn=99, timeout=10, game_id=gid)["status"] == "finished"
    assert wait_until(lambda: len(c.leaderboard("fog")) == 2)
    assert {r["name"] for r in c.leaderboard("fog")} == {"Fox", "Hound"}
    assert c.leaderboard() == []
    data = tmp_path / "data"
    assert (data / "leaderboard_fog.json").exists() and not (data / "leaderboard.json").exists()
    assert call(server, "GET", "/api/leaderboard?mode=standard")[1] == []
    assert call(server, "GET", "/api/leaderboard?mode=chess")[0] == 400


def test_house_bots_finish_a_fog_game(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=5, max_turns=25, fog=True,
                        bots=["strategist", "rusher", "economist", "turtle", "random"], **FAST)
    assert c.wait(since_turn=99, timeout=60, game_id=gid)["status"] == "finished"
    session = server.manager.get(gid)
    assert all(s.bot_errors == 0 for s in session.seats.values())
    assert all(getattr(s.bot, "last_error", None) is None for s in session.seats.values() if s.bot)
