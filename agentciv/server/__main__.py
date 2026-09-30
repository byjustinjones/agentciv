"""Command-line entry point: ``python -m agentciv.server`` / ``agentciv-server``."""
from __future__ import annotations

import argparse
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentciv-server",
                                     description="Run the AgentCiv game server (HTTP API + spectator GUI).")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (0.0.0.0 for all; default %(default)s)")
    parser.add_argument("--port", type=int, default=8765, help="port (default %(default)s; 0 = any free port)")
    parser.add_argument("--data-dir", default="data", help="where replays and the leaderboard are stored")
    parser.add_argument("--web-dir", default=None, help="static GUI directory (default: the repo's web/)")
    parser.add_argument("--spectator-key", default=os.environ.get("AGENTCIV_SPECTATOR_KEY"),
                        help="operator key for full live views (default: AGENTCIV_SPECTATOR_KEY; unset = off)")
    parser.add_argument("--open-ratings", action="store_true",
                        help="rate every game created with rated=true (default: only games under standard "
                             "conditions: server seed, full turn limit, a deadline, no idle/random bots picked)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .app import serve
    try:
        serve(args.host, args.port, args.data_dir, args.web_dir, open_ratings=args.open_ratings,
              spectator_key=args.spectator_key)
    except OSError as e:
        print(f"error: could not start server: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
