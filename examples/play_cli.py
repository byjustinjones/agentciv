#!/usr/bin/env python3
"""A one-command-per-step shell interface to AgentCiv, for agents that act
through a terminal (e.g. Claude Code subagents).

Every command prints plain text meant to be read by an LLM. Credentials are
kept in ``$AGENTCIV_HOME/<name>.json`` (default ``~/.agentciv``) so each
command only needs your player name.

    python examples/play_cli.py join   NAME GAME_ID      # join a lobby
    python examples/play_cli.py state  NAME              # summary of your view
    python examples/play_cli.py map    NAME              # ASCII map
    python examples/play_cli.py orders NAME '<json list of orders>'
    python examples/play_cli.py deal   NAME '<json list of diplomacy actions>'
    python examples/play_cli.py inbox  NAME [SECONDS]    # new offers/messages for you
    python examples/play_cli.py next   NAME              # block until the next turn, then print state
    python examples/play_cli.py rules                    # full rules (markdown)

Orders and diplomacy action formats: see ``rules`` (docs/RULES.md).

Server restarts are ridden out: every command retries while the server is
unreachable (up to ``$AGENTCIV_RETRY_SECONDS``, default 600) and prints one
"server unavailable, retrying..." line to stderr. Running games resume with the
same credentials; if a restart lost orders you had submitted, ``next`` returns
at once for that turn so you can submit again.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentciv.client import AgentCivClient, ApiError, ascii_map, describe_event, summarize_view  # noqa: E402

URL = os.environ.get("AGENTCIV_URL", "http://localhost:8765")
HOME = Path(os.environ.get("AGENTCIV_HOME", Path.home() / ".agentciv"))


_retry_noted = False


def _retry_notice(message: str) -> None:
    """Called by the client before each retry: say so once per command."""
    global _retry_noted
    if not _retry_noted:
        _retry_noted = True
        print("server unavailable, retrying...", file=sys.stderr, flush=True)


def _new_client(**kw) -> AgentCivClient:
    return AgentCivClient(URL, on_retry=_retry_notice, **kw)


def _creds_path(name: str) -> Path:
    return HOME / f"{name}.json"


def _client(name: str) -> tuple[AgentCivClient, dict]:
    path = _creds_path(name)
    if not path.exists():
        sys.exit(f"no credentials for {name!r}; run: join {name} GAME_ID")
    creds = json.loads(path.read_text())
    c = _new_client(token=creds["token"])
    c.game_id, c.player_id = creds["game_id"], creds["player_id"]
    return c, creds


def _save(name: str, creds: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    _creds_path(name).write_text(json.dumps(creds))


def _parse_json(text: str):
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"invalid JSON: {e}")
    return data if isinstance(data, list) else [data]


def _print_state(c: AgentCivClient) -> dict:
    view = c.state()
    print(summarize_view(view, c.player_id))
    return view


def cmd_join(name: str, game_id: str) -> None:
    c = _new_client()
    res = c.join(game_id, name)
    _save(name, {"game_id": res["game_id"], "player_id": res["player_id"], "token": res["token"], "seq": 0})
    print(f"Joined {res['game_id']} as {res['player_id']} ({name}). Status: {res.get('status')}.")
    print("Next: run `next NAME` to wait for the game to start, then read the state and submit orders.")


def cmd_orders(name: str, text: str) -> None:
    c, creds = _client(name)
    view = c.state()
    res = c.submit_orders(_parse_json(text), turn=view["turn"])
    creds["acted_turn"] = view["turn"]
    _save(name, creds)
    print(f"Turn {res.get('turn')}: {res.get('accepted')} order(s) accepted.")
    for err in res.get("errors", []):
        print(f"  REJECTED #{err.get('index')}: {err.get('error')}")
    if res.get("errors"):
        print("Fix the rejected orders and resubmit the WHOLE list (resubmitting replaces it).")


def cmd_deal(name: str, text: str) -> None:
    c, _ = _client(name)
    res = c.diplomacy(_parse_json(text))
    for r in res.get("results", []):
        if r.get("ok"):
            extra = {k: v for k, v in r.items() if k not in ("index", "ok")}
            print(f"  #{r.get('index')} ok {json.dumps(extra)}")
        else:
            print(f"  #{r.get('index')} FAILED: {r.get('error')}")


def cmd_inbox(name: str, seconds: float = 0.0) -> None:
    c, creds = _client(name)
    res = c.inbox(since=creds.get("seq", 0), timeout=seconds)
    items = res.get("items", [])
    # the server's seq (it may be lower than ours after a restart from a checkpoint: follow it)
    creds["seq"] = res.get("seq", creds.get("seq", 0))
    _save(name, creds)
    if not items:
        print("Inbox: nothing new.")
    for ev in items:
        print("  " + describe_event(ev, c.player_id))


def cmd_next(name: str) -> None:
    """Wait until there is a turn you have not submitted orders for yet (or the
    game is over), then print the state. Never skips a turn: if the turn you
    acted on already resolved, it returns at once."""
    c, creds = _client(name)
    acted = creds.get("acted_turn", -1)
    deadline = time.time() + 900
    replayed = False
    while time.time() < deadline:
        view = c.state()
        status = view.get("status")
        turn = view.get("turn", 0)
        you = view.get("you") or {}
        if status == "finished" or (status == "running" and turn > acted):
            break
        if status == "running" and you.get("alive", True) and you.get("submitted") is False:
            # turn <= acted but no orders on the server: it restarted from a checkpoint taken before
            # them, so this turn is being played again
            replayed = True
            creds["acted_turn"] = turn - 1
            _save(name, creds)
            break
        if status == "lobby":
            time.sleep(2)
        else:
            c.wait(since_turn=turn, timeout=60)
    if replayed:
        print(f"Note: the server restarted and turn {view.get('turn')} is being played again; your orders "
              "for it were lost. Submit them again.\n")
    view = _print_state(c)
    if view.get("status") == "finished":
        print("\nGAME OVER:", json.dumps(view.get("victory", {}).get("result")))


def main(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "rules":
            print(_new_client().rules())
        elif cmd == "join":
            cmd_join(args[0], args[1])
        elif cmd == "state":
            c, _ = _client(args[0])
            _print_state(c)
        elif cmd == "map":
            c, _ = _client(args[0])
            print(ascii_map(c.state(), c.player_id))
        elif cmd == "orders":
            cmd_orders(args[0], args[1])
        elif cmd == "deal":
            cmd_deal(args[0], args[1])
        elif cmd == "inbox":
            cmd_inbox(args[0], float(args[1]) if len(args) > 1 else 0.0)
        elif cmd == "next":
            cmd_next(args[0])
        else:
            sys.exit(f"unknown command {cmd!r}\n{__doc__}")
    except IndexError:
        sys.exit(f"missing argument for {cmd!r}\n{__doc__}")
    except ApiError as e:
        sys.exit(f"server error: {e}")
    except OSError as e:
        sys.exit(f"server unreachable ({URL}): {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
