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

Trading (§13)
-------------
* **Extortion**: demands tribute (gold per turn for 10 turns) plus a
  15-turn peace from weaker rivals its army threatens — not from its
  current target unless that assault has stalled.
* Peace with its target is worth minus the expected spoils of the conquest
  (plunder + capital, scaled by how feasible the assault looks), so buying
  it off costs real money.
* **Opportunist**: honours a contract only while the payee is militarily
  respectable (otherwise the gold goes to the army and the contract may
  default); breaks a peace treaty (50 influence) when the partner cheated
  on tribute, or when the partner's tribute has ended and it has become an
  easy prey.
"""
from __future__ import annotations

from agentciv.engine import constants as C

from .common import (DealValuer, bank_of, contract_income, raw_strength, total_units,
                     treaty_proposals_to_me)
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

    # trading
    COUNTER_SHARE = 0.65
    COUNTER_LIMIT = 1
    CONCEDE = 0.25
    TRIBUTE_TURNS = 10
    TREATY_PLEDGE = 0
    TRIBUTE_PEACE = 20
    EXTORT_RATIO = 1.3          # our army near them / their defence
    BREAK_RATIO = 1.8           # break a peace only against a much weaker partner

    def pipeline(self):
        return [self.pick_target, self.opportunism, self.diplomacy, self.food_safety, self.defend, self.sell,
                self.build_army, self.attack, self.expand, self.develop, self.garrison_moves]

    # -- negotiation ------------------------------------------------------
    def trade_setup(self, w) -> None:
        super().trade_setup(w)
        self.pick_target()

    def trade_needs(self) -> tuple:
        needs, gold = super().trade_needs()
        if self.target is not None:
            needs["stone"] = max(needs["stone"], 20 * self.siege_needed(self.target))
        return needs, gold

    def conquest_value(self, q: str) -> float:
        """Expected spoils of taking ``q``'s capital (plunder + a capital)."""
        w = self.w
        v = DealValuer(w) if getattr(self, "tv", None) is None or self.tv.w is not w else self.tv
        res = (w.players.get(q) or {}).get("resources", {}) or {}
        plunder = sum(res.get(r, 0) * C.PLUNDER_FRACTION * v.prices.get(r, 1.0) for r in C.TRADABLE)
        mine = max(30.0, float(raw_strength(w.my_units)))
        feas = min(1.0, mine / max(1.0, v.defense(q)))
        if self.memory.get("hard", {}).get(self.memory.get("target"), -1) > w.turn:
            feas *= 0.3
        return (plunder + 250.0) * feas

    def peace_bias(self) -> dict:
        b = super().peace_bias()
        if self.target_owner is not None:
            b[self.target_owner] = b.get(self.target_owner, 0.0) - self.conquest_value(self.target_owner)
        return b

    def trade_proposals(self) -> list:
        """Tribute + peace demanded from weaker rivals our army threatens."""
        w, v = self.tw, self.tv
        mine = raw_strength(w.my_units)
        if mine < 40:
            return []
        stalled = self.memory.get("hard", {}).get(self.memory.get("target"), -1) > w.turn
        out = []
        for q in self.partners():
            if q in w.treaties or (q == self.target_owner and not stalled) or w.sign_problem(w.me, q):
                continue
            t = v.threat(w.me, q)
            if t < 40 or t < self.EXTORT_RATIO * v.defense(q):
                continue
            pl = w.players.get(q) or {}
            inc = (pl.get("income") or {}).get("gold", 0)
            gold = (pl.get("resources") or {}).get("gold", 0)
            x = int(max(3, min(60, 0.5 * inc + gold / 40.0)))
            get = {"per_turn": {"gold": x}, "turns": self.TRIBUTE_TURNS}
            deal = {"from": w.me, "to": q, "give": {}, "get": get, "peace": self.TRIBUTE_PEACE}
            if v.deal_gain(deal, q) < 0:
                x = max(3, x // 2)
                get = {"per_turn": {"gold": x}, "turns": self.TRIBUTE_TURNS}
                deal["get"] = get
            out.append({"to": q, "give": {}, "get": get, "peace": self.TRIBUTE_PEACE, "kind": "tribute",
                        "value": v.deal_gain(deal),
                        "text": f"pay {x} gold/turn for {self.TRIBUTE_TURNS} turns and we keep the peace"})
        out.sort(key=lambda p: -p["value"])
        return out[:1]

    # -- opportunism in act() ---------------------------------------------
    def honour_contract(self, c: dict) -> bool:
        w = self.w
        if bank_of(w):
            return True       # a default would take the rest of the obligation from our bank
        payee = (w.players.get(c.get("payee")) or {}).get("military_power", 0) or 0
        return payee >= 0.7 * max(1, raw_strength(w.my_units))

    def opportunism(self) -> None:
        """Break a peace with a partner that cheated on tribute, or with a
        weak partner that no longer pays."""
        w, p = self.w, self.p
        cheat = self.memory.setdefault("cheaters", {})
        for e in w.events:
            if e.get("type") == "contract_default" and e.get("payee") == w.me:
                cheat[e.get("payer")] = w.turn
        from .common import break_cost
        if w.res.get("influence", 0) < w.break_influence() + 10 or self.memory.get("broke", -99) > w.turn - 12:
            return
        paying = contract_income(w)
        v = DealValuer(w)
        best = None
        for q in sorted(w.treaties):
            if q not in w.alive or self.relic_runner(q):
                continue
            payer = any(c.get("payer") == q and c.get("payee") == w.me for c in w.view.get("contracts", []) or [])
            if payer and paying.get("gold", 0) > 0 and q not in cheat:
                continue
            t = v.threat(w.me, q)
            ratio = t / max(1.0, v.defense(q))
            if q in cheat and cheat[q] >= w.turn - 10:
                ratio *= 1.5
            if ratio >= self.BREAK_RATIO and (best is None or ratio > best[0]) \
                    and self.conquest_value(q) > break_cost(w, q)[1]:
                best = (ratio, q)
        if best is not None:
            p.orders.append({"type": "break_treaty", "with": best[1]})
            self.memory["broke"] = w.turn

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
                self.p.accept_treaty(pr["from"], self.pledge())

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
