"""CLI command regressions using temporary credentials and a mocked client."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from agentciv.client import AgentCivClient


CLI_PATH = Path(__file__).resolve().parent.parent / "examples" / "play_cli.py"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTCIV_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTCIV_URL", "http://unused.invalid")
    spec = importlib.util.spec_from_file_location("play_cli_commands", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = Mock(spec=AgentCivClient)
    client.player_id = "p1"
    factory = Mock(return_value=client)
    monkeypatch.setattr(module, "AgentCivClient", factory)
    monkeypatch.setattr(module.time, "sleep", Mock(side_effect=AssertionError("unexpected sleep")))
    return module, client, factory


def save_creds(module, **extra):
    creds = {"game_id": "game", "player_id": "p1", "token": "saved-token", **extra}
    module._save("A", creds)
    return creds


def load_creds(module):
    return json.loads((module.HOME / "A.json").read_text())


def eliminated_view(status="running", eliminated_turn=8):
    return {
        "status": status, "turn": 9,
        "players": [{"id": "p2", "alive": True},
                    {"id": "p1", "alive": False, "eliminated_turn": eliminated_turn}],
        "victory": {"result": {"winner": "p2", "reason": "last_survivor"}},
    }


@pytest.mark.parametrize("acted_turn", [7, 9])
@pytest.mark.parametrize("compact", [False, True])
def test_eliminated_next_returns_without_wait(cli, capsys, acted_turn, compact):
    module, client, _ = cli
    before = save_creds(module, acted_turn=acted_turn)
    client.state.return_value = eliminated_view()
    module.main(["next", "A"] + (["--compact"] if compact else []))
    assert capsys.readouterr().out == (
        "ELIMINATED on turn 8. You can no longer act in this game; it continues without you.\n"
    )
    client.state.assert_called_once_with()
    client.wait.assert_not_called()
    assert load_creds(module) == before


def test_eliminated_next_includes_finished_result(cli, capsys):
    module, client, _ = cli
    save_creds(module, acted_turn=9)
    client.state.return_value = eliminated_view("finished")
    module.main(["next", "A"])
    out = capsys.readouterr().out
    assert out.startswith("ELIMINATED on turn 8.")
    assert json.loads(out.split("GAME OVER: ", 1)[1]) == client.state.return_value["victory"]["result"]
    client.wait.assert_not_called()


@pytest.mark.parametrize("status", ["running", "finished"])
def test_eliminated_orders_never_submit(cli, capsys, status):
    module, client, _ = cli
    before = save_creds(module, seen_turn=9)
    client.state.return_value = eliminated_view(status)
    module.main(["orders", "A", "[]"])
    assert capsys.readouterr().out.startswith(
        "ELIMINATED on turn 8. You can no longer act in this game; it continues without you.\n"
    )
    client.submit_orders.assert_not_called()
    assert load_creds(module) == before


def test_eliminated_turn_falls_back_to_current_turn(cli, capsys):
    module, client, _ = cli
    save_creds(module)
    client.state.return_value = eliminated_view(eliminated_turn=None)
    module.main(["next", "A"])
    assert capsys.readouterr().out.startswith("ELIMINATED on turn 9.")


def test_duplicate_join_keeps_credentials_and_makes_no_client(cli, capsys):
    module, client, factory = cli
    before = save_creds(module, seq=37, acted_turn=9, summary_snapshot={"turn": 8})
    module.main(["join", "A", "game"])
    assert capsys.readouterr().out == "already joined game as p1; use next\n"
    assert load_creds(module) == before
    factory.assert_not_called()
    client.join.assert_not_called()


@pytest.mark.parametrize("previous_game", [None, "old-game"])
def test_join_initializes_inbox_cursor_from_current_view(cli, capsys, previous_game):
    module, client, factory = cli
    if previous_game:
        save_creds(module, game_id=previous_game, seq=2)
    client.join.return_value = {
        "game_id": "new-game", "player_id": "p4", "token": "new-token", "status": "running",
    }
    client.state.return_value = {"diplomacy_seq": 47}
    module.main(["join", "A", "new-game"])
    factory.assert_called_once_with("http://unused.invalid")
    client.join.assert_called_once_with("new-game", "A")
    client.state.assert_called_once_with()
    assert load_creds(module) == {
        "game_id": "new-game", "player_id": "p4", "token": "new-token", "seq": 47,
    }
    assert "Joined new-game as p4 (A)." in capsys.readouterr().out


def test_legacy_inbox_saves_current_cursor_before_poll(cli, capsys):
    module, client, _ = cli
    save_creds(module)
    client.state.return_value = {"diplomacy_seq": 47}

    def inbox(*, since, timeout):
        assert since == 47 and timeout == 3.0
        assert load_creds(module)["seq"] == 47
        return {"seq": 49, "items": []}

    client.inbox.side_effect = inbox
    module.main(["inbox", "A", "3"])
    client.state.assert_called_once_with()
    client.inbox.assert_called_once_with(since=47, timeout=3.0)
    assert load_creds(module)["seq"] == 49
    assert capsys.readouterr().out == "Inbox: nothing new.\n"


def test_inbox_uses_saved_cursor(cli):
    module, client, _ = cli
    save_creds(module, seq=21)
    client.inbox.return_value = {"seq": 24, "items": []}
    module.main(["inbox", "A", "2"])
    client.state.assert_not_called()
    client.inbox.assert_called_once_with(since=21, timeout=2.0)
    assert load_creds(module)["seq"] == 24


@pytest.mark.parametrize("args, timeout", [(["--all"], 0.0), (["3", "--all"], 3.0)])
@pytest.mark.parametrize("saved_seq", [None, 21])
def test_inbox_all_fetches_full_history(cli, args, timeout, saved_seq):
    module, client, _ = cli
    save_creds(module, **({"seq": saved_seq} if saved_seq is not None else {}))
    client.inbox.return_value = {"seq": 24, "items": []}
    module.main(["inbox", "A", *args])
    client.state.assert_not_called()
    client.inbox.assert_called_once_with(since=0, timeout=timeout)
    assert load_creds(module)["seq"] == 24


@pytest.mark.parametrize("kind", ["propose", "counter", "reject"])
def test_deal_message_over_limit_prevents_entire_batch(cli, kind):
    module, client, _ = cli
    save_creds(module)
    actions = [{"type": "say", "text": "hello"}, {"type": kind, "message": "x" * 301}]
    with pytest.raises(SystemExit, match="NOT SENT: deal action #1 message exceeds the 300-character limit"):
        module.main(["deal", "A", json.dumps(actions)])
    client.diplomacy.assert_not_called()


@pytest.mark.parametrize("kind", ["propose", "counter", "reject"])
def test_deal_message_at_limit_is_sent(cli, kind, capsys):
    module, client, _ = cli
    save_creds(module)
    action = {"type": kind, "message": "x" * 300}
    client.diplomacy.return_value = {"results": [{"index": 0, "ok": True}]}
    module.main(["deal", "A", json.dumps(action)])
    client.diplomacy.assert_called_once_with([action])
    assert "#0 ok" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["say", "message"])
def test_chat_text_keeps_500_character_limit(cli, kind):
    module, client, _ = cli
    save_creds(module)
    action = {"type": kind, "to": "all", "text": "x" * 500}
    client.diplomacy.return_value = {"results": [{"index": 0, "ok": True}]}
    module.main(["deal", "A", json.dumps(action)])
    client.diplomacy.assert_called_once_with([action])


@pytest.mark.parametrize("args", [["--help"], ["deal", "--help"], ["inbox", "--help"]])
def test_command_help_lists_limits_and_flags(cli, capsys, args):
    module, _, factory = cli
    module.main(args)
    out = capsys.readouterr().out
    assert "state  NAME [--compact]" in out
    assert "next   NAME [--compact]" in out
    assert "inbox  NAME [SECONDS] [--all]" in out
    assert "300-character limit" in out
    assert "500-character limit" in out
    factory.assert_not_called()
