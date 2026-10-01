"""Player tooling facts, contract projections, and draft confirmation checks."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from agentciv.client import (AgentCivClient, _food_outlook, deal_warnings, order_warning_details,
                             order_warnings, summarize_compact, summarize_view, view_alerts)
from agentciv.mcp_server import AgentCivMCP


def tooling_view():
    return {
        "turn": 8, "max_turns": 150, "status": "running",
        "you": {"id": "p1", "resources": {"food": 300, "wood": 100, "stone": 100, "gold": 0},
                "income": {"food": 40}, "upkeep": 25, "market_fee": 0.05},
        "players": [{"id": "p1", "name": "One", "bank": 0, "economic_streak": 0},
                    {"id": "p2", "name": "Two", "bank": 0, "economic_streak": 0}],
        "cities": [], "armies": [], "events": [], "contracts": [], "treaties": [],
        "deals": {"open": []}, "map": {"relics": []},
        "victory": {"thresholds": {"bank": 500, "streak_turns": 10}},
        "costs": {"units": {"infantry": {"strength": 10, "upkeep": 2, "cost": {}}}},
        "market": {"pools": {r: {"resource": 1000, "gold": 2160} for r in ("food", "wood", "stone")}},
    }


def contract(cid="c1", payer="p1", payee="p2", turns=3, **per_turn):
    return {"id": cid, "payer": payer, "payee": payee, "turns_left": turns, "per_turn": per_turn}


def offer(give=None, get=None):
    return {"id": "d1", "from": "p2", "to": "p1", "give": give or {},
            "get": get if get is not None else {"per_turn": {"food": 400}, "turns": 3}}


def market_orders(sold="stone", qty=100):
    return [{"type": "market", "side": "buy", "resource": "wood", "qty": 40},
            {"type": "market", "side": "sell", "resource": sold, "qty": qty}]


@pytest.mark.parametrize("strength", [10, 13])
def test_city_alerts_use_chebyshev_distance_power_and_nearest_city(strength):
    view = tooling_view()
    view["costs"]["units"]["infantry"]["strength"] = strength
    view["cities"] = [{"owner": "p1", "name": "Far", "x": 3, "y": 3},
                      {"owner": "p1", "name": "Near", "x": 4, "y": 4},
                      {"owner": "p2", "name": "Other", "x": 7, "y": 7}]
    view["treaties"] = [{"a": "p1", "b": "p2", "until_turn": 20}]
    view["armies"] = [{"owner": "p3", "x": 5, "y": 6, "units": {"infantry": 4}},
                      {"owner": "p3", "x": 5, "y": 5, "units": {"infantry": 2}},
                      {"owner": "p2", "x": 6, "y": 6, "units": {"infantry": 9}},
                      {"owner": "p1", "x": 4, "y": 4, "units": {"infantry": 9}},
                      {"owner": "p3", "x": 5, "y": 4, "units": {"infantry": 0}},
                      {"owner": "p3", "x": 7, "y": 7, "units": {"infantry": 9}}]
    lines = view_alerts(view)
    assert len(lines) == 2
    assert (f"p3 stack [5,6] (4 infantry, power {4 * strength}) 2 tiles from your city [4,4]; no treaty."
            in lines)
    assert (f"p3 stack [5,5] (2 infantry, power {2 * strength}) 1 tile from your city [4,4]; no treaty."
            in lines)


def test_relic_alerts_still_require_orthogonal_adjacency():
    view = tooling_view()
    view["map"]["relics"] = [{"owner": "p1", "x": 10, "y": 10}]
    view["armies"] = [{"owner": "p1", "x": 10, "y": 10, "units": {"infantry": 1}},
                      {"owner": "p2", "x": 10, "y": 11, "units": {"infantry": 2}},
                      {"owner": "p2", "x": 11, "y": 11, "units": {"infantry": 3}},
                      {"owner": "p2", "x": 10, "y": 12, "units": {"infantry": 4}}]
    assert view_alerts(view) == ["p2 stack [10,11] adjacent to your relic [10,10]; no treaty."]


@pytest.mark.parametrize("participation", [
    {"sides": ["p2", "p1"], "losses": {"p2": {"archer": 3}}},
    {"sides": ["p2"], "losses": {"p1": {"infantry": 2}, "p2": {"archer": 3}}},
    {"attacker": "p1", "defender": "p2", "losses": {"p2": {"archer": 3}}},
    {"attacker": "p2", "defender": "p1", "losses": {"p2": {"archer": 3}}},
])
def test_compact_battles_include_each_participation_field(participation):
    view = tooling_view()
    view["events"] = [{"type": "battle", "turn": 7, "x": 5, "y": 6, "winner": "p1", **participation},
                      {"type": "battle", "turn": 6, "x": 1, "y": 2, "sides": ["p1", "p2"]},
                      {"type": "battle", "turn": 7, "x": 8, "y": 9, "sides": ["p2", "p3"]}]
    line = next(s for s in summarize_compact(view).splitlines() if s.startswith("Battles involving"))
    assert "[5,6] sides " in line and "p1" in line and "p2" in line
    assert "winner p1" in line and "losses " in line and "p2: 3 archer" in line
    assert "[1,2]" not in line and "[8,9]" not in line


def test_compact_battles_list_every_current_battle_and_clash_endpoints():
    view = tooling_view()
    view["events"] = [{"type": "battle", "turn": 7, "x": 2, "y": 3, "sides": ["p1", "p2"],
                       "winner": None, "losses": {"p1": {"infantry": 2}, "p2": {"cavalry": 1}}},
                      {"type": "battle", "turn": 7, "x": 5, "y": 6, "to": [6, 6], "clash": True,
                       "sides": ["p3", "p1"], "winner": "p3", "losses": {"p1": {"archer": 4}}}]
    line = next(s for s in summarize_compact(view).splitlines() if s.startswith("Battles involving"))
    for part in ("[2,3]", "winner none", "p1: 2 infantry", "p2: 1 cavalry",
                 "[5,6]–[6,6]", "winner p3", "p1: 4 archer"):
        assert part in line


@pytest.mark.parametrize("events", [[], [{"type": "battle", "turn": 6, "sides": ["p1", "p2"]}],
                                   [{"type": "battle", "turn": 7, "sides": ["p2", "p3"]}]])
def test_compact_battles_print_none_when_no_current_participation(events):
    view = tooling_view()
    view["events"] = events
    assert "Battles involving you last turn: none." in summarize_compact(view)


def test_food_forecast_subtracts_instalment_and_corrects_starvation():
    view = tooling_view()
    view["contracts"] = [contract(food=400)]
    text = summarize_view(view)
    assert "Food this turn: 300 + 40 income - 25 upkeep - 400 contract instalments = -85 at turn end." in text
    assert "about 43 unit(s) will starve" in text


def test_food_forecast_incoming_and_outgoing_are_separate_and_expired_ignored():
    view = tooling_view()
    view["contracts"] = [contract(food=400), contract("c2", "p2", "p1", food=90),
                         contract("c3", turns=0, food=999), contract("c4", "p2", "p1", turns=0, food=999),
                         contract("c5", "p2", "p3", food=999)]
    lines = _food_outlook(view, "p1", 300, 25)
    assert lines == ["Food this turn: 300 + 40 income - 25 upkeep - 400 contract instalments "
                     "+ 90 contract receipts = 5 at turn end."]


@pytest.mark.parametrize("turns, expected", [
    (5, "20/turn against 25 upkeep, -100 contract instalments: -105/turn, "
        "so stored food lasts about 7 turn(s) of winter"),
    (2, "20/turn against 25 upkeep: -5/turn, so stored food lasts about 166 turn(s) of winter"),
])
def test_next_season_food_accounts_for_contract_duration(turns, expected):
    view = tooling_view()
    view["season"] = {"name": "summer", "next": "winter", "turns_left": 2, "modifiers": {"food": 1.0}}
    view["costs"]["seasons"] = {"cycle": [{"name": "winter", "modifiers": {"food": 0.5}}]}
    view["contracts"] = [contract(turns=turns, food=100)]
    lines = _food_outlook(view, "p1", 1000, 25)
    assert expected in lines[1]


def test_next_season_food_includes_contract_receipts():
    view = tooling_view()
    view["season"] = {"name": "summer", "next": "winter", "turns_left": 2, "modifiers": {"food": 1.0}}
    view["costs"]["seasons"] = {"cycle": [{"name": "winter", "modifiers": {"food": 0.5}}]}
    view["contracts"] = [contract(turns=5, food=100), contract("c2", "p2", "p1", turns=5, food=10)]
    lines = _food_outlook(view, "p1", 1000, 25)
    assert "+ 10 contract receipts = 925 at turn end." in lines[0]
    assert ("20/turn against 25 upkeep, -100 contract instalments, +10 contract receipts: -95/turn, "
            "so stored food lasts about 8 turn(s) of winter" in lines[1])


def test_order_food_warning_accounts_for_instalments_and_new_upkeep():
    view = tooling_view()
    view["contracts"] = [contract(food=400)]
    warnings = order_warnings(view, [{"type": "recruit", "unit": "infantry", "count": 1}])
    assert len(warnings) == 1 and "about 44 unit(s) will starve" in warnings[0]
    assert order_warnings(view, []) == []  # retain the existing caller's trigger


def test_market_sequence_warning_names_buy_and_later_sale_without_duplicate():
    view = tooling_view()
    orders = market_orders()
    warnings, flags = order_warning_details(view, orders)
    assert warnings == ["market buy of 40 wood (about 95 gold) depends on gold from the stone sale, "
                        "but stone clears after wood (food, wood, stone clear in that order): "
                        "the buy will likely FAIL"]
    assert flags == {"market_sequencing"}
    assert order_warnings(view, orders) == warnings


def test_market_sequence_retains_generic_warning_when_later_sale_is_insufficient():
    warnings, flags = order_warning_details(tooling_view(), market_orders(qty=1))
    assert len(warnings) == 2
    assert "only about 0 gold is available when wood clears" in warnings[0]
    assert "gold from selling a resource later in that list is not available" in warnings[1]
    assert not any("depends on gold" in w for w in warnings)
    assert flags == {"market_sequencing"}


@pytest.mark.parametrize("gold,sold", [(100, "stone"), (0, "food")])
def test_market_buy_with_sufficient_gold_or_earlier_sale_has_no_sequence_warning(gold, sold):
    view = tooling_view()
    view["you"]["resources"]["gold"] = gold
    assert order_warning_details(view, market_orders(sold=sold)) == ([], set())


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCIV_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTCIV_URL", "http://unused.invalid")
    path = Path(__file__).resolve().parent.parent / "examples" / "play_cli.py"
    spec = importlib.util.spec_from_file_location("play_cli_tooling", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = Mock(spec=AgentCivClient)
    client.player_id = "p1"
    client.state.return_value = tooling_view()
    client.submit_orders.return_value = {"turn": 8, "accepted": 2, "errors": []}
    monkeypatch.setattr(module, "AgentCivClient", Mock(return_value=client))
    monkeypatch.setattr(module, "_spawn_release", Mock())
    monkeypatch.setattr(module.time, "sleep", Mock())
    monkeypatch.setattr(module.time, "time_ns", Mock(side_effect=[100, 200, 300]))
    module.FIX_WINDOW = 23
    module._save("A", {"game_id": "game", "player_id": "p1", "token": "token", "seen_turn": 8})
    return module, client


def saved_creds(module):
    return json.loads((module.HOME / "A.json").read_text())


@pytest.mark.parametrize("qty", [100, 1])
def test_cli_market_sequence_keeps_draft_and_releases_it(cli, capsys, qty):
    module, client = cli
    orders = market_orders(qty=qty)
    module.main(["orders", "A", json.dumps(orders)])
    client.submit_orders.assert_called_once_with(orders, turn=8, ready=False)
    saved = saved_creds(module)
    assert saved["pending_orders"] == orders and saved["pending_turn"] == saved["acted_turn"] == 8
    module._spawn_release.assert_called_once_with("A", saved["submit_stamp"])
    out = capsys.readouterr().out
    assert "WARNINGS" in out and "wood" in out and "stone" in out
    assert "held open for up to 23s" in out and "confirmed automatically afterwards unless replaced" in out
    module.cmd_release("A", str(saved["submit_stamp"]))
    module.time.sleep.assert_called_once_with(23)
    assert client.submit_orders.call_args == call(orders, turn=8)


def test_cli_clean_replacement_confirms_and_invalidates_old_release(cli):
    module, client = cli
    module.main(["orders", "A", json.dumps(market_orders())])
    old_stamp = saved_creds(module)["submit_stamp"]
    client.submit_orders.reset_mock()
    module.main(["orders", "A", "[]"])
    assert client.submit_orders.call_args_list == [call([], turn=8, ready=False), call([], turn=8)]
    saved = saved_creds(module)
    assert "pending_orders" not in saved and "pending_turn" not in saved
    assert saved["submit_stamp"] != old_stamp
    module._spawn_release.assert_called_once()
    client.submit_orders.reset_mock()
    module.cmd_release("A", str(old_stamp))
    client.submit_orders.assert_not_called()


@pytest.mark.parametrize("streak,bank,has_fact", [(0, 0, False), (2, 0, True), (0, 500, True), (0, 499, False)])
def test_deal_shortfall_and_conditional_default_facts(streak, bank, has_fact):
    view = tooling_view()
    view["players"][0].update(economic_streak=streak, bank=bank)
    view["deals"]["open"] = [offer()]
    warnings = deal_warnings(view, [{"type": "accept", "deal": "d1"}])
    assert len(warnings) == 1
    assert "Deal d1: first contract instalment 400 food exceeds projected stock 315 food" in warnings[0]
    assert ("economic_streak to 0" in warnings[0]) is has_fact
    assert ("seizes the gold value of the remaining obligation from your bank" in warnings[0]) is has_fact


def test_deal_projection_includes_immediate_resources_and_existing_obligations():
    view = tooling_view()
    view["you"]["resources"]["gold"] = 50
    view["you"]["income"]["gold"] = 10
    view["contracts"] = [contract(food=50, gold=10), contract("expired", turns=0, food=900, gold=900)]
    view["deals"]["open"] = [offer(give={"food": 30, "gold": 5},
                                    get={"food": 20, "gold": 15, "per_turn": {"food": 400, "gold": 50},
                                         "turns": 3})]
    warnings = deal_warnings(view, [{"type": "accept", "deal": "d1"}])
    assert len(warnings) == 2
    assert "400 food exceeds projected stock 275 food" in warnings[0]
    assert "50 gold exceeds projected stock 40 gold" in warnings[1]


def test_deal_immediate_receipt_can_cover_first_instalment():
    view = tooling_view()
    view["deals"]["open"] = [offer(give={"food": 85})]
    assert deal_warnings(view, [{"type": "accept", "deal": "d1"}]) == []


@pytest.mark.parametrize("case", ["reject", "missing", "other_recipient", "receiving_only", "spot_trade"])
def test_deal_warnings_ignore_actions_without_new_payable_contract(case):
    view = tooling_view()
    deal = offer()
    action = {"type": "accept", "deal": "d1"}
    if case == "reject":
        action["type"] = "reject"
    elif case == "missing":
        action["deal"] = "d2"
    elif case == "other_recipient":
        deal.update(to="p2", **{"from": "p1"})
    elif case == "receiving_only":
        deal["give"], deal["get"] = deal["get"], {}
    elif case == "spot_trade":
        deal["get"] = {"food": 100}
    view["deals"]["open"] = [deal]
    assert deal_warnings(view, [action]) == []


def test_cli_deal_refuses_batch_without_force(cli, capsys):
    module, client = cli
    client.state.return_value["deals"]["open"] = [offer()]
    actions = [{"type": "say", "to": "all", "text": "hello"}, {"type": "accept", "deal": "d1"}]
    module.main(["deal", "A", json.dumps(actions)])
    client.diplomacy.assert_not_called()
    out = capsys.readouterr().out
    assert "400 food exceeds projected stock 315 food" in out and "NOT SENT" in out and "--force" in out


@pytest.mark.parametrize("flag_first", [False, True])
def test_cli_deal_force_sends_and_prints_warning(cli, capsys, flag_first):
    module, client = cli
    client.state.return_value["deals"]["open"] = [offer()]
    action = {"type": "accept", "deal": "d1"}
    client.diplomacy.return_value = {"results": [{"index": 0, "ok": True, "deal": "d1"}]}
    args = ["--force", json.dumps(action)] if flag_first else [json.dumps(action), "--force"]
    module.main(["deal", "A", *args])
    client.diplomacy.assert_called_once_with([action])
    out = capsys.readouterr().out
    assert "400 food exceeds projected stock 315 food" in out and "#0 ok" in out and "NOT SENT" not in out
    assert "[--force]" in module.__doc__


def test_mcp_accept_includes_warnings_computed_before_deal_executes(monkeypatch):
    client = Mock(spec=AgentCivClient)
    client.token = "token"
    view = tooling_view()
    view["deals"]["open"] = [offer()]
    client.state.return_value = view

    def execute(actions):
        assert actions == [{"type": "accept", "deal": "d1"}]
        client.state.assert_called_once_with()
        view["deals"]["open"] = []
        return {"results": [{"ok": True, "deal": "d1"}]}

    client.diplomacy.side_effect = execute
    monkeypatch.setattr("agentciv.mcp_server.AgentCivClient", Mock(return_value=client))
    text = AgentCivMCP("http://unused.invalid").respond_to_deal("d1", "accept")
    client.diplomacy.assert_called_once_with([{"type": "accept", "deal": "d1"}])
    assert "Deal d1 accepted and executed" in text
    assert "400 food exceeds projected stock 315 food" in text
