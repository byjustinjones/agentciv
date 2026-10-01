#!/usr/bin/env python3
"""A one-command-per-step shell interface to AgentCiv, for agents that act
through a terminal (e.g. Claude Code subagents).

Every command prints plain text meant to be read by an LLM. Credentials are
kept in ``$AGENTCIV_HOME/<name>.json`` (default ``~/.agentciv``) so each
command only needs your player name.

    python examples/play_cli.py join   NAME GAME_ID [--agent-json JSON]  # join a lobby
    python examples/play_cli.py state  NAME [--compact]  # summary of your view
    python examples/play_cli.py map    NAME              # ASCII map
    python examples/play_cli.py orders NAME '<json list of orders>'
    python examples/play_cli.py deal   NAME '<json list of diplomacy actions>' [--force] [--done]
    python examples/play_cli.py done   NAME              # synchronous games: end your negotiation round
    python examples/play_cli.py inbox  NAME [SECONDS] [--all]  # new items; --all: full history
    python examples/play_cli.py next   NAME [--compact]  # wait for the next turn (or phase), then print state
    python examples/play_cli.py rules                    # full rules (markdown)

Synchronous games (created with "sync": true; the state shows a SYNCHRONOUS
TURN line): each turn is a few negotiation rounds, then an orders phase.
In a negotiation round `deal` queues actions; they are applied when every
seat has ended the round (`done`, or `deal ... --done`), in the turn's
rotating seat order, and `next` then shows their results. `next` in a round
you have already looked at ends that round for you and waits for the next
phase. In the orders phase diplomacy is closed and `orders` is open.

When some orders are rejected or market sequencing warnings occur,
the turn is held open for ``$AGENTCIV_FIX_WINDOW``
seconds (default 60) so a corrected list can be resubmitted; after that the
accepted orders are confirmed automatically by a small background process.

``join --agent-json '{"model": "...", "harness": "..."}'`` (or ``$AGENTCIV_AGENT``)
records what plays the seat (keys: model, model_version, effort, harness,
harness_version, prompt_sha256, tools, memory, notes; strings only). It is
shown in the game summary and replay and never affects matchmaking.

Orders and diplomacy action formats: see ``rules`` (docs/RULES.md).
Deal propose/counter/reject ``message`` fields have a 300-character limit;
say/message text has a 500-character limit. ``deal --help`` prints this help.
Deal accepts with projected contract shortfalls are not sent unless ``--force`` is present.
Peace deals that hand something over while you are on a streak print a NOTE (sent anyway).
State summaries print factual ALERT lines first. Compact summaries include
changes from the latest turn events and the previous saved view.

Server restarts are ridden out: every command retries while the server is
unreachable (up to ``$AGENTCIV_RETRY_SECONDS``, default 600) and prints one
"server unavailable, retrying..." line to stderr. Running games resume with the
same credentials; if a restart lost orders you had submitted, ``next`` returns
at once for that turn so you can submit again.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentciv.client import (AgentCivClient, ApiError, agent_from_env, ascii_map, deal_warnings, describe_event, peace_deal_notes,  # noqa: E402
                             order_warning_details, summarize_view, summarize_compact, view_alerts, view_changes)
from agentciv.engine.constants import DEAL_MESSAGE_MAX_LENGTH  # noqa: E402

URL = os.environ.get("AGENTCIV_URL", "http://localhost:8765")
HOME = Path(os.environ.get("AGENTCIV_HOME", Path.home() / ".agentciv"))
FIX_WINDOW = float(os.environ.get("AGENTCIV_FIX_WINDOW", "60"))


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


def _seen(name: str, creds: dict, view: dict) -> None:
    """Remember the turn this player last looked at: ``orders`` are for that turn."""
    if view.get("status") == "running":
        creds["seen_turn"] = view.get("turn")
        _save(name, creds)


def _print_state(c: AgentCivClient, name: str, creds: dict, compact: bool = False,
                 view: dict | None = None) -> dict:
    view = c.state() if view is None else view
    changes, snapshot = view_changes(view, creds.get("summary_snapshot"))
    creds["summary_snapshot"] = snapshot
    if view.get("status") == "running":
        creds["seen_turn"] = view.get("turn")
        if view.get("phase"):
            creds["seen_phase"] = view["phase"].get("id")
    _save(name, creds)
    alerts = view_alerts(view, c.player_id)
    if alerts:
        print("ALERTS:")
        print("\n".join("ALERT: " + line for line in alerts))
    print(summarize_compact(view, c.player_id, changes) if compact else summarize_view(view, c.player_id))
    return view


def cmd_join(name: str, game_id: str, agent_json: str | None = None) -> None:
    path = _creds_path(name)
    if path.exists():
        creds = json.loads(path.read_text())
        if creds.get("game_id") == game_id:
            print(f"already joined {game_id} as {creds['player_id']}; use next")
            return
    try:
        agent = agent_from_env(agent_json)
    except ValueError as e:
        sys.exit(str(e))
    c = _new_client()
    res = c.join(game_id, name, agent=agent)
    creds = {"game_id": res["game_id"], "player_id": res["player_id"], "token": res["token"]}
    _save(name, creds)  # retain the token even if the initial state request fails
    creds["seq"] = c.state().get("diplomacy_seq", 0)
    _save(name, creds)
    print(f"Joined {res['game_id']} as {res['player_id']} ({name}). Status: {res.get('status')}.")
    print("Next: run `next NAME` to wait for the game to start, then read the state and submit orders.")


def cmd_orders(name: str, text: str) -> None:
    c, creds = _client(name)
    orders = _parse_json(text)
    turn = creds.get("seen_turn")
    before = c.state()  # the state these orders are written against (warnings use it, not a later turn)
    if _print_eliminated(before, c.player_id):
        return
    if turn is None:
        turn = before["turn"]
    warnings, flags = order_warning_details(before, orders) if before.get("turn") == turn else ([], set())
    sequencing = "market_sequencing" in flags
    # Submit as a draft first so the turn cannot resolve while rejected orders are being fixed;
    # rejections and market sequencing warnings keep the draft open for FIX_WINDOW seconds.
    try:
        res = c.submit_orders(orders, turn=turn, ready=False)
        if not res.get("errors") and not sequencing:
            res = c.submit_orders(orders, turn=turn)
    except ApiError as e:
        phase = (e.body or {}).get("phase") if isinstance(e.body, dict) else None
        if e.status == 409 and phase and phase.get("kind") == "negotiate":
            sys.exit(f"NOT APPLIED: orders open after the negotiation rounds (now: round {phase.get('round')} of "
                     f"{phase.get('of')}). End the round with `done {name}` (or `next {name}`), then submit "
                     "orders in the orders phase.")
        if e.status == 409 and "stale turn" in e.message:
            sys.exit(f"NOT APPLIED: these orders were for turn {turn}, which has already resolved "
                     f"(current turn: {e.body.get('turn')}). Run `state` (or `next`) and submit orders "
                     "for the current turn.")
        raise
    stamp = time.time_ns()
    creds.update(acted_turn=turn, submit_stamp=stamp)
    errors = res.get("errors", [])
    held = bool(errors) or sequencing
    if held:
        creds.update(pending_orders=orders, pending_turn=turn)
    else:
        creds.pop("pending_orders", None)
        creds.pop("pending_turn", None)
    _save(name, creds)
    if held:
        _spawn_release(name, stamp)
    print(f"Turn {res.get('turn')}: {res.get('accepted')} order(s) accepted, {len(errors)} rejected.")
    for err in errors:
        print(f"  REJECTED #{err.get('index')}: {err.get('error')}")
        if err.get("example") is not None:
            print(f"    example: {json.dumps(err['example'])}")
        if err.get("hint"):
            print(f"    hint: {err['hint']}")
    if warnings:
        print("WARNINGS (estimates; accepted orders that may not work out):")
        for w in warnings:
            print(f"  - {w}")
    if errors:
        print(f"The {res.get('accepted')} accepted order(s) are queued; the rejected ones were dropped. "
              f"Turn {turn} is held open for up to {FIX_WINDOW:g}s so you can fix them: run `orders` again "
              "with the full corrected list, including the accepted orders (it replaces everything queued). "
              f"If you don't, the accepted orders are confirmed automatically after {FIX_WINDOW:g}s.")
    elif sequencing:
        print(f"The {res.get('accepted')} accepted order(s) are queued. Turn {turn} is held open for up to "
              f"{FIX_WINDOW:g}s; the queued orders are confirmed automatically afterwards unless replaced "
              "by another `orders` command with the full list.")


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


def cmd_deal(name: str, text: str, force: bool = False, done: bool = False) -> None:
    c, creds = _client(name)
    actions = _parse_json(text)
    for i, action in enumerate(actions):
        if (isinstance(action, dict) and action.get("type") in ("propose", "counter", "reject")
                and isinstance(action.get("message"), str)
                and len(action["message"]) > DEAL_MESSAGE_MAX_LENGTH):
            sys.exit(f"NOT SENT: deal action #{i} message exceeds the {DEAL_MESSAGE_MAX_LENGTH}-character limit.")
    warnings, notes = [], []
    if any(isinstance(action, dict) and (action.get("type") == "accept"
                                         or action.get("type") in ("propose", "counter") and action.get("peace"))
           for action in actions):
        view = c.state()
        warnings = deal_warnings(view, actions)
        notes = peace_deal_notes(view, actions)
    for note in notes:
        print(f"NOTE: {note}")
    if warnings:
        print("WARNINGS (projected contract payments):")
        for warning in warnings:
            print(f"  - {warning}")
        if not force:
            print("NOT SENT: these actions have projected contract shortfalls; `deal NAME '<json>' --force` "
                  "sends them despite these warnings.")
            return
    try:
        if done or creds.get("seen_phase") is not None:  # synchronous game
            res = c.diplomacy(actions, done=True if done else None, phase=creds.get("seen_phase"))
        else:
            res = c.diplomacy(actions)
    except ApiError as e:
        _sync_refusal(name, e)
        raise
    for r in res.get("results", []):
        if r.get("ok") and r.get("status") == "queued":
            print(f"  #{r.get('index')} queued")
        elif r.get("ok"):
            extra = {k: v for k, v in r.items() if k not in ("index", "ok")}
            print(f"  #{r.get('index')} ok {json.dumps(extra)}")
        else:
            print(f"  #{r.get('index')} FAILED: {r.get('error')}")
            if r.get("example") is not None:
                print(f"    example: {json.dumps(r['example'])}")
    phase = res.get("phase")
    if phase:
        print(f"Queued for the end of negotiation round {phase.get('round')} of {phase.get('of')} "
              f"({res.get('queued')} action(s) in your queue). " +
              (f"You have ended this round; run `next {name}` for the results." if res.get("done") else
               f"End the round with `done {name}` (or `next {name}`) when you have nothing more to send."))


def _sync_refusal(name: str, e: ApiError) -> None:
    """Exit with a plain explanation of a synchronous game's 409 (stale or closed phase)."""
    phase = (e.body or {}).get("phase") if isinstance(e.body, dict) else None
    if e.status != 409 or not phase:
        return
    if phase.get("kind") == "orders":
        sys.exit(f"NOT SENT: diplomacy is closed in the orders phase of this synchronous turn. Submit orders "
                 f"(`orders {name} ...`); negotiation reopens next turn.")
    if "stale phase" in e.message:
        sys.exit(f"NOT SENT: the round you looked at has ended (now: round {phase.get('round')} of "
                 f"{phase.get('of')}). Run `state {name}` or `next {name}` first.")
    sys.exit(f"NOT SENT: {e.message}")


def cmd_done(name: str) -> None:
    c, creds = _client(name)
    try:
        res = c.end_round(phase=creds.get("seen_phase"))
    except ApiError as e:
        _sync_refusal(name, e)
        raise
    phase = res.get("phase") or {}
    print(f"Ended negotiation round {phase.get('round')} of {phase.get('of')} with {res.get('queued')} queued "
          f"action(s). Waiting for: {', '.join(phase.get('waiting') or []) or 'nobody'}. Run `next {name}`.")


def cmd_inbox(name: str, seconds: float = 0.0, all_history: bool = False) -> None:
    c, creds = _client(name)
    if "seq" not in creds and not all_history:
        creds["seq"] = c.state().get("diplomacy_seq", 0)
        _save(name, creds)
    res = c.inbox(since=0 if all_history else creds["seq"], timeout=seconds)
    items = res.get("items", [])
    # the server's seq (it may be lower than ours after a restart from a checkpoint: follow it)
    creds["seq"] = res.get("seq", creds.get("seq", 0))
    _save(name, creds)
    if not items:
        print("Inbox: nothing new.")
    for ev in items:
        print("  " + describe_event(ev, c.player_id))


def _print_eliminated(view: dict, pid: str) -> bool:
    me = next((p for p in view.get("players", []) if p.get("id") == pid), {})
    if me.get("alive") is not False:
        return False
    turn = me.get("eliminated_turn")
    if turn is None:
        turn = view.get("turn")
    print(f"ELIMINATED on turn {turn}. You can no longer act in this game; it continues without you.")
    if view.get("status") == "finished":
        print("GAME OVER:", json.dumps((view.get("victory") or {}).get("result")))
    return True


def cmd_next(name: str, compact: bool = False) -> None:
    """Wait until there is a turn you have not submitted orders for yet (or the
    game is over), then print the state. Never skips a turn: if the turn you
    acted on already resolved, it returns at once. Synchronous games: also
    returns at every negotiation round you have not looked at yet; in one you
    have looked at, it ends the round for you first."""
    c, creds = _client(name)
    acted = creds.get("acted_turn", -1)
    deadline = time.time() + 900
    replayed = False
    ended = None
    while time.time() < deadline:
        view = c.state()
        if _print_eliminated(view, c.player_id):
            return
        status = view.get("status")
        turn = view.get("turn", 0)
        you = view.get("you") or {}
        phase = view.get("phase") if status == "running" else None
        if status == "finished" or (status == "running" and turn > acted and not
                                    (phase and phase.get("kind") == "negotiate")):
            break
        if status == "running" and turn <= acted and you.get("alive", True) and you.get("submitted") is False:
            # turn <= acted but no orders on the server: it restarted from a checkpoint taken before
            # them, so this turn is being played again
            replayed = True
            creds["acted_turn"] = turn - 1
            _save(name, creds)
            break
        if phase and phase.get("kind") == "negotiate":
            if not phase.get("you_done"):
                if phase.get("id") != creds.get("seen_phase"):
                    break  # a round you have not seen yet
                try:
                    c.end_round(phase=phase.get("id"))
                    ended = phase.get("round")
                except ApiError as e:
                    if e.status != 409:
                        raise
            c.wait(since_phase=phase.get("id"), timeout=60)
            continue
        if status == "lobby":
            time.sleep(2)
        elif phase:
            c.wait(since_phase=phase.get("id"), timeout=60)
        else:
            c.wait(since_turn=turn, timeout=60)
    if ended is not None:
        print(f"(Ended your negotiation round {ended}.)\n")
    if replayed:
        print(f"Note: the server restarted and turn {view.get('turn')} is being played again; your orders "
              "for it were lost. Submit them again.\n")
    view = _print_state(c, name, creds, compact, view)
    if view.get("status") == "finished":
        print("\nGAME OVER:", json.dumps(view.get("victory", {}).get("result")))
    elif view.get("phase"):
        _print_new_diplomacy(c, name, creds)


def _print_new_diplomacy(c: AgentCivClient, name: str, creds: dict) -> None:
    """Synchronous games: what the last barrier delivered to you (inbox items since the last look)."""
    if "seq" not in creds:
        return
    res = c.inbox(since=creds["seq"], timeout=0)
    creds["seq"] = res.get("seq", creds["seq"])
    _save(name, creds)
    items = res.get("items") or []
    if items:
        print("\nNEW DIPLOMACY:")
        for ev in items:
            print("  " + describe_event(ev, c.player_id))


def main(argv: list[str]) -> None:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        return
    try:
        if cmd == "rules":
            print(_new_client().rules())
        elif cmd == "join":
            agent_json = None
            if "--agent-json" in args:
                i = args.index("--agent-json")
                agent_json = args[i + 1]
                args = args[:i] + args[i + 2:]
            cmd_join(args[0], args[1], agent_json)
        elif cmd == "state":
            c, creds = _client(args[0])
            _print_state(c, args[0], creds, "--compact" in args[1:])
        elif cmd == "map":
            c, creds = _client(args[0])
            view = c.state()
            _seen(args[0], creds, view)
            print(ascii_map(view, c.player_id))
        elif cmd == "orders":
            cmd_orders(args[0], args[1])
        elif cmd == "deal":
            rest = [arg for arg in args[1:] if arg not in ("--force", "--done")]
            cmd_deal(args[0], rest[0], "--force" in args[1:], "--done" in args[1:])
        elif cmd == "done":
            cmd_done(args[0])
        elif cmd == "inbox":
            rest = [arg for arg in args[1:] if arg != "--all"]
            cmd_inbox(args[0], float(rest[0]) if rest else 0.0, "--all" in args[1:])
        elif cmd == "next":
            cmd_next(args[0], "--compact" in args[1:])
        elif cmd == "_release":
            cmd_release(args[0], args[1])
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
