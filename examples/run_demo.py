"""Start a server and a 6-player demo game you can watch in the browser.

    python examples/run_demo.py                 # then open http://localhost:8765/
    python examples/run_demo.py --exit-when-done --turn-delay 0 --max-turns 60

The game has 4 house bots (run in-process by the server) and 2 *remote*
players that connect over HTTP exactly like your agent would: the
examples/simple_bot.py strategy and a built-in bot driven through the SDK.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from agentciv.client import AgentCivClient, run_bot  # noqa: E402
from agentciv.server import create_server  # noqa: E402
from agentciv.server.manager import bot_available  # noqa: E402
from simple_bot import decide  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--max-turns", type=int, default=150)
    ap.add_argument("--turn-delay", type=float, default=0.4, help="seconds per turn, so humans can watch")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--exit-when-done", action="store_true", help="stop the server when the game ends")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    srv = create_server(args.host, args.port, args.data_dir).start_background()
    url = srv.url
    wanted = ["strategist", "economist", "rusher", "turtle"]
    house = [b if bot_available(b) else "idle" for b in wanted]
    remote_bot = "strategist" if bot_available("strategist") else "idle"
    options = dict(name="Demo: 4 house bots + 2 remote agents", max_players=6, bots=house,
                   max_turns=args.max_turns, turn_timeout=10, turn_delay=args.turn_delay)
    if args.seed is not None:
        options["seed"] = args.seed
    gid = AgentCivClient(url).create_game(**options)
    print(f"Server: {url}   API index: {url}/api")
    print(f"Watch the game: {url}/#/game/{gid}")

    results = {}

    def play(name, bot):
        results[name] = run_bot(bot, url, game_id=gid, name=name)

    threads = [threading.Thread(target=play, args=("SimpleBot", decide), daemon=True),
               threading.Thread(target=play, args=(f"remote-{remote_bot}", remote_bot), daemon=True)]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
        res = next(iter(results.values()))["result"]
        names = {p["id"]: p["name"] for p in AgentCivClient(url).game(gid)["players"]}
        print(f"Game over on turn {res['turn']}: {names.get(res['winner'])} wins by {res['condition']}.")
        print("Placements: " + ", ".join(f"{i}. {names[p]}" for i, p in enumerate(res["placements"], 1)))
        print(f"Replay: {url}/#/game/{gid}   Leaderboard: {url}/api/leaderboard")
        if not args.exit_when_done:
            print("Server still running (Ctrl-C to stop).")
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
