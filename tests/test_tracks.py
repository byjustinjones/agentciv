"""Frozen evaluation tracks (agentciv/server/tracks.py, docs/EVALUATION.md):
option freezing, the track's own rating pool with a pinned rules hash,
operator-only seats and seed, and anonymous seats while a track game is live."""
from __future__ import annotations

import http.client
import json
import logging

import pytest

from agentciv.server import create_server, manager as manager_mod, tracks
from agentciv.server.manager import ApiError, parse_game_options, unrated_reason
from agentciv.server.tracks import Track

from test_server import _read_sse_event, call, wait_until

KEY = "operator-key-123"
TRACK = "eval-6p-fog-v1"
SMALL = "test-3p-v1"
OP = {"X-Spectator-Key": KEY}


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture()
def small_track(monkeypatch):
    """A 3-seat, 2-turn track (one negotiation round) so a game finishes in a test."""
    t = Track(id=SMALL, title="test", about="test track", players=3, fog=True, negotiation_rounds=1,
              phase_limit=60.0, max_turns=2)
    monkeypatch.setitem(tracks.TRACKS, SMALL, t)
    return t


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data"), spectator_key=KEY).start_background()
    yield srv
    srv.stop()


def agent(model, harness="test-harness 1"):
    return {"model": model, "harness": harness, "tools": "get_state,submit_orders", "notes": f"notes of {model}"}


def raw(srv, path, token=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    try:
        h = dict(headers or {})
        if token:
            h["Authorization"] = f"Bearer {token}"
        conn.request("GET", path, headers=h)
        r = conn.getresponse()
        body = r.read()
        assert r.status == 200, (path, r.status, body[:300])
        return body
    finally:
        conn.close()


def sse_frame(srv, gid, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    try:
        h = {"Authorization": f"Bearer {token}"} if token else {}
        conn.request("GET", f"/api/games/{gid}/stream", headers=h)
        r = conn.getresponse()
        event, data = _read_sse_event(r)
        assert event == "state"
        r.close()
        return data.encode()
    finally:
        conn.close()


def state(srv, gid, token):
    s, v = call(srv, "GET", f"/api/games/{gid}/state", token=token)
    assert s == 200
    return v


def play_turn(srv, gid, seats, say=None):
    """One synchronous turn: every seat ends each negotiation round, then submits no orders.
    ``say``: (index of the seat, text) said publicly in the first round."""
    v = state(srv, gid, seats[0]["token"])
    turn = v["turn"]
    while v.get("status") == "running" and v["turn"] == turn and v["phase"]["kind"] == "negotiate":
        pid = v["phase"]["id"]
        for i, seat in enumerate(seats):
            body = {"done": True}
            if say is not None and say[0] == i and v["phase"]["round"] == 1:
                body["actions"] = [{"type": "say", "to": "all", "text": say[1]}]
            s, r = call(srv, "POST", f"/api/games/{gid}/diplomacy", body, token=seat["token"])
            assert s == 200, r
        assert wait_until(lambda: state(srv, gid, seats[0]["token"])["phase"]["id"] != pid
                          or state(srv, gid, seats[0]["token"])["status"] != "running")
        v = state(srv, gid, seats[0]["token"])
    for seat in seats:
        s, r = call(srv, "POST", f"/api/games/{gid}/orders", {"turn": turn, "orders": []}, token=seat["token"])
        assert s == 200, r
    assert wait_until(lambda: (lambda w: w["status"] == "finished" or w["turn"] > turn)(
        state(srv, gid, seats[0]["token"])))


# ---------------------------------------------------------------- option freezing
def test_track_fills_in_the_frozen_options():
    o = parse_game_options({"track": TRACK, "name": "eval"})
    assert o["track"] == TRACK and o["max_players"] == o["min_players"] == 6 and o["bots"] == []
    assert o["fog"] is True and o["sync"] is True and o["negotiation_rounds"] == 3
    assert o["turn_timeout"] == 600.0 and o["max_turns"] == 150 and o["fill_with_bots"] is False
    assert o["lobby_timeout"] is None and o["turn_delay"] is None and o["rated"] is True
    assert o["seats"] is None and o["seed_given"] is False and o["name"] == "eval"
    assert unrated_reason(o) is None
    # repeating a frozen value is fine
    o = parse_game_options({"track": TRACK, "fog": True, "max_players": 6, "turn_timeout": 600, "bots": []})
    assert o["turn_timeout"] == 600.0


@pytest.mark.parametrize("key,value", [
    ("fog", False), ("fog", 1), ("max_players", 4), ("min_players", 2), ("turn_timeout", 30),
    ("max_turns", 100), ("bots", ["random"]), ("fill_with_bots", True), ("sync", False),
    ("negotiation_rounds", 1), ("lobby_timeout", 30), ("turn_delay", 1), ("rated", False),
])
def test_conflicting_option_is_refused_by_name(key, value):
    with pytest.raises(ApiError) as e:
        parse_game_options({"track": TRACK, key: value})
    assert e.value.status == 400 and f"option {key} conflicts with track {TRACK}" in e.value.message


def test_unknown_options_and_tracks_are_refused():
    with pytest.raises(ApiError) as e:
        parse_game_options({"track": TRACK, "puzzle": "p1"})
    assert e.value.status == 400 and "option puzzle is not allowed" in e.value.message
    with pytest.raises(ApiError) as e:
        parse_game_options({"track": "eval-nope-v9"})
    assert e.value.status == 400 and "unknown track" in e.value.message
    with pytest.raises(ApiError) as e:
        parse_game_options({"seats": ["a", "b"]})
    assert e.value.status == 400 and "only valid with a track" in e.value.message


def test_seed_and_seats_are_operator_only():
    for body in ({"seed": 5}, {"seats": [f"n{i}" for i in range(6)]}):
        with pytest.raises(ApiError) as e:
            parse_game_options({"track": TRACK, **body})
        assert e.value.status == 403 and "operator" in e.value.message
    o = parse_game_options({"track": TRACK, "seed": 5, "seats": [f"n{i}" for i in range(6)]}, operator=True)
    assert o["seed"] == 5 and o["seed_given"] and o["seats"] == [f"n{i}" for i in range(6)]
    assert unrated_reason(o) is None  # operator-seeded track games are still rated
    for seats, msg in ((["a"] * 6, "repeat"), (["a", "b"], "list of 6"), ([f"n{i}" for i in range(5)] + ["Player 2"],
                                                                           "anonymous seat name")):
        with pytest.raises(ApiError) as e:
            parse_game_options({"track": TRACK, "seats": seats}, operator=True)
        assert e.value.status == 400 and msg in e.value.message


def test_http_create_quickmatch_and_tracks_endpoint(server):
    s, r = call(server, "POST", "/api/games", {"track": TRACK, "turn_timeout": 30})
    assert s == 400 and "turn_timeout" in r["error"]
    s, r = call(server, "POST", "/api/games", {"track": TRACK, "seed": 3})
    assert s == 403
    s, r = call(server, "POST", "/api/games", {"track": TRACK, "seed": 3}, headers=OP)
    assert s == 200 and r["track"] == TRACK and r["rated"] is True and r["fog"] is True
    s, summ = call(server, "GET", f"/api/games/{r['game_id']}", headers=OP)
    assert summ["seed"] == 3 and summ["track"] == TRACK and summ["sync"] and summ["turn_timeout"] == 600.0
    s, summ = call(server, "GET", f"/api/games/{r['game_id']}")
    assert summ["seed"] is None  # the seed (hence the fog map) is hidden from the public while live
    s, ts = call(server, "GET", "/api/tracks")
    t = next(x for x in ts if x["id"] == TRACK)
    assert t["options"]["fog"] is True and t["policy"]["identity"] and t["agent"]["required"] == ["model", "harness"]
    assert t["rules_sha256"] == t["current_rules_sha256"] and t["open"] is True
    # quickmatch into a track: frozen options, manifest required, never bots
    s, r = call(server, "POST", "/api/quickmatch", {"track": TRACK, "name": "q1", "fill_with_bots": True,
                                                    "agent": agent("m")})
    assert s == 400 and "fill_with_bots" in r["error"]
    s, r = call(server, "POST", "/api/quickmatch", {"track": TRACK, "name": "q1"})
    assert s == 400 and "requires an agent manifest" in r["error"]
    s, r = call(server, "POST", "/api/quickmatch", {"track": TRACK, "name": "q1",
                                                    "agent": {"model": "m", "harness": "h", "tools": "web_search"}})
    assert s == 400 and "web_search" in r["error"]
    s, a = call(server, "POST", "/api/quickmatch", {"track": TRACK, "name": "q1", "agent": agent("m")})
    assert s == 200 and a["seat_name"] == "Player 1"
    s, b = call(server, "POST", "/api/quickmatch", {"track": TRACK, "name": "q2", "agent": agent("m")})
    assert s == 200 and b["game_id"] == a["game_id"] and b["seat_name"] == "Player 2"
    s, summ = call(server, "GET", f"/api/games/{a['game_id']}")
    assert summ["quickmatch"] and [p["name"] for p in summ["players"]] == ["Player 1", "Player 2"]
    assert summ["status"] == "lobby" and summ["fill_with_bots"] is False


def test_join_requires_a_manifest_and_refuses_seat_like_names(server):
    s, r = call(server, "POST", "/api/games", {"track": TRACK})
    gid = r["game_id"]
    s, e = call(server, "POST", f"/api/games/{gid}/join", {"name": "Alice"})
    assert s == 400 and "agent manifest" in e["error"]
    s, e = call(server, "POST", f"/api/games/{gid}/join", {"name": "Alice", "agent": {"model": "x"}})
    assert s == 400 and "harness" in e["error"]
    s, e = call(server, "POST", f"/api/games/{gid}/join", {"name": "player 3", "agent": agent("x")})
    assert s == 400 and "reserved" in e["error"]
    s, a = call(server, "POST", f"/api/games/{gid}/join", {"name": "Alice", "agent": agent("x")})
    assert s == 200 and a["seat_name"] == "Player 1" and set(a) == {"game_id", "player_id", "token", "status",
                                                                    "seat_name"}
    s, e = call(server, "POST", f"/api/games/{gid}/join", {"name": "alice", "agent": agent("y")})
    assert s == 409 and "alice" not in e["error"].lower() and "already" not in e["error"]
    s, e = call(server, "POST", f"/api/games/{gid}/start", {}, token=a["token"])
    assert s == 409  # a track game starts only with every seat taken


# ---------------------------------------------------------------- pools and the pinned rules hash
def test_track_pool_pins_the_rules_hash(server, monkeypatch):
    data = server.manager.storage.root
    monkeypatch.setattr(manager_mod, "rules_sha256", lambda: "a" * 64)
    s, r = call(server, "POST", "/api/games", {"track": TRACK})
    assert s == 200
    pool = json.loads((data / f"leaderboard_{TRACK}.json").read_text())
    assert pool == {"format": 2, "players": {}, "applied": [], "rules_sha256": "a" * 64}
    assert not (data / "leaderboard.json").exists() and not (data / "leaderboard_fog.json").exists()
    s, r = call(server, "POST", "/api/games", {"track": TRACK})
    assert s == 200
    monkeypatch.setattr(manager_mod, "rules_sha256", lambda: "b" * 64)
    s, r = call(server, "POST", "/api/games", {"track": TRACK})
    assert s == 409 and "eval-6p-fog-v2" in r["error"] and r["pinned_rules_sha256"] == "a" * 64
    assert "new track version" in r["error"]
    s, ts = call(server, "GET", "/api/tracks")
    t = next(x for x in ts if x["id"] == TRACK)
    assert t["rules_sha256"] == "a" * 64 and t["current_rules_sha256"] == "b" * 64 and t["open"] is False
    # open games are not affected
    s, r = call(server, "POST", "/api/games", {"max_players": 2, "fog": True})
    assert s == 200


def test_leaderboard_endpoint_validates_the_track(server):
    s, rows = call(server, "GET", f"/api/leaderboard?track={TRACK}")
    assert s == 200 and rows == []
    s, e = call(server, "GET", "/api/leaderboard?track=nope-v1")
    assert s == 404
    s, e = call(server, "GET", f"/api/leaderboard?track={TRACK}&mode=fog")
    assert s == 400


# ---------------------------------------------------------------- anonymity
REAL = ["Zebra-Alpha", "Quokka-Beta", "Narwhal-Gamma", "Okapi-Delta", "Axolotl-Eps", "Pangolin-Zeta"]
MODELS = ["model-unicorn-7", "model-griffin-8", "model-sphinx-9", "model-kraken-1", "model-hydra-2",
          "model-phoenix-3"]


def secrets_in(blob: bytes) -> list[str]:
    low = blob.lower()
    words = REAL + MODELS + ["test-harness", "notes of"]
    return [w for w in words if w.lower().encode() in low]


def test_live_track_game_never_shows_real_names(server):
    s, r = call(server, "POST", "/api/games", {"track": TRACK, "name": "anon test"})
    gid = r["game_id"]
    seats = []
    for name, model in zip(REAL, MODELS):
        s, j = call(server, "POST", f"/api/games/{gid}/join", {"name": name, "agent": agent(model)})
        assert s == 200, j
        assert not secrets_in(json.dumps(j).encode())
        seats.append(j)
    assert [j["seat_name"] for j in seats] == [f"Player {k}" for k in range(1, 7)]
    tok = seats[0]["token"]
    assert wait_until(lambda: state(server, gid, tok)["status"] == "running")
    # one negotiation round with a message (that does not self-identify), so the inbox has an item
    for i, seat in enumerate(seats):
        body = {"done": True, "actions": [{"type": "say", "to": "all", "text": "hello table"}] if i == 1 else []}
        s, x = call(server, "POST", f"/api/games/{gid}/diplomacy", body, token=seat["token"])
        assert s == 200 and not secrets_in(json.dumps(x).encode())
    assert wait_until(lambda: state(server, gid, tok)["phase"]["round"] == 2)
    paths = [f"/api/games", f"/api/games/{gid}", f"/api/games/{gid}/state", f"/api/games/{gid}/replay",
             f"/api/games/{gid}/replay?compact=1", f"/api/games/{gid}/wait?timeout=0"]
    for token in (None, tok):
        for p in paths:
            blob = raw(server, p, token=token)
            assert not secrets_in(blob), (p, token is not None, secrets_in(blob))
            if p != "/api/games":
                assert b"Player 1" in blob or b"/wait" in p.encode()
        frame = sse_frame(server, gid, token)
        assert not secrets_in(frame) and b"Player 2" in frame
    inbox = raw(server, f"/api/games/{gid}/inbox?since=0&timeout=0", token=tok)
    assert b"hello table" in inbox and not secrets_in(inbox)
    assert not secrets_in(raw(server, f"/api/leaderboard?track={TRACK}"))
    # the operator sees both names and the manifests
    summ = json.loads(raw(server, f"/api/games/{gid}", headers=OP))
    assert [p["name"] for p in summ["players"]] == REAL
    assert [p["seat_name"] for p in summ["players"]] == [f"Player {k}" for k in range(1, 7)]
    assert [p["agent"]["model"] for p in summ["players"]] == MODELS
    listing = raw(server, "/api/games", headers=OP)
    assert all(n.encode() in listing for n in REAL)
    assert all(n.encode() in raw(server, f"/api/games/{gid}/replay", headers=OP) for n in REAL)
    # a full game: joining says nothing about the seated names
    s, e = call(server, "POST", f"/api/games/{gid}/join", {"name": REAL[0], "agent": agent("x")})
    assert s == 409 and not secrets_in(json.dumps(e).encode())


def _finished_small_game(srv, seats_order=None, say=None):
    body = {"track": SMALL, "name": "small"}
    if seats_order:
        body["seats"] = seats_order
    s, r = call(srv, "POST", "/api/games", body, headers=OP if seats_order else None)
    assert s == 200, r
    gid = r["game_id"]
    seats = []
    for name, model in zip(REAL[:3], MODELS[:3]):
        s, j = call(srv, "POST", f"/api/games/{gid}/join", {"name": name, "agent": agent(model)})
        assert s == 200, j
        seats.append(j)
    assert wait_until(lambda: state(srv, gid, seats[0]["token"])["status"] == "running")
    return gid, seats


def test_finished_track_game_reveals_mapping_and_rates_real_names(server, small_track):
    gid, seats = _finished_small_game(server)
    play_turn(server, gid, seats, say=(0, "I am Zebra-Alpha"))
    live = raw(server, f"/api/games/{gid}")
    assert b"Zebra-Alpha" not in live  # (the message itself is public in the frames: the report flags it)
    play_turn(server, gid, seats)
    assert wait_until(lambda: call(server, "GET", f"/api/games/{gid}")[1]["status"] == "finished")
    assert wait_until(lambda: server.manager.storage.is_applied(gid, SMALL))
    s, summ = call(server, "GET", f"/api/games/{gid}")
    assert [(p["id"], p["name"], p["seat_name"]) for p in summ["players"]] == [
        ("p1", REAL[0], "Player 1"), ("p2", REAL[1], "Player 2"), ("p3", REAL[2], "Player 3")]
    assert [p["agent"]["model"] for p in summ["players"]] == MODELS[:3]
    assert summ["rating"]["pool"] == SMALL and sorted(n for n, _ in summ["rating"]["entries"]) == sorted(REAL[:3])
    assert summ["seed"] is not None and summ["track"] == SMALL and summ["seats_fixed"] is False
    replay = json.loads(raw(server, f"/api/games/{gid}/replay"))
    assert [p["name"] for p in replay["summary"]["players"]] == REAL[:3]
    assert {p["name"] for p in replay["frames"][-1]["players"]} == {"Player 1", "Player 2", "Player 3"}
    says = [d for t in replay["actions"]["turns"] for d in t["diplomacy"] if d["action"]["type"] == "say"]
    assert says and says[0]["by"] == "p1"
    rows = json.loads(raw(server, f"/api/leaderboard?track={SMALL}"))
    assert sorted(r["name"] for r in rows) == sorted(REAL[:3])
    assert json.loads(raw(server, "/api/leaderboard")) == [] and json.loads(raw(server, "/api/leaderboard?mode=fog")) == []
    pool = json.loads((server.manager.storage.root / f"leaderboard_{SMALL}.json").read_text())
    assert pool["applied"] == [gid] and len(pool["rules_sha256"]) == 64


def test_operator_seats_fix_who_plays_which_seat(server, small_track):
    order = [REAL[2], REAL[0], REAL[1]]
    s, r = call(server, "POST", "/api/games", {"track": SMALL, "seats": order, "seed": 11})
    assert s == 403
    s, r = call(server, "POST", "/api/games", {"track": SMALL, "seats": order, "seed": 11}, headers=OP)
    gid = r["game_id"]
    assert r["rated"] is True
    s, e = call(server, "POST", f"/api/games/{gid}/join", {"name": "Stranger", "agent": agent("x")})
    assert s == 409 and "Stranger" not in e["error"]
    seats = {}
    for name, model in zip(REAL[:3], MODELS[:3]):   # join order differs from the seat order
        s, j = call(server, "POST", f"/api/games/{gid}/join", {"name": name, "agent": agent(model)})
        assert s == 200, j
        seats[name] = j
    assert seats[REAL[2]]["player_id"] == "p1" and seats[REAL[0]]["player_id"] == "p2"
    assert seats[REAL[2]]["seat_name"] == "Player 1"
    ordered = [seats[n] for n in order]
    assert wait_until(lambda: state(server, gid, ordered[0]["token"])["status"] == "running")
    s, summ = call(server, "GET", f"/api/games/{gid}")
    assert [p["id"] for p in summ["players"]] == ["p1", "p2", "p3"] and summ["seats_fixed"] is True
    assert summ["seed"] is None
    play_turn(server, gid, ordered)
    play_turn(server, gid, ordered)
    assert wait_until(lambda: server.manager.storage.is_applied(gid, SMALL))
    s, summ = call(server, "GET", f"/api/games/{gid}")
    assert [p["name"] for p in summ["players"]] == order and summ["seed"] == 11 and summ["rated"] is True


def test_checkpoint_restore_keeps_anonymity_and_the_mapping(tmp_path, small_track):
    data = str(tmp_path / "data")
    srv = create_server("127.0.0.1", 0, data_dir=data, spectator_key=KEY).start_background()
    try:
        gid, seats = _finished_small_game(srv)
        play_turn(srv, gid, seats)
    finally:
        srv.stop()
    srv = create_server("127.0.0.1", 0, data_dir=data, spectator_key=KEY).start_background()
    try:
        assert gid in srv.manager.restored
        for token in (None, seats[1]["token"]):
            for p in (f"/api/games/{gid}", f"/api/games/{gid}/state", f"/api/games/{gid}/replay", "/api/games"):
                assert not secrets_in(raw(srv, p, token=token)), p
        summ = json.loads(raw(srv, f"/api/games/{gid}", headers=OP))
        assert [(p["name"], p["seat_name"]) for p in summ["players"]] == [
            (REAL[k], f"Player {k + 1}") for k in range(3)]
        play_turn(srv, gid, seats)
        assert wait_until(lambda: srv.manager.storage.is_applied(gid, SMALL))
        rows = json.loads(raw(srv, f"/api/leaderboard?track={SMALL}"))
        assert sorted(r["name"] for r in rows) == sorted(REAL[:3])
    finally:
        srv.stop()
