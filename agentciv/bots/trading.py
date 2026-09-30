"""Negotiation machinery shared by the built-in bots (docs/DESIGN.md §13).

:class:`Trader` is a mixin for :class:`~agentciv.bots.common.SafeBot`
subclasses. ``negotiate(view)`` runs one negotiation round:

1. **Incoming deals** (addressed to us) are valued with the shared
   :class:`~agentciv.bots.common.DealValuer`. A deal is accepted when our
   gain clears :meth:`Trader.accept_threshold` (a margin plus a share of the
   deal size; haggling bots also insist on a share of the estimated joint
   surplus), countered (the gold term moved so that we get
   :meth:`Trader.counter_share` of the joint surplus) while the thread has
   counters left (conceding :attr:`Trader.CONCEDE` of the gap to our last
   offer in the thread), and rejected otherwise. At most one deal is
   accepted per round (the valuation is based on the stock *before* the
   round). Deals that would help a player close to winning
   (:attr:`Trader.WIN_GUARD`), come from a refused partner or fail the
   bot's :meth:`Trader.veto` (default: no food/wood for dangerous armies)
   are rejected outright.
2. **Our open proposals** that turned bad are withdrawn.
3. **New proposals** from :meth:`Trader.trade_proposals` (bot specific), at
   most :attr:`Trader.MAX_NEW_PER_TURN` per turn, one open deal per partner.
   Rejected/expired proposal kinds cool down per partner.

The kind of a proposal travels in its message (``"[sell] 60 stone for 120
gold"``) so rejections can be attributed and humans can read the log.

:meth:`Trader.priced` sets a proposal's gold term so that it splits the
estimated joint surplus in a chosen ratio; :meth:`Trader.sale_offers`,
:meth:`Trader.purchase_bids` and :meth:`Trader.peace_offers` build the
common proposals on top of it.
"""
from __future__ import annotations

import math
import re

from agentciv.engine import constants as C

from .common import CAPPED, DealValuer, World

KIND_RE = re.compile(r"^\[([a-z_]+)\]")


def deal_num(d: dict) -> int:
    try:
        return int(str(d.get("id", "d0"))[1:])
    except ValueError:
        return 0


def deal_kind(d: dict) -> str:
    m = KIND_RE.match(str(d.get("message") or ""))
    return m.group(1) if m else "offer"


def clean(bundle: dict) -> dict:
    """Drop zero/negative resource amounts and empty contract terms."""
    out = {}
    for r in C.TRADABLE:
        v = int(bundle.get(r, 0) or 0)
        if v > 0:
            out[r] = v
    tiles = bundle.get("tiles")
    if tiles:
        out["tiles"] = [list(t) for t in tiles]
    per = {r: int(v) for r, v in (bundle.get("per_turn") or {}).items() if int(v or 0) > 0}
    if per and int(bundle.get("turns", 0) or 0) > 0:
        out["per_turn"] = per
        out["turns"] = int(bundle["turns"])
    return out


def describe(b: dict) -> str:
    parts = [f"{b[r]} {r}" for r in C.TRADABLE if b.get(r)]
    if b.get("tiles"):
        parts.append(f"{len(b['tiles'])} tile(s)")
    if b.get("per_turn"):
        per = ", ".join(f"{v} {r}" for r, v in b["per_turn"].items())
        parts.append(f"{per}/turn x{b.get('turns')}")
    return " + ".join(parts) or "nothing"


class Trader:
    """Mixin: §13 negotiation for SafeBot subclasses (see module doc)."""

    TRADE = True
    ACCEPT_MARGIN = 2.0          # gold-equivalent gain a deal must bring at least...
    ACCEPT_FRACTION = 0.015      # ...plus this fraction of the deal's size
    COUNTER_LIMIT = 2            # own counters per negotiation thread
    COUNTER_SHARE = 0.5          # share of the joint surplus asked for in a counter...
    CONCEDE = 0.5                # ...but at most our last offer minus this share of the gap
    ACCEPT_AFTER_HAGGLE = True   # out of counters: accept anything above the margin
    PARTNER_MIN = 3.0            # a proposal must leave the partner (estimated) this much
    WIN_GUARD = 0.7              # never help a player at/over this victory progress
    MAX_NEW_PER_TURN = 2
    MAX_OPEN = 4
    EXPIRES_IN = 1
    DISCOUNT = 0.97              # per-turn discount of contract instalments
    PEACE_SCALE = 1.0            # how much we like peace (threat-based value ×)
    COOLDOWN = 4                 # turns before re-proposing a rejected kind to a partner
    MIN_LOT = 20                 # smallest resource lot worth a proposal

    # ------------------------------------------------------------------
    # hooks (override)
    # ------------------------------------------------------------------
    def trade_setup(self, w: World) -> None:
        """Per-round preparation (the view is fresh every round)."""

    def trade_needs(self) -> tuple:
        """(needs {capped resource: stock wanted on hand}, gold need)."""
        return None, 0

    def trade_horizon(self):
        """Turns after which our own contract payments stop mattering."""
        return None

    def peace_bias(self) -> dict:
        return {}

    def refuse_partner(self, q: str, deal: dict | None = None) -> bool:
        """Refuse to deal with ``q`` (``deal``: the deal in question, if any)."""
        return False

    FOG_REJECT = "no deal"       # the one rejection text used in fog games

    WAR_SUPPLY_VETO = True       # no food/wood for a hostile army that could reach us...
    WARMONGER = 1.5              # ...nor for an army this many times the average size

    def veto(self, q: str, bundle_to_q: dict, deal: dict) -> str | None:
        """Reason to refuse a deal that hands ``bundle_to_q`` to ``q`` (None
        = no objection). Default: don't feed an army that threatens us."""
        if self.WAR_SUPPLY_VETO and self.arms_threat(q, bundle_to_q):
            return "no war supplies for you"
        return None

    def arms_threat(self, q: str, bundle_to_q: dict) -> bool:
        """Would ``bundle_to_q`` (food/wood: units) feed a dangerous army — a
        hostile one that can reach one of our cities within a few turns, or
        (even under a treaty: it attacks someone) a conqueror holding 2+
        capitals or an army ``WARMONGER`` times the average?"""
        w = self.tw
        if not (bundle_to_q.get("food") or bundle_to_q.get("wood")):
            return False
        if w.hostile(w.me, q) and self.tv.threat(q, w.me) > 0:
            return True
        pl = w.players.get(q) or {}
        if int(pl.get("capitals_held", 0) or 0) >= 2:
            return True
        mps = [float((w.players.get(x) or {}).get("military_power", 0) or 0) for x in w.alive]
        avg = sum(mps) / max(1, len(mps))
        return float(pl.get("military_power", 0) or 0) >= self.WARMONGER * max(1.0, avg)

    def trade_proposals(self) -> list:
        """Candidate proposals: dicts ``{to, give, get, peace?, kind, text?}``."""
        return []

    def accept_threshold(self, d: dict, gain: float, their: float) -> float:
        return self.ACCEPT_MARGIN + self.ACCEPT_FRACTION * self.tv.size(d)

    def counter_share(self, d: dict, n: int) -> float:
        return self.COUNTER_SHARE

    # ------------------------------------------------------------------
    # driver
    # ------------------------------------------------------------------
    def decide_deals(self, view: dict) -> list:
        w = World(view)
        if w.me is None or not w.n_tiles or w.me not in w.players:
            return []
        mem = self.memory.setdefault("trade", {})
        if mem.get("turn") != w.turn:
            mem["turn"] = w.turn
            mem["round"] = 0
            mem["new"] = 0
        else:
            mem["round"] = mem.get("round", 0) + 1
        self._learn(view, mem)
        self.tw = w
        self.tview = view
        self.tmem = mem
        self.trade_setup(w)
        needs, gold_need = self.trade_needs()
        self.tv = DealValuer(w, needs=needs, gold_need=gold_need, discount=self.DISCOUNT,
                             horizon=self.trade_horizon(), peace_bias=self.peace_bias(),
                             peace_scale=self.PEACE_SCALE)
        deals = view.get("deals") or {}
        open_ = [d for d in (deals.get("open") or []) if isinstance(d, dict) and d.get("status", "open") == "open"]
        incoming = sorted((d for d in open_ if d.get("to") == w.me), key=deal_num)
        mine = [d for d in open_ if d.get("from") == w.me]
        actions: list = []
        best = None
        # fog games: every rejection carries one fixed text, so a rejection
        # never describes the bot's hidden stock or what it sees (a deal it
        # cannot settle and one it declines read the same)
        fog = bool((view.get("fog") or {}).get("active"))
        for d in incoming:
            try:
                verdict, payload = self.respond(d)
            except Exception:   # malformed deal in the view: refuse it
                verdict, payload = ("reject", "cannot evaluate this deal") if d.get("id") else (None, None)
            if verdict == "accept":
                if best is None or payload > best[0]:
                    best = (payload, d)
            elif verdict == "counter":
                actions.append(payload)
            elif verdict == "reject":
                actions.append({"type": "reject", "deal": d["id"], "message": self.FOG_REJECT if fog else payload})
        if best is not None:
            actions.insert(0, {"type": "accept", "deal": best[1]["id"]})
            mem["accepted"] = mem.get("accepted", 0) + 1
        busy = {d["from"] for d in incoming} | {d["to"] for d in mine}
        for d in mine:
            if self._withdraw(d):
                actions.append({"type": "withdraw", "deal": d["id"]})
        if best is None and mem["new"] < self.MAX_NEW_PER_TURN and len(mine) < self.MAX_OPEN:
            for prop in self.trade_proposals() or []:
                if mem["new"] >= self.MAX_NEW_PER_TURN:
                    break
                to = prop.get("to")
                if to in busy or to not in w.alive or to == w.me or self.cooling(to, prop.get("kind", "offer")):
                    continue
                act = self._proposal_action(prop)
                if act is None:
                    continue
                actions.append(act)
                busy.add(to)
                mem["new"] += 1
        return actions

    def _learn(self, view: dict, mem: dict) -> None:
        """Cool down proposal kinds a partner rejected or let expire."""
        seen = mem.setdefault("seen", [])
        cool = mem.setdefault("cool", {})
        me = (view.get("you") or {}).get("id")
        turn = int(view.get("turn", 0))
        for d in ((view.get("deals") or {}).get("recent") or []):
            if d.get("from") != me or d.get("id") in seen:
                continue
            seen.append(d.get("id"))
            if d.get("status") in ("rejected", "expired", "failed"):
                key = f"{d.get('to')}:{deal_kind(d)}"
                prev = cool.get(key, [0, 0])
                n = prev[1] + 1
                cool[key] = [turn + self.COOLDOWN * min(4, n), n]
            elif d.get("status") == "accepted":
                key = f"{d.get('to')}:{deal_kind(d)}"
                if key in cool:
                    cool[key][1] = 0
        if len(seen) > 200:
            del seen[:100]
        if mem.get("round", 0):
            return
        threads = mem.setdefault("threads", {})
        if len(threads) > 100:
            for k in sorted(threads, key=lambda t: threads[t][1])[:50]:
                del threads[k]

    def cooling(self, q: str, kind: str) -> bool:
        c = self.tmem.get("cool", {}).get(f"{q}:{kind}")
        return c is not None and c[0] > self.tw.turn

    # ------------------------------------------------------------------
    # responding
    # ------------------------------------------------------------------
    def counters_in(self, d: dict) -> int:
        t = self.tmem.setdefault("threads", {}).get(str(d.get("thread") or d.get("id")))
        return t[0] if t else 0

    def respond(self, d: dict):
        """('accept', gain) | ('counter', action) | ('reject', message) | (None, None)."""
        w, v = self.tw, self.tv
        other = d.get("from")
        if other not in w.alive:
            return None, None
        if d.get("problem"):
            return "reject", f"cannot settle: {d['problem']}"[:C.DEAL_MESSAGE_MAX_LENGTH]
        if self.refuse_partner(other, d):
            return "reject", "no deals with you right now"
        if v.helps_winner(other, d.get("get") or {}, self.WIN_GUARD, d.get("give") or {}):
            return "reject", "you are too close to winning"
        if not self.can_deliver(d.get("get") or {}):
            return "reject", "I cannot deliver that"
        why = self.veto(other, d.get("get") or {}, d)
        if why:
            return "reject", why
        gain = v.deal_gain(d)
        their = v.deal_gain(d, other)
        thr = self.accept_threshold(d, gain, their)
        if gain >= thr:
            return "accept", gain
        base_thr = self.ACCEPT_MARGIN + self.ACCEPT_FRACTION * v.size(d)
        got = self.make_counter(d, gain, their)
        if got is not None:
            counter, target = got
            if gain >= base_thr and target - gain <= max(2.0, 0.01 * v.size(d)):
                return "accept", gain          # close enough: don't haggle over crumbs
            return "counter", counter
        # no more counters: take a deal that is still worth it at the end
        if gain >= base_thr and self.counters_in(d) >= self.COUNTER_LIMIT and self.ACCEPT_AFTER_HAGGLE:
            return "accept", gain
        self.tmem.setdefault("cool", {})[f"{other}:{deal_kind(d)}"] = [w.turn + self.COOLDOWN, 1]
        return "reject", "not worth it for me"

    def my_last_offer(self, d: dict):
        """Our latest (countered) offer in ``d``'s negotiation thread."""
        thread = d.get("thread")
        if not thread or thread == d.get("id"):
            return None
        best = None
        for x in ((self.tview.get("deals") or {}).get("recent") or []):
            if x.get("thread") == thread and x.get("from") == self.tw.me and x.get("status") == "countered":
                if best is None or deal_num(x) > deal_num(best):
                    best = x
        return best

    def make_counter(self, d: dict, gain: float, their: float):
        """Counter by moving the gold term so that we get our share of the
        (estimated) joint surplus, conceding ``CONCEDE`` of the gap between
        our last offer in the thread and theirs. Returns ``(action, target
        gain)`` or None if there is no room for a deal."""
        n = self.counters_in(d)
        if n >= self.COUNTER_LIMIT:
            return None
        v, w = self.tv, self.tw
        other = d["from"]
        surplus = gain + their
        base_thr = self.ACCEPT_MARGIN + self.ACCEPT_FRACTION * v.size(d)
        if surplus < base_thr + self.PARTNER_MIN:
            return None
        share = self.counter_share(d, n)
        target = share * surplus
        prev = self.my_last_offer(d)
        if prev is not None:
            last = v.deal_gain(prev)
            if last > gain:
                target = min(target, last - self.CONCEDE * (last - gain))
        target = max(base_thr + 1.0, target)
        # our view: we give what they want (d.get) and get what they offer (d.give)
        give, get = dict(d.get("get") or {}), dict(d.get("give") or {})
        tmpl = {"from": w.me, "to": other, "give": give, "get": get, "peace": d.get("peace")}
        deal = self.shift_gold(tmpl, target - gain)
        if deal is None:
            return None
        g2 = v.deal_gain(deal)
        if g2 < target - 1:
            deal = self.shift_gold(deal, target - g2)
            if deal is None or v.deal_gain(deal) < base_thr:
                return None
        if v.deal_gain(deal, other) < 0 or deal == tmpl:
            return None
        if not self.can_deliver(deal["give"]) or not self.partner_can_deliver(other, deal["get"]):
            return None
        if not (deal["give"] or deal["get"] or deal.get("peace")):
            return None
        thread = str(d.get("thread") or d["id"])
        self.tmem.setdefault("threads", {})[thread] = [n + 1, w.turn]
        kind = deal_kind(d)
        act = {"type": "counter", "deal": d["id"], "give": deal["give"], "get": deal["get"],
               "message": f"[{kind}] {describe(deal['give'])} for {describe(deal['get'])}"[:C.DEAL_MESSAGE_MAX_LENGTH]}
        if deal.get("peace"):
            act["peace"] = deal["peace"]
        return act, v.deal_gain(deal)

    def shift_gold(self, deal: dict, delta: float):
        """Copy of ``deal`` (from our side) improved for us by ~``delta``
        gold (negative: conceded): less gold given first, then more asked."""
        give, get = dict(deal.get("give") or {}), dict(deal.get("get") or {})
        delta = int(math.ceil(delta)) if delta > 0 else -int(math.ceil(-delta))
        if delta > 0:
            take = min(give.get("gold", 0), delta)
            give["gold"] = give.get("gold", 0) - take
            get["gold"] = get.get("gold", 0) + (delta - take)
        elif delta < 0:
            take = min(get.get("gold", 0), -delta)
            get["gold"] = get.get("gold", 0) - take
            give["gold"] = give.get("gold", 0) + (-delta - take)
        out = dict(deal)
        out["give"], out["get"] = clean(give), clean(get)
        return out

    # ------------------------------------------------------------------
    # our proposals
    # ------------------------------------------------------------------
    def can_deliver(self, bundle: dict) -> bool:
        return self.partner_can_deliver(self.tw.me, bundle)

    def partner_can_deliver(self, q: str, bundle: dict) -> bool:
        w = self.tw
        stock = self.tv.stock(q)
        for r in C.TRADABLE:
            if int(bundle.get(r, 0) or 0) > stock.get(r, 0):
                return False
        for t in bundle.get("tiles", ()) or ():
            try:
                x, y = int(t[0]), int(t[1])
            except (TypeError, ValueError, IndexError):
                return False
            if not (0 <= x < w.w and 0 <= y < w.h):
                return False
            i = w.idx(x, y)
            if w.owner[i] != q or i in w.cities or i in w.relic_set:
                return False
        return True

    def _withdraw(self, d: dict) -> bool:
        v = self.tv
        if d.get("problem") and not self.can_deliver(d.get("give") or {}):
            return True
        if self.refuse_partner(d["to"], d) or v.helps_winner(d["to"], d.get("give") or {}, self.WIN_GUARD,
                                                               d.get("get") or {}):
            return True
        if self.veto(d["to"], d.get("give") or {}, d):
            return True
        return v.deal_gain(d) < 0

    def _proposal_action(self, prop: dict):
        w, v = self.tw, self.tv
        to = prop["to"]
        give, get = clean(prop.get("give") or {}), clean(prop.get("get") or {})
        peace = prop.get("peace")
        if not (give or get or peace):
            return None
        deal = {"from": w.me, "to": to, "give": give, "get": get, "peace": peace}
        if not self.can_deliver(give) or not self.partner_can_deliver(to, get):
            return None
        if self.refuse_partner(to, dict(deal, message=f"[{prop.get('kind', 'offer')}]")) \
                or v.helps_winner(to, give, self.WIN_GUARD, get):
            return None
        if self.veto(to, give, dict(deal, message=f"[{prop.get('kind', 'offer')}]")):
            return None
        if not prop.get("free") and v.deal_gain(deal) < self.ACCEPT_MARGIN:
            return None
        kind = prop.get("kind", "offer")
        text = prop.get("text") or f"{describe(give)} for {describe(get)}"
        act = {"type": "propose", "to": to, "give": give, "get": get,
               "message": f"[{kind}] {text}"[:C.DEAL_MESSAGE_MAX_LENGTH], "expires_in": self.EXPIRES_IN}
        if peace:
            act["peace"] = int(peace)
        return act

    def priced(self, to: str, give: dict, get: dict, share: float, peace=None,
               partner_min: float | None = None):
        """Set the gold term of a proposal so that we get ``share`` of the
        estimated joint surplus while the partner keeps at least
        ``partner_min`` (their acceptance margin). Returns the deal (our
        side) or None when no split works."""
        v, w = self.tv, self.tw
        deal = {"from": w.me, "to": to, "give": clean(give), "get": clean(get), "peace": peace}
        mine, their = v.deal_gain(deal), v.deal_gain(deal, to)
        surplus = mine + their
        pm = self.PARTNER_MIN if partner_min is None else partner_min
        if surplus < self.ACCEPT_MARGIN + pm:
            return None
        target = share * surplus
        target = min(target, surplus - pm)
        deal = self.shift_gold(deal, target - mine)
        for _ in range(2):   # the marginal value of gold is not exactly 1: correct
            mine = v.deal_gain(deal)
            their = v.deal_gain(deal, to)
            if their < pm:
                deal = self.shift_gold(deal, -(pm - their) - 1)
            elif abs(mine - target) > 2:
                deal = self.shift_gold(deal, target - mine)
            else:
                break
        if v.deal_gain(deal) < self.ACCEPT_MARGIN or v.deal_gain(deal, to) < 0:
            return None
        return deal

    def partner_threshold(self, deal: dict) -> float:
        """Estimated acceptance margin of a (built-in style) partner."""
        return Trader.ACCEPT_MARGIN + Trader.ACCEPT_FRACTION * self.tv.size(deal) + 2.0

    # -- common proposal builders -----------------------------------------
    def surplus(self, r: str) -> int:
        v = self.tv
        return int(v.stock(self.tw.me).get(r, 0) - (v.needs(self.tw.me) or {}).get(r, 0))

    def shortfall(self, q: str, r: str) -> int:
        v = self.tv
        return int(v.needs(q).get(r, 0) - v.stock(q).get(r, 0))

    def partners(self) -> list:
        w = self.tw
        return [q for q in w.rivals if not self.refuse_partner(q)]

    def sale_offers(self, resources=CAPPED, share: float = 0.5, max_lot: int = 400,
                    min_surplus: int | None = None, exploit: bool = False,
                    min_price: float = 0.0) -> list:
        """Sell surplus ``resources`` to the players who need them most (for
        at least ``min_price`` × what the market would pay us)."""
        w, v = self.tw, self.tv
        out = []
        lot_min = self.MIN_LOT if min_surplus is None else min_surplus
        for r in resources:
            have = self.surplus(r)
            if have < lot_min:
                continue
            buyers = []
            for q in self.partners():
                short = self.shortfall(q, r)
                if short >= self.MIN_LOT and v.stock(q).get("gold", 0) >= 20:
                    buyers.append((-short, q))
            for _, q in sorted(buyers):
                qty = min(have, self.shortfall(q, r), max_lot)
                qty = min(qty, int(v.stock(q).get("gold", 0) / max(0.1, v.prices[r])))
                if qty < self.MIN_LOT:
                    continue
                tmpl_give = {r: qty}
                probe = {"from": w.me, "to": q, "give": tmpl_give, "get": {}, "peace": None}
                pm = self.partner_threshold(probe) if exploit else None
                deal = self.priced(q, tmpl_give, {}, share, partner_min=pm)
                floor = min_price * qty * v.sell_unit(w.me, r, qty)
                if deal is not None and deal["get"].get("gold", 0) >= max(1, floor):
                    out.append({"to": q, "give": deal["give"], "get": deal["get"], "kind": "sell",
                                "text": f"{qty} {r} for {deal['get'].get('gold', 0)} gold",
                                "value": v.deal_gain(deal)})
                break      # one buyer per resource and turn
        out.sort(key=lambda p: -p["value"])
        return out

    def peace_offers(self, turns: int = 30, share: float = 0.5, min_ratio: float = 0.5) -> list:
        """Gold for ``turns`` turns of peace with the rival whose army
        threatens us most (threat ≥ ``min_ratio`` × our defence)."""
        w, v = self.tw, self.tv
        out = []
        mine = v.defense(w.me)
        for q in self.partners():
            if q in w.treaties or getattr(self, "relic_runner", lambda _q: False)(q):
                continue
            t = v.threat(q, w.me)
            if t < min_ratio * mine or t < 30:
                continue
            deal = self.priced(q, {}, {}, share, peace=turns)
            if deal is None or not deal["give"].get("gold"):
                continue
            out.append({"to": q, "give": deal["give"], "get": deal["get"], "peace": turns,
                        "kind": "peace", "value": v.deal_gain(deal),
                        "text": f"{deal['give'].get('gold')} gold for {turns} turns of peace"})
        out.sort(key=lambda p: -p["value"])
        return out[:1]

    def purchase_bids(self, wanted: dict, share: float = 0.5, exploit: bool = False,
                      max_sellers: int = 2) -> list:
        """Buy ``wanted`` {resource: qty} from players with spare stock."""
        w, v = self.tw, self.tv
        out = []
        for r, want in wanted.items():
            if want < self.MIN_LOT:
                continue
            sellers = []
            for q in self.partners():
                spare = int(v.stock(q).get(r, 0) - v.needs(q).get(r, 0))
                if spare >= self.MIN_LOT:
                    sellers.append((-spare, q))
            left = want
            for spare, q in sorted(sellers)[:max_sellers]:
                qty = min(left, -spare)
                if qty < self.MIN_LOT:
                    continue
                probe = {"from": w.me, "to": q, "give": {}, "get": {r: qty}, "peace": None}
                pm = self.partner_threshold(probe) if exploit else None
                gold = int(qty * v.prices[r])
                deal = self.priced(q, {"gold": gold}, {r: qty}, share, partner_min=pm)
                if deal is None or deal["give"].get("gold", 0) > v.stock(w.me).get("gold", 0):
                    continue
                out.append({"to": q, "give": deal["give"], "get": deal["get"], "kind": "buy",
                            "text": f"{deal['give'].get('gold', 0)} gold for {qty} {r}",
                            "value": v.deal_gain(deal)})
                left -= qty
        out.sort(key=lambda p: -p["value"])
        return out
