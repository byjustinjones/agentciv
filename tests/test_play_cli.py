"""examples/play_cli.py against a live server: turn pinning and held-open turns."""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

from agentciv.client import AgentCivClient
from agentciv.server import create_server

CLI_PATH = Path(__file__).resolve().parent.parent / "examples" / "play_cli.py"


@pytest.fixture()
def cli(tmp_path):
    srv = create_server("127.0.0.1", 0, data_dir=str(tmp_path / "data")).start_background()
    spec = importlib.util.spec_from_file_location("play_cli", CLI_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.URL, mod.HOME = srv.url, tmp_path / "home"
    c = AgentCivClient(srv.url)
    gid = c.create_game(max_players=2, bots=["idle"], turn_timeout=60, turn_delay=0)
    mod.main(["join", "A", gid])
    creds = json.loads((mod.HOME / "A.json").read_text())
    c.game_id, c.token = gid, creds["token"]
    deadline = time.time() + 10
    while c.state()["status"] != "running" and time.time() < deadline:
        time.sleep(0.1)
    yield mod, c
    srv.stop()


def _turn(c: AgentCivClient) -> int:
    return c.state()["turn"]


def test_orders_are_pinned_to_the_turn_last_viewed(cli, capsys):
    mod, c = cli
    mod.main(["state", "A"])
    mod.main(["orders", "A", "[]"])
    c.wait(since_turn=0, timeout=10)
    assert _turn(c) == 1
    capsys.readouterr()
    with pytest.raises(SystemExit) as ei:  # still pinned to turn 0: refused, not applied to turn 1
        mod.main(["orders", "A", "[]"])
    assert "NOT APPLIED" in str(ei.value) and "turn 0" in str(ei.value)
    assert not c.state()["you"]["submitted"]
    mod.main(["state", "A"])
    mod.main(["orders", "A", "[]"])
    assert "Turn 1: 0 order(s) accepted" in capsys.readouterr().out


def test_rejected_orders_hold_the_turn_open_until_resubmitted(cli, capsys):
    mod, c = cli
    mod.main(["next", "A"])
    mod.main(["orders", "A", '[{"type": "bogus"}]'])
    out = capsys.readouterr().out
    assert "1 rejected" in out and "hint:" in out and "held open" in out
    time.sleep(3)  # longer than the server's grace period for a fix
    assert _turn(c) == 0
    mod.main(["orders", "A", "[]"])
    assert c.wait(since_turn=0, timeout=10)["turn"] == 1


def test_a_held_turn_is_released_after_the_fix_window(cli):
    mod, c = cli
    mod.FIX_WINDOW = 4
    mod.main(["next", "A"])
    mod.main(["orders", "A", '[{"type": "bogus"}]'])
    time.sleep(2.5)
    assert _turn(c) == 0
    assert c.wait(since_turn=0, timeout=15)["turn"] == 1
