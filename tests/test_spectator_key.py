"""Operator spectator access, with ephemeral servers and manually advanced games."""
from __future__ import annotations

import http.client
import json
import logging
from urllib.parse import urlencode

import pytest

from agentciv.client import AgentCivClient
from agentciv.server import create_server

from test_server import _read_sse_event, call, wait_until


KEY = "operator-only/+?&=key"


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data"),
                        spectator_key=KEY).start_background()
    yield srv
    srv.stop()


def _game(server, *, fog=True):
    client = AgentCivClient(server.url)
    gid = client.create_game(max_players=3, turn_timeout=0, max_turns=3, fog=fog)
    players = [AgentCivClient(server.url) for _ in range(3)]
    for player, name in zip(players, ("Alice", "Bob", "Carol")):
        player.join(gid, name)
    assert wait_until(lambda: players[0].state()["status"] == "running")
    _advance(players, 0)
    return gid, players


@pytest.fixture()
def game(server):
    return _game(server)


def _advance(players, turn):
    for player in players:
        player.submit_orders([], turn=turn)
    assert wait_until(lambda: players[0].state()["turn"] == turn + 1)


def _path(gid, endpoint, **query):
    return f"/api/games/{gid}/{endpoint}" + ("?" + urlencode(query) if query else "")


def _bytes(server, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        assert response.status == 200
        return response.read()
    finally:
        conn.close()


def _stream(server, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    response = None
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("text/event-stream")
        event, data = _read_sse_event(response)
        assert event == "state"
        return data.encode()
    finally:
        if response is not None:
            response.close()
        conn.close()


def _hidden(view):
    assert view["armies"] == []
    assert all(player["resources"] is None for player in view["players"])


def _full(view):
    assert {army["owner"] for army in view["armies"]} == {"p1", "p2", "p3"}
    assert all(player["resources"] is not None for player in view["players"])


@pytest.mark.parametrize("via", ["header", "query"])
def test_live_fog_state_stream_and_replay(server, game, via):
    gid, _ = game
    headers = {"X-Spectator-Key": KEY} if via == "header" else {}
    query = {"spectator_key": KEY} if via == "query" else {}
    session = server.manager.get(gid)
    public = _bytes(server, _path(gid, "state"))
    _hidden(json.loads(public))
    cached_public = session.state_bytes()
    full = _bytes(server, _path(gid, "state", **query), headers)
    _full(json.loads(full))
    assert full == session._spectator_pair()[0]
    assert _stream(server, _path(gid, "stream", **query), headers) == full
    assert _stream(server, _path(gid, "stream")) == public
    assert _bytes(server, _path(gid, "state")) == public
    assert session.state_bytes() is cached_public
    for options in ({}, {"compact": 1}, {"from": 1, "to": 1},
                    {"compact": 1, "from": 1, "to": 1}):
        path = _path(gid, "replay", **options)
        before = _bytes(server, path)
        for frame in json.loads(before)["frames"]:
            _hidden(frame)
        keyed = json.loads(_bytes(server, _path(gid, "replay", **options, **query), headers))
        assert keyed["frames"]
        for frame in keyed["frames"]:
            _full(frame)
        assert _bytes(server, path) == before


@pytest.mark.parametrize("endpoint", ["state", "stream", "replay"])
def test_invalid_or_empty_key_is_unauthorized(server, game, endpoint):
    gid, _ = game
    cases = [({}, {"X-Spectator-Key": "wrong-key"}),
             ({"spectator_key": "wrong-key"}, {}),
             ({}, {"X-Spectator-Key": ""}),
             ({"spectator_key": ""}, {}),
             ({"spectator_key": "wrong-key"}, {"X-Spectator-Key": KEY}),
             ({"spectator_key": KEY}, {"X-Spectator-Key": "wrong-key"})]
    for query, headers in cases:
        status, body = call(server, "GET", _path(gid, endpoint, **query), headers=headers)
        assert status == 401
        assert "spectator" in body["error"].lower() and "key" in body["error"].lower()
        assert "wrong-key" not in body["error"] and KEY not in body["error"]
    duplicate = _path(gid, endpoint, spectator_key=KEY) + "&spectator_key=wrong-key"
    assert call(server, "GET", duplicate)[0] == 401


def test_presented_key_when_feature_disabled_is_unauthorized(tmp_path):
    server = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "disabled")).start_background()
    try:
        gid = AgentCivClient(server.url).create_game(max_players=3, turn_timeout=0, fog=True)
        for endpoint in ("state", "stream", "replay"):
            for query, headers in (({}, {"X-Spectator-Key": KEY}), ({"spectator_key": KEY}, {})):
                status, body = call(server, "GET", _path(gid, endpoint, **query), headers=headers)
                assert status == 401 and "spectator" in body["error"].lower()
        assert call(server, "GET", _path(gid, "state"))[0] == 200
    finally:
        server.stop()


def test_full_and_public_streams_share_change_keys_without_sharing_visibility(server, game):
    gid, players = game
    session = server.manager.get(gid)
    public_key, public, done = session.next_frame(None, 0)
    full_key, full, full_done = session.next_frame(None, 0, full=True)
    assert full_key == public_key and not done and not full_done
    _hidden(json.loads(public))
    _full(json.loads(full))
    assert session.next_frame(public_key, 0) == (public_key, None, False)
    assert session.next_frame(full_key, 0, full=True) == (full_key, None, False)
    players[0].submit_orders([], turn=1)
    next_key, next_full, done = session.next_frame(full_key, 0, full=True)
    next_public_key, next_public, public_done = session.next_frame(public_key, 0)
    assert next_key == next_public_key and next_key != full_key
    assert not done and not public_done
    _full(json.loads(next_full))
    _hidden(json.loads(next_public))
    assert json.loads(next_public)["players"][0]["submitted"] is True


@pytest.mark.parametrize("token_via", ["header", "query"])
def test_player_token_takes_precedence(server, game, token_via):
    gid, players = game
    headers = {"Authorization": f"Bearer {players[0].token}"} if token_via == "header" else {}
    query = {"token": players[0].token} if token_via == "query" else {}
    path = _path(gid, "state", **query)
    baseline = _bytes(server, path, headers)
    headers["X-Spectator-Key"] = KEY
    assert _bytes(server, path, headers) == baseline
    view = json.loads(baseline)
    assert {army["owner"] for army in view["armies"]} == {"p1"}
    assert {player["id"] for player in view["players"] if player["resources"] is None} == {"p2", "p3"}
    _hidden(json.loads(_stream(server, _path(gid, "stream", **query), headers)))
    for compact in (0, 1):
        replay = json.loads(_bytes(server, _path(gid, "replay", compact=compact, **query), headers))
        for frame in replay["frames"]:
            _hidden(frame)
    headers["X-Spectator-Key"] = "wrong-key"
    assert call(server, "GET", path, headers=headers)[0] == 401


def test_nonfog_operator_can_see_private_diplomacy(server):
    gid, players = _game(server, fog=False)
    players[0].say("p2", "private operator-visible message")
    _advance(players, 1)
    headers = {"X-Spectator-Key": KEY}
    public = json.loads(_bytes(server, _path(gid, "state")))
    full = json.loads(_bytes(server, _path(gid, "state"), headers))
    assert all(message["to"] == "all" for message in public["messages"])
    assert any(message["text"] == "private operator-visible message" for message in full["messages"])
    assert json.loads(_stream(server, _path(gid, "stream"), headers)) == full
    for compact in (0, 1):
        replay = json.loads(_bytes(server, _path(gid, "replay", compact=compact), headers))
        assert any(message["text"] == "private operator-visible message"
                   for message in replay["frames"][-1]["messages"])


def test_finished_and_archived_views_are_unchanged(server, game, tmp_path):
    gid, players = game
    for turn in (1, 2):
        _advance(players, turn)
    assert wait_until(lambda: players[0].state()["status"] == "finished")
    assert wait_until(lambda: (tmp_path / "data" / "replays" / f"{gid}.json").exists())
    archive = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data"),
                            spectator_key=KEY).start_background()
    try:
        for srv in (server, archive):
            for endpoint, query in (("state", {}), ("replay", {}), ("replay", {"compact": 1})):
                path = _path(gid, endpoint, **query)
                assert _bytes(srv, path, {"X-Spectator-Key": KEY}) == _bytes(srv, path)
            assert _stream(srv, _path(gid, "stream", spectator_key=KEY)) == _stream(srv, _path(gid, "stream"))
    finally:
        archive.stop()


def test_spectator_key_is_never_logged(server, game, caplog, monkeypatch):
    gid, _ = game
    caplog.set_level(logging.DEBUG)
    for endpoint in ("state", "stream", "replay"):
        path = _path(gid, endpoint, spectator_key=KEY)
        if endpoint == "stream":
            _stream(server, path)
        else:
            assert call(server, "GET", path)[0] == 200
        assert call(server, "GET", _path(gid, endpoint, spectator_key="secret-wrong-key"))[0] == 401

    def fail(*args, **kwargs):
        raise RuntimeError("test failure")

    monkeypatch.setattr(server.manager.get(gid), "state_bytes", fail)
    assert call(server, "GET", _path(gid, "state", spectator_key=KEY))[0] == 500
    assert KEY not in caplog.text
    assert urlencode({"spectator_key": KEY}).split("=", 1)[1] not in caplog.text
    assert "secret-wrong-key" not in caplog.text


@pytest.mark.parametrize("environment,flag,expected", [
    (None, None, None), ("env-key", None, "env-key"),
    ("env-key", "flag-key", "flag-key"), (None, "flag-key", "flag-key"),
    ("env-key", "", ""),
])
def test_cli_flag_and_environment(monkeypatch, environment, flag, expected):
    from agentciv.server import app
    from agentciv.server.__main__ import main

    if environment is None:
        monkeypatch.delenv("AGENTCIV_SPECTATOR_KEY", raising=False)
    else:
        monkeypatch.setenv("AGENTCIV_SPECTATOR_KEY", environment)
    calls = []
    monkeypatch.setattr(app, "serve", lambda *args, **kwargs: calls.append((args, kwargs)))
    argv = ["--port", "0"]
    if flag is not None:
        argv += ["--spectator-key", flag]
    assert main(argv) == 0
    assert len(calls) == 1
    assert calls[0][1].get("spectator_key") == expected
