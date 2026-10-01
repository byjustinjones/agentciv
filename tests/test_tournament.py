"""Tournament runner: run_game, scheduling, summaries and the CLI."""
from __future__ import annotations

import json

from agentciv import tournament as T


def test_label_bots():
    assert T.label_bots(["a", "b", "b", "c", "b"]) == ["a", "b#1", "b#2", "c", "b#3"]


def test_run_game_smoke():
    r = T.run_game(["strategist", "economist", "rusher", "turtle", "random", "random"], seed=3, max_turns=15)
    assert r["condition"] in T.CONDITIONS
    assert r["turns"] <= 15
    labels = {"strategist", "economist", "rusher", "turtle", "random#1", "random#2"}
    assert set(r["placements"]) == labels
    assert r["winner"] == r["placements"][0]
    assert set(r["scores"]) == labels
    assert not r["bot_exceptions"]
    assert sum(r["prevalidation_errors"].values()) <= 3
    json.dumps(r)   # JSON-serialisable


def test_run_game_with_pairs_and_determinism():
    specs = [("alpha", "economist"), ("beta", "random"), ("gamma", "idle")]
    a = T.run_game(specs, seed=9, max_turns=12)
    b = T.run_game(specs, seed=9, max_turns=12)
    assert set(a["placements"]) == {"alpha", "beta", "gamma"}
    assert a["placements"] == b["placements"] and a["scores"] == b["scores"]


def test_schedule_rotates_and_is_seeded():
    bots = ["strategist", "economist", "random", "random"]
    s1 = T.schedule(bots, games=8, players=4, seed=5)
    s2 = T.schedule(bots, games=8, players=4, seed=5)
    assert s1 == s2
    seats = {}
    for specs, _ in s1:
        assert sorted(lab for lab, _ in specs) == ["economist", "random#1", "random#2", "strategist"]
        for k, (lab, _) in enumerate(specs):
            seats.setdefault(lab, set()).add(k)
    assert all(len(v) >= 2 for v in seats.values())      # nobody glued to one seat
    assert len({seed for _, seed in s1}) == 8            # distinct map seeds
    # more bots than seats -> sampled subsets; fewer -> repeated
    s3 = T.schedule(["a", "b", "c", "d", "e"], games=3, players=3, seed=1)
    assert all(len(specs) == 3 for specs, _ in s3)
    s4 = T.schedule(["economist"], games=1, players=3, seed=1)
    assert [lab for lab, _ in s4[0][0]] and len(s4[0][0]) == 3


def test_run_tournament_summary_and_format():
    s = T.run_tournament(["economist", "random", "random"], games=3, players=3, seed=2, max_turns=20, jobs=1)
    assert s["games"] == 3
    names = {b["bot"] for b in s["bots"]}
    assert names == {"economist", "random#1", "random#2"}
    for b in s["bots"]:
        assert b["games"] == 3
        assert 1 <= b["avg_place"] <= 3
        assert b["rating"] is not None
    assert sum(s["conditions"].values()) == 3
    text = T.format_summary(s)
    assert "economist" in text and "ending conditions" in text


def test_cli_json(tmp_path, capsys):
    out = tmp_path / "t.json"
    rc = T.main(["--bots", "economist,random", "--games", "2", "--seed", "1", "--max-turns", "10",
                 "--json", str(out), "--quiet"])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["games"] == 2 and len(data["results"]) == 2
    assert "ending conditions" in capsys.readouterr().out


def test_parallel_matches_serial():
    kw = dict(games=2, players=3, seed=4, max_turns=12)
    a = T.run_tournament(["economist", "turtle", "random"], jobs=1, **kw)
    b = T.run_tournament(["economist", "turtle", "random"], jobs=2, **kw)
    assert [r["placements"] for r in a["results"]] == [r["placements"] for r in b["results"]]


def test_summary_reports_median_seats_and_start_slots():
    s = T.run_tournament(["economist", "turtle", "random"], games=4, players=3, seed=3, max_turns=15, jobs=1)
    assert s["median_turns"] > 0
    assert sum(r["games"] for r in s["seats"]) == 12
    assert sorted(r["seat"] for r in s["start_slots"]) == [0, 1, 2]
    assert sum(r["wins"] for r in s["start_slots"]) == 4
    for r in s["results"]:
        assert sorted(seat["slot"] for seat in r["seats"]) == [0, 1, 2]
    assert "by start slot" in T.format_summary(s)


def test_count_events_includes_seized_bank_gold():
    from collections import Counter
    trade = {pid: dict.fromkeys(T.TRADE_KEYS, 0) for pid in ("p1", "p2")}
    T._count_events(None, trade, Counter(), [
        {"type": "contract_default", "payer": "p1", "payee": "p2", "per_turn": {"gold": 50},
         "turns_left": 5, "seized": 250}])
    assert trade["p1"]["defaults"] == 1 and trade["p1"]["gold_paid"] == 250 and trade["p1"]["net_value"] == -250
    assert trade["p2"]["gold_received"] == 250 and trade["p2"]["net_value"] == 250


def test_treaty_counters_per_game_and_in_the_summary():
    from collections import Counter

    from agentciv.engine.testing import new_game, run_turn
    g = new_game(4)
    tally = Counter()
    run_turn(g, {"p1": [{"type": "propose_treaty", "to": "p2", "turns": 20}]})
    ev = run_turn(g, {"p2": [{"type": "accept_treaty", "from": "p1"}]})
    T._count_treaties(g, tally, ev)
    g.player("p1").resources["influence"] = 100
    g.player("p1").legacy = 100
    ev = run_turn(g, {"p1": [{"type": "break_treaty", "with": "p2"}]})
    T._count_treaties(g, tally, ev)
    tot = T._treaty_totals(tally)
    assert (tot["signed"], tot["broken"], tot["break_influence"], tot["legacy_lost"]) == (1, 1, 50, 10)
    assert tot["avg_live"] == 0.25 and tot["peak_live"] == 0.5 and tot["peak_max"] == 1
    s = T.run_tournament(["rusher", "turtle", "economist"], games=2, players=3, seed=2, max_turns=25)
    assert set(T.TREATY_KEYS) <= set(s["treaties_per_game"]) and "treaties per game: signed" in T.format_summary(s)
    assert all("treaties" in r for r in s["results"])


def test_placement_ranks_share_ties_and_a_victory_winner_ranks_alone():
    from agentciv.engine.testing import new_game
    g = new_game(3)
    assert g.placement_ranks() == []          # not finished
    g._finish("p2", "wonder")                  # equal scores at the start
    assert g.result["placements"][0] == "p2" and g.placement_ranks() == [1, 2, 2]
    assert "ranks" not in g.result             # recorded views stay as they were
    g = new_game(3)
    g._finish("p3", "score")
    assert g.placement_ranks() == [1, 1, 1]


def test_tied_idle_bots_rate_identically_in_either_seat_order():
    rows = []
    for specs in ([("a", "idle"), ("b", "idle")], [("b", "idle"), ("a", "idle")]):
        r = T.run_game(specs, seed=1, max_turns=12, rounds=0)
        assert r["scores"]["a"] == r["scores"]["b"] and r["ranks"] == [1, 1]
        s = T.summarize([r])
        rows.append({b["bot"]: (b["mu"], b["sigma"], b["wins"], b["avg_place"]) for b in s["bots"]})
        assert rows[-1]["a"] == rows[-1]["b"]
        assert [x["avg_place"] for x in s["seats"]] == [1.0, 1.0]
    assert rows[0] == rows[1]


def test_summarize_accepts_results_without_ranks():
    r = T.run_game([("a", "idle"), ("b", "idle")], seed=1, max_turns=5, rounds=0)
    del r["ranks"]
    s = T.summarize([r])
    by = {b["bot"]: b for b in s["bots"]}
    assert by["a"]["avg_place"] == 1.0 and by["b"]["avg_place"] == 2.0


def test_replan_tracker_streaks_breaks_leads_and_attacks():
    """ReplanTracker on hand-made turns: streak events by reason, a city
    capture credited to its capturer, lead changes in victory progress,
    the target-to-win time and attacks after turn 30."""
    from agentciv.engine import constants as C
    from agentciv.engine.testing import sandbox
    g = sandbox(3)
    for pid, xy in zip(("p1", "p2", "p3"), ((2, 2), (12, 2), (2, 12))):
        g.add_city(*xy, pid, capital=True)
    label = {"p1": "banker", "p2": "spoiler", "p3": "other"}
    tr = T.ReplanTracker(g, label)

    def turn(t, events=(), bank=None):
        if bank:
            for pid, b in bank.items():
                g.player(pid).bank = b
            g._invalidate()
        tr.before_step()
        tr.after_step(t, list(events))

    turn(10, bank={"p1": 2000})                       # p1 leads (0.8 * 2000/3600 >= 0.25)
    turn(20, bank={"p2": 2500})                       # p2 overtakes: one lead change
    turn(25, [{"type": "streak_started", "player": "p1", "condition": "economic"}], bank={"p1": C.BANK_VICTORY})
    turn(26, [{"type": "streak_paused", "player": "p1", "condition": "economic", "reason": "deposit"}])
    x, y = 2, 2
    turn(31, [{"type": "battle", "x": x, "y": y, "clash": False, "sides": ["p2", "p1"], "winner": "p2"},
              {"type": "city_captured", "x": x, "y": y, "from": "p1", "to": "p2"},
              {"type": "streak_ended", "player": "p1", "condition": "economic", "reason": "city_lost"},
              {"type": "streak_ended", "player": "p3", "condition": "influence"}])
    out = tr.result({"winner": "p1", "condition": "economic", "turn": 45})
    assert out["streaks"]["economic"] == {"started": 1, "paused": {"deposit": 1}, "ended": {"city_lost": 1}}
    assert out["streaks"]["influence"]["ended"] == {"unmet": 1}
    assert out["streak_breaks"] == [{"turn": 31, "victim": "banker", "conditions": ["economic"], "by": ["spoiler"]}]
    assert out["lead_changes"] == 2                   # p1 -> p2 -> p1 (at the bank target)
    assert out["first_target"] == {"banker": {"bank": 25}}
    assert out["target_to_win"] == 20 and out["winner_streak_ends"] == {"city_lost": 1}
    assert out["attacked_after_30"] == {"banker": 1}  # the battle on its city; the capturer was not attacked
    json.dumps(out)


def test_replanning_summary_and_format():
    s = T.run_tournament(["banker", "economist", "idle"], games=3, players=3, seed=2, max_turns=150)
    rp = s["replanning"]
    assert rp["games"] == 3 and set(rp["streaks_per_game"]) == {"economic", "influence"}
    assert list(rp["fields"]) == ["banker, economist, idle"] and rp["fields"]["banker, economist, idle"]["games"] == 3
    assert rp["streak_wins"] == sum(1 for r in s["results"] if r["condition"] in ("economic", "influence"))
    if rp["target_to_win"].get("economic"):
        assert rp["target_to_win"]["economic"]["min"] >= T.C.VICTORY_STREAK_TURNS - 1
    assert all("streak_breaks_by_per_game" in b and "streak_resets_per_game" in b for b in s["bots"])
    text = T.format_summary(s)
    assert "forced replanning (per game):" in text and "field [banker, economist, idle] (3 games)" in text
    assert "lead changes in victory progress" in text and "winners never attacked after turn 30" in text
    json.dumps(s)
    assert T.field_key({"seats": [{"bot": "b"}, {"bot": "a"}, {"bot": "b"}]}) == "a, b x2"
