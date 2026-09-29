"""End-to-end barter (docs/DESIGN.md §13): remote SDK agents haggle with each
other mid-turn, and a remote run_bot agent trades with a house bot."""
from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

import pytest

from agentciv.client import AgentCivClient, run_bot, summarize_view
from agentciv.server import create_server

from test_server import call, wait_until
from test_server_diplomacy import Haggler

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    yield srv
    srv.stop()


def test_two_remote_agents_haggle_mid_turn(server):
    host = AgentCivClient(server.url)
    gid = host.create_game(max_players=3, turn_timeout=0, rated=False, bots=["idle"])
    seller, buyer = AgentCivClient(server.url), AgentCivClient(server.url)
    seller.join(gid, "Seller")
    buyer.join(gid, "Buyer")
    assert wait_until(lambda: host.game(gid)["status"] == "running")
    s0, b0 = seller.state()["you"]["resources"], buyer.state()["you"]["resources"]
    log: list[str] = []

    def buyer_agent():
        """Waits for an offer, counters once at a lower price, then waits for the outcome."""
        box = buyer.inbox(timeout=15)
        offer = next(e["deal"] for e in box["items"] if e["type"] == "deal_proposed")
        log.append(f"buyer got {offer['id']}")
        assert offer["give"] == {"wood": 30} and offer["get"] == {"gold": 40}
        r = buyer.counter(offer["id"], give={"gold": 30}, get={"wood": 30}, message="30 gold")
        assert r["ok"], r
        log.append(f"buyer countered with {r['deal']}")
        while True:
            box = buyer.inbox(timeout=15)
            done = [e for e in box["items"] if e["type"] in ("deal_executed", "deal_rejected")]
            if done or box["timed_out"]:
                log.append(f"buyer saw {done[0]['type'] if done else 'timeout'}")
                return

    t = threading.Thread(target=buyer_agent)
    t.start()
    time.sleep(0.2)
    r = seller.propose(buyer.player_id, give={"wood": 30}, get={"gold": 40}, message="fine wood")
    assert r["ok"]
    box = seller.inbox(timeout=15)
    counter = next(e for e in box["items"] if e["type"] == "deal_countered")
    assert counter["deal"] == r["deal"] and counter["new"]["give"] == {"gold": 30}
    assert seller.accept(counter["new"]["id"])["status"] == "accepted"
    t.join(15)
    assert log == ["buyer got d1", "buyer countered with d2", "buyer saw deal_executed"]

    # the trade shows in both views and in the public log, all within turn 0
    for c, before, sign in ((seller, s0, -1), (buyer, b0, 1)):
        v = c.state()
        assert v["turn"] == 0
        me = v["you"]["resources"]
        assert me["wood"] == before["wood"] + sign * 30 and me["gold"] == before["gold"] - sign * 30
        assert v["deals"]["log"][-1] == {"id": "d2", "turn": 0, "from": buyer.player_id, "to": seller.player_id,
                                          "give": {"gold": 30}, "get": {"wood": 30}, "peace": None}
        text = summarize_view(v)
        assert "Recent public deals" in text and "d2" in text
    s, spec = call(server, "GET", f"/api/games/{gid}/state")
    assert spec["deals"]["log"][-1]["id"] == "d2" and spec["deals"]["open"] == []
    rep = {p["id"]: p["reputation"]["deals"] for p in spec["players"]}
    assert rep[seller.player_id] == rep[buyer.player_id] == 1


class HouseTrader:
    """Remote agent (run_bot): proposes wood for 9 gold to the house bot on
    turn 0 and accepts any counter from it."""

    name = "house-trader"

    def __init__(self, target: str):
        self.target = target
        self.proposed = False
        self.accepted: list[str] = []
        self.acts: list[int] = []

    def negotiate(self, view):
        me, out = view["you"]["id"], []
        for d in view["deals"]["open"]:
            if d["to"] == me and d["from"] == self.target:
                out.append({"type": "accept", "deal": d["id"]})
                self.accepted.append(d["id"])
        if not self.proposed:
            self.proposed = True
            out.append({"type": "propose", "to": self.target, "give": {"wood": 12}, "get": {"gold": 9},
                        "message": "9 gold for 12 wood?"})
        return out

    def act(self, view):
        self.acts.append(view["turn"])
        return []


def test_remote_run_bot_trades_with_a_house_bot(server):
    host = AgentCivClient(server.url)
    gid = host.create_game(max_players=2, max_turns=3, turn_timeout=6, rated=False, bots=["idle"])
    session = server.manager.sessions[gid]
    house = Haggler(price=5)
    with session.cond:
        seat = next(s for s in session.seats.values() if s.is_bot)
        seat.bot = house
    trader = HouseTrader(seat.pid)
    t0 = time.monotonic()
    res = run_bot(trader, server.url, game_id=gid, name="Trader", negotiate_window=3.0)
    assert res["result"]["turn"] == 2
    assert time.monotonic() - t0 < 30
    # the house bot countered at 5 gold, the remote agent accepted — during turn 0
    assert trader.accepted == ["d2"]
    log = session.game.deal_log
    assert [(e["id"], e["turn"], e["from"], e["to"], e["give"], e["get"]) for e in log] == [
        ("d2", 0, seat.pid, "p2", {"gold": 5}, {"wood": 12})]
    assert trader.acts[:1] == [0] and house.calls.count(0) >= 4  # 3 rounds + at least one reactive answer


def _load_example(name: str):
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_example_barter_bot_plays_a_game(server):
    bb = _load_example("barter_bot")
    host = AgentCivClient(server.url)
    gid = host.create_game(max_players=3, max_turns=4, turn_timeout=5, rated=False, bots=["idle"])
    out = {}
    bots = {"A": bb.BarterBot("idle"), "B": bb.BarterBot("idle")}
    threads = [threading.Thread(target=lambda n=n: out.update({n: run_bot(bots[n], server.url, game_id=gid, name=n,
                                                                          negotiate_window=0.5)}))
               for n in bots]
    for t in threads:
        t.start()
    # force a surplus so the barter bots have something to haggle about
    session = server.manager.sessions[gid]
    assert wait_until(lambda: len(session.seats) == 3 and session.status == "running")
    with session.cond:
        a, b = (next(s.pid for s in session.seats.values() if s.name == n) for n in ("A", "B"))
        pa, pb = session.game.player(a), session.game.player(b)
        pa.resources.update(wood=290, stone=0)
        pb.resources.update(stone=290, wood=0)
    for t in threads:
        t.join(60)
    assert out["A"]["result"]["turn"] == 3 and out["A"]["result"] == out["B"]["result"]
    kinds = {x["action"]["type"] for x in session.game.diplomacy_log}
    assert "propose" in kinds
    assert any({e["from"], e["to"]} == {a, b} for e in session.game.deal_log)


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeAnthropic:
    """Scripted stand-in for the Anthropic SDK (no network): on turn 0 the
    "model" proposes a deal to the house bot, waits for replies, accepts the
    counter, then submits orders; on later turns it just submits."""

    class RateLimitError(Exception):
        pass

    class APIStatusError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    def __init__(self, target: str):
        self.target = target
        self.requests: list = []
        outer = self

        class _Messages:
            def create(self, **kw):
                return outer.respond(kw)

        class _Beta:
            messages = _Messages()

        self.messages = _Messages()
        self.beta = _Beta()

    def respond(self, kw):
        self.requests.append(kw)
        msgs = kw["messages"]
        first = msgs[0]["content"]
        n = sum(1 for m in msgs if m["role"] == "assistant")
        k = len(self.requests)

        def tool(name, **inp):
            return _Block(type="tool_use", id=f"tu{k}", name=name, input=inp)

        if "Turn 0 of" in first:
            last = msgs[-1]["content"]
            if n == 0:
                blocks = [tool("propose_deal", to=self.target, give={"wood": 12}, get={"gold": 9},
                               message="9 gold?")]
            elif n == 1:
                blocks = [tool("wait_for_replies", seconds=5)]
            elif n == 2:
                text = last[0]["content"]
                deal = text.split("with ")[1].split(" ")[0]  # "... countered deal d1 with d2 to you: ..."
                blocks = [tool("respond_to_deal", deal=deal, response="accept")]
            elif n == 3:
                blocks = [tool("submit_orders", orders=[], notes="traded wood")]
            else:
                blocks = [_Block(type="text", text="done")]
        else:
            blocks = [tool("submit_orders", orders=[])] if n == 0 else [_Block(type="text", text="ok")]
        return _Block(content=blocks, stop_reason="tool_use" if blocks[0].type == "tool_use" else "end_turn")


def test_llm_agent_example_barters_with_a_fake_model(server, monkeypatch):
    import argparse
    import sys
    import types
    llm = _load_example("llm_agent")
    host = AgentCivClient(server.url)
    gid = host.create_game(max_players=2, max_turns=3, turn_timeout=20, rated=False, bots=["idle"])
    session = server.manager.sessions[gid]
    house = Haggler(price=5)
    with session.cond:
        seat = next(s for s in session.seats.values() if s.is_bot)
        seat.bot = house
    fake = _FakeAnthropic(seat.pid)
    mod = types.ModuleType("anthropic")
    mod.RateLimitError, mod.APIStatusError = fake.RateLimitError, fake.APIStatusError
    mod.APIConnectionError = fake.APIConnectionError
    mod.Anthropic = lambda: fake
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    args = argparse.Namespace(url=server.url, model="claude-opus-5-5", effort="low", name="Claude", game=gid,
                              quickmatch=False, players=2, turn_timeout=20.0, max_steps=10, no_fallback=False)
    llm.LLMAgent(args).run()
    assert [(e["id"], e["turn"], e["give"], e["get"]) for e in session.game.deal_log] == [
        ("d2", 0, {"gold": 5}, {"wood": 12})]
    assert session.game.status == "finished"
    tool_names = {t["name"] for t in fake.requests[0]["tools"]}
    assert {"propose_deal", "respond_to_deal", "say", "wait_for_replies", "submit_orders"} <= tool_names
    assert fake.requests[0]["model"] == "claude-opus-5-5"
