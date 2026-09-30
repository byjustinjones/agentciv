"""A haggling AgentCiv bot — a template for agents that barter (docs/RULES.md "Barter & deals").

    python -m agentciv.server                        # terminal 1: the game server
    python examples/barter_bot.py --quickmatch       # terminal 2: this bot
    open http://localhost:8765/                      # watch it (executed deals show up live)

Orders come from a built-in bot (``--base economist``); this file only adds
``negotiate(view)``, which :func:`agentciv.client.run_bot` calls at the start
of every turn and again whenever something lands in the inbox (a proposal, a
counter, a reply...), until shortly before the deadline. Every action it
returns is applied immediately through ``POST /api/games/{id}/diplomacy``.

Strategy (deliberately simple — make it smarter):
* value both sides of a deal at market prices (``bundle_value``);
* accept offers worth at least ``ACCEPT`` x what they cost us, counter
  near-misses once or twice at a fair price, reject the rest;
* when a resource piles up near its storage cap, offer the surplus for
  whatever we are shortest of, slightly below market to make it attractive.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # run from a checkout without installing
from agentciv.bots import get_bot  # noqa: E402
from agentciv.client import run_bot  # noqa: E402
from agentciv.engine.deals import bundle_value  # noqa: E402

GOODS = ("food", "wood", "stone")
ACCEPT = 1.0        # accept if what we get is worth >= this x what we give
COUNTER_FLOOR = 0.6  # counter offers between this and ACCEPT; reject below
MAX_COUNTERS = 2    # counters per negotiation thread
LOT = 40            # units of surplus offered at once


class BarterBot:
    name = "barter"

    def __init__(self, base: str = "economist", seed: int = 0):
        self.base = get_bot(base, seed)
        self.counters: dict[str, int] = {}  # thread id -> counters sent

    def act(self, view: dict) -> list:
        return self.base.act(view)

    # ------------------------------------------------------------ haggling
    def negotiate(self, view: dict) -> list:
        me = view["you"]
        prices = (view.get("market") or {}).get("prices") or {}
        res, caps = me["resources"], me.get("caps") or {}
        out = []
        mine = [d for d in view["deals"]["open"] if d["from"] == me["id"]]
        for d in view["deals"]["open"]:
            if d["to"] != me["id"]:
                continue
            # a deal TO us: we receive d["give"] and hand over d["get"]
            if d.get("peace") or any(k in b for b in (d["give"], d["get"]) for k in ("tiles", "per_turn")):
                out.append({"type": "reject", "deal": d["id"], "message": "I only swap goods for goods"})
                continue
            gain, cost = bundle_value(d["give"], prices), bundle_value(d["get"], prices)
            affordable = all(res.get(r, 0) >= v for r, v in d["get"].items())
            ratio = gain / cost if cost else float("inf")
            if affordable and ratio >= ACCEPT:
                out.append({"type": "accept", "deal": d["id"]})
            elif affordable and ratio >= COUNTER_FLOOR and self.counters.get(d["thread"], 0) < MAX_COUNTERS:
                self.counters[d["thread"]] = self.counters.get(d["thread"], 0) + 1
                # counter at a fair price: pay only what their side is worth to us (give/get: OUR view now)
                pay = {r: max(1, int(v * ratio)) for r, v in d["get"].items()}
                out.append({"type": "counter", "deal": d["id"], "give": pay, "get": d["give"],
                            "message": f"fair market value is {int(gain)} gold-equivalent"})
            else:
                out.append({"type": "reject", "deal": d["id"],
                            "message": "can't afford that" if not affordable else "too expensive"})
        if not mine:  # one proposal of our own at a time
            offer = self.surplus_offer(view, prices, res, caps)
            if offer:
                out.append(offer)
        return out

    def surplus_offer(self, view: dict, prices: dict, res: dict, caps: dict) -> dict | None:
        fill = {r: res.get(r, 0) / caps[r] for r in GOODS if caps.get(r)}
        if not fill:
            return None
        rich, poor = max(fill, key=fill.get), min(fill, key=fill.get)
        if fill[rich] < 0.8 or fill[poor] > 0.5 or rich == poor:
            return None
        # ask the player holding the most of what we lack
        me = view["you"]["id"]
        # fog games (rules §14): other players' stock is hidden (null); those rows are skipped
        others = [p for p in view["players"] if p["id"] != me and p.get("alive", True)
                  and p.get("resources") is not None]
        if not others:
            return None
        partner = max(others, key=lambda p: (p.get("resources") or {}).get(poor, 0))
        want = int(LOT * prices.get(rich, 1.0) / max(prices.get(poor, 1.0), 0.01) * 0.9)  # 10% below market
        if want <= 0:
            return None
        return {"type": "propose", "to": partner["id"], "give": {rich: LOT}, "get": {poor: want},
                "message": f"{LOT} {rich} for {want} {poor}, below market"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Haggling AgentCiv bot")
    ap.add_argument("--url", default="http://localhost:8765")
    ap.add_argument("--name", default="BarterBot")
    ap.add_argument("--game", help="game id to join (default: quickmatch)")
    ap.add_argument("--quickmatch", action="store_true")
    ap.add_argument("--players", type=int, default=6, help="quickmatch lobby size")
    ap.add_argument("--base", default="economist", help="built-in bot that decides the orders")
    a = ap.parse_args()
    result = run_bot(BarterBot(a.base), a.url, game_id=a.game, name=a.name, quickmatch=not a.game,
                     players=a.players, verbose=True)
    print(f"Finished: place {result['place']}, winner {result['result'].get('winner')} "
          f"by {result['result'].get('condition')}")
