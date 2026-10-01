"""Yields, seasons, deposits, storage caps, upkeep and starvation, elimination."""
from agentciv.engine import constants as C
from agentciv.engine.testing import events_of, run_turn, sandbox, set_terrain


def one_city(n=2):
    g = sandbox(n)
    g.add_city(3, 3, "p1", capital=True)
    for k in range(1, n):
        g.add_city(12, 3 + 4 * k, f"p{k + 1}", capital=True)
    return g


def test_city_and_tile_yields():
    g = one_city()
    inc = g.stats()["p1"]["income"]
    # capital + 8 plains (sandbox terrain is plains)
    assert inc == {"food": 2 + 8 * 2, "wood": 1, "stone": 1, "gold": 2, "influence": 2}


def test_seasons_multiply_food():
    g = one_city()
    p = g.player("p1")
    raw_food = 18
    foods = []
    for t in range(24):
        f0 = p.resources["food"]
        p.resources["food"] = 100
        g.step()
        foods.append(p.resources["food"] - 100)
    # spring, summer (x1.5), autumn, winter (x0.5), no units -> no upkeep
    assert foods[0] == raw_food
    assert foods[6] == int(raw_food * 1.5)
    assert foods[12] == raw_food
    assert foods[18] == int(raw_food * 0.5)


def test_relic_and_temple_influence():
    g = one_city()
    r = g.relics[0]
    g.set_owner(r % g.width, r // g.width, "p1")
    set_terrain(g, 4, 2, "h")
    g.improvement[g.idx(4, 2)] = "temple"
    g._invalidate()
    inc = g.stats()["p1"]["income"]
    temple = C.IMPROVEMENTS["temple"]["bonus"]["influence"]
    assert inc["influence"] == 2 + C.RELIC_INFLUENCE_UNGUARDED + temple          # nobody on the relic
    assert inc["stone"] == 1 + 2          # temple keeps the hills base yield
    g.place_units(r % g.width, r // g.width, "p1", {"infantry": 1})
    g._invalidate()
    assert g.stats()["p1"]["income"]["influence"] == 2 + C.RELIC_INFLUENCE + temple     # guarded


def test_deposit_depletion():
    g = one_city()
    set_terrain(g, 4, 3, "g")
    i = g.idx(4, 3)
    g.improvement[i] = "mine"
    g.deposits[i] = 4
    g._invalidate()
    p = g.player("p1")
    gold0 = p.resources["gold"]
    g.step()            # extracts 3 (1 base + 2 mine)
    assert g.deposits[i] == 1 and p.resources["gold"] == gold0 + 2 + 3
    g.step()            # only 1 left
    assert g.deposits[i] == 0 and p.resources["gold"] == gold0 + 2 + 3 + 2 + 1
    g.step()
    assert p.resources["gold"] == gold0 + 2 + 3 + 2 + 1 + 2


def test_storage_caps():
    g = one_city()
    p = g.player("p1")
    p.resources.update(food=299, wood=500, stone=300, gold=1000)
    g.step()
    assert p.resources["food"] == 300 and p.resources["wood"] == 300 and p.resources["stone"] == 300
    assert p.resources["gold"] == 1002
    g.cities[g.idx(3, 3)].warehouse = 1
    p.resources["wood"] = 490
    g.step()
    assert p.resources["wood"] == 491


def test_upkeep():
    g = one_city()
    g.place_units(3, 3, "p1", {"infantry": 2, "cavalry": 1})
    p = g.player("p1")
    p.resources["food"] = 50
    g.step()
    assert p.resources["food"] == 50 + 18 - (2 + 2)
    assert g.stats()["p1"]["upkeep"] == 4


def test_starvation_highest_upkeep_first_largest_stack():
    g = one_city()
    set_terrain(g, 3, 3, "m")                   # irrelevant for the city tile yield
    g.place_units(3, 3, "p1", {"infantry": 10, "cavalry": 1})
    g.place_units(5, 5, "p1", {"cavalry": 3, "siege": 1})
    p = g.player("p1")
    p.resources["food"] = 0
    # upkeep 10 + 2*4 + 2 = 20; income 18 -> deficit 2 -> lose 1 unit: siege (upkeep 2, first in order)
    ev = run_turn(g)
    st = events_of(ev, "starvation")[0]
    assert st["deficit"] == 2 and st["lost"] == {"siege": 1}
    assert p.resources["food"] == 0
    # now bigger deficit: set income to 0 by removing tiles
    for j in g.radius(g.idx(3, 3), 1):
        if j != g.idx(3, 3):
            g._set_owner(j, None)
    g._invalidate()
    ev = run_turn(g)
    # upkeep 10 + 8 = 18, income 2 food -> deficit 16 -> lose 8: 4 cavalry first (largest stack first), then 4 infantry
    st = events_of(ev, "starvation")[0]
    assert st["lost"] == {"cavalry": 4, "infantry": 4}
    assert g.armies[g.idx(3, 3)]["p1"] == {"infantry": 6}
    assert g.idx(5, 5) not in g.armies


def test_elimination_clears_units_and_tiles():
    g = one_city(3)
    g.place_units(8, 8, "p2", {"infantry": 2})
    p2_city = [c for c in g.cities.values() if c.owner == "p2"][0]
    g.cities[p2_city.idx].owner = "p3"
    g._set_owner(p2_city.idx, "p3")
    g.set_owner(10, 10, "p2")
    ev = run_turn(g)
    p2 = g.player("p2")
    assert not p2.alive and p2.eliminated_turn == 0
    assert all(o != "p2" for o in g.owner)
    assert all("p2" not in per for per in g.armies.values())
    assert events_of(ev, "eliminated")
    assert g.submit_orders("p2", [])[0]["index"] == -1
    assert "p2" not in g.alive_players()
