"""Order pre-validation and the action phase (build/claim/settle/recruit/disband)."""
import pytest

from agentciv.engine import constants as C
from agentciv.engine.rules import building_cost, claim_cost, settle_cost, unit_cost
from agentciv.engine.testing import events_of, new_game, run_turn, sandbox, set_terrain


def two_city_sandbox():
    g = sandbox(2)
    g.add_city(3, 3, "p1", capital=True)
    g.add_city(12, 12, "p2", capital=True)
    return g


def failed(events, pid=None):
    return [e for e in events_of(events, "order_failed") if pid is None or e["player"] == pid]


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("bad", [None, 5, "orders", {"type": "claim"}, 3.5])
def test_non_list_orders(bad):
    g = new_game(2)
    errs = g.submit_orders("p1", bad)
    assert errs and errs[0]["index"] == -1
    assert g.has_submitted("p1") is True
    g.step()


def test_unknown_player_and_not_running():
    g = new_game(2, start=False)
    assert g.submit_orders("p1", [])[0]["index"] == -1
    g.start()
    assert g.submit_orders("p9", [])[0]["index"] == -1
    assert g.submit_orders(None, [])[0]["index"] == -1


@pytest.mark.parametrize("order,fragment", [
    ({"type": "nope"}, "unknown order type"),
    ({}, "unknown order type"),
    ("claim", "must be an object"),
    ({"type": "claim"}, "[x, y]"),
    ({"type": "claim", "at": [99, 0]}, "off the map"),
    ({"type": "claim", "at": [True, 0]}, "integer"),
    ({"type": "claim", "at": [1, 2, 3]}, "[x, y]"),
    ({"type": "recruit", "city": [0, 0], "unit": "infantry"}, "do not own a city"),
    ({"type": "market", "side": "hold", "resource": "food", "qty": 1}, "side"),
    ({"type": "market", "side": "buy", "resource": "influence", "qty": 1}, "resource"),
    ({"type": "market", "side": "buy", "resource": "food", "qty": 0}, "qty"),
    ({"type": "market", "side": "buy", "resource": "food", "qty": 10 ** 9}, "25%"),
    ({"type": "market", "side": "buy", "resource": "food", "qty": 5, "limit": -1}, "limit"),
    ({"type": "offer_trade", "to": "p1", "give": {"wood": 1}}, "yourself"),
    ({"type": "offer_trade", "to": "p2", "give": {"influence": 1}}, "not tradable"),
    ({"type": "offer_trade", "to": "p2"}, "give or want"),
    ({"type": "accept_trade", "offer_id": "t99"}, "no open trade offer"),
    ({"type": "propose_treaty", "to": "p2", "turns": 5}, "turns"),
    ({"type": "accept_treaty", "from": "p2"}, "no treaty proposal"),
    ({"type": "break_treaty", "with": "p2"}, "no treaty"),
    ({"type": "message", "to": "p2", "text": ""}, "empty"),
    ({"type": "message", "to": "p2", "text": "x" * 501}, "longer"),
    ({"type": "message", "to": "zz", "text": "hi"}, "unknown player"),
    ({"type": "build", "at": [0, 0], "building": "castle"}, "unknown building"),
    ({"type": "disband", "at": [0, 0]}, "no units"),
    ({"type": "move", "from": [0, 0], "to": [1, 0]}, "no units"),
])
def test_prevalidation_errors(order, fragment):
    g = new_game(2)
    errs = g.submit_orders("p1", [order])
    assert len(errs) == 1 and errs[0]["index"] == 0
    assert fragment in errs[0]["error"]


def test_too_many_orders_and_messages():
    g = new_game(2)
    orders = [{"type": "message", "to": "all", "text": f"m{i}"} for i in range(7)]
    orders += [{"type": "disband", "at": [0, 0]}] * 100
    errs = g.submit_orders("p1", orders)
    idx = {e["index"] for e in errs}
    assert {5, 6} <= idx                         # messages over the per-turn limit
    assert set(range(100, 107)) <= idx           # orders over the cap
    run_turn(g)
    assert len([m for m in g.messages if m["from"] == "p1"]) == 5


def test_resubmit_replaces():
    g = two_city_sandbox()
    g.submit_orders("p1", [{"type": "claim", "at": [5, 3]}])
    g.submit_orders("p1", [{"type": "claim", "at": [3, 5]}])
    run_turn(g)
    assert g.owner[g.idx(3, 5)] == "p1" and g.owner[g.idx(5, 3)] is None


def test_lenient_formats():
    g = two_city_sandbox()
    g.place_units(3, 3, "p1", {"infantry": 2})
    errs = g.submit_orders("p1", [
        {"type": "claim", "at": {"x": 5, "y": 3}},
        {"type": "claim", "at": ["3", 5.0]},
        {"type": "recruit", "at": [3, 3], "unit": "archer"},   # alias + default count 1
        {"type": "move", "from": [3, 3], "to": [3, 2]},         # all units
    ])
    assert errs == []
    run_turn(g)
    assert g.owner[g.idx(5, 3)] == "p1" and g.owner[g.idx(3, 5)] == "p1"
    assert g.armies[g.idx(3, 2)]["p1"] == {"infantry": 2}
    assert g.armies[g.idx(3, 3)]["p1"] == {"archer": 1}


# ---------------------------------------------------------------- move validation
def test_move_validation():
    g = two_city_sandbox()
    set_terrain(g, 4, 4, "m")
    g.place_units(3, 4, "p1", {"infantry": 2, "cavalry": 2})
    ok = [
        {"type": "move", "from": [3, 4], "to": [2, 4], "units": {"infantry": 1}},
        {"type": "move", "from": [3, 4], "path": [[3, 5], [3, 6]], "units": {"cavalry": 2}},
    ]
    assert g.submit_orders("p1", ok) == []
    bad = [
        {"type": "move", "from": [3, 4], "to": [4, 4]},                                  # mountain
        {"type": "move", "from": [3, 4], "to": [5, 4]},                                  # not adjacent
        {"type": "move", "from": [3, 4], "path": [[2, 4], [1, 4]], "units": {"infantry": 1}},  # 2 steps
        {"type": "move", "from": [3, 4], "to": [2, 4], "units": {"infantry": 3}},        # too many
        {"type": "move", "from": [3, 4], "path": [], "units": {"infantry": 1}},          # empty path
        {"type": "move", "from": [3, 4], "to": [2, 4], "units": {"dragon": 1}},
    ]
    errs = g.submit_orders("p1", bad)
    assert [e["index"] for e in errs] == list(range(len(bad)))
    # cumulative split: second order exceeds what is left
    errs = g.submit_orders("p1", [
        {"type": "move", "from": [3, 4], "to": [2, 4], "units": {"infantry": 2}},
        {"type": "move", "from": [3, 4], "to": [3, 5], "units": {"infantry": 1}},
    ])
    assert [e["index"] for e in errs] == [1]


def test_split_stack_and_cavalry_double_move():
    g = two_city_sandbox()
    g.place_units(3, 4, "p1", {"infantry": 3, "cavalry": 2})
    run_turn(g, {"p1": [
        {"type": "move", "from": [3, 4], "to": [2, 4], "units": {"infantry": 1}},
        {"type": "move", "from": [3, 4], "to": [4, 4], "units": {"infantry": 1}},
        {"type": "move", "from": [3, 4], "path": [[3, 5], [3, 6]], "units": {"cavalry": 2}},
    ]})
    a = g.armies
    assert a[g.idx(2, 4)]["p1"] == {"infantry": 1}
    assert a[g.idx(4, 4)]["p1"] == {"infantry": 1}
    assert a[g.idx(3, 4)]["p1"] == {"infantry": 1}
    assert a[g.idx(3, 6)]["p1"] == {"cavalry": 2}


def test_two_step_blocked_by_hostile_army():
    g = two_city_sandbox()
    g.place_units(3, 4, "p1", {"cavalry": 1})
    g.place_units(3, 5, "p2", {"infantry": 1})
    errs = g.submit_orders("p1", [{"type": "move", "from": [3, 4], "path": [[3, 5], [3, 6]]}])
    assert errs and "hostile" in errs[0]["error"]


# ---------------------------------------------------------------- claim
def test_claim_costs_influence_and_chains():
    g = two_city_sandbox()
    p = g.player("p1")
    tiles = p.tiles
    inf = p.resources["influence"]
    ev = run_turn(g, {"p1": [{"type": "claim", "at": [5, 3]}, {"type": "claim", "at": [6, 3]}]})
    assert g.owner[g.idx(5, 3)] == "p1" and g.owner[g.idx(6, 3)] == "p1"
    cost = claim_cost(tiles) + claim_cost(tiles + 1)
    income = g.stats()["p1"]["income"]["influence"]
    assert p.resources["influence"] == inf - cost + income
    assert len(events_of(ev, "claim")) == 2


def test_claim_rejected_not_adjacent_and_hostile_units():
    g = two_city_sandbox()
    errs = g.submit_orders("p1", [{"type": "claim", "at": [8, 8]}])
    assert "adjacent" in errs[0]["error"]
    g.place_units(5, 3, "p2", {"infantry": 1})
    ev = run_turn(g, {"p1": [{"type": "claim", "at": [5, 3]}]})
    assert g.owner[g.idx(5, 3)] is None
    assert "hostile" in failed(ev, "p1")[0]["reason"]


def test_claim_fails_without_influence():
    g = two_city_sandbox()
    g.player("p1").resources["influence"] = 0
    ev = run_turn(g, {"p1": [{"type": "claim", "at": [5, 3]}]})
    assert g.owner[g.idx(5, 3)] is None
    assert "influence" in failed(ev, "p1")[0]["reason"]


def test_claim_contention_refunds():
    g = sandbox(2)
    g.add_city(3, 3, "p1", capital=True)
    g.add_city(7, 3, "p2", capital=True)
    before = [p.resources["influence"] for p in g.players]
    ev = run_turn(g, {"p1": [{"type": "claim", "at": [5, 3]}], "p2": [{"type": "claim", "at": [5, 3]}]})
    assert g.owner[g.idx(5, 3)] is None
    assert len(failed(ev)) == 2 and all("contested" in e["reason"] for e in failed(ev))
    inc = g.stats()
    for p, b in zip(g.players, before):
        assert p.resources["influence"] == b + inc[p.id]["income"]["influence"]


# ---------------------------------------------------------------- settle
def test_settle_founds_city_and_scales_cost():
    g = two_city_sandbox()
    p = g.player("p1")
    p.resources.update(food=300, wood=300, stone=300, gold=300)
    for x in range(5, 8):
        g.set_owner(x, 3, "p1")
    ev = run_turn(g, {"p1": [{"type": "settle", "at": [7, 3]}]})
    c = g.cities.get(g.idx(7, 3))
    assert c is not None and c.owner == "p1" and not c.capital
    assert events_of(ev, "city_founded")
    assert all(g.owner[j] == "p1" for j in g.radius(g.idx(7, 3), 1))
    assert settle_cost(2) == {r: int(v * 1.5) for r, v in C.SETTLE_BASE_COST.items()}
    assert g.player_view("p1")["you"]["settle_cost"] == settle_cost(2)


def test_settle_rules():
    g = two_city_sandbox()
    g.player("p1").resources.update(food=300, wood=300, stone=300, gold=300)
    errs = g.submit_orders("p1", [{"type": "settle", "at": [5, 3]}])
    assert "too close" in errs[0]["error"]
    errs = g.submit_orders("p1", [{"type": "settle", "at": [9, 9]}])
    assert "adjacent" in errs[0]["error"]
    r = g.relics[0]
    errs = g.submit_orders("p1", [{"type": "settle", "at": [r % g.width, r // g.width]}])
    assert "relic" in errs[0]["error"]


def test_settle_contention_within_radius():
    g = sandbox(2)
    g.add_city(2, 2, "p1", capital=True)
    g.add_city(12, 2, "p2", capital=True)
    for x in range(3, 12):
        g.set_owner(x, 2, "p1" if x < 7 else "p2")
    for p in g.players:
        p.resources.update(food=300, wood=300, stone=300, gold=300)
    ev = run_turn(g, {"p1": [{"type": "settle", "at": [6, 2]}], "p2": [{"type": "settle", "at": [8, 2]}]})
    assert len(g.cities) == 2
    assert all("contested" in e["reason"] for e in failed(ev))
    assert g.player("p1").resources["gold"] >= 300


def test_settle_unaffordable():
    g = two_city_sandbox()
    g.player("p1").resources["food"] = 0
    for x in range(5, 8):
        g.set_owner(x, 3, "p1")
    ev = run_turn(g, {"p1": [{"type": "settle", "at": [7, 3]}]})
    assert len(g.cities) == 2 and "afford" in failed(ev, "p1")[0]["reason"]


# ---------------------------------------------------------------- build
def test_build_improvement_and_income():
    g = two_city_sandbox()
    set_terrain(g, 5, 3, "f")
    g.set_owner(5, 3, "p1")
    base = g.stats()["p1"]["income"]["wood"]
    p = g.player("p1")
    w0, g0 = p.resources["wood"], p.resources["gold"]
    run_turn(g, {"p1": [{"type": "build", "at": [5, 3], "building": "lumber_mill"}]})
    assert g.improvement[g.idx(5, 3)] == "lumber_mill"
    assert g.stats()["p1"]["income"]["wood"] == base + 2
    cost = C.IMPROVEMENTS["lumber_mill"]["cost"]
    assert p.resources["gold"] == g0 - cost["gold"] + g.stats()["p1"]["income"]["gold"]
    assert p.resources["wood"] == w0 - cost["wood"] + base + 2   # built before the economy phase


def test_build_validation():
    g = two_city_sandbox()
    g.set_owner(5, 3, "p1")
    errs = g.submit_orders("p1", [
        {"type": "build", "at": [5, 3], "building": "mine"},       # wrong terrain
        {"type": "build", "at": [9, 9], "building": "farm"},       # not owned
        {"type": "build", "at": [3, 3], "building": "farm"},       # city tile
        {"type": "build", "at": [5, 3], "building": "farm"},       # ok
        {"type": "build", "at": [5, 3], "building": "temple"},     # already improved (in this list)
        {"type": "build", "at": [5, 3], "building": "walls"},      # no city
    ])
    assert [e["index"] for e in errs] == [0, 1, 2, 4, 5]


def test_walls_warehouse_market_hall():
    g = two_city_sandbox()
    p = g.player("p1")
    p.resources.update(food=300, wood=300, stone=300, gold=300)
    run_turn(g, {"p1": [
        {"type": "build", "at": [3, 3], "building": "walls"},
        {"type": "build", "at": [3, 3], "building": "walls"},
        {"type": "build", "at": [3, 3], "building": "warehouse"},
        {"type": "build", "at": [3, 3], "building": "market_hall"},
    ]}, check=False)
    c = g.cities[g.idx(3, 3)]
    assert (c.walls, c.warehouse, c.market_hall) == (2, 1, 1)
    spent_stone = building_cost("walls", 1)["stone"] + building_cost("walls", 2)["stone"] \
        + building_cost("warehouse", 1)["stone"] + building_cost("market_hall", 1)["stone"]
    assert p.resources["stone"] == 300 - spent_stone + g.stats()["p1"]["income"]["stone"]
    assert g.caps("p1")["food"] == C.STORAGE_BASE + C.WAREHOUSE_STORAGE
    assert g.player_view("p1")["you"]["market_fee"] == C.MARKET_HALL_FEE
    assert g.stats()["p1"]["income"]["gold"] == C.CITY_YIELD["gold"] + C.MARKET_HALL_GOLD
    errs = g.submit_orders("p1", [{"type": "build", "at": [3, 3], "building": "warehouse"}])
    assert "max level" in errs[0]["error"]


def test_wonder_rules():
    g = two_city_sandbox()
    g.add_city(3, 10, "p1")
    p = g.player("p1")
    p.resources.update(food=300, wood=300, stone=300, gold=900)
    errs = g.submit_orders("p1", [
        {"type": "build", "at": [3, 3], "building": "wonder"},
        {"type": "build", "at": [3, 3], "building": "wonder"},
    ])
    assert [e["index"] for e in errs] == [1]
    ev = run_turn(g)
    assert g.cities[g.idx(3, 3)].wonder_stage == 1 and p.wonder_city == g.idx(3, 3)
    assert events_of(ev, "wonder_stage")[0]["stage"] == 1
    errs = g.submit_orders("p1", [{"type": "build", "at": [3, 10], "building": "wonder"}])
    assert "wonder is in" in errs[0]["error"]
    assert g.stats()["p1"]["wonder_stage"] == 1


# ---------------------------------------------------------------- recruit / disband
def test_recruit_spawns_after_movement():
    g = two_city_sandbox()
    p = g.player("p1")
    before = dict(p.resources)
    ev = run_turn(g, {"p1": [{"type": "recruit", "city": [3, 3], "unit": "cavalry", "count": 2}]})
    assert g.armies[g.idx(3, 3)]["p1"] == {"cavalry": 2}
    cost = unit_cost("cavalry", 2)
    inc = g.stats()["p1"]["income"]
    assert p.resources["gold"] == before["gold"] - cost["gold"] + inc["gold"]
    assert p.resources["food"] == before["food"] - cost["food"] + inc["food"] - 2 * C.UNITS["cavalry"]["upkeep"]
    assert events_of(ev, "recruit")


def test_recruit_unaffordable_and_count_limits():
    g = two_city_sandbox()
    errs = g.submit_orders("p1", [{"type": "recruit", "city": [3, 3], "unit": "infantry", "count": 0},
                                  {"type": "recruit", "city": [3, 3], "unit": "infantry", "count": 51}])
    assert len(errs) == 2
    ev = run_turn(g, {"p1": [{"type": "recruit", "city": [3, 3], "unit": "infantry", "count": 50}]})
    assert "afford" in failed(ev, "p1")[0]["reason"]
    assert g.idx(3, 3) not in g.armies


def test_disband():
    g = two_city_sandbox()
    g.place_units(3, 3, "p1", {"infantry": 3, "archer": 1})
    run_turn(g, {"p1": [{"type": "disband", "at": [3, 3], "units": {"infantry": 2}}]})
    assert g.armies[g.idx(3, 3)]["p1"] == {"infantry": 1, "archer": 1}
    run_turn(g, {"p1": [{"type": "disband", "at": [3, 3]}]})
    assert g.idx(3, 3) not in g.armies


def test_action_order_is_submission_order():
    """Paying for the first order can make a later one unaffordable."""
    g = two_city_sandbox()
    p = g.player("p1")
    p.resources.update(wood=30, gold=100, stone=0)
    g.set_owner(5, 3, "p1")
    g.set_owner(3, 5, "p1")
    ev = run_turn(g, {"p1": [
        {"type": "build", "at": [5, 3], "building": "farm"},
        {"type": "build", "at": [3, 5], "building": "farm"},
    ]})
    assert g.improvement[g.idx(5, 3)] == "farm" and g.improvement[g.idx(3, 5)] is None
    assert failed(ev, "p1")[0]["index"] == 1
