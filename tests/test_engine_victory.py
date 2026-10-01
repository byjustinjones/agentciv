"""Victory conditions, score, placements, victory_progress."""
import pytest

from agentciv.engine import constants as C
from agentciv.engine.rules import rules_json
from agentciv.engine.rules import bank_target, building_cost, conquest_capitals, legacy_target, streak_deposit
from agentciv.engine.testing import events_of, new_game, run_turn, sandbox


def world(n=4, max_turns=150):
    g = sandbox(n, max_turns=max_turns)
    spots = [(2, 2), (13, 2), (2, 13), (13, 13), (8, 8), (8, 2)]
    for k in range(n):
        x, y = spots[k]
        g.add_city(x, y, f"p{k + 1}", capital=True)
    return g


def deposit(g, pid, gold=None):
    """Orders banking enough gold for the economic streak this turn (§11)."""
    need = streak_deposit(g.bank_limit(pid)) if gold is None else gold
    g.player(pid).resources["gold"] += need
    return {pid: [{"type": "bank", "gold": need}]}


def test_thresholds():
    # a majority of the original capitals (all of them in games of 2-3)
    assert [conquest_capitals(n) for n in (2, 3, 4, 5, 6, 8, 12)] == [2, 3, 3, 3, 4, 5, 7]
    g = new_game(6)
    thr = g.spectator_view()["victory"]["thresholds"]
    assert thr == {"conquest_capitals": 4, "wonder_stage": C.WONDER_VICTORY_STAGE,
                   "legacy": C.LEGACY_VICTORY, "relics_total": 6, "bank": C.BANK_VICTORY,
                   "streak_turns": C.VICTORY_STREAK_TURNS, "max_turns": 150}
    assert C.VICTORY_CONDITIONS == ("conquest", "wonder", "influence", "economic")
    victory = rules_json()["victory"]
    assert not any(k.startswith("relic") for k in victory)
    assert victory["bank"] == {"target": C.BANK_VICTORY, "base": C.BANK_BASE, "per_market_hall": C.BANK_PER_MARKET_HALL,
                               "streak_deposit_divisor": C.STREAK_DEPOSIT_DIVISOR, "seize_fraction": C.BANK_SEIZE_FRACTION}


@pytest.mark.parametrize("max_turns,bank,legacy", [(150, 3600, 2700), (90, 2160, 1620), (60, 1800, 1350),
                                                   (200, 3600, 2700), (20, 1800, 1350)])
def test_targets_scale_with_game_length(max_turns, bank, legacy):
    assert (bank_target(max_turns), legacy_target(max_turns)) == (bank, legacy)
    thr = new_game(3, max_turns=max_turns).spectator_view()["victory"]["thresholds"]
    assert (thr["bank"], thr["legacy"]) == (bank, legacy)


def test_economic_victory():
    g = world()
    p = g.player("p3")
    p.bank = C.BANK_VICTORY
    p.economic_streak = C.VICTORY_STREAK_TURNS - 1
    ev = run_turn(g, deposit(g, "p3"))
    assert g.finished and g.result["winner"] == "p3" and g.result["condition"] == "economic"
    assert g.result["turn"] == 0 and g.result["placements"][0] == "p3"
    assert events_of(ev, "victory")
    assert g.step() == []                                  # finished games do not advance
    assert g.submit_orders("p1", [])[0]["index"] == -1


def test_gold_on_hand_is_not_the_economic_condition():
    g = world()
    g.player("p3").resources["gold"] = 10 * C.BANK_VICTORY
    run_turn(g)
    assert not g.finished and g.player("p3").economic_streak == 0


def test_influence_victory():
    g = world()
    p = g.player("p2")
    p.legacy = C.LEGACY_VICTORY
    p.influence_streak = C.VICTORY_STREAK_TURNS - 1
    run_turn(g)
    assert g.result["winner"] == "p2" and g.result["condition"] == "influence"


@pytest.mark.parametrize("condition", ["economic", "influence"])
def test_streak_counts_to_the_win(condition):
    g = world()
    p = g.player("p1")
    if condition == "economic":
        p.bank = C.BANK_VICTORY
    else:
        p.legacy = C.LEGACY_VICTORY
    orders = (lambda: deposit(g, "p1")) if condition == "economic" else (lambda: None)
    ev = run_turn(g, orders())
    assert [e for e in events_of(ev, "streak_started")] == [
        {"turn": 0, "type": "streak_started", "player": "p1", "condition": condition}]
    for t in range(2, C.VICTORY_STREAK_TURNS):
        ev = run_turn(g, orders())
        assert not g.finished and getattr(p, condition + "_streak") == t
        assert not events_of(ev, "streak_started")
        vp = g.spectator_view()["players"][0]["victory_progress"][condition]
        assert vp == round(C.LEDGER_PROGRESS_WEIGHT + (1 - C.LEDGER_PROGRESS_WEIGHT) * t / C.VICTORY_STREAK_TURNS, 3)
        assert vp < 1.0
    run_turn(g, orders())
    assert g.result["winner"] == "p1" and g.result["condition"] == condition
    assert g.spectator_view()["players"][0]["victory_progress"][condition] == 1.0


@pytest.mark.parametrize("condition", ["economic", "influence"])
def test_streak_needs_the_original_capital(condition):
    g = world()
    p = g.player("p1")
    g.add_city(6, 2, "p1")                    # a second city keeps p1 alive
    if condition == "economic":
        p.bank = C.BANK_VICTORY
    else:
        p.legacy = C.LEGACY_VICTORY
    orders = (lambda: deposit(g, "p1")) if condition == "economic" else (lambda: None)
    run_turn(g, orders())
    run_turn(g, orders())
    assert getattr(p, condition + "_streak") == 2
    cap = g.cities[g.idx(2, 2)]
    cap.owner = "p2"                          # capital handed over (no capture event; bank/legacy untouched)
    g._set_owner(cap.idx, "p2")
    g._invalidate()
    ev = run_turn(g, orders())
    assert getattr(p, condition + "_streak") == 0 and p.alive
    assert {"turn": 2, "type": "streak_ended", "player": "p1", "condition": condition} in ev
    run_turn(g, orders())
    assert getattr(p, condition + "_streak") == 0


def test_streak_resets_below_target():
    g = world()
    p = g.player("p1")
    p.bank = C.BANK_VICTORY
    run_turn(g, deposit(g, "p1"))
    assert p.economic_streak == 1
    p.bank = C.BANK_VICTORY - 100
    ev = run_turn(g, deposit(g, "p1"))
    assert p.bank < C.BANK_VICTORY and p.economic_streak == 0 and events_of(ev, "streak_ended")


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


def guard(g, pid, relics):
    """Own the relics and put one infantry on each (a guarded relic)."""
    for r in relics:
        g._set_owner(r, pid)
        g.place_units(r % g.width, r // g.width, pid, {"infantry": 1})
    g._invalidate()


def test_relics_are_not_a_victory_condition():
    g = world(max_turns=60)
    guard(g, "p4", g.relics)                 # every relic, guarded, for 30 turns
    for _ in range(30):
        run_turn(g)
        assert not g.finished
    row = g.spectator_view()["players"][3]
    assert row["relics_guarded"] == len(g.relics)
    assert "relics" not in row["victory_progress"] and "relic_streak" not in row


def test_conquest_by_capitals():
    g = world(4)                       # need 3 of the 4 original capitals
    g.player("p2").resources["gold"] = 0
    c3 = g.cities[g.idx(2, 13)]        # p1 already took p3's capital
    c3.owner = "p1"
    g._set_owner(c3.idx, "p1")
    g._invalidate()
    run_turn(g)
    assert not g.finished and not g.player("p3").alive
    # p1 takes p2's capital (garrison 40) with 5 infantry (50)
    g.place_units(12, 2, "p1", {"infantry": 5})
    run_turn(g, {"p1": [{"type": "move", "from": [12, 2], "to": [13, 2]}]})
    assert g.result["winner"] == "p1" and g.result["condition"] == "conquest"
    assert g.result["placements"][-2:] == ["p2", "p3"]   # latest-eliminated first


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
    g.player("p1").bank = C.BANK_VICTORY               # the bank counts for the score
    g.player("p1").economic_streak = C.VICTORY_STREAK_TURNS - 1
    g.player("p2").legacy = C.LEGACY_VICTORY
    g.player("p2").influence_streak = C.VICTORY_STREAK_TURNS - 1
    g.player("p2").resources["gold"] = C.BANK_VICTORY + 200
    g.set_owner(6, 6, "p2")
    g.set_owner(6, 7, "p2")
    g.set_owner(6, 8, "p2")
    g.set_owner(6, 9, "p2")
    st = g.stats()
    assert st["p2"]["score"] > st["p1"]["score"] + 5
    ev = run_turn(g, deposit(g, "p1"))
    assert g.player("p1").economic_streak == C.VICTORY_STREAK_TURNS        # p1 met its condition too
    assert g.result["winner"] == "p2" and g.result["condition"] == "influence"
    assert events_of(ev, "victory")


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
    p.bank = 130
    g.place_units(2, 2, "p1", {"infantry": 3, "cavalry": 1})
    g._set_owner(g.relics[0], "p1")
    g._invalidate()
    s = g.stats()["p1"]
    w, d = C.SCORE_WEIGHTS, C.SCORE_DIVISORS
    expected = (w["tiles"] * p.tiles + w["cities"] * 1 + w["capitals_held"] * 1 + 0
                + 23 // d["influence"] + 45 // d["gold"] + w["relics_held"] * 1 + 42 // d["military_power"]
                + 130 // d["gold"])
    assert s["score"] == expected and s["military_power"] == 42


def test_victory_progress_values():
    g = world(4)
    p = g.player("p1")
    p.resources.update(gold=1000, influence=150)        # stock on hand counts for neither
    p.bank, p.legacy, p.economic_streak = 900, 150, 0
    g._invalidate()
    vp = g.spectator_view()["players"][0]["victory_progress"]
    assert vp == {"conquest": round(1 / 3, 3), "wonder": 0.0, "influence": round(0.8 * 150 / C.LEGACY_VICTORY, 3),
                  "economic": round(0.8 * 900 / C.BANK_VICTORY, 3), "score": 0.0}
    p.bank, p.economic_streak = 2 * C.BANK_VICTORY, 4    # the bank part is capped at 0.8
    g._invalidate()
    assert g.spectator_view()["players"][0]["victory_progress"]["economic"] == round(0.8 + 0.2 * 4 / 10, 3)
