"""A minimal remote AgentCiv bot — copy this file to start your own agent.

    python -m agentciv.server                       # terminal 1: the game server
    python examples/simple_bot.py --quickmatch      # terminal 2: this bot
    open http://localhost:8765/                     # watch it play

Strategy: grab land, improve every tile, keep a small army. Replace `decide`
with something smarter. Rules: GET /api/rules (docs/RULES.md).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # run from a checkout without installing
from agentciv.client import run_bot  # noqa: E402

# Which improvement to build on which terrain (see the rules for costs).
IMPROVEMENT = {".": "farm", "f": "lumber_mill", "h": "quarry", "g": "mine"}
# Claim preference: gold, hills, forest, plains (relics cannot be claimed, only occupied).
CLAIM_ORDER = "ghf."


def decide(view: dict) -> list:
    """Called once per turn with your player view; returns a list of orders."""
    me, m = view["you"], view["map"]
    owner, terrain = m["owner"], m["terrain"]
    relics = {(r["x"], r["y"]) for r in m["relics"]}
    built = {(i["x"], i["y"]) for i in m["improvements"]}
    cities = {(c["x"], c["y"]) for c in view["cities"]}
    res, orders = dict(me["resources"]), []

    def mine(x, y):
        return 0 <= y < m["height"] and 0 <= x < m["width"] and owner[y][x] == me["id"]

    # 1. Claim the best unowned tile next to our territory (costs influence).
    candidates = []
    for y in range(m["height"]):
        for x in range(m["width"]):
            t = "*" if (x, y) in relics else terrain[y][x]
            if owner[y][x] is None and t in CLAIM_ORDER and any(mine(x + dx, y + dy) for dx, dy in
                                                                 ((1, 0), (-1, 0), (0, 1), (0, -1))):
                candidates.append((CLAIM_ORDER.index(t), x, y))
    if candidates and res["influence"] >= me["claim_cost"]:
        _, x, y = min(candidates)
        orders.append({"type": "claim", "at": [x, y]})

    # 2. Improve owned tiles, paying as we go (costs come from view["costs"]).
    for y in range(m["height"]):
        for x in range(m["width"]):
            b = IMPROVEMENT.get(terrain[y][x])
            if b and mine(x, y) and (x, y) not in built | cities | relics:
                cost = view["costs"]["buildings"]["improvements"][b]["cost"]
                if all(res.get(r, 0) >= v for r, v in cost.items()):
                    orders.append({"type": "build", "at": [x, y], "building": b})
                    for r, v in cost.items():
                        res[r] -= v

    # 3. Keep ~40 military power at home: combat is optional, but undefended capitals get rushed.
    mine_power = next(p["military_power"] for p in view["players"] if p["id"] == me["id"])
    if mine_power < 40 and res["food"] >= 30 and res["wood"] >= 20 and res["gold"] >= 10:
        orders.append({"type": "recruit", "city": me["capital"], "unit": "infantry", "count": 1})
    return orders


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Simple AgentCiv bot")
    ap.add_argument("--url", default="http://localhost:8765")
    ap.add_argument("--name", default="SimpleBot")
    ap.add_argument("--game", help="game id to join (default: quickmatch)")
    ap.add_argument("--quickmatch", action="store_true")
    ap.add_argument("--players", type=int, default=6, help="quickmatch lobby size")
    a = ap.parse_args()
    # run_bot loops wait -> state -> decide -> submit until the game ends.
    result = run_bot(decide, a.url, game_id=a.game, name=a.name, quickmatch=not a.game, players=a.players,
                     verbose=True)
    print(f"Finished: place {result['place']}, winner {result['result'].get('winner')} "
          f"by {result['result'].get('condition')}")
