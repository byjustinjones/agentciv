"""Combat math (pure functions) and battles / captures inside the engine."""
import math

import pytest

from agentciv.engine import combat
from agentciv.engine import constants as C
from agentciv.engine.combat import Side
from agentciv.engine.testing import events_of, run_turn, sandbox, set_terrain


def always_hostile(a, b):
    return a != b


# ---------------------------------------------------------------- pure math
def test_counter_multiplier():
    assert combat.counter_multiplier("infantry", {"cavalry": 2, "archer": 2}) == pytest.approx(1.25)
    assert combat.counter_multiplier("infantry", {"infantry": 5}) == 1.0
    assert combat.counter_multiplier("siege", {"infantry": 5}) == 1.0
    assert combat.counter_multiplier("archer", {}) == 1.0


def test_power_counters_and_terrain():
    inf = Side("a", {"infantry": 10})
    cav = Side("b", {"cavalry": 5})
    assert combat.power(inf, cav) == pytest.approx(150)
    inf.terrain_bonus = True
    assert combat.power(inf, cav) == pytest.approx(150 * C.TERRAIN_DEFENSE_BONUS)
    assert combat.power(cav, inf) == pytest.approx(60)   # cavalry has no edge on infantry


def test_walls_siege_and_garrison():
    owner = Side("q", {}, defender=True, city_owner=True, garrison=20)
    siege = Side("p", {"siege": 3})
    # walls 2, 3 siege cancel one level: x(1 + 0.5 * 1)
    assert combat.power(owner, siege, walls=2) == pytest.approx(20 * 1.5)
    # siege count x4 attacking a city
    assert combat.power(siege, owner, walls=2) == pytest.approx(3 * 4 * 4)
    # siege attacking a non-city side has normal strength
    assert combat.power(siege, Side("r", {"infantry": 1})) == pytest.approx(12)
    assert combat.wall_multiplier(3, 0) == pytest.approx(2.5)
    assert combat.wall_multiplier(1, 9) == pytest.approx(1.0)


def test_archers_defending_city():
    owner = Side("q", {"archer": 5}, defender=True, city_owner=True, garrison=10)
    att = Side("p", {"infantry": 4})
    assert combat.power(owner, att) == pytest.approx(5 * 8 * C.ARCHER_CITY_DEFENSE * C.COUNTER_MULTIPLIER + 10)


def test_lanchester_losses():
    assert combat.lanchester_losses({"infantry": 10}, 60, 100) == {"infantry": 2}
    assert combat.lanchester_losses({"infantry": 10}, 0, 100) == {}
    assert combat.lanchester_losses({"infantry": 10, "archer": 3}, 100, 100) == {"infantry": 10, "archer": 3}


def test_duel_winner_and_losses():
    a = Side("a", {"infantry": 10})
    b = Side("b", {"infantry": 6})
    rec = combat.duel(a, b)
    assert rec["winner"] == "a"
    assert a.units == {"infantry": 8} and b.units == {} and b.defeated
    assert rec["losses"] == {"a": {"infantry": 2}, "b": {"infantry": 6}}


def test_tie_rules():
    a = Side("a", {"infantry": 2}, defender=True)
    b = Side("b", {"infantry": 2})
    assert combat.duel(a, b)["winner"] == "a"
    assert a.units == {} and not a.defeated and b.defeated
    c = Side("c", {"infantry": 2})
    d = Side("d", {"infantry": 2})
    rec = combat.duel(c, d)
    assert rec["winner"] is None and c.defeated and d.defeated


def test_multi_side_weakest_first():
    a = Side("a", {"infantry": 5}, order=(1, 0))
    b = Side("b", {"infantry": 8}, order=(1, 1))
    c = Side("c", {"infantry": 10}, order=(1, 2))
    recs = combat.resolve([a, b, c], always_hostile)
    assert [r["sides"] for r in recs] == [["a", "b"], ["b", "c"]]
    assert b.units == {} and a.units == {}
    # b: 8 - round(8*(1-sqrt(1-(50/80)^2))) = 6 ; c: 10 - round(10*0.2) = 8
    assert c.units == {"infantry": 8}


def test_multi_side_with_alliance():
    a = Side("a", {"infantry": 5}, order=(1, 0))
    b = Side("b", {"infantry": 8}, order=(1, 1))
    c = Side("c", {"infantry": 10}, order=(1, 2))
    allies = {("a", "b"), ("b", "a")}
    recs = combat.resolve([a, b, c], lambda x, y: x != y and (x, y) not in allies)
    assert [r["sides"] for r in recs] == [["a", "c"], ["b", "c"]]
    # c loses 1 to a (ratio .5), then 90 vs 80 -> c loses round(9*(1-sqrt(1-(80/90)^2))) = 5
    lost2 = int(math.floor(9 * (1 - math.sqrt(1 - (80 / 90) ** 2)) + 0.5))
    assert c.units == {"infantry": 9 - lost2}


# ---------------------------------------------------------------- engine battles
def world(n=3):
    g = sandbox(n)
    g.add_city(2, 2, "p1", capital=True)
    g.add_city(12, 12, "p2", capital=True)
    if n >= 3:
        g.add_city(2, 12, "p3", capital=True)
    return g


def test_capture_undefended_tile():
    g = world()
    g.set_owner(8, 8, "p2")
    g.place_units(7, 8, "p1", {"infantry": 1})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [7, 8], "to": [8, 8]}]})
    assert g.owner[g.idx(8, 8)] == "p1"
    assert events_of(ev, "tile_captured")[0]["from"] == "p2"
    assert not events_of(ev, "battle")


def test_garrison_holds_on_tie_and_falls_to_superior_force():
    g = world()
    city = g.add_city(8, 8, "p2")          # non-capital: garrison 10
    g.place_units(7, 8, "p1", {"infantry": 1})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [7, 8], "to": [8, 8]}]})
    assert city.owner == "p2" and g.idx(8, 8) not in g.armies
    assert events_of(ev, "battle")[0]["winner"] == "p2"
    g.place_units(7, 8, "p1", {"infantry": 2})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [7, 8], "to": [8, 8]}]})
    assert city.owner == "p1"
    cap = events_of(ev, "city_captured")[0]
    assert cap["from"] == "p2" and cap["to"] == "p1" and cap["plunder"] == {}
    assert all(g.owner[j] == "p1" for j in g.radius(g.idx(8, 8), 1))
    assert g.armies[g.idx(8, 8)]["p1"] == {"infantry": 2}


def test_capital_capture_plunders_and_drops_walls():
    g = world()
    cap = g.cities[g.idx(12, 12)]
    cap.walls = 1
    victim = g.player("p2")
    victim.resources.update(food=100, wood=81, stone=40, gold=60)
    # garrison 20 * walls(1.5) = 30 -> 4 infantry (40) win
    g.place_units(11, 12, "p1", {"infantry": 4})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [11, 12], "to": [12, 12]}]})
    assert cap.owner == "p1" and cap.walls == 0 and cap.capital
    ce = events_of(ev, "city_captured")[0]
    assert ce["plunder"] == {"food": 50, "wood": 40, "stone": 20, "gold": 30}
    assert g.stats()["p1"]["capitals_held"] == 2
    assert not victim.alive                     # no cities left
    assert events_of(ev, "eliminated")[0]["player"] == "p2"


def test_walls_vs_siege():
    g = world()
    city = g.add_city(8, 8, "p2")
    city.walls = 3
    # garrison 10 * 2.5 = 25 beats 2 infantry (20)
    g.place_units(7, 8, "p1", {"infantry": 2})
    run_turn(g, {"p1": [{"type": "move", "from": [7, 8], "to": [8, 8]}]})
    assert city.owner == "p2"
    # 3 siege: 48 attack; walls reduced to 1 + 0.5*(3-1) = 2 -> 20 defence
    g.place_units(7, 8, "p1", {"siege": 3})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [7, 8], "to": [8, 8]}]})
    b = events_of(ev, "battle")[0]
    assert b["powers"] == {"p1": 48.0, "p2": 20.0}
    assert city.owner == "p1" and city.walls == 2


def test_defenders_terrain_bonus_and_garrison_stack():
    g = world()
    set_terrain(g, 8, 8, "h")
    g.place_units(8, 8, "p2", {"infantry": 2})
    g.place_units(7, 8, "p1", {"infantry": 2})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [7, 8], "to": [8, 8]}]})
    b = events_of(ev, "battle")[0]
    assert b["powers"] == {"p1": 20.0, "p2": 25.0} and b["winner"] == "p2"
    assert g.armies[g.idx(8, 8)] == {"p2": {"infantry": 1}}


def test_city_defenders_fight_with_garrison():
    g = world()
    city = g.add_city(8, 8, "p2")
    g.place_units(8, 8, "p2", {"archer": 1})
    g.place_units(7, 8, "p1", {"infantry": 3})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [7, 8], "to": [8, 8]}]})
    b = events_of(ev, "battle")[0]
    # archer: 8 * 1.5 (city) * 1.5 (counter vs infantry) + garrison 10 = 28 vs 30
    assert b["powers"]["p2"] == pytest.approx(28.0)
    assert b["winner"] == "p1" and city.owner == "p1"


def test_border_clash():
    g = world()
    g.place_units(5, 5, "p1", {"infantry": 3})
    g.place_units(6, 5, "p2", {"infantry": 2})
    ev = run_turn(g, {
        "p1": [{"type": "move", "from": [5, 5], "to": [6, 5]}],
        "p2": [{"type": "move", "from": [6, 5], "to": [5, 5]}],
    })
    b = events_of(ev, "battle")
    assert len(b) == 1 and b[0]["clash"] is True and b[0]["winner"] == "p1"
    assert g.armies == {**{k: v for k, v in g.armies.items() if k != g.idx(6, 5)},
                        g.idx(6, 5): {"p1": {"infantry": 2}}}
    assert g.idx(5, 5) not in g.armies


def test_three_way_battle_on_one_tile():
    g = world()
    g.place_units(7, 8, "p1", {"infantry": 5})
    g.place_units(9, 8, "p2", {"infantry": 8})
    g.place_units(8, 9, "p3", {"infantry": 10})
    ev = run_turn(g, {
        "p1": [{"type": "move", "from": [7, 8], "to": [8, 8]}],
        "p2": [{"type": "move", "from": [9, 8], "to": [8, 8]}],
        "p3": [{"type": "move", "from": [8, 9], "to": [8, 8]}],
    })
    b = events_of(ev, "battle")
    assert [x["sides"] for x in b] == [["p1", "p2"], ["p2", "p3"]]
    assert g.armies[g.idx(8, 8)] == {"p3": {"infantry": 8}}


def test_capture_skips_tiles_with_foreign_units():
    g = world()
    city = g.add_city(8, 8, "p2")
    g.place_units(9, 8, "p2", {"infantry": 1})       # p2 unit next to the city
    g.place_units(7, 8, "p1", {"infantry": 3})
    run_turn(g, {"p1": [{"type": "move", "from": [7, 8], "to": [8, 8]}]})
    assert city.owner == "p1"
    assert g.owner[g.idx(9, 8)] == "p2"
    assert g.owner[g.idx(8, 7)] == "p1"


def test_wonder_destroyed_on_capture():
    g = world()
    city = g.add_city(8, 8, "p2")
    city.wonder_stage = 3
    g.player("p2").wonder_city = city.idx
    g.place_units(7, 8, "p1", {"infantry": 3})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [7, 8], "to": [8, 8]}]})
    assert city.wonder_stage == 0 and g.player("p2").wonder_city is None
    assert events_of(ev, "city_captured")[0]["wonder_destroyed"] == 3


def test_recruits_lost_when_city_captured():
    g = world()
    g.add_city(8, 8, "p2")
    g.place_units(7, 8, "p1", {"infantry": 3})
    g.submit_orders("p2", [{"type": "recruit", "city": [8, 8], "unit": "infantry", "count": 1}])
    ev = run_turn(g, {"p1": [{"type": "move", "from": [7, 8], "to": [8, 8]}]})
    assert g.armies[g.idx(8, 8)] == {"p1": {"infantry": 3}}
    assert any("recruits lost" in e["reason"] for e in events_of(ev, "order_failed"))


def test_units_on_hostile_tile_capture_next_turn():
    g = world()
    g.set_owner(8, 8, "p2")
    g.place_units(8, 8, "p1", {"infantry": 1})
    run_turn(g)
    assert g.owner[g.idx(8, 8)] == "p1"
