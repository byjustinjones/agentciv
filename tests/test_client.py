"""SDK tests: text helpers on engine views, run_bot, and the CLI."""
from __future__ import annotations

import json
import threading

import pytest

from agentciv.bots.base import IdleBot
from agentciv.client import AgentCivClient, ApiError, ascii_map, main, run_bot, summarize_view
from agentciv.engine.testing import new_game
from agentciv.server import create_server


@pytest.fixture()
def server(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    yield srv
    srv.stop()


def test_summarize_view_and_ascii_map_on_engine_views():
    g = new_game(6, seed=3)
    for _ in range(3):
        g.step()
    view = g.player_view("p2")
    text = summarize_view(view)
    assert "YOU: p2" in text and "Resources" in text and "Victory thresholds" in text
    for p in g.players:
        assert p.id in text
    spec = summarize_view(g.spectator_view())  # no "you": still works
    assert "Players" in spec and "YOU:" not in spec
    m = ascii_map(view)
    rows = m.splitlines()
    assert len(rows) == 2 + g.height + 2
    assert "@2" in m  # p2's capital
    assert "(you)" in m
    json.dumps(view)  # untouched by the helpers


def test_summary_mentions_threats_and_proposals():
    g = new_game(2, seed=5)
    cap = g.player("p1").capital
    x, y = g.xy(cap)
    g.place_units(x + 1 if x + 1 < g.width else x - 1, y, "p2", {"cavalry": 2})
    g.treaty_proposals.append({"from": "p2", "to": "p1", "turns": 20, "turn": g.turn - 1})
    text = summarize_view(g.player_view("p1"))
    assert "Other players' armies within 3 tiles of your cities:" in text and "p2 at" in text
    assert 'accept_treaty' in text


def test_summary_lists_other_players_cities_with_units_inside():
    g = new_game(2, seed=5)
    x, y = g.xy(g.player("p2").capital)
    g.place_units(x, y, "p2", {"archer": 4})
    text = summarize_view(g.player_view("p1"))
    line = next(l for l in text.splitlines() if f"p2 " in l and f"[{x},{y}]" in l and "walls" in l)
    assert "(original capital)" in line and "archer" in line and "unit power" in line
    assert "Other players' cities (1):" in text


def test_ascii_map_lobby():
    g = new_game(2, start=False)
    assert "not started" in ascii_map(g.spectator_view())


def test_client_errors(server):
    c = AgentCivClient(server.url)
    with pytest.raises(ApiError) as ei:
        c.join("g404", "A")
    assert ei.value.status == 404
    with pytest.raises(ValueError):
        c.state()  # no game joined
    assert "idle" in c.bots()
    assert "units" in c.rules_json()
    assert c.rules().startswith("#")


def test_run_bot_with_bot_instance_and_callable(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=3, max_turns=5, bots=["idle"], turn_timeout=0.05, turn_delay=0)
    out = {}
    t = threading.Thread(target=lambda: out.update(a=run_bot(IdleBot(), server.url, game_id=gid, name="InstanceBot")))
    t.start()
    out["b"] = run_bot(lambda view: [{"type": "message", "to": "all", "text": "hi"}], server.url,
                       game_id=gid, name="CallableBot")
    t.join(30)
    assert out["a"]["result"] == out["b"]["result"]
    assert out["a"]["result"]["turn"] == 4
    assert {out["a"]["place"], out["b"]["place"]} <= {1, 2, 3}
    msgs = c.state(gid)["messages"]
    assert any(m["text"] == "hi" for m in msgs)


def test_run_bot_quickmatch_with_builtin_name(server):
    c = AgentCivClient(server.url)
    c.quickmatch("QM", players=2, lobby_timeout=0.1, max_turns=3, turn_timeout=0.05)
    res = run_bot("idle", server.url, client=c)
    assert res["result"]["turn"] == 2 and res["name"] == "idle-remote"


def test_run_bot_requires_a_game(server):
    with pytest.raises(ValueError):
        run_bot("idle", server.url)


def test_cli_runs_a_builtin_bot(server, capsys):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, max_turns=3, bots=["idle"], turn_timeout=0.05, turn_delay=0)
    assert main(["--url", server.url, "--bot", "idle", "--name", "CliBot", "--game", gid, "--quiet"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["game_id"] == gid and res["name"] == "CliBot" and res["result"]["turn"] == 2
    assert main(["--url", server.url, "--bot", "idle", "--game", "g999", "--quiet"]) == 1


def test_summary_shows_deals_contracts_reputation_and_public_log():
    from agentciv.client import bundle_str, describe_event
    g = new_game(3, seed=4)
    g.step()
    r = g.diplomacy("p1", [{"type": "propose", "to": "p2", "give": {"wood": 10, "tiles": []}, "get": {"gold": 5},
                            "message": "wood for gold"},
                           {"type": "propose", "to": "p3", "give": {"per_turn": {"gold": 2}, "turns": 3},
                            "get": {"wood": 5}, "peace": 20}])
    assert all(x["ok"] for x in r)
    assert g.diplomacy("p3", [{"type": "accept", "deal": "d2"}])[0]["ok"]
    p2 = summarize_view(g.player_view("p2"))
    assert 'd1 TO YOU from p1: p1 gives 10 wood; p2 gives 5 gold' in p2 and '"wood for gold"' in p2
    assert '{"type":"accept","deal":"d1"}' in p2 and "deliverable now" in p2
    assert "c1: p1 pays p3 2 gold/turn, 3 turns left" in p2
    assert "Recent public deals: t1 d2: p1 gives 2 gold/turn for 3 turns; p3 gives 5 wood; peace 20" in p2
    assert "deals 1, honoured 0, defaults 0, betrayals 0" in p2
    p1 = summarize_view(g.player_view("p1"))
    assert "d1 (yours, waiting for p2)" in p1 and "<- you pay" in p1 and "Your recently closed deals: d2 accepted" in p1
    spec = summarize_view(g.spectator_view())
    assert "Recent public deals" in spec and "d1" not in spec.split("Recent public deals")[0]
    # an older server's view (no "deals") still renders its trade offers
    legacy = g.player_view("p2")
    del legacy["deals"]
    assert "Trade offer d1" in summarize_view(legacy)
    assert bundle_str({"wood": 3, "tiles": [[1, 2], [3, 4]], "per_turn": {"gold": 1}, "turns": 4}) == \
        "3 wood + tiles [1,2] [3,4] + 1 gold/turn for 4 turns"
    assert bundle_str({}) == "nothing"
    items = g.inbox("p2", 0)["items"]
    lines = [describe_event(e, "p2") for e in items]
    assert "proposed deal d1 to you" in lines[0]
    assert any("deal d2 EXECUTED" in line for line in lines)


def test_sdk_barter_helpers(server):
    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, turn_timeout=0, rated=False)
    a, b = AgentCivClient(server.url), AgentCivClient(server.url)
    a.join(gid, "A")
    b.join(gid, "B")
    a.wait(since_turn=-1, timeout=10)
    r = a.propose(b.player_id, give={"wood": 5}, get={"gold": 4}, peace=20, message="deal?", expires_in=3)
    assert r["ok"] and r["deal"] == "d1" and r["seq"] >= 2
    d = b.state()["deals"]["open"][0]
    assert d["peace"] == 20 and d["expires_turn"] == 3 and d["message"] == "deal?"
    assert b.counter("d1", give={"gold": 3}, get={"wood": 5})["countered"] == "d1"
    assert a.reject("d2", "no")["ok"]
    assert a.propose(b.player_id, give={"wood": 1})["deal"] == "d3"
    assert a.withdraw("d3")["ok"]
    assert not b.accept("d3")["ok"]
    assert b.say("all", "hi")["ok"]
    res = a.diplomacy({"type": "say", "to": b.player_id, "text": "x"}, turn=0)
    assert res["ok"] and res["turn"] == 0
    with pytest.raises(ApiError) as ei:
        a.diplomacy([], turn=3)
    assert ei.value.status == 409
    types = [e["type"] for e in b.inbox(timeout=1)["items"]]
    assert types == ["deal_proposed", "deal_rejected", "deal_proposed", "deal_withdrawn", "say"]
    assert b.inbox(timeout=0.1)["items"] == []  # remembers where it left off
    assert len(b.inbox(since=0, timeout=0)["items"]) == 5


def test_run_bot_recomputes_orders_after_its_own_post_order_accept(server):
    """The inbox never shows the bot's own accept, so run_bot must learn from the
    diplomacy result that a deal executed and re-run act() (§13.6)."""
    import time

    class LateAccepter(IdleBot):
        def __init__(self):
            super().__init__(0)
            self.acts: list[tuple[int, int]] = []

        def negotiate(self, view):
            me = view["you"]["id"]
            return [{"type": "accept", "deal": d["id"]} for d in view["deals"]["open"] if d["to"] == me]

        def act(self, view):
            self.acts.append((view["turn"], view["you"]["resources"]["wood"]))
            return []

    c = AgentCivClient(server.url)
    gid = c.create_game(max_players=2, max_turns=2, turn_timeout=0, rated=False)
    b = AgentCivClient(server.url)
    b.join(gid, "Proposer")
    bot, out = LateAccepter(), {}
    t = threading.Thread(target=lambda: out.update(r=run_bot(bot, server.url, game_id=gid, name="Late",
                                                              negotiate_window=0.2)))
    t.start()
    try:
        end = time.monotonic() + 15
        while not bot.acts and time.monotonic() < end:
            time.sleep(0.05)
        assert bot.acts and bot.acts[0][0] == 0  # orders are in
        wood0 = bot.acts[0][1]
        pid = next(p["id"] for p in b.state()["players"] if p["id"] != b.player_id)
        b.propose(pid, give={"gold": 5}, get={"wood": 10})
        while len(bot.acts) < 2 and time.monotonic() < end:
            time.sleep(0.05)
        assert bot.acts[:2] == [(0, wood0), (0, wood0 - 10)]
    finally:
        for turn in (0, 1):
            try:
                b.submit_orders([], turn=turn)
                b.wait(since_turn=turn, timeout=5)
            except ApiError:
                pass
        t.join(30)
    assert out["r"]["result"]["turn"] == 1


def test_summary_shows_relic_guards_and_every_victory_clock():
    g = new_game(2, seed=5)
    r = g.relics[0]
    x, y = g.xy(r)
    g.owner[r] = "p2"
    g.place_units(x, y, "p2", {"infantry": 3})
    g.player("p2").relic_streak = 4
    g._stats = None
    text = summarize_view(g.player_view("p1"))
    relic = next(l for l in text.splitlines() if l.startswith(f"  [{x},{y}] owner p2"))
    assert "guarded" in relic and "3 infantry" in relic and "streak 4" in relic
    p2 = next(l for l in text.splitlines() if l.startswith("  p2 P2: "))
    assert "streak 4/16" in p2 and "capitals 1/" in p2 and "wonder 0/5" in p2


def test_summary_income_matches_what_the_engine_credits_and_warns_of_starvation():
    import re
    g = new_game(2, seed=5)
    for season in ("summer", "winter"):
        while g.player_view("p1")["season"]["name"] != season:
            g.step()
        v = g.player_view("p1")
        before = v["you"]["resources"]["food"]
        shown = int(re.search(r"Food this turn: \d+ \+ (\d+) income - (\d+) upkeep", summarize_view(v)).group(1))
        upkeep = v["you"]["upkeep"]
        g.step()
        after = g.player("p1").resources["food"]
        cap = g.caps("p1")["food"]
        assert after == min(cap, before + shown - upkeep), (season, before, shown, upkeep, after)
    x, y = g.xy(g.player("p1").capital)
    g.place_units(x, y, "p1", {"cavalry": 60})
    g.player("p1").resources["food"] = 10
    g._stats = None
    assert "WARNING: food runs out this turn" in summarize_view(g.player_view("p1"))


def test_summary_puts_my_failed_orders_first():
    g = new_game(2, seed=5)
    g.submit_orders("p1", [{"type": "market", "side": "buy", "resource": "stone", "qty": 150}])
    g.step()
    text = summarize_view(g.player_view("p1"))
    head = text.split("Your cities")[0]
    assert "LAST TURN, THESE FAILED OR HURT YOU:" in head and "cannot afford" in head


def test_order_warnings_market_order_and_recruit_food():
    from agentciv.client import order_warnings
    g = new_game(2, seed=5)
    g.step()
    v = g.player_view("p1")
    gold = v["you"]["resources"]["gold"]
    w = order_warnings(v, [{"type": "market", "side": "buy", "resource": "food", "qty": gold},
                           {"type": "market", "side": "sell", "resource": "stone", "qty": 40}])
    assert any("likely FAIL" in s for s in w) and any("in that order" in s for s in w)
    x, y = g.xy(g.player("p1").capital)
    w = order_warnings(v, [{"type": "recruit", "at": [x, y], "unit": "cavalry", "count": 50}])
    assert any("will FAIL" in s for s in w)
    assert order_warnings(v, []) == []


def test_summary_counts_down_treaties_and_shows_all_resources():
    g = new_game(3, seed=5)
    g.treaties[g._pair("p1", "p2")] = g.turn + 3
    text = summarize_view(g.player_view("p1"))
    line = next(l for l in text.splitlines() if l.startswith("Your treaties"))
    assert line.startswith("Your treaties (1/1 slots used; unpledged bank 0, required bond 0): ")
    assert "p2: peace until turn" in line and "ENDS SOON" in line and "p3: NO treaty" in line
    p2 = next(l for l in text.splitlines() if l.startswith("  p2 P2: "))
    food = g.player("p2").resources["food"]
    assert f"| {food} " in p2  # food, wood, stone, gold, influence are all listed


def test_order_warnings_limits_all_resources_and_open_offers():
    from agentciv.client import order_warnings
    g = new_game(2, seed=5)
    g.step()
    v = g.player_view("p1")
    w = order_warnings(v, [{"type": "market", "side": "buy", "resource": "wood", "qty": 5, "limit": 0.01}])
    assert any("has limit 0.01" in s for s in w)
    x, y = g.xy(g.player("p1").capital)
    wood = v["you"]["resources"]["wood"]
    w = order_warnings(v, [{"type": "recruit", "at": [x, y], "unit": "archer", "count": wood // 15 + 1}])
    assert any("wood" in s and "will FAIL" in s for s in w)
    gold = g.player("p1").resources["gold"]
    res = g.diplomacy("p1", [{"type": "propose", "to": "p2", "give": {"gold": gold}, "get": {}}])
    assert res[0]["ok"], res
    v = g.player_view("p1")
    w = order_warnings(v, [{"type": "recruit", "at": [x, y], "unit": "infantry", "count": 1}])
    assert any("open offers" in s for s in w)
    assert "Promised in your open offers" in summarize_view(v)


def test_describe_contract_default_shows_the_seized_bank_gold():
    from agentciv.client import describe_event
    ev = {"type": "contract_default", "turn": 4, "contract": "c3", "payer": "p1", "payee": "p2",
          "per_turn": {"gold": 50}, "turns_left": 5, "penalty": 125, "debt": 0, "seized": 250, "deal": "d2"}
    line = describe_event(ev, "p2")
    assert line == ("contract c3 DEFAULTED: p1 could not pay you (penalty 125 influence), "
                    "250 gold moved from the payer's bank to the payee")
    fogged = {k: v for k, v in ev.items() if k not in ("per_turn", "penalty", "debt", "seized")}
    assert describe_event(fogged, "p3") == "contract c3 DEFAULTED: p1 could not pay p2"
