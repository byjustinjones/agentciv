"""Per-seat ratings aggregated by identity (the same name in several seats)."""
import json
import math
import random

import pytest

from agentciv import ratings


def _old_update(table, placements, ranks=None):
    """ratings.update as it was before per-seat aggregation (single seats only)."""
    if ranks is None:
        ranks = list(range(1, len(placements) + 1))
    for name in placements:
        table.setdefault(name, {**ratings.new_rating(), "games": 0, "wins": 0, "total_place": 0})
    olds = [table[name] for name in placements]
    news = ratings.rate(olds, list(ranks))
    for name, rank, new in zip(placements, ranks, news):
        entry = table[name]
        entry["mu"], entry["sigma"] = new["mu"], new["sigma"]
        entry["games"] = entry.get("games", 0) + 1
        entry["wins"] = entry.get("wins", 0) + (1 if rank == 1 else 0)
        entry["total_place"] = entry.get("total_place", 0) + rank
    return table


def _old_record(table, placements, ranks):
    """The old Storage.record_result: duplicates keep their best placement."""
    seen, seen_ranks = [], []
    for name, rank in zip(placements, ranks):
        if name not in seen:
            seen.append(name)
            seen_ranks.append(rank)
    if len(seen) < 2:
        return
    dense, prev, out = 0, None, []
    for i, r in enumerate(seen_ranks):
        if r != prev:
            dense, prev = i + 1, r
        out.append(dense)
    _old_update(table, seen, out)


def test_llm_second_against_five_copies_beats_four_seats_and_loses_to_one():
    table = {}
    placements = ["economist", "LLM", "economist", "economist", "economist", "economist"]
    ratings.update(table, placements)
    # the same game against five distinct players at the same ratings
    ref = {}
    ratings.update(ref, ["e1", "LLM", "e2", "e3", "e4", "e5"])
    assert table["LLM"]["mu"] > ratings.MU
    assert table["LLM"]["mu"] == ref["LLM"]["mu"] and table["LLM"]["sigma"] == ref["LLM"]["sigma"]
    copies = [ref[f"e{i}"] for i in range(1, 6)]
    assert table["economist"]["mu"] == pytest.approx(
        ratings.MU + sum(c["mu"] - ratings.MU for c in copies) / 5)
    assert table["economist"]["sigma"] == pytest.approx(
        math.sqrt(sum(c["sigma"] ** 2 for c in copies) / 5))
    # bookkeeping counts seats
    assert table["economist"]["games"] == 5 and table["economist"]["wins"] == 1
    assert table["economist"]["total_place"] == 1 + 3 + 4 + 5 + 6
    assert table["LLM"] == {**table["LLM"], "games": 1, "wins": 0, "total_place": 2}
    # the old code saw a 2-player game the economist won
    old = {}
    _old_record(old, placements, list(range(1, 7)))
    assert old["LLM"]["mu"] < ratings.MU


def test_ties_between_copies_count_every_tied_seat_as_a_win():
    table = {}
    ratings.update(table, ["bot", "bot", "alice"], [1, 1, 3])
    assert table["bot"]["wins"] == 2 and table["bot"]["games"] == 2
    assert table["alice"]["mu"] < ratings.MU < table["bot"]["mu"]


def test_equal_skill_one_vs_five_copies_does_not_drift_apart():
    rng = random.Random(0)
    new, old = {}, {}
    for _ in range(600):
        seats = ["human"] + ["bot"] * 5
        rng.shuffle(seats)  # equal skill: every seat equally likely to win
        ratings.update(new, seats)
        _old_record(old, seats, list(range(1, 7)))
    for name in ("human", "bot"):
        assert abs(new[name]["mu"] - ratings.MU) < 3.0
    assert abs(new["human"]["mu"] - new["bot"]["mu"]) < 4.0
    # the old best-placement rule makes the copies look much stronger
    assert old["bot"]["mu"] - old["human"]["mu"] > 6.0
    assert new["bot"]["games"] == 3000 and new["human"]["games"] == 600


def test_single_seat_games_are_byte_identical_to_the_old_update():
    rng = random.Random(7)
    names = [f"p{i}" for i in range(9)]
    new, old = {}, {}
    for _ in range(300):
        n = rng.randint(2, 8)
        seats = rng.sample(names, n)
        ranks, rank = [], 0
        for i in range(n):  # random ties, non-decreasing ranks
            if i == 0 or rng.random() > 0.2:
                rank = i + 1
            ranks.append(rank)
        ratings.update(new, seats, ranks)
        _old_update(old, seats, ranks)
    assert json.dumps(new, sort_keys=True) == json.dumps(old, sort_keys=True)
