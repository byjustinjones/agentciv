"""Server integration details: agent onboarding (self-describing API, actionable
order errors), the compact/ranged replay, gzip, SSE fan-out to many spectators,
long-poll timing under a deadline, and replay-frame memory."""
from __future__ import annotations

import gzip
import http.client
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from agentciv.client import AgentCivClient
from agentciv.server import create_server
from agentciv.server.guide import ORDER_EXAMPLES
from agentciv.server.manager import ERROR_GRACE
from agentciv.server.replay import FrameStore

FAST = {"turn_timeout": 0.05, "turn_delay": 0}


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    yield srv
    srv.stop()


def get(srv, path, headers=None):
    req = urllib.request.Request(srv.url + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def two_player_game(srv, **opts):
    c = AgentCivClient(srv.url)
    gid = c.create_game(max_players=2, **opts)
    a, b = AgentCivClient(srv.url), AgentCivClient(srv.url)
    a.join(gid, "A")
    b.join(gid, "B")
    return c, gid, a, b


# ---------------------------------------------------------------- onboarding
def test_api_index_is_self_describing(server):
    status, _, body = get(server, "/api")
    idx = json.loads(body)
    assert status == 200
    steps = idx["how_to_play"]
    assert len(steps) == 4 and all(server.url in s for s in steps)
    assert idx["first_call"] == {"method": "POST", "url": f"{server.url}/api/quickmatch", "body": {"name": "YourAgent"}}
    assert {o["type"] for o in idx["order_examples"]} == set(ORDER_EXAMPLES)
    joined = " ".join(idx["endpoints"])
    for ep in ("/api/quickmatch", "/state", "/orders", "/wait", "/replay", "/stream", "/api/rules"):
        assert ep in joined
    assert "401" in idx["errors"] and "409" in idx["errors"]
    assert server.url in idx["clients"]["mcp"]


def test_rules_markdown_ends_with_api_quickref(server):
    status, headers, body = get(server, "/api/rules")
    md = body.decode()
    assert status == 200 and headers["Content-Type"].startswith("text/markdown")
    assert md.startswith("#") and "## HTTP API quick reference" in md
    assert f"POST {server.url}/api/quickmatch" in md and '{"type":"claim","at":[6,4]}' in md


def test_unknown_endpoint_and_bad_token_messages_point_somewhere(server):
    status, _, body = get(server, "/api/nope")
    assert status == 404 and "GET /api" in json.loads(body)["error"]
    c, gid, a, b = two_player_game(server, turn_timeout=0)
    status, _, body = get(server, f"/api/games/{gid}/state?token=wrong")
    assert status == 401 and "quickmatch" in json.loads(body)["error"]


def test_rejected_orders_carry_examples_hints_and_a_note(server):
    c, gid, a, b = two_player_game(server, turn_timeout=0)
    res = a.submit_orders([
        {"type": "claim", "at": "6,4"},                 # wrong shape
        {"type": "build", "at": [0, 0], "building": "farmm"},
        {"claim": [1, 2]},                              # no type
        "hello",                                        # not an object
        {"type": "mvoe"},                               # unknown type
    ], turn=0)
    errs = {e["index"]: e for e in res["errors"]}
    assert set(errs) == {0, 1, 2, 3, 4} and res["accepted"] == 0
    assert errs[0]["example"] == ORDER_EXAMPLES["claim"] and "hint" in errs[0]
    assert errs[1]["example"]["type"] == "build" and "farm" in errs[1]["error"]
    assert "needs a \"type\"" in errs[2]["hint"]
    assert "JSON object" in errs[3]["hint"]
    assert "valid types" in errs[4]["hint"] and "move" in errs[4]["hint"]
    assert "Resubmit" in res["note"] and res["ready"] is True
    ok = a.submit_orders([], turn=0)
    assert ok["errors"] == [] and "note" not in ok


def test_turn_waits_briefly_for_a_fix_after_rejected_orders(server):
    c, gid, a, b = two_player_game(server, turn_timeout=0)
    a.submit_orders([], turn=0)
    t0 = time.monotonic()
    b.submit_orders([{"type": "claim", "at": [-5, -5]}], turn=0)  # rejected → grace period
    time.sleep(0.3)
    assert c.game(gid)["turn"] == 0                                  # still open for a fix
    b.submit_orders([], turn=0)                                      # the fix resolves it at once
    w = c.wait(since_turn=0, timeout=10, game_id=gid)
    assert w["turn"] == 1 and time.monotonic() - t0 < ERROR_GRACE


def test_ready_false_holds_the_turn_until_ready(server):
    c, gid, a, b = two_player_game(server, turn_timeout=0)
    a.submit_orders([], turn=0)
    res = b.submit_orders([], turn=0, ready=False)
    assert res["ready"] is False
    assert c.wait(since_turn=0, timeout=0.4, game_id=gid)["timed_out"] is True
    b.submit_orders([], turn=0)
    assert c.wait(since_turn=0, timeout=10, game_id=gid)["turn"] == 1
    # the draft flag resets every turn
    a.submit_orders([], turn=1)
    b.submit_orders([], turn=1)
    assert c.wait(since_turn=1, timeout=10, game_id=gid)["turn"] == 2


def test_ready_false_still_respects_the_deadline(server):
    c, gid, a, b = two_player_game(server, turn_timeout=0.5)
    a.submit_orders([], turn=0)
    b.submit_orders([], turn=0, ready=False)
    t0 = time.monotonic()
    w = c.wait(since_turn=0, timeout=10, game_id=gid)
    assert w["turn"] == 1 and time.monotonic() - t0 < 3


# ---------------------------------------------------------------- lobby
def test_lobby_spectator_view_and_summary_after_joins(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=4, bots=["idle"], turn_timeout=0)
    c.state(gid, spectator=True)
    AgentCivClient(server.url).join(gid, "Remote-1")
    view = c.state(gid, spectator=True)  # must include the new player (no stale engine caches)
    assert view["status"] == "lobby" and [p["name"] for p in view["players"]] == ["idle", "Remote-1"]
    summary = c.game(gid)
    assert [p["is_bot"] for p in summary["players"]] == [True, False]
    assert summary["frames"] == 0 or summary["frames"] == 1


# ---------------------------------------------------------------- replay + gzip
def test_compact_and_ranged_replay_live_and_archived(server, tmp_path):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=3, bots=["idle", "idle", "idle"], max_turns=5, **FAST)
    assert c.wait(since_turn=10, timeout=20, game_id=gid)["status"] == "finished"

    def replay(q=""):
        status, headers, body = get(server, f"/api/games/{gid}/replay{q}")
        assert status == 200
        return json.loads(body)

    full = replay()
    n = len(full["frames"])
    assert n == 6 and "compact" not in full
    rng = replay("?from=1&to=2")
    assert [f["turn"] for f in rng["frames"]] == [1, 2] and rng["total_frames"] == n
    assert rng["frames"] == full["frames"][1:3]
    comp = replay("?compact=1&from=4")
    assert comp["compact"] and comp["from"] == 4 and comp["to"] == 5 and comp["total_frames"] == n
    assert all("costs" not in f and "terrain" not in f["map"] for f in comp["frames"])
    assert comp["static"]["terrain"] == full["frames"][0]["map"]["terrain"]
    # beyond the end: empty, but well-formed
    assert replay("?compact=1&from=99")["frames"] == []

    # after a restart the game is archived: same answers from the replay file
    server.stop()
    srv2 = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    try:
        status, _, body = get(srv2, f"/api/games/{gid}/replay?compact=1&from=4")
        again = json.loads(body)
        assert status == 200 and again["frames"] == comp["frames"] and again["static"] == comp["static"]
        status, _, body = get(srv2, f"/api/games/{gid}/replay?from=1&to=2")
        assert json.loads(body)["frames"] == rng["frames"]
        status, _, body = get(srv2, f"/api/games/{gid}/state")
        assert json.loads(body) == full["frames"][-1]
    finally:
        srv2.stop()


def test_gzip_when_accepted(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, bots=["idle", "idle"], max_turns=3, **FAST)
    c.wait(since_turn=10, timeout=20, game_id=gid)
    status, h, plain = get(server, f"/api/games/{gid}/replay")
    assert status == 200 and "Content-Encoding" not in h
    status, h, packed = get(server, f"/api/games/{gid}/replay", {"Accept-Encoding": "gzip, deflate"})
    assert h["Content-Encoding"] == "gzip" and h["Vary"] == "Accept-Encoding"
    assert gzip.decompress(packed) == plain and len(packed) < len(plain) / 3
    assert int(h["Content-Length"]) == len(packed)
    status, h, _ = get(server, "/api/bots", {"Accept-Encoding": "gzip"})  # small: sent as is
    assert "Content-Encoding" not in h
    assert "X-Server-Time" in h and abs(float(h["X-Server-Time"]) - time.time()) < 5


def test_replay_frame_memory_150_turns_8_players():
    """In-memory frames are zlib-compressed: a 150-turn, 8-player game stays small."""
    from agentciv.bots import get_bot
    from agentciv.engine import Game, GameConfig
    g = Game(GameConfig(seed=5, max_turns=150, game_id="m", max_players=8))
    bots = {g.add_player(f"b{i}"): get_bot("idle", i) for i in range(8)}
    g.start()
    store = FrameStore()
    dump = lambda: json.dumps(g.spectator_view(), separators=(",", ":")).encode()  # noqa: E731
    store.append(dump())
    while g.status != "finished":
        for pid, bot in bots.items():
            g.submit_orders(pid, bot.act(g.player_view(pid)))
        g.step()
        store.append(dump())
    assert len(store) >= 100
    assert store.memory_bytes() < 0.3 * store.raw_bytes
    assert store.memory_bytes() < 3_000_000
    assert json.loads(store.full(len(store) - 1)[0]) == json.loads(dump())


# ---------------------------------------------------------------- many clients
def _read_event(fp):
    event, data = None, []
    while True:
        line = fp.readline().decode()
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


def test_many_sse_spectators_get_every_turn(server):
    c, gid, a, b = two_player_game(server, turn_timeout=0)
    n = 60
    conns, results = [], []
    for _ in range(n):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
        conn.request("GET", f"/api/games/{gid}/stream")
        resp = conn.getresponse()
        assert resp.status == 200
        ev, data = _read_event(resp.fp)
        assert ev == "state" and json.loads(data)["turn"] == 0
        conns.append((conn, resp))

    def reader(resp):
        turns = []
        while len(turns) < 2 or turns[-1] < 2:
            ev, data = _read_event(resp.fp)
            if ev is None:
                break
            if ev == "state":
                turns.append(json.loads(data)["turn"])
        results.append(turns)

    threads = [threading.Thread(target=reader, args=(r,)) for _, r in conns]
    for t in threads:
        t.start()
    for turn in (0, 1):
        a.submit_orders([], turn=turn)
        b.submit_orders([], turn=turn)
        assert c.wait(since_turn=turn, timeout=10, game_id=gid)["turn"] == turn + 1
    for t in threads:
        t.join(20)
    for conn, _ in conns:
        conn.close()
    assert len(results) == n
    assert all(r and r[-1] == 2 and 1 in r for r in results), results[:3]
    # the server is still responsive
    assert c.game(gid)["turn"] == 2


def test_long_poll_returns_at_the_deadline(server):
    c, gid, a, b = two_player_game(server, turn_timeout=0.6)
    a.submit_orders([], turn=0)  # b never submits: the deadline resolves the turn
    t0 = time.monotonic()
    w = b.wait(since_turn=0, timeout=10)
    dt = time.monotonic() - t0
    assert w["turn"] == 1 and w["timed_out"] is False and w["status"] == "running"
    assert dt < 1.6
    assert w["deadline"] is not None and w["deadline"] > time.time()
    # a short long-poll with nothing happening times out cleanly
    a.submit_orders([], turn=1)
    t0 = time.monotonic()
    w = a.wait(since_turn=1, timeout=0.2)
    assert w["timed_out"] is True and w["turn"] == 1 and time.monotonic() - t0 < 0.6
