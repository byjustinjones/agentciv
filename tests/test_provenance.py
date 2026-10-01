"""Provenance: agent manifests at join, the rules hash, the per-turn action
log in the replay (operator-only while live), and their checkpoint round trip."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentciv.client import AgentCivClient, agent_from_env
from agentciv.engine import rules_json, rulesdoc
from agentciv.server import create_server
from agentciv.server.manager import GameManager, GameSession
from agentciv.server.provenance import (MAX_DIPLOMACY_LOGGED, MAX_ITEM_BYTES, ActionLog, ManifestError,
                                        validate_agent)

from test_server import call, wait_until
from test_spectator_key import KEY, _bytes, _path

ROOT = Path(__file__).resolve().parent.parent
SHA = "ab" * 32


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def no_workers():
    with patch.object(GameSession, "launch", lambda self: None):
        yield


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data"), spectator_key=KEY).start_background()
    yield srv
    srv.stop()


# ---------------------------------------------------------------- manifest validation
def test_manifest_is_normalised():
    got = validate_agent({"model": "  claude-opus-5-5 ", "harness": "my  harness\n1.0", "prompt_sha256": SHA.upper(),
                          "notes": " line one\nline two ", "memory": "   "})
    assert got == {"model": "claude-opus-5-5", "harness": "my harness 1.0", "prompt_sha256": SHA,
                   "notes": "line one\nline two"}
    assert validate_agent(None) is None and validate_agent({}) is None and validate_agent({"tools": ""}) is None


@pytest.mark.parametrize("bad, message", [
    ([], "must be an object"),
    ("gpt", "must be an object"),
    ({"model": "x", "temperature": "1"}, "unknown agent fields ['temperature']"),
    ({"model": 5}, "agent.model must be a string"),
    ({"tools": ["a", "b"]}, "agent.tools must be a string"),
    ({"model": "x" * 121}, "at most 120"),
    ({"notes": "y" * 1001}, "at most 1000"),
    ({"prompt_sha256": "abc"}, "64 hex digits"),
    ({"harness": "a\x00b"}, "unprintable"),
])
def test_manifest_rejects(bad, message):
    with pytest.raises(ManifestError, match=message.replace("[", r"\[").replace("]", r"\]")):
        validate_agent(bad)


def test_agent_from_env(monkeypatch):
    monkeypatch.delenv("AGENTCIV_AGENT", raising=False)
    assert agent_from_env() is None
    monkeypatch.setenv("AGENTCIV_AGENT", '{"model": "m1"}')
    assert agent_from_env() == {"model": "m1"}
    assert agent_from_env('{"harness": "h"}') == {"harness": "h"}
    for bad in ("[1]", "{nope"):
        with pytest.raises(ValueError):
            agent_from_env(bad)


def test_join_and_quickmatch_store_the_manifest(server):
    agent = {"model": "claude-opus-5-5", "effort": "high", "harness": "test", "prompt_sha256": SHA}
    host = AgentCivClient(server.url)
    gid = host.create_game(max_players=3, turn_timeout=0, max_turns=2)
    status, body = call(server, "POST", f"/api/games/{gid}/join", {"name": "Bad", "agent": {"modle": "x"}})
    assert status == 400 and "unknown agent fields" in body["error"]
    a, b = AgentCivClient(server.url), AgentCivClient(server.url)
    a.join(gid, "Alice", agent=agent)
    b.join(gid, "Bob")
    players = {p["name"]: p for p in host.game(gid)["players"]}
    assert set(players) == {"Alice", "Bob"}  # the refused join took no seat
    assert players["Alice"]["agent"] == agent and "agent" not in players["Bob"]
    # quickmatch: a bad manifest is refused before any lobby is made; a good one never splits lobbies
    status, body = call(server, "POST", "/api/quickmatch", {"name": "Q0", "agent": {"model": 1}})
    assert status == 400 and "agent.model" in body["error"]
    q1, q2 = AgentCivClient(server.url), AgentCivClient(server.url)
    r1 = q1.quickmatch("Q1", players=3, lobby_timeout=600, agent={"model": "m1"})
    r2 = q2.quickmatch("Q2", players=3, lobby_timeout=600, agent={"model": "m2", "harness": "other"})
    assert r1["game_id"] == r2["game_id"]
    players = {p["name"]: p.get("agent") for p in host.game(r1["game_id"])["players"]}
    assert players == {"Q1": {"model": "m1"}, "Q2": {"model": "m2", "harness": "other"}}


# ---------------------------------------------------------------- rules hash
def test_rules_hash_is_stable_and_covers_text_and_constants(monkeypatch):
    def block(obj):
        return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    text = (ROOT / "docs" / "RULES.md").read_bytes()
    want = hashlib.sha256(text + b"\0" + block(rules_json()) + b"\0"
                          + block(rulesdoc.engine_constants())).hexdigest()
    assert rulesdoc.rules_sha256() == want == rulesdoc.rules_sha256()
    # a changed constant (or rules text) gives a new hash
    monkeypatch.setattr(rulesdoc, "_HASH_CACHE", None)
    real = rules_json()
    monkeypatch.setattr("agentciv.engine.rules.rules_json", lambda: {**real, "_probe": 1})
    assert rulesdoc.rules_sha256() != want
    monkeypatch.setattr(rulesdoc, "_HASH_CACHE", None)


@pytest.mark.parametrize("name,value", [("MAPGEN_WATER_FRACTION", 0.4), ("ENGINE_VERSION", 10 ** 6),
                                        ("PROTOCOL_VERSION", 10 ** 6), ("MAX_ORDERS_PER_TURN", 7)])
def test_rules_hash_covers_what_the_served_rules_leave_out(monkeypatch, name, value):
    """Map generation settings, limits and the engine/protocol versions are
    not in the rules text or ``rules_json()``, but they change the game."""
    from agentciv.engine import constants as C
    before = rulesdoc.rules_sha256()
    served = (rulesdoc.render(), json.dumps(rules_json(), sort_keys=True))
    assert getattr(C, name) != value
    monkeypatch.setattr(C, name, value)
    monkeypatch.setattr(rulesdoc, "_HASH_CACHE", None)
    assert rulesdoc.rules_sha256() != before
    if name != "MAX_ORDERS_PER_TURN":   # (that one is quoted in the rules text)
        assert (rulesdoc.render(), json.dumps(rules_json(), sort_keys=True)) == served
    monkeypatch.setattr(rulesdoc, "_HASH_CACHE", None)


def test_engine_version_is_tied_to_the_recorded_games():
    """Re-recording the golden games (engine behaviour changed on purpose)
    needs a new ENGINE_VERSION, so the rules hash and with it every track pin
    change too: add the new version and the new file's sha256 here."""
    from agentciv.engine import constants as C
    golden = hashlib.sha256((ROOT / "tests" / "data" / "nofog_golden.json").read_bytes()).hexdigest()
    recorded = {1: "17062bceefd9c92f7714c2522c6174ed8a77b3f6aab8d515981c77406a77d0a5"}
    assert recorded.get(C.ENGINE_VERSION) == golden, (
        "tests/data/nofog_golden.json changed: bump ENGINE_VERSION in agentciv/engine/constants.py and record "
        f"{{{C.ENGINE_VERSION}: {golden!r}}} here")


def test_rules_hash_in_summary_and_replay(tmp_path, no_workers):
    manager = GameManager(str(tmp_path), open_ratings=True, restore=False)
    s = manager.create_game({"max_players": 2, "max_turns": 1, "turn_timeout": 30})
    assert s.summary()["rules_sha256"] == rulesdoc.rules_sha256()
    s.join("Alice")
    s.join("Bob")
    with s.cond:
        s._advance()
    assert s._finalize()
    replay = json.loads(manager.storage.read_replay(s.game_id))
    assert replay["summary"]["rules_sha256"] == rulesdoc.rules_sha256()
    assert manager.storage.summary(s.game_id)["rules_sha256"] == rulesdoc.rules_sha256()
    manager.shutdown()


# ---------------------------------------------------------------- action log content
def _two_player(tmp_path, **opts):
    manager = GameManager(str(tmp_path), open_ratings=True, restore=False)
    s = manager.create_game({"max_players": 2, "max_turns": 3, "turn_timeout": 30, **opts})
    a, b = s.join("Alice", agent={"model": "m-a"}), s.join("Bob")
    assert s.status == "running"
    return manager, s, a, b


def _log(session):
    return json.loads(session.actions.to_bytes())


def test_action_log_records_orders_rejections_diplomacy_and_misses(tmp_path, no_workers):
    manager, s, a, b = _two_player(tmp_path)
    bad = {"type": "build", "at": [0, 0], "building": "no-such-building"}
    s.submit(a.pid, {"turn": 0, "orders": [bad]})
    s.submit(a.pid, {"turn": 0, "orders": [], "ready": True})
    s.diplomacy(a.pid, {"actions": [{"type": "say", "to": b.pid, "text": "hello"},
                                    {"type": "accept", "deal": "d999"}]})
    s.submit(b.pid, {"turn": 0, "orders": [], "ready": False})   # still drafting at the deadline
    with s.cond:
        s._advance()                     # turn 0 resolves (as if the deadline passed)
        s._advance()                     # turn 1: nobody submitted
    log = _log(s)
    assert log["format"] == 1 and [t["turn"] for t in log["turns"]] == [0, 1]
    t0 = log["turns"][0]
    alice = t0["orders"][a.pid]
    assert alice["orders"] == [] and alice["submissions"] == 2 and alice["ready"] is True
    assert alice["rejected"] and alice["rejected"][0]["submission"] == 1
    assert alice["rejected"][0]["order"] == bad and alice["rejected"][0]["error"]
    assert t0["orders"][b.pid]["ready"] is False
    said, accepted = t0["diplomacy"]
    assert said["by"] == a.pid and said["action"]["text"] == "hello" and said["result"]["ok"] is True
    assert accepted["action"]["deal"] == "d999" and accepted["result"]["ok"] is False and accepted["result"]["error"]
    assert "example" not in json.dumps(t0["diplomacy"])     # hints added for the caller are not logged
    assert t0["end"] == "deadline" and t0["missed"] == {b.pid: "draft"}
    t1 = log["turns"][1]
    assert t1["end"] == "deadline" and t1["missed"] == {a.pid: "no_orders", b.pid: "no_orders"}
    with s.cond:                         # everyone ready: no misses
        s.submit(a.pid, {"turn": 2, "orders": []})
        s.submit(b.pid, {"turn": 2, "orders": []})
        s._advance()
    assert s.status == "finished"
    t2 = _log(s)["turns"][2]
    assert t2["end"] == "all_ready" and "missed" not in t2
    manager.shutdown()


def test_action_log_logs_house_bot_orders(tmp_path, no_workers):
    manager = GameManager(str(tmp_path), open_ratings=True, restore=False)
    s = manager.create_game({"max_players": 2, "max_turns": 2, "turn_timeout": 30, "bots": ["strategist"]})
    human = s.join("Alice")
    s._house_turn(0)
    s.submit(human.pid, {"turn": 0, "orders": []})
    with s.cond:
        s._advance()
    t0 = _log(s)["turns"][0]
    bot = next(pid for pid, seat in s.seats.items() if seat.is_bot)
    assert t0["orders"][bot]["orders"] and t0["end"] == "all_ready"
    manager.shutdown()


def test_action_log_caps_sizes():
    log = ActionLog()
    huge = {"type": "say", "to": "all", "text": "x" * (MAX_ITEM_BYTES + 10)}
    log.orders(0, "p1", [huge] + [{"type": "x"}] * 150, [{"index": 0, "error": "too big"}], True, 1.0)
    log.diplomacy(0, "p1", [{"type": "say"}] * (MAX_DIPLOMACY_LOGGED + 5), [], 2.0)
    entry = json.loads(log.to_bytes())["turns"][0]
    rec = entry["orders"]["p1"]
    assert rec["orders"][0] == {"truncated": True, "bytes": len(json.dumps(huge, separators=(",", ":")))}
    assert len(rec["orders"]) == 100 and rec["orders_omitted"] == 51
    assert rec["rejected"][0]["order"]["truncated"] is True
    assert len(entry["diplomacy"]) == MAX_DIPLOMACY_LOGGED and entry["diplomacy_omitted"] == {"p1": 5}


# ---------------------------------------------------------------- visibility
def _actions_of(raw: bytes):
    return json.loads(raw).get("actions")


@pytest.mark.parametrize("fog", [True, False])
def test_action_log_is_operator_only_while_live(server, fog):
    host = AgentCivClient(server.url)
    gid = host.create_game(max_players=2, turn_timeout=0, max_turns=2, fog=fog)
    a, b = AgentCivClient(server.url), AgentCivClient(server.url)
    a.join(gid, "Alice")
    b.join(gid, "Bob")
    assert wait_until(lambda: a.state()["status"] == "running")
    a.diplomacy([{"type": "say", "to": b.player_id, "text": "secret plan"}], turn=0)
    a.submit_orders([], turn=0)
    b.submit_orders([], turn=0)
    assert wait_until(lambda: a.state()["turn"] == 1)
    a.submit_orders([{"type": "build", "at": [0, 0], "building": "nope"}], turn=1)
    for options in ({}, {"from": 0, "to": 1}, {"compact": 1}):
        public = _bytes(server, _path(gid, "replay", **options))
        assert _actions_of(public) is None and b"secret plan" not in public
        for player in (a, b):  # a player's token never selects the operator view
            mine = _bytes(server, _path(gid, "replay", token=player.token, **options))
            assert _actions_of(mine) is None and b"secret plan" not in mine
            mine = _bytes(server, _path(gid, "replay", spectator_key=KEY, token=player.token, **options))
            assert _actions_of(mine) is None
    full = _actions_of(_bytes(server, _path(gid, "replay", spectator_key=KEY)))
    assert full["turns"][0]["diplomacy"][0]["action"]["text"] == "secret plan"
    assert full["turns"][1]["orders"][a.player_id]["rejected"]           # the live turn so far
    ranged = _actions_of(_bytes(server, _path(gid, "replay", spectator_key=KEY, **{"from": 1, "to": 1})))
    assert [t["turn"] for t in ranged["turns"]] == [1]
    assert _actions_of(_bytes(server, _path(gid, "replay", spectator_key=KEY, compact=1))) is None
    # finished: part of every full replay, the same from memory and from the saved file
    b.submit_orders([], turn=1)
    assert wait_until(lambda: host.game(gid)["status"] == "finished")
    session = server.manager.sessions[gid]
    assert wait_until(lambda: session._fin_done)
    public = _bytes(server, _path(gid, "replay"))
    log = _actions_of(public)
    assert [t["turn"] for t in log["turns"]] == [0, 1]
    assert log["turns"][1]["end"] == "all_ready"
    assert public == server.manager.storage.read_replay(gid)
    ranged = _actions_of(_bytes(server, _path(gid, "replay", **{"from": 1, "to": 1})))
    assert [t["turn"] for t in ranged["turns"]] == [1]


def test_finished_replay_is_identical_live_and_saved(tmp_path, no_workers):
    manager, s, a, b = _two_player(tmp_path)
    s.diplomacy(a.pid, {"actions": [{"type": "say", "to": "all", "text": "hi"}]})
    with s.cond:
        while s.status != "finished":
            s._advance()
    live = s.replay_bytes()
    assert json.loads(live)["actions"]["turns"][0]["diplomacy"]
    assert s._finalize()
    assert s.replay_bytes() == live == manager.storage.read_replay(s.game_id)
    manager.shutdown()


# ---------------------------------------------------------------- checkpoints and old data
def test_checkpoint_round_trip_keeps_log_and_manifest(tmp_path, no_workers):
    manager, s, a, b = _two_player(tmp_path)
    s.submit(a.pid, {"turn": 0, "orders": [{"type": "nope"}]})
    with s.cond:
        s._advance()
    s.diplomacy(b.pid, {"actions": [{"type": "say", "to": a.pid, "text": "mid-turn"}]})
    before = _log(s)
    assert s.checkpoint(force=True, bots_idle=True)
    manager.shutdown()
    m2 = GameManager(str(tmp_path), open_ratings=True)
    s2 = m2.sessions[s.game_id]
    assert _log(s2) == before
    assert {p["name"]: p.get("agent") for p in s2.summary()["players"]} == {"Alice": {"model": "m-a"}, "Bob": None}
    assert s2.summary()["rules_sha256"] == rulesdoc.rules_sha256()
    s2.submit(a.pid, {"turn": 1, "orders": []})
    with s2.cond:
        s2._advance()
    turns = _log(s2)["turns"]
    assert [t["turn"] for t in turns] == [0, 1]
    assert turns[1]["diplomacy"][0]["action"]["text"] == "mid-turn" and a.pid in turns[1]["orders"]
    m2.shutdown()


def test_old_checkpoint_without_provenance_restores(tmp_path, no_workers):
    manager, s, a, b = _two_player(tmp_path)
    with s.cond:
        state = s.snapshot_state()
    del state["actions"]
    del state["opts"]["rules_sha256"]
    for seat in state["seats"]:
        del seat["agent"]
    old = GameSession.from_snapshot(manager, state, [], 0)
    assert len(old.actions) == 0 and old.summary()["rules_sha256"] is None
    assert all("agent" not in p for p in old.summary()["players"])
    old.submit(a.pid, {"turn": 0, "orders": []})
    assert _log(old)["turns"][0]["orders"][a.pid]["submissions"] == 1
    manager.shutdown()


def test_old_replay_without_actions_still_serves(tmp_path, no_workers):
    manager, s, a, b = _two_player(tmp_path)
    with s.cond:
        while s.status != "finished":
            s._advance()
    frames = s.frames.all_full()
    summary = {k: v for k, v in s.summary().items() if k != "rules_sha256"}
    manager.storage.save_replay("g77", summary, s.game.result, frames)  # as written before this change
    assert "actions" not in json.loads(manager.storage.read_replay("g77"))
    for compact, lo, hi in ((False, None, None), (False, 1, 2), (True, None, None)):
        doc = json.loads(manager.archived_replay_bytes("g77", compact, lo, hi))
        assert "actions" not in doc and doc["frames"]
    manager.shutdown()


# ---------------------------------------------------------------- llm_agent call log
def _llm_agent():
    spec = importlib.util.spec_from_file_location("llm_agent", ROOT / "examples" / "llm_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _response(model, stop="tool_use", content=None, iterations=None):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=3,
                            cache_read_input_tokens=7, iterations=iterations)
    return SimpleNamespace(model=model, stop_reason=stop, usage=usage,
                           content=content if content is not None else
                           [SimpleNamespace(type="tool_use", name="submit_orders")])


def test_llm_call_log_flags_fallbacks_and_errors(tmp_path):
    llm = _llm_agent()
    path = tmp_path / "log.jsonl"
    log = llm.CallLog(str(path))
    req = "claude-opus-5-5"
    plain = log.record(3, req, _response(req), 1.23456)
    assert plain["fallback"] is False and plain["different_model"] is False and plain["latency_s"] == 1.235
    assert plain["usage"] == {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 3,
                              "cache_read_input_tokens": 7}
    blocks = [SimpleNamespace(type="fallback"), SimpleNamespace(type="text")]
    fb = log.record(4, req, _response("claude-opus-4-8", stop="end_turn", content=blocks), 2.0)
    assert fb["fallback"] is True and fb["different_model"] is True and fb["tool_calls"] == []
    sticky = log.record(5, req, _response("claude-opus-4-8", iterations=[{"type": "fallback_message"}]), 1.0)
    assert sticky["fallback"] is True
    refused = log.record(6, req, _response(req, stop="refusal", content=[]), 0.5)
    assert refused["refusal"] is True

    class APIStatusError(Exception):
        status_code = 529
        message = "overloaded"
    err = log.record_error(7, req, APIStatusError(), 0.1)
    assert err["error"] == {"type": "APIStatusError", "status": 529, "message": "overloaded"}
    total = log.summary(game_id="g1")
    assert total["calls"] == 5 and total["errors"] == 1 and total["refusals"] == 1
    assert total["fallback_calls"] == 2 and total["calls_answered_by_other_model"] == 2
    assert total["any_call_answered_by_other_model"] is True
    assert total["models"] == {req: 2, "claude-opus-4-8": 2} and total["usage"]["input_tokens"] == 40
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert [x["type"] for x in lines] == ["call"] * 5 + ["summary"] and lines[-1]["game_id"] == "g1"


def test_llm_agent_fallbacks_are_opt_in_and_manifest(monkeypatch):
    llm = _llm_agent()
    monkeypatch.delenv("AGENTCIV_AGENT", raising=False)
    monkeypatch.delenv("AGENTCIV_LOG_DIR", raising=False)
    calls = []

    class Endpoint:
        def __init__(self, beta):
            self.beta = beta

        def create(self, **kw):
            calls.append((self.beta, kw))
            return _response(kw["model"], stop="end_turn", content=[])

    agent = llm.LLMAgent.__new__(llm.LLMAgent)
    agent.llm = SimpleNamespace(messages=Endpoint(False), beta=SimpleNamespace(messages=Endpoint(True)))
    agent.system, agent.turn, agent.calls = [], 0, llm.CallLog()
    for fallback, beta in ((False, False), (True, True)):
        agent.args = SimpleNamespace(model="claude-opus-5-5", effort="low", fallback=fallback, no_fallback=False,
                                     max_steps=4)
        agent.create([{"role": "user", "content": "hi"}])
        assert calls[-1][0] is beta and ("fallbacks" in calls[-1][1]) is beta
    ns = SimpleNamespace(model="claude-opus-5-5", effort="low", fallback=False, max_steps=4)
    manifest = llm.agent_manifest(ns, "1.2.3")
    assert validate_agent(manifest) == manifest
    assert manifest["harness_version"].endswith("anthropic 1.2.3") and "fallbacks off" in manifest["notes"]
    monkeypatch.setenv("AGENTCIV_AGENT", '{"notes": "run 7", "model_version": "2026-09"}')
    manifest = llm.agent_manifest(ns)
    assert manifest["notes"] == "run 7" and manifest["model_version"] == "2026-09"
    ap_args = ["--no-fallback"]  # the old flag is still accepted (a no-op)
    with patch("sys.argv", ["llm_agent.py", *ap_args]), patch.object(llm, "LLMAgent") as cls:
        llm.main()
    parsed = cls.call_args[0][0]
    assert parsed.fallback is False and parsed.log_dir is None



def test_mcp_join_tools_pass_the_manifest(server, monkeypatch):
    from agentciv.mcp_server import TOOLS, AgentCivMCP
    schemas = {t["name"]: t["inputSchema"]["properties"] for t in TOOLS}
    assert "agent" in schemas["join_game"] and "agent" in schemas["quickmatch"]
    host = AgentCivClient(server.url)
    gid = host.create_game(max_players=3, turn_timeout=0, max_turns=2)
    monkeypatch.setenv("AGENTCIV_AGENT", '{"harness": "from-env"}')
    AgentCivMCP(server.url).join_game(gid, "Explicit", agent={"model": "m"})
    AgentCivMCP(server.url).join_game(gid, "FromEnv")
    agents = {p["name"]: p.get("agent") for p in host.game(gid)["players"]}
    assert agents == {"Explicit": {"model": "m"}, "FromEnv": {"harness": "from-env"}}
