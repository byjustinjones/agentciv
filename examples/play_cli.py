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

When some orders are rejected, the turn is held open for ``$AGENTCIV_FIX_WINDOW``
seconds (default 60) so a corrected list can be resubmitted; after that the
accepted orders are confirmed automatically by a small background process.

Orders and diplomacy action formats: see ``rules`` (docs/RULES.md).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentciv.client import (AgentCivClient, ApiError, ascii_map, describe_event, order_warnings,  # noqa: E402
                             summarize_view)

URL = os.environ.get("AGENTCIV_URL", "http://localhost:8765")
HOME = Path(os.environ.get("AGENTCIV_HOME", Path.home() / ".agentciv"))
FIX_WINDOW = float(os.environ.get("AGENTCIV_FIX_WINDOW", "60"))


def _creds_path(name: str) -> Path:
    return HOME / f"{name}.json"


def _client(name: str) -> tuple[AgentCivClient, dict]:
    path = _creds_path(name)
    if not path.exists():
        sys.exit(f"no credentials for {name!r}; run: join {name} GAME_ID")
    creds = json.loads(path.read_text())
    c = AgentCivClient(URL, token=creds["token"])
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


def _seen(name: str, creds: dict, view: dict) -> None:
    """Remember the turn this player last looked at: ``orders`` are for that turn."""
    if view.get("status") == "running":
        creds["seen_turn"] = view.get("turn")
        _save(name, creds)


def _print_state(c: AgentCivClient, name: str, creds: dict) -> dict:
    view = c.state()
    _seen(name, creds, view)
    print(summarize_view(view, c.player_id))
    return view


def cmd_join(name: str, game_id: str) -> None:
    c = AgentCivClient(URL)
    res = c.join(game_id, name)
    _save(name, {"game_id": res["game_id"], "player_id": res["player_id"], "token": res["token"], "seq": 0})
    print(f"Joined {res['game_id']} as {res['player_id']} ({name}). Status: {res.get('status')}.")
    print("Next: run `next NAME` to wait for the game to start, then read the state and submit orders.")


def cmd_orders(name: str, text: str) -> None:
    c, creds = _client(name)
    orders = _parse_json(text)
    turn = creds.get("seen_turn")
    before = c.state()  # the state these orders are written against (warnings use it, not a later turn)
    if turn is None:
        turn = before["turn"]
    # Submit as a draft first so the turn cannot resolve while rejected orders are being fixed;
    # a clean list is then confirmed at once, a list with rejections stays open for FIX_WINDOW seconds.
    try:
        res = c.submit_orders(orders, turn=turn, ready=False)
        if not res.get("errors"):
            res = c.submit_orders(orders, turn=turn)
    except ApiError as e:
        if e.status == 409 and "stale turn" in e.message:
            sys.exit(f"NOT APPLIED: these orders were for turn {turn}, which has already resolved "
                     f"(current turn: {e.body.get('turn')}). Run `state` (or `next`) and submit orders "
                     "for the current turn.")
        raise
    stamp = time.time_ns()
    creds.update(acted_turn=turn, submit_stamp=stamp)
    errors = res.get("errors", [])
    if errors:
        creds.update(pending_orders=orders, pending_turn=turn)
    _save(name, creds)
    if errors:
        _spawn_release(name, stamp)
    print(f"Turn {res.get('turn')}: {res.get('accepted')} order(s) accepted, {len(errors)} rejected.")
    for err in errors:
        print(f"  REJECTED #{err.get('index')}: {err.get('error')}")
        if err.get("example") is not None:
            print(f"    example: {json.dumps(err['example'])}")
        if err.get("hint"):
            print(f"    hint: {err['hint']}")
    warnings = order_warnings(before, orders) if before.get("turn") == turn else []
    if warnings:
        print("WARNINGS (estimates; accepted orders that may not work out):")
        for w in warnings:
            print(f"  - {w}")
    if errors:
        print(f"The {res.get('accepted')} accepted order(s) are queued; the rejected ones were dropped. "
              f"Turn {turn} is held open for up to {FIX_WINDOW:g}s so you can fix them: run `orders` again "
              "with the full corrected list, including the accepted orders (it replaces everything queued). "
              f"If you don't, the accepted orders are confirmed automatically after {FIX_WINDOW:g}s.")


def _spawn_release(name: str, stamp: int) -> None:
    """Confirm a held-open submission after FIX_WINDOW unless it was replaced first."""
    env = {**os.environ, "AGENTCIV_URL": URL, "AGENTCIV_HOME": str(HOME), "AGENTCIV_FIX_WINDOW": str(FIX_WINDOW)}
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_release", name, str(stamp)], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def cmd_release(name: str, stamp: str) -> None:
    time.sleep(FIX_WINDOW)
    c, creds = _client(name)
    if str(creds.get("submit_stamp")) != stamp:
        return  # resubmitted in the meantime
    try:
        c.submit_orders(creds["pending_orders"], turn=creds["pending_turn"])
    except ApiError:
        pass  # the turn already resolved


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
    while time.time() < deadline:
        view = c.state()
        status = view.get("status")
        if status == "finished" or (status == "running" and view.get("turn", 0) > acted):
            break
        if status == "lobby":
            time.sleep(2)
        else:
            c.wait(since_turn=view.get("turn", 0), timeout=60)
    view = _print_state(c, name, creds)
    if view.get("status") == "finished":
        print("\nGAME OVER:", json.dumps(view.get("victory", {}).get("result")))


def main(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "rules":
            print(AgentCivClient(URL).rules())
        elif cmd == "join":
            cmd_join(args[0], args[1])
        elif cmd == "state":
            c, creds = _client(args[0])
            _print_state(c, args[0], creds)
        elif cmd == "map":
            c, creds = _client(args[0])
            view = c.state()
            _seen(args[0], creds, view)
            print(ascii_map(view, c.player_id))
        elif cmd == "orders":
            cmd_orders(args[0], args[1])
        elif cmd == "deal":
            cmd_deal(args[0], args[1])
        elif cmd == "inbox":
            cmd_inbox(args[0], float(args[1]) if len(args) > 1 else 0.0)
        elif cmd == "next":
            cmd_next(args[0])
        elif cmd == "_release":
            cmd_release(args[0], args[1])
        else:
            sys.exit(f"unknown command {cmd!r}\n{__doc__}")
    except IndexError:
        sys.exit(f"missing argument for {cmd!r}\n{__doc__}")
    except ApiError as e:
        sys.exit(f"server error: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
