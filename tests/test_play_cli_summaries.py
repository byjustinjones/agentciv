"""Compact CLI summaries, saved changes, and alerts from player-visible data."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentciv.client import order_warnings, summarize_compact, summarize_view, view_alerts, view_changes
from agentciv.engine.testing import new_game, run_turn


def alert_view():
    view = new_game(3).player_view("p1")
    view["turn"] = 20
    view["map"]["relics"] = [{"x": 5, "y": 5, "owner": "p1", "guarded": False}]
    view["cities"] = [{"x": 2, "y": 2, "owner": "p1", "name": "Home", "buildings": {"walls": 2}}]
    view["armies"] = [{"x": 5, "y": 4, "owner": "p2", "units": {"infantry": 1}},
                      {"x": 3, "y": 2, "owner": "p2", "units": {"archer": 2}},
                      {"x": 2, "y": 2, "owner": "p1", "units": {"infantry": 3}}]
    view["treaties"] = [{"a": "p3", "b": "p1", "until_turn": 22}]
    view["events"] = [{"type": "treaty_broken", "by": "p2", "with": "p1", "turn": 19}]
    view["treaty_cooldowns"] = [{"a": "p1", "b": "p2", "until_turn": 34}]
    for field, value in (("relic_streak", 4), ("economic_streak", 2), ("influence_streak", 1)):
        view["players"][1][field] = value
    view["you"]["resources"].update(food=1, gold=2)
    view["you"]["income"]["food"] = 2
    view["you"]["upkeep"] = 4
    view["contracts"] = [{"id": "k1", "payer": "p1", "payee": "p2", "per_turn": {"gold": 3}, "turns_left": 2}]
    return view


def test_each_alert_and_completion_turn():
    lines = view_alerts(alert_view())
    assert lines == [
        "Your relic [5,5] has no units of yours.",
        "Treaty with p3 ends on turn 22.",
        "Treaty slots full: 1 of 1 in use; no new treaty can be signed (renewals excepted).",
        "Treaty cooldown with p2 until turn 34: no treaty with p2 can be signed before then.",
        "p2 stack [5,4] adjacent to your relic [5,5]; no treaty.",
        "p2 stack [3,2] adjacent to your city [2,2]; no treaty.",
        "Treaty p2–p1 broken on turn 19.",
        "p2 relic streak 4/16; completes at end of turn 31 if maintained.",
        "p2 economic streak 2/10; completes at end of turn 27 if maintained.",
        "p2 influence streak 1/10; completes at end of turn 28 if maintained.",
        "Contract k1 next instalment: 3 gold; holding 2.",
        "Next resolution food: 1 + 2 income - 4 upkeep = -1.",
    ]


def test_alert_exclusions_and_boundaries():
    view = alert_view()
    view["armies"] += [{"x": 5, "y": 5, "owner": "p1", "units": {"infantry": 1}}]
    view["treaties"] = [{"a": "p1", "b": "p2", "until_turn": 23}]
    view["events"] = [{"type": "treaty_broken", "by": "p2", "with": "p3", "turn": 19}]
    view["treaty_cooldowns"] = [{"a": "p2", "b": "p3", "until_turn": 34}, {"a": "p1", "b": "p3", "until_turn": 20}]
    view["you"]["treaty"]["slots"] = 2
    for p in view["players"]:
        p.update(relic_streak=0, economic_streak=0, influence_streak=0)
    view["you"]["resources"].update(food=2, gold=3)
    view["contracts"] += [{"id": "k2", "payer": "p2", "payee": "p1", "per_turn": {"gold": 999}, "turns_left": 1},
                          {"id": "k3", "payer": "p1", "payee": "p2", "per_turn": {"gold": 999}, "turns_left": 0}]
    assert view_alerts(view) == []
    view["treaties"][0]["until_turn"] = 20
    assert view_alerts(view) == ["Treaty with p2 ends on turn 20."]
    view["treaties"] = []
    view["armies"] = [a for a in view["armies"] if a["owner"] == "p1"] + [
        {"x": 3, "y": 3, "owner": "p2", "units": {"infantry": 1}},  # diagonal
        {"x": 2, "y": 3, "owner": "p3", "units": {"infantry": 0}},  # empty
    ]
    assert view_alerts(view) == []


def test_streak_completion_matches_engine_resolution():
    g = new_game(3)
    g.player("p1").bank = 10000
    run_turn(g)
    line = next(s for s in view_alerts(g.player_view("p1")) if "economic streak" in s)
    assert "1/10; completes at end of turn 9" in line
    for _ in range(9):
        run_turn(g)
    assert g.result["condition"] == "economic" and g.result["turn"] == 9


def test_fog_uses_current_visible_armies_only():
    from test_fog_clients import fog_view
    _, view = fog_view()
    # A remembered stack can be adjacent, but is not a currently visible army.
    city = next(c for c in view["cities"] if c["owner"] == "p1")
    view["sightings"] = [{"owner": "p2", "x": city["x"] + 1, "y": city["y"], "units": {"siege": 999}}]
    before = view_alerts(view)
    view["sightings"] = []
    assert view_alerts(view) == before
    text = summarize_compact(view)
    other = next(s for s in text.splitlines() if "p2 P2:" in s)
    assert "score ?" in other and "military" not in other and "bank 0" in other
    assert "999" not in text and "None" not in text


def test_compact_has_requested_fields_and_one_line_per_city_army_player():
    view = alert_view()
    text = summarize_compact(view)
    assert "Turn 20/150 (running); season" in text and "deadline none" in text
    assert "income in parentheses; upkeep 4 food" in text
    assert "Home [2,2]: walls 2; wonder 0" in text
    assert "[2,2]: 3 infantry" in text
    row = next(line for line in text.splitlines() if "p2 P2:" in line)
    for field in ("score", "cities", "tiles", "military", "relics held", "guarded", "relic streak",
                  "wonder", "bank", "legacy", "economic streak", "influence streak"):
        assert field in row
    assert "Your treaties (1/1 slots): p3 ends t22." in text
    assert "Treaty cooldowns: p2 until t34." in text
    assert "Market (gold/unit): food" in text
    assert "Changes since your last turn:" in text and "Treaty p2–p1 broken" in text
    assert len(text) < len(summarize_view(view)) / 2


def test_changes_events_snapshots_and_no_duplicates():
    view = alert_view()
    _, previous = view_changes(view)
    view["turn"] += 1
    view["map"]["relics"][0]["owner"] = "p2"
    view["players"][1]["relic_streak"] = 0
    view["events"] = [
        {"type": "tile_captured", "relic": True, "x": 5, "y": 5, "from": "p1", "to": "p2", "turn": 20},
        {"type": "city_captured", "city": "Home", "x": 2, "y": 2, "from": "p1", "to": "p2", "turn": 20},
        {"type": "treaty_signed", "a": "p2", "b": "p3", "until_turn": 30, "turn": 20},
        {"type": "treaty_expired", "a": "p1", "b": "p3", "turn": 20},
        {"type": "eliminated", "player": "p1", "turn": 20},
        {"type": "streak_started", "player": "p2", "condition": "economic", "turn": 20},
        {"type": "streak_ended", "player": "p3", "condition": "influence", "turn": 20},
        {"type": "tile_captured", "relic": False, "x": 9, "y": 9, "from": "p1", "to": "p2", "turn": 20},
    ]
    lines, snapshot = view_changes(view, previous)
    assert len(lines) == 8
    text = "\n".join(lines)
    for part in ("Relic [5,5] owner p1 → p2", "City Home", "Treaty p2–p3 signed; ends on turn 30",
                 "Treaty p1–p3 expired", "p1 eliminated", "p2 economic streak started",
                 "p3 influence streak ended", "p2 relic streak ended"):
        assert part in text
    assert "9,9" not in text
    assert view_changes(view, snapshot)[0] == []
    # Public relic ownership/streaks still change when a fog-hidden capture event is absent.
    view["turn"] += 2
    view["events"] = []
    view["map"]["relics"][0]["owner"] = "p3"
    view["players"][2]["relic_streak"] = 1
    lines, _ = view_changes(view, snapshot)
    assert "intervening events" in lines[0]
    assert "Relic [5,5] owner p2 → p3 (since saved view)." in lines
    assert "p3 relic streak started (since saved view)." in lines


@pytest.mark.parametrize("command", ["state", "next"])
@pytest.mark.parametrize("compact", [False, True])
def test_cli_alerts_first_and_persisted_changes(tmp_path, monkeypatch, capsys, command, compact):
    path = Path(__file__).resolve().parent.parent / "examples" / "play_cli.py"
    spec = importlib.util.spec_from_file_location("play_cli_summary_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HOME = tmp_path
    view = alert_view()
    client = SimpleNamespace(player_id="p1", state=lambda: view)
    monkeypatch.setattr(mod, "_client", lambda name: (client, json.loads((tmp_path / "A.json").read_text())))
    mod._save("A", {"game_id": "test1", "player_id": "p1", "token": "unused"})
    args = [command, "A"] + (["--compact"] if compact else [])
    mod.main(args)
    output = capsys.readouterr().out
    prefix = "ALERTS:\n" + "\n".join("ALERT: " + line for line in view_alerts(view)) + "\n"
    assert output.startswith(prefix)
    if compact:
        assert "Changes since your last turn:" in output
    else:
        assert output == prefix + summarize_view(view) + "\n"
    saved = json.loads((tmp_path / "A.json").read_text())
    assert saved["seen_turn"] == 20 and saved["summary_snapshot"]["turn"] == 20
    mod.main(["state", "A", "--compact"])
    assert "Changes since your last turn:\n  No changes in this view." in capsys.readouterr().out


def test_market_sell_warning_ignores_later_income():
    view = alert_view()
    view["you"]["resources"]["wood"] = 5
    view["you"]["income"]["wood"] = 100
    sell = {"type": "market", "side": "sell", "resource": "wood", "qty": 6}
    warnings = order_warnings(view, [sell])
    assert any("exceeds current stock 5" in s and "before this turn's income (step 7)" in s for s in warnings)
    sell["qty"] = 5
    assert not any("exceeds current stock" in s for s in order_warnings(view, [sell]))


def test_treaty_alerts_bonds_and_warnings_from_the_engine():
    """Slots, bonds, cooldowns and break previews come from the engine view (rules §9)."""
    from agentciv.engine import constants as C
    g = new_game(5)
    run_turn(g, {"p1": [{"type": "propose_treaty", "to": "p2", "turns": 20, "bond": 0}]})
    g.player("p1").bank = 200
    run_turn(g, {"p2": [{"type": "accept_treaty", "from": "p1"}]})
    g.diplomacy("p1", [{"type": "propose", "to": "p3", "give": {"bond": 40}, "peace": 30}])
    g.diplomacy("p3", [{"type": "accept", "deal": "d1"}])
    view = g.player_view("p1")
    assert view["you"]["treaty"]["slots"] == 2 and view["you"]["treaty"]["held"] == 2
    assert "Treaty slots full: 2 of 2 in use; no new treaty can be signed (renewals excepted)." in view_alerts(view)
    text = summarize_compact(view)
    assert "Your treaties (2/2 slots): p2 ends t21; p3 ends t32, bonds you 40 / p3 0." in text
    full = summarize_view(view)
    assert "(2/2 slots used; unpledged bank 160, required bond 0)" in full
    assert f"breaking it now: {C.TREATY_BREAK_COST} influence, legacy -0, 60 gold to p3" in full
    w = order_warnings(view, [{"type": "propose_treaty", "to": "p4", "turns": 20},
                              {"type": "release_treaty", "with": "p2"}])
    assert any("propose_treaty with p4 will FAIL: you hold 2 of 2 treaty slots" in s for s in w)
    assert any("only if p2 also orders release_treaty" in s for s in w)
    # a break: cooldown alert for both, bond alert for the breaker without a bank
    g.player("p1").resources["influence"] = 200
    g.player("p1").bank = 0
    run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    view = g.player_view("p1")
    lines = view_alerts(view)
    until = 2 + C.TREATY_RESIGN_COOLDOWN
    assert f"Treaty cooldown with p2 until turn {until}: no treaty with p2 can be signed before then." in lines
    assert (f"Required treaty bond {C.TREATY_BOND_PER_BETRAYAL} exceeds your unpledged bank 0: "
            "no treaty can be signed or renewed.") in lines
    assert f"Treaty cooldowns: p2 until t{until}." in summarize_compact(view)
    assert any("will FAIL: no treaty with p2 can be signed before turn" in s
               for s in order_warnings(view, [{"type": "propose_treaty", "to": "p2", "turns": 20}]))
