"""Diagnostic positions (docs/PUZZLES.md): reference solutions, baselines,
determinism, the server hook, checkpoints and the leaderboards."""
from __future__ import annotations

import json
import logging
import pickle
import time
from unittest.mock import patch

import pytest

from agentciv.bots import get_bot
from agentciv.client import AgentCivClient, ApiError, run_bot
from agentciv.puzzles import PUZZLES, get_puzzle, new_game
from agentciv.puzzles.__main__ import main as cli_main
from agentciv.puzzles.base import PuzzleGame
from agentciv.puzzles.contracts import BEST
from agentciv.puzzles.runner import run_puzzle
from agentciv.server import create_server
from agentciv.server.manager import GameManager, GameSession
from test_neutral_text import ADVICE

IDS = sorted(PUZZLES)


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


def wait_until(pred, timeout=30.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


# ---------------------------------------------------------------- offline
@pytest.mark.parametrize("pid", IDS)
def test_reference_solution_scores_high_and_baseline_low(pid):
    pz = get_puzzle(pid)
    sol = run_puzzle(pid, pz.solution())
    base = run_puzzle(pid, pz.baseline())
    assert sol["score"] >= 90, sol["explanation"]
    assert base["score"] <= 20, base["explanation"]
    for r in (sol, base):
        assert r["game"].status == "finished" and r["explanation"]
        assert r["turns"] <= pz.horizon


@pytest.mark.parametrize("pid", IDS)
def test_same_play_same_score(pid):
    """Two runs of the same solver give the same score, explanation and final state."""
    a = run_puzzle(pid, get_bot("strategist", 3))
    b = run_puzzle(pid, get_bot("strategist", 3))
    assert (a["score"], a["explanation"]) == (b["score"], b["explanation"])
    assert a["game"].spectator_view(full=True) == b["game"].spectator_view(full=True)


@pytest.mark.parametrize("pid", IDS)
def test_position_and_puzzle_block(pid):
    pz = get_puzzle(pid)
    g = new_game(pid)
    me = pz.solver_pid()
    assert g.status == "running" and g.turn == pz.start_turn and g.max_turns == pz.max_turns
    assert g.players[-1].id == me and len(g.players) == len(pz.roles)
    view = g.player_view(me)
    block = view["puzzle"]
    assert block == g.spectator_view()["puzzle"]
    assert {k: block[k] for k in ("puzzle", "title", "objective", "horizon", "scoring")} == \
        {k: pz.info()[k] for k in ("puzzle", "title", "objective", "horizon", "scoring")}
    assert block["solver"] == me and block["score"] is None and block["explanation"] is None
    assert block["last_turn"] == pz.start_turn + pz.horizon - 1
    # the agent-facing text states the objective and the formula, no advice
    text = (block["objective"] + " " + block["scoring"]).lower()
    assert not [p for p in ADVICE if p in text]
    # a fresh game is the same position every time
    assert new_game(pid).spectator_view(full=True) == g.spectator_view(full=True)


def test_puzzle_game_pickles_mid_game_and_finishes_alike():
    """A checkpoint (pickle) of a puzzle game in progress, opponent included, ends with the same score."""
    pid = "stop-victory"
    pz = get_puzzle(pid)
    g = new_game(pid)
    rival = pz.opponent("rival")
    sol = pz.solution()

    def turn(game, bot):
        game.submit_orders("p1", bot.act(game.player_view("p1")))
        game.submit_orders("p2", sol.act(game.player_view("p2")))
        game.step()

    turn(g, rival)
    g2, rival2 = pickle.loads(pickle.dumps((g, rival)))
    for game, bot in ((g, rival), (g2, rival2)):
        while game.status == "running":
            turn(game, bot)
    assert g.puzzle_outcome == g2.puzzle_outcome and g.puzzle_outcome["score"] == 100


def test_contract_best_set_is_the_documented_one():
    best, value = get_puzzle("contracts").best()
    assert best == BEST and value == get_puzzle("contracts").value(BEST)


def test_contract_scores_follow_the_valuation():
    """A trap offer scores 0; half of the best set scores 50; the score ignores everything but accepts."""
    pz = get_puzzle("contracts")

    class Accept:
        def __init__(self, *terms):
            self.terms = list(terms)

        def negotiate(self, view):
            return [{"type": "accept", "deal": d["id"]} for d in view["deals"]["open"]
                    if (d["give"], d["get"]) in self.terms]

        def act(self, view):
            return [{"type": "market", "side": "sell", "resource": "wood", "qty": 30}]  # not scored

    o1 = ({"gold": 120}, {"food": 100})
    o5 = ({"gold": 150}, {"stone": 60})
    assert run_puzzle("contracts", Accept(o1))["score"] == 50
    assert run_puzzle("contracts", Accept(o1, o5))["score"] == 0
    assert pz.value(("O5",)) < pz.value(())


def test_winter_counts_lost_units_and_partial_builds():
    pz = get_puzzle("winter")
    g = new_game("winter")
    g.player("p1").resources.update(food=0)
    while g.status == "running":
        g.step()
    score, why = g.puzzle_outcome["score"], g.puzzle_outcome["explanation"]
    assert score <= 3 and "starved" in why
    assert pz.score(g)[0] == score


def test_cli_list_and_run(capsys):
    assert cli_main(["list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["puzzle"] for r in rows] == list(PUZZLES)
    assert cli_main(["run", "stop-victory", "--solution", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["score"] == 100 and out["solver"] == "solution"
    assert cli_main(["run", "market", "--bot", "idle"]) == 0
    assert "market / bot idle" in capsys.readouterr().out


# ---------------------------------------------------------------- server
@pytest.fixture
def no_workers():
    """Drive the session lifecycle synchronously (no worker threads)."""
    with patch.object(GameSession, "launch", lambda self: None):
        yield


@pytest.mark.parametrize("body,needle", [
    ({"puzzle": "nope"}, "unknown puzzle"),
    ({"puzzle": "winter", "bots": ["economist"]}, "remove ['bots']"),
    ({"puzzle": "winter", "seed": 3}, "remove ['seed']"),
    ({"puzzle": "winter", "max_turns": 150}, "remove ['max_turns']"),
    ({"puzzle": "winter", "rated": True}, "always unrated"),
    ({"puzzle": "winter", "fog": True}, "no fog"),
])
def test_conflicting_options_are_refused(tmp_path, no_workers, body, needle):
    from agentciv.server.manager import ApiError as ServerError
    m = GameManager(str(tmp_path), restore=False)
    with pytest.raises(ServerError) as e:
        m.create_game(body)
    assert e.value.status == 400 and needle in e.value.message
    m.shutdown()


def test_puzzle_game_is_unrated_starts_on_join_and_keeps_its_replay(tmp_path, no_workers):
    """Unrated ("puzzle") even with open ratings; starts when its one remote seat joins; the
    finished summary and replay carry the score, identical before and after the replay is saved;
    the leaderboards are untouched."""
    m = GameManager(str(tmp_path), open_ratings=True, restore=False)
    s = m.create_game({"puzzle": "market", "fog": False, "rated": False, "name": "try it"})
    assert s.opts["rated"] is False and s.opts["unrated_reason"] == "puzzle"
    assert s.opts["turn_timeout"] == 300.0 and s.status == "lobby" and s.name == "try it"
    seat = s.join("Solver")
    assert s.status == "running" and seat.pid == "p1" and s.game.turn == 6
    sol = get_puzzle("market").solution()
    while s.status == "running":
        s.submit(seat.pid, {"orders": sol.act(s.game.player_view(seat.pid)), "turn": s.game.turn})
        with s.cond:
            s._advance()
    summary = s.summary()
    assert summary["puzzle"]["puzzle"] == "market" and summary["puzzle"]["score"] == 100
    assert summary["puzzle"]["explanation"] and summary["rating"] is None and summary["frames"] == 9
    s.checkpoint(force=True, bots_idle=True)
    live = s.replay_bytes()
    rep = json.loads(live)
    assert [f["turn"] for f in rep["frames"]] == list(range(6, 15))
    assert rep["frames"][-1]["puzzle"]["score"] == 100 and rep["summary"]["puzzle"]["score"] == 100
    assert {e["turn"] for e in rep["actions"]["turns"]} == set(range(6, 14))
    part = json.loads(s.replay_bytes(lo=1, hi=2))  # frames 1-2 = turns 7-8, with their actions
    assert [f["turn"] for f in part["frames"]] == [7, 8]
    assert [e["turn"] for e in part["actions"]["turns"]] == [7, 8]
    assert s._finalize()
    assert s.replay_bytes() == live == m.storage.read_replay(s.game_id)
    assert s.summary()["frames"] == 9
    archived = json.loads(m.archived_replay_bytes(s.game_id, False, 1, 2))
    assert archived["actions"] == part["actions"] and archived["frames"] == part["frames"]
    for pool in ("standard", "fog"):
        assert m.storage.leaderboard(pool) == []
    m.shutdown()


def _server_play(srv, pid: str, solver, name: str = "Solver") -> tuple[str, dict]:
    c = AgentCivClient(srv.url, retry_seconds=0)
    gid = c.create_game(puzzle=pid, turn_timeout=60)
    run_bot(solver, srv.url, game_id=gid, name=name, negotiate_window=0.2)
    assert wait_until(lambda: c.game(gid)["status"] == "finished")
    return gid, c.game(gid)


@pytest.mark.parametrize("pid", ["contracts", "stop-victory"])
def test_reference_solution_and_baseline_through_the_server(tmp_path, pid):
    """The Python client plays a puzzle like any game; the server's score equals the offline one."""
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    try:
        pz = get_puzzle(pid)
        for solver, offline in ((pz.solution(), run_puzzle(pid, pz.solution())),
                                (pz.baseline(), run_puzzle(pid, pz.baseline()))):
            gid, summary = _server_play(srv, pid, solver)
            assert summary["puzzle"]["score"] == offline["score"]
            assert summary["puzzle"]["explanation"] == offline["explanation"]
            assert summary["unrated_reason"] == "puzzle" and summary["rating"] is None
            assert summary["players"][-1]["name"] == "Solver"
            assert all(p["bot"].startswith(f"puzzle:{pid}:") for p in summary["players"][:-1])
        c = AgentCivClient(srv.url, retry_seconds=0)
        assert c.leaderboard() == [] and c.leaderboard("fog") == []
        with pytest.raises(ApiError) as e:
            c.create_game(puzzle=pid, max_players=3)
        assert e.value.status == 400
    finally:
        srv.stop()


def test_puzzle_survives_a_server_restart_mid_game(tmp_path):
    """Checkpoint/restore mid-puzzle: the restored game (stateful opponent included) ends
    with the score the same play gets offline."""
    data = tmp_path / "data"
    pid = "stop-victory"
    sol = get_puzzle(pid).solution()
    srv = create_server("127.0.0.1", 0, data_dir=str(data)).start_background()
    try:
        c = AgentCivClient(srv.url, retry_seconds=0)
        gid = c.create_game(puzzle=pid, turn_timeout=60, turn_delay=0)
        me = AgentCivClient(srv.url, retry_seconds=0)
        me.join(gid, "Solver")
        assert wait_until(lambda: me.game()["status"] == "running")
        view = me.state()
        me.submit_orders(sol.act(view), turn=view["turn"])
        assert wait_until(lambda: me.game()["turn"] == 41)
        assert wait_until(lambda: me.state()["players"][0]["submitted"])  # the rival has acted
        before = me.state()
    finally:
        srv.stop()
    srv2 = create_server("127.0.0.1", 0, data_dir=str(data)).start_background()
    try:
        me.base_url = srv2.url
        assert srv2.manager.restored == [gid]
        assert wait_until(lambda: me.state()["players"][0]["submitted"])
        after = me.state()
        for v in (before, after):
            v.pop("deadline", None)
        assert after == before and isinstance(srv2.manager.get(gid).game, PuzzleGame)
        while True:
            view = me.state()
            if view["status"] == "finished":
                break
            t = view["turn"]
            me.submit_orders(sol.act(view), turn=t)
            assert wait_until(lambda: me.game()["turn"] > t or me.game()["status"] == "finished")
        summary = me.game()
        assert summary["puzzle"]["score"] == run_puzzle(pid, sol)["score"] == 100
        assert wait_until(lambda: (data / "replays" / f"{gid}.json").exists())
        replay = json.loads((data / "replays" / f"{gid}.json").read_bytes())
        assert replay["summary"]["puzzle"]["score"] == 100
        assert [f["turn"] for f in replay["frames"]] == list(range(40, 40 + len(replay["frames"])))
        assert AgentCivClient(srv2.url).leaderboard() == []
    finally:
        srv2.stop()
