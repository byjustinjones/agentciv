"""Regression tests for the server review findings: private-information leaks
through the spectator endpoints, leaderboard farming/impersonation, resource
exhaustion (connections, games, memory), quickmatch griefing and HTTP-level
robustness."""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from agentciv import ratings
from agentciv.client import AgentCivClient, ApiError
from agentciv.engine import constants as C
from agentciv.server import create_server
from agentciv.server import app as server_app
from agentciv.server.manager import GameSession
from agentciv.server.storage import Storage

from test_server import FAST, call, wait_until


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    yield srv
    srv.stop()


def raw(srv, data: bytes, timeout: float = 5.0) -> bytes:
    """Send raw bytes, return everything the server sends until it closes (or the timeout)."""
    s = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=timeout)
    s.sendall(data)
    out = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    except socket.timeout:
        pass
    finally:
        s.close()
    return out


# ---------------------------------------------------------------- private diplomacy
def _secret_turn(server, max_turns=3):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=3, turn_timeout=0, max_turns=max_turns)
    a, b, x = AgentCivClient(server.url), AgentCivClient(server.url), AgentCivClient(server.url)
    a.join(gid, "Alice")
    b.join(gid, "Bob")
    x.join(gid, "Carol")
    assert wait_until(lambda: a.state()["status"] == "running")
    res = a.submit_orders([
        {"type": "message", "to": "p2", "text": "secret: let's gang up on Carol"},
        {"type": "message", "to": "all", "text": "hello everyone"},
        {"type": "offer_trade", "to": "p2", "give": {"gold": 1}, "want": {}},
        {"type": "propose_treaty", "to": "p2", "turns": 20},
        {"type": "claim", "at": [-5, -5]},  # fails: a private order_failed event
    ], turn=0)
    assert len(res["errors"]) == 1, res
    b.submit_orders([], turn=0)
    x.submit_orders([], turn=0)
    assert wait_until(lambda: a.state()["turn"] == 1)
    return gid, a, b, x


def _private_bits(view):
    return {
        "private_msgs": [m for m in view["messages"] if m["to"] != "all"],
        "offers": view["trade_offers"],
        "proposals": view["treaty_proposals"],
        "private_events": [e for e in view["events"]
                           if e["type"] in ("order_failed", "treaty_proposed", "trade_offered", "trade_executed")],
    }


def test_live_spectator_endpoints_hide_private_diplomacy(server):
    gid, a, b, x = _secret_turn(server)
    bob = _private_bits(b.state())
    assert bob["private_msgs"] and bob["offers"] and bob["proposals"]  # the recipient sees them
    assert not any(_private_bits(x.state()).values())                 # a third player doesn't
    spec = AgentCivClient(server.url).state(gid, spectator=True)
    assert not any(_private_bits(spec).values()), _private_bits(spec)
    assert [m["text"] for m in spec["messages"]] == ["hello everyone"]  # public ones still show
    # the SSE stream and the replay (full, ranged and compact) of the running game are public too
    s = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
    s.sendall(f"GET /api/games/{gid}/stream HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    buf = b""
    while b"event: state\n" not in buf or b"\n\n" not in buf.split(b"event: state\n", 1)[1]:
        buf += s.recv(65536)
    s.close()
    frame = json.loads(buf.split(b"event: state\ndata: ", 1)[1].split(b"\n\n", 1)[0])
    assert frame["turn"] == 1 and not any(_private_bits(frame).values())
    for q in ("", "?compact=1", "?from=1&to=1"):
        st, rep = call(server, "GET", f"/api/games/{gid}/replay{q}")
        assert st == 200
        for f in rep["frames"]:
            assert not any(_private_bits(f).values()), (q, f["turn"])
    blob = call(server, "GET", f"/api/games/{gid}/replay")[1]
    assert "secret" not in json.dumps(blob)
    # once the game is over, the replay reveals everything
    end = time.time() + 20
    while a.game()["status"] != "finished" and time.time() < end:
        for c in (a, b, x):
            try:
                c.submit_orders([])
            except ApiError:
                pass
        a.wait(timeout=1)
    assert a.game()["status"] == "finished"
    assert wait_until(lambda: "secret" in json.dumps(call(server, "GET", f"/api/games/{gid}/replay")[1]))
    frames = call(server, "GET", f"/api/games/{gid}/replay")[1]["frames"]
    assert _private_bits(frames[1])["private_msgs"] and _private_bits(frames[1])["offers"]


# ---------------------------------------------------------------- ratings
def test_rating_eligibility_and_farming(server):
    s, g = call(server, "POST", "/api/games", {"max_players": 5, "bots": ["idle"] * 5, "max_turns": 1})
    assert s == 200 and g["rated"] is False and "max_turns" in g["unrated_reason"]
    for body, why in [({"seed": 7}, "seed"), ({"turn_timeout": 0}, "turn_timeout"),
                      ({"bots": ["idle"]}, "idle"), ({"bots": ["random"]}, "random"),
                      ({"rated": False}, "rated")]:
        s, g = call(server, "POST", "/api/games", {"max_players": 3, **body})
        assert g["rated"] is False and why in g["unrated_reason"], (body, g)
        assert call(server, "GET", f"/api/games/{g['game_id']}")[1]["rated"] is False
    s, g = call(server, "POST", "/api/games", {"max_players": 3, "bots": ["strategist"]})
    assert g["rated"] is True and g["unrated_reason"] is None
    s, q = call(server, "POST", "/api/quickmatch", {"name": "Q", "max_turns": 1, "lobby_timeout": 60})
    assert call(server, "GET", f"/api/games/{q['game_id']}")[1]["rated"] is False
    # the farm from the report: many one-turn games against idle bots -> no leaderboard entries
    c = AgentCivClient(server.url)
    for _ in range(3):
        gid = c.create_game(max_players=3, bots=["idle", "idle"], max_turns=1, **FAST)
        AgentCivClient(server.url).join(gid, "Farmer")
        assert c.wait(since_turn=99, timeout=10, game_id=gid)["status"] == "finished"
    time.sleep(0.2)
    assert c.leaderboard() == []


def test_score_ties_are_rated_as_ties_not_by_seat_order():
    table: dict = {}
    ratings.update(table, ["p_a", "p_b", "p_c"], [1, 1, 3])
    assert table["p_a"]["mu"] == pytest.approx(table["p_b"]["mu"])
    assert table["p_a"]["wins"] == table["p_b"]["wins"] == 1 and table["p_c"]["wins"] == 0
    assert table["p_c"]["mu"] < table["p_a"]["mu"]


def test_equal_scores_share_a_rank_in_a_finished_game(server):
    server.manager.open_ratings = True
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, max_turns=2, **FAST)
    AgentCivClient(server.url).join(gid, "FamousAgent")
    AgentCivClient(server.url).join(gid, "Mallory")
    assert c.wait(since_turn=99, timeout=10, game_id=gid)["status"] == "finished"
    res = c.game(gid)["result"]
    assert wait_until(lambda: len(c.leaderboard()) == 2)
    board = {r["name"]: r for r in c.leaderboard()}
    if res["condition"] == "score" and len(set(res["scores"].values())) == 1:
        assert board["FamousAgent"]["mu"] == board["Mallory"]["mu"]  # not "p1 wins every tie"


def test_registered_names_cannot_be_impersonated(server, tmp_path):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=4, turn_timeout=5)
    AgentCivClient(server.url).join(gid, "FamousAgent", key="correct horse battery")
    for key in (None, "wrong key 123"):
        with pytest.raises(ApiError) as e:
            AgentCivClient(server.url).join(c.create_game(max_players=4, turn_timeout=5), "famousagent", key=key)
        assert e.value.status == 403
    with pytest.raises(ApiError) as e:
        AgentCivClient(server.url).quickmatch("FamousAgent", lobby_timeout=60)
    assert e.value.status == 403
    AgentCivClient(server.url).join(c.create_game(max_players=4, turn_timeout=5), "FamousAgent",
                                    key="correct horse battery")
    assert call(server, "POST", f"/api/games/{gid}/join", {"name": "Z", "key": "short"})[0] == 400
    st = Storage(tmp_path / "data")  # persisted, and leaderboard rows carry "verified"
    assert st.is_registered("famousagent") and not st.is_registered("Mallory")
    st.record_result(["FamousAgent", "Mallory"])
    assert {r["name"]: r["verified"] for r in st.leaderboard()} == {"FamousAgent": True, "Mallory": False}


def test_house_bot_seeds_are_not_derived_from_the_public_seed(server):
    seeds = []
    for _ in range(2):
        gid = AgentCivClient(server.url).create_game(max_players=3, bots=["strategist"] * 2, seed=42,
                                                     turn_timeout=5)
        s = server.manager.get(gid)
        seeds.append([seat.bot.seed for seat in s.seats.values()])
    assert seeds[0] != seeds[1]
    assert (42 * 7919 + 17) % 2 ** 31 not in seeds[0]  # the old, predictable derivation


# ---------------------------------------------------------------- resources
def test_connection_cap_and_idle_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(server_app.Handler, "timeout", 0.5)
    srv = server_app.AgentCivServer(("127.0.0.1", 0), server_app.GameManager(str(tmp_path / "d")),
                                    max_connections=4).start_background()
    try:
        port = srv.server_address[1]
        idle = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(4)]
        for s in idle:  # slowloris: headers promise a body that never comes
            s.sendall(b"POST /api/games HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n{")
        time.sleep(0.2)
        assert b" 503 " in raw(srv, b"GET /api/bots HTTP/1.1\r\nHost: x\r\n\r\n").split(b"\r\n", 1)[0]
        for s in idle:  # the idle connections are dropped after the socket timeout...
            s.settimeout(5)
            assert s.recv(100) == b""
            s.close()
        time.sleep(0.2)  # ...which frees their slots
        assert b" 200 " in raw(srv, b"GET /api/bots HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    finally:
        srv.stop()


def test_lobby_caps_reaping_and_finished_eviction(server):
    mgr = server.manager
    mgr.max_open_lobbies = 3
    ids = [call(server, "POST", "/api/games", {"max_players": 4, "turn_timeout": 5})[1]["game_id"]
           for _ in range(3)]
    s, err = call(server, "POST", "/api/games", {"max_players": 4})
    assert s == 503 and "lobbies" in err["error"]
    s, err = call(server, "POST", "/api/quickmatch", {"name": "Q"})
    assert s == 503
    threads = [mgr.get(g)._thread for g in ids]
    mgr.lobby_max_age = 0.0
    call(server, "GET", "/api/games")  # sweeps
    assert all(call(server, "GET", f"/api/games/{g}")[0] == 404 for g in ids)
    assert wait_until(lambda: not any(t.is_alive() for t in threads), timeout=5)
    mgr.lobby_max_age = 3600
    # finished games are dropped from memory once saved, but keep working from the replay file
    mgr.finished_keep = 0
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, turn_timeout=0.05, max_turns=3)
    p = AgentCivClient(server.url)
    p.join(gid, "P")
    AgentCivClient(server.url).join(gid, "Q")
    assert p.wait(since_turn=99, timeout=10)["status"] == "finished"
    assert wait_until(lambda: gid not in mgr.sessions)
    assert p.state()["status"] == "finished"               # the token still works for reading
    with pytest.raises(ApiError) as e:
        p.submit_orders([])
    assert e.value.status == 409
    assert c.game(gid)["status"] == "finished" and len(c.replay(gid)["frames"]) == 4


def test_unknown_game_lookup_does_not_copy_the_archive_index(server, monkeypatch):
    monkeypatch.setattr(server.manager.storage, "archived", lambda: pytest.fail("copied the index"))
    assert call(server, "GET", "/api/games/nope/state")[0] == 404
    for _ in range(3):
        call(server, "POST", "/api/games", {"max_players": 2, "turn_timeout": 5})
    s, games = call(server, "GET", "/api/games?limit=1")
    assert s == 200 and len(games) == 3  # every live game is always listed; limit caps archived ones


# ---------------------------------------------------------------- quickmatch
def test_quickmatch_does_not_hold_the_manager_lock_while_starting(server, monkeypatch):
    held = []
    orig = GameSession._start

    def spy(self, *a, **kw):
        held.append(server.manager.lock._is_owned())
        return orig(self, *a, **kw)

    monkeypatch.setattr(GameSession, "_start", spy)
    a = AgentCivClient(server.url).quickmatch("A", players=2, lobby_timeout=60)
    b = AgentCivClient(server.url).quickmatch("B", players=2, lobby_timeout=60)
    assert a["game_id"] == b["game_id"] and held == [False]


def test_quickmatch_buckets_include_lobby_settings(server):
    grief = AgentCivClient(server.url).quickmatch("griefer", lobby_timeout=86400)
    nofill = AgentCivClient(server.url).quickmatch("nofill", fill_with_bots=False)
    victim = AgentCivClient(server.url).quickmatch("victim")
    assert len({grief["game_id"], nofill["game_id"], victim["game_id"]}) == 3
    assert AgentCivClient(server.url).game(victim["game_id"])["lobby_timeout"] == 30
    # lobby_timeout 0 = start right away (with bots), never "wait forever"
    now = AgentCivClient(server.url)
    res = now.quickmatch("now", players=3, lobby_timeout=0)
    assert wait_until(lambda: now.game(res["game_id"])["status"] == "running", timeout=10)
    assert [p["is_bot"] for p in now.game(res["game_id"])["players"]] == [False, True, True]


def test_quickmatch_lobby_cannot_be_started_by_a_stranger(server):
    a = AgentCivClient(server.url)
    a.quickmatch("A", players=4, lobby_timeout=60)
    AgentCivClient(server.url).quickmatch("B", players=4, lobby_timeout=60)
    s, err = call(server, "POST", f"/api/games/{a.game_id}/start")
    assert s == 403
    assert a.start()["started"] is True  # a seated player may


# ---------------------------------------------------------------- HTTP robustness
def test_bad_numbers_and_deep_json_are_400(server):
    gid = AgentCivClient(server.url).create_game(max_players=2, turn_timeout=5)
    assert call(server, "GET", f"/api/games/{gid}/wait?since_turn=1e400&timeout=0")[0] == 400
    assert call(server, "GET", f"/api/games/{gid}/replay?from=1e400")[0] == 400
    assert call(server, "POST", "/api/games", raw_body=b'{"seed": ' + b"9" * 400 + b"}")[0] == 400
    assert call(server, "POST", "/api/games", raw_body=b"[" * 200000)[0] == 400
    p = AgentCivClient(server.url)
    p.join(gid, "A")
    AgentCivClient(server.url).join(gid, "B")
    s, err = call(server, "POST", f"/api/games/{gid}/orders", raw_body=b'{"turn": 1' + b"0" * 400 + b', "orders": []}',
                  token=p.token)
    assert s == 400


def test_order_with_non_string_type_gets_errors_not_500(server):
    gid = AgentCivClient(server.url).create_game(max_players=2, turn_timeout=5)
    p = AgentCivClient(server.url)
    p.join(gid, "A")
    AgentCivClient(server.url).join(gid, "B")
    for t in (["move"], {"x": 1}, 5):
        res = p.submit_orders([{"type": t}, {"type": "claim", "at": [0, 0]}], turn=0)
        assert res["errors"][0]["index"] == 0 and "hint" in res["errors"][0]


def test_protocol_errors_get_a_json_response(server):
    out = raw(server, b"GET / HTTX/1.1\r\nHost: x\r\n\r\n")
    assert out.startswith(b"HTTP/1.1 400") and b'"error"' in out
    out = raw(server, b"GET /" + b"a" * 70000 + b" HTTP/1.1\r\n\r\n")
    assert b" 414" in out.split(b"\r\n", 1)[0] and b'"error"' in out


def test_get_with_a_body_cannot_smuggle_a_second_request(server):
    inner = b"GET /api/bots HTTP/1.1\r\nHost: x\r\n\r\n"
    out = raw(server, b"GET /api/games HTTP/1.1\r\nHost: x\r\nContent-Length: " + str(len(inner)).encode()
              + b"\r\n\r\n" + inner)
    assert out.count(b"HTTP/1.1 200") == 1


def test_head_stream_and_wait_do_not_block(server):
    gid = AgentCivClient(server.url).create_game(max_players=2, turn_timeout=5)
    t0 = time.time()
    out = raw(server, f"HEAD /api/games/{gid}/stream HTTP/1.1\r\nHost: x\r\n\r\n".encode(), timeout=5)
    assert out.startswith(b"HTTP/1.1 200") and out.endswith(b"\r\n\r\n") and b"event:" not in out
    out = raw(server, f"HEAD /api/games/{gid}/wait?timeout=30 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
              .encode(), timeout=5)
    assert out.startswith(b"HTTP/1.1 200") and out.endswith(b"\r\n\r\n")
    assert time.time() - t0 < 4


def test_many_lobbies_threads_exit_after_close(server):
    """Each lobby holds a worker thread; closed lobbies must release it."""
    before = threading.active_count()
    mgr = server.manager
    ids = [AgentCivClient(server.url).create_game(max_players=3, turn_timeout=5) for _ in range(10)]
    assert threading.active_count() >= before + 10
    mgr.lobby_max_age = 0.0
    mgr.list_games()
    assert wait_until(lambda: threading.active_count() <= before + 1, timeout=5)
    assert all(g not in mgr.sessions for g in ids)
    assert C.MAX_PLAYERS >= 3
