"""Espionage in fog games (docs/RULES.md §14): spy and counterintel orders,
deterministic outcomes, reports, events and reputation."""
import pytest

from agentciv.engine import constants as C
from agentciv.engine import fog as F
from agentciv.engine.orders import ORDER_TYPES, prevalidate
from agentciv.engine.testing import events_of, new_game, run_turn, sandbox


def world():
    g = sandbox(3, fog=True)
    g.relics, g.relic_set = [], frozenset()
    g.add_city(2, 2, "p1", capital=True)
    g.add_city(12, 2, "p2", capital=True)
    g.add_city(2, 12, "p3", capital=True)
    for p in g.players:
        p.resources["gold"] = 2000
    return g


def spy(target="p2", mission="treasury", invest=40):
    return {"type": "spy", "target": target, "mission": mission, "invest": invest}


def outcome(events, spy_pid="p1"):
    return [e["outcome"] for e in events_of(events, "spy_report") if e["player"] == spy_pid]


def types(view):
    return [e["type"] for e in view["events"]]


# ============================================================ outcomes
@pytest.mark.parametrize("delta,expected", [(-1, "failed"), (0, "detected"), ("2ci-1", "detected"),
                                            ("2ci", "success")])
def test_outcome_thresholds(delta, expected):
    g = world()
    g.player("p2").ci_pool = 25
    ci = C.CI_BASE + C.CI_PER_CITY * 1 + 25
    assert F.ci_rating(g, "p2") == ci == 40
    invest = {"2ci-1": 2 * ci - 1, "2ci": 2 * ci}.get(delta, ci + (delta if isinstance(delta, int) else 0))
    gold = g.player("p1").resources["gold"]
    ev = run_turn(g, {"p1": [spy(invest=invest)]})
    assert outcome(ev) == [expected]
    assert g.player("p1").resources["gold"] <= gold - invest + 50     # paid (income is small)
    reps = g.player_view("p1")["intel"]
    assert bool(reps) == (expected != "failed")
    if reps:
        assert reps[0]["outcome"] == expected and reps[0]["target"] == "p2" and reps[0]["as_of_turn"] == 1


def test_same_turn_counterintel_counts():
    g = world()
    assert F.ci_rating(g, "p2") == 15
    ev = run_turn(g, {"p1": [spy(invest=30)], "p2": [{"type": "counterintel", "invest": 20}]})
    assert outcome(ev) == ["failed"]
    g2 = world()
    assert outcome(run_turn(g2, {"p1": [spy(invest=30)]})) == ["success"]


def test_outcomes_independent_of_seat_rotation():
    results = []
    for warmup in (0, 1, 2):
        g = world()
        for _ in range(warmup):
            run_turn(g)
        for p in g.players:
            p.resources["gold"] = 2000
            p.ci_pool = 0
        ev = run_turn(g, {"p1": [spy("p2", invest=40), spy("p3", "military", invest=20)],
                          "p3": [spy("p2", "military", invest=60), spy("p1", invest=31)],
                          "p2": [{"type": "counterintel", "invest": 10}]})
        results.append(sorted((e["player"], e["target"], e["mission"], e["outcome"])
                              for e in events_of(ev, "spy_report")))
    assert results[0] == results[1] == results[2]
    assert results[0] == [("p1", "p2", "treasury", "detected"),   # p2 CI 25: 25 <= 40 < 50
                          ("p1", "p3", "military", "detected"),   # p3 CI 15: 15 <= 20 < 30
                          ("p3", "p1", "treasury", "success"),    # p1 CI 15: 31 >= 30
                          ("p3", "p2", "military", "success")]    # 60 >= 50


def test_gold_spent_on_failure_and_affordability():
    g = world()
    g.player("p2").ci_pool = 100
    g.player("p1").resources["gold"] = 500
    ev = run_turn(g, {"p1": [spy(invest=50)]})
    assert outcome(ev) == ["failed"]
    inc = g.player_view("p1")["you"]["income"]["gold"]
    assert g.player("p1").resources["gold"] == 500 - 50 + inc
    g.player("p1").resources["gold"] = 5
    ev = run_turn(g, {"p1": [spy(invest=20), {"type": "counterintel", "invest": 30}]})
    fails = [e["reason"] for e in events_of(ev, "order_failed")]
    assert any("cannot afford spy" in r for r in fails) and any("cannot afford counterintel" in r for r in fails)
    assert outcome(ev) == [] and g.player("p1").ci_pool == 0


def test_eliminated_target_costs_nothing():
    g = world()
    assert g.submit_orders("p1", [spy("p3")]) == []
    g.player("p3").alive = False
    gold = g.player("p1").resources["gold"]
    ev = g.step()
    assert [e["reason"] for e in events_of(ev, "order_failed")] == ["target eliminated; nothing spent"]
    assert g.player("p1").resources["gold"] >= gold


def test_counterintel_pool_and_decay():
    g = world()
    ev = run_turn(g, {"p2": [{"type": "counterintel", "invest": 100}]})
    assert events_of(ev, "counterintel")[0]["pool"] == 100
    assert g.player("p2").ci_pool == 75                         # 100 * 3 // 4
    v = g.player_view("p2")
    assert v["you"]["counterintel"] == {"pool": 75, "rating": C.CI_BASE + C.CI_PER_CITY + 75}
    assert "counterintel" in types(v) and "counterintel" not in types(g.player_view("p1"))
    g.player("p2").ci_pool = 7
    run_turn(g)
    assert g.player("p2").ci_pool == 5
    run_turn(g)
    assert g.player("p2").ci_pool == 3


# ============================================================ reports
def test_treasury_report_matches_target_view():
    g = world()
    run_turn(g, {"p1": [spy(invest=100)]})
    rep = g.player_view("p1")["intel"][0]
    own = g.player_view("p2")
    me = next(p for p in own["players"] if p["id"] == "p2")
    assert rep["as_of_turn"] == g.turn and rep["outcome"] == "success"
    assert rep["data"] == {"resources": own["you"]["resources"], "income": own["you"]["income"],
                           "score": me["score"], "victory_progress": me["victory_progress"]}


def test_military_report_matches_target_view_and_feeds_sightings():
    g = world()
    g.place_units(12, 2, "p2", {"infantry": 4})
    g.place_units(14, 8, "p2", {"cavalry": 2})
    run_turn(g, {"p1": [spy(mission="military", invest=100)]})
    v1 = g.player_view("p1")
    rep = v1["intel"][0]
    own = g.player_view("p2")
    me = next(p for p in own["players"] if p["id"] == "p2")
    assert rep["data"]["units"] == me["units"] and rep["data"]["military_power"] == me["military_power"]
    assert rep["data"]["upkeep"] == me["upkeep"]
    assert rep["data"]["armies"] == [{"x": a["x"], "y": a["y"], "units": a["units"]}
                                     for a in own["armies"] if a["owner"] == "p2"]
    seen = {(s["x"], s["y"]): (s["units"], s["turn"]) for s in v1["sightings"] if s["owner"] == "p2"}
    assert seen == {(12, 2): ({"infantry": 4}, g.turn), (14, 8): ({"cavalry": 2}, g.turn)}


def test_reports_expire():
    g = world()
    run_turn(g, {"p1": [spy(invest=100)]})
    t0 = g.turn
    for _ in range(C.SPY_REPORT_TURNS):
        run_turn(g)
        assert g.player_view("p1")["intel"][0]["as_of_turn"] == t0
    run_turn(g)
    assert g.player_view("p1")["intel"] == []


# ============================================================ events and reputation
def test_event_visibility_and_incidents():
    g = world()
    g.player("p2").ci_pool = 100
    run_turn(g, {"p1": [spy(invest=50)]})
    v1, v2, v3, pub = (g.player_view("p1"), g.player_view("p2"), g.player_view("p3"), g.spectator_view())
    assert "spy_report" in types(v1) and "spy_report" not in types(v2) + types(v3) + types(pub)
    det = [e for e in v2["events"] if e["type"] == "spy_detected"]
    assert det == [{"turn": 0, "type": "spy_detected", "player": "p2", "spy": "p1", "mission": "treasury",
                    "outcome": "failed"}]
    assert "spy_detected" not in types(v1) + types(v3)
    for v in (v1, v2, v3, pub):
        assert [e for e in v["events"] if e["type"] == "spy_incident"] == \
               [{"turn": 0, "type": "spy_incident", "spy": "p1", "target": "p2"}]
        assert next(p for p in v["players"] if p["id"] == "p1")["reputation"]["spy_incidents"] == 1
    rep = [e for e in v1["events"] if e["type"] == "spy_report"][0]
    assert set(rep) == {"turn", "type", "player", "target", "mission", "invest", "outcome"}


def test_detected_mission_alerts_target_without_incident():
    g = world()
    ev = run_turn(g, {"p1": [spy(invest=20)]})                 # CI 15: detected
    assert outcome(ev) == ["detected"]
    assert "spy_detected" in types(g.player_view("p2")) and "spy_incident" not in types(g.player_view("p3"))
    assert g.player("p1").spy_incidents == 0


# ============================================================ validation
def test_standard_game_rejects_fog_orders_with_unchanged_text():
    g = new_game(2)
    errs = g.submit_orders("p1", [spy(), {"type": "counterintel", "invest": 5}])
    want = f"unknown order type 'spy'; valid: {', '.join(ORDER_TYPES)}"
    assert errs[0] == {"index": 0, "error": want}
    assert errs[1]["error"].startswith("unknown order type 'counterintel'; valid: move,")


@pytest.mark.parametrize("order,fragment", [
    (spy(target="p1"), "cannot target yourself"),
    (spy(target="p9"), "unknown player"),
    (spy(mission="economy"), "mission must be one of military, treasury"),
    (spy(invest=19), "invest must be 20..1000"),
    (spy(invest=1001), "invest must be 20..1000"),
    (spy(invest="lots"), "invest must be an integer"),
    ({"type": "counterintel", "invest": 0}, "invest must be 1..500"),
    ({"type": "counterintel", "invest": 501}, "invest must be 1..500"),
])
def test_fog_order_errors(order, fragment):
    g = world()
    errs = g.submit_orders("p1", [order])
    assert errs and fragment in errs[0]["error"]


def test_fog_order_limits():
    g = world()
    errs = g.submit_orders("p1", [spy(), spy(), spy("p3"), spy("p3", "military"),
                                  {"type": "counterintel", "invest": 5}, {"type": "counterintel", "invest": 5}])
    assert [e["index"] for e in errs] == [1, 3, 5]
    assert "duplicate spy order" in errs[0]["error"] and "at most 2 spy orders" in errs[1]["error"]
    assert "one counterintel order" in errs[2]["error"]
    errs = g.submit_orders("p1", [{"type": "move", "from": [0, 0], "to": [0, 1]}, {"type": "junk"}])
    assert "valid: move," in errs[1]["error"] and errs[1]["error"].endswith("say, spy, counterintel")


def test_prevalidation_independent_of_target_pool_and_gold():
    orders = [spy(invest=20), spy("p3", "military", invest=1000), {"type": "counterintel", "invest": 500},
              spy("p2", "military", invest=10 ** 6)]
    out = []
    for pool, gold in ((0, 0), (10 ** 4, 10 ** 6)):
        g = world()
        g.player("p1").resources["gold"] = 30
        for q in ("p2", "p3"):
            g.player(q).ci_pool = pool
            g.player(q).resources["gold"] = gold
        out.append(prevalidate(g, "p1", orders))
    assert out[0] == out[1]
    assert [e["index"] for e in out[0][1]] == [3]                # own gold is checked at resolution
