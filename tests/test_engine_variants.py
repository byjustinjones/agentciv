"""Experimental rule variants (agentciv.engine.variants, docs/BALANCE.md §10):
off by default, and each one does what its definition says."""
import math

import pytest

from agentciv.engine import Game, GameConfig, combat
from agentciv.engine import constants as C
from agentciv.engine import variants as V
from agentciv.engine.combat import Side
from agentciv.engine.rules import streak_deposit
from agentciv.engine.testing import events_of, run_turn, sandbox
from agentciv.tournament import run_game


def hostile_except(*pairs):
    peace = {frozenset(p) for p in pairs}
    return lambda a, b: a != b and frozenset((a, b)) not in peace


def always_hostile(a, b):
    return a != b


def losses_left(lost, n):
    return n - int(math.floor(n * (1 - math.sqrt(1 - lost ** 2)) + 0.5))


# ---------------------------------------------------------------- parsing
def test_validate_and_parse():
    assert V.validate({}) == {} and V.validate(None) == {}
    assert V.validate({"city_loss": "Held:10", "pool_allies": 1, "legacy_target": 3000}) == {
        "city_loss": "held:10", "pool_allies": True, "legacy_target": 3000}
    for bad in ({"nope": 1}, {"city_loss": "minus"}, {"city_loss": "held:0"}, {"bank_base": -1},
                {"bank_target": True}, {"symmetric_ties": "yes"}):
        with pytest.raises(ValueError):
            V.validate(bad)
    assert V.parse_city_loss("minus:3") == ("minus", 3)
    assert V.parse_arg("pool_allies=true") == ("pool_allies", True)
    assert V.parse_arg("bank_base=40") == ("bank_base", 40)
    assert V.parse_arg("city_loss=held:10") == ("city_loss", "held:10")
    with pytest.raises(ValueError):
        Game(GameConfig(variants={"city_loss": "sometimes"}))


def test_default_config_has_no_variants_and_results_carry_none():
    assert GameConfig().variants == {} and Game().variants == {}
    plain = run_game(["economist", "turtle"], seed=3, max_turns=12)
    empty = run_game(["economist", "turtle"], seed=3, max_turns=12, variants={})
    assert "variants" not in plain
    drop = ("seconds", "think_seconds", "think_ms_per_turn", "think_ms_max", "negotiate_ms_per_turn",
            "negotiate_ms_per_call", "negotiate_ms_max")
    assert {k: v for k, v in plain.items() if k not in drop} == {k: v for k, v in empty.items() if k not in drop}
    on = run_game(["economist", "turtle"], seed=3, max_turns=12, variants={"pool_allies": True})
    assert on["variants"] == {"pool_allies": True}


def test_threshold_variants_reach_the_views_and_the_victory_check():
    g = sandbox(2)
    base = g.thresholds()
    g.variants = V.validate({"legacy_target": 3000, "bank_target": 4000, "bank_base": 40})
    thr = g.player_view("p1")["victory"]["thresholds"]
    assert thr["legacy"] == 3000 and thr["bank"] == 4000
    assert {k: v for k, v in thr.items() if k not in ("bank", "legacy")} == \
        {k: v for k, v in base.items() if k not in ("bank", "legacy")}
    g.add_city(2, 2, "p1", capital=True)
    assert g.bank_limit("p1") == 40
    assert g.player_view("p1")["you"]["bank_limit"] == 40


# ---------------------------------------------------------------- city loss
SPOTS = [(2, 2), (12, 2), (2, 12)]


def leader(variant, turns=5):
    g = sandbox(3)
    g.relics, g.relic_set = [], frozenset()
    for k in range(3):
        g.add_city(*SPOTS[k], f"p{k + 1}", capital=True)
    g.variants = V.validate({"city_loss": variant} if variant else {})
    p1 = g.player("p1")
    g.add_city(6, 2, "p1")
    p1.bank, p1.legacy = C.BANK_VICTORY + 500, C.LEGACY_VICTORY + 500
    for _ in range(turns):
        run_turn(g, {"p1": deposit(g)})
    assert (p1.economic_streak, p1.influence_streak) == (turns, turns)
    return g, p1


def deposit(g, pid="p1"):
    need = streak_deposit(g.bank_limit(pid))
    g.player(pid).resources["gold"] += need
    return [{"type": "bank", "gold": need}]


def lose_the_new_city(g):
    g.place_units(7, 2, "p2", {"infantry": 8})
    ev = run_turn(g, {"p1": deposit(g), "p2": [{"type": "move", "from": [7, 2], "to": [6, 2]}]})
    assert events_of(ev, "city_captured")[0]["to"] == "p2"
    return ev


def test_minus_variant_costs_three_streak_turns():
    g, p1 = leader("minus:3", turns=5)
    ev = lose_the_new_city(g)
    assert (p1.economic_streak, p1.influence_streak) == (2, 2)
    paused = [e for e in events_of(ev, "streak_paused") if e["player"] == "p1"]
    assert {(e["condition"], e["reason"], e["lost_turns"]) for e in paused} == {
        ("economic", "city_lost", 3), ("influence", "city_lost", 3)}
    assert not events_of(ev, "streak_ended")
    run_turn(g, {"p1": deposit(g)})
    assert (p1.economic_streak, p1.influence_streak) == (3, 3)


def test_minus_variant_ends_a_short_streak():
    g, p1 = leader("minus:3", turns=3)
    ev = lose_the_new_city(g)
    assert (p1.economic_streak, p1.influence_streak) == (0, 0)
    assert {e.get("reason") for e in events_of(ev, "streak_ended")} == {"city_lost"}


def test_held_variant_ignores_a_city_held_less_than_n_turns():
    g, p1 = leader("held:10", turns=4)
    ev = lose_the_new_city(g)
    assert not events_of(ev, "streak_ended")
    assert (p1.economic_streak, p1.influence_streak) == (5, 5)


def test_held_variant_resets_for_a_city_held_n_turns():
    g, p1 = leader("held:10", turns=4)
    g.cities[g.idx(6, 2)].founded_turn = g.turn - 10
    lose_the_new_city(g)
    assert (p1.economic_streak, p1.influence_streak) == (0, 0)


def test_held_variant_counts_from_the_capture_of_a_city():
    g, p1 = leader("held:10", turns=2)
    city = g.add_city(9, 6, "p2")
    city.founded_turn = -50
    g.place_units(9, 7, "p1", {"infantry": 8})
    run_turn(g, {"p1": deposit(g) + [{"type": "move", "from": [9, 7], "to": [9, 6]}]})
    assert city.owner == "p1" and g.city_taken_turn[city.idx] == g.turn - 1
    g.place_units(9, 5, "p2", {"infantry": 20})
    ev = run_turn(g, {"p1": deposit(g), "p2": [{"type": "move", "from": [9, 5], "to": [9, 6]}]})
    assert events_of(ev, "city_captured")[0]["from"] == "p1"
    assert (p1.economic_streak, p1.influence_streak) == (4, 4)


def test_default_still_resets_on_any_city_loss():
    g, p1 = leader(None, turns=4)
    lose_the_new_city(g)
    assert (p1.economic_streak, p1.influence_streak) == (0, 0)


# ---------------------------------------------------------------- combat: reference cases
def test_today_two_allied_stacks_of_6_lose_to_10():
    a, b = Side("a", {"infantry": 6}, order=(1, 0)), Side("b", {"infantry": 6}, order=(1, 1))
    c = Side("c", {"infantry": 10}, order=(1, 2))
    recs = combat.resolve([a, b, c], hostile_except(("a", "b")))
    assert [r["winner"] for r in recs] == ["c", "c"]
    assert a.units == {} and b.units == {}
    assert c.units == {"infantry": 5}         # 10 -> 8 against the first 6, then 8 -> 5


def test_one_stack_of_12_beats_10_with_7_left():
    a, c = Side("a", {"infantry": 12}), Side("c", {"infantry": 10})
    rec = combat.resolve([a, c], always_hostile)[0]
    assert rec["winner"] == "a" and a.units == {"infantry": 7} and c.units == {}


@pytest.mark.parametrize("order", [((1, 0), (1, 1)), ((1, 1), (1, 0))])
def test_pooled_allies_6_and_6_fight_like_12(order):
    a, b = Side("a", {"infantry": 6}, order=order[0]), Side("b", {"infantry": 6}, order=order[1])
    c = Side("c", {"infantry": 10}, order=(1, 2))
    recs = combat.resolve([a, b, c], hostile_except(("a", "b")), pool=True)
    assert len(recs) == 1
    rec = recs[0]
    first = "a" if order[0] < order[1] else "b"
    assert rec["winner"] == first and rec["coalitions"] == [[first, "b" if first == "a" else "a"]]
    assert sorted(rec["sides"]) == ["a", "b", "c"]
    assert c.units == {} and c.defeated and not a.defeated and not b.defeated
    # 12 vs 10 loses 5: 2.5 each, the odd unit goes to the first in the queue
    assert a.units["infantry"] + b.units["infantry"] == 7
    left = {"a": a.units["infantry"], "b": b.units["infantry"]}
    assert left[first] == 3 and sum(left.values()) - left[first] == 4
    assert rec["losses"]["c"] == {"infantry": 10}
    assert sum(rec["losses"][q]["infantry"] for q in ("a", "b")) == 5


def test_share_losses_is_exact_and_proportional():
    m = [Side("a", {"infantry": 9, "archer": 1}), Side("b", {"infantry": 3}), Side("c", {"infantry": 1})]
    out = combat.share_losses(m, {"infantry": 7, "archer": 1})
    assert sum(o.get("infantry", 0) for o in out) == 7 and out[0]["archer"] == 1
    assert out[0]["infantry"] == 5 and out[1]["infantry"] == 2 and "infantry" not in out[2]


def test_pooled_losing_coalition_loses_everything():
    a, b = Side("a", {"infantry": 3}, order=(1, 0)), Side("b", {"infantry": 4}, order=(1, 1))
    c = Side("c", {"infantry": 10}, order=(1, 2))
    rec = combat.resolve([a, b, c], hostile_except(("a", "b")), pool=True)[0]
    assert rec["winner"] == "c" and a.defeated and b.defeated and a.units == b.units == {}
    assert c.units == {"infantry": losses_left(0.7, 10)}


def test_city_owner_is_never_pooled():
    owner = Side("q", {"infantry": 2}, defender=True, city_owner=True, garrison=15, order=(0, 0))
    ally = Side("r", {"infantry": 2}, defender=True, order=(0, 1))
    att = Side("p", {"infantry": 6}, order=(1, 2))
    recs = combat.resolve([owner, ally, att], hostile_except(("q", "r")), walls=1, pool=True)
    assert all("coalitions" not in r for r in recs)
    assert [r["sides"] for r in recs] == [["r", "p"], ["q", "p"]]


@pytest.mark.parametrize("pool,symmetric", [(True, False), (False, True), (True, True)])
def test_two_side_battles_are_unchanged(pool, symmetric):
    cases = [
        (Side("a", {"infantry": 10}), Side("b", {"infantry": 6}), 0),
        (Side("a", {"infantry": 2}, defender=True), Side("b", {"infantry": 2}), 0),
        (Side("c", {"infantry": 2}), Side("d", {"infantry": 2}), 0),
        (Side("q", {"archer": 5}, defender=True, city_owner=True, garrison=10), Side("p", {"siege": 3, "cavalry": 6}), 2),
        (Side("x", {"cavalry": 4}, terrain_bonus=True, defender=True), Side("y", {"archer": 7}), 0),
    ]
    for a, b, walls in cases:
        ref = [Side(s.pid, dict(s.units), s.defender, s.city_owner, s.garrison, s.terrain_bonus) for s in (a, b)]
        want = combat.resolve(ref, always_hostile, walls=walls)
        got = combat.resolve([a, b], always_hostile, walls=walls, pool=pool, symmetric=symmetric)
        assert got == want
        assert [(s.units, s.defeated, s.alive) for s in (a, b)] == [(s.units, s.defeated, s.alive) for s in ref]


# ---------------------------------------------------------------- combat: symmetric ties
def three(units=None):
    return [Side(q, dict(units or {"infantry": 5}), order=(1, k)) for k, q in enumerate("abc")]


def test_today_three_equal_attackers_leave_the_last_in_the_queue():
    s = three()
    combat.resolve(s, always_hostile)
    assert [x.units for x in s] == [{}, {}, {"infantry": 5}]


def test_symmetric_ties_destroy_all_three_equal_attackers():
    s = three()
    recs = combat.resolve(s, always_hostile, symmetric=True)
    assert len(recs) == 1 and recs[0]["simultaneous"] and recs[0]["winner"] is None
    assert sorted(recs[0]["sides"]) == ["a", "b", "c"]
    assert all(x.units == {} and x.defeated and not x.alive for x in s)


def test_symmetric_ties_need_pairwise_hostility_and_equal_matchups():
    s = three()
    recs = combat.resolve(s, hostile_except(("a", "b")), symmetric=True)   # a, b at peace: queue order
    assert not any(r.get("simultaneous") for r in recs)
    assert [x.units for x in s] == [{}, {"infantry": 5}, {}]
    mixed = [Side("a", {"infantry": 6}, order=(1, 0)), Side("b", {"cavalry": 5}, order=(1, 1)),
             Side("c", {"archer": 7, "siege": 1}, order=(1, 2))]          # raw 60 each, counters differ
    assert {combat.raw_power(x) for x in mixed} == {60}
    recs = combat.resolve(mixed, always_hostile, symmetric=True)
    assert not any(r.get("simultaneous") for r in recs)


def test_symmetric_ties_leave_a_defender_tie_alone():
    s = three()
    s[0].defender = True
    s[0].order = (0, 0)
    recs = combat.resolve(s, always_hostile, symmetric=True)
    assert not any(r.get("simultaneous") for r in recs)


# ---------------------------------------------------------------- inside the engine
def world(variants):
    g = sandbox(3)
    g.add_city(2, 2, "p1", capital=True)
    g.add_city(12, 12, "p2", capital=True)
    g.add_city(2, 12, "p3", capital=True)
    g.variants = V.validate(variants)
    return g


@pytest.mark.parametrize("variants,survivor", [({}, "p3"), ({"pool_allies": True}, "p1|p2")])
def test_engine_allied_attack_on_a_stack(variants, survivor):
    g = world(variants)
    g.treaties[("p1", "p2")] = 99
    g.place_units(8, 8, "p3", {"infantry": 10})
    g.place_units(7, 8, "p1", {"infantry": 6})
    g.place_units(9, 8, "p2", {"infantry": 6})
    run_turn(g, {"p1": [{"type": "move", "from": [7, 8], "to": [8, 8]}],
                 "p2": [{"type": "move", "from": [9, 8], "to": [8, 8]}]})
    left = g.armies.get(g.idx(8, 8), {})
    if survivor == "p3":
        assert set(left) == {"p3"}
    else:
        assert set(left) == {"p1", "p2"}
        assert sum(u["infantry"] for u in left.values()) == 7


def test_engine_symmetric_three_way_tie_on_every_turn():
    for turn in (0, 1, 2):
        g = world({"symmetric_ties": True})
        g.turn = turn
        for q, src in (("p1", (7, 8)), ("p2", (9, 8)), ("p3", (8, 9))):
            g.place_units(*src, q, {"infantry": 5})
        ev = run_turn(g, {q: [{"type": "move", "from": list(src), "to": [8, 8]}]
                          for q, src in (("p1", (7, 8)), ("p2", (9, 8)), ("p3", (8, 9)))})
        assert g.idx(8, 8) not in g.armies
        assert [b.get("simultaneous") for b in events_of(ev, "battle")] == [True]


def test_tournament_cli_passes_variants(capsys):
    from agentciv import tournament
    assert tournament.main(["--bots", "economist,turtle", "--games", "1", "--max-turns", "5", "--quiet",
                            "--variant", "city_loss=minus:3", "--variant", "symmetric_ties=true"]) == 0
    assert "experimental variants: city_loss=minus:3, symmetric_ties=True" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        tournament.main(["--bots", "economist,turtle", "--games", "1", "--variant", "nope=1"])
