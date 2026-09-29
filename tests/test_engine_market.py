"""Market batch auction math."""
import math

import pytest

from agentciv.engine import constants as C
from agentciv.engine import market as M
from agentciv.engine.testing import events_of, run_turn, sandbox


def game():
    g = sandbox(2)
    g.add_city(2, 2, "p1", capital=True)
    g.add_city(12, 12, "p2", capital=True)
    for p in g.players:
        p.resources.update(food=200, wood=200, stone=200, gold=1000)
    return g


def buy(r, q, limit=None):
    o = {"type": "market", "side": "buy", "resource": r, "qty": q}
    if limit is not None:
        o["limit"] = limit
    return o


def sell(r, q, limit=None):
    o = buy(r, q, limit)
    o["side"] = "sell"
    return o


def test_auction_price_formula():
    assert M.auction_price(800, 800, 0) == 1.0
    assert M.auction_price(800, 800, 100) == pytest.approx((800 * 800 / 700 - 800) / 100)
    assert M.auction_price(800, 800, -100) == pytest.approx((800 - 800 * 800 / 900) / 100)


def test_single_buyer():
    g = game()
    R, G = g.pools["food"]
    ev = run_turn(g, {"p1": [buy("food", 100)]})
    p = (R * G / (R - 100) - G) / 100
    cost = math.ceil(100 * p * (1 + C.MARKET_FEE) - 1e-9)
    m = events_of(ev, "market")[0]
    assert m["gold"] == cost and m["price"] == round(p, 4)
    # pool after trade, then 5% reversion
    r_after, g_after = R - 100, R * G / (R - 100)
    exp = [r_after + (R - r_after) * C.MARKET_REVERSION, g_after + (G - g_after) * C.MARKET_REVERSION]
    assert g.pools["food"] == pytest.approx(exp)
    inc = g.stats()["p1"]["income"]
    assert g.player("p1").resources["gold"] == 1000 - cost + inc["gold"]


def test_buyers_and_sellers_share_one_price():
    g = game()
    R, G = g.pools["wood"]
    ev = run_turn(g, {"p1": [buy("wood", 100)], "p2": [sell("wood", 60)]})
    net = 40
    p = (R * G / (R - net) - G) / net
    fills = {e["player"]: e for e in events_of(ev, "market")}
    assert fills["p1"]["price"] == fills["p2"]["price"] == round(p, 4)
    assert fills["p1"]["gold"] == math.ceil(100 * p * 1.05 - 1e-9)
    assert fills["p2"]["gold"] == math.floor(60 * p * 0.95 + 1e-9)


def test_zero_net_uses_spot_price():
    g = game()
    R, G = g.pools["stone"]
    ev = run_turn(g, {"p1": [buy("stone", 50)], "p2": [sell("stone", 50)]})
    for e in events_of(ev, "market"):
        assert e["price"] == round(G / R, 4)


def test_limit_violation_drops_order_and_recomputes():
    g = game()
    R, G = g.pools["food"]
    # p1's big buy would push the price above p2's buy limit: p2 dropped, p1 fills alone
    ev = run_turn(g, {"p1": [buy("food", 150)], "p2": [buy("food", 50, limit=1.05)]})
    fails = [e for e in events_of(ev, "order_failed") if e["player"] == "p2"]
    assert fails and "limit" in fails[0]["reason"]
    m = events_of(ev, "market")
    assert [e["player"] for e in m] == ["p1"]
    assert m[0]["price"] == round((R * G / (R - 150) - G) / 150, 4)


def test_sell_limit_and_insufficient_goods():
    g = game()
    g.player("p2").resources["stone"] = 10
    ev = run_turn(g, {"p1": [sell("stone", 50, limit=5.0)], "p2": [sell("stone", 20)]})
    reasons = {e["player"]: e["reason"] for e in events_of(ev, "order_failed")}
    assert "limit" in reasons["p1"] and "not enough" in reasons["p2"]
    assert not events_of(ev, "market")


def test_cannot_afford():
    g = game()
    g.player("p1").resources["gold"] = 5
    ev = run_turn(g, {"p1": [buy("food", 100)]})
    assert "afford" in events_of(ev, "order_failed")[0]["reason"]


def test_order_size_cap():
    g = game()
    cap = int(g.pools["food"][0] * C.MARKET_MAX_ORDER_FRACTION)
    assert g.submit_orders("p1", [buy("food", cap)]) == []
    assert g.submit_orders("p1", [buy("food", cap + 1)])


def test_market_hall_fee():
    g = game()
    g.cities[g.idx(2, 2)].market_hall = 1
    ev = run_turn(g, {"p1": [buy("food", 20)], "p2": [buy("food", 20)]})
    fills = {e["player"]: e["gold"] for e in events_of(ev, "market")}
    p = events_of(ev, "market")[0]["price"]
    assert fills["p1"] < fills["p2"]
    R, G = 800.0, 800.0
    exact = (R * G / (R - 40) - G) / 40
    assert fills["p1"] == math.ceil(20 * exact * (1 + C.MARKET_HALL_FEE) - 1e-9)
    assert p == round(exact, 4)


def test_pool_reverts_toward_initial():
    g = game()
    g.pools["food"] = [400.0, 1600.0]
    run_turn(g)
    init = g.pool_init["food"]
    k = C.MARKET_REVERSION
    assert g.pools["food"] == pytest.approx([400 + (init[0] - 400) * k, 1600 + (init[1] - 1600) * k])


def test_clear_resource_pure():
    pool = [800.0, 800.0]
    bal = {"a": {"gold": 1000, "food": 0}, "b": {"gold": 0, "food": 100}}
    orders = [M.MarketOrder("a", "buy", "food", 30, None, 0), M.MarketOrder("b", "sell", "food", 30, None, 0)]
    fills, fails, price = M.clear_resource(pool, orders, bal, {"a": 0.05, "b": 0.05})
    assert not fails and price == 1.0 and pool == [800.0, 800.0]
    assert bal["a"] == {"gold": 1000 - 32, "food": 30}
    assert bal["b"] == {"gold": 28, "food": 70}


def test_net_cap_protects_pool():
    pool = [100.0, 100.0]
    bal = {c: {"gold": 10 ** 6, "food": 0} for c in "abcd"}
    orders = [M.MarketOrder(c, "buy", "food", 25, None, 0) for c in "abcd"]
    fills, fails, price = M.clear_resource(pool, orders, bal, {})
    assert len(fills) == 3 and len(fails) == 1 and pool[0] > 0


def test_market_history_limited():
    g = game()
    for _ in range(60):
        g.step()
    v = g.spectator_view()
    assert len(v["market"]["history"]) == C.MARKET_HISTORY_TURNS
    assert v["market"]["history"][-1]["turn"] == 59


# ------------------------------------------------- phantom orders (regressions)
def five():
    from agentciv.engine.testing import new_game
    return new_game(5, seed=3)


def test_broke_players_buys_cannot_trigger_depletion_drop():
    """A player with 0 gold submitting huge buys used to get a real 400-stone
    buy dropped as 'pool depleted' and then lose its own orders for free."""
    g = five()
    g.player("p1").resources["gold"] = 3000
    g.player("p5").resources["gold"] = 0
    ev = run_turn(g, {"p1": [buy("stone", 400)], "p5": [buy("stone", 399) for _ in range(4)]})
    fills = events_of(ev, "market")
    assert [(e["player"], e["qty"]) for e in fills] == [("p1", 400)]
    assert not [e for e in events_of(ev, "order_failed") if e["player"] == "p1"]


def test_phantom_buys_do_not_move_the_price():
    def run(griefer):
        g = five()
        g.player("p1").resources["gold"] = 3000
        orders = {"p1": [buy("wood", 100, limit=1.8)]}
        if griefer:
            for r in g.player("p5").resources:
                g.player("p5").resources[r] = 0
            orders["p5"] = [buy("wood", 499) for _ in range(3)] + [sell("wood", 499)]
        return [(e["player"], e["qty"], e["price"]) for e in events_of(run_turn(g, orders), "market")]
    assert run(True) == run(False) == [("p1", 100, run(False)[0][2])]


def test_layered_limits_cannot_block_a_resource():
    """Layered buy/sell limits used to force a violator in every one of the
    5+1 rounds, so every stone order of every player failed ('did not
    converge') and a rival could not buy the stone for a wonder stage."""
    g = five()
    R, G = g.pools["stone"]
    price = lambda n: M.auction_price(R, G, n)
    need = 160
    gap = 1
    a, b1, s2, b2, s3, b3 = gap, 2 * gap + 1, 3 * gap + 2, 4 * gap + 3, 5 * gap + 4, 1
    base = (b1 + b2 + b3) - (a + s2 + s3) + need
    n = [base, base + a, base + a - b1, base + a - b1 + s2, base + a - b1 + s2 - b2, base + a - b1 + s2 - b2 + s3]
    mid = lambda x, y: (price(x) + price(y)) / 2
    griefer = [sell("stone", a, 1e9), buy("stone", b1, mid(n[0], n[1])), sell("stone", s2, mid(n[2], n[0])),
               buy("stone", b2, mid(n[1], n[3])), sell("stone", s3, mid(n[4], n[2])), buy("stone", b3, mid(n[3], n[5]))]
    v, gr = g.player("p1"), g.player("p2")
    cap = g.cities[v.capital]
    cap.wonder_stage, v.wonder_city, cap.warehouse = 3, v.capital, 1
    v.resources.update(stone=500, wood=500, gold=3000)
    gr.resources.update(stone=a + s2 + s3, gold=150)
    ev = run_turn(g, {"p1": [buy("stone", need), {"type": "build", "at": [cap.x, cap.y], "building": "wonder"}],
                      "p2": griefer})
    assert cap.wonder_stage == 4
    assert not any("converge" in e["reason"] for e in events_of(ev, "order_failed"))


def test_clearing_is_always_valid_and_terminates():
    import random
    rng = random.Random(7)
    for _ in range(300):
        pool = [2000.0, 4000.0]
        players = [f"p{i}" for i in range(1, 6)]
        bal = {p: {"gold": rng.choice([0, 50, 300, 2000]), "stone": rng.choice([0, 20, 200])} for p in players}
        gold0 = {p: b["gold"] for p, b in bal.items()}
        orders = [M.MarketOrder(p, rng.choice(["buy", "sell"]), "stone", rng.randint(1, 500),
                                rng.choice([None, round(rng.uniform(0.5, 6), 2)]), k)
                  for p in players for k in range(rng.randint(0, 4))]
        fills, fails, price = M.clear_resource(pool, orders, bal, {})
        assert len(fills) + len(fails) == len(orders)
        for o, _ in fills:
            assert o.limit is None or (price <= o.limit + 1e-9 if o.side == "buy" else price >= o.limit - 1e-9)
        assert all(b["gold"] >= 0 and b["stone"] >= 0 for b in bal.values())
        assert all(bal[p]["gold"] >= 0 for p in gold0) and pool[0] > 0
