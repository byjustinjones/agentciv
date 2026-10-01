"""``python -m agentciv.puzzles list`` and
``python -m agentciv.puzzles run <id> (--bot NAME | --solution | --baseline) [--json]``."""
from __future__ import annotations

import argparse
import json
import sys

from ..bots import BOT_NAMES, get_bot
from . import PUZZLES, get_puzzle
from .runner import run_puzzle


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agentciv.puzzles",
                                 description="Diagnostic positions (docs/PUZZLES.md), played offline.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="list the puzzles")
    ls.add_argument("--json", action="store_true", help="machine-readable output")
    run = sub.add_parser("run", help="play one solver through a puzzle and print its score")
    run.add_argument("puzzle", choices=list(PUZZLES))
    who = run.add_mutually_exclusive_group(required=True)
    who.add_argument("--bot", choices=BOT_NAMES, help="a registry bot in the puzzle seat")
    who.add_argument("--solution", action="store_true", help="the shipped reference solution")
    who.add_argument("--baseline", action="store_true", help="the do-nothing baseline")
    run.add_argument("--seed", type=int, default=0, help="seed of the registry bot (default 0)")
    run.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        rows = [p.info() for p in PUZZLES.values()]
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for r in rows:
                print(f"{r['puzzle']:13s} {r['horizon']:2d} turns  {r['title']}\n    {r['objective']}\n"
                      f"    score: {r['scoring']}")
        return 0

    pz = get_puzzle(args.puzzle)
    if args.solution:
        solver, label = pz.solution(), "solution"
    elif args.baseline:
        solver, label = pz.baseline(), "baseline"
    else:
        solver, label = get_bot(args.bot, args.seed), f"bot {args.bot} (seed {args.seed})"
    res = run_puzzle(pz.id, solver)
    out = {"puzzle": pz.id, "solver": label, "score": res["score"], "explanation": res["explanation"],
           "turns": res["turns"]}
    if args.json:
        print(json.dumps(out))
    else:
        print(f"{pz.id} / {label}: {res['score']}\n  {res['explanation']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
