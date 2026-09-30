"""Generate ``nofog_golden.json``: digests of every view of two short bot
games, recorded at the base commit before fog of war existed (0d4a71c).

``tests/test_fog_off_identity.py`` replays the recorded orders and checks
that a game created without ``fog`` still produces byte-identical views (the
only allowed difference is the static ``costs.fog`` rules block).

Run from the repository root:  python tests/data/make_nofog_golden.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agentciv.bots import get_bot
from agentciv.engine import Game, GameConfig, rules_json

SEEDS = (3, 11)
PLAYERS = 5
TURNS = 12
BOTS = ("strategist", "rusher", "economist", "turtle", "random")
OUT = Path(__file__).with_name("nofog_golden.json")


def digest(obj) -> str:
    """sha256 of canonical JSON, ignoring the ``costs.fog`` block."""
    if isinstance(obj, dict) and isinstance(obj.get("costs"), dict) and "fog" in obj["costs"]:
        obj = dict(obj, costs={k: v for k, v in obj["costs"].items() if k != "fog"})
    data = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(data.encode()).hexdigest()


def snapshot(g: Game, step_events=None) -> dict:
    out = {pid: digest(g.player_view(pid)) for pid in (p.id for p in g.players)}
    out["public"] = digest(g.spectator_view())
    out["full"] = digest(g.spectator_view(full=True))
    if step_events is not None:
        out["step"] = digest(step_events)
    return out


def new_game(seed: int) -> Game:
    g = Game(GameConfig(seed=seed, max_turns=40, game_id=f"golden{seed}"))
    for k in range(PLAYERS):
        g.add_player(f"B{k + 1}")
    g.start()
    return g


def record(seed: int) -> dict:
    g = new_game(seed)
    bots = {p.id: get_bot(BOTS[k], seed=seed * 131 + k) for k, p in enumerate(g.players)}
    turns = [{"orders": {}, "digests": snapshot(g)}]
    for _ in range(TURNS):
        orders = {}
        for pid in g.alive_players():
            o = bots[pid].act(g.player_view(pid))
            orders[pid] = json.loads(json.dumps(o))
            g.submit_orders(pid, orders[pid])
        ev = g.step()
        turns.append({"orders": orders, "digests": snapshot(g, ev)})
        if g.finished:
            break
    return {"seed": seed, "turns": turns}


def main() -> None:
    data = {"base_commit": "0d4a71c", "players": PLAYERS, "rules": digest(rules_json()),
            "games": [record(s) for s in SEEDS]}
    OUT.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
