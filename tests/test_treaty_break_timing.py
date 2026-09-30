"""Turn T breaks allow entry on T+1, including peace signed by live deals."""
import pytest

from agentciv.engine import constants as C
from agentciv.engine.testing import events_of, run_turn, sandbox


def treaty_world(path):
    g = sandbox(3)
    g.add_city(2, 2, "p1", capital=True)
    g.add_city(12, 2, "p2", capital=True)
    g.add_city(2, 12, "p3", capital=True)
    if path == "orders":
        run_turn(g, {"p1": [{"type": "propose_treaty", "to": "p2", "turns": 20}]})
        run_turn(g, {"p2": [{"type": "accept_treaty", "from": "p1"}]})
    else:
        proposal = g.diplomacy("p1", [{"type": "propose", "to": "p2", "peace": 20}])[0]
        assert proposal["ok"]
        accepted = g.diplomacy("p2", [{"type": "accept", "deal": proposal["deal"]}])[0]
        assert accepted["ok"]
    assert g.treaty("p1", "p2")
    g.set_owner(7, 2, "p2")
    g.place_units(6, 2, "p1", {"infantry": 3})
    g.player("p1").resources["influence"] = C.TREATY_BREAK_COST + 10
    return g


@pytest.mark.parametrize("path", ["orders", "live_deal"])
def test_break_on_turn_t_allows_entry_on_t_plus_one(path):
    g = treaty_world(path)
    turn_t = g.turn
    move = {"type": "move", "from": [6, 2], "to": [7, 2]}
    # The submitted break is queued; it has not removed the treaty yet.
    errors = g.submit_orders("p1", [{"type": "break_treaty", "with": "p2"}, move])
    assert len(errors) == 1 and errors[0]["index"] == 1
    assert "treaty partner" in errors[0]["error"]
    assert g.treaty("p1", "p2")

    broken = run_turn(g)
    assert events_of(broken, "treaty_broken") == [
        {"turn": turn_t, "type": "treaty_broken", "by": "p1", "with": "p2"},
    ]
    assert not g.treaty("p1", "p2")
    assert g.owner[g.idx(7, 2)] == "p2"
    assert g.armies[g.idx(6, 2)]["p1"] == {"infantry": 3}

    assert g.turn == turn_t + 1
    # The first turn after the break accepts and executes the entry.
    assert g.submit_orders("p1", [move]) == []
    moved = run_turn(g)
    assert not events_of(moved, "order_failed")
    assert g.owner[g.idx(7, 2)] == "p1"
    assert g.armies[g.idx(7, 2)]["p1"] == {"infantry": 3}
    assert g.idx(6, 2) not in g.armies
    captured = events_of(moved, "tile_captured")
    assert any(e["turn"] == turn_t + 1 and (e["x"], e["y"]) == (7, 2) for e in captured)


def test_live_diplomacy_cannot_break_a_peace_deal():
    g = treaty_world("live_deal")
    before = g.player("p1").resources["influence"]
    result = g.diplomacy("p1", [{"type": "break_treaty", "with": "p2"}])[0]
    assert result["ok"] is False
    assert "unknown diplomacy action 'break_treaty'" in result["error"]
    assert g.treaty("p1", "p2")
    assert g.player("p1").resources["influence"] == before
    assert g.player("p1").betrayals == 0
