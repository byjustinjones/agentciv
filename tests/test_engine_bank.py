"""Bank, legacy and the economic/influence streaks (docs/RULES.md §5, §8, §10, §11),
including what a contract default takes from the payer's bank."""
import pytest

from agentciv.engine import constants as C
from agentciv.engine import deals as D
from agentciv.engine.orders import prevalidate
from agentciv.engine.rules import bank_limit, streak_deposit
from agentciv.engine.testing import events_of, run_turn, sandbox


def world(n=3, fog=False):
    g = sandbox(n, fog=fog)
    g.relics, g.relic_set = [], frozenset()
    g.add_city(2, 2, "p1", capital=True)
    g.add_city(12, 2, "p2", capital=True)
    if n >= 3:
        g.add_city(2, 12, "p3", capital=True)
    return g


def bank_orders(*amounts):
    return [{"type": "bank", "gold": a} for a in amounts]


def deposit(g, pid="p1"):
    """Orders banking the gold an economic streak turn needs (§11)."""
    need = streak_deposit(g.bank_limit(pid))
    g.player(pid).resources["gold"] += need
    return {pid: bank_orders(need)}


def failures(ev, pid="p1"):
    return [e["reason"] for e in events_of(ev, "order_failed") if e["player"] == pid]


# ====================================================================== the bank order
def test_bank_moves_the_smallest_of_amount_gold_and_limit():
    g = world()
    p1 = g.player("p1")
    p1.resources["gold"] = 100
    assert g.bank_limit("p1") == bank_limit(1, 0) == C.BANK_BASE == 50
    assert g.player_view("p1")["you"]["bank_limit"] == 50
    assert g.player_view("p1")["you"]["streak_deposit"] == 25
    ev = run_turn(g, {"p1": bank_orders(60)})
    assert p1.bank == 50 and not failures(ev)
    gold_income = g.stats()["p1"]["income"]["gold"]           # the bank pays no interest
    assert p1.resources["gold"] == 100 - 50 + gold_income
    assert events_of(ev, "bank") == [{"turn": 0, "type": "bank", "player": "p1", "gold": 50, "bank": 50}]
    assert "bank" in [e["type"] for e in g.player_view("p1")["events"]]
    assert "bank" not in [e["type"] for e in g.player_view("p2")["events"]]        # private
    assert "bank" not in [e["type"] for e in g.spectator_view()["events"]]
    p1.resources["gold"] = 4                     # gold on hand is the limit now
    run_turn(g, {"p1": bank_orders(60)})
    assert p1.bank == 54 and p1.resources["gold"] == g.stats()["p1"]["income"]["gold"]


def test_bank_limit_counts_cities_and_halls_built_earlier_in_the_list_and_is_shared():
    g = world()
    p1 = g.player("p1")
    p1.resources.update(gold=500, food=500, wood=500, stone=500)
    g.set_owner(5, 2, "p1")
    orders = ([{"type": "build", "at": [2, 2], "building": "market_hall"},
               {"type": "settle", "at": [6, 2]}]
              + bank_orders(40, 40, 1))
    ev = run_turn(g, {"p1": orders})
    assert g.bank_limit("p1") == C.BANK_BASE + C.BANK_PER_MARKET_HALL == 60     # the second city adds nothing
    assert [e["gold"] for e in events_of(ev, "bank")] == [40, 20]
    assert p1.bank == 60 and failures(ev) == ["bank limit for this turn reached"]
    assert p1.banked == 0                          # the allowance is per turn
    run_turn(g, {"p1": bank_orders(60)})
    assert p1.bank == 120


def test_bank_fails_without_gold():
    g = world()
    g.player("p1").resources["gold"] = 0
    ev = run_turn(g, {"p1": bank_orders(5)})
    assert failures(ev) == ["no gold to bank"] and g.player("p1").bank == 0


@pytest.mark.parametrize("bad", [0, -3, 1.5, "x", True, None, [5]])
def test_bank_validation(bad):
    g = world()
    ok, errs = prevalidate(g, "p1", [{"type": "bank", "gold": bad}])
    assert ok == [] and len(errs) == 1


def test_bank_validation_accepts_integers_and_several_orders():
    g = world()
    ok, errs = prevalidate(g, "p1", [{"type": "bank", "gold": 5}, {"type": "bank", "gold": "7"},
                                     {"type": "bank", "gold": 3.0}])
    assert errs == [] and [o["gold"] for o in ok] == [5, 7, 3]


def test_bank_cannot_be_traded():
    g = world()
    g.player("p1").bank = 500
    for bundle in ({"bank": 10}, {"per_turn": {"bank": 1}, "turns": 3}):
        res = g.diplomacy("p1", [{"type": "propose", "to": "p2", "give": bundle, "get": {"wood": 1}}])
        assert not res[0]["ok"], res
    assert "bank" not in C.TRADABLE


def test_viability_pass_does_not_leak_bank_changes():
    """A contested claim triggers the replay of the whole action list."""
    g = world()
    p1, p2 = g.player("p1"), g.player("p2")
    p1.resources["gold"] = 100
    g.set_owner(7, 2, "p2")                        # (6, 2) is adjacent to both
    g.set_owner(5, 2, "p1")
    ev = run_turn(g, {"p1": bank_orders(10) + [{"type": "claim", "at": [6, 2]}],
                      "p2": [{"type": "claim", "at": [6, 2]}]})
    assert any(r.startswith("contested") for r in failures(ev))
    assert p1.bank == 10 and [e["gold"] for e in events_of(ev, "bank")] == [10]
    assert not [r for r in failures(ev) if "bank" in r]


# ====================================================================== interest, legacy
def test_the_bank_pays_no_interest():
    g = world()
    p1 = g.player("p1")
    base = g.stats()["p1"]["income"]["gold"]
    p1.bank = 2500
    g._invalidate()
    assert g.stats()["p1"]["income"]["gold"] == base
    gold = p1.resources["gold"]
    run_turn(g)
    assert p1.resources["gold"] == gold + base and p1.bank == 2500


def test_bank_allowance_needs_a_city_and_grows_only_with_market_halls():
    assert bank_limit(0, 0) == 0
    assert bank_limit(1, 0) == bank_limit(5, 0) == C.BANK_BASE
    assert bank_limit(3, 2) == C.BANK_BASE + 2 * C.BANK_PER_MARKET_HALL


def test_legacy_adds_influence_income_including_relics():
    g = sandbox(2)
    g.add_city(2, 2, "p1", capital=True)
    g.add_city(12, 12, "p2", capital=True)
    g._set_owner(g.relics[0], "p1")              # owned, unguarded
    g._invalidate()
    inc = g.stats()["p1"]["income"]["influence"]
    assert inc >= C.RELIC_INFLUENCE_UNGUARDED + C.CITY_YIELD["influence"]
    run_turn(g)
    run_turn(g)
    assert g.player("p1").legacy == 2 * inc
    assert g.spectator_view()["players"][0]["legacy"] == 2 * inc


def test_legacy_not_lowered_by_claims_or_default_debt_but_lowered_by_a_treaty_break():
    g = world()
    p1 = g.player("p1")
    p1.resources["influence"] = 200
    p1.legacy = 500
    inc = g.stats()["p1"]["income"]["influence"]
    g.treaties[("p1", "p2")] = 50
    res = g.diplomacy("p1", [{"type": "propose", "to": "p2", "give": {"per_turn": {"gold": 900}, "turns": 3}}])
    assert g.diplomacy("p2", [{"type": "accept", "deal": res[0]["deal"]}])[0]["ok"]
    ev = run_turn(g, {"p1": [{"type": "claim", "at": [4, 2]}, {"type": "break_treaty", "with": "p2"}]})
    assert events_of(ev, "claim") and events_of(ev, "contract_default")
    broken = events_of(ev, "treaty_broken")[0]
    # a break removes TREATY_BREAK_PCT % of the legacy (§9); claims and the default fine do not
    assert broken["legacy_lost"] == 500 * C.TREATY_BREAK_PCT // 100
    assert p1.influence_debt > 0 and p1.resources["influence"] == 0
    assert p1.legacy == 500 - broken["legacy_lost"] + inc
    run_turn(g)                                    # the debt eats this turn's influence, not the legacy
    assert p1.legacy == 500 - broken["legacy_lost"] + 2 * inc


# ====================================================================== capture
def capture_world():
    g = world()
    g.add_city(12, 7, "p2")                        # p2 survives losing its capital
    return g


def test_capital_capture_takes_half_the_bank_and_a_quarter_of_the_legacy():
    g = capture_world()
    p1, p2 = g.player("p1"), g.player("p2")
    p2.bank, p2.legacy = 1001, 401
    p2.resources["gold"] = 0
    gold = p1.resources["gold"]
    g.place_units(11, 2, "p1", {"infantry": 5})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [11, 2], "to": [12, 2]}]})
    ce = events_of(ev, "city_captured")[0]
    assert ce["plunder"]["bank"] == 500 and "gold" not in ce["plunder"] and ce["legacy_lost"] == 100
    assert p2.bank == 501 and p1.bank == 0
    assert p1.resources["gold"] == gold + 500 + g.stats()["p1"]["income"]["gold"]
    assert p2.legacy == 301 + g.stats()["p2"]["income"]["influence"]
    # the original owner takes it back: nothing is seized from the captor
    p1.bank, p1.legacy = 800, 600
    g.place_units(12, 3, "p2", {"infantry": 10})
    ev = run_turn(g, {"p2": [{"type": "move", "from": [12, 3], "to": [12, 2]}]})
    ce = events_of(ev, "city_captured")[0]
    assert ce["to"] == "p2" and ce["plunder"] == {} and "legacy_lost" not in ce
    assert p1.bank == 800 and p1.legacy == 600 + g.stats()["p1"]["income"]["influence"]


def test_capture_of_a_non_capital_takes_nothing_from_the_bank():
    g = capture_world()
    p2 = g.player("p2")
    p2.bank, p2.legacy = 1000, 400
    g.place_units(11, 7, "p1", {"infantry": 3})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [11, 7], "to": [12, 7]}]})
    ce = events_of(ev, "city_captured")[0]
    assert ce["plunder"] == {} and "legacy_lost" not in ce and p2.bank == 1000


# ====================================================================== contract defaults
def contract(g, per_turn, turns, payer="p1", payee="p2"):
    res = g.diplomacy(payer, [{"type": "propose", "to": payee, "give": {"per_turn": per_turn, "turns": turns}}])
    assert res[0]["ok"], res
    assert g.diplomacy(payee, [{"type": "accept", "deal": res[0]["deal"]}])[0]["ok"]


@pytest.mark.parametrize("bank,seized", [(500, 250), (120, 120), (0, 0)])
def test_default_takes_the_remaining_obligation_from_the_bank(bank, seized):
    g = world()
    p1, p2 = g.player("p1"), g.player("p2")
    contract(g, {"gold": 50}, 5)
    p1.resources["gold"] = 0
    p1.bank = bank
    gold2 = p2.resources["gold"]
    ev = run_turn(g)
    d = events_of(ev, "contract_default")[0]
    assert d["seized"] == seized == min(bank, 50 * 5)      # the missed instalment counts
    assert p1.bank == bank - seized
    assert p2.resources["gold"] == gold2 + seized + g.stats()["p2"]["income"]["gold"]
    assert p2.bank == 0                                     # paid as gold on hand
    assert p1.defaults == 1 and d["penalty"] == D.default_penalty(250) == 125


def test_obligation_value_uses_fixed_start_prices():
    assert D.REFERENCE_PRICES == {"food": 1.0, "wood": 1.5, "stone": 2.0}
    assert D.obligation_value({"gold": 2, "stone": 4}, 3) == 6 + 24
    assert D.obligation_value({"wood": 1}, 3) == 4                     # 4.5 rounded down
    assert D.obligation_value({"influence": 5}, 2) == 10               # no price: 1 gold per unit
    assert D.obligation_value({"food": 3}, 0) == 0
    assert D.obligation_value({"gold": 2, "stone": 4}, 3, {"stone": 2.49}) == 6 + 29   # explicit prices


def test_default_values_non_gold_resources_at_the_start_price():
    g = world()
    p1 = g.player("p1")
    contract(g, {"stone": 40, "gold": 2}, 3)
    p1.resources["stone"] = 0
    p1.bank = 5000
    ev = run_turn(g)
    d = events_of(ev, "contract_default")[0]
    assert d["seized"] == 6 + 120 * 2 == D.obligation_value({"stone": 40, "gold": 2}, 3)
    assert d["penalty"] == D.default_penalty(246) == 123
    assert p1.bank == 5000 - d["seized"]


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_same_turn_market_orders_do_not_move_the_seizure(side):
    """Review finding: the payee buying (or the payer selling) the owed
    resource in the defaulting turn must not change what is seized."""
    g = world()
    p1, p2 = g.player("p1"), g.player("p2")
    contract(g, {"stone": 50}, 30)
    p1.resources["stone"] = 0
    p1.bank = 6000
    cap = int(g.pools["stone"][0] * C.MARKET_MAX_ORDER_FRACTION)
    if side == "buy":
        p2.resources["gold"] = 5000
        orders = {"p2": [{"type": "market", "side": "buy", "resource": "stone", "qty": cap}]}
    else:
        p1.resources["stone"] = 49                   # still short of the instalment after selling
        orders = {"p1": [{"type": "market", "side": "sell", "resource": "stone", "qty": 49}]}
    ev = run_turn(g, orders)
    assert events_of(ev, "market")                   # the trade happened and moved the spot price
    assert g._prices()["stone"] != 2.0
    d = events_of(ev, "contract_default")[0]
    assert d["seized"] == 50 * 30 * 2 == 3000 and p1.bank == 3000
    assert d["penalty"] == 1500


def test_default_ends_the_economic_streak_but_not_the_legacy():
    g = world()
    p1 = g.player("p1")
    p1.bank = C.BANK_VICTORY + 100
    p1.legacy = 700
    run_turn(g, deposit(g))
    run_turn(g, deposit(g))
    assert p1.economic_streak == 2
    bank = p1.bank
    contract(g, {"gold": 60}, 5)
    p1.resources["gold"] = 0
    ev = run_turn(g)
    ended = events_of(ev, "streak_ended")
    assert ended == [{"turn": 2, "type": "streak_ended", "player": "p1", "condition": "economic",
                      "reason": "contract_default"}]
    assert p1.bank == bank - 300 < C.BANK_VICTORY
    assert p1.economic_streak == 0 and not events_of(ev, "streak_started")
    assert p1.legacy == 700 + 3 * g.stats()["p1"]["income"]["influence"]


def test_default_turn_does_not_count_toward_a_new_streak():
    g = world()
    p1 = g.player("p1")
    p1.bank = C.BANK_VICTORY + 1000
    p1.resources["influence"] = 500
    for _ in range(5):
        run_turn(g, deposit(g))
    assert p1.economic_streak == 5
    contract(g, {"gold": 100}, 2)                          # more than the gold income
    p1.resources["gold"] = 0
    ev = run_turn(g)
    assert p1.bank >= C.BANK_VICTORY                       # still above the target after the seizure
    assert [e["type"] for e in ev if e["type"].startswith("streak")] == ["streak_ended"]
    assert p1.economic_streak == 0
    assert g.player_view("p2")["players"][0]["economic_streak"] == 0
    ev = run_turn(g, deposit(g))                           # the next turn end counts again
    assert [e["type"] for e in ev if e["type"].startswith("streak")] == ["streak_started"]
    assert p1.economic_streak == 1


def test_elimination_ends_running_streaks():
    g = world()
    p2 = g.player("p2")
    p2.bank, p2.legacy = C.BANK_VICTORY + 400, C.LEGACY_VICTORY + 100
    for _ in range(4):
        run_turn(g, deposit(g, "p2"))
    assert p2.economic_streak == 4 and p2.influence_streak == 4
    g.place_units(11, 2, "p1", {"infantry": 5})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [11, 2], "to": [12, 2]}]})
    assert not p2.alive
    ended = [(e["condition"], e.get("reason")) for e in events_of(ev, "streak_ended") if e["player"] == "p2"]
    assert ended == [("economic", "eliminated"), ("influence", "eliminated")]
    types = [e["type"] for e in ev]
    assert types.index("streak_ended") < types.index("eliminated")
    row = g.spectator_view()["players"][1]
    assert (row["economic_streak"], row["influence_streak"]) == (0, 0) and "relic_streak" not in row


def test_seized_visibility():
    # standard game: the default and its amounts are public
    g = world()
    contract(g, {"gold": 40}, 5)
    g.player("p1").resources["gold"] = 0
    g.player("p1").bank = 90
    run_turn(g)
    for view in (g.player_view("p3"), g.spectator_view()):
        d = [e for e in view["events"] if e["type"] == "contract_default"][0]
        assert d["seized"] == 90
    # fog game: only the parties see the amounts
    g = world(fog=True)
    contract(g, {"gold": 40}, 5)
    g.player("p1").resources["gold"] = 0
    g.player("p1").bank = 90
    run_turn(g)
    for pid in ("p1", "p2"):
        d = [e for e in g.player_view(pid)["events"] if e["type"] == "contract_default"][0]
        assert d["seized"] == 90
    d3 = [e for e in g.player_view("p3")["events"] if e["type"] == "contract_default"][0]
    assert "seized" not in d3 and d3["payer"] == "p1"
    assert "seized" not in [e for e in g.spectator_view()["events"] if e["type"] == "contract_default"][0]
    assert [e for e in g.spectator_view(full=True)["events"] if e["type"] == "contract_default"][0]["seized"] == 90


# ====================================================================== views and fog
def test_rows_and_you_carry_the_new_fields():
    g = world()
    p1 = g.player("p1")
    p1.bank, p1.legacy, p1.economic_streak, p1.influence_streak = 70, 80, 0, 0
    g._invalidate()
    v = g.player_view("p2")
    r = v["players"][0]
    assert (r["bank"], r["legacy"], r["economic_streak"], r["influence_streak"]) == (70, 80, 0, 0)
    assert v["you"]["bank_limit"] == C.BANK_BASE and v["you"]["streak_deposit"] == -(-C.BANK_BASE // 2)
    assert g.stats()["p1"]["score"] - g.stats()["p2"]["score"] == 70 // C.SCORE_DIVISORS["gold"]


def test_fog_bank_event_private_streak_events_and_capture_plunder_scoped():
    g = world(fog=True)
    p1 = g.player("p1")
    p1.resources["gold"] = 50
    p1.legacy = C.LEGACY_VICTORY
    run_turn(g, {"p1": bank_orders(5)})

    def types(pid):
        return [e["type"] for e in g.player_view(pid)["events"]]
    assert "bank" in types("p1") and "bank" not in types("p2") and "bank" not in types("p3")
    for pid in ("p1", "p2", "p3"):
        assert "streak_started" in types(pid)
    assert "streak_started" in [e["type"] for e in g.spectator_view()["events"]]
    # capturing p2's capital: plunder (with the bank) only for the parties, legacy_lost public
    g.add_city(12, 7, "p2")
    g.player("p2").bank = 400
    g.place_units(11, 2, "p1", {"infantry": 5})
    run_turn(g, {"p1": [{"type": "move", "from": [11, 2], "to": [12, 2]}]})
    cap = {pid: [e for e in g.player_view(pid)["events"] if e["type"] == "city_captured"][0]
           for pid in ("p1", "p2", "p3")}
    assert cap["p1"]["plunder"]["bank"] == 200 == cap["p2"]["plunder"]["bank"]
    assert "plunder" not in cap["p3"] and cap["p3"]["legacy_lost"] == 0
    assert g.player_view("p3")["players"][1]["bank"] == 200             # the bank itself is public
