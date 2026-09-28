"""Rusher: early military aggression aiming for the conquest victory.

Strategy
--------
* From turn 0 pours food/wood/gold into infantry and cavalry (the mix is
  chosen to counter what defends the target), adding siege engines when the
  target has walls (3 siege cancel one wall level).
* Marches on the nearest rival's original capital, gathers next to it and
  assaults when the engine-exact battle simulation says it wins with a
  margin. After a capture it leaves a small garrison and moves on to the next
  capital until it holds enough capitals for conquest.
* Keeps a thin economy (claims, farms and lumber mills) to feed the army,
  sells stone it doesn't need, and disbands rather than starves.
* Accepts treaties only from players it is not currently targeting and never
  proposes any.
"""
from __future__ import annotations

from .common import raw_strength, total_units, treaty_proposals_to_me
from .planner import PlannerBot


class RusherBot(PlannerBot):
    name = "rusher"

    INFLUENCE_WEIGHT = 2.0
    ALLOW_TEMPLES = False
    MAX_CITIES = 3
    MARKET_HALL = False
    WAREHOUSE = False
    DEFENSE_REACH = 1
    DEFENSE_MARGIN = 0.8
    MIN_GARRISON = 1
    CITY_GARRISON = 1
    SELL_FLOOR = 0.5
    ATTACK_RATIO = 1.1
    ECON_SHARE = 0.25          # share of wood/gold left for the economy

    def pipeline(self):
        return [self.pick_target, self.diplomacy, self.food_safety, self.defend, self.sell,
                self.build_army, self.attack, self.expand, self.develop, self.garrison_moves]

    # -- target -----------------------------------------------------------
    def pick_target(self) -> None:
        """Keep the current target unless it fell/allied or we are stuck in
        front of it; otherwise choose the rival capital with the best mix of
        short distance and weak defence (non-capitals as a fallback)."""
        w = self.w
        hard = self.memory.setdefault("hard", {})
        tgt = self.memory.get("target")
        if tgt is not None:
            c = w.cities.get(tgt)
            if c is None or c["owner"] == w.me or w.at_peace(w.me, c["owner"]) or hard.get(tgt, -1) > w.turn:
                tgt = None
        if tgt is None or w.turn % 10 == 0:
            tgt = self.best_target(hard) or tgt
        self.memory["target"] = tgt
        self.target = tgt
        self.target_owner = w.cities[tgt]["owner"] if tgt is not None else None

    def best_target(self, hard: dict):
        w = self.w
        src = w.capital if w.capital in w.my_cities else (w.my_cities[0] if w.my_cities else None)
        if w.my_armies:
            src = max(sorted(w.my_armies), key=lambda i: total_units(w.my_armies[i]))
        if src is None:
            return None
        dist = w.bfs([src], w.can_enter_fn(w.me))
        power = max(30, raw_strength(w.my_units))
        best, best_s = None, None
        for c, cc in w.cities.items():
            o = cc["owner"]
            if o == w.me or w.at_peace(w.me, o):
                continue
            d = dist.get(c)
            if d is None:
                continue
            walls = cc["buildings"].get("walls", 0)
            defense = (raw_strength(w.armies.get(c, {}).get(o, {})) + cc.get("garrison", 0)) * (1 + 0.5 * walls)
            score = d + 12.0 * defense / power + (0 if cc.get("capital") else 8)
            if hard.get(c, -1) > w.turn:
                score += 40
            if best_s is None or score < best_s:
                best, best_s = c, score
        return best

    def diplomacy(self) -> None:
        for pr in treaty_proposals_to_me(self.w):
            if pr["from"] != self.target_owner:
                self.p.accept_treaty(pr["from"])

    # -- economy tweaks -------------------------------------------------------
    def keep(self, r: str) -> int:
        return {"food": 30, "wood": 30, "stone": 20}.get(r, 0)

    def sell(self) -> None:
        # stone is only useful for siege: sell the rest; keep food/wood for troops
        w, p = self.w, self.p
        stone_keep = 20 * self.siege_needed(self.target) if self.target is not None else 20
        extra = p.budget.get("stone", 0) - stone_keep - 20
        if extra >= 10:
            p.sell("stone", extra, 0.5 * 2.0)
        cap = w.caps.get("food", 300)
        for r in ("food", "wood"):
            over = p.budget.get(r, 0) + self.raw.get(r, 0) - cap
            if over > 0:
                p.sell(r, over, 0.3)

    def build_army(self) -> None:
        w, p = self.w, self.p
        if not w.my_cities or self.target is None:
            return
        # recruit where the army stands closest to the target
        enter = w.can_enter_fn(w.me)
        dist = w.bfs([self.target], enter)
        city = min(w.my_cities, key=lambda c: (dist.get(c, 999), c))
        enemy = dict(w.armies.get(self.target, {}).get(self.target_owner, {}))
        # sustainable army size: new upkeep must be covered by food income
        # (averaged over the seasons) plus a slice of the stock
        food_raw = self.raw.get("food", 0)
        room = int(food_raw * 0.95 + p.budget.get("food", 0) / 15 - w.upkeep)
        if room <= 0:
            return
        reserve = {"food": 25, "wood": int(self.raw.get("wood", 0) * self.ECON_SHARE * 2), "gold": 10}
        self.reserved.update({k: max(self.reserved.get(k, 0), v) for k, v in reserve.items()})
        siege = self.siege_needed(self.target)
        self.recruit_army(city, enemy, min(50, room), siege=siege)

    def attack(self) -> None:
        if self.target is None:
            return
        attacked = self.offense(self.target, min_ratio=self.ATTACK_RATIO)
        # stuck in front of a fortress? give up on it for a while
        w = self.w
        adjacent = any(self.target in w.nb[i] for i in w.my_armies)
        wait = self.memory.setdefault("wait", {})
        if adjacent and not attacked:
            wait[self.target] = wait.get(self.target, 0) + 1
            if wait[self.target] > 6:
                self.memory.setdefault("hard", {})[self.target] = w.turn + 15
                wait[self.target] = 0
        elif attacked:
            wait[self.target] = 0

    def garrison_moves(self) -> None:
        # units that are not part of the attack stay put
        return
