"""Clients in synchronous games (docs/DESIGN.md §13.6): the SDK's run_bot,
examples/play_cli.py, the MCP tools and examples/llm_agent.py play phase by
phase (negotiation rounds with a barrier, then orders)."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import pytest

from agentciv.client import AgentCivClient, run_bot
from agentciv.mcp_server import AgentCivMCP, ToolError
from agentciv.server import create_server

from test_e2e_barter import HouseTrader, _Block
from test_server import wait_until
from test_server_diplomacy import Haggler

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    yield srv
    srv.stop()


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sync_game_with_haggler(server, max_turns=2, turn_timeout=20.0, rounds=3):
    host = AgentCivClient(server.url)
    gid = host.create_game(max_players=2, max_turns=max_turns, turn_timeout=turn_timeout, rated=False,
                           bots=["idle"], sync=True, negotiation_rounds=rounds)
    session = server.manager.sessions[gid]
    house = Haggler(price=5)
    with session.cond:
        seat = next(s for s in session.seats.values() if s.is_bot)
        seat.bot = house
    return host, gid, session, seat, house


def test_run_bot_trades_with_a_house_bot_round_by_round(server):
    host, gid, session, seat, house = sync_game_with_haggler(server)
    trader = HouseTrader(seat.pid)
    res = run_bot(trader, server.url, game_id=gid, name="Trader")
    assert res["result"]["turn"] == 1
    # round 1: proposal (9 gold); round 2: the house bot counters at 5; round 3: the remote agent accepts
    assert trader.accepted == ["d2"]
    assert [(e["id"], e["turn"], e["give"], e["get"]) for e in session.game.deal_log] == [
        ("d2", 0, {"gold": 5}, {"wood": 12})]
    turns = host.replay(gid)["actions"]["turns"]
    rounds = [(d["by"], d["action"]["type"], d["round"]) for d in turns[0]["diplomacy"]]
    assert rounds == [("p2", "propose", 1), (seat.pid, "counter", 2), ("p2", "accept", 3)]
    assert house.calls.count(0) == 3 and trader.acts == [0, 1]
    assert "phase_missed" not in turns[0]


def test_play_cli_plays_phase_by_phase(server, tmp_path, capsys):
    cli = _load("play_cli")
    cli.URL, cli.HOME = server.url, tmp_path / "home"
    host, gid, session, seat, house = sync_game_with_haggler(server, max_turns=3, turn_timeout=60)
    cli.main(["join", "A", gid])
    assert wait_until(lambda: session.status == "running")
    capsys.readouterr()
    cli.main(["next", "A"])
    out = capsys.readouterr().out
    assert "SYNCHRONOUS TURN: negotiation round 1 of 3" in out
    with pytest.raises(SystemExit) as e:
        cli.main(["orders", "A", "[]"])
    assert "NOT APPLIED" in str(e.value) and "round 1 of 3" in str(e.value)
    cli.main(["deal", "A", json.dumps([{"type": "propose", "to": seat.pid, "give": {"wood": 12},
                                        "get": {"gold": 9}}])])
    out = capsys.readouterr().out
    assert "#0 queued" in out and "round 1 of 3" in out
    assert json.loads(session.state_bytes(seat.pid))["deals"]["open"] == []  # nothing applied yet
    cli.main(["next", "A"])  # a round A has seen: ends it, waits for the barrier, shows round 2
    out = capsys.readouterr().out
    assert "Ended your negotiation round 1" in out and "negotiation round 2 of 3" in out
    assert "round 1: propose to " + seat.pid in out and "-> ok (deal d1)" in out
    assert "NEW DIPLOMACY" not in out or "d1" in out
    cli.main(["done", "A"])  # round 2: nothing to send
    assert "Ended negotiation round 2 of 3" in capsys.readouterr().out
    cli.main(["next", "A"])
    out = capsys.readouterr().out
    assert "negotiation round 3 of 3" in out and "countered" in out  # the house bot's counter (d2)
    cli.main(["deal", "A", json.dumps([{"type": "accept", "deal": "d2"}]), "--done"])
    assert "You have ended this round" in capsys.readouterr().out
    cli.main(["next", "A"])
    out = capsys.readouterr().out
    assert "orders phase" in out and "round 3: accept d2 -> ok" in out
    with pytest.raises(SystemExit) as e:
        cli.main(["deal", "A", json.dumps([{"type": "say", "to": "all", "text": "hi"}])])
    assert "diplomacy is closed" in str(e.value)
    cli.main(["orders", "A", "[]"])
    assert "Turn 0: 0 order(s) accepted" in capsys.readouterr().out
    cli.main(["next", "A"])
    out = capsys.readouterr().out
    assert "turn 1/3" in out and "negotiation round 1 of 3" in out
    assert [e["id"] for e in session.game.deal_log] == ["d2"]


def test_mcp_tools_in_a_sync_game(server):
    host, gid, session, seat, house = sync_game_with_haggler(server, max_turns=3, turn_timeout=60)
    m = AgentCivMCP(server.url)
    m.join_game(gid, "M")
    assert "SYNCHRONOUS TURN: negotiation round 1 of 3" in m.wait_for_turn(10)
    text = m.propose_deal(seat.pid, give={"wood": 12}, get={"gold": 9})
    assert text.startswith("Queued for the end of negotiation round 1 of 3")
    with pytest.raises(ToolError) as e:
        m.submit_orders([])
    assert "orders open after the negotiation rounds" in str(e.value)
    text = m.end_round(10)
    assert "round 1 of 3 closed" in text and "-> ok (deal d1)" in text and "Now: negotiation round 2" in text
    text = m.wait_for_inbox(5)  # in a negotiation round: ends it like end_round
    assert "round 2 of 3 closed" in text and "countered" in text
    assert m.respond_to_deal("d2", "accept").startswith("Queued")
    text = m.wait_for_turn(10)
    assert "round 3: accept d2 -> ok" in text and "submit_orders" in text
    with pytest.raises(ToolError):
        m.say("all", "too late")
    assert "Turn 0: 0 order(s) accepted" in m.submit_orders([])
    text = m.wait_for_turn(10)
    assert "turn 1/3" in text and "negotiation round 1 of 3" in text
    assert [e["id"] for e in session.game.deal_log] == ["d2"]


class _SyncFake:
    """Scripted model for a synchronous game: turn 0 proposes, ends rounds,
    accepts the counter and submits; later turns submit orders at once."""

    class RateLimitError(Exception):
        pass

    class APIStatusError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    def __init__(self, target: str):
        self.target = target
        self.requests: list = []
        self.results: list[str] = []
        outer = self

        class _Messages:
            def create(self, **kw):
                return outer.respond(kw)

        self.messages = _Messages()

    def respond(self, kw):
        self.requests.append(kw)
        msgs = kw["messages"]
        n = sum(1 for m in msgs if m["role"] == "assistant")
        k = len(self.requests)
        if msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list):
            self.results.append(msgs[-1]["content"][0]["content"])

        def tool(name, **inp):
            return _Block(type="tool_use", id=f"tu{k}", name=name, input=inp)

        if "Turn 0 of" in msgs[0]["content"]:
            script = [tool("propose_deal", to=self.target, give={"wood": 12}, get={"gold": 9}),
                      tool("wait_for_replies", seconds=5), tool("wait_for_replies", seconds=5),
                      tool("respond_to_deal", deal="d2", response="accept"), tool("wait_for_replies", seconds=5),
                      tool("submit_orders", orders=[], notes="traded")]
            blocks = [script[n]] if n < len(script) else [_Block(type="text", text="done")]
        else:
            blocks = [tool("submit_orders", orders=[])] if n == 0 else [_Block(type="text", text="ok")]
        usage = _Block(input_tokens=10, output_tokens=2, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return _Block(content=blocks, stop_reason="tool_use" if blocks[0].type == "tool_use" else "end_turn",
                      model=kw["model"], usage=usage)


def test_llm_agent_maps_wait_for_replies_to_the_barrier(server, monkeypatch):
    llm = _load("llm_agent")
    host, gid, session, seat, house = sync_game_with_haggler(server, max_turns=2, turn_timeout=20)
    fake = _SyncFake(seat.pid)
    mod = types.ModuleType("anthropic")
    mod.RateLimitError, mod.APIStatusError = fake.RateLimitError, fake.APIStatusError
    mod.APIConnectionError = fake.APIConnectionError
    mod.Anthropic = lambda: fake
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("AGENTCIV_AGENT", raising=False)
    args = argparse.Namespace(url=server.url, model="claude-opus-5-5", effort="low", name="Claude", game=gid,
                              quickmatch=False, players=2, turn_timeout=20.0, max_steps=10, fallback=False,
                              no_fallback=False, log_dir=None, sync=False)
    t0 = time.monotonic()
    llm.LLMAgent(args).run()
    assert time.monotonic() - t0 < 30
    assert session.game.status == "finished"
    assert [(e["id"], e["turn"], e["give"], e["get"]) for e in session.game.deal_log] == [
        ("d2", 0, {"gold": 5}, {"wood": 12})]
    system = fake.requests[0]["system"][0]["text"]
    assert "This game is synchronous: each turn has 3 negotiation round(s)" in system
    assert "Diplomacy is live" not in system
    r = fake.results
    assert r[0].startswith("queued for the end of negotiation round 1 of 3")
    assert "round 1 of 3 closed" in r[1] and "deal d1" in r[1]
    assert "countered" in r[2] and "d2" in r[2]
    assert "round 3: accept d2 -> ok" in r[4] and "Negotiation is over" in r[4]
    turns = host.replay(gid)["actions"]["turns"]
    assert turns[1]["orders"]["p2"]["ready"] is True and "phase_missed" not in turns[1]
