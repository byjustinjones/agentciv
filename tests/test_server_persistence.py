"""Running games survive a server restart (checkpoints in <data_dir>/live), and
clients ride out the outage (docs/DESIGN.md §12 "Server restarts")."""
from __future__ import annotations

import importlib.util
import json
import pickle
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from agentciv.bots import get_bot
from agentciv.client import AgentCivClient, ApiError, run_bot
from agentciv.engine.testing import new_game
from agentciv.mcp_server import AgentCivMCP
from agentciv.server import create_server
from agentciv.server.manager import GameManager
from agentciv.server.persist import LiveStore

ROOT = Path(__file__).resolve().parents[1]


def wait_until(pred, timeout=20.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def strip_deadline(view: dict) -> dict:
    """A restart gives the current turn a fresh deadline; everything else must match."""
    view = json.loads(json.dumps(view))
    view.pop("deadline", None)
    return view


def play_turn(game, bots):
    """One engine turn for ``bots`` ({pid: bot}): negotiate once, then act."""
    for p, b in bots.items():
        if game.player(p).alive:
            acts = b.negotiate(game.player_view(p))
            if acts:
                game.diplomacy(p, acts)
    for p, b in bots.items():
        if game.player(p).alive:
            game.submit_orders(p, b.act(game.player_view(p)))
    game.step()


# ---------------------------------------------------------------- engine
def test_engine_game_pickles_and_continues_deterministically():
    g = new_game(5, seed=11, max_turns=45)
    names = ["strategist", "economist", "rusher", "turtle", "random"]
    bots = {p.id: get_bot(n, 100 + i) for i, (p, n) in enumerate(zip(g.players, names))}
    for _ in range(15):
        play_turn(g, bots)
    blob = pickle.dumps((g, bots), protocol=pickle.HIGHEST_PROTOCOL)
    g2, bots2 = pickle.loads(blob)
    assert json.dumps(g2.spectator_view(full=True), sort_keys=True) == \
        json.dumps(g.spectator_view(full=True), sort_keys=True)
    while g.status != "finished":
        play_turn(g, bots)
        play_turn(g2, bots2)
        assert g.turn == g2.turn
    assert g2.status == "finished"
    assert g2.result == g.result
    assert json.dumps(g2.spectator_view(full=True), sort_keys=True) == \
        json.dumps(g.spectator_view(full=True), sort_keys=True)
    assert g2.diplomacy_log == g.diplomacy_log


def test_fog_game_pickles_with_all_hidden_state():
    """Checkpoints carry fog state (sightings, spy reports, counter-intelligence),
    banks, legacy and streaks, and treaty bonds/cooldowns: every player's own
    fogged view matches after a pickle round trip and play continues identically."""
    g = new_game(6, seed=7, max_turns=60, fog=True)
    names = ["strategist", "economist", "rusher", "turtle", "strategist_lite", "economist"]
    bots = {p.id: get_bot(n, 200 + i) for i, (p, n) in enumerate(zip(g.players, names))}
    for _ in range(25):
        play_turn(g, bots)
    p1 = g.player("p1")
    p1.bank = max(p1.bank, 500)
    p1.legacy = max(p1.legacy, 300)
    g._invalidate()

    def views(game):
        return [json.dumps(game.player_view(p.id), sort_keys=True) for p in game.players]

    g2, bots2 = pickle.loads(pickle.dumps((g, bots), protocol=pickle.HIGHEST_PROTOCOL))
    assert views(g2) == views(g)
    assert g2.sightings == g.sightings and g2.intel_reports == g.intel_reports
    assert [(p.bank, p.legacy, p.economic_streak, p.influence_streak, p.ci_pool) for p in g2.players] == \
        [(p.bank, p.legacy, p.economic_streak, p.influence_streak, p.ci_pool) for p in g.players]
    for _ in range(10):
        play_turn(g, bots)
        play_turn(g2, bots2)
    assert views(g2) == views(g)
    assert json.dumps(g2.spectator_view(full=True), sort_keys=True) == \
        json.dumps(g.spectator_view(full=True), sort_keys=True)


# ---------------------------------------------------------------- helpers
def submit_bot_orders(client: AgentCivClient, bot) -> dict:
    view = client.state()
    return client.submit_orders(bot.act(view), turn=view["turn"])


def turn_of(client: AgentCivClient) -> int:
    return client.game()["turn"]


def live_files(data: Path, gid: str) -> list[Path]:
    return [p for p in (data / "live" / f"{gid}.pkl", data / "live" / f"{gid}.frames") if p.exists()]


def _point(clients, srv):
    for c in clients:
        c.base_url = srv.url


# ---------------------------------------------------------------- the main scenario
def test_running_game_survives_restart_and_finishes(tmp_path):
    data = tmp_path / "data"
    srv = create_server("127.0.0.1", 0, data_dir=str(data)).start_background()
    try:
        creator = AgentCivClient(srv.url, retry_seconds=0)
        gid = creator.create_game(max_players=4, bots=["economist", "turtle"], turn_timeout=60, turn_delay=0,
                                  max_turns=9, seed=5, name="restart me")
        a = AgentCivClient(srv.url, retry_seconds=0)
        b = AgentCivClient(srv.url, retry_seconds=0)
        a.join(gid, "Alice")
        b.join(gid, "Bob")  # full: starts
        bot_a, bot_b = get_bot("economist", 1), get_bot("strategist", 2)
        assert wait_until(lambda: a.game()["status"] == "running")
        # a few normal turns with real orders
        for t in range(3):
            assert wait_until(lambda: turn_of(a) == t)
            submit_bot_orders(a, bot_a)
            submit_bot_orders(b, bot_b)
            assert wait_until(lambda: turn_of(a) == t + 1)
        # mid-turn: an executed deal, an open proposal, a private message, one player's orders
        turn = turn_of(a)
        view_a = a.state()
        give = {r: 5 for r in ("wood",) if view_a["you"]["resources"].get("wood", 0) >= 5}
        r = a.propose(b.player_id, give=give or {"food": 1}, get={"food": 1}, message="small swap")
        assert r["ok"], r
        acc = b.accept(r["deal"])
        assert acc["ok"] and acc["status"] == "accepted", acc
        open_deal = b.propose(a.player_id, give={"food": 2}, get={"wood": 1})
        assert open_deal["ok"], open_deal
        assert b.say(a.player_id, "hello there")["ok"]
        submit_bot_orders(a, bot_a)
        # the throttled checkpoint (not only the shutdown flush) records the submission
        store = LiveStore(data / "live")
        assert wait_until(lambda: store.load_state(gid)["game"].has_submitted(a.player_id), timeout=5)
        assert turn_of(a) == turn

        before = {
            "a": strip_deadline(a.state()), "b": strip_deadline(b.state()),
            "spec": strip_deadline(a.state(spectator=True)),
            "replay": creator.replay(gid)["frames"],
            "summary": a.game(),
            "inbox_b": b.inbox(since=0, timeout=0)["items"],
            "full_frames": [json.loads(f) for f in srv.manager.get(gid).frames.full()],
        }
        assert len(before["replay"]) == turn + 1 == len(before["full_frames"])
    finally:
        srv.stop()
    assert len(live_files(data, gid)) == 2

    srv2 = create_server("127.0.0.1", 0, data_dir=str(data)).start_background()
    try:
        _point([creator, a, b], srv2)
        assert srv2.manager.restored == [gid]
        listed = {g["game_id"]: g for g in creator.list_games()}
        assert listed[gid]["status"] == "running" and listed[gid]["turn"] == turn
        s2 = a.game()
        assert s2["deadline"] > time.time() + 50  # a full turn after the restart
        for k in ("name", "players", "seed", "max_turns", "turn_timeout", "created", "rated", "frames"):
            assert s2[k] == before["summary"][k], k
        # same tokens, identical views (but the deadline)
        assert strip_deadline(a.state()) == before["a"]
        assert strip_deadline(b.state()) == before["b"]
        assert strip_deadline(a.state(spectator=True)) == before["spec"]
        assert creator.replay(gid)["frames"] == before["replay"]
        assert b.inbox(since=0, timeout=0)["items"] == before["inbox_b"]
        assert a.state()["you"]["submitted"] is True and b.state()["you"]["submitted"] is False
        # the open deal is still open and can be accepted; ids keep counting
        assert a.accept(open_deal["deal"])["ok"]
        # new games get fresh ids
        new_id = creator.create_game(max_players=2, turn_timeout=5)
        assert int(new_id[1:]) > int(gid[1:])
        # play on to the end with the same credentials
        submit_bot_orders(b, bot_b)
        assert wait_until(lambda: turn_of(a) == turn + 1)
        while True:
            view = a.state()
            if view["status"] == "finished":
                break
            t = view["turn"]
            submit_bot_orders(a, bot_a)
            submit_bot_orders(b, bot_b)
            assert wait_until(lambda: a.game()["turn"] > t or a.game()["status"] == "finished")
        # replay saved with every frame (before and after the restart), live files gone
        assert wait_until(lambda: (data / "replays" / f"{gid}.json").exists())
        assert wait_until(lambda: not live_files(data, gid))
        replay = json.loads((data / "replays" / f"{gid}.json").read_bytes())
        frames = replay["frames"]
        assert [f["turn"] for f in frames] == list(range(len(frames)))
        assert replay["summary"]["status"] == "finished" and len(frames) == replay["summary"]["turn"] + 1
        assert frames[:turn + 1] == before["full_frames"]  # the frames recorded before the restart, unchanged
        # tokens of the finished game still resolve (409, not 401)
        with pytest.raises(ApiError) as e:
            a.submit_orders([])
        assert e.value.status == 409
    finally:
        srv2.stop()


def test_lobby_survives_restart_and_stays_joinable(tmp_path):
    data = tmp_path / "data"
    srv = create_server("127.0.0.1", 0, data_dir=str(data)).start_background()
    try:
        creator = AgentCivClient(srv.url, retry_seconds=0)
        gid = creator.create_game(max_players=4, bots=["idle"], turn_timeout=5, turn_delay=0)
        a = AgentCivClient(srv.url, retry_seconds=0)
        a.join(gid, "Alice")
    finally:
        srv.stop()
    srv2 = create_server("127.0.0.1", 0, data_dir=str(data)).start_background()
    try:
        _point([creator, a], srv2)
        assert a.game()["status"] == "lobby"
        assert a.state()["you"]["name"] == "Alice"
        b = AgentCivClient(srv2.url, retry_seconds=0)
        with pytest.raises(ApiError) as e:
            b.start(gid)  # remote players are seated: needs a token
        assert e.value.status == 403
        b.join(gid, "Bob")
        with pytest.raises(ApiError):
            b.join(gid, "Alice")
        assert creator.start(gid)["started"] is True  # the creator token survived too
        assert wait_until(lambda: a.game()["status"] == "running")
        assert a.submit_orders([])["turn"] == 0
    finally:
        srv2.stop()


def test_corrupt_checkpoint_is_moved_aside(tmp_path, caplog):
    data = tmp_path / "data"
    live = data / "live"
    live.mkdir(parents=True)
    (live / "g7.pkl").write_bytes(b"not a pickle")
    (live / "g7.frames").write_bytes(b"\x00\x00")
    (live / "g8.pkl").write_bytes(pickle.dumps({"format": 999, "game_id": "g8"}))
    (live / "g9.frames").write_bytes(b"orphan")
    mgr = GameManager(str(data))
    try:
        assert mgr.restored == [] and mgr.sessions == {}
        assert not list(live.glob("*.pkl")) and not list(live.glob("*.frames"))
        moved = sorted(p.name.split(".")[0] for p in (live / "corrupt").iterdir())
        assert moved == ["g7", "g7", "g8", "g9"]
        assert int(mgr._next_id()[1:]) > 8  # ids of unusable snapshots are not reused
    finally:
        mgr.shutdown()


def test_no_restore_keeps_files_and_ids_unique(tmp_path):
    data = tmp_path / "data"
    mgr = GameManager(str(data))
    s = mgr.create_game({"max_players": 2, "bots": ["idle"], "turn_timeout": 5})
    gid = s.game_id
    mgr.shutdown()
    mgr2 = GameManager(str(data), restore=False)
    try:
        assert mgr2.sessions == {}
        assert (data / "live" / f"{gid}.pkl").exists()
        assert mgr2._next_id() != gid
    finally:
        mgr2.shutdown()
    mgr3 = GameManager(str(data))
    try:
        assert mgr3.restored == [gid]
    finally:
        mgr3.shutdown()


def test_bot_only_game_resumes_and_finishes(tmp_path):
    data = tmp_path / "data"
    mgr = GameManager(str(data))
    s = mgr.create_game({"max_players": 3, "bots": ["strategist", "economist", "rusher"], "max_turns": 8,
                         "turn_timeout": 0.05, "turn_delay": 0.05})
    gid = s.game_id
    assert wait_until(lambda: s.game.turn >= 3)
    mgr.shutdown()
    turn = s.game.turn
    mgr2 = GameManager(str(data))
    try:
        s2 = mgr2.sessions[gid]
        assert s2.game.turn == turn and len(s2.frames) == turn + 1
        assert all(seat.bot is not None for seat in s2.seats.values())
        assert all(seat.bot_blob for seat in s2.seats.values())  # restored from their pickled state
        assert wait_until(lambda: s2.saved, timeout=30)
        assert not live_files(data, gid)
        replay = json.loads((data / "replays" / f"{gid}.json").read_bytes())
        assert [f["turn"] for f in replay["frames"]] == list(range(9))
    finally:
        mgr2.shutdown()


# ---------------------------------------------------------------- crash (orders lost) and clients
def _load_play_cli(monkeypatch, url: str, home: Path):
    monkeypatch.setenv("AGENTCIV_URL", url)
    monkeypatch.setenv("AGENTCIV_HOME", str(home))
    spec = importlib.util.spec_from_file_location("play_cli_under_test", ROOT / "examples" / "play_cli.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_crash_losing_orders_repeats_the_turn_for_play_cli(tmp_path, monkeypatch, capsys):
    """A kill -9 loses what happened after the last checkpoint: here the orders
    of the current turn and a message. ``next`` then returns at once for the
    replayed turn and ``inbox`` keeps delivering new items."""
    data = tmp_path / "data"
    srv = create_server("127.0.0.1", 0, data_dir=str(data)).start_background()
    port = srv.server_address[1]
    try:
        creator = AgentCivClient(srv.url, retry_seconds=0)
        gid = creator.create_game(max_players=3, turn_timeout=60, turn_delay=0, max_turns=20)
        cli = _load_play_cli(monkeypatch, srv.url, tmp_path / "home")
        cli.cmd_join("Alice", gid)
        mcp = AgentCivMCP(srv.url)
        mcp.join_game(gid, "Carol")
        b = AgentCivClient(srv.url, retry_seconds=0)
        b.join(gid, "Bob")
        assert wait_until(lambda: b.game()["status"] == "running")
        store = LiveStore(data / "live")
        assert wait_until(lambda: store.load_state(gid)["game"].status == "running")
        time.sleep(1.2)  # let the post-start checkpoint settle
        snapshot = (data / "live" / f"{gid}.pkl").read_bytes()
        b.say("p1", "first")  # after the snapshot: lost in the crash
        cli.cmd_inbox("Alice")
        cli.cmd_orders("Alice", "[]")
        mcp.get_state()
        mcp.submit_orders([])
        capsys.readouterr()
    finally:
        srv.stop()
    (data / "live" / f"{gid}.pkl").write_bytes(snapshot)  # as if killed before the next checkpoint
    srv2 = create_server("127.0.0.1", port, data_dir=str(data)).start_background()
    try:
        b.base_url = srv2.url
        assert b.game()["turn"] == 0 and not b.state()["players"][0]["submitted"]
        cli.cmd_next("Alice")  # returns at once: turn 0 again, orders lost
        out = capsys.readouterr().out
        assert "turn 0 is being played again" in out
        b.say("p1", "second")
        cli.cmd_inbox("Alice")
        assert "second" in capsys.readouterr().out
        text = mcp.get_state()
        assert "orders for turn 0 were lost" in text
        assert "orders for turn 0 were lost" not in mcp.get_state()
        mcp.submit_orders([])
        cli.cmd_orders("Alice", "[]")
        b.submit_orders([])
        assert wait_until(lambda: b.game()["turn"] == 1)
        cli.cmd_next("Alice")
        out = capsys.readouterr().out
        assert out and "played again" not in out
    finally:
        srv2.stop()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_client_retries_through_a_short_outage(tmp_path):
    data = tmp_path / "data"
    srv = create_server("127.0.0.1", 0, data_dir=str(data)).start_background()
    port = srv.server_address[1]
    notes = []
    c = AgentCivClient(srv.url, retry_seconds=30, on_retry=notes.append)
    gid = c.create_game(max_players=2, bots=["idle"], turn_timeout=60, turn_delay=0)
    c.join(gid, "Alice")
    assert wait_until(lambda: c.game()["status"] == "running")
    srv.stop()
    box = {}

    def restart():
        time.sleep(1.0)
        box["srv"] = create_server("127.0.0.1", port, data_dir=str(data)).start_background()

    th = threading.Thread(target=restart)
    th.start()
    try:
        t0 = time.monotonic()
        view = c.state()  # connection refused for ~1 s, then succeeds
        assert time.monotonic() - t0 >= 0.5
        assert view["you"]["name"] == "Alice" and view["status"] == "running"
        assert notes and "retrying" in notes[0]
        assert c.submit_orders([])["accepted"] == 0
        # 4xx errors are not retried
        t0 = time.monotonic()
        with pytest.raises(ApiError) as e:
            c.game("nope")
        assert e.value.status == 404 and time.monotonic() - t0 < 1.0
    finally:
        th.join()
        if "srv" in box:
            box["srv"].stop()


def test_client_gives_up_after_retry_seconds_and_env_default(monkeypatch):
    port = _free_port()  # nothing listens here
    c = AgentCivClient(f"http://127.0.0.1:{port}", retry_seconds=1.0)
    t0 = time.monotonic()
    with pytest.raises(OSError):
        c.bots()
    assert 0.3 < time.monotonic() - t0 < 5
    monkeypatch.setenv("AGENTCIV_RETRY_SECONDS", "7")
    assert AgentCivClient("http://x").retry_seconds == 7.0
    monkeypatch.delenv("AGENTCIV_RETRY_SECONDS")
    assert AgentCivClient("http://x").retry_seconds == 600.0
    assert AgentCivClient("http://x", retry_seconds=0).retry_seconds == 0.0


def test_run_bot_rides_out_a_restart(tmp_path):
    data = tmp_path / "data"
    srv = create_server("127.0.0.1", 0, data_dir=str(data)).start_background()
    port = srv.server_address[1]
    gid = AgentCivClient(srv.url).create_game(max_players=2, bots=["economist"], turn_timeout=5, turn_delay=0,
                                              max_turns=12)
    out = {}
    c = AgentCivClient(srv.url, retry_seconds=60)
    th = threading.Thread(target=lambda: out.update(run_bot("economist", client=c, game_id=gid,
                                                            name="Remote")))
    th.start()
    assert wait_until(lambda: srv.manager.get(gid).game.turn >= 3)
    srv.stop()
    time.sleep(0.5)
    srv2 = create_server("127.0.0.1", port, data_dir=str(data)).start_background()
    try:
        th.join(60)
        assert not th.is_alive()
        assert out["game_id"] == gid and out["result"] is not None
        assert wait_until(lambda: (data / "replays" / f"{gid}.json").exists())
    finally:
        srv2.stop()
