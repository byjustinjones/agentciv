"""Barter & deals (DESIGN §13): live negotiation, contracts, land, peace,
reputation, visibility, limits, determinism and fuzzing."""
import copy
import json
import random

import pytest

from agentciv.engine import Game, GameConfig, rules_json
from agentciv.engine import constants as C
from agentciv.engine import deals as D
from agentciv.engine.testing import events_of, run_turn, sandbox


def world():
    g = sandbox(3)
    g.add_city(2, 2, "p1", capital=True)
    g.add_city(12, 2, "p2", capital=True)
    g.add_city(2, 12, "p3", capital=True)
    return g


def dip(g, pid, *actions):
    return g.diplomacy(pid, list(actions))


def ok(g, pid, action):
    res = g.diplomacy(pid, [action])
    assert res[0]["ok"], res
    return res[0]


def err(g, pid, action):
    res = g.diplomacy(pid, [action])
    assert not res[0]["ok"], res
    return res[0]["error"]


def totals(g):
    return {r: sum(p.resources[r] for p in g.players) for r in C.TRADABLE}


def res(g, pid):
    return dict(g.player(pid).resources)


P12 = {"type": "propose", "to": "p2", "give": {"wood": 30}, "get": {"gold": 20}}


# ====================================================================== pure helpers
def test_parse_bundle_normalises():
    b = D.parse_bundle({"gold": "5", "food": 0, "wood": 3.0, "tiles": [[1, 2], {"x": 3, "y": 4}],
                        "per_turn": {"stone": 2, "gold": 0}, "turns": 4})
    assert b == {"wood": 3, "gold": 5, "tiles": [[1, 2], [3, 4]], "per_turn": {"stone": 2}, "turns": 4}
    assert list(b) == ["wood", "gold", "tiles", "per_turn", "turns"]
    assert D.parse_bundle(None) == {} and D.parse_bundle({"gold": 0, "tiles": []}) == {}
    # per_turn with only zeros: no contract, `turns` ignored
    assert D.parse_bundle({"per_turn": {"gold": 0}, "turns": 99}) == {}
    assert D.parse_bundle(D.parse_bundle({"gold": 5, "per_turn": {"food": 1}, "turns": 2})) == \
        {"gold": 5, "per_turn": {"food": 1}, "turns": 2}


@pytest.mark.parametrize("raw, fragment", [
    ({"influence": 5}, "influence is not tradable"),
    ({"per_turn": {"influence": 1}, "turns": 2}, "influence is not tradable"),
    ({"diamonds": 5}, "unknown key"),
    ({"gold": -1}, ">= 0"),
    ({"gold": C.DEAL_MAX_QTY + 1}, f"<= {C.DEAL_MAX_QTY}"),
    ({"gold": True}, "integer"),
    ({"gold": 1.5}, "integer"),
    ({"gold": "lots"}, "integer"),
    ({"tiles": [[0, 0]] * 2}, "twice"),
    ({"tiles": [[k, 0] for k in range(C.DEAL_MAX_TILES + 1)]}, f"at most {C.DEAL_MAX_TILES} tiles"),
    ({"tiles": [[-1, 0]]}, "off the map"),
    ({"tiles": "5,6"}, "list"),
    ({"tiles": [[1]]}, "[x, y]"),
    ({"per_turn": {"gold": 5}}, "needs 'turns'"),
    ({"per_turn": {"gold": 5}, "turns": 0}, "turns must be"),
    ({"per_turn": {"gold": 5}, "turns": C.DEAL_CONTRACT_MAX_TURNS + 1}, "turns must be"),
    ({"per_turn": [5], "turns": 3}, "must be an object"),
    ([1, 2], "must be an object"),
])
def test_parse_bundle_errors(raw, fragment):
    b, e = D.check_bundle(raw, 10, 10)
    assert b is None and fragment in e
    with pytest.raises(D.DealError):
        D.parse_bundle(raw, 10, 10)


def test_parse_action_aliases_and_canonical_form():
    a = D.parse_action({"type": "offer_trade", "to": "p2", "give": {"wood": 5}, "want": {"gold": 4}})
    assert a == {"type": "propose", "to": "p2", "give": {"wood": 5}, "get": {"gold": 4}, "peace": None,
                 "message": None, "expires_in": C.DEAL_DEFAULT_EXPIRES_IN}
    assert D.parse_action({"type": "accept_trade", "offer_id": "t7"}) == {"type": "accept", "deal": "d7"}
    assert D.parse_action({"type": "accept_trade", "offer_id": 7}) == {"type": "accept", "deal": "d7"}
    assert D.parse_action({"type": "accept", "deal": "3"}) == {"type": "accept", "deal": "d3"}
    assert D.parse_action({"type": "message", "text": "hi"}) == {"type": "say", "to": "all", "text": "hi"}
    c = D.parse_action({"type": "counter", "id": "d2", "get": {"gold": 1}, "peace": 20, "message": "m",
                        "expires_in": 5})
    assert c == {"type": "counter", "deal": "d2", "give": {}, "get": {"gold": 1}, "peace": 20, "message": "m",
                 "expires_in": 5}
    for raw in (a, c, {"type": "reject", "deal": "d1", "message": "no"}, {"type": "withdraw", "deal": "d1"},
                {"type": "say", "to": "p2", "text": "x"}):
        canon = D.parse_action(raw)
        assert D.parse_action(copy.deepcopy(canon)) == canon      # canonical form is a fixed point
    with pytest.raises(D.DealError, match="only resources"):
        D.parse_action({"type": "offer_trade", "to": "p2", "give": {"tiles": [[1, 1]]}})
    with pytest.raises(D.DealError, match="peace must be"):
        D.parse_action({"type": "propose", "to": "p2", "peace": 5})
    with pytest.raises(D.DealError, match="expires_in"):
        D.parse_action({"type": "propose", "to": "p2", "give": {"gold": 1}, "expires_in": 9})
    with pytest.raises(D.DealError, match="at least one term"):
        D.parse_action({"type": "propose", "to": "p2", "give": {"gold": 0}})
    with pytest.raises(D.DealError, match="unknown diplomacy action"):
        D.parse_action({"type": "bribe"})


def test_view_helpers_and_bundle_value():
    g = world()
    v = g.player_view("p1")
    p1 = g.player("p1")
    assert D.view_delivery_problem(v, "p1", {"gold": p1.resources["gold"]}) is None
    assert "short of 1 gold" in D.view_delivery_problem(v, "p1", {"gold": p1.resources["gold"] + 1})
    assert D.view_delivery_problem(v, "p1", {"tiles": [[3, 3]]}) is None
    assert "not owned by p1" in D.view_delivery_problem(v, "p1", {"tiles": [[12, 3]]})
    assert "is a city" in D.view_delivery_problem(v, "p1", {"tiles": [[2, 2]]})
    assert "not tradable" in D.view_delivery_problem(v, "p1", {"influence": 1})
    assert "unknown player" in D.view_delivery_problem(v, "p9", {"gold": 1})
    ok(g, "p1", P12)
    v = g.player_view("p2")
    deal = v["deals"]["open"][0]
    assert D.view_deal_problem(v, deal) is None and deal["deliverable"] is True and deal["problem"] is None
    assert D.bundle_value({"gold": 10, "wood": 2}, {"wood": 1.5}) == pytest.approx(13.0)
    assert D.bundle_value({"per_turn": {"gold": 5}, "turns": 3}) == pytest.approx(15.0)
    assert D.bundle_value({"per_turn": {"gold": 10}, "turns": 2}, discount=0.5) == pytest.approx(15.0)
    assert D.bundle_value({"tiles": [[1, 1], [2, 2]]}, tile_value=7) == pytest.approx(14.0)


# ====================================================================== propose
def test_propose_opens_private_deal():
    g = world()
    r = ok(g, "p1", dict(P12, message="surplus wood", expires_in=3))
    assert r == {"index": 0, "ok": True, "deal": "d1"}
    d = g.player_view("p2")["deals"]["open"][0]
    assert d == {"id": "d1", "thread": "d1", "from": "p1", "to": "p2", "give": {"wood": 30},
                 "get": {"gold": 20}, "peace": None, "message": "surplus wood", "turn": 0, "expires_turn": 3,
                 "status": "open", "problem": None, "deliverable": True}
    assert g.player_view("p1")["deals"]["open"][0]["id"] == "d1"
    assert g.player_view("p3")["deals"]["open"] == []
    # nothing moved yet
    assert g.player("p1").resources["wood"] == C.START_RESOURCES["wood"]


@pytest.mark.parametrize("action, fragment", [
    (dict(P12, to="p1"), "yourself"),
    (dict(P12, to="p9"), "unknown player"),
    (dict(P12, to=5), "player id"),
    ({"type": "propose", "to": "p2"}, "at least one term"),
    ({"type": "propose", "to": "p2", "give": {"tiles": [[12, 3]]}}, "not owned by p1"),
    ({"type": "propose", "to": "p2", "get": {"tiles": [[3, 3]]}}, "not owned by p2"),
    ({"type": "propose", "to": "p2", "give": {"tiles": [[2, 2]]}}, "is a city"),
    ({"type": "propose", "to": "p2", "give": {"tiles": [[99, 3]]}}, "off the map"),
    ({"type": "propose", "to": "p2", "peace": 60}, "peace must be"),
    ({"type": "propose", "to": "p2", "give": {"gold": 1}, "expires_in": 0}, "expires_in"),
    ({"type": "propose", "to": "p2", "give": {"gold": 1}, "message": "x" * (C.DEAL_MESSAGE_MAX_LENGTH + 1)},
     "longer than"),
    ({"type": "propose", "to": "p2", "give": {"gold": 1}, "message": 5}, "must be a string"),
])
def test_propose_errors(action, fragment):
    g = world()
    assert fragment in err(g, "p1", action)
    assert g.open_deals == {} and g.diplomacy_log == []


def test_propose_relic_tile_rejected():
    g = world()
    r = g.relics[0]
    g._set_owner(r, "p1")
    x, y = g.xy(r)
    assert "relic" in err(g, "p1", {"type": "propose", "to": "p2", "give": {"tiles": [[x, y]]}})


def test_propose_to_eliminated_player():
    g = world()
    g.cities.pop(g.idx(2, 12))
    run_turn(g)
    assert not g.player("p3").alive
    assert "eliminated" in err(g, "p1", dict(P12, to="p3"))
    assert "eliminated" in g.diplomacy("p3", [P12])[0]["error"]


def test_max_open_proposals():
    g = world()
    for k in range(C.DEAL_MAX_OPEN_PER_PLAYER):
        ok(g, "p1", dict(P12, to="p2" if k % 2 else "p3"))
    assert "open proposals" in err(g, "p1", P12)
    ok(g, "p1", {"type": "withdraw", "deal": "d1"})
    ok(g, "p1", P12)
    # proposals made to you don't count against you
    ok(g, "p2", {"type": "propose", "to": "p1", "give": {"gold": 1}})


# ====================================================================== counter
def test_counter_thread():
    g = world()
    ok(g, "p1", P12)
    r = ok(g, "p2", {"type": "counter", "deal": "d1", "give": {"gold": 12}, "get": {"wood": 30},
                     "message": "12 or nothing"})
    assert r["deal"] == "d2" and r["countered"] == "d1"
    assert g.deals["d1"]["status"] == "countered" and "d2" in g.deals["d1"]["reason"]
    d2 = g.deals["d2"]
    assert (d2["from"], d2["to"], d2["thread"], d2["give"], d2["get"]) == ("p2", "p1", "d1", {"gold": 12}, {"wood": 30})
    # counter of a counter stays in the thread
    ok(g, "p1", {"type": "counter", "deal": "d2", "give": {"wood": 30}, "get": {"gold": 16}})
    assert g.deals["d3"]["thread"] == "d1" and g.deals["d3"]["to"] == "p2"
    v1 = g.player_view("p1")["deals"]
    assert [d["id"] for d in v1["open"]] == ["d3"]
    assert [(d["id"], d["status"]) for d in v1["recent"]] == [("d2", "countered"), ("d1", "countered")]
    assert g.player_view("p3")["deals"]["recent"] == []
    ok(g, "p2", {"type": "accept", "deal": "d3"})
    assert g.deals["d3"]["status"] == "accepted"
    ev = run_turn(g)
    types = [e["type"] for e in ev if e["type"].startswith("deal_")]
    assert types == ["deal_proposed", "deal_countered", "deal_countered", "deal_executed"]
    assert events_of(ev, "deal_countered")[0]["new"]["id"] == "d2"


def test_counter_errors():
    g = world()
    ok(g, "p1", P12)
    # only the recipient may counter; the proposer is told so
    assert "only the recipient" in err(g, "p1", {"type": "counter", "deal": "d1", "give": {"gold": 1}})
    # a third party cannot tell the deal exists
    e_exists = err(g, "p3", {"type": "counter", "deal": "d1", "give": {"gold": 1}})
    e_missing = err(g, "p3", {"type": "counter", "deal": "d99", "give": {"gold": 1}})
    assert e_exists.replace("d1", "dX") == e_missing.replace("d99", "dX")
    # the counter itself must be valid; the original stays open when it is not
    assert "not owned by p2" in err(g, "p2", {"type": "counter", "deal": "d1", "give": {"tiles": [[3, 3]]}})
    assert g.deals["d1"]["status"] == "open"
    ok(g, "p2", {"type": "reject", "deal": "d1"})
    assert "no longer open (rejected)" in err(g, "p2", {"type": "counter", "deal": "d1", "give": {"gold": 1}})


# ====================================================================== accept
def test_accept_settles_immediately_and_publicly():
    g = world()
    ok(g, "p1", P12)
    before = totals(g)
    r1, r2 = res(g, "p1"), res(g, "p2")
    r = ok(g, "p2", {"type": "accept", "deal": "d1"})
    assert r == {"index": 0, "ok": True, "deal": "d1", "status": "accepted"}
    assert res(g, "p1")["wood"] == r1["wood"] - 30 and res(g, "p1")["gold"] == r1["gold"] + 20
    assert res(g, "p2")["wood"] == r2["wood"] + 30 and res(g, "p2")["gold"] == r2["gold"] - 20
    assert totals(g) == before
    # views update at once, reputation counts both sides
    v3 = g.player_view("p3")
    assert v3["deals"]["log"] == [{"id": "d1", "turn": 0, "from": "p1", "to": "p2", "give": {"wood": 30},
                                   "get": {"gold": 20}, "peace": None}]
    assert v3["players"][0]["resources"]["wood"] == r1["wood"] - 30
    assert [p["reputation"]["deals"] for p in v3["players"]] == [1, 1, 0]
    assert g.spectator_view()["deals"]["log"][0]["id"] == "d1"
    ev = run_turn(g)
    ex = events_of(ev, "deal_executed")[0]
    assert ex["from"] == "p1" and ex["to"] == "p2" and ex["by"] == "p2" and ex["give"] == {"wood": 30}
    assert "deal_executed" in {e["type"] for e in g.player_view("p3")["events"]}
    assert "deal_executed" in {e["type"] for e in g.spectator_view()["events"]}
    assert "deal_proposed" not in {e["type"] for e in g.player_view("p3")["events"]}


def test_accept_role_and_reuse_errors():
    g = world()
    ok(g, "p1", P12)
    assert "only the recipient" in err(g, "p1", {"type": "accept", "deal": "d1"})
    assert "no open deal" in err(g, "p3", {"type": "accept", "deal": "d1"})
    assert "deal must be" in err(g, "p2", {"type": "accept"})
    ok(g, "p2", {"type": "accept", "deal": "d1"})
    assert "no longer open (accepted)" in err(g, "p2", {"type": "accept", "deal": "d1"})


def test_accept_is_atomic_on_failure():
    g = world()
    # p1 can pay the resources, but p2 no longer owns the tile it promised
    g.set_owner(4, 2, "p2")                             # (next to p1's land)
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"gold": 30}, "get": {"wood": 5, "tiles": [[4, 2]]}})
    g.set_owner(4, 2, "p3")
    before = {p.id: res(g, p.id) for p in g.players}
    owners = list(g.owner)
    r = g.diplomacy("p2", [{"type": "accept", "deal": "d1"}])[0]
    assert r["ok"] is False and r["status"] == "failed" and "not owned by p2" in r["error"]
    assert {p.id: res(g, p.id) for p in g.players} == before and g.owner == owners
    assert g.deals["d1"]["status"] == "failed" and g.contracts == []
    assert g.player("p1").deals == g.player("p2").deals == 0
    assert g.player_view("p1")["deals"]["recent"][0]["reason"] == "tile [4, 2] is not owned by p2"
    ev = run_turn(g)
    f = events_of(ev, "deal_failed")[0]
    assert f["by"] == "p2" and "not owned" in f["reason"]
    assert "deal_failed" in {e["type"] for e in g.player_view("p1")["events"]}
    assert "deal_failed" not in {e["type"] for e in g.player_view("p3")["events"]}


@pytest.mark.parametrize("who, fragment", [("p1", "p1 is short of 70 wood"), ("p2", "p2 is short of 50 gold")])
def test_accept_fails_when_either_side_cannot_pay(who, fragment):
    g = world()
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"wood": 100}, "get": {"gold": 100}})
    g.player("p1").resources["wood"] = 100 if who == "p2" else 30
    g.player("p2").resources["gold"] = 100 if who == "p1" else 50
    before = totals(g)
    r = g.diplomacy("p2", [{"type": "accept", "deal": "d1"}])[0]
    assert not r["ok"] and fragment in r["error"]
    assert totals(g) == before
    assert g.player("p1").resources["wood"] == (100 if who == "p2" else 30)


def test_failed_accept_counts_as_applied_and_is_logged():
    g = world()
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"gold": 10 ** 5}})
    g.diplomacy("p2", [{"type": "accept", "deal": "d1"}])
    assert [e["action"]["type"] for e in g.diplomacy_log] == ["propose", "accept"]
    assert g._dip_counts["p2"] == [1, 0]


# ====================================================================== reject / withdraw / expiry
def test_reject_and_withdraw():
    g = world()
    ok(g, "p1", P12)
    ok(g, "p1", dict(P12, to="p3"))
    assert "only the recipient" in err(g, "p1", {"type": "reject", "deal": "d1"})
    assert "only the proposer" in err(g, "p2", {"type": "withdraw", "deal": "d1"})
    ok(g, "p2", {"type": "reject", "deal": "d1", "message": "too pricey"})
    ok(g, "p1", {"type": "withdraw", "deal": "d2"})
    assert (g.deals["d1"]["status"], g.deals["d1"]["reason"]) == ("rejected", "too pricey")
    assert g.deals["d2"]["status"] == "withdrawn"
    assert g.open_deals == {}
    ev = run_turn(g)
    rej = events_of(ev, "deal_rejected")[0]
    assert rej["message"] == "too pricey" and rej["by"] == "p2"
    assert events_of(ev, "deal_withdrawn")[0]["by"] == "p1"
    assert "deal_rejected" not in {e["type"] for e in g.player_view("p3")["events"]}


def test_expiry():
    g = world()
    ok(g, "p1", dict(P12, expires_in=1))      # open through the end of turn 1
    ok(g, "p1", dict(P12, to="p3"))           # default: through turn 2
    run_turn(g)
    assert g.deals["d1"]["status"] == "open"
    ev = run_turn(g)
    assert [e["deal"] for e in events_of(ev, "deal_expired")] == ["d1"]
    assert g.deals["d1"]["status"] == "expired" and g.deals["d2"]["status"] == "open"
    assert "no longer open (expired)" in err(g, "p2", {"type": "accept", "deal": "d1"})
    ev = run_turn(g)
    assert [e["deal"] for e in events_of(ev, "deal_expired")] == ["d2"]
    assert g.open_deals == {}


# ====================================================================== say / limits
def test_say_private_and_public():
    g = world()
    ok(g, "p1", {"type": "say", "to": "p2", "text": "psst"})
    ok(g, "p1", {"type": "say", "to": "all", "text": "hello all"})
    # messages are visible at once (no need to wait for the turn)
    assert [m["text"] for m in g.player_view("p2")["messages"]] == ["psst", "hello all"]
    assert [m["text"] for m in g.player_view("p3")["messages"]] == ["hello all"]
    assert [m["text"] for m in g.spectator_view()["messages"]] == ["hello all"]
    assert "to: cannot target yourself" in err(g, "p1", {"type": "say", "to": "p1", "text": "me"})
    assert "unknown player" in err(g, "p1", {"type": "say", "to": "p7", "text": "x"})
    assert "empty" in err(g, "p1", {"type": "say", "to": "all", "text": "  "})
    assert "longer" in err(g, "p1", {"type": "say", "to": "all", "text": "x" * (C.MAX_MESSAGE_LENGTH + 1)})
    ev = run_turn(g)
    says = events_of(ev, "say")
    assert [(e["to"], e["text"]) for e in says] == [("p2", "psst"), ("all", "hello all")]
    assert [e["text"] for e in g.player_view("p3")["events"] if e["type"] == "say"] == ["hello all"]


def test_per_turn_limits():
    g = world()
    for k in range(C.SAY_PER_TURN):
        ok(g, "p1", {"type": "say", "to": "all", "text": f"m{k}"})
    assert "messages per turn" in err(g, "p1", {"type": "say", "to": "all", "text": "one too many"})
    # errors don't count toward the limit
    for _ in range(50):
        err(g, "p1", {"type": "accept", "deal": "d404"})
    n_ok = 0
    for k in range(40):
        r = g.diplomacy("p1", [{"type": "propose", "to": "p2", "give": {"gold": 1}}])[0]
        if r["ok"]:
            n_ok += 1
            ok(g, "p1", {"type": "withdraw", "deal": r["deal"]})
            n_ok += 1
    assert n_ok == C.DIPLOMACY_ACTIONS_PER_TURN - C.SAY_PER_TURN
    assert "diplomacy actions per turn" in err(g, "p1", P12)
    ok(g, "p2", P12 | {"to": "p1"})            # other players are unaffected
    run_turn(g)
    ok(g, "p1", P12)                            # the limit resets every turn
    # one call with more entries than MAX_ACTIONS_PER_CALL
    out = g.diplomacy("p3", [{"type": "say", "to": "all", "text": "x"}] * (C.MAX_ACTIONS_PER_CALL + 3))
    assert len(out) == C.MAX_ACTIONS_PER_CALL + 3
    assert "too many actions in one call" in out[-1]["error"]


def test_limits_shared_between_orders_and_channel():
    g = world()
    for k in range(C.SAY_PER_TURN - 1):
        ok(g, "p1", {"type": "say", "to": "all", "text": f"c{k}"})
    ev = run_turn(g, {"p1": [{"type": "message", "to": "all", "text": "o1"},
                             {"type": "say", "to": "all", "text": "o2"}]})
    fails = events_of(ev, "order_failed")
    assert len(fails) == 1 and fails[0]["index"] == 1 and "messages per turn" in fails[0]["reason"]
    assert [m["text"] for m in g.messages][-2:] == [f"c{C.SAY_PER_TURN - 2}", "o1"]


# ====================================================================== diplomacy() robustness
def test_diplomacy_never_raises_on_garbage():
    g = world()
    assert g.diplomacy("p1", "nope")[0]["error"] == "actions must be a list"
    assert g.diplomacy("p9", [])[0]["error"].startswith("unknown player")
    assert g.diplomacy(None, [P12])[0]["ok"] is False
    assert g.diplomacy("p1", {"actions": [P12]})[0]["ok"]           # {"actions": [...]} form
    assert g.diplomacy("p1", P12)[0]["ok"]                          # single action object
    rng = random.Random(3)
    junk = [None, 5, "x", [], {}, {"type": None}, {"type": "propose"}, {"type": "accept", "deal": []},
            {"type": "propose", "to": "p2", "give": {"gold": float("nan")}},
            {"type": "propose", "to": "p2", "give": {"tiles": [[None, None]]}},
            {"type": "counter", "deal": {"x": 1}}, {"type": "say", "to": None, "text": "t"},
            {"type": "propose", "to": "p2", "give": {"gold": 10 ** 30}}]
    for _ in range(200):
        out = g.diplomacy("p1", [rng.choice(junk) for _ in range(rng.randrange(5))])
        assert all(isinstance(r["index"], int) and r["ok"] is False and isinstance(r["error"], str) for r in out)


def test_diplomacy_outside_running_game():
    g = Game(GameConfig(seed=1))
    g.add_player("A")
    g.add_player("B")
    assert "not running" in g.diplomacy("p1", [P12])[0]["error"]
    g.start()
    g.max_turns = 1
    run_turn(g)
    assert g.finished
    assert "not running" in g.diplomacy("p1", [P12])[0]["error"]


# ====================================================================== contracts
def test_contract_pays_after_yields_before_upkeep():
    g = world()
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"per_turn": {"food": 10}, "turns": 2}, "get": {"gold": 15}})
    ok(g, "p2", {"type": "accept", "deal": "d1"})
    c = g.contracts[0]
    assert c == {"id": "c1", "payer": "p1", "payee": "p2", "per_turn": {"food": 10}, "turns_left": 2, "deal": "d1"}
    assert g.spectator_view()["contracts"] == [c] and g.player_view("p3")["contracts"] == [c]
    p1, p2 = g.player("p1"), g.player("p2")
    food_income = g.stats()["p1"]["income"]["food"]
    assert food_income >= 10
    # p1 starts the step with no food: only this turn's yields can pay (=> yields first)
    p1.resources["food"] = 0
    # upkeep bigger than what is left after paying => starvation (=> payment before upkeep)
    g.place_units(2, 2, "p1", {"infantry": food_income - 10 + 4})
    f2 = p2.resources["food"]
    ev = run_turn(g)
    paid = events_of(ev, "contract_paid")[0]
    assert paid == {"turn": 0, "type": "contract_paid", "seq": paid["seq"], "contract": "c1", "payer": "p1",
                    "payee": "p2", "paid": {"food": 10}, "turns_left": 1}
    assert events_of(ev, "starvation")[0]["deficit"] == 4
    assert p1.resources["food"] == 0
    assert p2.resources["food"] == f2 + 10 + g.stats()["p2"]["income"]["food"]
    assert "contract_paid" not in {e["type"] for e in g.player_view("p3")["events"]}
    ev = run_turn(g)
    assert events_of(ev, "contract_completed")[0]["contract"] == "c1"
    assert g.contracts == [] and p1.contracts_honoured == 1 and p1.defaults == 0
    assert g.player_view("p3")["players"][0]["reputation"] == {"deals": 1, "contracts_honoured": 1,
                                                               "defaults": 0, "betrayals": 0,
                                                               "influence_debt": 0}


def test_contract_default():
    g = world()
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"per_turn": {"gold": 500, "wood": 1}, "turns": 5}})
    ok(g, "p2", {"type": "accept", "deal": "d1"})
    p1, p2 = g.player("p1"), g.player("p2")
    p1.resources["influence"] = 900
    g1, g2, w2 = p1.resources["gold"], p2.resources["gold"], p2.resources["wood"]
    ev = run_turn(g)
    dflt = events_of(ev, "contract_default")[0]
    # 5 x 501 units still owed -> 1 influence per CONTRACT_DEFAULT_OWED_PER_INFLUENCE units
    assert dflt["penalty"] == D.default_penalty({"gold": 500, "wood": 1}, 5) == 501
    assert dflt["payer"] == "p1" and dflt["debt"] == 0 and dflt["contract"] == "c1"
    assert not events_of(ev, "contract_paid")
    st = g.stats()
    assert p1.resources["gold"] == g1 + st["p1"]["income"]["gold"]        # nothing paid, not even partially
    assert p2.resources["gold"] == g2 + st["p2"]["income"]["gold"]
    assert p2.resources["wood"] == w2 + st["p2"]["income"]["wood"]
    assert p1.resources["influence"] == 900 - 501 + st["p1"]["income"]["influence"]
    assert p1.defaults == 1 and g.contracts == [] and p1.influence_debt == 0
    assert "contract_default" in {e["type"] for e in g.spectator_view()["events"]}     # public
    assert g.spectator_view()["players"][0]["reputation"]["defaults"] == 1


def test_default_penalty_scale():
    assert D.default_penalty({"gold": 1}, 1) == C.CONTRACT_DEFAULT_PENALTY == 25     # minimum
    assert D.default_penalty({"gold": 5}, 10) == 25
    assert D.default_penalty({"gold": 30}, 30) == 900 // C.CONTRACT_DEFAULT_OWED_PER_INFLUENCE == 180
    assert D.default_penalty({"gold": 13, "food": 1}, 10) == 28                       # rounded up


def test_contract_default_penalty_beyond_influence_becomes_debt():
    g = world()
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"per_turn": {"gold": 900}, "turns": 1}})
    ok(g, "p2", {"type": "accept", "deal": "d1"})
    p1 = g.player("p1")
    p1.resources["influence"] = 3
    inc = g.stats()["p1"]["income"]["influence"]      # influence income arrives before the instalment
    pen = D.default_penalty({"gold": 900}, 1)
    assert pen == 180 and 3 + inc < pen
    ev = run_turn(g)
    d = events_of(ev, "contract_default")[0]
    assert d["penalty"] == pen and d["debt"] == pen - 3 - inc
    assert p1.resources["influence"] == 0 and p1.influence_debt == pen - 3 - inc
    assert g.spectator_view()["players"][0]["reputation"]["influence_debt"] == pen - 3 - inc
    # the debt is paid from later influence income before anything else
    run_turn(g)
    assert p1.resources["influence"] == 0 and p1.influence_debt == pen - 3 - 2 * inc
    p1.influence_debt = 1
    run_turn(g)
    assert p1.influence_debt == 0 and p1.resources["influence"] == inc - 1


def test_default_cannot_be_dodged_by_spending_influence_first():
    """Review finding: borrow, spend gold on the market and influence on
    claims in the same turn, default in phase 7 -> the penalty is owed anyway."""
    g = world()
    p1, p2 = g.player("p1"), g.player("p2")
    p2.resources["gold"] = 600
    p1.resources["gold"] = 0
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"per_turn": {"gold": 30}, "turns": 30},
                 "get": {"gold": 500}})
    ok(g, "p2", {"type": "accept", "deal": "d1"})
    p1.resources["influence"] = 9
    ev = run_turn(g, {"p1": [{"type": "market", "side": "buy", "resource": "stone", "qty": 190},
                             {"type": "claim", "at": [4, 2]}, {"type": "claim", "at": [4, 3]},
                             {"type": "claim", "at": [4, 1]}]})
    assert len(events_of(ev, "claim")) == 3 and not events_of(ev, "order_failed")
    d = events_of(ev, "contract_default")[0]
    assert d["penalty"] == 180 and p1.influence_debt == 180 - (d["penalty"] - d["debt"]) > 150


def test_loan_both_directions_and_contract_order():
    """p1 lends 100 gold, p2 repays 12 gold/turn; contracts pay in creation order."""
    g = world()
    g.player("p1").resources["gold"] = 100
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"gold": 100},
                 "get": {"per_turn": {"gold": 12}, "turns": 3}})
    ok(g, "p2", {"type": "accept", "deal": "d1"})
    ok(g, "p3", {"type": "propose", "to": "p2", "give": {"per_turn": {"stone": 1}, "turns": 3},
                 "get": {"per_turn": {"food": 2}, "turns": 2}})
    r = ok(g, "p2", {"type": "accept", "deal": "d2"})
    assert [c["id"] for c in g.contracts] == ["c1", "c2", "c3"]
    assert [(c["payer"], c["payee"]) for c in g.contracts] == [("p2", "p1"), ("p3", "p2"), ("p2", "p3")]
    assert r["ok"]
    ev = run_turn(g)
    assert [e["contract"] for e in events_of(ev, "contract_paid")] == ["c1", "c2", "c3"]
    for _ in range(3):
        run_turn(g)
    assert g.contracts == []
    assert g.player("p2").contracts_honoured == 2 and g.player("p3").contracts_honoured == 1
    json.dumps(g.player_view("p2"))


# ====================================================================== peace
def test_peace_from_deal():
    g = world()
    g.set_owner(7, 2, "p2")
    g.place_units(6, 2, "p1", {"infantry": 3})
    # the move was valid when submitted ...
    assert g.submit_orders("p1", [{"type": "move", "from": [6, 2], "to": [7, 2]}]) == []
    ok(g, "p1", {"type": "propose", "to": "p2", "peace": 20})
    ok(g, "p2", {"type": "accept", "deal": "d1"})
    assert g.treaty("p1", "p2") and g.treaties[("p1", "p2")] == 20
    assert g.player_view("p3")["treaties"] == [{"a": "p1", "b": "p2", "until_turn": 20}]
    # ... and is re-checked at resolution: peace now forbids it
    ev = run_turn(g)
    assert "treaty partner" in events_of(ev, "order_failed")[0]["reason"]
    assert g.owner[g.idx(7, 2)] == "p2"
    signed = events_of(ev, "treaty_signed")[0]
    assert signed == {"turn": 0, "type": "treaty_signed", "a": "p1", "b": "p2", "until_turn": 20, "deal": "d1"}


def test_peace_extends_existing_treaty_to_later_end():
    g = world()
    ok(g, "p1", {"type": "propose", "to": "p2", "peace": 30})
    ok(g, "p2", {"type": "accept", "deal": "d1"})
    ok(g, "p1", {"type": "propose", "to": "p2", "peace": 10, "give": {"gold": 1}})
    ok(g, "p2", {"type": "accept", "deal": "d2"})
    assert g.treaties[("p1", "p2")] == 30              # shorter peace doesn't shorten it
    run_turn(g)
    ok(g, "p2", {"type": "propose", "to": "p1", "peace": 50})
    ok(g, "p1", {"type": "accept", "deal": "d3"})
    assert g.treaties[("p1", "p2")] == 1 + 50
    # treaty proposals are refused now (already at peace)
    assert g.submit_orders("p3", [{"type": "propose_treaty", "to": "p1", "turns": 10}]) == []
    assert g.submit_orders("p1", [{"type": "propose_treaty", "to": "p2", "turns": 10}])


# ====================================================================== land
def test_tile_transfer_with_improvement():
    g = world()
    g.improvement[g.idx(3, 3)] = "farm"
    g._invalidate()
    g.set_owner(4, 3, "p2")          # p2 borders [3, 3]; [3, 2] touches it through [3, 3]
    t1, t2 = g.player("p1").tiles, g.player("p2").tiles
    inc1 = g.stats()["p1"]["income"]["food"]
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"tiles": [[3, 3], [3, 2]]}, "get": {"gold": 30}})
    # an order submitted before the deal (still valid then) is re-checked at resolution
    assert g.submit_orders("p1", [{"type": "build", "at": [3, 2], "building": "farm"}]) == []
    v = g.player_view("p2")
    assert v["deals"]["open"][0]["give"] == {"tiles": [[3, 3], [3, 2]]}
    assert v["trade_offers"] == []                      # not a resource-only deal
    ok(g, "p2", {"type": "accept", "deal": "d1"})
    assert g.owner[g.idx(3, 3)] == "p2" and g.owner[g.idx(3, 2)] == "p2"
    assert g.improvement[g.idx(3, 3)] == "farm"
    assert (g.player("p1").tiles, g.player("p2").tiles) == (t1 - 2, t2 + 2)
    st = g.stats()                                      # cached stats were recomputed
    assert st["p1"]["income"]["food"] == inc1 - 4 - 2 and st["p1"]["tiles"] == t1 - 2
    assert g.player_view("p1")["you"]["claim_cost"] == 2 + (t1 - 2) // C.CLAIM_TILES_PER_EXTRA
    assert g.player_view("p3")["map"]["owner"][3][3] == "p2"
    # p1 can no longer build there; p2 keeps the tile and its yield
    f2 = g.player("p2").resources["food"]
    inc2 = st["p2"]["income"]["food"]
    ev = run_turn(g)
    assert "do not own" in events_of(ev, "order_failed")[0]["reason"]
    assert g.owner[g.idx(3, 3)] == "p2" and g.improvement[g.idx(3, 2)] is None
    assert g.player("p2").resources["food"] == f2 + inc2


def border(g):
    """p2's land reaches p1's: p2 owns [4..10, 3] (touching p1's [3, 3])."""
    for x in range(4, 11):
        g.set_owner(x, 3, "p2")


def test_tile_with_foreign_units_cannot_be_transferred():
    """Review finding: a seller could sell a tile with its own army on it
    (with peace in the deal) and capture it back once the treaty ended."""
    g = world()
    border(g)
    g.place_units(3, 3, "p1", {"infantry": 2})         # p1's own army stays on the tile it sells
    for peace in (None, 10):
        ok(g, "p1", {"type": "propose", "to": "p2", "give": {"tiles": [[3, 3]]}, "get": {"gold": 10},
                     "peace": peace})
        did = f"d{g._deal_counter}"
        v = g.player_view("p2")
        deal = v["deals"]["open"][0]
        assert deal["deliverable"] is False and "holds units of p1" in deal["problem"]
        assert D.view_deal_problem(v, deal) == deal["problem"]            # the pure helper agrees
        r = g.diplomacy("p2", [{"type": "accept", "deal": did}])[0]
        assert not r["ok"] and "holds units of p1" in r["error"] and g.owner[g.idx(3, 3)] == "p1"
    # units of a treaty partner of the receiver block too
    g.armies.clear()
    g.treaties[g._pair("p2", "p3")] = 40
    g.place_units(3, 3, "p3", {"infantry": 1})
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"tiles": [[3, 3]]}})
    r = g.diplomacy("p2", [{"type": "accept", "deal": f"d{g._deal_counter}"}])[0]
    assert not r["ok"] and "holds units of p3" in r["error"]
    # the receiver's own units don't block
    g.armies.clear()
    g.place_units(3, 3, "p2", {"infantry": 1})
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"tiles": [[3, 3]]}})
    ok(g, "p2", {"type": "accept", "deal": f"d{g._deal_counter}"})
    assert g.owner[g.idx(3, 3)] == "p2"


def test_traded_tiles_must_touch_the_receivers_land():
    """Review finding: gifting the tiles around a capital to a third party
    with a treaty with the attacker made the capital unreachable."""
    g = world()
    g.treaties[g._pair("p1", "p3")] = 40
    # p2 cannot plant p3's land next to its own capital: p3's land is far away
    e = err(g, "p2", {"type": "propose", "to": "p3", "give": {"tiles": [[11, 2], [13, 2], [12, 1], [12, 3]]}})
    assert "give: tile [11, 2] does not touch p3's land" in e
    e = err(g, "p3", {"type": "propose", "to": "p2", "get": {"tiles": [[11, 2]]}})
    assert e.startswith("get: tile [11, 2] does not touch p3's land")
    # adjacent to the receiver, or chained through another tile of the same bundle
    border(g)
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"tiles": [[3, 3], [3, 2], [3, 1]]}})
    # tiles the receiver hands over in the same deal don't count as its land
    g.set_owner(4, 4, "p1")
    e = err(g, "p1", {"type": "propose", "to": "p2", "give": {"tiles": [[4, 4]]}, "get": {"tiles": [[4, 3]]}})
    assert e.startswith("give: tile [4, 4] does not touch p2's land")
    # re-checked on accept: the land it touched changed hands meanwhile
    g.set_owner(5, 4, "p1")
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"tiles": [[5, 4]]}})
    did = f"d{g._deal_counter}"
    g.set_owner(5, 3, "p3")
    v = g.player_view("p2")
    deal = next(d for d in v["deals"]["open"] if d["id"] == did)
    assert "does not touch p2's land" in deal["problem"] and D.view_deal_problem(v, deal) == deal["problem"]
    r = g.diplomacy("p2", [{"type": "accept", "deal": did}])[0]
    assert not r["ok"] and r["status"] == "failed"


def test_tiles_received_per_turn_are_capped():
    """Review finding: a losing player could hand dozens of tiles to pick
    the score winner on the final turn."""
    g = world()
    border(g)
    cap = C.DEAL_MAX_TILES_RECEIVED_PER_TURN
    for x in range(4, 11):
        g.set_owner(x, 4, "p1")
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"tiles": [[x, 4] for x in range(4, 4 + cap - 1)]}})
    ok(g, "p2", {"type": "accept", "deal": f"d{g._deal_counter}"})
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"tiles": [[9, 4], [10, 4]]}})
    did = f"d{g._deal_counter}"
    v = g.player_view("p2")
    deal = next(d for d in v["deals"]["open"] if d["id"] == did)
    assert f"at most {cap} tiles" in deal["problem"] and D.view_deal_problem(v, deal) == deal["problem"]
    assert D.tiles_received(v["deals"]["log"], v["turn"], "p2") == cap - 1
    assert not g.diplomacy("p2", [{"type": "accept", "deal": did}])[0]["ok"]
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"tiles": [[9, 4]]}})
    ok(g, "p2", {"type": "accept", "deal": f"d{g._deal_counter}"})
    run_turn(g)                                           # a new turn: the count starts over
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"tiles": [[10, 4]]}})
    ok(g, "p2", {"type": "accept", "deal": f"d{g._deal_counter}"})
    assert g.owner[g.idx(10, 4)] == "p2"


def test_storage_caps_apply_at_end_of_turn():
    g = world()
    g.player("p1").resources["wood"] = 290
    g.player("p2").resources["wood"] = 300
    ok(g, "p2", {"type": "propose", "to": "p1", "give": {"wood": 250}})
    ok(g, "p1", {"type": "accept", "deal": "d1"})
    assert g.player("p1").resources["wood"] == 540     # over the cap mid-turn: spendable this turn
    run_turn(g)
    assert g.player("p1").resources["wood"] == C.STORAGE_BASE


# ====================================================================== elimination
def test_elimination_cleans_up_deals_and_contracts():
    g = world()
    ok(g, "p3", {"type": "propose", "to": "p1", "give": {"gold": 5}})
    ok(g, "p1", {"type": "propose", "to": "p3", "give": {"gold": 5}})
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"gold": 5}})
    ok(g, "p1", {"type": "propose", "to": "p3", "give": {"per_turn": {"gold": 1}, "turns": 10}})
    ok(g, "p3", {"type": "accept", "deal": "d4"})
    assert len(g.contracts) == 1
    g.cities.pop(g.idx(2, 12))
    ev = run_turn(g)
    assert not g.player("p3").alive
    wd = events_of(ev, "deal_withdrawn")
    assert sorted(e["deal"] for e in wd) == ["d1", "d2"] and all(e["by"] is None for e in wd)
    assert g.deals["d1"]["reason"] == "p3 was eliminated"
    assert list(g.open_deals) == ["d3"] and g.contracts == []
    assert "no longer open (withdrawn)" in err(g, "p1", {"type": "accept", "deal": "d1"})


# ====================================================================== visibility
def test_private_deals_hidden_from_others_and_public_spectator():
    g = world()
    g.max_turns = 3
    ok(g, "p1", dict(P12, message="SECRET price"))
    ok(g, "p2", {"type": "counter", "deal": "d1", "give": {"gold": 25}, "get": {"wood": 30}})
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"stone": 5}, "message": "SECRET gift"})
    ok(g, "p2", {"type": "accept", "deal": "d3"})
    ok(g, "p1", {"type": "say", "to": "p2", "text": "SECRET chat"})
    for v in (g.player_view("p3"), g.spectator_view()):
        assert v["deals"]["open"] == [] and v["deals"]["recent"] == [] and v["trade_offers"] == []
        assert [e["id"] for e in v["deals"]["log"]] == ["d3"]
        assert "SECRET" not in json.dumps(v)
    # the public spectator can't gauge private haggling from the counter
    assert g.spectator_view()["diplomacy_seq"] is None
    assert g.player_view("p3")["diplomacy_seq"] == g.spectator_view(full=True)["diplomacy_seq"] == g.diplomacy_seq
    run_turn(g)
    for v in (g.player_view("p3"), g.spectator_view()):
        types = {e["type"] for e in v["events"]}
        assert "deal_executed" in types and not types & {"deal_proposed", "deal_countered", "say"}
        assert "SECRET" not in json.dumps(v)
    for v in (g.player_view("p1"), g.player_view("p2"), g.spectator_view(full=True)):
        assert "SECRET price" in json.dumps(v) and "SECRET chat" in json.dumps(v)
        assert [d["id"] for d in v["deals"]["open"]] == ["d2"]
    run_turn(g)
    run_turn(g)
    assert g.finished
    v = g.spectator_view()                               # revealed after the game
    assert v["diplomacy_seq"] == g.diplomacy_seq
    assert "SECRET price" in json.dumps(v) and {d["id"] for d in v["deals"]["recent"]} == {"d1", "d2", "d3"}


def test_inbox():
    g = world()
    ok(g, "p1", P12)
    ok(g, "p1", {"type": "say", "to": "all", "text": "hi"})
    ok(g, "p3", {"type": "say", "to": "p1", "text": "private"})
    box = g.inbox("p2")
    assert box["seq"] == g.diplomacy_seq
    assert [i["type"] for i in box["items"]] == ["deal_proposed", "say"]
    assert all("_vis" not in i for i in box["items"])
    assert g.inbox("p1")["items"][0]["text"] == "private"   # own actions are not echoed
    since = box["seq"]
    assert g.inbox("p2", since)["items"] == []
    ok(g, "p2", {"type": "reject", "deal": "d1"})
    items = g.inbox("p1", since)["items"]
    assert [i["type"] for i in items] == ["deal_rejected"] and items[0]["seq"] > since
    assert g.inbox("p2", "garbage")["seq"] == g.diplomacy_seq
    assert g.player_view("p1")["diplomacy_seq"] == g.diplomacy_seq


def test_views_are_fresh_and_json():
    g = world()
    g.set_owner(4, 3, "p2")
    ok(g, "p1", {"type": "propose", "to": "p2", "give": {"tiles": [[3, 3]], "per_turn": {"gold": 1}, "turns": 2}})
    ok(g, "p2", {"type": "accept", "deal": "d1"})
    ok(g, "p1", P12)
    v = g.player_view("p2")
    json.dumps(v)
    v["deals"]["open"][0]["give"]["wood"] = 9999
    v["deals"]["log"][0]["give"]["tiles"].append([0, 0])
    v["contracts"][0]["per_turn"]["gold"] = 77
    assert g.deals["d2"]["give"] == {"wood": 30}
    assert g.deal_log[0]["give"] == {"tiles": [[3, 3]], "per_turn": {"gold": 1}, "turns": 2}
    assert g.contracts[0]["per_turn"] == {"gold": 1}
    # event payloads are copies too: mutating them can't reach the game state
    g.inbox("p2")["items"][0]["deal"]["give"]["gold"] = 5
    run_turn(g)
    v = g.spectator_view(full=True)
    v["events"][0]["deal"]["give"]["wood"] = 1
    assert g.deals["d1"]["give"] == {"tiles": [[3, 3]], "per_turn": {"gold": 1}, "turns": 2}


def test_rules_json_exposes_deal_constants():
    r = rules_json()["diplomacy"]["deals"]
    assert r["max_open_per_player"] == C.DEAL_MAX_OPEN_PER_PLAYER == 8
    assert r["contract_default_penalty"] == C.CONTRACT_DEFAULT_PENALTY == 25
    assert r["default_expires_in"] == C.DEAL_DEFAULT_EXPIRES_IN == 2
    assert r["actions_per_turn"] == 30 and r["say_per_turn"] == 10
    assert r["contract_turns"] == [1, 30] and r["peace_turns"] == [10, 50] and r["max_tiles_per_bundle"] == 5
    assert r["aliases"] == {"offer_trade": "propose", "accept_trade": "accept", "message": "say"}


# ====================================================================== inside orders
def test_diplomacy_actions_inside_orders():
    g = world()
    errs = g.submit_orders("p1", [{"type": "propose", "to": "p2", "give": {"wood": 10}, "get": {"gold": 5}},
                                  {"type": "say", "to": "p2", "text": "take it"},
                                  {"type": "propose", "to": "p2", "give": {"tiles": [[12, 3]]}},
                                  {"type": "accept", "deal": "d9"}])
    assert [e["index"] for e in errs] == [2, 3]
    assert "not owned by p1" in errs[0]["error"] and "no open deal" in errs[1]["error"]
    ev = run_turn(g)
    assert g.deals["d1"]["status"] == "open" and events_of(ev, "deal_proposed")
    assert [e["via"] for e in g.diplomacy_log] == ["orders", "orders"]
    r1 = res(g, "p1")
    ev = run_turn(g, {"p2": [{"type": "accept", "deal": "d1"}]})
    assert g.deals["d1"]["status"] == "accepted" and events_of(ev, "deal_executed")
    # resolution re-checks: the deal was withdrawn after the orders were submitted
    ok(g, "p1", P12)
    assert g.submit_orders("p2", [{"type": "accept", "deal": "d2"}, {"type": "accept", "deal": "d2"}])[0]["index"] == 1
    ok(g, "p1", {"type": "withdraw", "deal": "d2"})
    ev = run_turn(g)
    assert "no longer open (withdrawn)" in events_of(ev, "order_failed")[0]["reason"]
    assert r1


def test_orders_round_robin_rotates():
    """Two accepts compete for the same 50 gold of p3: the rotating start
    player of phase 1 wins (turn 0: p1 first; turn 1: p2 first)."""
    for turn, winner, loser in ((0, "p1", "p2"), (1, "p2", "p1")):
        g = world()
        if turn:
            run_turn(g)
        g.player("p3").resources["gold"] = 50
        ok(g, "p3", {"type": "propose", "to": "p1", "give": {"gold": 50}})
        ok(g, "p3", {"type": "propose", "to": "p2", "give": {"gold": 50}})
        ids = {"p1": "d1", "p2": "d2"}
        ev = run_turn(g, {"p1": [{"type": "accept", "deal": "d1"}], "p2": [{"type": "accept", "deal": "d2"}]})
        assert g.deals[ids[winner]]["status"] == "accepted"
        assert g.deals[ids[loser]]["status"] == "failed"
        assert events_of(ev, "order_failed")[0]["player"] == loser


def test_legacy_aliases_in_channel_and_orders():
    g = world()
    r = g.diplomacy("p1", [{"type": "offer_trade", "to": "p2", "give": {"wood": 5}, "want": {"gold": 3}},
                           {"type": "message", "to": "p2", "text": "legacy"}])
    assert [x["ok"] for x in r] == [True, True]
    assert g.player_view("p2")["trade_offers"] == [{"id": "d1", "from": "p1", "to": "p2", "give": {"wood": 5},
                                                    "want": {"gold": 3}, "turn": 0, "expires_turn": 2}]
    assert g.diplomacy("p2", [{"type": "accept_trade", "offer_id": "t1"}])[0]["ok"]
    assert g.deals["d1"]["status"] == "accepted"
    assert "only resources" in g.submit_orders("p1", [{"type": "offer_trade", "to": "p2",
                                                       "give": {"tiles": [[3, 3]]}}])[0]["error"]


# ====================================================================== determinism & fuzz
def random_bundle(rng, view, pid, tiles_of, to=None):
    b = {}
    for r in C.TRADABLE:
        if rng.random() < 0.35:
            b[r] = rng.choice([0, 1, 5, 20, 60, 150, 400])
    if tiles_of.get(pid) and rng.random() < 0.2:
        cands = tiles_of[pid]
        if to is not None and rng.random() < 0.7:        # land on the border with the receiver
            owner = view["map"]["owner"]
            h, w = len(owner), len(owner[0])
            cands = [t for t in cands if any(0 <= t[0] + dx < w and 0 <= t[1] + dy < h
                                             and owner[t[1] + dy][t[0] + dx] == to
                                             for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))] or cands
        b["tiles"] = rng.sample(cands, min(len(cands), rng.randint(1, 2)))
    if rng.random() < 0.15:
        b["per_turn"] = {rng.choice(C.TRADABLE): rng.randint(1, 15)}
        b["turns"] = rng.randint(1, 8)
    return b


def random_diplomacy(view, rng, n=4):
    me = view["you"]["id"]
    others = [p["id"] for p in view["players"] if p["id"] != me and p["alive"]]
    w, h = view["map"]["width"], view["map"]["height"]
    owner = view["map"]["owner"]
    blocked = {(c["x"], c["y"]) for c in view["cities"]} | {(r["x"], r["y"]) for r in view["map"]["relics"]}
    tiles_of = {}
    for y in range(h):
        for x in range(w):
            o = owner[y][x]
            if o is not None and (x, y) not in blocked:
                tiles_of.setdefault(o, []).append([x, y])
    to_me = [d for d in view["deals"]["open"] if d["to"] == me]
    mine = [d for d in view["deals"]["open"] if d["from"] == me]
    out = []
    for _ in range(n):
        k = rng.randrange(10)
        if k <= 2 and others:
            to = rng.choice(others)
            a = {"type": "propose", "to": to, "give": random_bundle(rng, view, me, tiles_of, to),
                 "get": random_bundle(rng, view, to, tiles_of, me)}
            if rng.random() < 0.2:
                a["peace"] = rng.randint(10, 20)
            if rng.random() < 0.3:
                a["expires_in"] = rng.randint(1, 5)
            if rng.random() < 0.2:
                a["message"] = "deal?"
            out.append(a)
        elif k == 3 and to_me:
            d = rng.choice(to_me)
            out.append({"type": "counter", "deal": d["id"], "give": random_bundle(rng, view, me, tiles_of, d["from"]),
                        "get": random_bundle(rng, view, d["from"], tiles_of, me)})
        elif k in (4, 5) and to_me:
            out.append({"type": "accept", "deal": rng.choice(to_me)["id"]})
        elif k == 6 and to_me:
            out.append({"type": "reject", "deal": rng.choice(to_me)["id"], "message": "no"})
        elif k == 7 and mine:
            out.append({"type": "withdraw", "deal": rng.choice(mine)["id"]})
        elif k == 8:
            out.append({"type": "say", "to": rng.choice(others + ["all"]), "text": f"t{rng.randrange(99)}"})
        else:
            out.append({"type": rng.choice(D.ALL_ACTION_TYPES), "deal": f"d{rng.randrange(40)}",
                        "to": rng.choice(["p1", "p2", "p9", None]), "give": rng.choice([None, {"gold": -1}, {}]),
                        "text": rng.choice(["", "x", None])})
    return out


def _check_invariants(g):
    for p in g.players:
        assert all(v >= 0 for v in p.resources.values()), (p.id, p.resources)
    assert sum(p.tiles for p in g.players) == sum(1 for o in g.owner if o is not None)
    for d in g.open_deals.values():
        assert d["status"] == "open"


def _check_view_problems(g):
    """The pure view helper agrees with the engine's settlement check."""
    v = g.spectator_view(full=True)
    for d in v["deals"]["open"]:
        assert D.view_deal_problem(v, d) == d["problem"], d


def play_with_diplomacy(seed, n_players=5, turns=40, rounds=3, record=None):
    """A random game with negotiation rounds before each turn's orders
    (like the tournament runner). Returns (game, orders per turn, trace)."""
    from test_engine_fuzz import plausible_orders
    g = Game(GameConfig(seed=seed, max_turns=turns, game_id=f"dip{seed}"))
    for i in range(n_players):
        g.add_player(f"N{i}")
    g.start()
    rng = random.Random(seed)
    orders_log, trace = [], []
    while not g.finished:
        alive = g.alive_players()
        for rnd in range(rounds):
            k = (g.turn + rnd) % len(alive)
            for pid in alive[k:] + alive[:k]:
                acts = random_diplomacy(g.player_view(pid), rng)
                before_res, before_tiles = totals(g), sum(p.tiles for p in g.players)
                out = g.diplomacy(pid, acts)
                assert len(out) == len(acts)
                assert totals(g) == before_res                       # deals conserve resources
                assert sum(p.tiles for p in g.players) == before_tiles
                _check_invariants(g)
            _check_view_problems(g)
        turn_orders = {}
        for pid in alive:
            view = g.player_view(pid)
            orders = plausible_orders(view, rng, 8) + random_diplomacy(view, rng, 2)
            rng.shuffle(orders)
            turn_orders[pid] = copy.deepcopy(orders)
            g.submit_orders(pid, orders)
        orders_log.append(turn_orders)
        g.step()
        _check_invariants(g)
        trace.append(json.dumps(g.spectator_view(full=True), sort_keys=True))
    return g, orders_log, trace


def replay(seed, n_players, turns, orders_log, diplomacy_log):
    g = Game(GameConfig(seed=seed, max_turns=turns, game_id=f"dip{seed}"))
    for i in range(n_players):
        g.add_player(f"N{i}")
    g.start()
    by_turn = {}
    for e in diplomacy_log:
        if e["via"] == "channel":
            by_turn.setdefault(e["turn"], []).append(e)
    trace = []
    for t, turn_orders in enumerate(orders_log):
        assert g.turn == t
        for e in by_turn.get(t, []):
            r = g.diplomacy(e["pid"], [e["action"]])[0]
            assert r["ok"] or r.get("status") == "failed", r
        for pid, orders in turn_orders.items():
            g.submit_orders(pid, copy.deepcopy(orders))
        g.step()
        trace.append(json.dumps(g.spectator_view(full=True), sort_keys=True))
    return g, trace


def test_replay_from_orders_and_diplomacy_log_is_exact():
    g, orders_log, trace = play_with_diplomacy(21, n_players=5, turns=30)
    kinds = {e["action"]["type"] for e in g.diplomacy_log}
    assert {"propose", "counter", "accept", "reject", "withdraw", "say"} <= kinds
    assert any(e["via"] == "orders" for e in g.diplomacy_log)
    assert g.deal_log and g.player("p1").deals + g.player("p2").deals > 0
    log = json.loads(json.dumps(g.diplomacy_log))        # survives a JSON round trip (replay files)
    g2, trace2 = replay(21, 5, 30, orders_log, log)
    assert trace2 == trace
    assert g2.diplomacy_log == g.diplomacy_log and g2.diplomacy_seq == g.diplomacy_seq
    assert g2.result == g.result
    # and the same seed + same negotiation is fully deterministic
    _, _, trace3 = play_with_diplomacy(21, n_players=5, turns=30)
    assert trace3 == trace


def test_fuzz_random_diplomacy_in_games():
    seen = set()
    for seed in (4, 5, 6):
        g, _, _ = play_with_diplomacy(seed, n_players=4 + seed % 3, turns=45)
        assert g.finished
        seen |= {d["status"] for d in g.deals.values()}
        seen |= {e["type"] for e in g._dip_feed}
        json.dumps(g.spectator_view(full=True))
        for p in g.players:
            json.dumps(g.player_view(p.id))
    assert {"accepted", "countered", "rejected", "withdrawn", "expired", "failed"} <= seen, seen
    assert {"contract_paid", "deal_executed", "say"} <= seen, seen


@pytest.mark.parametrize("bad", ["²", "1" * 5000, "٣", "-" + "9" * 20])
def test_odd_number_strings_are_validation_errors(bad):
    """Review finding: int() raised a bare ValueError ('internal error')."""
    with pytest.raises(D.DealError, match="must be an integer"):
        D.parse_bundle({"gold": bad})
    g = world()
    r = g.diplomacy("p1", [{"type": "propose", "to": "p2", "give": {"gold": bad}}])[0]
    assert not r["ok"] and "gold must be an integer" in r["error"]
    errs = g.submit_orders("p1", [{"type": "recruit", "at": [2, 2], "unit": "infantry", "count": bad},
                                  {"type": "propose", "to": "p2", "give": {"gold": bad}}])
    assert [e["index"] for e in errs] == [0, 1]
    assert all("must be an integer" in e["error"] for e in errs), errs
