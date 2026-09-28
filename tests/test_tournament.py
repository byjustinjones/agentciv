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
