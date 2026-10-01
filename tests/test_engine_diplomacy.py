"""Treaties, trades and messages."""
from agentciv.engine import constants as C
from agentciv.engine.testing import events_of, run_turn, sandbox


def world():
    g = sandbox(3)
    g.add_city(2, 2, "p1", capital=True)
    g.add_city(12, 2, "p2", capital=True)
    g.add_city(2, 12, "p3", capital=True)
    return g


def sign(g, a="p1", b="p2", turns=C.TREATY_MIN_TURNS):
    run_turn(g, {a: [{"type": "propose_treaty", "to": b, "turns": turns}]})
    ev = run_turn(g, {b: [{"type": "accept_treaty", "from": a}]})
    return ev


# ---------------------------------------------------------------- treaties
def test_treaty_proposal_visibility_and_accept_next_turn_only():
    g = world()
    run_turn(g, {"p1": [{"type": "propose_treaty", "to": "p2", "turns": 20}]})
    assert g.player_view("p2")["treaty_proposals"] == [{"from": "p1", "to": "p2", "turns": 20, "turn": 0}]
    assert g.player_view("p3")["treaty_proposals"] == []
    assert g.spectator_view()["treaty_proposals"] == []          # private while running
    assert len(g.spectator_view(full=True)["treaty_proposals"]) == 1
    run_turn(g)             # p2 lets it lapse
    errs = g.submit_orders("p2", [{"type": "accept_treaty", "from": "p1"}])
    assert errs and "no treaty proposal" in errs[0]["error"]


def test_treaty_signed_and_expires():
    g = world()
    k = C.TREATY_MIN_TURNS
    ev = sign(g, turns=k)
    signed = events_of(ev, "treaty_signed")[0]
    assert signed["until_turn"] == 1 + k and signed["bond"] == {"p1": 0, "p2": 0}
    assert g.treaty("p1", "p2") and not g.hostile("p1", "p2")
    assert g.spectator_view()["treaties"] == [{"a": "p1", "b": "p2", "until_turn": 1 + k, "signed_turn": 1,
                                               "bond": {"p1": 0, "p2": 0}}]
    while g.turn <= 1 + k:
        assert g.treaty("p1", "p2")
        ev = run_turn(g)
    assert not g.treaty("p1", "p2")
    assert events_of(ev, "treaty_expired")[0]["released"] == {"p1": 0, "p2": 0}
    assert g.treaty_terms == {}


def test_treaty_blocks_movement_and_combat():
    g = world()
    sign(g)
    g.set_owner(7, 2, "p2")
    g.place_units(6, 2, "p1", {"infantry": 3})
    g.place_units(6, 4, "p2", {"infantry": 1})
    errs = g.submit_orders("p1", [{"type": "move", "from": [6, 2], "to": [7, 2]}])
    assert "treaty partner" in errs[0]["error"]
    errs = g.submit_orders("p1", [{"type": "move", "from": [6, 2], "to": [6, 3]}])
    assert errs == []
    # both move onto the same neutral tile: they coexist, no battle
    ev = run_turn(g, {"p1": [{"type": "move", "from": [6, 2], "to": [6, 3]}],
                      "p2": [{"type": "move", "from": [6, 4], "to": [6, 3]}]})
    assert not events_of(ev, "battle")
    assert g.armies[g.idx(6, 3)] == {"p1": {"infantry": 3}, "p2": {"infantry": 1}}
    # moving onto a partner's army is not allowed
    g.place_units(8, 8, "p1", {"infantry": 1})
    g.place_units(9, 8, "p2", {"infantry": 1})
    assert g.submit_orders("p1", [{"type": "move", "from": [8, 8], "to": [9, 8]}])


def test_break_treaty_costs_influence_and_restrictions_lift_two_turns_later():
    g = world()
    sign(g)
    p1 = g.player("p1")
    p1.resources["influence"] = 60
    g.set_owner(7, 2, "p2")
    g.place_units(6, 2, "p1", {"infantry": 3})
    # break + move in the same turn: the move is still blocked this turn
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    assert events_of(ev, "treaty_broken")[0] == {
        "turn": 2, "type": "treaty_broken", "by": "p1", "with": "p2", "cost": C.TREATY_BREAK_COST,
        "legacy_lost": 0, "bank_share": 0, "bond": 0, "refund": 0, "paid": 0, "removed": 0, "bank_fee": 0, "debt": 0,
        "free": False,
            "cancelled": [],
        "betrayals": 1}
    assert p1.betrayals == 1 and not g.treaty("p1", "p2")
    assert p1.resources["influence"] == 60 - C.TREATY_BREAK_COST + g.stats()["p1"]["income"]["influence"]
    assert g.player_view("p3")["players"][0]["betrayals"] == 1
    # the turn after the break the pair is still movement-restricted (notice)
    errs = g.submit_orders("p1", [{"type": "move", "from": [6, 2], "to": [7, 2]}])
    assert errs and "restrictions last through turn 3" in errs[0]["error"]
    run_turn(g)
    assert g.owner[g.idx(7, 2)] == "p2"
    ev = run_turn(g, {"p1": [{"type": "move", "from": [6, 2], "to": [7, 2]}]})
    assert g.owner[g.idx(7, 2)] == "p1"


def test_break_treaty_same_turn_move_blocked():
    g = world()
    sign(g)
    g.player("p1").resources["influence"] = 100
    g.set_owner(7, 2, "p2")
    g.place_units(6, 2, "p1", {"infantry": 3})
    # the move was pre-validated while the treaty existed -> rejected at submit time
    errs = g.submit_orders("p1", [{"type": "break_treaty", "with": "p2"},
                                  {"type": "move", "from": [6, 2], "to": [7, 2]}])
    assert [e["index"] for e in errs] == [1]


def test_break_treaty_without_influence_fails():
    g = world()
    sign(g)
    g.player("p1").resources["influence"] = 10
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    assert g.treaty("p1", "p2")
    assert "influence" in events_of(ev, "order_failed")[0]["reason"]


# ---------------------------------------------------------------- trades
# offer_trade / accept_trade are aliases of the deal actions propose / accept
# (DESIGN §13); the legacy ``trade_offers`` view field lists open
# resource-only deals.
def test_trade_offer_accept_and_privacy():
    g = world()
    ev = run_turn(g, {"p1": [{"type": "offer_trade", "to": "p2", "give": {"wood": 30}, "want": {"gold": 20}}]})
    offers = g.player_view("p2")["trade_offers"]
    assert len(offers) == 1 and offers[0]["id"] == "d1"
    assert offers[0]["expires_turn"] == 0 + C.DEAL_DEFAULT_EXPIRES_IN
    assert g.player_view("p2")["deals"]["open"][0]["get"] == {"gold": 20}
    assert g.player_view("p3")["trade_offers"] == []
    assert g.player_view("p3")["deals"]["open"] == []
    assert not any(e["type"] == "deal_proposed" for e in g.player_view("p3")["events"])
    assert events_of(ev, "deal_proposed")
    p1, p2 = g.player("p1"), g.player("p2")
    w1, g1, w2, g2 = p1.resources["wood"], p1.resources["gold"], p2.resources["wood"], p2.resources["gold"]
    ev = run_turn(g, {"p2": [{"type": "accept_trade", "offer_id": "d1"}]})
    st = g.stats()
    assert p1.resources["wood"] == w1 - 30 + st["p1"]["income"]["wood"]
    assert p1.resources["gold"] == g1 + 20 + st["p1"]["income"]["gold"]
    assert p2.resources["wood"] == w2 + 30 + st["p2"]["income"]["wood"]
    assert p2.resources["gold"] == g2 - 20 + st["p2"]["income"]["gold"]
    assert events_of(ev, "deal_executed")
    assert g.player_view("p2")["trade_offers"] == []


def test_trade_needs_both_to_pay_and_expires():
    g = world()
    run_turn(g, {"p1": [{"type": "offer_trade", "to": "p2", "give": {"wood": 30}, "want": {"gold": 20}}]})
    g.player("p1").resources["wood"] = 0
    ev = run_turn(g, {"p2": [{"type": "accept_trade", "offer_id": "d1"}]})
    assert "p1 is short of" in events_of(ev, "order_failed")[0]["reason"]
    assert events_of(ev, "deal_failed")
    assert g.player_view("p2")["trade_offers"] == []          # a failed accept closes the deal
    assert g.player_view("p2")["deals"]["recent"][0]["status"] == "failed"
    # the receiver must be able to pay too
    run_turn(g, {"p1": [{"type": "offer_trade", "to": "p2", "give": {"wood": 1}, "want": {"gold": 20}}]})
    g.player("p2").resources["gold"] = 0
    ev = run_turn(g, {"p2": [{"type": "accept_trade", "offer_id": "d2"}]})
    assert "p2 is short of 20 gold" in events_of(ev, "order_failed")[0]["reason"]
    # expiry: made on turn 4, open through turn 4 + DEAL_DEFAULT_EXPIRES_IN
    run_turn(g, {"p1": [{"type": "offer_trade", "to": "p2", "give": {"wood": 1}}]})
    assert g.turn == 5 and g.player_view("p2")["trade_offers"][0]["expires_turn"] == 4 + C.DEAL_DEFAULT_EXPIRES_IN
    run_turn(g)
    ev = run_turn(g)
    assert events_of(ev, "deal_expired")
    assert g.turn == 7 and g.player_view("p2")["trade_offers"] == []
    assert g.submit_orders("p2", [{"type": "accept_trade", "offer_id": "d3"}])


def test_accept_trade_not_addressed_to_you():
    g = world()
    run_turn(g, {"p1": [{"type": "offer_trade", "to": "p2", "give": {"wood": 1}}]})
    errs = g.submit_orders("p3", [{"type": "accept_trade", "offer_id": "d1"}, {"type": "accept_trade", "offer_id": "d2"}])
    # identical errors: probing must not reveal which private offers exist
    assert errs[0]["error"].replace("d1", "dX") == errs[1]["error"].replace("d2", "dX")
    assert "no open deal" in errs[0]["error"]


# ---------------------------------------------------------------- messages
def test_message_visibility():
    g = world()
    run_turn(g, {"p1": [{"type": "message", "to": "p2", "text": "secret"},
                        {"type": "message", "to": "all", "text": "hello all"}]})
    texts = lambda v: [m["text"] for m in v["messages"]]
    assert texts(g.player_view("p1")) == ["secret", "hello all"]
    assert texts(g.player_view("p2")) == ["secret", "hello all"]
    assert texts(g.player_view("p3")) == ["hello all"]
    assert texts(g.spectator_view()) == ["hello all"]      # the public view: no private messages
    assert texts(g.spectator_view(full=True)) == ["secret", "hello all"]
    assert g.spectator_view(full=True)["messages"][0] == {"turn": 0, "from": "p1", "to": "p2", "text": "secret"}


def test_messages_in_view_are_bounded():
    g = world()
    for t in range(15):
        run_turn(g, {"p1": [{"type": "message", "to": "all", "text": f"m{t}-{k}"} for k in range(5)]})
    msgs = g.player_view("p2")["messages"]
    assert len(msgs) == C.MESSAGES_IN_VIEW and msgs[-1]["text"] == "m14-4"


def test_spectator_view_hides_private_diplomacy_until_the_game_ends():
    """Regression: the token-less spectator view must not leak private
    messages, trade offers, treaty proposals or private events of a running
    game (DESIGN §1/§10)."""
    g = world()
    g.max_turns = 2
    run_turn(g, {"p1": [{"type": "message", "to": "p2", "text": "SECRET plan"},
                        {"type": "offer_trade", "to": "p2", "give": {"wood": 1}},
                        {"type": "propose_treaty", "to": "p2", "turns": 20}]})
    v = g.spectator_view()
    assert v["messages"] == [] and v["trade_offers"] == [] and v["treaty_proposals"] == []
    assert v["deals"]["open"] == [] and v["deals"]["recent"] == []
    assert not {e["type"] for e in v["events"]} & {"deal_proposed", "say", "treaty_proposed", "order_failed"}
    assert "SECRET" not in str(v)
    full = g.spectator_view(full=True)
    assert full["messages"] and full["trade_offers"] and full["treaty_proposals"]
    assert full["deals"]["open"]
    assert {"deal_proposed", "treaty_proposed", "say"} <= {e["type"] for e in full["events"]}
    run_turn(g)
    assert g.finished
    assert [m["text"] for m in g.spectator_view()["messages"]] == ["SECRET plan"]  # revealed afterwards


def test_simultaneous_break_charges_both_partners():
    """Regression: the lower seat used to pay while the partner's break failed
    for free ('no treaty')."""
    g = world()
    sign(g)
    for p in g.players:
        p.resources["influence"] = 100
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}],
                      "p2": [{"type": "break_treaty", "with": "p1"}]})
    assert not g.treaty("p1", "p2")
    p1, p2 = g.player("p1"), g.player("p2")
    assert p1.betrayals == p2.betrayals == 1
    inc = g.stats()
    assert p1.resources["influence"] - inc["p1"]["income"]["influence"] == 100 - C.TREATY_BREAK_COST
    assert p2.resources["influence"] - inc["p2"]["income"]["influence"] == 100 - C.TREATY_BREAK_COST
    assert len(events_of(ev, "treaty_broken")) == 2 and not events_of(ev, "order_failed")
