"""Strategist: the strongest built-in bot.

It plays a strong economy like the economist, but reads the whole board
every turn and adapts:

* **Victory ETA model.** For itself and every rival it estimates how many
  turns each victory condition is away (economic and influence from the
  observed per-turn growth of gold/influence, wonder from the value of the
  remaining stages and the observed stage pace, relics from walking
  distances and guarded relics, conquest from capitals held). Its own race is
  the fastest of economic / wonder / influence (relics only when clearly
  faster: relic streaks get contested), with hysteresis; once the ETA is
  close it commits (reserves wonder stages and buys what they need, builds
  temples everywhere, or hoards gold).
* **Raids.** It captures a rival capital (plunder: half the victim's
  resources, conquest progress) or a wonder city (the wonder is destroyed)
  only when the value clearly exceeds the cost of a strike force that beats
  the defenders, their neighbours and one turn of emergency recruiting. The
  force gathers out of the target's sight and strikes in one go (breaking a
  treaty first if needed).
* **Relics.** Relics are taken by occupation: in a relic campaign it marches
  guards onto the cheapest relics and keeps enough units on each to hold
  against nearby armies; otherwise it parks a unit on free relics next to
  its army (+influence, +score).
* **Threat assessment.** Each hostile army is assigned to the city it is
  closest to; the city recruits the best counter (and raises walls, buying
  food/wood if needed) until the engine-exact battle simulation says it
  holds with a margin. Armies sitting in their own cities count only partly.
* **Diplomacy.** 50-turn treaties with strong armies (so only the weak need
  watching), no treaties with hoarders (they are raid targets) or with
  players close to winning.
* **Economy.** Improvements by ROI with market-price-based values, early
  market hall, warehouse before hitting caps, season-aware food planning,
  market sales split so the batch price stays near the spot price, and a
  random back-off when a claim/settle was contested (two players retrying
  the same tile would both fail forever).
* **Trading (§13): the skilled trader.**

  - *Haggling*: incoming offers are accepted only if they leave it a share
    of the estimated joint surplus that starts high (anchoring, 0.8) and
    drops with every counter in the thread (concessions converging to
    0.5; a bit less in the last round of a turn); otherwise it counters
    (up to 3 times) with the gold term moved to that share.
  - *Exploiting needs*: it sells its surplus to the players who need it
    most and buys what its race needs from players with spare stock, priced
    so the partner keeps only about its acceptance margin; but it never
    sells to the leader, nor anything that would bring a player within
    reach of victory (e.g. stone to a wonder builder only while that
    doesn't make stage 3+ affordable).
  - *Funding its path with contracts*: when committed to the wonder or
    influence race and short of gold, it borrows (gold now, gold per turn
    later); its own instalments only count until its expected victory.
  - *Tribute*: accepts peace-for-gold from players it has no plans against
    (never from its raid/prey/block targets or the leader); pays a tribute
    demand only when the threat is worth more than the tribute.
"""
from __future__ import annotations

import copy
import math
import random

from agentciv.engine import combat as CB
from agentciv.engine import constants as C
from agentciv.engine.rules import building_cost

from .common import (CAPPED, add_units, bank_limit, base_price, best_counter, danger,
                     season_mods, sell_price, simulate_attack, threat_to,
                     treaty_proposals_to_me, wonder_city)
from .planner import PlannerBot

INF = float("inf")


class StrategistBot(PlannerBot):
    name = "strategist"

    INFLUENCE_WEIGHT = 3.0
    ALLOW_TEMPLES = True
    TEMPLE_BIAS = 0.9
    MAX_CITIES = 7
    SETTLE_RADIUS = 8
    RELIC_INTEREST = 14.0
    DEFENSE_REACH = 3
    DEFENSE_MARGIN = 1.1
    GARRISON_THREAT_SHARE = 0.4   # armies sitting in their own cities are mostly garrisons
    MIN_GARRISON = 2
    CITY_GARRISON = 1
    SELL_FLOOR = 0.7
    HISTORY = 6
    COMMIT_HORIZON = 30           # only divert resources to a victory path this close
    USE_PREY = False              # (legacy) conquer weakly defended rival capitals
    USE_BLOCK = False             # (legacy) attack the rival about to win; raids replace both
    BLOCK_FEASIBILITY = True      # ...but only if we can get there in time
    EARLY_TREATY_TURNS = 6        # accept any harmless treaty this early
    CONQUEST_SLACK = 1.3          # conquest ETAs are rough: block even if a bit late
    RIVAL_POTENTIAL = 0.0         # share of a rival's production assumed sellable
    #                               for gold (0 = observed growth only; higher values
    #                               raised false alarms in tournaments)
    TRAVEL_FACTOR = 1.0           # approach speed assumed for blocking plans (tiles/turn)
    USE_RELICS = True             # occupy relics (campaign / cheap grabs)
    PATHS = ("economic", "wonder", "influence")   # races we run ourselves
    RELIC_ETA_FACTOR = 1.5        # >1: relic campaigns only when clearly faster
    INFLUENCE_GOAL_WEIGHT = 9.0   # value of influence while racing for it (temples)
    INFLUENCE_EARLY_COMMIT = 15   # commit this much earlier to influence (temples pay late)
    PREY_GOLD = 1200              # a rival with this much gold is worth plundering
    TREATY_TURNS = 50             # length of the treaties we propose (long peace with strong armies)
    BLOCK_PATIENCE = 25           # give up a block after this many turns...
    BLOCK_COOLDOWN = 15           # ...for this many turns

    # trading
    COUNTER_LIMIT = 3
    WIN_GUARD = 0.55              # stricter than the others: nobody gets near a win with our help
    ANCHOR_SHARE = 0.8            # first ask: 80% of the joint surplus...
    MIN_SHARE = 0.5               # ...conceding toward an even split
    CONCESSION = 0.12             # per counter in the thread
    MAX_NEW_PER_TURN = 3
    LOAN_MAX = 1500
    LOAN_TURNS = 30               # long loans: instalments after our victory are never paid
    LOAN_INTEREST = 0.25
    LOAN_ETA = 35                 # borrow only this close to our expected victory
    LOAN_INCOME_SHARE = 0.3       # instalments at most this share of our potential gold income
    LEADER_REFUSE = 0.45          # refuse the leader once its progress reaches this
    CONCEDE = 0.3                 # concede 30% of the gap per counter (others: 50%)
    BARGAIN_PRICE = 0.3           # probe offers: their spare stock at 30% of spot
    SELL_PREMIUM = 1.1            # our sales: at least 110% of what the market pays

    def pipeline(self):
        return [self.observe, self.diplomacy, self.food_safety, self.plan_site, self.defend,
                self.plan_goal_spending, self.relic_ops, self.plan_military, self.sell, self.goal_spending,
                self.field_army, self.expand, self.develop, self.garrison_moves, self.espionage]

    ESPIONAGE_LEAD = 0.5          # fog games: rival progress that triggers a treasury spy
    COUNTERINTEL_PER_TURN = 50    # fog games: gold into counter-intelligence while our own lead is >= that
    ESPIONAGE_RESERVE = 100       # gold kept back

    def espionage(self) -> None:
        """Fog games only (rules §14): pay counter-intelligence while close to
        an economic/influence win; spy on the treasury of the rival closest to
        one (no fresh report), investing twice its base rating."""
        w, p = self.w, self.p
        if not (w.view.get("fog") or {}).get("active"):
            return
        keep = {"gold": self.ESPIONAGE_RESERVE}

        def lead(row: dict) -> float:
            vp = row.get("victory_progress") or {}
            return max(float(vp.get("economic") or 0), float(vp.get("influence") or 0))

        if lead(w.players.get(w.me) or {}) >= self.ESPIONAGE_LEAD:
            cost = {"gold": self.COUNTERINTEL_PER_TURN}
            if p.can(cost, keep):
                p.orders.append({"type": "counterintel", "invest": self.COUNTERINTEL_PER_TURN})
                p.pay(cost)
        # a failed mission doubles the next investment against that target;
        # a report resets it (last turn's spy_report events, counted once per turn)
        fails = self.memory.setdefault("spy_fails", {})
        if self.memory.get("spy_seen_turn") != w.turn:
            self.memory["spy_seen_turn"] = w.turn
            for e in w.view.get("events") or ():
                if e.get("type") == "spy_report" and e.get("player") == w.me:
                    t = e.get("target")
                    fails[t] = fails.get(t, 0) + 1 if e.get("outcome") == "failed" else 0
        known = {r.get("target") for r in w.view.get("intel") or () if r.get("mission") == "treasury"}
        cands = sorted((-lead(w.players[q]), q) for q in w.rivals
                       if q in w.players and q not in known and lead(w.players[q]) >= self.ESPIONAGE_LEAD)
        if not cands:
            return
        q = cands[0][1]
        cities = int(w.players[q].get("cities") or 0)
        base = 2 * (C.CI_BASE + C.CI_PER_CITY * cities) * 2 ** min(5, fails.get(q, 0))
        invest = min(C.SPY_MAX_INVEST, max(C.SPY_MIN_INVEST, base))
        if p.can({"gold": invest}, keep):
            p.orders.append({"type": "spy", "target": q, "mission": "treasury", "invest": invest})
            p.pay({"gold": invest})

    def plan_military(self) -> None:
        self.raid = None
        self.objective = None
        self.objective_ratio = 1.15
        if self.USE_RAIDS:
            self.plan_raid()
        elif self.USE_BLOCK:
            self.block()

    # ------------------------------------------------------------------
    # negotiation (§13)
    # ------------------------------------------------------------------
    # state that observe()/plan_goal_spending() update; negotiation must not
    # leave traces in it (act() recomputes it once per turn, and repeated
    # updates would change the path hysteresis)
    _OBSERVED_MEMORY = ("hist", "wstage", "relic_progress", "path")
    _GOAL_KNOBS = ("SELL_FLOOR", "MIN_ROI", "TEMPLE_BIAS", "INFLUENCE_WEIGHT",
                   "BUY_FOR_IMPROVEMENTS", "INFLUENCE_RESERVE")

    def trade_setup(self, w) -> None:
        super().trade_setup(w)
        mem = {k: copy.deepcopy(self.memory[k]) for k in self._OBSERVED_MEMORY if k in self.memory}
        knobs = {k: self.__dict__[k] for k in self._GOAL_KNOBS if k in self.__dict__}
        try:
            self.observe()
            self.plan_goal_spending()
        finally:
            for k in self._OBSERVED_MEMORY:
                if k in mem:
                    self.memory[k] = mem[k]
                else:
                    self.memory.pop(k, None)
            for k in self._GOAL_KNOBS:
                if k in knobs:
                    self.__dict__[k] = knobs[k]
                else:
                    self.__dict__.pop(k, None)

    def goal_order(self):
        """(city, next wonder stage cost) when the wonder is our race."""
        w = self.w
        wc = wonder_city(w)
        if not (self.goal == "wonder" or (wc is not None and w.cities[wc].get("wonder_stage", 0) >= 3)):
            return None
        home = wc or self.home()
        if home is None:
            return None
        st = w.cities[home].get("wonder_stage", 0)
        if st >= C.WONDER_VICTORY_STAGE:
            return None
        return home, building_cost("wonder", st + 1)

    def goal_shortfall(self):
        """(missing stone/wood, gold still missing) for the next step of our
        race: a wonder stage, or temples for the influence race."""
        w = self.w
        stock = w.res
        go = self.goal_order()
        if go is not None:
            cost = go[1]
        elif self.goal == "influence":
            k = min(6, self.temple_slots())
            if k <= 0:
                return None
            tc = C.IMPROVEMENTS["temple"]["cost"]
            cost = {"stone": tc["stone"] * k, "wood": 0, "gold": tc["gold"] * k}
        else:
            return None
        miss = {r: max(0, cost.get(r, 0) - stock.get(r, 0)) for r in ("stone", "wood")}
        gold_needed = cost.get("gold", 0) + sum(q * self.price(r) * 1.1 for r, q in miss.items())
        return miss, max(0, int(gold_needed - stock.get("gold", 0)))

    def temple_slots(self) -> int:
        w = self.w
        ok = C.IMPROVEMENTS["temple"]["terrain"]
        return sum(1 for i in w.my_tiles if i not in w.cities and i not in w.improvement and w.terrain[i] in ok)

    def trade_needs(self) -> tuple:
        needs, gold = super().trade_needs()
        w = self.w
        sf = self.goal_shortfall()
        go = self.goal_order()
        if go is not None:
            cost = go[1]
            now = sf is not None and sf[1] <= 0
            for r in ("stone", "wood"):
                needs[r] = max(needs[r], cost[r] if now else min(cost[r], w.caps.get(r, C.STORAGE_BASE)))
            gold += cost["gold"]
        elif self.goal == "influence" and sf is not None:
            k = min(6, self.temple_slots())
            needs["stone"] = max(needs["stone"], 20 * k)
            gold += 20 * k
        elif self.goal == "economic":
            gold = max(gold, w.res.get("gold", 0))      # gold is the race: spend none of it
        if self.goal != "economic" and self.my_path != "economic":
            # our stock feeds our own growth (settlers, buildings, the
            # wonder): sell only what would overflow the storage cap
            mods = season_mods(w.turn)
            for r in CAPPED:
                cap = w.caps.get(r, C.STORAGE_BASE)
                inc = int(self.raw.get(r, 0) * mods.get(r, 1.0)) - (w.upkeep if r == "food" else 0)
                needs[r] = max(needs[r], cap - max(0, inc))
        return needs, gold

    def bank_wanted(self) -> bool:
        return "economic" in self.PATHS and self.memory.get("path") == "economic"

    def trade_horizon(self):
        # our instalments only matter until we expect to have won
        if self.goal in ("wonder", "influence", "economic") and self.my_eta < self.remaining:
            return int(self.my_eta) + 3
        return None

    def refuse_partner(self, q: str, deal: dict | None = None) -> bool:
        if q == self.leader and (self.danger or danger(self.w, q) >= self.LEADER_REFUSE):
            # ...except to borrow from it on our terms: the gold leaves the
            # leader now and comes back (in part) only after we have won
            return not (deal is not None and self.borrowing(deal))
        return False

    def borrowing(self, deal: dict) -> bool:
        """Is ``deal`` a loan to us in a thread we opened (gold now, gold
        per turn later)?"""
        from .trading import deal_kind
        w = self.w
        if deal_kind(deal) != "loan":
            return False
        mine_out = deal.get("give") if deal.get("from") == w.me else deal.get("get")
        mine_in = deal.get("get") if deal.get("from") == w.me else deal.get("give")
        mine_out, mine_in = mine_out or {}, mine_in or {}
        if not mine_out.get("per_turn") or any(mine_out.get(r) for r in C.TRADABLE) or set(mine_in) - {"gold"}:
            return False
        return deal.get("from") == w.me or self.my_last_offer(deal) is not None

    def peace_bias(self) -> dict:
        b = super().peace_bias()
        w = self.w
        raid = self.memory.get("raid")
        targets = {self.memory.get("block_owner"), self.memory.get("prey_owner"),
                   raid.get("owner") if isinstance(raid, dict) else None}
        if self.danger:
            targets.add(self.leader)
        # contenders may have to be blocked: no peace with them
        targets |= {q for q in w.rivals if self.contender(q)}
        for q in targets:
            if q is not None and q != w.me:
                b[q] = b.get(q, 0.0) - 1000.0
        return b

    def counter_share(self, d: dict, n: int) -> float:
        return max(self.MIN_SHARE, self.ANCHOR_SHARE - self.CONCESSION * n)

    def rival_path(self, q: str):
        et = self.etas.get(q, {})
        return min(et, key=lambda k: (et[k], k)) if et else None

    def contender(self, q: str) -> bool:
        """A rival in the race with us: its victory ETA is not far behind
        ours, or it has made real progress."""
        w = self.w
        if q is None or q == w.me:
            return False
        e = min(self.etas.get(q, {}).values(), default=INF)
        mine = self.my_eta if self.my_eta < INF else w.max_turns - w.turn
        if e <= 1.3 * mine + 10:
            return True
        prog = w.progress(q)
        return max(float(prog.get(k, 0) or 0) for k in ("wonder", "influence", "relics", "economic")) >= 0.45

    def veto(self, q: str, bundle_to_q: dict, deal: dict):
        """Never pay instalments except for loans we asked for ourselves,
        and never feed a contender's own race."""
        from .trading import deal_kind
        w = self.w
        if bundle_to_q.get("per_turn"):
            ours = deal.get("from") == w.me or self.my_last_offer(deal) is not None
            if deal.get("peace"):
                pass                 # tribute for peace: the valuation decides
            elif not (ours and deal_kind(deal) == "loan"):
                return "I only borrow on my own terms"
        v = self.tv
        if w.hostile(w.me, q) and (bundle_to_q.get("food", 0) or bundle_to_q.get("wood", 0)):
            # don't arm an army that could march on us (or on anyone: conquest)
            mp = float((w.players.get(q) or {}).get("military_power", 0) or 0)
            mine = float((w.players.get(w.me) or {}).get("military_power", 0) or 0)
            if mp >= 0.5 * max(1.0, mine) or self.arms_threat(q, bundle_to_q) or self.rival_path(q) == "conquest":
                return "no war supplies for you"
        if not self.contender(q) or self.borrowing(deal):
            return None
        path = self.rival_path(q)
        val = {r: int(bundle_to_q.get(r, 0) or 0) * v.prices.get(r, 1.0) for r in C.TRADABLE}
        per = bundle_to_q.get("per_turn") or {}
        if per:
            val["gold"] += v.installment(per) * int(bundle_to_q.get("turns", 0) or 0)
        on_path = {"economic": ("gold",), "wonder": ("stone", "wood", "gold"),
                   "influence": ("stone", "gold")}.get(path, ())
        if sum(val[r] for r in on_path) >= 30:
            return "you are racing me"
        return None

    def accept_threshold(self, d: dict, gain: float, their: float) -> float:
        base = self.ACCEPT_MARGIN + self.ACCEPT_FRACTION * self.tv.size(d)
        n = self.counters_in(d)
        last = self.tmem.get("round", 0) >= 2 or int(d.get("expires_turn", 99999)) <= self.tw.turn
        share = self.MIN_SHARE - 0.05 if last else max(self.MIN_SHARE, self.ANCHOR_SHARE - self.CONCESSION * (n + 1))
        thr = max(base, share * (gain + their))
        if self.contender(d.get("from")):
            thr = max(thr, their + base)      # a rival in the race must not gain more than we do
        return thr

    def bargain_offers(self) -> list:
        """Opponent modelling: offer to buy a partner's spare stock (all of
        it, in one bundle) far below market value. Rational partners refuse
        (and the offer then waits longer each time, 4 up to 16 turns);
        partners that accept such offers get them again."""
        w, v = self.tw, self.tv
        out = []
        gold = v.stock(w.me).get("gold", 0)
        for q in self.partners():
            get = {}
            for r in CAPPED:
                spare = int(v.stock(q).get(r, 0) - v.needs(q).get(r, 0))
                if spare >= self.MIN_LOT:
                    get[r] = min(spare, 300)
            if not get:
                continue
            price = max(1, int(sum(k * v.prices[r] for r, k in get.items()) * self.BARGAIN_PRICE))
            if price > gold // 3:
                continue
            deal = {"from": w.me, "to": q, "give": {"gold": price}, "get": get, "peace": None}
            out.append({"to": q, "give": {"gold": price}, "get": get, "kind": "bargain",
                        "value": v.deal_gain(deal), "text": f"{price} gold for all that?"})
        out.sort(key=lambda p: -p["value"])
        return out

    def trade_proposals(self) -> list:
        # sell only overflow (see trade_needs), and only at a real premium
        out = self.sale_offers(share=self.ANCHOR_SHARE, exploit=True, min_price=self.SELL_PREMIUM)
        sf = self.goal_shortfall()
        if sf is not None:
            miss, gold_short = sf
            out += self.loan_requests(gold_short)
            if gold_short <= 0:
                out += self.purchase_bids(miss, share=self.ANCHOR_SHARE, exploit=True)
            else:
                # stock up (below the storage cap) on what the next stage
                # needs: cheaper now from a player than in bulk on the market
                w = self.tw
                room = {r: min(q, max(0, w.caps.get(r, C.STORAGE_BASE) - w.res.get(r, 0)))
                        for r, q in miss.items()}
                if w.res.get("gold", 0) > 150:
                    out += self.purchase_bids(room, share=self.ANCHOR_SHARE, exploit=True)
        # peace with an army at our gates (contenders are excluded by the
        # peace bias): cheaper than a war, and it marches on someone else
        out += self.peace_offers(30, share=self.ANCHOR_SHARE, min_ratio=0.4)
        out.sort(key=lambda p: -p.get("value", 0))
        return out + self.bargain_offers()

    def loan_requests(self, short: int) -> list:
        """Borrow the gold our race is missing from a rich player (the
        leader too: see :meth:`refuse_partner`)."""
        w, v = self.tw, self.tv
        if self.goal not in ("wonder", "influence") or self.my_eta > self.LOAN_ETA:
            return []
        go = self.goal_order()
        if go is not None:
            # pre-finance the stage after next as well (lenders only check
            # whether the *next* stage becomes affordable)
            st = w.cities[go[0]].get("wonder_stage", 0)
            if st + 2 <= C.WONDER_VICTORY_STAGE:
                after = building_cost("wonder", st + 2)
                short = max(0, short) + int(0.5 * sum(q * (1.0 if r == "gold" else self.price(r))
                                                      for r, q in after.items()))
        if short < 60:
            return []
        if any(c.get("payer") == w.me for c in w.view.get("contracts", []) or []):
            return []                       # one loan at a time
        # instalments must stay small next to what we earn (else a default)
        max_inst = self.LOAN_INCOME_SHARE * self.potential_gold_rate()
        turns = min(C.DEAL_CONTRACT_MAX_TURNS, self.LOAN_TURNS, max(1, v.remaining - 2))
        lenders = sorted((-v.stock(q).get("gold", 0), q) for q in w.rivals if v.stock(q).get("gold", 0) >= 400)
        out = []
        for g, q in lenders[:2]:
            principal = int(min(self.LOAN_MAX, short + 50, -g // 2,
                                max_inst * turns / (1 + self.LOAN_INTEREST)))
            if principal < 100:
                continue
            inst = int(math.ceil(principal * (1 + self.LOAN_INTEREST) / turns))
            give = {"per_turn": {"gold": inst}, "turns": turns}
            get = {"gold": principal}
            deal = {"from": w.me, "to": q, "give": give, "get": get, "peace": None}
            if v.deal_gain(deal) < self.ACCEPT_MARGIN:
                continue
            out.append({"to": q, "give": give, "get": get, "kind": "loan", "value": v.deal_gain(deal),
                        "text": f"lend me {principal} gold for {inst} gold/turn x{turns}"})
        return out

    # ------------------------------------------------------------------
    # observation & ETA model
    # ------------------------------------------------------------------
    def observe(self) -> None:
        w = self.w
        hist = self.memory.setdefault("hist", {})
        for q, pl in w.players.items():
            h = hist.setdefault(q, [])
            if not h or h[-1][0] != w.turn:
                h.append((w.turn, int(pl.get("bank", 0) or 0), int(pl.get("legacy", 0) or 0),
                          pl.get("wonder_stage", 0)))
                if len(h) > self.HISTORY + 1:
                    del h[0]
            # turn at which each wonder stage was first seen (for the pace)
            ws = self.memory.setdefault("wstage", {}).setdefault(q, {})
            st = pl.get("wonder_stage", 0)
            if st == 0:
                ws.clear()
            elif st not in ws:
                ws[st] = w.turn
        # relic campaign progress (a stalled campaign gets a worse ETA)
        mine_g = w.players.get(w.me, {}).get("relics_guarded", 0)
        rp = self.memory.get("relic_progress")
        if rp is None or mine_g > rp[1] or self.memory.get("path") != "relics":
            self.memory["relic_progress"] = (w.turn, mine_g)
        self.etas = {q: self.eta(q) for q in w.alive}
        paths = self.PATHS + (("relics",) if self.USE_RELICS else ())
        mine = {k: v for k, v in self.etas.get(w.me, {}).items() if k in paths}
        if "relics" in mine:
            # relic streaks get contested: only prefer them when clearly faster
            mine["relics"] *= self.RELIC_ETA_FACTOR
        # hysteresis: stick to the current path unless another is clearly faster
        cur = self.memory.get("path")
        best = min(mine, key=lambda k: (mine[k], k)) if mine else "score"
        if cur in mine and mine[cur] < INF and mine[best] > 0.8 * mine[cur] - 2:
            best = cur
        self.memory["path"] = best
        self.my_path = best
        self.my_eta = mine.get(best, INF)
        leader, lpath, leta = None, None, INF
        for q in w.rivals:
            for k, v in sorted(self.etas.get(q, {}).items()):
                if v < leta:
                    leader, lpath, leta = q, k, v
        self.leader, self.leader_path, self.leader_eta = leader, lpath, leta
        left = w.max_turns - w.turn
        # the leader is a danger if it would win before us (or before the end)
        self.danger = leader is not None and leta < left and leta <= self.my_eta * 1.15 + 2

    def rate(self, q: str, k: int) -> float:
        h = self.memory.get("hist", {}).get(q, [])
        if len(h) < 2:
            return 0.0
        dt = h[-1][0] - h[0][0]
        return (h[-1][k] - h[0][k]) / dt if dt > 0 else 0.0

    def price(self, r: str) -> float:
        pool = self.w.pools.get(r)
        return pool[1] / pool[0] if pool and pool[0] > 0 else base_price(r)

    def production_value(self, q: str) -> float:
        """Gold-equivalent value of a player's public per-turn income."""
        inc = self.w.players[q].get("income", {}) or {}
        v = inc.get("gold", 0)
        for r in CAPPED:
            amt = inc.get(r, 0) - ((self.w.players[q].get("upkeep") or 0) if r == "food" else 0)
            v += max(0, amt) * self.price(r)
        return v

    def wonder_cost_value(self, stage: int, stock: dict | None = None) -> float:
        """Gold-equivalent value of the remaining wonder stages (minus stock)."""
        need = {}
        for k in range(stage + 1, C.WONDER_VICTORY_STAGE + 1):
            for r, v in building_cost("wonder", k).items():
                need[r] = need.get(r, 0) + v
        if stock:
            need = {r: max(0, v - stock.get(r, 0)) for r, v in need.items()}
        return sum(v * (1.0 if r == "gold" else self.price(r)) for r, v in need.items())

    def eta(self, q: str) -> dict:
        """Estimated turns until ``q`` meets each victory condition."""
        w = self.w
        pl = w.players[q]
        res = pl.get("resources") or {}
        inc = pl.get("income", {}) or {}
        th = w.thresholds
        me = q == w.me
        out = {}
        # economic and influence: reach the bank / legacy target, then hold it
        # for the streak (only while owning the original capital)
        home = w.capital_of(q) is not None
        streak_turns = th.get("streak_turns", C.VICTORY_STREAK_TURNS)
        need = th.get("bank", C.BANK_VICTORY) - int(pl.get("bank", 0) or 0)
        r = self.rate(q, 1)
        if me:
            r = max(r, min(bank_limit(w), self.potential_gold_rate()))
        clock = max(0, streak_turns - int(pl.get("economic_streak", 0) or 0))
        out["economic"] = ((0 if need <= 0 else (need / r if r > 0.5 else INF)) + clock) if home else INF
        need = th.get("legacy", C.LEGACY_VICTORY) - int(pl.get("legacy", 0) or 0)
        r = max(self.rate(q, 2), inc.get("influence", 0) * (1.0 if me else 0.8))
        clock = max(0, streak_turns - int(pl.get("influence_streak", 0) or 0))
        out["influence"] = ((0 if need <= 0 else (need / r if r > 0.2 else INF)) + clock) if home else INF
        # wonder
        stage = pl.get("wonder_stage", 0)
        left = C.WONDER_VICTORY_STAGE - stage
        pv = self.potential_gold_rate() if me else self.production_value(q)
        value_eta = max(left, self.wonder_cost_value(stage, res) / pv) if pv > 1 else INF
        if me:
            out["wonder"] = value_eta + (3 if stage == 0 else 0)
        elif stage > 0:
            out["wonder"] = max(value_eta, 0.8 * self.wonder_pace_eta(q, stage))
        else:
            out["wonder"] = INF
        # relics (only relics with units on them count for the streak)
        streak = pl.get("relic_streak", 0)
        need_r = th.get("relics_needed", 99)
        hold_turns = th.get("relic_turns", C.RELIC_VICTORY_TURNS)
        held = pl.get("relics_guarded", pl.get("relics_held", 0))
        if held >= need_r:
            out["relics"] = max(0, hold_turns - streak)
        elif me:
            out["relics"] = self.relic_acquire_turns(need_r - held) + hold_turns
            rp = self.memory.get("relic_progress")
            if rp is not None and self.memory.get("path") == "relics":
                # no progress for a while: the relics are contested
                out["relics"] += max(0, w.turn - rp[0] - 8) * 1.5
        elif held == need_r - 1:
            out["relics"] = hold_turns + 4
        else:
            out["relics"] = INF
        # conquest
        caps = pl.get("capitals_held", 0)
        need_c = th.get("conquest_capitals", 99)
        out["conquest"] = 10 * (need_c - caps) + 3 if caps >= max(2, need_c - 2) else INF
        if caps >= need_c:
            out["conquest"] = 0
        return out

    def wonder_pace_eta(self, q: str, stage: int) -> float:
        """Turns ``q`` needs for the remaining stages at the pace (value of
        the stages built per turn) observed since its first stage."""
        ws = self.memory.get("wstage", {}).get(q, {})
        if not ws:
            return INF
        first = min(ws.values())
        elapsed = max(6.0, self.w.turn - first + 8.0)      # +saving for stage 1
        done = self.wonder_value_between(0, stage)
        rate = done / elapsed
        left = self.wonder_value_between(stage, C.WONDER_VICTORY_STAGE)
        return left / rate if rate > 0 else INF

    def wonder_value_between(self, a: int, b: int) -> float:
        tot = 0.0
        for k in range(a + 1, b + 1):
            for r, v in building_cost("wonder", k).items():
                tot += v * (1.0 if r == "gold" else base_price(r))
        return tot

    def relic_acquire_turns(self, missing: int) -> float:
        """Rough turns needed to occupy ``missing`` more relics: walking
        distance from our land plus raising the guards, plus a fight for
        relics that others guard."""
        w = self.w
        if missing <= 0:
            return 0.0
        if not w.my_tiles:
            return INF
        dist = w.bfs(w.my_tiles, max_dist=20)
        costs = []
        for r in w.relics:
            o = w.owner[r]
            if (o == w.me and w.me in w.armies.get(r, {})) or r not in dist:
                continue
            c = dist[r] * 1.2
            enemy = w.enemy_units_at(r)
            if enemy:
                c += 6 + sum(enemy.values()) * 0.8
            if o is not None and o != w.me and w.at_peace(w.me, o):
                c += 10
            costs.append(c)
        if len(costs) < missing:
            return INF
        costs.sort()
        # guards must be raised and fed: a few turns per relic
        return costs[missing - 1] + 2.5 * missing

    def potential_gold_rate(self) -> float:
        """Gold per turn if all surplus production were sold at spot prices."""
        w = self.w
        g = self.raw.get("gold", 0)
        for r in CAPPED:
            amt = self.raw.get(r, 0) - (w.upkeep if r == "food" else 0)
            if amt > 0:
                pool = w.pools.get(r)
                price = pool[1] / pool[0] if pool else base_price(r)
                g += amt * price * (1 - w.fee) * 0.8
        return g

    # ------------------------------------------------------------------
    # diplomacy
    # ------------------------------------------------------------------
    def diplomacy(self) -> None:
        """Accept treaties from harmless players, propose them to distant or
        militarily strong ones; never to the leader or our target."""
        w, p = self.w, self.p
        cap = self.home()
        dist = w.bfs([cap]) if cap is not None else {}
        need_r = w.thresholds.get("relics_needed", 99)

        def far(q):
            c = w.capital_of(q) or next(iter(w.cities_of(q)), None)
            return c is None or dist.get(c, 99) >= 14

        def risky(q):
            pl = w.players.get(q, {})
            if q == self.memory.get("block_owner") or (self.danger and q == self.leader):
                return True
            if min(self.etas.get(q, {}).values(), default=INF) < 30:
                return True
            return pl.get("wonder_stage", 0) > 0 or pl.get("relics_guarded", 0) >= need_r - 1

        mine = max(1, w.players[w.me].get("military_power") or 0)

        def rich(q):
            # hoarders are prey: capturing their capital plunders half their gold
            return (w.players[q].get("resources") or {}).get("gold", 0) >= self.PREY_GOLD

        def useful(q):
            # a treaty protects us from strong armies; far-away players cost
            # little to leave alone unless they hoard; others (peaceful
            # builders) never attack us anyway, so we keep free to strike them
            strong = (w.players[q].get("military_power") or 0) > 1.2 * mine
            return strong or (far(q) and not rich(q))

        for pr in treaty_proposals_to_me(w):
            q = pr["from"]
            if not risky(q) and useful(q) and q != self.memory.get("prey_owner"):
                p.accept_treaty(q)
        if w.turn % 5 == 1:
            for q in w.rivals:
                if q in w.treaties or risky(q):
                    continue
                if (far(q) and not rich(q)) or (w.players[q].get("military_power") or 0) > 1.5 * mine:
                    p.propose(q, self.TREATY_TURNS)

    def home(self):
        w = self.w
        if w.capital in w.my_cities:
            return w.capital
        return w.my_cities[0] if w.my_cities else None

    # ------------------------------------------------------------------
    # defence: keep armies home while a hostile army is within reach
    # ------------------------------------------------------------------
    HOME_REACH = 0                # >0: also keep units home vs armies this many turns away

    def defend(self) -> None:
        super().defend()
        w = self.w
        self.home_threat = {}
        if not self.HOME_REACH:
            return
        for c in sorted(w.my_cities, key=lambda c: (0 if w.cities[c].get("capital") else 1, c)):
            threat = self.city_threat(c, reach=self.HOME_REACH)
            if not threat:
                continue
            self.home_threat[c] = threat
            self.lock_garrison(c, threat, 1.0, minimum=False)
            if not self.defended(c, threat, 1.0, add_units(self.p.recruited.get(c, {}),
                                                            self.locked.get(c, {}))):
                self.rally(c)

    def rally(self, c: int) -> None:
        """Bring free units within a few steps back to threatened city c."""
        w = self.w
        enter = w.can_enter_fn(w.me)
        dist = w.bfs([c], enter, max_dist=6)
        for i in sorted(w.my_armies):
            if i == c or i not in dist:
                continue
            if i in w.cities and w.cities[i]["owner"] == w.me:
                continue
            free = self.free_units(i)
            if not free:
                continue
            nxt = c if dist[i] == 1 else w.step_towards(i, dist, enter)
            if nxt is not None and self.safe_move(i, nxt, free):
                pass

    # ------------------------------------------------------------------
    # goal-specific spending
    # ------------------------------------------------------------------
    def committed(self) -> bool:
        w = self.w
        horizon = self.COMMIT_HORIZON + (self.INFLUENCE_EARLY_COMMIT if self.my_path == "influence" else 0)
        return self.my_eta <= horizon or self.my_eta >= w.max_turns - w.turn - 5 and w.turn > 40

    def plan_goal_spending(self) -> None:
        w = self.w
        self.goal = self.my_path if self.committed() else "grow"
        if self.goal in ("economic", "wonder", "influence") and self.my_eta < 20:
            # investments only count until we expect to win
            self.remaining = max(1, min(self.remaining, int(self.my_eta) + 8))
        # wonder: reserve the next stage when it is our path
        wc = wonder_city(w)
        if self.goal == "wonder" or (wc is not None and w.cities[wc].get("wonder_stage", 0) >= 3):
            home = wc or self.home()
            if home is not None:
                st = w.cities[home].get("wonder_stage", 0)
                if st < C.WONDER_VICTORY_STAGE:
                    self.add_reserve({r: min(v, w.caps.get(r, v)) if r in CAPPED else v
                                      for r, v in building_cost("wonder", st + 1).items()})
        # per-turn knob adjustments start from the class defaults
        self.SELL_FLOOR = type(self).SELL_FLOOR
        self.MIN_ROI = type(self).MIN_ROI
        if self.goal == "wonder":
            self.SELL_FLOOR = 0.5
        if self.goal == "economic" and self.my_eta < 25:
            self.MIN_ROI = 1 / 12.0
        if self.goal == "influence":
            # temples everywhere (buying their stone), influence kept
            self.TEMPLE_BIAS = 3.0
            self.INFLUENCE_WEIGHT = self.INFLUENCE_GOAL_WEIGHT
            self.BUY_FOR_IMPROVEMENTS = True
            self.wts = self.weights()
            if self.my_eta < 30:
                self.INFLUENCE_RESERVE = int(w.res.get("influence", 0) * 0.8)
        else:
            self.TEMPLE_BIAS = type(self).TEMPLE_BIAS
            self.INFLUENCE_WEIGHT = type(self).INFLUENCE_WEIGHT
            self.BUY_FOR_IMPROVEMENTS = type(self).BUY_FOR_IMPROVEMENTS
            self.INFLUENCE_RESERVE = 0

    def goal_spending(self) -> None:
        w, p = self.w, self.p
        wc = wonder_city(w)
        if self.goal == "wonder" or (wc is not None and w.cities[wc].get("wonder_stage", 0) >= 3):
            home = wc or self.home()
            if home is None:
                return
            st = w.cities[home].get("wonder_stage", 0)
            if st >= C.WONDER_VICTORY_STAGE:
                return
            cost = building_cost("wonder", st + 1)
            # the winning path outranks every other reservation
            if self.build_with_market(home, "wonder", max_mult=1.5):
                for r, v in cost.items():
                    self.reserved[r] = max(0, self.reserved.get(r, 0) - v)

    # ------------------------------------------------------------------
    # market
    # ------------------------------------------------------------------
    def sell(self) -> None:
        """Sell before overflow and sell surplus, splitting volume so the
        batch price stays within ~8% of spot (the rest is sold next turn)."""
        w, p = self.w, self.p
        mods = season_mods(w.turn)
        for r in CAPPED:
            if r == "food" and self.food_short:
                continue
            stock = p.budget.get(r, 0)
            inc = math.floor(self.raw.get(r, 0) * mods.get(r, 1.0)) - (w.upkeep if r == "food" else 0)
            cap = w.caps.get(r, C.STORAGE_BASE)
            overflow = stock + inc - cap
            surplus = stock - self.reserve(r) - self.keep(r)
            pool = w.pools.get(r)
            spot = pool[1] / pool[0] if pool else base_price(r)
            floor = self.SELL_FLOOR * base_price(r)
            sold = 0
            qty = surplus
            if qty >= 5 and spot >= floor:
                # limit price impact on voluntary sales; the rest waits a turn
                while qty > 5 and sell_price(w, r, qty) < spot * 0.92:
                    qty = int(qty * 0.75)
                sold = p.sell(r, qty, max(floor, spot * 0.85))
            if overflow - sold >= 3:
                p.sell(r, overflow - sold, 0.2 * base_price(r))

    def keep(self, r: str) -> int:
        return {"food": 30, "wood": 45, "stone": 30}.get(r, 0)

    # ------------------------------------------------------------------
    # expansion tweaks
    # ------------------------------------------------------------------
    def site_danger_weight(self) -> float:
        return 0.6

    def settle_step(self) -> None:
        # a new city pays off slowly: skip it when we are about to win
        if getattr(self, "goal", "grow") in ("economic", "wonder", "influence") and self.my_eta < 15:
            return
        super().settle_step()

    def plan_site(self) -> None:
        if self.my_path in ("economic", "wonder", "influence") and self.my_eta < 15:
            self.site, self.site_path = None, []
            return
        super().plan_site()

    # ------------------------------------------------------------------
    # relics
    # ------------------------------------------------------------------
    def relic_ops(self) -> None:
        """Note held relics that a hostile army threatens (guarded in
        :meth:`field_army`) and whether we campaign for the relic victory."""
        w = self.w
        self.relic_campaign_on = (self.USE_RELICS and self.my_path == "relics"
                                  and self.my_eta < w.max_turns - w.turn
                                  and self.my_eta <= self.COMMIT_HORIZON + 10)
        self.relic_guard_targets = []
        for r in w.relics:
            if w.owner[r] != w.me:
                continue
            th = threat_to(w, r, reach=2)
            if th:
                self.relic_guard_targets.append((r, th))

    # ------------------------------------------------------------------
    # blocking & field army
    # ------------------------------------------------------------------
    def block(self) -> None:
        """Pick a military objective against a rival that threatens to win
        before us and record it for :meth:`field_army`. The objective is kept
        across turns while that rival stays dangerous (no flip-flopping
        between rivals)."""
        w = self.w
        self.objective = None
        self.objective_ratio = 1.15
        rival, path = None, None
        prev = self.memory.get("block")
        if prev:
            q, pth = prev
            eta = self.etas.get(q, {}).get(pth, INF)
            if q in w.rivals and eta < INF and eta <= 1.6 * self.leader_eta + 3 \
                    and eta <= self.my_eta * 1.3 + 3:
                rival, path = q, pth
        if not self.USE_BLOCK:
            rival = None
        elif rival is None and self.danger and self.leader is not None:
            rival, path = self.leader, self.leader_path
        # a block that makes no progress for too long is dropped for a while
        cool = self.memory.setdefault("block_cooldown", {})
        if rival is not None and cool.get(f"{rival}:{path}", -1) >= w.turn:
            rival = None
        start = self.memory.get("block_start")
        if rival is not None:
            if not start or start[0] != rival or start[1] != path:
                self.memory["block_start"] = (rival, path, w.turn)
            elif w.turn - start[2] > self.BLOCK_PATIENCE:
                cool[f"{rival}:{path}"] = w.turn + self.BLOCK_COOLDOWN
                self.memory["block_start"] = None
                rival = None
        else:
            self.memory["block_start"] = None
        if rival is None:
            self.memory["block"] = None
            self.memory["block_owner"] = None
            return
        tgt = self.block_target(rival, path)
        if tgt is not None and self.BLOCK_FEASIBILITY and not self.block_feasible(tgt, rival, path):
            tgt = None
        if tgt is None:
            self.memory["block"] = None
            self.memory["block_owner"] = None
            return
        self.memory["block"] = (rival, path)
        self.memory["block_owner"] = rival
        self.objective = tgt
        self.reserve_for_force(tgt, plain=w.at_peace(w.me, rival))
        if w.at_peace(w.me, rival):
            # keep enough influence to break the treaty when ready
            self.INFLUENCE_RESERVE = max(self.INFLUENCE_RESERVE, C.TREATY_BREAK_COST + 2)

    def block_feasible(self, tgt: int, rival: str, path: str) -> bool:
        """Can we assemble and deliver the strike force before ``rival``
        wins? (Otherwise racing is the better use of our resources.)"""
        w = self.w
        eta = self.etas.get(rival, {}).get(path, INF)
        plain = w.at_peace(w.me, rival)
        cost = self.force_cost(self.missing_force(tgt, plain))
        prod = max(1.0, self.potential_gold_rate())
        stock = sum(w.res.get(r, 0) * (1.0 if r == "gold" else self.price(r)) for r in C.TRADABLE)
        t_build = max(0.0, cost - 0.5 * stock) / prod
        srcs = list(w.my_armies) or list(w.my_cities)
        dist = w.bfs([tgt])
        travel = min((dist.get(i, 99) for i in srcs), default=99) * self.TRAVEL_FACTOR
        # conquest ETAs are rough guesses: allow more slack
        slack = self.CONQUEST_SLACK if path == "conquest" else 1.1
        return t_build + travel + 1 <= eta * slack + 2

    def block_target(self, q: str, path: str):
        """The tile to hit to stop ``q`` winning by ``path``."""
        w = self.w
        if path == "relics":
            guarded = [r for r in w.relics if w.owner[r] == q and q in w.armies.get(r, {})]
            return self.cheapest_target(guarded, q) if guarded else None
        if path == "wonder":
            return self.closest_to_army([c for c in w.cities_of(q) if w.cities[c].get("wonder_stage", 0) > 0])
        cands = [w.capital_of(q)]
        if path == "influence":
            cands += [r for r in w.relics if w.owner[r] == q] + list(w.cities_of(q))
        elif path == "conquest":
            # any capital it holds will do: pick the cheapest one to take
            caps = [c for c in w.cities_of(q) if w.cities[c].get("capital")]
            return self.cheapest_target(caps, q)
        return self.closest_to_army(cands)

    def cheapest_target(self, cands: list, owner: str):
        """Candidate minimising (turns to build the missing force + travel)."""
        w = self.w
        cands = [c for c in cands if c is not None]
        if not cands:
            return None
        srcs = list(w.my_armies) or list(w.my_cities)
        if not srcs:
            return None
        dist = w.bfs(srcs)
        prod = max(1.0, self.potential_gold_rate())
        plain = w.at_peace(w.me, owner)
        best, best_t = None, INF
        for c in cands:
            d = dist.get(c)
            if d is None:
                continue
            t = d + self.force_cost(self.missing_force(c, plain)) / prod
            if t < best_t:
                best, best_t = c, t
        return best

    def closest_to_army(self, cands: list):
        """Candidate tile closest (plain walking distance) to our forces."""
        w = self.w
        cands = [c for c in cands if c is not None]
        if not cands:
            return None
        srcs = list(w.my_armies) or list(w.my_cities)
        if not srcs:
            return None
        dist = w.bfs(srcs)
        reach = [(dist.get(c, 999), c) for c in cands]
        reach.sort()
        return reach[0][1] if reach[0][0] < 999 else None

    def field_army(self) -> None:
        """Recruit and move the mobile army: guard relics, pursue the block
        objective (staging at the border and breaking the treaty first if the
        target is a treaty partner), or take a weakly defended rival
        city/relic nearby."""
        w, p = self.w, self.p
        if self.home() is None:
            return
        for r, th in getattr(self, "relic_guard_targets", []):
            self.guard_tile(r, th)
        campaign = getattr(self, "relic_campaign_on", False)
        if campaign:
            # the relic guards come first: a raid only uses what is left
            self.relic_campaign()
        if self.raid is not None:
            self.execute_raid()
            if self.USE_RELICS and not campaign:
                self.grab_relics()
            return
        tgt = self.objective
        if tgt is not None:
            owner = w.cities[tgt]["owner"] if tgt in w.cities else w.owner[tgt]
            if owner is not None and w.at_peace(w.me, owner):
                self.ensure_army_for(tgt, plain=True)
                self.stage_against(tgt, owner)
                return
            self.ensure_army_for(tgt)
            self.offense(tgt, min_ratio=self.objective_ratio)
            return
        tgt = self.opportunity()
        if tgt is not None:
            self.offense(tgt, min_ratio=1.3, max_dist=6)
            return
        prey = self.prey() if self.USE_PREY and not getattr(self, "relic_campaign_on", False) else None
        if prey is not None:
            self.ensure_army_for(prey)
            self.offense(prey, min_ratio=1.25)
            return
        if self.USE_RELICS:
            self.grab_relics()
        self.position_army()

    # ------------------------------------------------------------------
    # raids: capture a rival capital (plunder) or wonder city (denial)
    # ------------------------------------------------------------------
    USE_RAIDS = True
    RAID_PATIENCE = 30            # abandon a raid after this many turns
    RAID_COOLDOWN = 15            # ...and leave that rival alone this long
    RAID_RATIO = 1.15             # simulated power margin required to strike
    RAID_GATHER = 3               # gather this far from the target (out of sight)
    RAID_MIN_VALUE_RATIO = 2.0    # value / force cost needed for a raid (raids are costly)
    CONQUEST_VALUE = 500.0        # value of a capital toward conquest (x progress)

    def res_value(self, res: dict, frac: float = 1.0) -> float:
        return sum(res.get(r, 0) * frac * (1.0 if r == "gold" else self.price(r)) for r in C.TRADABLE)

    RAID_BUY_SHARE = 0.2          # share of its gold a target may spend on defenders

    def reinforcement(self, q: str, vs: dict) -> dict:
        """Units ``q`` could recruit in one turn (the best counter to ``vs``)
        with its stock plus what it could buy with part of its gold — what a
        target can add once it sees us coming."""
        res = (self.w.players.get(q) or {}).get("resources") or {}
        t = best_counter(vs or {"infantry": 1}, allowed=("infantry", "archer"))
        cost = C.UNITS[t]["cost"]
        n = min(int(res.get(r, 0) // v) for r, v in cost.items() if v)
        per_unit = sum(v * (1.0 if r == "gold" else self.price(r) * 1.3) for r, v in cost.items())
        n += int(res.get("gold", 0) * self.RAID_BUY_SHARE / max(1.0, per_unit))
        return {t: max(0, min(n, 2 * C.MAX_RECRUIT_PER_ORDER))}

    def raid_defenders(self, tgt: int, owner: str, vs: dict) -> dict:
        w = self.w
        defenders = {q: dict(u) for q, u in w.armies.get(tgt, {}).items() if q != w.me}
        for j in w.nb[tgt]:
            u = w.armies.get(j, {}).get(owner)
            if u:
                defenders[owner] = add_units(defenders.get(owner, {}), u)
        defenders[owner] = add_units(defenders.get(owner, {}), self.reinforcement(owner, vs))
        return defenders

    def raid_wins(self, tgt: int, owner: str, force: dict) -> bool:
        if not force:
            return False
        d = self.raid_defenders(tgt, owner, force)
        win, _, ratio = simulate_attack(self.w, self.w.me, force, tgt, defenders_override=d, assume_war=True)
        return win and ratio >= self.RAID_RATIO

    def raid_force(self, tgt: int, owner: str) -> dict:
        """Cheapest force (siege vs walls + one of a few compositions) that
        beats the defenders, their neighbours and one turn of reinforcements."""
        best, best_cost = None, INF
        siege = self.siege_needed(tgt)
        for mix in (("cavalry",), ("infantry",), ("archer",), ("infantry", "cavalry")):
            k = 1
            while k <= 60:
                force = {"siege": siege} if siege else {}
                for t in mix:
                    force[t] = force.get(t, 0) + k
                if self.raid_wins(tgt, owner, force):
                    c = self.force_cost(force)
                    if c < best_cost:
                        best, best_cost = force, c
                    break
                k += 1 if k < 8 else 3
        return best or {}

    def plan_raid(self) -> None:
        """Choose (or keep) a raid target and reserve what its force needs."""
        w = self.w
        if not w.my_cities:
            return
        mem = self.memory.get("raid")
        cool = self.memory.setdefault("raid_cooldown", {})
        if mem is not None:
            tgt, owner, start = mem["tgt"], mem["owner"], mem["start"]
            c = w.cities.get(tgt)
            gone = c is None or c["owner"] != owner or owner not in w.rivals
            if gone or w.turn - start > self.RAID_PATIENCE or not self.raid_still_worth(tgt, owner):
                if not gone:
                    cool[owner] = w.turn + self.RAID_COOLDOWN
                self.memory["raid"] = None
                mem = None
        if mem is None and w.turn % 2 == 0 and w.turn >= 10:
            mem = self.choose_raid(cool)
            if mem is not None:
                self.memory["raid"] = mem
        if mem is None:
            return
        self.raid = mem
        tgt, owner = mem["tgt"], mem["owner"]
        self.memory["block_owner"] = owner
        force = self.raid_force(tgt, owner)
        mem["force"] = force
        self.reserve_missing(tgt, force)
        if w.at_peace(w.me, owner):
            self.INFLUENCE_RESERVE = max(self.INFLUENCE_RESERVE, C.TREATY_BREAK_COST + 2)

    def raid_value(self, tgt: int, owner: str) -> float:
        w = self.w
        c = w.cities[tgt]
        pl = w.players.get(owner, {})
        v = 25 * 6.0 + 60 * c.get("wonder_stage", 0) ** 2
        if c.get("capital") and c.get("original_owner") == owner:
            v += self.res_value(pl.get("resources") or {}, C.PLUNDER_FRACTION)
        if c.get("capital"):
            # conquest progress: worth more the closer it brings us
            need = w.thresholds.get("conquest_capitals", 99)
            have = w.players.get(w.me, {}).get("capitals_held", 0)
            if have + 1 >= need:
                v += 4000.0
            else:
                v += self.CONQUEST_VALUE * (have + 1) / need
        # denial: the rival would win before us
        eta = min(self.etas.get(owner, {}).values(), default=INF)
        if eta < INF and eta <= self.my_eta + 5:
            v += 2500.0 * max(0.0, 1.0 - eta / 60.0) + 500
        return v

    def raid_still_worth(self, tgt: int, owner: str) -> bool:
        return self.raid_value(tgt, owner) > 0.6 * self.force_cost(self.raid_force(tgt, owner))

    def choose_raid(self, cool: dict):
        w = self.w
        srcs = list(w.my_cities)
        dist = w.bfs(srcs, max_dist=24)
        prod = max(1.0, self.potential_gold_rate())
        best, best_key = None, None
        for c, cc in w.cities.items():
            owner = cc["owner"]
            if owner == w.me or owner not in w.rivals or cool.get(owner, -1) >= w.turn:
                continue
            if not (cc.get("capital") or cc.get("wonder_stage", 0) > 0):
                continue
            if c not in dist:
                continue
            if w.at_peace(w.me, owner) and w.res.get("influence", 0) < C.TREATY_BREAK_COST:
                continue
            value = self.raid_value(c, owner)
            force = self.raid_force(c, owner)
            if not force:
                continue
            cost = self.force_cost(force) * (1 + dist[c] / 20.0)
            eta = min(self.etas.get(owner, {}).values(), default=INF)
            t_ready = cost / prod + dist[c] + 2
            if eta < INF and eta <= self.my_eta + 5 and t_ready > eta + 3:
                continue          # cannot get there in time
            if value < self.RAID_MIN_VALUE_RATIO * cost:
                continue
            key = (-(value - cost), c)
            if best_key is None or key < best_key:
                best, best_key = {"tgt": c, "owner": owner, "start": w.turn, "phase": "gather"}, key
        return best

    def reserve_missing(self, tgt: int, force: dict) -> None:
        have = self.army_near(tgt, plain=True, radius=12)
        cost: dict = {}
        for t, k in force.items():
            miss = max(0, k - have.get(t, 0))
            for r, v in C.UNITS[t]["cost"].items():
                cost[r] = cost.get(r, 0) + v * min(miss, 12)
        self.add_reserve({r: min(v, self.w.res.get(r, 0)) for r, v in cost.items()})

    def execute_raid(self) -> None:
        """Gather the force out of sight (RAID_GATHER tiles away), recruit
        what is missing, then strike in one go (breaking a treaty first if
        needed)."""
        w, p = self.w, self.p
        mem = self.raid
        tgt, owner, force = mem["tgt"], mem["owner"], mem.get("force") or {}
        plain = w.bfs([tgt])
        enter = w.can_enter_fn(w.me)
        # units available for the raid: free units within 12 steps
        near: dict = {}
        for i in w.my_armies:
            if plain.get(i, 99) <= 12:
                near = add_units(near, self.free_units(i))
        gather = self.gather_distance(force)
        close: dict = {}
        for i in w.my_armies:
            if plain.get(i, 99) <= gather + 1:
                close = add_units(close, self.free_units(i))
        at_peace = w.at_peace(w.me, owner)
        if mem["phase"] == "gather":
            # recruit the missing units in the city closest to the target
            city = min(w.my_cities, key=lambda c: (plain.get(c, 999), c))
            room = int(self.raw.get("food", 0) * 0.9 + p.budget.get("food", 0) / 12 - w.upkeep)
            have = add_units(near, p.recruited.get(city, {}))
            for t, k in sorted(force.items(), key=lambda kv: kv[0] != "siege"):
                miss = k - have.get(t, 0)
                if miss > 0 and room > 0:
                    got = p.recruit(city, t, min(miss, max(1, room // C.UNITS[t]["upkeep"])), {"food": 15})
                    room -= got * C.UNITS[t]["upkeep"]
            ready = self.raid_wins(tgt, owner, close)
            if ready:
                if at_peace:
                    if w.res.get("influence", 0) >= C.TREATY_BREAK_COST and self.memory.get("broke") != w.turn:
                        p.orders.append({"type": "break_treaty", "with": owner})
                        self.memory["broke"] = w.turn
                    # hold position this turn; strike next turn
                    for i in w.my_armies:
                        if plain.get(i, 99) <= gather + 1:
                            fr = self.free_units(i)
                            if fr:
                                self.locked[i] = add_units(self.locked.get(i, {}), fr)
                else:
                    mem["phase"] = "strike"
                    mem["strike_start"] = w.turn
            if mem["phase"] == "gather":
                self.gather_near(tgt, plain, enter, gather)
                return
        # strike: march in and attack when the adjacent force wins; a strike
        # that stalls (the target out-recruited us) goes back to gathering
        if w.turn - mem.get("strike_start", w.turn) > 6 and not self.raid_wins(tgt, owner, near):
            mem["phase"] = "gather"
        self.offense(tgt, min_ratio=1.0)

    def gather_distance(self, force: dict) -> int:
        """Gather out of the target's sight: cavalry is seen (and answered)
        from twice as far away as foot units."""
        if force and all(t == "cavalry" for t in force):
            return 2 * self.RAID_GATHER - 1
        return self.RAID_GATHER

    def gather_near(self, tgt: int, plain: dict, enter, gather: int | None = None) -> None:
        """Move free units toward ``tgt`` but stop ``gather`` steps away."""
        w = self.w
        gather = self.RAID_GATHER if gather is None else gather
        dist = w.bfs([tgt], enter)
        for i in sorted(w.my_armies):
            d = dist.get(i)
            free = self.free_units(i)
            if not free or d is None or d > 16:
                continue
            if d <= gather:
                if d < gather:
                    # too close (seen): step back
                    back = None
                    for j in w.nb[i]:
                        if dist.get(j, -1) == d + 1 and enter(j) and not w.hostile_units_on(w.me, j):
                            back = j
                            break
                    if back is not None and self.safe_move(i, back, free, allow_fight=False):
                        continue
                self.locked[i] = add_units(self.locked.get(i, {}), free)
                continue
            nxt = w.step_towards(i, dist, enter)
            if nxt is not None:
                self.safe_move(i, nxt, free, allow_fight=False)

    # ------------------------------------------------------------------
    # relic occupation
    # ------------------------------------------------------------------
    RELIC_GUARD = 4               # units kept on each relic during a campaign
    RELIC_GUARD_REACH = 5         # guards must hold vs armies this many turns away

    def guard_need(self, tile: int, have: dict, base: int) -> dict:
        """Units (taken from ``have``) to leave on ``tile`` so that it holds
        against hostile units within 3 turns (x1.2), at least ``base``."""
        w = self.w
        threat = threat_to(w, tile, reach=self.RELIC_GUARD_REACH)
        keep: dict = {}
        order = sorted(have, key=lambda t: (-C.UNITS[t]["strength"], t))
        left = dict(have)

        def add_one():
            for t in order:
                if left.get(t, 0) > 0:
                    left[t] -= 1
                    keep[t] = keep.get(t, 0) + 1
                    return True
            return False
        while sum(keep.values()) < base and add_one():
            pass
        for _ in range(40):
            ok = True
            for q, units in threat.items():
                scaled = {u: int(math.ceil(k * 1.2)) for u, k in units.items()}
                win, _, _ = simulate_attack(w, q, scaled, tile, defenders_override={w.me: keep})
                if win:
                    ok = False
                    break
            if ok or not add_one():
                break
        return keep

    def relic_targets(self, count: int) -> list:
        """The ``count`` relics we do not guard that are cheapest to occupy
        (distance from our units/cities, plus the enemies standing on them)."""
        w = self.w
        enter = w.can_enter_fn(w.me)
        srcs = list(w.my_armies) + list(w.my_cities)
        if not srcs or count <= 0:
            return []
        dist = w.bfs(srcs, enter, max_dist=24)
        cands = []
        for r in w.relics:
            if w.owner[r] == w.me and w.me in w.armies.get(r, {}):
                continue
            if r not in dist:
                continue
            enemy = w.enemy_units_at(r)
            cost = dist[r] + (4 + 0.1 * sum(C.UNITS[u]["strength"] * c for u, c in enemy.items()) if enemy else 0)
            cands.append((cost, self.salt[r], r))
        cands.sort()
        return [r for _, _, r in cands[:count]]

    def relic_campaign(self) -> None:
        """Occupy and hold the relics needed for the relic victory: keep a
        guard on each held relic, march detachments to the cheapest missing
        ones, fight for guarded ones, and recruit what the guards need."""
        w, p = self.w, self.p
        need = w.thresholds.get("relics_needed", 99)
        held = [r for r in w.relics if w.owner[r] == w.me and w.me in w.armies.get(r, {})]
        for r in held:
            keep = self.guard_need(r, self.free_units(r), self.RELIC_GUARD)
            if keep:
                self.locked[r] = add_units(self.locked.get(r, {}), keep)
        targets = self.relic_targets(need - len(held))
        enter = w.can_enter_fn(w.me)
        fought = False
        want_total = 0
        for t in targets:
            enemy = w.enemy_units_at(t)
            want_total += self.RELIC_GUARD + (sum(enemy.values()) if enemy else 0)
            if enemy:
                if not fought:
                    fought = True
                    self.offense(t, min_ratio=1.2, max_dist=10)
                continue
            self.send_detachment(t, self.RELIC_GUARD, enter)
        # raise the troops the campaign needs
        have = sum(sum(u.values()) for u in w.my_armies.values())
        short = want_total + len(held) * self.RELIC_GUARD - have
        if short > 0 and targets:
            self.recruit_near(targets[0], short)

    def send_detachment(self, tile: int, count: int, enter) -> None:
        """Move up to ``count`` free units (nearest stacks first) toward
        ``tile`` (onto it when adjacent)."""
        w = self.w
        dist = w.bfs([tile], enter, max_dist=16)
        sent = 0
        for d, i in sorted((dist[i], i) for i in w.my_armies if i in dist and i != tile):
            if sent >= count:
                break
            free = self.free_units(i)
            if not free:
                continue
            take: dict = {}
            for t in sorted(free, key=lambda t: (C.UNITS[t]["move"], -C.UNITS[t]["strength"], t)):
                k = min(free[t], count - sent - sum(take.values()))
                if k > 0:
                    take[t] = k
            if not take:
                continue
            nxt = tile if d == 1 else w.step_towards(i, dist, enter)
            if nxt is None:
                continue
            if self.safe_move(i, nxt, take, allow_fight=(nxt == tile)):
                sent += sum(take.values())

    def recruit_near(self, tile: int, count: int) -> None:
        """Recruit up to ``count`` infantry/archers in the city closest to
        ``tile``, within what our food income can feed."""
        w, p = self.w, self.p
        if not w.my_cities:
            return
        dist = w.bfs([tile], w.can_enter_fn(w.me))
        city = min(w.my_cities, key=lambda c: (dist.get(c, 999), c))
        room = int(self.raw.get("food", 0) * 0.8 + p.budget.get("food", 0) / 15 - w.upkeep)
        k = min(count, room, 6)
        if k <= 0:
            return
        threat: dict = {}
        for u in threat_to(w, tile, reach=4).values():
            threat = add_units(threat, u)
        t = best_counter(threat, allowed=("infantry", "archer")) if threat else "infantry"
        p.recruit(city, t, k, {r: v for r, v in self.reserved.items() if r == "food"})

    def grab_relics(self) -> None:
        """Outside a relic campaign: park a single unit on unguarded relics
        close to our free units (+influence and score each turn held) when no
        hostile army is near."""
        w = self.w
        enter = w.can_enter_fn(w.me)
        for r in sorted(w.relics, key=lambda r: self.salt[r]):
            if w.armies.get(r) or not enter(r):
                continue
            if threat_to(w, r, reach=2):
                continue
            dist = w.bfs([r], enter, max_dist=4)
            if not any(i in dist and self.free_units(i) for i in w.my_armies):
                continue
            self.send_detachment(r, 1, enter)
        # keep the units already sitting on our relics
        for r in w.relics:
            if w.owner[r] == w.me:
                here = self.free_units(r)
                if here:
                    self.locked[r] = add_units(self.locked.get(r, {}), here)

    def force_cost(self, force: dict) -> float:
        tot = 0.0
        for t, k in force.items():
            for r, v in C.UNITS[t]["cost"].items():
                tot += k * v * (1.0 if r == "gold" else self.price(r))
        return tot

    def prey(self):
        """A weakly defended rival capital worth conquering: plunder, 25
        score, conquest progress and one competitor less. Chosen when the
        estimated strike force costs less than what the capture is worth."""
        w = self.w
        if w.turn < 8 or not w.my_cities:
            return None
        memo = self.memory.get("prey")
        if memo is not None:
            c = w.cities.get(memo)
            if c is not None and c["owner"] != w.me and not w.at_peace(w.me, c["owner"]) \
                    and self.memory.get("prey_until", 0) >= w.turn:
                return memo
        if w.turn % 3:
            return None
        dist = w.bfs(w.my_tiles, w.can_enter_fn(w.me), max_dist=12)
        caps_needed = w.thresholds.get("conquest_capitals", 99) - w.players[w.me].get("capitals_held", 0)
        best, best_v = None, 0.0
        for c, cc in w.cities.items():
            o = cc["owner"]
            if o == w.me or w.at_peace(w.me, o) or c not in dist:
                continue
            if not cc.get("capital"):
                continue
            force = self.strike_force(c)
            if sum(force.values()) > 30:
                continue
            res = w.players[o].get("resources") or {}
            plunder = 0.0
            if cc.get("original_owner") == o:
                plunder = sum(res.get(r, 0) * C.PLUNDER_FRACTION * (1.0 if r == "gold" else self.price(r))
                              for r in C.TRADABLE)
            value = 25 * 8 + plunder + (400 if caps_needed <= 1 else 0)
            cost = self.force_cost(force) * (1 + dist[c] / 10.0)
            v = value / max(1.0, cost)
            if v > 1.0 and v > best_v:
                best, best_v = c, v
        if best is not None:
            self.memory["prey"] = best
            self.memory["prey_owner"] = w.cities[best]["owner"]
            self.memory["prey_until"] = w.turn + 25
        return best

    def stage_against(self, tgt: int, owner: str) -> None:
        """March toward a treaty partner's tile as far as the treaty allows;
        break the treaty once the gathered force can win the assault."""
        w, p = self.w, self.p
        plain = w.bfs([tgt])
        enter = w.can_enter_fn(w.me)
        near: dict = {}
        for i in sorted(w.my_armies):
            free = self.free_units(i)
            d = plain.get(i)
            if not free or d is None:
                continue
            if d <= 7:
                near = add_units(near, free)
            nxt = w.step_towards(i, plain, enter)
            moved = nxt is not None and plain.get(nxt, 99) < d and self.safe_move(i, nxt, free, allow_fight=False)
            if not moved and d <= 7:
                # staged at the border: hold position
                self.locked[i] = add_units(self.locked.get(i, {}), free)
        if not near or w.res.get("influence", 0) < C.TREATY_BREAK_COST:
            return
        win, _, ratio = simulate_attack(w, w.me, near, tgt, assume_war=True)
        if win and ratio >= 1.4 and self.memory.get("broke") != w.turn:
            p.orders.append({"type": "break_treaty", "with": owner})
            self.memory["broke"] = w.turn

    def guard_tile(self, tile: int, threat: dict) -> None:
        """Bring free units next to/onto a threatened relic tile."""
        w, p = self.w, self.p
        mine_here = p.available(tile)
        for q, units in threat.items():
            win, _, _ = simulate_attack(w, q, units, tile, defenders_override={w.me: mine_here})
            if not win:
                continue
            enter = w.can_enter_fn(w.me)
            dist = w.bfs([tile], enter, max_dist=4)
            for i in sorted(w.my_armies):
                if i == tile or dist.get(i) is None:
                    continue
                free = self.free_units(i)
                if not free:
                    continue
                nxt = tile if dist[i] == 1 else w.step_towards(i, dist, enter)
                if nxt is not None:
                    self.safe_move(i, nxt, free)
            # keep the ones already there
            self.locked[tile] = add_units(self.locked.get(tile, {}), mine_here)
            break

    def opportunity(self):
        """A rival relic tile or city we can take cheaply with the units we
        already have nearby (never a treaty partner)."""
        w = self.w
        if not w.my_armies:
            return None
        best, best_key = None, None
        enter = w.can_enter_fn(w.me)
        srcs = [i for i in w.my_armies if self.free_units(i)]
        if not srcs:
            return None
        dist = w.bfs(srcs, enter, max_dist=6)
        free_total = {}
        for i in srcs:
            free_total = add_units(free_total, self.free_units(i))
        need_r = w.thresholds.get("relics_needed", 99)
        for t in list(w.relics) + list(w.cities):
            o = w.owner[t]
            if o is None or o == w.me or w.at_peace(w.me, o):
                continue
            d = dist.get(t)
            if d is None:
                continue
            win, surv, ratio = simulate_attack(w, w.me, free_total, t)
            if not win or ratio < 1.4:
                continue
            val = 0.0
            if t in w.relic_set:
                val += 10 + 10 * (w.players[o].get("relics_held", 0) >= need_r - 1)
            if t in w.cities:
                c = w.cities[t]
                val += 25 + (40 if c.get("capital") else 0) + 20 * c.get("wonder_stage", 0)
            key = (-(val / (1 + d)), t)
            if best_key is None or key < best_key:
                best, best_key = t, key
        return best

    def strike_force(self, tgt: int) -> dict:
        """Smallest force (siege to cancel the walls + the best counter to
        the defenders) that wins the simulated assault on ``tgt`` with a 1.35
        power ratio, counting defenders that could step in from next door."""
        w = self.w
        owner = w.cities[tgt]["owner"] if tgt in w.cities else w.owner[tgt]
        defenders = {q: dict(u) for q, u in w.armies.get(tgt, {}).items() if q != w.me}
        if owner:
            for j in w.nb[tgt]:
                u = w.armies.get(j, {}).get(owner)
                if u:
                    defenders[owner] = add_units(defenders.get(owner, {}), u)
        enemy: dict = {}
        for u in defenders.values():
            enemy = add_units(enemy, u)
        force = {}
        siege = self.siege_needed(tgt)
        if siege:
            force["siege"] = siege
        t = best_counter(enemy or {"infantry": 1}, allowed=("infantry", "cavalry", "archer"))
        k = 1
        while k <= 80:
            force[t] = k
            win, _, ratio = simulate_attack(w, w.me, force, tgt, defenders_override=defenders, assume_war=True)
            if win and ratio >= 1.35:
                return force
            k += 1 if k < 10 else 3
        return force

    def army_near(self, tgt: int, plain: bool = False, radius: int = 10) -> dict:
        w = self.w
        dist = w.bfs([tgt]) if plain else w.bfs([tgt], w.can_enter_fn(w.me))
        have: dict = {}
        for i in w.my_armies:
            if dist.get(i, 999) <= radius:
                have = add_units(have, self.free_units(i))
        return have

    def missing_force(self, tgt: int, plain: bool = False) -> dict:
        need = self.strike_force(tgt)
        have = self.army_near(tgt, plain)
        return {t: k - have.get(t, 0) for t, k in need.items() if k > have.get(t, 0)}

    def reserve_for_force(self, tgt: int, plain: bool = False) -> None:
        """Keep the resources the missing strike force will need (so they are
        not sold or spent on buildings first)."""
        miss = self.missing_force(tgt, plain)
        cost: dict = {}
        for t, k in miss.items():
            for r, v in C.UNITS[t]["cost"].items():
                cost[r] = cost.get(r, 0) + v * min(k, 15)
        w = self.w
        self.add_reserve({r: min(v, w.res.get(r, 0)) for r, v in cost.items()})

    def pick_unit(self, enemy: dict, allowed=("infantry", "cavalry", "archer")) -> str:
        """Attacker type giving the most power for what we can afford now."""
        p = self.p
        best, best_v = allowed[0], -1.0
        for t in allowed:
            cost = C.UNITS[t]["cost"]
            n = min(int(p.budget.get(r, 0) // v) for r, v in cost.items() if v)
            mult = CB.counter_multiplier(t, enemy) if enemy else 1.0
            v = n * C.UNITS[t]["strength"] * mult + 0.01 * C.UNITS[t]["strength"] * mult
            if v > best_v:
                best, best_v = t, v
        return best

    def ensure_army_for(self, tgt: int, plain: bool = False) -> None:
        """Recruit, in the city closest to the objective and within food
        limits, until the gathered force wins the simulated assault with a
        1.35 ratio (siege first against walls). When a rival is about to
        win this outranks our own goal reservations."""
        w, p = self.w, self.p
        if not w.my_cities:
            return
        dist = w.bfs([tgt]) if plain else w.bfs([tgt], w.can_enter_fn(w.me))
        city = min(w.my_cities, key=lambda c: (dist.get(c, 999), c))
        have = add_units(self.army_near(tgt, plain), p.recruited.get(city, {}))
        room = int(self.raw.get("food", 0) * 0.9 + p.budget.get("food", 0) / 12 - w.upkeep)
        if room <= 0:
            return
        owner = w.cities[tgt]["owner"] if tgt in w.cities else w.owner[tgt]
        defenders = {q: dict(u) for q, u in w.armies.get(tgt, {}).items() if q != w.me}
        if owner:
            for j in w.nb[tgt]:
                u = w.armies.get(j, {}).get(owner)
                if u:
                    defenders[owner] = add_units(defenders.get(owner, {}), u)
        enemy: dict = {}
        for u in defenders.values():
            enemy = add_units(enemy, u)
        reserve = {"food": 20} if self.danger else {r: v for r, v in self.reserved.items() if r == "food"}
        siege_need = self.siege_needed(tgt) - have.get("siege", 0)
        if siege_need > 0:
            k = p.recruit(city, "siege", min(siege_need, room), reserve)
            have = add_units(have, {"siege": k})
            room -= 2 * k
        for _ in range(12):
            if room <= 0:
                break
            win, _, ratio = simulate_attack(w, w.me, have, tgt, defenders_override=defenders,
                                            assume_war=True) if have else (False, {}, 0.0)
            if win and ratio >= 1.35:
                break
            t = best_counter(enemy, allowed=("infantry", "cavalry", "archer")) if enemy else self.pick_unit(enemy)
            k = p.recruit(city, t, min(3, room), reserve)
            if k == 0:
                t = self.pick_unit(enemy)
                k = p.recruit(city, t, min(3, room), reserve)
            if k == 0:
                break
            have = add_units(have, {t: k})
            room -= k * C.UNITS[t]["upkeep"]

    def position_army(self) -> None:
        """Idle free units drift toward the relic nearest to home (or stay home)."""
        w, p = self.w, self.p
        mine = [r for r in w.relics if w.owner[r] == w.me]
        if not mine:
            return
        enter = w.can_enter_fn(w.me)
        dist = w.bfs(mine, enter)
        for i in sorted(w.my_armies):
            free = self.free_units(i)
            if free and dist.get(i) == 0:
                self.locked[i] = add_units(self.locked.get(i, {}), free)
                continue
            if not free or dist.get(i) is None:
                continue
            if dist[i] > 6:
                continue
            nxt = w.step_towards(i, dist, enter)
            if nxt is not None:
                self.safe_move(i, nxt, free, allow_fight=False)


class StrategistNoTradeBot(StrategistBot):
    """The strategist with trading disabled (never negotiates): the
    ablation baseline measuring what barter is worth to a skilled bot."""

    name = "strategist_notrade"
    TRADE = False

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        # same random tie-breaks as the trading strategist: only trading differs
        self.rng = random.Random(f"strategist:{seed}")


class StrategistLiteBot(StrategistBot):
    """A handicapped strategist for skill ladders: it always races for the
    economic victory (no path choice), never raids or campaigns for relics,
    and only signs short treaties."""

    name = "strategist_lite"
    PATHS = ("economic",)
    USE_RAIDS = False
    USE_RELICS = False
    TREATY_TURNS = 20
