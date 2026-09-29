"""Built-in bots and barter (docs/DESIGN.md §13): the shared deal valuation,
every bot's handling of incoming deals, the bot-specific proposals, the
"never help a player close to winning" guard, and negotiation inside the
tournament runner."""
from __future__ import annotations

import time

import pytest

from agentciv import tournament as T
from agentciv.bots import BOT_NAMES, get_bot
from agentciv.bots import trading
from agentciv.bots.common import DealValuer, World, danger
from agentciv.engine import constants as C
from agentciv.engine.testing import new_game, sandbox, set_terrain

RATIONAL = ["economist", "turtle", "rusher", "strategist"]


def propose(g, frm, to, give, get, peace=None, message=None):
    act = {"type": "propose", "to": to, "give": give, "get": get}
    if peace:
        act["peace"] = peace
    if message:
        act["message"] = message
    res = g.diplomacy(frm, [act])
    assert res[0]["ok"], res
    return res[0]["deal"]


def answers(bot, g, pid, deal_id):
    """Actions ``bot`` (player ``pid``) takes on ``deal_id`` this round."""
    return [a for a in bot.negotiate(g.player_view(pid)) if a.get("deal") == deal_id]


def accepted(actions):
    return any(a["type"] == "accept" for a in actions)


# ---------------------------------------------------------------------------
# incoming deals: every rational bot rejects lopsided offers, takes good ones
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", RATIONAL)
def test_lopsided_offer_rejected(name):
    g = new_game(3, seed=4)
    g.player("p1").resources["gold"] = 400
    bot = get_bot(name, seed=1)
    d = propose(g, "p2", "p1", {"wood": 1}, {"gold": 150})
    acts = answers(bot, g, "p1", d)
    assert not accepted(acts), acts
    # it may counter, but only with a much smaller price
    for a in acts:
        if a["type"] == "counter":
            assert a["give"].get("gold", 0) < 100, a
    assert bot.last_error is None


@pytest.mark.parametrize("name", RATIONAL)
def test_good_offer_accepted_and_settles(name):
    g = new_game(3, seed=4)
    bot = get_bot(name, seed=1)
    d = propose(g, "p2", "p1", {"gold": 45}, {"stone": 8})
    acts = bot.negotiate(g.player_view("p1"))
    assert {"type": "accept", "deal": d} in acts, acts
    before = g.player("p1").resources["gold"]
    res = g.diplomacy("p1", acts)
    assert res[0]["ok"] and res[0]["status"] == "accepted"
    assert g.player("p1").resources["gold"] == before + 45
    assert bot.last_error is None


@pytest.mark.parametrize("name", RATIONAL)
def test_no_deals_with_a_player_close_to_winning(name):
    g = new_game(3, seed=4)
    g.player("p2").resources["gold"] = int(0.9 * C.ECONOMIC_VICTORY_GOLD)
    bot = get_bot(name, seed=1)
    d = propose(g, "p2", "p1", {"gold": 120}, {"stone": 20})
    acts = answers(bot, g, "p1", d)
    assert not accepted(acts)
    assert any(a["type"] == "reject" for a in acts), acts


@pytest.mark.parametrize("name", RATIONAL)
def test_undeliverable_offer_rejected(name):
    g = new_game(3, seed=4)
    bot = get_bot(name, seed=1)
    d = propose(g, "p2", "p1", {"gold": 5000}, {"stone": 5})     # p2 can't pay
    acts = answers(bot, g, "p1", d)
    assert not accepted(acts)


def test_random_bot_takes_some_offers_and_never_raises():
    bot = get_bot("random", seed=3)
    took = 0
    for k in range(12):
        g = new_game(3, seed=10 + k)
        d = propose(g, "p2", "p1", {"wood": 12}, {"gold": 15})      # a bit worse than the market
        acts = bot.negotiate(g.player_view("p1"))
        took += accepted([a for a in acts if a.get("deal") == d])
    assert 0 < took < 12
    assert bot.last_error is None


def test_random_bot_cannot_be_milked():
    """Review finding: random accepted 25% of all deliverable deals, so a
    player could flood it with 'give me everything' proposals in rated
    games (random fills quickmatch seats)."""
    g = new_game(3, seed=5)
    me = g.player("p1")
    tiles = [[i % g.width, i // g.width] for i, o in enumerate(g.owner)
             if o == "p1" and i not in g.cities and i not in g.relic_set][:5]
    scams = [{"food": me.resources["food"], "gold": me.resources["gold"]},
             {"tiles": tiles[:1]},
             {"per_turn": {"gold": 5}, "turns": 30},
             {"wood": me.resources["wood"] // 2, "stone": me.resources["stone"] // 2}]
    for k in range(40):
        bot = get_bot("random", seed=k)
        g._dip_counts = {}                                 # (test only: lift the per-turn action limit)
        for get in scams:
            if get.get("tiles"):
                g.set_owner(tiles[0][0] + 1, tiles[0][1], "p2")   # (land trades need a border)
            d = propose(g, "p2", "p1", {"wood": 1}, get)
            acts = bot.negotiate(g.player_view("p1"))
            assert not accepted([a for a in acts if a.get("deal") == d]), (k, get)
            g.diplomacy("p2", [{"type": "withdraw", "deal": d}])
    # at most one accept per turn, even for fair offers
    bot = get_bot("random", seed=1)
    g._dip_counts = {}
    for _ in range(6):
        propose(g, "p2", "p1", {"wood": 10}, {"gold": 5})
    n = 0
    for _ in range(10):
        acts = bot.negotiate(g.player_view("p1"))
        n += sum(a["type"] == "accept" for a in acts)
        g.diplomacy("p1", acts)
    assert n <= 1


# ---------------------------------------------------------------------------
# valuation
# ---------------------------------------------------------------------------
def test_valuer_needs_and_surplus():
    g = new_game(3, seed=4)
    w = World(g.player_view("p1"))
    v = DealValuer(w, needs={"food": 50, "wood": 40, "stone": 200}, gold_need=0)
    # stone we need costs more to give than surplus food, per unit of value
    assert v.give_cost("p1", "stone", 20) > 20 * v.prices["stone"]
    assert v.give_cost("p1", "food", 20) < 20 * v.prices["food"]
    # receiving what we need is worth more than receiving surplus
    assert v.recv_value("p1", "stone", 20) > v.recv_value("p1", "food", 20) * v.prices["stone"] / v.prices["food"]
    # a contract is worth less than its face value, less from a defaulter
    b = {"per_turn": {"gold": 10}, "turns": 10}
    good = v.contract_value("p1", b, "p2", "p1")
    assert 0 < good < 100
    w.players["p2"]["reputation"] = {"deals": 3, "contracts_honoured": 0, "defaults": 2, "betrayals": 0}
    assert DealValuer(w).contract_value("p1", b, "p2", "p1") < 0.5 * good


def test_valuer_peace_by_threat():
    g = sandbox(2, seed=1)
    g.add_city(5, 5, "p1", capital=True)
    g.add_city(15, 15, "p2", capital=True)
    calm = DealValuer(World(g.player_view("p1"))).peace_value("p1", "p2", 20)
    g.place_units(6, 5, "p2", {"infantry": 15})
    scary = DealValuer(World(g.player_view("p1"))).peace_value("p1", "p2", 20)
    assert scary > calm + 100


def test_guard_projects_wonder_and_net_gold():
    g = sandbox(3, seed=1)
    g.add_city(4, 4, "p1", capital=True)
    c2 = g.add_city(14, 14, "p2", capital=True)
    g.add_city(4, 14, "p3", capital=True)
    c2.wonder_stage = 3
    g.player("p2").wonder_city = c2.idx
    g.player("p2").resources.update(stone=0, wood=0, gold=0)
    g._invalidate()
    v = DealValuer(World(g.player_view("p1")))
    assert danger(v.w, "p2") == pytest.approx(0.6)
    assert not v.helps_winner("p2", {"stone": 10})
    # enough for stage 4: projected 0.8
    assert v.helps_winner("p2", {"stone": 700, "wood": 500, "gold": 600})
    # net gold counts: a loan handed out is not "help" for an economic racer
    g.player("p3").resources["gold"] = int(0.65 * C.ECONOMIC_VICTORY_GOLD)
    v = DealValuer(World(g.player_view("p1")))
    assert v.helps_winner("p3", {"gold": 1000})
    assert not v.helps_winner("p3", {"per_turn": {"gold": 30}, "turns": 30}, 0.7, {"gold": 1000})


# ---------------------------------------------------------------------------
# bot-specific behaviour
# ---------------------------------------------------------------------------
def test_threatened_economist_pays_tribute_for_peace_only_when_threatened():
    def game(army_at):
        g = sandbox(2, seed=1)
        g.add_city(5, 5, "p1", capital=True)
        g.add_city(15, 15, "p2", capital=True)
        g.place_units(*army_at, "p2", {"infantry": 14})
        return g
    for at, ok in (((6, 5), True), ((15, 14), False)):
        g = game(at)
        d = propose(g, "p2", "p1", {}, {"per_turn": {"gold": 3}, "turns": 10}, peace=15, message="[tribute] pay")
        acts = answers(get_bot("economist", seed=1), g, "p1", d)
        assert accepted(acts) == ok, (at, acts)


def test_rusher_extorts_a_threatened_neighbour():
    g = sandbox(3, seed=1)
    g.add_city(4, 4, "p1", capital=True)          # weak neighbour, army next to it
    g.add_city(12, 12, "p2", capital=True)        # the rusher
    g.add_city(15, 12, "p3", capital=True)        # its (closer) target
    g.place_units(12, 12, "p2", {"infantry": 3})
    g.place_units(5, 4, "p2", {"infantry": 12})
    g.player("p1").resources["gold"] = 300
    bot = get_bot("rusher", seed=1)
    bot.memory["hard"] = {g.idx(4, 4): 99}      # its capital is a tough nut: target p3
    acts = bot.negotiate(g.player_view("p2"))
    tribute = [a for a in acts if a["type"] == "propose" and a["to"] == "p1"]
    assert tribute, acts
    a = tribute[0]
    assert a["get"].get("per_turn", {}).get("gold", 0) > 0 and a.get("peace")
    assert a["message"].startswith("[tribute]")
    res = g.diplomacy("p2", tribute)
    assert res[0]["ok"]


def test_economist_offers_loans_to_solvent_players():
    g = sandbox(2, seed=1)
    g.add_city(4, 4, "p1", capital=True)
    c2 = g.add_city(14, 14, "p2", capital=True)
    for x in range(9, 15):
        set_terrain(g, x, 11, "g")               # gold income for the borrower
        g.set_owner(x, 11, "p2")
    c2.wonder_stage = 1                          # a wonder builder: needs gold
    g.player("p2").wonder_city = c2.idx
    g.turn = 20
    g.player("p1").resources["gold"] = 2000
    g._invalidate()
    bot = get_bot("economist", seed=1)
    acts = bot.negotiate(g.player_view("p1"))
    loans = [a for a in acts if a["type"] == "propose" and a["message"].startswith("[loan]")]
    assert loans, acts
    a = loans[0]
    assert a["give"]["gold"] > 0 and a["get"]["per_turn"]["gold"] * a["get"]["turns"] > a["give"]["gold"]
    assert g.diplomacy("p1", loans)[0]["ok"]


def _strategist_seller(stage):
    """p1 (strategist) has stone overflowing its cap; p2 builds a wonder."""
    g = sandbox(2, seed=1)
    g.add_city(4, 4, "p1", capital=True)
    c2 = g.add_city(14, 14, "p2", capital=True)
    for x in range(0, 12):
        for y in (8, 9):
            set_terrain(g, x, y, "h")
            g.set_owner(x, y, "p1")
    g.player("p1").resources.update(stone=300, gold=100)
    c2.wonder_stage = stage
    g.player("p2").wonder_city = c2.idx
    g.player("p2").resources.update(stone=0, wood=2000, gold=6000)
    g._invalidate()
    return g


def test_strategist_sells_stone_to_a_wonder_builder_only_far_from_a_win():
    early = _strategist_seller(1)
    acts = get_bot("strategist", seed=1).negotiate(early.player_view("p1"))
    sales = [a for a in acts if a["type"] == "propose" and a["to"] == "p2" and a["give"].get("stone")]
    assert sales, acts
    s = sales[0]
    v = DealValuer(World(early.player_view("p1")))
    qty = s["give"]["stone"]
    assert s["get"]["gold"] >= 1.1 * qty * v.sell_unit("p1", "stone", qty)   # beats the market
    assert early.diplomacy("p1", sales)[0]["ok"]
    late = _strategist_seller(3)
    acts = get_bot("strategist", seed=1).negotiate(late.player_view("p1"))
    assert not [a for a in acts if a["type"] == "propose" and a["to"] == "p2" and a["give"].get("stone")]


def test_strategist_haggles_and_concedes():
    """Counters ask for more than the offer, then concede toward it."""
    g = new_game(2, seed=4)
    g.player("p1").resources.update(stone=40, gold=200)
    bot = get_bot("strategist", seed=1)
    # p2 wants 30 stone for a price below what it is worth to p1
    d1 = propose(g, "p2", "p1", {"gold": 40}, {"stone": 30}, message="[buy] stone")
    acts = answers(bot, g, "p1", d1)
    counters = [a for a in acts if a["type"] == "counter"]
    if not counters:          # no zone of agreement from its point of view
        assert not accepted(acts)
        return
    c1 = counters[0]
    ask1 = c1["get"].get("gold", 0) - c1["give"].get("gold", 0)
    assert ask1 > 40
    res = g.diplomacy("p1", acts)
    new = res[0]["deal"]
    # p2 counters back half-way; the strategist's next ask is lower
    mid = (40 + ask1) // 2
    r2 = g.diplomacy("p2", [{"type": "counter", "deal": new, "give": {"gold": mid}, "get": {"stone": 30},
                             "message": "[buy] meet halfway"}])
    assert r2[0]["ok"]
    acts2 = answers(bot, g, "p1", r2[0]["deal"])
    if accepted(acts2):
        return
    c2 = [a for a in acts2 if a["type"] == "counter"]
    assert c2, acts2
    ask2 = c2[0]["get"].get("gold", 0) - c2[0]["give"].get("gold", 0)
    assert mid < ask2 < ask1


def test_strategist_refuses_to_pay_others_loans_but_borrows_on_its_terms():
    g = new_game(3, seed=4)
    bot = get_bot("strategist", seed=1)
    d = propose(g, "p2", "p1", {"gold": 300}, {"per_turn": {"gold": 12}, "turns": 30},
                message="[loan] a generous loan")
    acts = answers(bot, g, "p1", d)
    assert not accepted(acts)


def test_notrade_strategist_never_negotiates():
    g = new_game(3, seed=4)
    propose(g, "p2", "p1", {"gold": 45}, {"stone": 8})
    assert get_bot("strategist_notrade", seed=1).negotiate(g.player_view("p1")) == []
    assert "strategist_notrade" in BOT_NAMES


def test_war_supplies_are_not_sold_to_an_army_at_our_gates():
    g = sandbox(2, seed=1)
    g.add_city(5, 5, "p1", capital=True)
    g.add_city(15, 15, "p2", capital=True)
    g.player("p1").resources["food"] = 280
    g.place_units(6, 5, "p2", {"cavalry": 10})
    d = propose(g, "p2", "p1", {"gold": 400}, {"food": 60})
    acts = answers(get_bot("turtle", seed=1), g, "p1", d)
    assert not accepted(acts)


def test_rusher_breaks_peace_with_a_weak_partner_that_stopped_paying():
    g = sandbox(2, seed=1)
    g.add_city(5, 5, "p1", capital=True)
    g.add_city(12, 5, "p2", capital=True)
    g.place_units(6, 5, "p2", {"infantry": 20})
    g.treaties[g._pair("p1", "p2")] = g.turn + 20
    g.player("p2").resources["influence"] = 100
    g._invalidate()
    orders = get_bot("rusher", seed=1).act(g.player_view("p2"))
    assert {"type": "break_treaty", "with": "p1"} in orders


def test_planner_reserves_contract_instalments():
    g = new_game(2, seed=4)
    g.player("p1").resources["gold"] = 60
    g.contracts.append({"id": "c1", "payer": "p1", "payee": "p2", "per_turn": {"gold": 50},
                        "turns_left": 5, "deal": "d0"})
    g._invalidate()
    bot = get_bot("turtle", seed=1)
    bot.act(g.player_view("p1"))
    assert bot.reserved.get("gold", 0) >= 50 - bot.raw.get("gold", 0)
    rusher = get_bot("rusher", seed=1)
    rusher.act(g.player_view("p1"))
    # the payee's army is no weaker than ours: the rusher pays too
    assert rusher.contract_due.get("gold") == 50


# ---------------------------------------------------------------------------
# robustness & speed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", [b for b in BOT_NAMES if b != "idle"])
def test_negotiate_handles_odd_views(name):
    bot = get_bot(name, seed=1)
    assert bot.negotiate({}) == []
    assert bot.negotiate({"you": None}) == []
    g = new_game(3, seed=2)
    v = g.player_view("p1")
    v["status"] = "finished"
    assert bot.negotiate(v) == []
    v = g.player_view("p1")
    v["deals"]["open"] = [{"id": "d9", "from": "p9", "to": "p1", "give": {"gold": "x"}, "get": None,
                           "status": "open", "thread": "d9"},
                          {"id": "d8", "from": "p2", "to": "p1", "give": {"gold": 5}, "get": {"tiles": [[99, 99]]},
                           "status": "open", "thread": "d8", "problem": None}]
    v["contracts"] = [{"payer": "p1", "payee": "p2", "per_turn": {"gold": 1}, "turns_left": 2}]
    out = bot.negotiate(v)
    assert isinstance(out, list)
    assert getattr(bot, "last_error", None) is None, bot.last_error


def test_dry_negotiation_does_not_change_play(monkeypatch):
    """Negotiating (valuations, proposals) without sending anything must not
    change what the bots do in act(): no hidden side effects."""
    field = ["strategist", "economist", "rusher", "turtle"]
    a = T.run_game(field, seed=6, max_turns=40, rounds=0)
    orig = trading.Trader.decide_deals

    def dry(self, view):
        orig(self, view)
        return []
    monkeypatch.setattr(trading.Trader, "decide_deals", dry)
    b = T.run_game(field, seed=6, max_turns=40, rounds=3)
    assert a["placements"] == b["placements"] and a["scores"] == b["scores"]


def test_game_with_barter_runs_clean_and_fast():
    r = T.run_game(["strategist", "economist", "rusher", "turtle", "random", "random"], seed=5, max_turns=70)
    assert not r["bot_exceptions"] and not r["last_errors"], (r["bot_exceptions"], r["last_errors"])
    assert r["deals_executed"] > 0
    assert sum(t["deals"] for t in r["trade"].values()) >= 2 * r["deals_executed"] - 1
    for lab, t in r["trade"].items():
        assert t["diplomacy_errors"] <= 2, (lab, t)
    for lab, ms in r["negotiate_ms_per_call"].items():
        assert ms < 20, (lab, ms)     # typical is 1-2 ms


def test_bots_negotiate_deterministically():
    field = ["strategist", "economist", "rusher", "turtle", "random"]
    a = T.run_game(field, seed=8, max_turns=35)
    b = T.run_game(field, seed=8, max_turns=35)
    assert a["trade"] == b["trade"] and a["deal_kinds"] == b["deal_kinds"]
    assert a["placements"] == b["placements"]


def test_negotiate_is_fast():
    g = new_game(6, seed=3)
    bots = {pid: get_bot(n, seed=k) for k, (pid, n) in
            enumerate(zip(g.alive_players(), ["strategist", "economist", "rusher", "turtle", "random", "random"]))}
    for _ in range(25):
        for pid in g.alive_players():
            g.diplomacy(pid, bots[pid].negotiate(g.player_view(pid)))
        for pid in g.alive_players():
            g.submit_orders(pid, bots[pid].act(g.player_view(pid)))
        g.step()
    for pid, b in bots.items():
        v = g.player_view(pid)
        t0 = time.perf_counter()
        for _ in range(5):
            b.negotiate(v)
        ms = 1000 * (time.perf_counter() - t0) / 5
        assert ms < 20, (b.name, ms)


@pytest.mark.parametrize("name", RATIONAL + ["strategist_lite"])
def test_bots_do_not_pay_for_land_an_army_can_retake(name):
    """Review finding: bots paid for a tile next to the seller's army, which
    walked onto it (undefended land is captured) the next turn."""
    for army, peace in ((False, None), (True, None), (True, 20)):
        g = sandbox(2, seed=1)
        g.add_city(4, 4, "p1", capital=True)
        g.add_city(14, 14, "p2", capital=True)
        for x in range(6, 12):
            g.set_owner(x, 4, "p2")
            set_terrain(g, x, 4, "f")
        if army:
            g.place_units(7, 5, "p2", {"infantry": 1})
        g.player("p1").resources["gold"] = 300
        g._invalidate()
        d = propose(g, "p2", "p1", {"tiles": [[6, 4]]}, {"gold": 40}, peace=peace)
        bot = get_bot(name, seed=1)
        acts = answers(bot, g, "p1", d)
        if army and not peace:
            assert not accepted(acts), acts
        elif not army:
            assert accepted(acts), acts             # the same land, unthreatened, is worth 40 gold
        v = DealValuer(World(g.player_view("p1")))
        assert (v.tile_value("p1", [6, 4], False, "p2", peace or 0) == 0) == (army and not peace)


def test_credit_limit_for_loans():
    """Review finding: bots lent large sums to players with no history, who
    could spend the gold and default."""
    g = new_game(3, seed=4)
    g.turn = 20
    g.player("p1").resources["gold"] = 1000
    w = World(g.player_view("p1"))
    v = DealValuer(w, discount=0.995)
    assert v.credit_limit("p2") == 150
    loan = {"from": "p2", "to": "p1", "give": {"per_turn": {"gold": 40}, "turns": 10}, "get": {"gold": 300}}
    small = {"from": "p2", "to": "p1", "give": {"per_turn": {"gold": 12}, "turns": 10}, "get": {"gold": 100}}
    assert v.deal_gain(loan) < 0 and v.deal_gain(small) > 0
    # tribute (nothing handed over now) is not capped
    tribute = {"from": "p2", "to": "p1", "give": {"per_turn": {"gold": 40}, "turns": 10}, "get": {}}
    assert v.deal_gain(tribute) > v.credit_limit("p2")
    w.players["p2"]["reputation"] = {"deals": 3, "contracts_honoured": 3, "defaults": 0, "betrayals": 0}
    assert DealValuer(w).credit_limit("p2") == 600
    w.players["p2"]["reputation"]["defaults"] = 1
    assert DealValuer(w).credit_limit("p2") == 0
