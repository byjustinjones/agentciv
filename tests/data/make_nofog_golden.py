"""Generate ``nofog_golden.json``: digests of every view of two short bot
games, recorded at the base commit before fog of war existed (0d4a71c).

Every turn the bots first run ``NEGOTIATION_ROUNDS`` rounds of
``negotiate()`` through ``Game.diplomacy`` (live deals: open deals with
their ``problem``, recent deals, the deal log, contracts, the inbox), then
submit their orders.

``tests/test_fog_off_identity.py`` replays the recorded diplomacy actions and
orders and checks that a game created without ``fog`` still produces
byte-identical views, diplomacy results and inboxes (the only allowed
difference is the static ``costs.fog`` rules block).

Run with the base commit's package on the path, e.g.:
  git archive 0d4a71c | tar -x -C /tmp/base
  PYTHONPATH=/tmp/base python tests/data/make_nofog_golden.py
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agentciv.bots import get_bot
from agentciv.engine import Game, GameConfig, rules_json

SEEDS = (3, 11)
PLAYERS = 5
TURNS = 24
BOTS = ("strategist", "rusher", "economist", "turtle", "random")
NEGOTIATION_ROUNDS = 3
OUT = Path(__file__).with_name("nofog_golden.json")


def digest(obj) -> str:
    """sha256 of canonical JSON, ignoring the ``costs.fog`` block."""
    if isinstance(obj, dict) and isinstance(obj.get("costs"), dict) and "fog" in obj["costs"]:
        obj = dict(obj, costs={k: v for k, v in obj["costs"].items() if k != "fog"})
    data = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(data.encode()).hexdigest()


def snapshot(g: Game, step_events=None, inbox: bool = False) -> dict:
    out = {pid: digest(g.player_view(pid)) for pid in (p.id for p in g.players)}
    if inbox:
        out.update({f"inbox:{p.id}": digest(g.inbox(p.id)) for p in g.players})
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


def probes(g: Game, talks: list) -> None:
    """Scripted diplomacy that reaches the deal paths bots rarely take,
    appended to ``talks`` as ``[pid, actions, digest(results)]``: an over-ask
    (a ``problem`` for the bot to quote), the same gift offered and accepted
    twice (the second accept fails on stock), a contract, and land that does
    not touch the receiver's territory (the accept fails)."""
    def run(pid, acts):
        res = g.diplomacy(pid, acts)
        talks.append([pid, acts, digest(res)])
        return [r.get("deal") for r in res]

    alive = set(g.alive_players())
    if {"p1", "p2"} <= alive:
        run("p1", [{"type": "propose", "to": "p2", "give": {"wood": 1},
                    "get": {"food": 900, "wood": 900, "stone": 900, "gold": 900}, "message": "probe"}])
    food = g.player("p3").resources.get("food", 0) if "p3" in alive else 0
    if food > 0 and {"p4", "p5"} <= alive:
        ids = run("p3", [{"type": "propose", "to": q, "give": {"food": food}, "message": "gift"}
                         for q in ("p4", "p5")])
        for q, d in zip(("p4", "p5"), ids):
            if d:
                run(q, [{"type": "accept", "deal": d}])
    if {"p4", "p2"} <= alive and g.turn % 3 == 0:
        ids = run("p4", [{"type": "propose", "to": "p2", "give": {"per_turn": {"gold": 1}, "turns": 2},
                          "message": "stipend"}])
        if ids[0]:
            run("p2", [{"type": "accept", "deal": ids[0]}])
    land = [i for i, o in enumerate(g.owner) if o == "p2" and i not in g.cities]
    if land and {"p1", "p2"} <= alive:
        x, y = g.xy(land[-1])
        ids = run("p2", [{"type": "propose", "to": "p1", "give": {"tiles": [[x, y]]}, "message": "land"}])
        if ids[0]:
            run("p1", [{"type": "accept", "deal": ids[0]}])


def record(seed: int) -> dict:
    g = new_game(seed)
    bots = {p.id: get_bot(BOTS[k], seed=seed * 131 + k) for k, p in enumerate(g.players)}
    turns = [{"orders": {}, "digests": snapshot(g)}]
    for _ in range(TURNS):
        talks = []
        probes(g, talks)
        for _ in range(NEGOTIATION_ROUNDS):
            for pid in g.alive_players():
                acts = json.loads(json.dumps(bots[pid].negotiate(g.player_view(pid))))
                if acts:
                    talks.append([pid, acts, digest(g.diplomacy(pid, acts))])
        talked = snapshot(g, inbox=True)
        orders = {}
        for pid in g.alive_players():
            o = bots[pid].act(g.player_view(pid))
            orders[pid] = json.loads(json.dumps(o))
            g.submit_orders(pid, orders[pid])
        ev = g.step()
        turns.append({"diplomacy": talks, "after_diplomacy": talked, "orders": orders,
                      "digests": snapshot(g, ev)})
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
