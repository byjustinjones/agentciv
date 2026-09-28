"""Victory conditions, score, placements, victory_progress."""
import pytest

from agentciv.engine import constants as C
from agentciv.engine.rules import building_cost, conquest_capitals, relics_needed
from agentciv.engine.testing import events_of, new_game, run_turn, sandbox


def world(n=4, max_turns=150):
    g = sandbox(n, max_turns=max_turns)
    spots = [(2, 2), (13, 2), (2, 13), (13, 13), (8, 8), (8, 2)]
    for k in range(n):
        x, y = spots[k]
        g.add_city(x, y, f"p{k + 1}", capital=True)
    return g


def test_thresholds():
    assert [conquest_capitals(n) for n in (2, 3, 4, 5, 6, 8, 12)] == [2, 3, 2, 3, 3, 4, 6]
    g = new_game(6)
    thr = g.spectator_view()["victory"]["thresholds"]
    assert thr == {"conquest_capitals": 3, "wonder_stage": 5, "influence": 600, "relics_needed": 3,
                   "relics_total": 5, "relic_turns": 10, "economic_gold": 2000, "max_turns": 150}


def test_economic_victory():
    g = world()
    g.player("p3").resources["gold"] = C.ECONOMIC_VICTORY_GOLD - 2   # +2 city gold income
    ev = run_turn(g)
    assert g.finished and g.result["winner"] == "p3" and g.result["condition"] == "economic"
    assert g.result["turn"] == 0 and g.result["placements"][0] == "p3"
    assert events_of(ev, "victory")
    assert g.step() == []                                  # finished games do not advance
    assert g.submit_orders("p1", [])[0]["index"] == -1


def test_influence_victory():
    g = world()
    g.player("p2").resources["influence"] = C.INFLUENCE_VICTORY
    run_turn(g)
    assert g.result["winner"] == "p2" and g.result["condition"] == "influence"


def test_wonder_victory():
    g = world()
    p = g.player("p1")
    city = g.cities[g.idx(2, 2)]
    city.wonder_stage = C.WONDER_VICTORY_STAGE - 1
    p.wonder_city = city.idx
    cost = building_cost("wonder", C.WONDER_VICTORY_STAGE)
    p.resources.update({r: v for r, v in cost.items()})
    run_turn(g, {"p1": [{"type": "build", "at": [2, 2], "building": "wonder"}]})
    assert g.result["winner"] == "p1" and g.result["condition"] == "wonder"


def test_relic_victory_needs_streak():
    g = world()
    need = relics_needed(len(g.relics))
    for r in g.relics[:need]:
        g._set_owner(r, "p4")
    g._invalidate()
    for t in range(C.RELIC_VICTORY_TURNS - 1):
        run_turn(g)
        assert not g.finished
        assert g.player("p4").relic_streak == t + 1
        prog = g.spectator_view()["players"][3]["victory_progress"]["relics"]
        assert prog == pytest.approx((t + 1) / C.RELIC_VICTORY_TURNS, abs=1e-3)
    run_turn(g)
    assert g.result["winner"] == "p4" and g.result["condition"] == "relics"


def test_relic_streak_resets():
    g = world()
    need = relics_needed(len(g.relics))
    for r in g.relics[:need]:
        g._set_owner(r, "p4")
    run_turn(g)
    run_turn(g)
    assert g.player("p4").relic_streak == 2
    g._set_owner(g.relics[0], None)
    run_turn(g)
    assert g.player("p4").relic_streak == 0
    assert g.spectator_view()["players"][3]["victory_progress"]["relics"] == 0.0


def test_conquest_by_capitals():
    g = world(4)                       # need 2 capitals
    g.player("p2").resources["gold"] = 0
    # p1 takes p2's capital (garrison 20) with 3 infantry
    g.place_units(12, 2, "p1", {"infantry": 3})
    run_turn(g, {"p1": [{"type": "move", "from": [12, 2], "to": [13, 2]}]})
    assert g.result["winner"] == "p1" and g.result["condition"] == "conquest"
    assert g.result["placements"][-1] == "p2"            # eliminated this turn


def test_conquest_last_player_standing():
    g = world(2)
    g.cities.pop(g.idx(13, 2))
    run_turn(g)
    assert g.result["winner"] == "p1" and g.result["condition"] == "conquest"
    assert g.result["placements"] == ["p1", "p2"]


def test_score_victory_at_max_turns():
    g = world(3, max_turns=3)
    g.set_owner(5, 5, "p2")
    g.set_owner(5, 6, "p2")
    for _ in range(2):
        run_turn(g)
        assert not g.finished
    run_turn(g)
    assert g.finished and g.result["condition"] == "score" and g.result["turn"] == 2
    assert g.result["winner"] == "p2"
    scores = g.result["scores"]
    assert g.result["placements"] == sorted(scores, key=lambda q: (-scores[q], q))


def test_simultaneous_conditions_highest_score_wins():
    g = world()
    g.player("p1").resources["gold"] = C.ECONOMIC_VICTORY_GOLD
    g.player("p2").resources["influence"] = C.INFLUENCE_VICTORY
    g.set_owner(6, 6, "p2")
    g.set_owner(6, 7, "p2")
    g.set_owner(6, 8, "p2")
    g.set_owner(6, 9, "p2")
    st = g.stats()
    assert st["p2"]["score"] > st["p1"]["score"]
    run_turn(g)
    assert g.result["winner"] == "p2" and g.result["condition"] == "influence"


def test_placements_eliminated_latest_first():
    g = world(4, max_turns=4)
    g.cities.pop(g.idx(2, 13))           # p3 eliminated on turn 0
    run_turn(g)
    g.cities.pop(g.idx(13, 13))          # p4 eliminated on turn 1
    run_turn(g)
    run_turn(g)
    run_turn(g)
    assert g.finished
    pl = g.result["placements"]
    assert pl[2:] == ["p4", "p3"]
    assert g.player("p3").eliminated_turn == 0 and g.player("p4").eliminated_turn == 1


def test_score_formula():
    g = world(2)
    p = g.player("p1")
    p.resources.update(influence=23, gold=45)
    g.place_units(2, 2, "p1", {"infantry": 3, "cavalry": 1})
    g._set_owner(g.relics[0], "p1")
    g._invalidate()
    s = g.stats()["p1"]
    w, d = C.SCORE_WEIGHTS, C.SCORE_DIVISORS
    expected = (w["tiles"] * p.tiles + w["cities"] * 1 + w["capitals_held"] * 1 + 0
                + 23 // d["influence"] + 45 // d["gold"] + w["relics_held"] * 1 + 42 // d["military_power"])
    assert s["score"] == expected and s["military_power"] == 42


def test_victory_progress_values():
    g = world(4)
    g.player("p1").resources.update(gold=1000, influence=150)
    g._invalidate()
    vp = g.spectator_view()["players"][0]["victory_progress"]
    assert vp == {"conquest": 0.5, "wonder": 0.0, "influence": 0.25, "relics": 0.0, "economic": 0.5, "score": 0.0}
