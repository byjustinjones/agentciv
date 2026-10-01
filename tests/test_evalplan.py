"""python -m agentciv.evalplan: rotation plans, game creation and the paired
report (docs/EVALUATION.md), on synthetic replays with known answers, on a
live server, and read-only on the real replays in ~/agentciv/data."""
from __future__ import annotations

import io
import json
import logging
import os
import re
from collections import Counter
from pathlib import Path

import pytest

from agentciv import evalplan
from agentciv.evalplan import EvalError, Source, build_report, format_report, game_rows, make_plan
from agentciv.server import create_server, tracks
from agentciv.server.tracks import Track

from test_server import call, wait_until
from test_tracks import KEY, play_turn, state

MODELS = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta"]


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


# ---------------------------------------------------------------- plan
def test_full_rotation_puts_every_model_in_every_seat_on_every_seed():
    plan = make_plan("eval-6p-fog-v1", MODELS, seeds=3, seed_base=100)
    assert len(plan["games"]) == 18 and plan["seeds"] == [100, 101, 102] and plan["rotations"] == list(range(6))
    for seed in plan["seeds"]:
        games = [g for g in plan["games"] if g["seed"] == seed]
        cover = Counter((name, k) for g in games for k, name in enumerate(g["seats"]))
        assert set(cover) == {(m, k) for m in MODELS for k in range(6)} and set(cover.values()) == {1}
    g = plan["games"][7]
    assert g["body"] == {"track": "eval-6p-fog-v1", "name": g["body"]["name"], "seed": g["seed"],
                         "seats": g["seats"]}
    # the title is public while the game is live: it must not carry the hidden seed or the rotation
    names = [x["body"]["name"] for x in plan["games"]]
    assert len(set(names)) == 18 and all(re.fullmatch(r"eval-6p-fog-v1 [0-9a-f]{8}", n) for n in names)
    assert not any("seed" in n or "rotation" in n for n in names)
    # ... and neither does the order in which the games are created
    assert sorted(plan["create_order"]) == list(range(18))
    orders = {tuple(make_plan("eval-6p-fog-v1", MODELS, seeds=3)["create_order"]) for _ in range(5)}
    assert len(orders) > 1
    assert all(sorted(g["seats"]) == sorted(MODELS) and g["game_id"] is None for g in plan["games"])


def test_capped_rotations_and_repeats():
    plan = make_plan("eval-6p-fog-v1", MODELS, seeds=2, rotations=2)
    assert plan["rotations"] == [0, 3] and len(plan["games"]) == 4
    assert evalplan.rotation_shifts(6, 4) == [0, 1, 3, 4]
    with pytest.raises(EvalError, match="allow-repeats"):
        make_plan("eval-6p-fog-v1", MODELS[:4], seeds=1)
    plan = make_plan("eval-6p-fog-v1", MODELS[:4], seeds=1, allow_repeats=True)
    assert plan["lineup"] == MODELS[:4] + ["Alpha#2", "Beta#2"]
    cover = Counter((n, k) for g in plan["games"] for k, n in enumerate(g["seats"]))
    assert all(cover[("Alpha#2", k)] == 1 for k in range(6))
    with pytest.raises(EvalError, match="split them"):
        make_plan("eval-6p-fog-v1", MODELS + ["Eta"], seeds=1)
    with pytest.raises(EvalError, match="distinct"):
        make_plan("eval-6p-fog-v1", ["A", "a", "B", "C", "D", "E"], seeds=1)
    with pytest.raises(EvalError, match="reserved"):
        make_plan("eval-6p-fog-v1", ["Player 1", "B", "C", "D", "E", "F"], seeds=1)
    with pytest.raises(EvalError, match="unknown track"):
        make_plan("nope-v1", MODELS, seeds=1)
    assert len(make_plan("nope-v1", MODELS[:3], seeds=1, seats=3)["games"]) == 3


def test_cli_plan_writes_the_file(tmp_path, capsys):
    out = tmp_path / "plan.json"
    assert evalplan.main(["plan", "--track", "eval-6p-fog-v1", "--models", ",".join(MODELS), "--seeds", "2",
                          "--rotations", "3", "-o", str(out)]) == 0
    plan = json.loads(out.read_text())
    assert plan["format"] == 1 and len(plan["games"]) == 6
    assert evalplan.main(["plan", "--track", "eval-6p-fog-v1", "--models", "A,B", "--seeds", "1",
                          "-o", str(out)]) == 2
    assert "allow-repeats" in capsys.readouterr().err


# ---------------------------------------------------------------- synthetic replays
def replay(gid, seed, names, placements, scores, *, condition="score", agents=None, rules="r" * 64,
           actions=None, track="test-3p-v1"):
    pids = [f"p{k + 1}" for k in range(len(names))]
    players = []
    for pid, name in zip(pids, names):
        p = {"id": pid, "name": name, "seat_name": f"Player {pid[1:]}", "is_bot": False, "alive": True}
        if agents and name in agents:
            p["agent"] = agents[name]
        players.append(p)
    doc = {"game_id": gid,
           "summary": {"game_id": gid, "status": "finished", "players": players, "seed": seed, "track": track,
                       "rules_sha256": rules},
           "result": {"winner": placements[0], "condition": condition, "placements": placements, "scores": scores},
           "frames": [{"turn": 0, "players": [{"id": p, "name": f"Player {p[1:]}", "alive": True,
                                               "eliminated_turn": None} for p in pids]}]}
    if actions is not None:
        doc["actions"] = actions
    return doc


def agent(model, harness="h1", notes=None):
    a = {"model": model, "harness": harness}
    if notes:
        a["notes"] = notes
    return a


def write_synthetic(tmp_path, g2_rules="r" * 64):
    """Seed 1, three seats, models Alpha/Beta/Gamma in a full rotation; the third game never finished.

    g1 seats Alpha, Beta, Gamma:  Alpha 1st, Beta 2nd, Gamma 3rd
    g2 seats Gamma, Alpha, Beta:  Alpha 1st, Gamma and Beta tied for 2nd (2.5 each)
    g3 seats Beta, Gamma, Alpha:  not finished (no replay)"""
    d = tmp_path / "data" / "replays"
    d.mkdir(parents=True)
    agents1 = {"Alpha": agent("alpha-model-1"), "Beta": agent("beta-model-1", notes="refusal fallbacks on"),
               "Gamma": agent("gamma-model-1")}
    agents2 = {**agents1, "Gamma": agent("gamma-model-1", harness="h2")}
    actions = {"format": 1, "turns": [
        {"turn": 0, "orders": {}, "end": "deadline", "missed": {"p3": "no_orders"},
         "phase_missed": [{"pid": "p3", "phase": "negotiate", "round": 1}, {"pid": "p3", "phase": "orders",
                                                                           "reason": "no_orders"}],
         "diplomacy": [{"by": "p2", "t": 1.0, "round": 1,
                        "action": {"type": "say", "to": "all", "text": "Hi, ALPHA-model-1 here, peace?"}},
                       {"by": "p1", "t": 1.0, "round": 1, "action": {"type": "say", "to": "all", "text": "hello"}}]},
    ]}
    docs = [
        replay("g1", 1, ["Alpha", "Beta", "Gamma"], ["p1", "p2", "p3"], {"p1": 30, "p2": 20, "p3": 10},
               agents=agents1, actions={"format": 1, "turns": []}),
        replay("g2", 1, ["Gamma", "Alpha", "Beta"], ["p2", "p1", "p3"], {"p1": 20, "p2": 30, "p3": 20},
               agents=agents2, actions=actions, rules=g2_rules),
    ]
    for doc in docs:
        (d / f"{doc['game_id']}.json").write_text(json.dumps(doc))
    plan = make_plan("test-3p-v1", ["Alpha", "Beta", "Gamma"], seeds=1, seats=3)
    for g, gid in zip(plan["games"], ["g1", "g2", "g3"]):
        g["game_id"] = gid
    assert [g["seats"] for g in plan["games"]] == [["Alpha", "Beta", "Gamma"], ["Gamma", "Alpha", "Beta"],
                                                   ["Beta", "Gamma", "Alpha"]]
    return tmp_path / "data", plan


def test_report_on_synthetic_replays_has_the_known_answers(tmp_path):
    data, plan = write_synthetic(tmp_path)
    rep = build_report(Source(data_dir=data), plan, nboot=500)
    assert rep["games"]["planned"] == 3 and rep["games"]["finished"] == 2 and rep["games"]["missing"] == 1
    assert rep["games"]["unfinished"] == [{"game_id": "g3", "why": "no replay file"}]
    m = rep["models"]
    assert m["Alpha"]["placement"]["mean"] == 1.0 and m["Alpha"]["win_rate"]["mean"] == 1.0
    assert m["Beta"]["placement"]["mean"] == 2.25 and m["Gamma"]["placement"]["mean"] == 2.75
    assert m["Beta"]["win_rate"]["mean"] == 0.0 and m["Gamma"]["score"]["mean"] == 15.0
    assert m["Alpha"]["placement"]["ci95"] == [1.0, 1.0]
    lo, hi = m["Beta"]["placement"]["ci95"]
    assert lo == 2.0 and hi == 2.5
    pairs = {(p["a"], p["b"]): p for p in rep["paired"]}
    assert pairs[("Alpha", "Beta")]["pairs"] == 1 and pairs[("Alpha", "Beta")]["mean_diff"] == -1.0
    assert pairs[("Alpha", "Gamma")]["mean_diff"] == -1.5 and pairs[("Beta", "Gamma")]["mean_diff"] == -0.5
    assert pairs[("Alpha", "Beta")]["ci95"] == [None, None]   # one pair: no interval
    assert rep["seats"] == {"1": {"mean": 1.75, "ci95": rep["seats"]["1"]["ci95"], "n": 2},
                            "2": {"mean": 1.5, "ci95": rep["seats"]["2"]["ci95"], "n": 2},
                            "3": {"mean": 2.75, "ci95": rep["seats"]["3"]["ci95"], "n": 2}}
    assert rep["fields"] == [["Alpha", "Beta", "Gamma"]] and rep["by_field"] == []
    w = "\n".join(rep["warnings"])
    assert "Gamma: the agent manifest differs between games" in w
    assert "Beta in g1: the manifest notes say answers may come from another model" in w
    assert "different rules" not in w and len(rep["groups"]) == 1 and rep["groups"][0]["games"] == 2
    assert "Beta: missed 1 turn deadline(s) and 2 phase limit(s) (1 in negotiation)" in w
    assert "g2 turn 0: Alpha (p2) names itself (Alpha, alpha-model-1) in a message" in w
    assert "hello" not in w
    assert rep["deadlines"]["Beta"] == {"missed_turns": 1, "phase_missed": 2, "negotiate_missed": 1}
    text = format_report(rep)
    assert "3 games planned, 2 finished, 1 missing" in text and "Alpha vs Beta: -1.00" in text
    # deterministic: the same input gives the same report
    assert json.dumps(build_report(Source(data_dir=data), plan, nboot=500)) == json.dumps(rep)


def test_games_under_different_rules_are_not_paired(tmp_path):
    data, plan = write_synthetic(tmp_path, g2_rules="s" * 64)
    rep = build_report(Source(data_dir=data), plan, nboot=200)
    assert all(p["pairs"] == 0 and p["mean_diff"] is None for p in rep["paired"])
    assert len(rep["groups"]) == 2 and "different rules" in "\n".join(rep["warnings"])
    text = format_report(rep)
    assert "2 groups that are not comparable" in text and "no matched pairs for 3 of 3 model pairs" in text
    assert rep["models"]["Alpha"]["placement"]["mean"] == 1.0   # the per-model table still counts every game


def test_different_opponent_fields_are_not_paired(tmp_path):
    """A wins seat 1 on seed 7 against weak opponents, B loses seat 1 on seed 7
    against other, strong ones: that is not a paired advantage for A."""
    d = tmp_path / "data" / "replays"
    d.mkdir(parents=True)
    docs = [replay("g1", 7, ["A", "Weak1", "Weak2"], ["p1", "p2", "p3"], {"p1": 30, "p2": 20, "p3": 10}),
            replay("g2", 7, ["B", "Strong1", "Strong2"], ["p2", "p3", "p1"], {"p1": 10, "p2": 30, "p3": 20}),
            # the same field as g1, B in A's seat: this one is a matched pair
            replay("g3", 7, ["Weak1", "A", "Weak2"], ["p2", "p1", "p3"], {"p1": 20, "p2": 30, "p3": 10})]
    for doc in docs:
        (d / f"{doc['game_id']}.json").write_text(json.dumps(doc))
    rep = build_report(Source(data_dir=tmp_path / "data"), games=["g1", "g2", "g3"], nboot=200)
    pairs = {(p["a"], p["b"]): p for p in rep["paired"]}
    assert pairs[("A", "B")]["pairs"] == 0 and pairs[("A", "B")]["mean_diff"] is None
    assert pairs[("A", "Weak1")]["pairs"] == 2 and pairs[("A", "Weak1")]["mean_diff"] == -1.0
    assert [g["games"] for g in rep["groups"]] == [2, 1] or [g["games"] for g in rep["groups"]] == [1, 2]
    # a rule change mid-game or other conditions also split the groups
    changed = replay("g4", 7, ["Weak2", "Weak1", "A"], ["p3", "p2", "p1"], {"p1": 10, "p2": 20, "p3": 30})
    changed["summary"]["rules_changed"] = ["t" * 64]
    (d / "g4.json").write_text(json.dumps(changed))
    rep = build_report(Source(data_dir=tmp_path / "data"), games=["g1", "g3", "g4"], nboot=200)
    assert len(rep["groups"]) == 2 and "restarted with other rules" in "\n".join(rep["warnings"])
    assert {(p["a"], p["b"]): p["pairs"] for p in rep["paired"]}[("A", "Weak2")] == 0


def test_tie_for_first_splits_the_win_and_conditions_rank_alone():
    doc = replay("g9", 5, ["A", "B", "C", "D"], ["p1", "p2", "p3", "p4"],
                 {"p1": 50, "p2": 50, "p3": 50, "p4": 10})
    rows = {r["name"]: r for r in game_rows(doc)}
    assert [rows[n]["place"] for n in "ABCD"] == [2.0, 2.0, 2.0, 4.0]
    assert [rows[n]["win"] for n in "ABCD"] == pytest.approx([1 / 3, 1 / 3, 1 / 3, 0.0])
    doc = replay("g9", 5, ["A", "B", "C", "D"], ["p1", "p2", "p3", "p4"],
                 {"p1": 50, "p2": 50, "p3": 50, "p4": 10}, condition="wonder")
    rows = {r["name"]: r for r in game_rows(doc)}
    assert [rows[n]["place"] for n in "ABCD"] == [1.0, 2.5, 2.5, 4.0] and rows["A"]["win"] == 1.0
    eliminated = replay("g9", 5, ["A", "B"], ["p1", "p2"], {"p1": 5, "p2": 5})
    eliminated["frames"][-1]["players"][1].update(alive=False, eliminated_turn=4)
    assert [r["place"] for r in game_rows(eliminated)] == [1.0, 2.0]


def test_report_cli_with_games_and_json(tmp_path, capsys):
    data, plan = write_synthetic(tmp_path)
    out = tmp_path / "rep.json"
    assert evalplan.main(["report", "--data-dir", str(data), "--games", "g1,g2,g7", "--json", str(out),
                          "--bootstrap", "200"]) == 0
    text = capsys.readouterr().out
    assert "2 finished games, 1 missing" in text and "missing g7: no replay file" in text
    rep = json.loads(out.read_text())
    assert rep["models"]["Alpha"]["games"] == 2 and rep["bootstrap"]["resamples"] == 200
    pf = tmp_path / "plan.json"
    evalplan.save_plan(plan, pf)
    assert evalplan.main(["report", "--plan", str(pf), "--data-dir", str(data)]) == 0
    assert "Track test-3p-v1: 3 games planned" in capsys.readouterr().out


REAL_DATA = Path.home() / "agentciv" / "data"


@pytest.mark.skipif(not (REAL_DATA / "replays" / "g10.json").exists(), reason="no real replays on this machine")
def test_report_on_the_real_replays_read_only():
    files = sorted((REAL_DATA / "replays").glob("*.json"))
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in files}
    rep = build_report(Source(data_dir=REAL_DATA), games=["g8", "g9", "g10", "g404"], nboot=300)
    assert rep["games"]["finished"] == 3 and rep["games"]["unfinished"] == [{"game_id": "g404",
                                                                           "why": "no replay file"}]
    assert len(rep["models"]) == 6 and all(m["games"] == 3 for m in rep["models"].values())
    assert sum(st["n"] for st in rep["seats"].values()) == 18
    assert any("no action log in 3 game(s)" in w for w in rep["warnings"])
    format_report(rep)
    rep = build_report(Source(data_dir=REAL_DATA / "replays"), nboot=100)   # the replays dir itself works too
    assert rep["games"]["finished"] >= 10 and any("different seat counts" in w for w in rep["warnings"])
    assert {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in files} == before
    assert sorted((REAL_DATA / "replays").glob("*.json")) == files


# ---------------------------------------------------------------- against a server
@pytest.fixture()
def small_track(monkeypatch):
    t = Track(id="test-3p-v1", title="test", about="test track", players=3, fog=True, negotiation_rounds=1,
              phase_limit=60.0, max_turns=2)
    monkeypatch.setitem(tracks.TRACKS, t.id, t)
    return t


def test_create_then_play_then_report_from_the_server(tmp_path, small_track):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data"), spectator_key=KEY).start_background()
    try:
        plan = make_plan("test-3p-v1", ["Alpha", "Beta", "Gamma"], seeds=1, seed_base=42, rotations=2)
        pf = tmp_path / "plan.json"
        evalplan.save_plan(plan, pf)
        with pytest.raises(EvalError, match="HTTP 401: invalid spectator key"):
            evalplan.create_games(evalplan.load_plan(pf), pf, srv.url, "wrong-key-0", out=io.StringIO())
        assert evalplan.main(["create", "--plan", str(pf), "--url", srv.url, "--spectator-key", KEY]) == 0
        plan = evalplan.load_plan(pf)
        first, second = plan["create_order"]   # one game per call, in the plan's shuffled creation order
        assert plan["games"][first]["game_id"] and plan["games"][second]["game_id"] is None
        # nothing public about the game gives away its seed or its place in the schedule
        public = call(srv, "GET", f"/api/games/{plan['games'][first]['game_id']}")[1]
        assert public["seed"] is None and re.fullmatch(r"test-3p-v1 [0-9a-f]{8}", public["name"])
        assert "rotation" not in json.dumps(public)
        rep = build_report(Source(url=srv.url), plan, nboot=100)
        assert rep["games"]["missing"] == 2 and rep["games"]["not_created"] == 1
        assert rep["games"]["unfinished"][0]["why"] == "not finished (lobby)"
        out = io.StringIO()
        evalplan.create_games(plan, pf, srv.url, KEY, count=None, out=out)
        assert "seat 1 (p1): Gamma" in out.getvalue() or "seat 1 (p1): Alpha" in out.getvalue()
        plan = evalplan.load_plan(pf)
        for g in plan["games"]:
            gid = g["game_id"]
            joined = {}
            for name in reversed(g["seats"]):  # join order does not matter: the operator fixed the seats
                s, j = call(srv, "POST", f"/api/games/{gid}/join",
                            {"name": name, "agent": {"model": name.lower() + "-m", "harness": "h"}})
                assert s == 200, j
                joined[name] = j
            seats = [joined[n] for n in g["seats"]]
            assert [j["player_id"] for j in seats] == ["p1", "p2", "p3"]
            assert wait_until(lambda: state(srv, gid, seats[0]["token"])["status"] == "running")
            play_turn(srv, gid, seats, say=(1, f"I am {g['seats'][1]}"))
            play_turn(srv, gid, seats)
            assert wait_until(lambda: srv.manager.storage.is_applied(gid, "test-3p-v1"))
        rep = build_report(Source(url=srv.url), plan, nboot=100)
        assert rep["games"]["finished"] == 2 and rep["games"]["missing"] == 0
        assert set(rep["models"]) == {"Alpha", "Beta", "Gamma"}
        assert all(m["games"] == 2 for m in rep["models"].values())
        assert not any("differ from the plan" in w for w in rep["warnings"])
        assert sum(1 for w in rep["warnings"] if "names itself" in w) == 2
        assert all(p["pairs"] >= 0 for p in rep["paired"])
        # the track filter on a server listing finds the same games
        assert sorted(Source(url=srv.url).ids("test-3p-v1")) == sorted(g["game_id"] for g in plan["games"])
    finally:
        srv.stop()
