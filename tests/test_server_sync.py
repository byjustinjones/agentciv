"""Synchronous turn mode (docs/DESIGN.md §13.6): each turn is N negotiation
rounds with a barrier, then an orders phase. Diplomacy is queued and applied
at the barrier in the turn's rotating order, so arrival order never matters."""
from __future__ import annotations

import json
import logging
import pickle
from unittest.mock import patch

import pytest

from agentciv.bots.base import Bot
from agentciv.server import create_server
from agentciv.server.manager import ApiError, GameManager, GameSession, parse_game_options

from test_server import call, wait_until


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def no_workers():
    """Drive sessions synchronously (no worker threads)."""
    with patch.object(GameSession, "launch", lambda self: None):
        yield


def sync_session(tmp_path, n=3, rounds=3, seed=7, sub="data", **opts):
    m = GameManager(str(tmp_path / sub), restore=False)
    s = m.create_game({"max_players": n, "turn_timeout": 0, "rated": False, "seed": seed, "sync": True,
                       "negotiation_rounds": rounds, **opts})
    for i in range(n - len(opts.get("bots", []))):
        s.join(f"agent{i}")
    assert s.status == "running"
    return m, s


def view(s, pid):
    return json.loads(s.state_bytes(pid))


def barrier(s):
    with s.cond:
        s._sync_barrier()


def remote_pids(s):
    return [p for p, seat in s.seats.items() if not seat.is_bot]


# ---------------------------------------------------------------- options
def test_options_defaults_and_validation():
    o = parse_game_options({"sync": True})
    assert o["sync"] is True and o["negotiation_rounds"] == 3
    o = parse_game_options({})
    assert o["sync"] is False and o["negotiation_rounds"] is None
    assert parse_game_options({"sync": True, "negotiation_rounds": 0})["negotiation_rounds"] == 0
    for bad in ({"negotiation_rounds": 2}, {"sync": True, "negotiation_rounds": 11}, {"sync": "yes"},
                {"sync": True, "negotiation_rounds": 1.5}):
        with pytest.raises(ApiError) as e:
            parse_game_options(bad)
        assert e.value.status == 400


def test_phase_in_view_summary_and_live_untouched(tmp_path, no_workers):
    m, s = sync_session(tmp_path, n=2, rounds=2)
    p1, p2 = remote_pids(s)
    ph = view(s, p1)["phase"]
    assert ph["kind"] == "negotiate" and ph["round"] == 1 and ph["of"] == 2
    assert ph["you_done"] is False and ph["queued"] == [] and ph["results"] == [] and ph["waiting"] == [p1, p2]
    summ = s.summary()
    assert summ["sync"] is True and summ["negotiation_rounds"] == 2 and summ["phase"]["kind"] == "negotiate"
    assert "you_done" not in summ["phase"]
    # a live game: no phase anywhere, summary says sync false
    live = m.create_game({"max_players": 2, "turn_timeout": 0, "rated": False})
    a = live.join("x").pid
    live.join("y")
    assert "phase" not in view(live, a)
    assert live.summary()["sync"] is False and live.summary()["negotiation_rounds"] is None
    assert "phase" not in live.summary() and "phase" not in live.wait(None, 0)
    assert "phase" not in live.inbox(a, None, 0)


# ---------------------------------------------------------------- queueing and barriers
def test_queued_actions_are_invisible_until_the_barrier(tmp_path, no_workers):
    m, s = sync_session(tmp_path, n=3)
    p1, p2, p3 = remote_pids(s)
    seq0 = s.game.diplomacy_seq
    r = s.diplomacy(p1, {"actions": [{"type": "propose", "to": p2, "give": {"wood": 10}, "get": {"gold": 5}},
                                     {"type": "say", "to": "all", "text": "hello"}]})
    assert r["ok"] and r["queued"] == 2 and r["done"] is False
    assert [x["status"] for x in r["results"]] == ["queued", "queued"]
    assert s.game.diplomacy_seq == seq0
    # nobody else sees anything: no deal, no message, no inbox item, not even that p1 queued something
    for pid in (p2, p3):
        v = view(s, pid)
        assert v["deals"]["open"] == [] and v["phase"]["queued"] == [] and v["phase"]["done"] == []
        assert s.inbox(pid, 0, 0)["items"] == []
    assert json.loads(s.state_bytes(None))["deals"]["open"] == []
    assert view(s, p1)["phase"]["queued"][0]["type"] == "propose"
    # marking done is visible (who is done), the content is not
    s.diplomacy(p1, {"done": True})
    assert view(s, p2)["phase"]["done"] == [p1] and view(s, p2)["phase"]["waiting"] == [p2, p3]
    barrier(s)
    v2 = view(s, p2)
    assert v2["phase"]["round"] == 2 and [d["to"] for d in v2["deals"]["open"]] == [p2]
    res = view(s, p1)["phase"]["results"]
    assert res[0]["round"] == 1 and res[0]["results"][0]["ok"] and res[0]["results"][0]["deal"]
    assert res[0]["results"][0]["action"]["type"] == "propose"
    assert view(s, p2)["phase"]["results"] == []
    assert any(i["type"] == "deal_proposed" for i in s.inbox(p2, seq0, 0)["items"])


def test_malformed_actions_are_refused_when_queued(tmp_path, no_workers):
    m, s = sync_session(tmp_path, n=2)
    p1, p2 = remote_pids(s)
    r = s.diplomacy(p1, [{"type": "propose", "to": p2}, {"type": "nope"}, {"type": "say", "to": p2, "text": "hi"}])
    assert not r["ok"] and r["queued"] == 1
    assert [x["ok"] for x in r["results"]] == [False, False, True]
    assert r["results"][0].get("example") and r["results"][1].get("hint")
    # a state error (unknown deal) is only found at the barrier, and reported to the seat there
    s.diplomacy(p2, {"actions": [{"type": "accept", "deal": "d99"}], "done": True})
    s.diplomacy(p1, {"done": True})
    barrier(s)
    res = view(s, p2)["phase"]["results"][0]["results"][0]
    assert res["ok"] is False and "d99" in res["error"] and res["action"] == {"type": "accept", "deal": "d99"}


def _contested(tmp_path, sub, arrival):
    """p1 offers all its wood to both p2 and p3 in round 1; both accept in round 2."""
    m, s = sync_session(tmp_path, n=3, sub=sub)
    p1, p2, p3 = remote_pids(s)
    wood = s.game.player(p1).resources["wood"]
    s.diplomacy(p1, {"actions": [{"type": "propose", "to": p2, "give": {"wood": wood}, "get": {}},
                                 {"type": "propose", "to": p3, "give": {"wood": wood}, "get": {}}], "done": True})
    for p in (p2, p3):
        s.diplomacy(p, {"done": True})
    barrier(s)
    ids = {d["to"]: d["id"] for d in view(s, p1)["deals"]["open"]}
    for p in arrival:
        s.diplomacy(p, {"actions": [{"type": "accept", "deal": ids[p]}], "done": True})
    s.diplomacy(p1, {"done": True})
    barrier(s)
    return s, (p1, p2, p3)


def test_barrier_order_is_independent_of_arrival_order(tmp_path, no_workers):
    s_a, (p1, p2, p3) = _contested(tmp_path, "a", arrival=["p2", "p3"])
    s_b, _ = _contested(tmp_path, "b", arrival=["p3", "p2"])
    assert s_a.game.spectator_view(full=True) == s_b.game.spectator_view(full=True)
    # round index 1 on turn 0: order rotated by 1 -> p2, p3, p1; p2's accept settles first
    assert s_a._sync_results[p2][-1]["results"][0]["status"] == "accepted"
    r3 = s_a._sync_results[p3][-1]["results"][0]
    assert r3["ok"] is False
    assert s_a.game.player(p2).resources["wood"] > s_a.game.player(p3).resources["wood"]
    entry = json.loads(s_a.actions.turn_blobs()[-1])
    assert [b["order"] for b in entry["barriers"]] == [[p1, p2, p3], [p2, p3, p1]]
    assert {d["round"] for d in entry["diplomacy"]} == {1, 2}


def test_orders_closed_while_negotiating_and_diplomacy_closed_in_orders(tmp_path, no_workers):
    m, s = sync_session(tmp_path, n=2, rounds=1)
    p1, p2 = remote_pids(s)
    with pytest.raises(ApiError) as e:
        s.submit(p1, {"orders": []})
    assert e.value.status == 409 and e.value.extra["phase"]["kind"] == "negotiate"
    s.diplomacy(p1, {"done": True})
    with pytest.raises(ApiError) as e:  # done: no more actions this round
        s.diplomacy(p1, [{"type": "say", "to": p2, "text": "late"}])
    assert e.value.status == 409
    assert s.diplomacy(p1, {"done": True})["done"] is True  # idempotent
    phase = s._phase_id
    with pytest.raises(ApiError) as e:
        s.diplomacy(p2, {"actions": [], "done": True, "phase": phase - 1})
    assert e.value.status == 409 and "stale phase" in e.value.message
    s.diplomacy(p2, {"actions": [], "done": True, "phase": phase})
    barrier(s)
    assert view(s, p1)["phase"]["kind"] == "orders" and view(s, p1)["phase"]["round"] is None
    with pytest.raises(ApiError) as e:
        s.diplomacy(p1, [{"type": "say", "to": p2, "text": "hi"}])
    assert e.value.status == 409 and "closed" in e.value.message
    assert s.submit(p1, {"orders": []})["accepted"] == 0
    assert view(s, p2)["phase"]["done"] == [p1]
    s.submit(p2, {"orders": []})
    with s.cond:
        s._advance()
    assert s.game.turn == 1 and view(s, p1)["phase"]["round"] == 1 and view(s, p1)["phase"]["results"] == []


def test_zero_rounds_is_orders_only(tmp_path, no_workers):
    m, s = sync_session(tmp_path, n=2, rounds=0)
    p1, _ = remote_pids(s)
    assert view(s, p1)["phase"]["kind"] == "orders"
    with pytest.raises(ApiError):
        s.diplomacy(p1, {"done": True})


def test_wait_and_inbox_return_on_phase_change(tmp_path, no_workers):
    m, s = sync_session(tmp_path, n=2)
    p1, p2 = remote_pids(s)
    ph = s._phase_id
    w = s.wait(None, 0, since_phase=ph)
    assert w["timed_out"] and w["phase"]["id"] == ph
    assert not s.wait(None, 0, since_phase=ph - 1)["timed_out"]
    s.diplomacy(p1, {"done": True})
    s.diplomacy(p2, {"done": True})
    barrier(s)
    w = s.wait(None, 0, since_phase=ph)
    assert not w["timed_out"] and w["phase"]["round"] == 2
    assert s.inbox(p1, None, 0)["phase"]["round"] == 2


def test_eliminated_seat_never_blocks(tmp_path, no_workers):
    m, s = sync_session(tmp_path, n=3)
    p1, p2, p3 = remote_pids(s)
    s.diplomacy(p1, {"done": True})
    s.diplomacy(p2, {"done": True})
    assert s._round_wait() == float("inf")
    s.game.player(p3).alive = False
    assert s._round_wait() == 0.0


# ---------------------------------------------------------------- house bots
class Chatty(Bot):
    """Says something every round (to see where its batch lands)."""

    def negotiate(self, view):
        return [{"type": "say", "to": "all", "text": f"round from {view['you']['id']}"}]

    def act(self, view):
        return []


def test_house_bots_negotiate_in_their_seat_position(tmp_path, no_workers):
    m, s = sync_session(tmp_path, n=3, bots=["idle"])
    bot_pid = next(p for p, seat in s.seats.items() if seat.is_bot)
    s.seats[bot_pid].bot = Chatty()
    with s.cond:
        s._open_phase(0)  # re-evaluate which bots negotiate now that the bot was swapped
    assert s._bots_done is False
    p1, p2 = remote_pids(s)
    for k in range(3):
        s._sync_bots_negotiate(s.game.turn, s._phase_id)
        assert s._bots_done and bot_pid in view(s, p1)["phase"]["done"]
        assert view(s, p1)["phase"]["queued"] == []
        s.diplomacy(p1, {"actions": [{"type": "say", "to": "all", "text": "a"}], "done": True})
        s.diplomacy(p2, {"actions": [{"type": "say", "to": "all", "text": "b"}], "done": True})
        barrier(s)
    entry = json.loads(s.actions.turn_blobs()[-1])
    for b in entry["barriers"]:
        said = [d["by"] for d in entry["diplomacy"] if d["round"] == b["round"]]
        assert said == b["order"]
    assert [b["order"][0] for b in entry["barriers"]] == ["p1", "p2", "p3"]
    assert view(s, p1)["phase"]["kind"] == "orders" and s._bots_done is False


def test_bot_only_sync_game_finishes(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    try:
        s, r = call(srv, "POST", "/api/games", {"max_players": 3, "bots": ["economist", "turtle", "rusher"],
                                                "max_turns": 4, "turn_timeout": 5, "turn_delay": 0, "rated": False,
                                                "sync": True, "negotiation_rounds": 2})
        assert s == 200
        gid = r["game_id"]
        call(srv, "POST", f"/api/games/{gid}/start", {}, token=r["creator_token"])
        assert wait_until(lambda: call(srv, "GET", f"/api/games/{gid}")[1]["status"] == "finished", timeout=60)
        _, rep = call(srv, "GET", f"/api/games/{gid}/replay")
        turns = rep["actions"]["turns"]
        assert all(len(t["barriers"]) == 2 for t in turns if t.get("end"))
        assert "phase_missed" not in json.dumps(turns)
    finally:
        srv.stop()


# ---------------------------------------------------------------- timeouts (worker threads)
def test_phase_timeout_treats_seat_as_done_and_logs_the_miss(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    try:
        s, r = call(srv, "POST", "/api/games", {"max_players": 2, "turn_timeout": 0.4, "rated": False,
                                                "sync": True, "negotiation_rounds": 2, "max_turns": 3})
        gid = r["game_id"]
        _, a = call(srv, "POST", f"/api/games/{gid}/join", {"name": "fast"})
        _, b = call(srv, "POST", f"/api/games/{gid}/join", {"name": "silent"})
        st, v = call(srv, "GET", f"/api/games/{gid}/state", token=a["token"])
        assert v["phase"]["kind"] == "negotiate"
        # 'fast' queues a deal and ends round 1; 'silent' never answers: the round closes on its limit
        st, d = call(srv, "POST", f"/api/games/{gid}/diplomacy",
                     {"actions": [{"type": "say", "to": b["player_id"], "text": "hi"}], "done": True,
                      "phase": v["phase"]["id"]}, token=a["token"])
        assert st == 200 and d["done"] and d["phase"]["waiting"] == [b["player_id"]]
        st, w = call(srv, "GET", f"/api/games/{gid}/wait?since_phase={v['phase']['id']}&timeout=5")
        assert w["phase"]["round"] == 2
        st, v = call(srv, "GET", f"/api/games/{gid}/state", token=a["token"])
        assert v["phase"]["results"][0]["results"][0]["ok"]
        assert wait_until(lambda: call(srv, "GET", f"/api/games/{gid}")[1]["turn"] >= 1, timeout=10)
        assert wait_until(lambda: call(srv, "GET", f"/api/games/{gid}")[1]["status"] == "finished", timeout=15)
        _, rep = call(srv, "GET", f"/api/games/{gid}/replay")
        t0 = rep["actions"]["turns"][0]
        missed = t0["phase_missed"]
        assert {"pid": b["player_id"], "phase": "negotiate", "round": 1} in missed
        assert {"pid": b["player_id"], "phase": "negotiate", "round": 2} in missed
        assert {"pid": b["player_id"], "phase": "orders", "reason": "no_orders"} in missed
        assert not any(x["pid"] == a["player_id"] and x.get("round") == 1 for x in missed)
        assert t0["missed"][b["player_id"]] == "no_orders"
        assert t0["diplomacy"][0]["round"] == 1
    finally:
        srv.stop()


# ---------------------------------------------------------------- checkpoints
def test_checkpoint_restore_mid_round(tmp_path, no_workers):
    m, s = sync_session(tmp_path, n=2, rounds=2)
    p1, p2 = remote_pids(s)
    s.diplomacy(p1, {"actions": [{"type": "propose", "to": p2, "give": {"wood": 5}, "get": {}}], "done": True})
    s.diplomacy(p2, {"done": True})
    barrier(s)
    deal = view(s, p2)["deals"]["open"][0]["id"]
    s.diplomacy(p2, {"actions": [{"type": "accept", "deal": deal}], "done": True})
    phase_id = s._phase_id
    assert s.checkpoint(force=True, bots_idle=True)
    m2 = GameManager(str(tmp_path / "data"))
    r = m2.sessions[s.game_id]
    assert r._sync and r._phase_index == 1 and r._phase_id == phase_id and r._done == {p2}
    assert [a for a, _ in r._queued[p2]] == [{"type": "accept", "deal": deal}]
    assert r._sync_results[p1][0]["round"] == 1
    assert r.game.deadline is None  # turn_timeout 0: still no limit
    r.diplomacy(p1, {"done": True})
    barrier(r)
    assert r._sync_results[p2][-1]["results"][0]["status"] == "accepted"
    assert view(r, p1)["phase"]["kind"] == "orders"


def test_old_checkpoint_restores_as_live(tmp_path, no_workers):
    m = GameManager(str(tmp_path / "data"), restore=False)
    s = m.create_game({"max_players": 2, "turn_timeout": 0, "rated": False})
    s.join("a")
    s.join("b")
    state = s.snapshot_state()
    for k in ("sync", "negotiation_rounds"):
        state["opts"].pop(k)
    assert "sync" not in state
    r = GameSession.from_snapshot(m, pickle.loads(pickle.dumps(state)), [], 0)
    assert r._sync is False and "phase" not in r.summary() and r.summary()["sync"] is False


def test_sync_snapshot_without_phase_state_starts_the_turn_over(tmp_path, no_workers):
    m, s = sync_session(tmp_path, n=2)
    state = s.snapshot_state()
    state.pop("sync")
    r = GameSession.from_snapshot(m, state, [], 0)
    assert r._sync and r._phase_index == 0 and r._queued == {}


# ---------------------------------------------------------------- fog, quickmatch, HTTP
def test_fog_sync_game(tmp_path, no_workers):
    m, s = sync_session(tmp_path, n=2, rounds=1, fog=True)
    p1, p2 = remote_pids(s)
    s.diplomacy(p1, {"actions": [{"type": "propose", "to": p2, "give": {"wood": 5}, "get": {"gold": 1}}],
                     "done": True})
    assert view(s, p2)["deals"]["open"] == []
    s.diplomacy(p2, {"done": True})
    barrier(s)
    assert view(s, p2)["deals"]["open"][0]["from"] == p1 and view(s, p2)["phase"]["kind"] == "orders"


def test_quickmatch_never_mixes_sync_and_live(tmp_path, no_workers):
    m = GameManager(str(tmp_path / "data"), restore=False)
    live, _ = m.quickmatch({"name": "a", "players": 3})
    sync, _ = m.quickmatch({"name": "b", "players": 3, "sync": True})
    sync2, _ = m.quickmatch({"name": "c", "players": 3, "sync": True})
    sync4, _ = m.quickmatch({"name": "d", "players": 3, "sync": True, "negotiation_rounds": 4})
    live2, _ = m.quickmatch({"name": "e", "players": 3})
    assert live is live2 and sync is sync2 and len({live.game_id, sync.game_id, sync4.game_id}) == 3
    assert sync.opts["sync"] and sync.opts["negotiation_rounds"] == 3 and sync4.opts["negotiation_rounds"] == 4
    assert live.opts["match"] == (3, 30.0, live.opts["max_turns"], 30.0, True, False)


def test_http_round_trip(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    try:
        _, r = call(srv, "POST", "/api/games", {"max_players": 2, "turn_timeout": 0, "rated": False, "sync": True,
                                                "negotiation_rounds": 1})
        gid = r["game_id"]
        _, a = call(srv, "POST", f"/api/games/{gid}/join", {"name": "a"})
        _, b = call(srv, "POST", f"/api/games/{gid}/join", {"name": "b"})
        st, e = call(srv, "POST", f"/api/games/{gid}/orders", {"orders": []}, token=a["token"])
        assert st == 409 and e["phase"]["kind"] == "negotiate"
        for c in (a, b):
            assert call(srv, "POST", f"/api/games/{gid}/diplomacy", {"done": True}, token=c["token"])[0] == 200
        assert wait_until(lambda: call(srv, "GET", f"/api/games/{gid}")[1]["phase"]["kind"] == "orders")
        st, e = call(srv, "POST", f"/api/games/{gid}/diplomacy", {"done": True}, token=a["token"])
        assert st == 409
        for c in (a, b):
            assert call(srv, "POST", f"/api/games/{gid}/orders", {"orders": []}, token=c["token"])[0] == 200
        assert wait_until(lambda: call(srv, "GET", f"/api/games/{gid}")[1]["turn"] == 1)
        g = call(srv, "GET", f"/api/games/{gid}")[1]
        assert g["phase"]["kind"] == "negotiate" and g["sync"] is True
    finally:
        srv.stop()
