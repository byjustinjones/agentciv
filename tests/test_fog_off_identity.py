"""Games created without ``fog`` are unchanged by the fog-of-war feature.

``tests/data/nofog_golden.json`` was recorded at the base commit (before fog
existed) by ``tests/data/make_nofog_golden.py``: the orders five bots
submitted over 12 turns on two seeds, and a digest of every player view, the
public and full spectator views and every ``step()`` result. Replaying the
orders must reproduce every digest (``costs.fog`` is the only allowed
difference: the rules JSON is shared by all games).
"""
import importlib.util
import json
from pathlib import Path

import pytest

from agentciv.engine import rules_json

DATA = Path(__file__).resolve().parent / "data"
GOLDEN = json.loads((DATA / "nofog_golden.json").read_text())


def _gen():
    spec = importlib.util.spec_from_file_location("make_nofog_golden", DATA / "make_nofog_golden.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GEN = _gen()


@pytest.mark.parametrize("game", GOLDEN["games"], ids=lambda g: f"seed{g['seed']}")
def test_views_identical_to_base_commit(game):
    g = GEN.new_game(game["seed"])
    assert not getattr(g.config, "fog", False)
    turns = game["turns"]
    assert GEN.snapshot(g) == turns[0]["digests"]
    for k, turn in enumerate(turns[1:], start=1):
        for pid, orders in turn["orders"].items():
            g.submit_orders(pid, orders)
        ev = g.step()
        assert GEN.snapshot(g, ev) == turn["digests"], f"turn {k} differs from the base commit"


def test_rules_json_differs_from_base_only_by_fog_block():
    rules = {k: v for k, v in rules_json().items() if k != "fog"}
    assert GEN.digest(rules) == GOLDEN["rules"]
