"""Economic/influence streak rules and the free treaty break (docs/RULES.md
§5, §8, §9, §11): the streak deposit, city loss and breaks against a
player on a victory streak."""
from agentciv.engine import constants as C
from agentciv.engine.rules import bank_limit, streak_deposit
from agentciv.engine.testing import events_of, run_turn, sandbox

SPOTS = [(2, 2), (12, 2), (2, 12), (12, 12)]


def world(n=3, fog=False):
    g = sandbox(n, fog=fog)
    g.relics, g.relic_set = [], frozenset()
    for k in range(n):
        g.add_city(*SPOTS[k], f"p{k + 1}", capital=True)
    return g


def bank(gold):
    return [{"type": "bank", "gold": gold}]


def deposit(g, pid="p1", extra=0):
    need = streak_deposit(g.bank_limit(pid)) + extra
    g.player(pid).resources["gold"] += need
    return bank(need)


def streaks(p):
    return p.economic_streak, p.influence_streak


# ---------------------------------------------------------------- streak deposit
def test_streak_deposit_is_half_the_allowance_rounded_up():
    assert [streak_deposit(n) for n in (0, 1, 50, 60, 75)] == [0, 1, 25, 30, 38]
    assert streak_deposit(bank_limit(1, 0)) == -(-C.BANK_BASE // C.STREAK_DEPOSIT_DIVISOR)


def test_a_turn_without_the_deposit_pauses_the_economic_streak():
    g = world()
    p1 = g.player("p1")
    p1.bank = C.BANK_VICTORY
    run_turn(g, {"p1": deposit(g)})
    run_turn(g, {"p1": deposit(g)})
    assert p1.economic_streak == 2
    need = streak_deposit(g.bank_limit("p1"))
    p1.resources["gold"] += need
    ev = run_turn(g, {"p1": bank(need - 1)})
    assert p1.economic_streak == 2 and not events_of(ev, "streak_ended")
    assert events_of(ev, "streak_paused") == [{"turn": 2, "type": "streak_paused", "player": "p1",
                                              "condition": "economic", "reason": "deposit",
                                              "banked": need - 1, "needed": need}]
    ev = run_turn(g)                                    # nothing banked at all
    assert p1.economic_streak == 2 and events_of(ev, "streak_paused")[0]["banked"] == 0
    ev = run_turn(g, {"p1": deposit(g)})
    assert p1.economic_streak == 3 and not events_of(ev, "streak_paused")


def test_no_deposit_no_new_streak_and_no_pause_event():
    g = world()
    p1 = g.player("p1")
    p1.bank = C.BANK_VICTORY
    ev = run_turn(g)
    assert p1.economic_streak == 0
    assert not events_of(ev, "streak_paused") and not events_of(ev, "streak_started")


def test_the_deposit_needed_uses_the_allowance_at_the_start_of_the_actions_phase():
    g = world()
    p1 = g.player("p1")
    p1.bank = C.BANK_VICTORY
    p1.resources.update(gold=500, wood=500, stone=500)
    need = streak_deposit(g.bank_limit("p1"))
    assert g.player_view("p1")["you"]["streak_deposit"] == need == 25
    ev = run_turn(g, {"p1": [{"type": "build", "at": [2, 2], "building": "market_hall"}] + bank(need)})
    assert g.bank_limit("p1") == C.BANK_BASE + C.BANK_PER_MARKET_HALL       # the hall raised the allowance
    assert p1.economic_streak == 1 and not events_of(ev, "streak_paused")
    assert g.player_view("p1")["you"]["streak_deposit"] == 30               # from next turn on


def test_streak_paused_is_public_in_fog_games():
    g = world(fog=True)
    p1 = g.player("p1")
    p1.bank, p1.economic_streak = C.BANK_VICTORY, 4
    run_turn(g)
    for view in (g.player_view("p2"), g.player_view("p3"), g.spectator_view()):
        assert [e for e in view["events"] if e["type"] == "streak_paused"][0]["needed"] == 25


# ---------------------------------------------------------------- city loss
def two_city_leader(g):
    p1 = g.player("p1")
    g.add_city(6, 2, "p1")
    p1.bank, p1.legacy = C.BANK_VICTORY + 500, C.LEGACY_VICTORY + 500
    for _ in range(3):
        run_turn(g, {"p1": deposit(g)})
    assert streaks(p1) == (3, 3)
    return p1


def test_losing_any_city_ends_both_streaks():
    g = world()
    p1 = two_city_leader(g)
    g.place_units(7, 2, "p2", {"infantry": 8})
    ev = run_turn(g, {"p1": deposit(g), "p2": [{"type": "move", "from": [7, 2], "to": [6, 2]}]})
    assert events_of(ev, "city_captured")[0]["to"] == "p2"
    assert p1.alive and streaks(p1) == (0, 0)
    ended = [(e["condition"], e.get("reason")) for e in events_of(ev, "streak_ended") if e["player"] == "p1"]
    assert sorted(ended) == [("economic", "city_lost"), ("influence", "city_lost")]
    assert p1.bank >= C.BANK_VICTORY and p1.legacy >= C.LEGACY_VICTORY       # nothing seized for a plain city
    ev = run_turn(g, {"p1": deposit(g)})                # the next turn end counts again
    assert streaks(p1) == (1, 1)
    assert len(events_of(ev, "streak_started")) == 2


def test_losing_a_city_while_taking_one_still_ends_the_streaks():
    g = world()
    p1 = two_city_leader(g)
    g.add_city(9, 6, "p2")
    g.place_units(7, 2, "p2", {"infantry": 8})
    g.place_units(9, 7, "p1", {"infantry": 8})
    ev = run_turn(g, {"p1": deposit(g) + [{"type": "move", "from": [9, 7], "to": [9, 6]}],
                      "p2": [{"type": "move", "from": [7, 2], "to": [6, 2]}]})
    caps = {(e["from"], e["to"]) for e in events_of(ev, "city_captured")}
    assert caps == {("p1", "p2"), ("p2", "p1")}
    assert g.player("p1").tiles > 0 and streaks(p1) == (0, 0)


def test_losing_the_original_capital_ends_the_streaks_as_a_city_loss():
    g = world()
    p1 = two_city_leader(g)
    g.place_units(3, 2, "p2", {"infantry": 8})
    ev = run_turn(g, {"p1": deposit(g), "p2": [{"type": "move", "from": [3, 2], "to": [2, 2]}]})
    cap = events_of(ev, "city_captured")[0]
    assert cap["plunder"]["bank"] > 0 and cap["legacy_lost"] > 0
    ended = {e["condition"]: e.get("reason") for e in events_of(ev, "streak_ended") if e["player"] == "p1"}
    assert ended == {"economic": "city_lost", "influence": "city_lost"} and streaks(p1) == (0, 0)


# ---------------------------------------------------------------- free break
def treaty(g, a, b, bond=None):
    p = {"type": "propose_treaty", "to": b, "turns": C.TREATY_MIN_TURNS}
    if bond is not None:
        p["bond"] = bond
    run_turn(g, {a: [p]})
    run_turn(g, {b: [{"type": "accept_treaty", "from": a}]})


def test_breaking_with_a_streaking_partner_is_free():
    g = world()
    p1, p2 = g.player("p1"), g.player("p2")
    p1.bank = 300
    treaty(g, "p1", "p2", bond=100)
    p1.legacy = C.LEGACY_VICTORY + 100              # p1 is on its own influence streak
    run_turn(g)
    assert p1.influence_streak == 1
    p2.bank, p2.economic_streak = C.BANK_VICTORY, 3   # p2 shows an economic streak at the start of turn T
    p1.resources["influence"] = 0                     # a free break needs no influence
    preview = g.player_view("p1")["you"]["treaty"]["break_preview"]["p2"]
    assert preview == {"influence": 0, "legacy": 0, "free": True, "gold_to_partner": 0, "gold_removed": 0,
                       "bank_fee": 0, "influence_debt": 0, "cancels": []}
    legacy, gold2 = p1.legacy, p2.resources["gold"]
    t = g.turn
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    b = events_of(ev, "treaty_broken")[0]
    assert b["free"] is True and (b["cost"], b["legacy_lost"], b["bank_share"], b["bond"]) == (0, 0, 0, 0)
    assert (b["paid"], b["removed"], b["betrayals"]) == (0, 0, 0)
    assert p1.betrayals == 0 and p1.bank == 300 and not g.treaty("p1", "p2")
    assert p1.legacy == legacy + g.stats()["p1"]["income"]["influence"]
    assert p1.influence_streak == 2 and not [e for e in events_of(ev, "streak_ended") if e["player"] == "p1"]
    assert p2.resources["gold"] == gold2 + g.stats()["p2"]["income"]["gold"]
    cd = g.player_view("p3")["treaty_cooldowns"]
    assert cd == [{"a": "p1", "b": "p2", "until_turn": t + C.TREATY_RESIGN_COOLDOWN, "notice_until": t}]
    # turn T+1: p1 may already move onto p2's land
    g.set_owner(7, 2, "p2")
    g.place_units(6, 2, "p1", {"infantry": 2})
    ev = run_turn(g, {"p1": [{"type": "move", "from": [6, 2], "to": [7, 2]}]})
    assert events_of(ev, "tile_captured")[0]["to"] == "p1"


def test_a_free_break_still_pays_the_deal_refunds():
    g = world()
    p1, p2 = g.player("p1"), g.player("p2")
    p1.resources["gold"] = 0
    p2.resources["gold"] = 500
    r = g.diplomacy("p2", [{"type": "propose", "to": "p1", "give": {"gold": 200}, "get": {}, "peace": 20}])[0]
    assert g.diplomacy("p1", [{"type": "accept", "deal": r["deal"]}])[0]["ok"]
    p2.legacy, p2.influence_streak = C.LEGACY_VICTORY, 1
    gold2 = p2.resources["gold"]
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    b = events_of(ev, "treaty_broken")[0]
    assert b["free"] and b["refund"] == 200 and b["paid"] == 200 and b["removed"] == 0 and b["cost"] == 0
    assert p2.resources["gold"] == gold2 + 200 + g.stats()["p2"]["income"]["gold"]


def test_breaking_with_a_partner_not_on_a_streak_is_not_free():
    g = world()
    p1, p2 = g.player("p1"), g.player("p2")
    p1.bank = 1000
    treaty(g, "p1", "p2", bond=100)
    p2.bank = C.BANK_VICTORY                         # at the target but no streak shown yet
    p1.resources["influence"] = 100
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    b = events_of(ev, "treaty_broken")[0]
    assert not b["free"] and b["cost"] == C.TREATY_BREAK_COST and p1.betrayals == 1
    assert b["removed"] == 100 + 1000 * C.TREATY_BREAK_PCT // 100 and b["paid"] == 0
    assert p2.resources["gold"] < C.BANK_VICTORY                # nothing of it went to p2's gold
    assert g.player_view("p3")["treaty_cooldowns"][0]["notice_until"] == b["turn"] + C.TREATY_BREAK_NOTICE


def test_mutual_break_with_one_side_streaking():
    g = world()
    p1, p2 = g.player("p1"), g.player("p2")
    p1.bank = p2.bank = 500
    treaty(g, "p1", "p2", bond=50)
    p2.economic_streak = 2                           # p1's break is free, p2's is not
    p1.resources["influence"] = p2.resources["influence"] = 100
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}],
                      "p2": [{"type": "break_treaty", "with": "p1"}]})
    bb = {e["by"]: e for e in events_of(ev, "treaty_broken")}
    assert bb["p1"]["free"] and bb["p1"]["removed"] == 0 and p1.betrayals == 0
    assert not bb["p2"]["free"] and bb["p2"]["removed"] == 500 * C.TREATY_BREAK_PCT // 100 and p2.betrayals == 1
    assert g.player_view("p3")["treaty_cooldowns"][0]["notice_until"] == bb["p1"]["turn"] + C.TREATY_BREAK_NOTICE


def test_full_summary_lists_a_rivals_streak_pause():
    """summarize_view (play_cli state/next, MCP get_state) shows streak_paused
    among the notable events, for the player and for rivals (retune review)."""
    from agentciv.client import summarize_view
    g = world()
    p1 = g.player("p1")
    p1.bank = C.BANK_VICTORY
    run_turn(g, {"p1": deposit(g)})
    run_turn(g)                                         # p1 banks nothing: paused
    for viewer in ("p1", "p2"):
        text = summarize_view(g.player_view(viewer))
        assert "streak_paused" in text.split("Notable events last turn:")[1], viewer


def test_peace_deal_notes_for_a_streak_holder_handing_something_over():
    """Client notes (play_cli deal, MCP deal tools): a streak holder who buys
    peace is told the partner can break it for free and what comes back."""
    from agentciv.client import peace_deal_notes
    g = world()
    p1 = g.player("p1")
    p1.resources["gold"] += 500
    offer = {"type": "propose", "to": "p2", "give": {"gold": 400}, "get": {}, "peace": 20}
    assert peace_deal_notes(g.player_view("p1"), [offer]) == []          # no streak yet
    p1.bank = C.BANK_VICTORY
    run_turn(g, {"p1": deposit(g)})
    assert p1.economic_streak == 1
    v = g.player_view("p1")
    notes = peace_deal_notes(v, [offer])
    assert len(notes) == 1 and "p2 can break this treaty for free" in notes[0] and "400 gold" in notes[0]
    assert peace_deal_notes(v, [{**offer, "peace": None}]) == []           # no peace in the deal
    assert peace_deal_notes(v, [{**offer, "give": {}, "get": {"gold": 5}}]) == []   # nothing handed over
    notes = peace_deal_notes(v, [{**offer, "give": {"tiles": [[3, 2]]}}])
    assert len(notes) == 1 and "1 tile(s)" in notes[0]
    # accepting p2's peace offer that asks for wood and a contract
    d = g.diplomacy("p2", [{"type": "propose", "to": "p1", "give": {}, "peace": 20,
                            "get": {"wood": 10, "per_turn": {"gold": 5}, "turns": 10}}])[0]
    assert d["ok"], d
    notes = peace_deal_notes(g.player_view("p1"), [{"type": "accept", "deal": d["deal"]}])
    assert len(notes) == 1 and "10 wood" in notes[0] and "a contract" in notes[0]
    assert peace_deal_notes(g.player_view("p2"), [{"type": "accept", "deal": d["deal"]}]) == []
