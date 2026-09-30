"""Fog of war (games created with fog: true; docs/RULES.md §14): vision,
redaction, sightings, event scoping, deals and order validation."""
import json
import random
import re
from pathlib import Path

import pytest

from agentciv.bots import get_bot
from agentciv.engine import constants as C
from agentciv.engine import fog as F
from agentciv.engine.orders import prevalidate
from agentciv.engine.testing import new_game, run_turn, sandbox

from test_engine_views import PLAYER_KEYS

ROOT = Path(__file__).resolve().parent.parent
HIDDEN = set(C.FOG_HIDDEN_FIELDS)


def world(n=3):
    """p1 (2,2), p2 (12,2), p3 (2,12) capitals on an 18x18 plain; p1 sees x,y in 0..4."""
    g = sandbox(n, fog=True)
    g.relics, g.relic_set = [], frozenset()     # no relics: keep the geometry simple
    g.add_city(2, 2, "p1", capital=True)
    g.add_city(12, 2, "p2", capital=True)
    if n >= 3:
        g.add_city(2, 12, "p3", capital=True)
    return g


def square(g, x0, y0, x1, y1):
    return {g.idx(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}


def row(view, pid):
    return next(p for p in view["players"] if p["id"] == pid)


def types(view):
    return [e["type"] for e in view["events"]]


def army_tiles(view, owner=None):
    return {(a["x"], a["y"]) for a in view["armies"] if owner is None or a["owner"] == owner}


# ============================================================ vision
def test_vision_radii():
    g = world()
    base = square(g, 0, 0, 4, 4)                         # city radius 2 (= territory 1..3 radius 1)
    assert F.vision(g, "p1") == base
    g.set_owner(8, 8, "p1")                              # territory: radius 1
    assert F.vision(g, "p1") == base | square(g, 7, 7, 9, 9)
    g.place_units(15, 15, "p1", {"infantry": 1})         # units: radius 1
    assert F.vision(g, "p1") == base | square(g, 7, 7, 9, 9) | square(g, 14, 14, 16, 16)
    g.place_units(10, 15, "p1", {"infantry": 1, "cavalry": 1})   # cavalry: radius 2
    assert F.vision(g, "p1") >= square(g, 8, 13, 12, 17)
    assert F.vision(g, None) == frozenset()
    g.player("p3").alive = False
    assert F.vision(g, "p3") == frozenset()
    assert set(F.vision_all(g)) == {"p1", "p2"}


def test_band_edges():
    E, I = C.ECONOMIC_VICTORY_GOLD, C.INFLUENCE_VICTORY
    assert F.band(6749, E) == 0.4 and F.band(6750, E) == 0.5
    assert F.band(E - 1, E) == 0.9 and F.band(E, E) == 1.0 and F.band(10 * E, E) == 1.0
    assert F.band(-50, E) == 0.0 and F.band(0, E) == 0.0
    assert F.band(334, I) == 0.0 and F.band(335, I) == 0.1 and F.band(I - 1, I) == 0.9 and F.band(I, I) == 1.0


# ============================================================ player rows and view shape
def test_own_row_complete_rival_rows_redacted():
    g = world()
    g.player("p2").resources["gold"] = 6750
    g.player("p2").resources["influence"] = 1000
    full = g.spectator_view(full=True)
    v = g.player_view("p1")
    json.dumps(v)
    me = row(v, "p1")
    assert me["fogged"] is False and all(me[k] is not None for k in HIDDEN)
    assert me == dict(row(full, "p1"), fogged=False)
    for q in ("p2", "p3"):
        r, true = row(v, q), row(full, q)
        assert PLAYER_KEYS <= set(r) and r["fogged"] is True
        assert {k for k in r if r[k] is None and true.get(k) is not None} == HIDDEN
        for k in PLAYER_KEYS - HIDDEN - {"victory_progress", "reputation"}:
            assert r[k] == true[k], k
        assert r["reputation"] == dict(true["reputation"])
        assert {k: v_ for k, v_ in r["victory_progress"].items() if k not in ("economic", "influence")} == \
               {k: v_ for k, v_ in true["victory_progress"].items() if k not in ("economic", "influence")}
    assert row(v, "p2")["victory_progress"]["economic"] == 0.5
    assert row(v, "p2")["victory_progress"]["influence"] == 0.2
    assert row(full, "p2")["victory_progress"]["economic"] == round(6750 / C.ECONOMIC_VICTORY_GOLD, 3)
    assert v["you"]["counterintel"] == {"pool": 0, "rating": C.CI_BASE + C.CI_PER_CITY}
    assert me["reputation"]["spy_incidents"] == 0
    fog = v["fog"]
    assert fog["enabled"] and fog["active"] and fog["hidden_fields"] == list(C.FOG_HIDDEN_FIELDS)
    assert fog["visible_tiles"] == len(F.vision(g, "p1")) == 25
    vis = v["map"]["visible"]
    assert len(vis) == 18 and vis[0] == "11111" + "0" * 13 and vis[5] == "0" * 18
    assert v["sightings"] == [] and v["intel"] == []


def test_standard_game_views_have_no_fog_keys():
    g = new_game(3)
    v = g.player_view("p1")
    assert not ({"fog", "sightings", "intel"} & set(v))
    assert "fogged" not in v["players"][0] and "counterintel" not in v["you"]
    assert "visible" not in v["map"] and "spy_incidents" not in v["players"][0]["reputation"]
    assert all(p["resources"] is not None for p in v["players"])


def test_armies_filtered_by_sight():
    g = world()
    g.place_units(4, 4, "p2", {"infantry": 2})     # in p1's sight
    g.place_units(9, 9, "p2", {"archer": 1})       # not
    g.place_units(12, 2, "p2", {"infantry": 1})    # p2 capital, not
    v = g.player_view("p1")
    assert army_tiles(v, "p2") == {(4, 4)}
    assert army_tiles(g.player_view("p2"), "p2") == {(4, 4), (9, 9), (12, 2)}
    assert army_tiles(g.spectator_view(full=True)) == {(4, 4), (9, 9), (12, 2)}


def test_spectator_is_fogged_without_sight_and_full_view_is_not():
    g = world()
    g.place_units(4, 4, "p2", {"infantry": 2})
    pub = g.spectator_view()
    assert pub["you"] is None and pub["armies"] == [] and pub["sightings"] == [] and pub["intel"] == []
    assert all(p["fogged"] and p["resources"] is None and p["score"] is None for p in pub["players"])
    assert "visible" not in pub["map"] and pub["fog"]["active"] and pub["fog"]["visible_tiles"] == 0
    full = g.spectator_view(full=True)
    assert full["armies"] and all(not p["fogged"] and p["resources"] is not None for p in full["players"])
    assert not full["fog"]["active"]


def test_fog_lifts_when_the_game_ends():
    g = world()
    g.max_turns = 2
    g.place_units(9, 9, "p2", {"archer": 1})
    run_turn(g, {"p2": [{"type": "recruit", "city": [12, 2], "unit": "infantry", "count": 1}]})
    assert not g.finished
    run_turn(g, {"p2": [{"type": "recruit", "city": [12, 2], "unit": "infantry", "count": 1}]})
    assert g.finished and not g.fog
    v = g.player_view("p1")
    assert not v["fog"]["active"] and all(not p["fogged"] and p["resources"] is not None for p in v["players"])
    assert (9, 9) in army_tiles(v, "p2")
    assert "recruit" in types(v)        # the last turn's out-of-sight events are shown too
    assert "visible" not in v["map"]
    pub = g.spectator_view()
    assert (9, 9) in army_tiles(pub) and all(p["score"] is not None for p in pub["players"])


# ============================================================ sightings
def test_sightings_remember_leave_and_expire():
    g = world()
    g.place_units(8, 8, "p1", {"cavalry": 1})       # sees 6..10
    g.place_units(9, 9, "p2", {"infantry": 3})
    assert (9, 9) in army_tiles(g.player_view("p1"), "p2")
    # the cavalry withdraws two steps: (9,9) leaves p1's sight during the turn
    run_turn(g, {"p1": [{"type": "move", "from": [8, 8], "path": [[7, 8], [6, 8]]}]})
    v = g.player_view("p1")
    assert (9, 9) not in army_tiles(v, "p2")
    assert v["sightings"] == [{"x": 9, "y": 9, "owner": "p2", "units": {"infantry": 3}, "turn": 0}]
    assert g.player_view("p3")["sightings"] == []
    for _ in range(C.FOG_SIGHTING_TURNS - 1):
        run_turn(g)
    assert g.player_view("p1")["sightings"][0]["turn"] == 0
    run_turn(g)
    assert g.player_view("p1")["sightings"] == []          # older than FOG_SIGHTING_TURNS


def test_sighting_cleared_when_tile_seen_empty():
    g = world()
    g.place_units(8, 8, "p1", {"cavalry": 1})
    g.place_units(9, 9, "p2", {"infantry": 3})
    run_turn(g, {"p1": [{"type": "move", "from": [8, 8], "path": [[7, 8], [6, 8]]}],
                 "p2": [{"type": "move", "from": [9, 9], "to": [10, 9]}]})
    assert [(s["x"], s["y"]) for s in g.player_view("p1")["sightings"]] == [(9, 9)]
    run_turn(g, {"p1": [{"type": "move", "from": [6, 8], "path": [[7, 8], [8, 8]]}]})
    v = g.player_view("p1")
    assert (10, 9) in army_tiles(v, "p2") and v["sightings"] == []   # in sight again
    run_turn(g)
    assert 9 * 18 + 9 not in g.sightings["p1"]          # seen empty at the start of a turn: forgotten


# ============================================================ events
def test_out_of_sight_recruit_is_hidden():
    g = world()
    run_turn(g, {"p2": [{"type": "recruit", "city": [12, 2], "unit": "infantry", "count": 2}]})
    assert "recruit" not in types(g.player_view("p1"))
    assert "recruit" not in types(g.spectator_view())
    assert "recruit" in types(g.player_view("p2"))
    assert "recruit" in types(g.spectator_view(full=True))


def test_recruit_seen_from_pre_turn_sight():
    g = world()
    g.place_units(10, 3, "p1", {"cavalry": 1})      # sees (12, 2)
    run_turn(g, {"p1": [{"type": "move", "from": [10, 3], "path": [[9, 3], [8, 3]]}],
                 "p2": [{"type": "recruit", "city": [12, 2], "unit": "infantry", "count": 2}]})
    assert g.idx(12, 2) not in F.vision(g, "p1")
    assert "recruit" in types(g.player_view("p1"))
    assert "recruit" not in types(g.player_view("p3"))


def test_clash_seen_through_its_to_tile():
    g = world()
    g.place_units(9, 9, "p2", {"infantry": 3})
    g.place_units(10, 9, "p3", {"infantry": 2})
    g.place_units(11, 10, "p1", {"infantry": 1})    # sees (10, 9), not (9, 9)
    assert g.idx(9, 9) not in F.vision(g, "p1")
    run_turn(g, {"p2": [{"type": "move", "from": [9, 9], "to": [10, 9]}],
                 "p3": [{"type": "move", "from": [10, 9], "to": [9, 9]}]})
    clash = [e for e in g.player_view("p1")["events"] if e["type"] == "battle" and e["clash"]]
    assert clash and clash[0]["x"] == 9 and clash[0]["to"] == [10, 9]
    assert not [e for e in g.spectator_view()["events"] if e["type"] == "battle"]


def test_market_and_starvation_are_private():
    g = world()
    g.player("p2").resources["food"] = 0
    g.place_units(12, 2, "p2", {"infantry": 40})
    run_turn(g, {"p2": [{"type": "market", "side": "sell", "resource": "wood", "qty": 10}]})
    for et in ("market", "starvation"):
        assert et in types(g.player_view("p2"))
        assert et not in types(g.player_view("p1")) and et not in types(g.spectator_view())


def test_city_captured_plunder_only_for_the_parties():
    g = world()
    g.place_units(12, 3, "p1", {"infantry": 50})
    run_turn(g, {"p1": [{"type": "move", "from": [12, 3], "to": [12, 2]}]})
    cap = {pid: [e for e in g.player_view(pid)["events"] if e["type"] == "city_captured"]
           for pid in ("p1", "p2", "p3")}
    assert cap["p1"][0]["plunder"] and cap["p2"][0]["plunder"] == cap["p1"][0]["plunder"]
    assert cap["p3"] and "plunder" not in cap["p3"][0] and cap["p3"][0]["to"] == "p1"
    assert "eliminated" in types(g.player_view("p3"))


def test_deal_executed_bundles_only_for_the_parties():
    g = world()
    res = g.diplomacy("p1", [{"type": "propose", "to": "p2", "give": {"wood": 30}, "get": {"gold": 20}}])
    assert g.diplomacy("p2", [{"type": "accept", "deal": res[0]["deal"]}])[0]["ok"]
    ex3 = [e for e in g.inbox("p3")["items"] if e["type"] == "deal_executed"]
    ex1 = [e for e in g.inbox("p1")["items"] if e["type"] == "deal_executed"]
    assert ex3 and not ({"give", "get", "contracts"} & set(ex3[0])) and ex3[0]["from"] == "p1"
    assert ex1 and ex1[0]["give"] == {"wood": 30}
    run_turn(g)
    ev3 = [e for e in g.player_view("p3")["events"] if e["type"] == "deal_executed"][0]
    assert "give" not in ev3 and "get" not in ev3
    assert [e for e in g.player_view("p2")["events"] if e["type"] == "deal_executed"][0]["get"] == {"gold": 20}
    log3 = g.player_view("p3")["deals"]["log"][0]
    assert set(log3) == {"id", "turn", "from", "to", "peace"}
    assert g.player_view("p1")["deals"]["log"][0]["give"] == {"wood": 30}
    assert "give" not in g.spectator_view()["deals"]["log"][0]
    assert g.spectator_view(full=True)["deals"]["log"][0]["give"] == {"wood": 30}


def test_contracts_and_defaults_redacted_for_non_parties():
    g = world()
    res = g.diplomacy("p1", [{"type": "propose", "to": "p2", "give": {"per_turn": {"gold": 40}, "turns": 5},
                              "get": {"wood": 10}}])
    assert g.diplomacy("p2", [{"type": "accept", "deal": res[0]["deal"]}])[0]["ok"]
    assert g.player_view("p1")["contracts"][0]["per_turn"] == {"gold": 40}
    assert "per_turn" not in g.player_view("p3")["contracts"][0]
    g.player("p1").resources["gold"] = 0
    run_turn(g)
    d3 = [e for e in g.player_view("p3")["events"] if e["type"] == "contract_default"]
    d2 = [e for e in g.player_view("p2")["events"] if e["type"] == "contract_default"]
    assert d3 and not ({"per_turn", "penalty", "debt"} & set(d3[0])) and d3[0]["payer"] == "p1"
    assert d2 and d2[0]["per_turn"] == {"gold": 40} and d2[0]["penalty"] > 0


def _emit_types():
    out = set()
    for name in ("game.py", "deals.py"):
        src = (ROOT / "agentciv" / "engine" / name).read_text()
        out |= set(re.findall(r'_emit\(\s*(?:g,\s*)?"([a-z_]+)"', src))
    return out


def test_every_emit_type_has_fog_policy():
    found = _emit_types()
    assert {"battle", "recruit", "deal_executed", "spy_report", "spy_incident"} <= found
    assert not found - set(F.EVENT_POLICY), f"event types without a fog policy: {found - set(F.EVENT_POLICY)}"
    assert set(F.EVENT_POLICY.values()) <= {"emitted", "public", "player", "local"}


def test_fog_bot_game_scopes_every_non_public_event():
    g = new_game(5, seed=4, max_turns=30, fog=True)
    bots = {p.id: get_bot(name, seed=k) for k, (p, name) in
            enumerate(zip(g.players, ("rusher", "random", "strategist", "turtle", "economist")))}
    public = {t for t, pol in F.EVENT_POLICY.items() if pol in ("public", "emitted")}
    seen = set()
    while not g.finished:
        for pid in g.alive_players():
            g.submit_orders(pid, bots[pid].act(g.player_view(pid)))
        g.step()
        for e in g.last_events:
            seen.add(e["type"])
            assert "_vis" in e or "_fog" in e or e["type"] in public, e
            if e["type"] in ("market", "starvation", "recruit", "disband", "battle"):
                assert "_fog" in e
        if not g.finished:
            for pid in g.alive_players():
                v = g.player_view(pid)
                assert all(r["resources"] is None for r in v["players"] if r["id"] != pid)
    assert "recruit" in seen


# ============================================================ deals
P12 = {"type": "propose", "to": "p2", "give": {"wood": 30}, "get": {"gold": 999}}


def test_open_deal_problem_never_names_counterparty_shortfall():
    g = world()
    g.diplomacy("p1", [P12])
    mine = g.player_view("p1")["deals"]["open"][0]
    assert mine["problem"] is None and mine["deliverable"] is True
    theirs = g.player_view("p2")["deals"]["open"][0]
    assert theirs["deliverable"] is False and "p2 is short of" in theirs["problem"]
    g.player("p1").resources["wood"] = 0
    assert "p1 is short of" in g.player_view("p1")["deals"]["open"][0]["problem"]


def test_open_deal_view_independent_of_counterparty_stock():
    views = []
    for gold in (0, 10 ** 5):
        g = world()
        g.player("p2").resources["gold"] = gold
        g.diplomacy("p1", [P12])
        views.append(g.player_view("p1")["deals"]["open"][0])
    assert views[0] == views[1]


def test_open_deal_units_checked_only_in_sight():
    g = world()
    g.set_owner(13, 5, "p2")                   # p2's tile far from p1
    g.set_owner(5, 2, "p1")
    g.set_owner(6, 2, "p2")                    # p2's tile next to p1's land, in sight
    g.place_units(6, 2, "p3", {"infantry": 1})
    g.diplomacy("p1", [{"type": "propose", "to": "p2", "give": {"wood": 5}, "get": {"tiles": [[6, 2]]}}])
    assert "holds units of p3" in g.player_view("p1")["deals"]["open"][0]["problem"]


def test_accept_failure_reason_is_generic_for_hidden_causes():
    g = world()
    res = g.diplomacy("p1", [{"type": "propose", "to": "p2", "give": {"wood": 30}, "get": {"gold": 20}}])
    g.player("p1").resources["wood"] = 0       # the proposer cannot pay
    out = g.diplomacy("p2", [{"type": "accept", "deal": res[0]["deal"]}])[0]
    assert not out["ok"] and out["error"].endswith("p1 cannot deliver the agreed terms")
    rec = g.player_view("p1")["deals"]["recent"][0]
    assert rec["status"] == "failed" and rec["reason"] == "p1 cannot deliver the agreed terms"
    failed = [e for e in g.inbox("p1")["items"] if e["type"] == "deal_failed"]
    assert failed[0]["reason"] == "p1 cannot deliver the agreed terms"
    # the accepter's own shortfall is reported exactly to the accepter only
    res = g.diplomacy("p1", [{"type": "propose", "to": "p2", "give": {"stone": 5}, "get": {"gold": 999}}])
    out = g.diplomacy("p2", [{"type": "accept", "deal": res[0]["deal"]}])[0]
    assert "p2 is short of" in out["error"]
    assert g.player_view("p1")["deals"]["recent"][0]["reason"] == "p2 cannot deliver the agreed terms"


def test_accept_failure_public_cause_is_exact():
    g = world()
    g.set_owner(5, 2, "p1")
    g.set_owner(6, 2, "p2")
    res = g.diplomacy("p1", [{"type": "propose", "to": "p2", "give": {"wood": 5}, "get": {"tiles": [[6, 2]]}}])
    g.set_owner(6, 2, "p3")                    # the tile changed hands (public)
    out = g.diplomacy("p2", [{"type": "accept", "deal": res[0]["deal"]}])[0]
    assert "not owned by p2" in out["error"]
    assert "not owned by p2" in g.player_view("p1")["deals"]["recent"][0]["reason"]


# ============================================================ order validation
def _random_state(seed):
    rng = random.Random(seed)
    g = world()
    for _ in range(30):
        q = rng.choice(["p1", "p2", "p3"])
        g.place_units(rng.randrange(18), rng.randrange(18), q,
                      {rng.choice(["infantry", "cavalry"]): rng.randint(1, 3)})
    if rng.random() < 0.5:
        g.treaties[("p1", "p3")] = 99
    return g, rng


def _random_moves(g, rng, n=40):
    mine = [i for i, per in g.armies.items() if "p1" in per]
    out = []
    for _ in range(n):
        src = rng.choice(mine)
        x, y = g.xy(src)
        path, cx, cy = [], x, y
        for _ in range(rng.choice([1, 2])):
            dx, dy = rng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
            cx, cy = cx + dx, cy + dy
            path.append([cx, cy])
        out.append({"type": "move", "from": [x, y], "path": path, "units": dict(g.armies[src]["p1"])})
    return out


@pytest.mark.parametrize("seed", range(8))
def test_move_validation_ignores_armies_out_of_sight(seed):
    g, rng = _random_state(seed)
    orders = _random_moves(g, rng)
    before = prevalidate(g, "p1", orders)[1]
    sight = F.vision(g, "p1")
    for i in list(g.armies):                   # reshuffle every other army outside p1's sight
        if i not in sight:
            g.armies[i] = {q: u for q, u in g.armies[i].items() if q == "p1"}
            if not g.armies[i]:
                del g.armies[i]
    for i in range(18 * 18):
        if i not in sight and rng.random() < 0.3:
            g.armies.setdefault(i, {})[rng.choice(["p2", "p3"])] = {"infantry": 1}
    assert prevalidate(g, "p1", orders)[1] == before


# ============================================================ determinism
def _play(seed):
    g = new_game(4, seed=seed, max_turns=12, fog=True)
    bots = {p.id: get_bot(n, seed=k) for k, (p, n) in enumerate(zip(g.players, ("rusher", "random", "turtle",
                                                                                   "strategist")))}
    out = []
    while not g.finished:
        for pid in g.alive_players():
            g.submit_orders(pid, bots[pid].act(g.player_view(pid)))
        g.step()
        out.append(json.dumps([g.player_view(p.id) for p in g.players] + [g.spectator_view()], sort_keys=True))
    return out


def test_fog_game_is_deterministic_and_json():
    a, b = _play(9), _play(9)
    assert a == b and len(a) == 12
