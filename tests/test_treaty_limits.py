"""Scarce treaties (rules §9): slots, lengths, bonds, cooldowns, release, and
the priced break (influence, legacy, bank share, bond, deal refunds)."""
import random

import pytest

from agentciv.engine import constants as C
from agentciv.engine import deals as D
from agentciv.engine import rules_json
from agentciv.engine.rulesdoc import render as rules_md
from agentciv.engine.testing import events_of, run_turn, sandbox

SPOTS = [(2, 2), (12, 2), (2, 12), (12, 12), (20, 2), (20, 20)]


def world(n=4):
    g = sandbox(n)
    for k in range(n):
        g.add_city(*SPOTS[k], f"p{k + 1}", capital=True)
    return g


def order_sign(g, a, b, turns=C.TREATY_MIN_TURNS, bond=None, accept_bond=None):
    p = {"type": "propose_treaty", "to": b, "turns": turns}
    if bond is not None:
        p["bond"] = bond
    run_turn(g, {a: [p]})
    acc = {"type": "accept_treaty", "from": a}
    if accept_bond is not None:
        acc["bond"] = accept_bond
    return run_turn(g, {b: [acc]})


def deal_sign(g, a, b, peace=C.DEAL_PEACE_MIN_TURNS, give=None, get=None):
    r = g.diplomacy(a, [{"type": "propose", "to": b, "give": give or {}, "get": get or {}, "peace": peace}])[0]
    assert r["ok"], r
    r = g.diplomacy(b, [{"type": "accept", "deal": r["deal"]}])[0]
    assert r["ok"], r
    return r["deal"]


def failures(ev):
    return [e["reason"] for e in events_of(ev, "order_failed")]


# ------------------------------------------------------------------ slots
@pytest.mark.parametrize("n,slots", [(2, 1), (3, 1), (4, 2), (5, 2), (6, 3)])
def test_slot_count(n, slots):
    g = world(n)
    assert g.treaty_slots("p1") == slots == max(1, -(-(n - 1) // C.TREATY_SLOT_DIVISOR))
    assert g.player_view("p1")["you"]["treaty"]["slots"] == slots


def test_slot_cap_on_the_order_path():
    g = world(4)
    run_turn(g, {"p1": [{"type": "propose_treaty", "to": q, "turns": 20} for q in ("p2", "p3", "p4")]})
    ev = run_turn(g, {q: [{"type": "accept_treaty", "from": "p1"}] for q in ("p2", "p3", "p4")})
    assert len(events_of(ev, "treaty_signed")) == 2
    assert failures(ev) == ["p1 already has 2 treaties (limit 2)"]
    assert g.treaties_held("p1") == 2
    # a new proposal by a player at its limit fails at resolution
    ev = run_turn(g, {"p1": [{"type": "propose_treaty", "to": "p4", "turns": 20}]})
    assert failures(ev) == ["p1 already has 2 treaties (limit 2)"]
    # ... and so does one to a player at its limit
    ev = run_turn(g, {"p4": [{"type": "propose_treaty", "to": "p1", "turns": 20}]})
    assert failures(ev) == ["p1 already has 2 treaties (limit 2)"]


def test_slot_cap_on_the_deal_path_at_propose_and_at_settle():
    g = world(4)
    deal_sign(g, "p1", "p2")
    r = g.diplomacy("p1", [{"type": "propose", "to": "p3", "peace": 20}])[0]
    assert r["ok"]
    open_deal = r["deal"]
    deal_sign(g, "p1", "p4")                      # the last slot goes elsewhere first
    v = g.player_view("p3")
    d = next(x for x in v["deals"]["open"] if x["id"] == open_deal)
    assert d["problem"] == "p1 already has 2 treaties (limit 2)" and d["deliverable"] is False
    assert D.view_deal_problem(v, d) == d["problem"]
    r = g.diplomacy("p3", [{"type": "accept", "deal": open_deal}])[0]
    assert not r["ok"] and "limit 2" in r["error"] and not g.treaty("p1", "p3")
    r = g.diplomacy("p3", [{"type": "propose", "to": "p1", "peace": 20}])[0]
    assert not r["ok"] and r["error"] == "p1 already has 2 treaties (limit 2)"
    # a renewal needs no free slot
    deal_sign(g, "p2", "p1", peace=40, give={"gold": 1})
    assert g.treaties[("p1", "p2")] == g.turn + 40


def test_treaties_over_the_limit_are_kept_after_an_elimination():
    g = world(6)
    for q in ("p2", "p3", "p4"):
        deal_sign(g, "p1", q)
    assert g.treaty_slots("p1") == 3 and g.treaties_held("p1") == 3
    del g.cities[g.idx(*SPOTS[5])]                # p6 loses its only city
    g._invalidate()
    run_turn(g)
    assert not g.player("p6").alive
    assert g.treaty_slots("p1") == 2 and g.treaties_held("p1") == 3
    assert all(g.treaty("p1", q) for q in ("p2", "p3", "p4"))
    r = g.diplomacy("p1", [{"type": "propose", "to": "p5", "peace": 20}])[0]
    assert not r["ok"] and r["error"] == "p1 already has 3 treaties (limit 2)"
    deal_sign(g, "p1", "p2", peace=30, give={"gold": 1})   # renewals still work


# ------------------------------------------------------------------ lengths
def test_treaty_lengths():
    g = world(4)
    for turns, ok in ((C.TREATY_MIN_TURNS - 1, False), (C.TREATY_MIN_TURNS, True),
                      (C.TREATY_MAX_TURNS, True), (C.TREATY_MAX_TURNS + 1, False)):
        errs = g.submit_orders("p1", [{"type": "propose_treaty", "to": "p2", "turns": turns}])
        assert (errs == []) == ok, (turns, errs)
        r = g.diplomacy("p1", [{"type": "propose", "to": "p2", "peace": turns}])[0]
        assert r["ok"] == ok, (turns, r)
        if ok:
            g.diplomacy("p1", [{"type": "withdraw", "deal": r["deal"]}])
    assert (C.TREATY_MIN_TURNS, C.TREATY_MAX_TURNS) == (C.DEAL_PEACE_MIN_TURNS, C.DEAL_PEACE_MAX_TURNS) == (20, 40)


# ------------------------------------------------------------------ bonds
def test_bonds_are_recorded_public_and_limited_by_the_unpledged_bank():
    g = world(4)
    p1 = g.player("p1")
    p1.bank = 100
    ev = order_sign(g, "p1", "p2", bond=60)
    assert events_of(ev, "treaty_signed")[0]["bond"] == {"p1": 60, "p2": 0}
    row = g.player_view("p3")["treaties"][0]
    assert row["bond"] == {"p1": 60, "p2": 0} and row["signed_turn"] == 1
    t = g.player_view("p1")["you"]["treaty"]
    assert (t["bond_pledged"], t["bond_free"], t["bond_required"]) == (60, 40, 0)
    # a second pledge beyond the unpledged bank fails, on both paths
    r = g.diplomacy("p1", [{"type": "propose", "to": "p3", "peace": 20, "give": {"bond": 41}}])[0]
    assert not r["ok"] and r["error"] == "p1's bond (41) exceeds its unpledged bank (40)"
    ev = run_turn(g, {"p1": [{"type": "propose_treaty", "to": "p3", "turns": 20, "bond": 41}]})
    assert failures(ev) == ["p1's bond (41) exceeds its unpledged bank (40)"]
    deal_sign(g, "p1", "p3", give={"bond": 40})
    assert g.bond_free("p1") == 0
    # pledged gold stays in the bank and earns interest
    assert p1.bank >= 100
    # bond is only valid with peace, and never negative
    assert not g.diplomacy("p1", [{"type": "propose", "to": "p4", "give": {"bond": 5}}])[0]["ok"]
    assert g.submit_orders("p1", [{"type": "propose_treaty", "to": "p4", "turns": 20, "bond": -1}])


def test_the_required_bond_of_a_breaker():
    g = world(4)
    p1 = g.player("p1")
    p1.betrayals = 2
    assert g.bond_required("p1") == 2 * C.TREATY_BOND_PER_BETRAYAL
    ev = run_turn(g, {"p1": [{"type": "propose_treaty", "to": "p2", "turns": 20}]})
    assert failures(ev) == [f"p1's bond ({2 * C.TREATY_BOND_PER_BETRAYAL}) exceeds its unpledged bank (0)"]
    r = g.diplomacy("p2", [{"type": "propose", "to": "p1", "peace": 20}])[0]
    assert not r["ok"] and "p1's bond (100) exceeds" in r["error"]
    p1.bank = 130
    ev = order_sign(g, "p1", "p2", bond=30)
    assert events_of(ev, "treaty_signed")[0]["bond"] == {"p1": 130, "p2": 0}


def test_renewal_keeps_or_replaces_the_bond():
    g = world(4)
    g.player("p1").bank = g.player("p2").bank = 200
    deal_sign(g, "p1", "p2", give={"bond": 50}, get={"bond": 20})
    key = ("p1", "p2")
    assert g.treaty_terms[key]["bond"] == {"p1": 50, "p2": 20}
    deal_sign(g, "p2", "p1", peace=30, give={"gold": 1})          # no bond offered: both kept
    assert g.treaty_terms[key]["bond"] == {"p1": 50, "p2": 20}
    deal_sign(g, "p1", "p2", peace=30, give={"bond": 0}, get={"bond": 90})
    assert g.treaty_terms[key]["bond"] == {"p1": 0, "p2": 90}
    assert g.treaty_terms[key]["signed"] == 0 and len(g.treaty_terms[key]["deals"]) == 3
    # a kept bond is raised to the required minimum
    g.player("p2").betrayals = 2
    deal_sign(g, "p1", "p2", peace=30, give={"gold": 1})
    assert g.treaty_terms[key]["bond"] == {"p1": 0, "p2": 100}


def test_expiry_and_elimination_release_bonds():
    g = world(4)
    g.player("p1").bank = 100
    order_sign(g, "p1", "p2", bond=100)
    assert g.bond_free("p1") == 0
    ev = []
    while g.treaty("p1", "p2"):
        ev = run_turn(g)
    assert events_of(ev, "treaty_expired")[0]["released"] == {"p1": 100, "p2": 0}
    assert g.bond_free("p1") == g.player("p1").bank and not g.treaty_terms


# ------------------------------------------------------------------ release
def test_release_needs_both_parties_in_the_same_turn():
    g = world(4)
    deal_sign(g, "p3", "p4")
    ev = run_turn(g, {"p3": [{"type": "release_treaty", "with": "p4"}]})
    assert g.treaty("p3", "p4") and failures(ev) == ["p4 did not order release_treaty this turn"]
    ev = run_turn(g, {"p3": [{"type": "release_treaty", "with": "p4"}],
                      "p4": [{"type": "release_treaty", "with": "p3"}]})
    assert not g.treaty("p3", "p4")
    assert events_of(ev, "treaty_released")[0] == {"turn": ev[0]["turn"], "type": "treaty_released",
                                                   "a": "p3", "b": "p4"}
    assert g.player("p3").betrayals == g.player("p4").betrayals == 0 and not g.broken_pairs
    deal_sign(g, "p3", "p4")                      # no cooldown after a release
    assert g.submit_orders("p1", [{"type": "release_treaty", "with": "p2"}])
    assert g.submit_orders("p3", [{"type": "release_treaty", "with": "p4"},
                                  {"type": "break_treaty", "with": "p4"}])


# ------------------------------------------------------------------ breaking
def test_break_pipeline_exact_amounts():
    g = world(4)
    p1, p2 = g.player("p1"), g.player("p2")
    p1.bank, p1.legacy, p1.resources["gold"] = 1000, 800, 0
    p2.resources["gold"], p2.resources["food"] = 500, 300
    # p2 pays 300 gold now + 10 food/turn for 20 turns for 40 turns of peace; p1 pledges 100
    r = g.diplomacy("p2", [{"type": "propose", "to": "p1", "give": {"gold": 300, "per_turn": {"food": 10}, "turns": 20},
                            "get": {"bond": 100}, "peace": 40}])[0]
    assert g.diplomacy("p1", [{"type": "accept", "deal": r["deal"]}])[0]["ok"]
    for _ in range(10):
        run_turn(g)
    t = g.turn
    p1.resources["influence"] = 300
    bank, legacy, gold2 = p1.bank, p1.legacy, p2.resources["gold"]
    preview = g.player_view("p1")["you"]["treaty"]["break_preview"]["p2"]
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    b = events_of(ev, "treaty_broken")[0]
    refund = 300 * (40 - t) // 40
    share = bank * C.TREATY_BREAK_PCT // 100
    assert (b["cost"], b["legacy_lost"], b["bank_share"], b["bond"], b["refund"]) == (
        C.TREATY_BREAK_COST, legacy * C.TREATY_BREAK_PCT // 100, share, 100, refund)
    assert b["paid"] == 100 + share + refund and b["debt"] == 0 and b["betrayals"] == 1
    # the offered bond and the refund leave the bank: 1 influence per 2 gold, like a default
    fee = -(-(100 + refund) // C.CONTRACT_DEFAULT_GOLD_PER_INFLUENCE)
    assert b["bank_fee"] == fee
    assert preview == {"influence": b["cost"], "legacy": b["legacy_lost"], "gold_to_partner": b["paid"],
                       "bank_fee": fee, "influence_debt": 0, "cancels": b["cancelled"]}
    assert b["cancelled"] and not g.contracts
    assert events_of(ev, "contract_cancelled")[0]["reason"] == "treaty_broken"
    inc = g.stats()
    assert p1.bank == bank - b["paid"]                      # all of it from the bank
    assert p2.resources["gold"] == gold2 + b["paid"] + inc["p2"]["income"]["gold"]
    assert p1.betrayals == 1 and p1.legacy == legacy - b["legacy_lost"] + inc["p1"]["income"]["influence"]
    # escalation: the next break costs more
    assert g.treaty_break_cost("p1") == 2 * C.TREATY_BREAK_COST
    assert g.treaty_break_pct("p1") == 2 * C.TREATY_BREAK_PCT
    p1.betrayals = 10
    assert g.treaty_break_pct("p1") == C.TREATY_BREAK_MAX_PCT


def test_break_pays_from_bank_then_gold_then_debt():
    g = world(4)
    p1 = g.player("p1")
    p1.bank = 300
    order_sign(g, "p1", "p2", bond=300)
    p1.bank = 100                                   # e.g. seized by a default after pledging
    p1.resources["gold"] = 60
    p1.resources["influence"] = 100
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    b = events_of(ev, "treaty_broken")[0]
    owed = 300 + 100 * C.TREATY_BREAK_PCT // 100
    assert b["bank_share"] == 10 and b["paid"] == 160
    # 90 of the offered bond came from the bank: a 45 fee, held after the 50 break cost
    assert b["bank_fee"] == 45
    assert b["debt"] == -(-(owed - 160) // C.CONTRACT_DEFAULT_GOLD_PER_INFLUENCE)
    assert p1.bank == 0 and p1.resources["gold"] == g.stats()["p1"]["income"]["gold"]
    assert g.player_view("p3")["players"][0]["reputation"]["influence_debt"] == p1.influence_debt > 0


def _default_route_cost(n):
    """Influence a player pays to move ``n`` bank gold to another player's
    gold by defaulting on a contract worth ``n`` (the obligation is seized)."""
    return D.default_penalty(n)


@pytest.mark.parametrize("bond", [900, 1000])
def test_a_self_arranged_break_moves_bank_gold_no_cheaper_than_a_default(bond):
    g = world(4)
    p1, p2 = g.player("p1"), g.player("p2")
    p1.bank, p1.legacy, p1.resources["gold"] = 1000, 0, 0
    p1.resources["influence"] = 60
    gold2 = p2.resources["gold"]
    deal_sign(g, "p1", "p2", give={"bond": bond})
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    b = events_of(ev, "treaty_broken")[0]
    moved = 1000 - p1.bank
    assert moved == b["paid"] == 1000 and p2.resources["gold"] - gold2 - g.stats()["p2"]["income"]["gold"] == moved
    assert b["bank_fee"] == -(-(moved - b["bank_share"]) // 2)
    assert b["cost"] + b["bank_fee"] >= _default_route_cost(moved)
    # the fee beyond the influence left after the break cost is owed, plus the unpaid 100 of a 1000 bond
    unpaid = b["bank_share"] + bond - moved
    assert b["debt"] == b["bank_fee"] - (60 - b["cost"]) + -(-unpaid // 2)
    assert p1.influence_debt == b["debt"] - g.stats()["p1"]["income"]["influence"]   # phase 7 repays


def test_the_bank_paid_refund_of_a_peace_deal_carries_the_fee():
    g = world(4)
    p1, p2 = g.player("p1"), g.player("p2")
    p1.bank, p1.resources["gold"] = 1000, 0
    p2.resources["wood"] = 400
    deal_sign(g, "p2", "p1", give={"wood": 400})            # p2 "pays" 400 wood for peace
    p1.resources["influence"] = 1000
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    b = events_of(ev, "treaty_broken")[0]
    assert b["refund"] == 400 * D.REFERENCE_PRICES["wood"] and b["bond"] == 0
    assert b["bank_fee"] == -(-b["refund"] // 2) and b["debt"] == 0
    assert b["cost"] + b["bank_fee"] >= _default_route_cost(b["refund"])


def test_the_required_bond_and_bank_share_carry_no_fee():
    g = world(4)
    p1 = g.player("p1")
    p1.betrayals, p1.bank = 1, 500
    deal_sign(g, "p1", "p2")                                # p1 pledges the required 50
    assert g.treaty_terms[("p1", "p2")]["bond"]["p1"] == C.TREATY_BOND_PER_BETRAYAL
    p1.resources["influence"] = 200
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    b = events_of(ev, "treaty_broken")[0]
    assert b["paid"] == b["bank_share"] + C.TREATY_BOND_PER_BETRAYAL and b["bank_fee"] == 0


def test_refund_is_prorated_per_deal_and_only_for_the_victims_net_lump():
    g = world(4)
    p1, p2 = g.player("p1"), g.player("p2")
    p2.resources["gold"] = 1000
    p1.resources["wood"] = 200
    d1 = deal_sign(g, "p2", "p1", peace=20, give={"gold": 200})              # p2 pays 200 for 20 turns
    for _ in range(5):
        run_turn(g)
    # renewal: p2 hands 100 gold, p1 hands 50 wood back (net 100 - 50·wood price)
    d2 = deal_sign(g, "p2", "p1", peace=40, give={"gold": 100}, get={"wood": 50})
    assert g.treaty_terms[("p1", "p2")]["deals"] == [d1, d2]
    for _ in range(10):
        run_turn(g)
    t = g.turn
    wood = D.REFERENCE_PRICES["wood"]
    expect = int(200 * (20 - (t - 0)) / 20 * (t < 20) + (100 - 50 * wood) * (5 + 40 - t) / 40)
    assert D.peace_refund(g, {d1, d2}, "p2", "p1") == expect
    assert D.peace_refund(g, {d1, d2}, "p1", "p2") == 0      # p1 handed over no net value
    p1.resources["influence"] = 100
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    assert events_of(ev, "treaty_broken")[0]["refund"] == expect


def test_mutual_break_both_pay_in_full_and_each_is_paid():
    g = world(4)
    p1, p2 = g.player("p1"), g.player("p2")
    p1.bank, p2.bank = 500, 200
    deal_sign(g, "p1", "p2", give={"bond": 50}, get={"bond": 20})
    p1.resources["influence"] = p2.resources["influence"] = 100
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}],
                      "p2": [{"type": "break_treaty", "with": "p1"}]})
    bb = {e["by"]: e for e in events_of(ev, "treaty_broken")}
    assert set(bb) == {"p1", "p2"} and p1.betrayals == p2.betrayals == 1
    assert bb["p1"]["paid"] == 50 + 50 and bb["p2"]["paid"] == 20 + 20
    assert not failures(ev)


def test_cooldown_on_both_paths_and_notice():
    g = world(4)
    order_sign(g, "p1", "p2")
    g.player("p1").resources["influence"] = 100
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    bt = events_of(ev, "treaty_broken")[0]["turn"]
    until = bt + C.TREATY_RESIGN_COOLDOWN
    assert g.player_view("p3")["treaty_cooldowns"] == [{"a": "p1", "b": "p2", "until_turn": until}]
    msg = f"p2 and p1 cannot sign a treaty before turn {until} (broken on turn {bt})"
    r = g.diplomacy("p2", [{"type": "propose", "to": "p1", "peace": 20}])[0]
    assert not r["ok"] and r["error"] == msg
    ev = run_turn(g, {"p2": [{"type": "propose_treaty", "to": "p1", "turns": 20}]})
    assert failures(ev) == [msg]
    assert ("p1", "p2") in g._move_restricted              # notice turn (bt + 1)
    run_turn(g)
    assert ("p1", "p2") not in g._move_restricted
    while g.turn < until:
        run_turn(g)
    assert g.player_view("p3")["treaty_cooldowns"] == []
    r = g.diplomacy("p2", [{"type": "propose", "to": "p1", "peace": 20}])[0]
    assert not r["ok"] and r["error"] == "p1's bond (50) exceeds its unpledged bank (0)"
    g.player("p1").bank = C.TREATY_BOND_PER_BETRAYAL
    deal_sign(g, "p2", "p1")                                 # the cooldown is over; p1 pledges 50
    assert g.treaty_terms[("p1", "p2")]["bond"] == {"p1": C.TREATY_BOND_PER_BETRAYAL, "p2": 0}


def test_a_break_ends_the_influence_streak_and_blocks_it_that_turn():
    g = world(4)
    p1 = g.player("p1")
    order_sign(g, "p1", "p2")
    p1.legacy = 100000
    run_turn(g)
    assert p1.influence_streak == 1
    p1.resources["influence"] = 100
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    ended = [e for e in events_of(ev, "streak_ended") if e["player"] == "p1"]
    assert ended == [{"turn": ended[0]["turn"], "type": "streak_ended", "player": "p1",
                      "condition": "influence", "reason": "treaty_broken"}]
    assert p1.influence_streak == 0
    run_turn(g)
    assert p1.influence_streak == 1


def test_break_without_the_influence_fails_and_costs_nothing():
    g = world(4)
    order_sign(g, "p1", "p2")
    p1 = g.player("p1")
    p1.betrayals = 1
    p1.bank, p1.legacy = 100, 100
    order_sign(g, "p1", "p3", bond=0)  # needs 50 bond: covered by the bank
    p1.resources["influence"] = 2 * C.TREATY_BREAK_COST - 1
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    assert failures(ev) == [f"breaking this treaty costs {2 * C.TREATY_BREAK_COST} influence"]
    assert g.treaty("p1", "p2") and p1.betrayals == 1 and p1.legacy >= 100


# ------------------------------------------------------------------ fog and rules
def test_fog_redacts_the_deal_value_of_a_break():
    g = world(4)
    g.config.fog = True
    p1 = g.player("p1")
    p1.bank = 400
    g.player("p2").resources["gold"] = 500
    deal_sign(g, "p2", "p1", give={"gold": 100}, get={"bond": 50})
    p1.resources["influence"] = 100
    run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    seen = {pid: next(e for e in g.player_view(pid)["events"] if e["type"] == "treaty_broken")
            for pid in ("p1", "p2", "p3")}
    for k in ("refund", "paid", "bank_fee", "debt", "cancelled"):
        assert k in seen["p1"] and k in seen["p2"] and k not in seen["p3"]
    # documented (RULES §14): the public bank shows the bank payment, so when the bank
    # covers what is owed a third party can work out `paid` and `refund`
    b = seen["p1"]
    before = next(e for e in g.player_view("p3")["events"] if e["type"] == "treaty_broken")
    p3_bank = next(r for r in g.player_view("p3")["players"] if r["id"] == "p1")["bank"]
    assert p1.resources["gold"] >= 0 and 400 - p3_bank == b["paid"]
    assert b["paid"] - before["bank_share"] - before["bond"] == b["refund"]
    assert "bank payment of a treaty break" in rules_md()
    for k in ("cost", "legacy_lost", "bank_share", "bond", "betrayals"):
        assert seen["p3"][k] == seen["p1"][k]
    assert "break_preview" in g.player_view("p1")["you"]["treaty"]
    assert g.player_view("p3")["you"]["treaty"]["break_preview"] == {}


def test_rules_json_treaty_keys():
    r = rules_json()["diplomacy"]
    assert (r["treaty_min_turns"], r["treaty_max_turns"], r["treaty_break_cost"]) == (20, 40, 50)
    assert r["treaty_slot_divisor"] == C.TREATY_SLOT_DIVISOR == 2
    assert r["treaty_break_pct"] == [10, 40]
    assert r["treaty_bond_per_betrayal"] == 50
    assert r["treaty_resign_cooldown"] == 15 and r["treaty_break_notice"] == 1
    for knob in ("TREATY_SIGN_COST", "TREATY_SIGN_BASE", "TREATY_BOND_BASE", "TREATY_BREAK_BANK_SHARE"):
        assert not hasattr(C, knob)


def test_view_treaty_problem_matches_the_engine():
    rng = random.Random(4)
    g = world(5)
    for p in g.players:
        p.bank = rng.choice([0, 40, 120])
        p.betrayals = rng.choice([0, 0, 1, 2])
    pids = [p.id for p in g.players]
    for _ in range(40):
        a, b = rng.sample(pids, 2)
        bonds = {a: rng.choice([None, 0, 20, 70]), b: rng.choice([None, 0, 30])}
        v = g.player_view("p1")
        assert D.view_treaty_problem(v, a, b, bonds) == g.treaty_sign_problem(a, b, bonds)
        if not g.treaty_sign_problem(a, b, bonds) and rng.random() < 0.5:
            g.sign_treaty(a, b, 20, bonds=bonds)
        elif g.treaty(a, b) and rng.random() < 0.3:
            del g.treaties[g._pair(a, b)]
            g.treaty_terms.pop(g._pair(a, b), None)
            g.broken_pairs[g._pair(a, b)] = g.turn
