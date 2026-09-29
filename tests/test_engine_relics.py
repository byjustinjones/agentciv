"""Relic control by occupation, guarded relic streaks, capital garrisons,
market reversion and the fairness properties of the relic/start layout."""
import pytest

from agentciv.engine import constants as C
from agentciv.engine.mapgen import (_gap_starts, generate_map, land_shares,
                                    relic_rings, start_layout)
from agentciv.engine.rules import relic_count, relics_needed
from agentciv.engine.testing import events_of, new_game, run_turn, sandbox


def world(n=4):
    g = sandbox(n)
    spots = [(2, 2), (13, 2), (2, 13), (13, 13)]
    for k in range(n):
        x, y = spots[k]
        g.add_city(x, y, f"p{k + 1}", capital=True)
    return g


def relic_xy(g, k=0):
    r = g.relics[k]
    return r % g.width, r // g.width


# ---------------------------------------------------------------- claiming
def test_relic_cannot_be_claimed():
    g = world()
    x, y = relic_xy(g)
    g.set_owner(x - 1, y, "p1")                 # adjacent territory
    errs = g.submit_orders("p1", [{"type": "claim", "at": [x, y]}])
    assert errs and "relic" in errs[0]["error"]
    # the engine refuses it too (e.g. a stale order list)
    g.submit_orders("p1", [])
    g._orders["p1"] = [{"type": "claim", "at": g.idx(x, y), "index": 0}]
    ev = g.step()
    assert g.owner[g.idx(x, y)] is None
    assert any(e["type"] == "order_failed" and "relic" in e["reason"] for e in ev)


def test_new_city_does_not_take_adjacent_relic():
    g = world()
    x, y = relic_xy(g)
    g.add_city(x + 1, y, "p2")
    assert g.owner[g.idx(x, y)] is None
    assert g.owner[g.idx(x + 1, y + 1)] == "p2"


# ---------------------------------------------------------------- occupation
def test_units_alone_on_a_relic_take_it():
    g = world()
    x, y = relic_xy(g)
    g.place_units(x - 1, y, "p1", {"infantry": 1})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [x - 1, y], "to": [x, y]}]})
    assert g.owner[g.idx(x, y)] == "p1"
    cap = events_of(ev, "tile_captured")
    assert cap and cap[0]["relic"] is True and cap[0]["from"] is None and cap[0]["to"] == "p1"
    view = g.spectator_view()
    rel = next(r for r in view["map"]["relics"] if (r["x"], r["y"]) == (x, y))
    assert rel == {"x": x, "y": y, "owner": "p1", "guarded": True}
    assert view["players"][0]["relics_held"] == 1 and view["players"][0]["relics_guarded"] == 1


def test_units_that_leave_keep_ownership_but_not_the_guard():
    g = world()
    x, y = relic_xy(g)
    g.place_units(x, y, "p1", {"infantry": 1})
    run_turn(g)
    assert g.owner[g.idx(x, y)] == "p1" and g.relic_guarded(g.idx(x, y))
    run_turn(g, {"p1": [{"type": "move", "from": [x, y], "to": [x - 1, y]}]})
    assert g.owner[g.idx(x, y)] == "p1"
    assert not g.relic_guarded(g.idx(x, y))
    st = g.stats()["p1"]
    assert st["relics_held"] == 1 and st["relics_guarded"] == 0
    # an owned relic still yields influence
    assert st["income"]["influence"] >= C.RELIC_INFLUENCE


def test_hostile_occupation_takes_a_relic_and_contested_relic_stays():
    g = world()
    x, y = relic_xy(g)
    g.set_owner(x, y, "p1")
    g.place_units(x - 1, y, "p2", {"infantry": 2})
    run_turn(g, {"p2": [{"type": "move", "from": [x - 1, y], "to": [x, y]}]})
    assert g.owner[g.idx(x, y)] == "p2"
    # two treaty partners standing on an unowned relic: the larger force
    # takes it (ties: lower seat); the partner can't take it back
    x2, y2 = relic_xy(g, 1)
    g.treaties[("p3", "p4")] = 99
    g.place_units(x2, y2, "p3", {"infantry": 1})
    g.place_units(x2, y2, "p4", {"infantry": 2})
    run_turn(g)
    assert g.owner[g.idx(x2, y2)] == "p4"
    g.place_units(x2, y2, "p3", {"infantry": 5})
    run_turn(g)
    assert g.owner[g.idx(x2, y2)] == "p4"


def test_captured_city_does_not_hand_over_relic():
    g = world()
    x, y = relic_xy(g)
    g.add_city(x + 1, y, "p2")
    g.set_owner(x, y, "p2")
    g.place_units(x + 2, y, "p1", {"infantry": 10})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [x + 2, y], "to": [x + 1, y]}]})
    assert events_of(ev, "city_captured")
    assert g.owner[g.idx(x, y)] == "p2"          # relics change hands by occupation only


# ---------------------------------------------------------------- streak
def test_relic_streak_needs_units_on_the_relics():
    g = world()
    need = relics_needed(len(g.relics))
    for k in range(need):
        g._set_owner(g.relics[k], "p4")
    g._invalidate()
    run_turn(g)
    assert g.player("p4").relic_streak == 0          # owned but unguarded
    for k in range(need):
        x, y = relic_xy(g, k)
        g.place_units(x, y, "p4", {"infantry": 1})
    run_turn(g)
    assert g.player("p4").relic_streak == 1
    vp = g.spectator_view()["players"][3]["victory_progress"]["relics"]
    assert vp == pytest.approx(1 / C.RELIC_VICTORY_TURNS, abs=1e-3)
    # one guard walks away: the streak resets
    x, y = relic_xy(g, 0)
    run_turn(g, {"p4": [{"type": "move", "from": [x, y], "to": [x - 1, y]}]})
    assert g.player("p4").relic_streak == 0


def test_relics_needed_formula():
    assert [relics_needed(r) for r in (1, 2, 3)] == [1, 2, 2]          # majority when few
    assert [relics_needed(r) for r in (4, 5, 6, 8, 12)] == [2, 3, 3, 4, 6]
    assert [relic_count(n) for n in (2, 5, 6, 8)] == [2, 5, 6, 8]


# ---------------------------------------------------------------- garrison
def test_starting_army_cannot_snipe_an_undefended_capital():
    g = world(2)
    start_power = sum(C.UNITS[u]["strength"] * k for u, k in C.START_UNITS.items())
    assert C.GARRISON_CAPITAL > start_power
    g.place_units(12, 2, "p1", dict(C.START_UNITS))
    ev = run_turn(g, {"p1": [{"type": "move", "from": [12, 2], "to": [13, 2]}]})
    assert not events_of(ev, "city_captured")
    assert g.cities[g.idx(13, 2)].owner == "p2"


def test_bigger_army_takes_the_capital():
    g = world(2)
    k = C.GARRISON_CAPITAL // C.UNITS["infantry"]["strength"] + 2
    g.place_units(12, 2, "p1", {"infantry": k})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [12, 2], "to": [13, 2]}]})
    assert events_of(ev, "city_captured")


# ---------------------------------------------------------------- market
def test_market_reverts_toward_initial_reserves():
    g = new_game(3)
    pool, init = g.pools["food"], g.pool_init["food"]
    pool[0] = init[0] * 2
    pool[1] = init[1] / 2
    before = abs(pool[0] - init[0])
    g.step()
    after = abs(g.pools["food"][0] - init[0])
    assert after == pytest.approx(before * (1 - C.MARKET_REVERSION), rel=1e-6)


# ---------------------------------------------------------------- layout fairness
@pytest.mark.parametrize("n", [5, 6, 8])
def test_relics_one_per_gap_and_equidistant_from_flanking_capitals(n):
    for _unf, pts in relic_rings(n):
        assert len(pts) == relic_count(n)
        for p, (a, b) in zip(pts, _gap_starts(n)):
            da = abs(p[0] - a[0]) + abs(p[1] - a[1])
            db = abs(p[0] - b[0]) + abs(p[1] - b[1])
            assert abs(da - db) <= 1
        # never adjacent to each other
        assert all(max(abs(p[0] - q[0]), abs(p[1] - q[1])) >= C.RELIC_MIN_SPACING
                   for i, p in enumerate(pts) for q in pts[i + 1:])


def test_six_player_relic_distances_equal_for_the_needed_ranks():
    starts, _rots, _stamp = start_layout(6)
    need = relics_needed(relic_count(6))
    for _unf, pts in relic_rings(6):
        ranks = {tuple(sorted(abs(p[0] - s[0]) + abs(p[1] - s[1]) for p in pts)[:need]) for s in starts}
        assert len(ranks) == 1


@pytest.mark.parametrize("n", [5, 6, 8])
def test_land_shares_are_equal(n):
    for seed in (1, 2):
        m = generate_map(n, seed)
        share, region = land_shares(m.terrain, m.width, m.height, m.starts)
        value = [len(r) + C.MAPGEN_CONTESTED_LAND_VALUE * (s - len(r)) for s, r in zip(share, region)]
        assert max(value) - min(value) <= C.MAPGEN_LAND_TOLERANCE + 1.0 + 1e-9
        for t in ("h", "f", "g"):
            counts = [sum(1 for _d, i in r if m.terrain[i] == t) for r in region]
            assert max(counts) - min(counts) <= 1


def test_map_orientation_and_slots_vary_by_seed():
    maps = [generate_map(6, s) for s in range(12)]
    assert len({m.fairness["orientation"] for m in maps}) > 1
    for m in maps:
        assert sorted(m.slots) == list(range(6))
    g = new_game(6, seed=3)
    assert sorted(g.start_slots.values()) == list(range(6))
