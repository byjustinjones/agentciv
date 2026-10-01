"""HTTP server tests: every §12 endpoint, error paths, timing, persistence.

The server runs in-process on an ephemeral port with a temporary data dir.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from agentciv.client import AgentCivClient, ApiError, run_bot
from agentciv.server import create_server
from agentciv.server.manager import bot_available

FAST = {"turn_timeout": 0.05, "turn_delay": 0}


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    yield srv
    srv.stop()


def call(srv, method, path, body=None, token=None, raw_body=None, headers=None):
    """Return (status, parsed JSON or text)."""
    data = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json", **(headers or {})}
    if token:
        h["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(srv.url + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, payload, ctype = r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        status, payload, ctype = e.code, e.read(), e.headers.get("Content-Type", "")
    if "json" in ctype:
        return status, json.loads(payload)
    return status, payload.decode()


def wait_until(pred, timeout=10.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def extra_bot():
    return "random" if bot_available("random") else "idle"


# ---------------------------------------------------------------- basics
def test_index_rules_bots_and_static(server):
    s, idx = call(server, "GET", "/api")
    assert s == 200 and idx["endpoints"]
    s, md = call(server, "GET", "/api/rules")
    assert s == 200 and "AgentCiv" in md
    s, rj = call(server, "GET", "/api/rules.json")
    assert s == 200 and "units" in rj
    s, bots = call(server, "GET", "/api/bots")
    assert s == 200 and "idle" in bots
    s, lb = call(server, "GET", "/api/leaderboard")
    assert s == 200 and lb == []
    s, games = call(server, "GET", "/api/games")
    assert s == 200 and games == []
    s, html = call(server, "GET", "/")
    assert s == 200 and "<html" in html.lower()


def test_cors_preflight(server):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request("OPTIONS", "/api/games")
    r = conn.getresponse()
    assert r.status == 204
    assert r.getheader("Access-Control-Allow-Origin") == "*"
    assert "Authorization" in r.getheader("Access-Control-Allow-Headers")
    conn.close()


@pytest.mark.parametrize("path", ["/../pyproject.toml", "/%2e%2e/pyproject.toml", "/..%2f..%2fetc/passwd",
                                  "/nonexistent.js"])
def test_static_path_traversal_blocked(server, path):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request("GET", path)
    r = conn.getresponse()
    body = r.read()
    assert r.status == 404
    assert b"[project]" not in body and b"root:" not in body
    conn.close()


def test_error_paths(server):
    assert call(server, "GET", "/api/nope")[0] == 404
    assert call(server, "GET", "/api/games/g999/state")[0] == 404
    assert call(server, "POST", "/api/games", raw_body=b"{not json")[0] == 400
    assert call(server, "POST", "/api/games", body=[1, 2])[0] == 400
    assert call(server, "POST", "/api/games", {"max_players": 99})[0] == 400
    assert call(server, "POST", "/api/games", {"max_players": "lots"})[0] == 400
    assert call(server, "POST", "/api/games", {"bots": ["nosuchbot"]})[0] == 400
    assert call(server, "POST", "/api/games", {"max_players": 2, "bots": ["idle"] * 3})[0] == 400
    assert call(server, "POST", "/api/games", {"min_players": 5, "max_players": 3})[0] == 400
    assert call(server, "POST", "/api/games", {"turn_timeout": -1})[0] == 400
    assert call(server, "POST", "/api/games", {"fill_with_bots": "yes"})[0] == 400
    s, err = call(server, "POST", "/api/quickmatch", {})
    assert s == 400 and "name" in err["error"]
    # the server is still alive
    assert call(server, "GET", "/api/games")[0] == 200


def test_join_validation_and_lobby(server):
    s, g = call(server, "POST", "/api/games", {"max_players": 3, "turn_timeout": 5})
    gid = g["game_id"]
    assert call(server, "POST", f"/api/games/{gid}/join", {"name": ""})[0] == 400
    assert call(server, "POST", f"/api/games/{gid}/join", {"name": "strategist"})[0] == 400  # reserved
    assert call(server, "POST", f"/api/games/{gid}/join", {"name": "Idle#2"})[0] == 400
    assert call(server, "POST", f"/api/games/{gid}/join", {"name": "x" * 41})[0] == 400
    s, a = call(server, "POST", f"/api/games/{gid}/join", {"name": "Alice"})
    assert s == 200 and a["player_id"] == "p1" and a["token"]
    assert call(server, "POST", f"/api/games/{gid}/join", {"name": "alice"})[0] == 409  # duplicate
    # lobby: orders rejected, state works, start needs min_players
    s, err = call(server, "POST", f"/api/games/{gid}/orders", {"orders": []}, token=a["token"])
    assert s == 409
    s, view = call(server, "GET", f"/api/games/{gid}/state", token=a["token"])
    assert s == 200 and view["status"] == "lobby" and view["you"]["id"] == "p1"
    # once a remote player is seated, starting needs a seated player's (or the creator's) token
    s, err = call(server, "POST", f"/api/games/{gid}/start")
    assert s == 403 and "token" in err["error"]
    assert call(server, "POST", f"/api/games/{gid}/start", token="bogus")[0] == 401
    s, err = call(server, "POST", f"/api/games/{gid}/start", token=a["token"])
    assert s == 409 and "at least" in err["error"]
    call(server, "POST", f"/api/games/{gid}/join", {"name": "Bob"})
    s, res = call(server, "POST", f"/api/games/{gid}/start", token=g["creator_token"])
    assert s == 200 and res["ok"] and res["started"]
    s, res = call(server, "POST", f"/api/games/{gid}/start")  # idempotent
    assert s == 200 and res["ok"] and not res["started"]
    s, err = call(server, "POST", f"/api/games/{gid}/join", {"name": "Carol"})
    assert s == 409
    s, summary = call(server, "GET", f"/api/games/{gid}")
    assert s == 200 and summary["status"] == "running" and len(summary["players"]) == 2


def test_auth_and_orders(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, turn_timeout=0)  # no deadline
    a = AgentCivClient(server.url)
    a.join(gid, "A")
    b = AgentCivClient(server.url)
    b.join(gid, "B")
    other = c.create_game(max_players=1)
    o = AgentCivClient(server.url)
    o.join(other, "O")

    assert call(server, "POST", f"/api/games/{gid}/orders", {"orders": []})[0] == 401
    assert call(server, "POST", f"/api/games/{gid}/orders", {"orders": []}, token="bogus")[0] == 401
    assert call(server, "POST", f"/api/games/{gid}/orders", {"orders": []}, token=o.token)[0] == 403
    assert call(server, "GET", f"/api/games/{gid}/state", token="bogus")[0] == 401
    assert call(server, "POST", f"/api/games/{gid}/orders", {"turn": 0}, token=a.token)[0] == 400
    assert call(server, "POST", f"/api/games/{gid}/orders", {"orders": {"x": 1}}, token=a.token)[0] == 400
    assert call(server, "POST", f"/api/games/{gid}/orders", {"turn": "x", "orders": []}, token=a.token)[0] == 400

    # token via query string also works; spectator view has you = null
    s, v = call(server, "GET", f"/api/games/{gid}/state?token={a.token}")
    assert s == 200 and v["you"]["id"] == a.player_id
    s, spec = call(server, "GET", f"/api/games/{gid}/state")
    assert s == 200 and spec["you"] is None and spec["deadline"] is None

    capital = v["you"]["capital"]
    res = a.submit_orders([{"type": "claim", "at": [0, 0]},
                           {"type": "recruit", "city": capital, "unit": "infantry"},
                           {"type": "bogus"}], turn=0)
    assert res["turn"] == 0 and res["accepted"] == 1 and len(res["errors"]) == 2
    assert {e["index"] for e in res["errors"]} == {0, 2}
    # resubmission replaces; the turn waits for B because turn_timeout=0
    a.submit_orders([], turn=0)
    time.sleep(0.3)
    assert a.state()["turn"] == 0
    w = a.wait(since_turn=0, timeout=0.2)
    assert w["timed_out"] and w["turn"] == 0
    b.submit_orders([])
    w = a.wait(since_turn=0, timeout=10)
    assert w["turn"] == 1 and not w["timed_out"]
    with pytest.raises(ApiError) as ei:
        a.submit_orders([], turn=0)
    assert ei.value.status == 409 and ei.value.body["turn"] == 1


def test_deadline_advances_without_submissions(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, turn_timeout=0.2)
    a, b = AgentCivClient(server.url), AgentCivClient(server.url)
    a.join(gid, "A")
    b.join(gid, "B")
    t0 = time.time()
    w = a.wait(since_turn=1, timeout=10)
    assert w["turn"] >= 2
    assert time.time() - t0 >= 0.3  # two deadlines of 0.2 s had to pass
    assert a.state()["deadline"] > time.time() - 1


def test_house_bot_exception_is_contained(server):
    class Boom:
        name = "boom"

        def act(self, view):
            raise RuntimeError("kaboom")

    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, bots=["idle"], max_turns=4, **FAST)
    server.manager.sessions[gid].seats["p1"].bot = Boom()
    res = run_bot(lambda view: [], server.url, game_id=gid, name="Solo")
    assert res["result"]["turn"] == 3 and res["place"] in (1, 2)


def test_bot_import_failure_falls_back_to_idle(server, monkeypatch):
    from agentciv.bots import REGISTRY
    monkeypatch.setitem(REGISTRY, "broken", "agentciv.bots.does_not_exist:Nope")
    s, g = call(server, "POST", "/api/games", {"max_players": 2, "bots": ["broken"], "turn_timeout": 1})
    assert s == 200
    s, summary = call(server, "GET", f"/api/games/{g['game_id']}")
    assert summary["players"][0]["bot"] == "idle"


def test_full_game_two_sdk_players_four_house_bots(server, tmp_path):
    bots = ["idle", extra_bot(), "idle", "economist" if bot_available("economist") else "idle"]
    c = AgentCivClient(server.url)
    server.manager.open_ratings = True  # a short custom game only counts on an open-ratings server
    gid = c.create_game(name="Integration", max_players=6, max_turns=12, bots=bots, **FAST)
    results = {}

    def claimer(view):
        """Claim a tile next to our territory every turn (exercises real orders)."""
        pid = view["you"]["id"]
        owner = view["map"]["owner"]
        h, w = len(owner), len(owner[0])
        for y in range(h):
            for x in range(w):
                if owner[y][x] is None and view["map"]["terrain"][y][x] not in "m~":
                    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        if 0 <= x + dx < w and 0 <= y + dy < h and owner[y + dy][x + dx] == pid:
                            return [{"type": "claim", "at": [x, y]}]
        return []

    def play(name, fn):
        results[name] = run_bot(fn, server.url, game_id=gid, name=name)

    threads = [threading.Thread(target=play, args=("Claimer", claimer)),
               threading.Thread(target=play, args=("Lazy", lambda v: []))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert set(results) == {"Claimer", "Lazy"}
    res = results["Claimer"]["result"]
    assert res["turn"] == 11 and len(res["placements"]) == 6
    assert results["Claimer"]["place"] is not None

    # replay: one frame per turn plus the initial one; last frame is finished
    replay = c.replay(gid)
    assert replay["result"] == res
    assert len(replay["frames"]) == 13
    assert [f["turn"] for f in replay["frames"]] == list(range(13))
    assert replay["frames"][-1]["status"] == "finished"
    claimer_pid = results["Claimer"]["player_id"]
    tiles = [next(p["tiles"] for p in f["players"] if p["id"] == claimer_pid) for f in replay["frames"]]
    assert tiles[-1] > tiles[0]

    # persisted: replay file, leaderboard (bots rated by bot name)
    assert wait_until(lambda: (tmp_path / "data" / "replays" / f"{gid}.json").exists())
    assert wait_until(lambda: len(c.leaderboard()) >= 4)
    names = {r["name"] for r in c.leaderboard()}
    assert {"Claimer", "Lazy", "idle"} <= names
    assert "idle#2" not in names
    lb = json.loads((tmp_path / "data" / "leaderboard.json").read_text())
    assert lb["format"] == 2 and lb["applied"] == [gid]
    assert lb["players"]["Claimer"]["games"] == 1
    idle_seats = sum(1 for b in bots if b == "idle")  # every seat of a repeated bot is rated
    assert lb["players"]["idle"]["games"] == idle_seats
    assert c.replay(gid)["summary"]["rating"]["pool"] == "standard"

    # a restarted server still lists and serves the finished game
    srv2 = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    try:
        c2 = AgentCivClient(srv2.url)
        listed = {g["game_id"]: g for g in c2.list_games()}
        assert listed[gid]["status"] == "finished"
        assert c2.replay(gid)["result"] == res
        assert c2.state(gid)["status"] == "finished"
        assert c2.wait(0, timeout=1, game_id=gid)["status"] == "finished"
        assert {r["name"]: r["games"] for r in c2.leaderboard()}["Claimer"] == 1
        new_gid = c2.create_game(max_players=2)
        assert new_gid != gid  # ids continue after archived ones
    finally:
        srv2.stop()


def test_unrated_game_skips_leaderboard(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, bots=["idle", "idle"], max_turns=2, rated=False, **FAST)
    assert c.wait(since_turn=5, timeout=10, game_id=gid)["status"] == "finished"
    time.sleep(0.2)
    assert c.leaderboard() == []


def test_bot_only_game_autostarts_and_finishes(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=5, bots=["idle"] * 3 + [extra_bot()] * 2, max_turns=6, turn_timeout=0.05)
    w = c.wait(since_turn=100, timeout=20, game_id=gid)
    assert w["status"] == "finished"
    summary = c.game(gid)
    assert summary["result"]["turn"] == 5
    assert [p["name"] for p in summary["players"]][:3] == ["idle", "idle#2", "idle#3"]


def test_lobby_timeout_and_fill_with_bots(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=4, lobby_timeout=0.2, fill_with_bots=True, turn_timeout=5)
    a = AgentCivClient(server.url)
    a.join(gid, "Solo")
    w = a.wait(since_turn=-1, timeout=10)
    assert w["status"] == "running"
    summary = a.game()
    assert len(summary["players"]) == 4 and sum(p["is_bot"] for p in summary["players"]) == 3


def test_quickmatch_groups_players(server):
    a, b, c = (AgentCivClient(server.url) for _ in range(3))
    ra = a.quickmatch("QA", players=3, turn_timeout=5)
    rb = b.quickmatch("QB", players=3, turn_timeout=5)
    assert ra["game_id"] == rb["game_id"] and rb["player_id"] == "p2"
    other = AgentCivClient(server.url).quickmatch("QX", players=4, turn_timeout=5)
    assert other["game_id"] != ra["game_id"]  # different size → different lobby
    rc = c.quickmatch("QC", players=3, turn_timeout=5)
    assert rc["game_id"] == ra["game_id"] and rc["status"] == "running"  # full → auto-start
    rd = AgentCivClient(server.url).quickmatch("QA", players=3, turn_timeout=5)
    assert rd["game_id"] != ra["game_id"]  # previous lobby is running now
    assert a.state()["you"]["id"] == "p1"


def test_quickmatch_fills_with_bots_after_lobby_timeout(server):
    a = AgentCivClient(server.url)
    a.quickmatch("Lonely", players=5, lobby_timeout=0.2, turn_timeout=5)
    w = a.wait(since_turn=-1, timeout=10)
    assert w["status"] == "running"
    assert len(a.game()["players"]) == 5


def _read_sse_event(resp):
    """Read one SSE event (event name, data) from an http.client response."""
    event, data = None, []
    while True:
        line = resp.fp.readline().decode()
        if line == "":
            return None, None
        line = line.rstrip("\n")
        if line == "":
            if data or event:
                return event, "\n".join(data)
            continue
        if line.startswith(":") or line.startswith("retry:"):
            continue
        key, _, val = line.partition(":")
        if key == "event":
            event = val.strip()
        elif key == "data":
            data.append(val[1:] if val.startswith(" ") else val)


def test_sse_stream_pushes_each_turn(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, turn_timeout=0)
    a, b = AgentCivClient(server.url), AgentCivClient(server.url)
    a.join(gid, "A")
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.request("GET", f"/api/games/{gid}/stream")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
    ev, data = _read_sse_event(resp)
    assert ev == "state" and json.loads(data)["status"] == "lobby"
    b.join(gid, "B")  # fills the game → starts
    ev, data = _read_sse_event(resp)
    view = json.loads(data)
    assert ev == "state" and view["status"] == "running" and view["turn"] == 0
    a.submit_orders([])
    # a remote player's submission is pushed to spectators (live "submitted" indicator)
    ev, data = _read_sse_event(resp)
    view = json.loads(data)
    assert ev == "state" and view["turn"] == 0
    assert {p["id"]: p["submitted"] for p in view["players"]} == {"p1": True, "p2": False}
    b.submit_orders([])
    ev, data = _read_sse_event(resp)
    if ev == "state" and json.loads(data)["turn"] == 0:  # B's submission may be pushed before the turn resolves
        ev, data = _read_sse_event(resp)
    assert ev == "state" and json.loads(data)["turn"] == 1
    conn.close()


def test_sse_stream_of_finished_game_ends(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, bots=["idle", "idle"], max_turns=2, **FAST)
    assert c.wait(since_turn=5, timeout=10, game_id=gid)["status"] == "finished"
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.request("GET", f"/api/games/{gid}/stream")
    resp = conn.getresponse()
    ev, data = _read_sse_event(resp)
    assert ev == "state" and json.loads(data)["status"] == "finished"
    ev, _ = _read_sse_event(resp)
    assert ev == "finished"
    assert _read_sse_event(resp) == (None, None)  # server closed the stream
    conn.close()


def test_replay_of_running_game_and_spectator_cache(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, turn_timeout=0)
    a, b = AgentCivClient(server.url), AgentCivClient(server.url)
    a.join(gid, "A")
    b.join(gid, "B")
    rep = c.replay(gid)
    assert rep["result"] is None and len(rep["frames"]) == 1
    session = server.manager.sessions[gid]
    first = session.state_bytes()
    assert session.state_bytes() is first  # cached until something changes
    a.submit_orders([])
    assert session.state_bytes() is not first


def test_many_concurrent_waiters(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, turn_timeout=0)
    a, b = AgentCivClient(server.url), AgentCivClient(server.url)
    a.join(gid, "A")
    b.join(gid, "B")
    out = []

    def waiter():
        out.append(AgentCivClient(server.url).wait(since_turn=0, timeout=10, game_id=gid)["turn"])

    threads = [threading.Thread(target=waiter) for _ in range(20)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    a.submit_orders([])
    b.submit_orders([])
    for t in threads:
        t.join(15)
    assert out == [1] * 20


def test_bot_only_games_are_paced_for_spectators(server):
    c = AgentCivClient(server.url)
    paced = c.create_game(max_players=2, bots=["idle", "idle"], turn_timeout=30)  # default: 0.5 s per turn
    explicit = c.create_game(max_players=2, bots=["idle", "idle"], turn_timeout=30, turn_delay=0.1)
    time.sleep(1.2)
    assert 1 <= c.game(paced)["turn"] <= 3
    assert c.game(explicit)["turn"] >= 6
