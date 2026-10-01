"""Spoiler: goes after whoever is closest to winning.

The counterplay test. The other bots raid streak holders only when the
plunder pays for the strike force (the strategist) or not at all, and a
field has at most one aggressor, so a banker that holds its cities wins on
a fixed schedule. The spoiler plays the strategist's economy and opening,
then makes stopping the leader its main job.

Strategy
--------
* **Opening.** The strategist's economy, defence and own victory race;
  no raids before ``SPOIL_START``.
* **Whom.** Every turn it ranks the rivals by how close they are to any
  victory: the strategist's ETA model (bank/legacy growth plus the streak
  turns missing, wonder pace, capitals) and the public
  ``victory_progress``. A rival is a target when it is on an economic or
  influence streak, has ``SPOIL_PROGRESS`` in any condition, a wonder at
  stage ``SPOIL_WONDER_STAGE`` or more, or an ETA within ``SPOIL_ETA``.
* **Where.** Against a streak (or a bank/legacy race) any city will do,
  since the loss of any city resets both streaks (rules §11); against a
  wonder the wonder city; against conquest a capital it holds. It picks
  the city whose strike force is cheapest to raise and bring (the weakest
  city), among the two most dangerous rivals, and skips targets it cannot
  reach before the rival is expected to win.
* **How.** The strategist's raid machinery: the force must beat the
  garrison, walls (siege), the defenders next door and one turn of
  emergency recruiting with a 1.2 margin (``simulate_attack``); it gathers
  out of sight and only strikes when the gathered force wins. In fog games
  it first buys a military spy report on the target (``scout``), so it
  does not count on armies it cannot see.
* **Treaties.** It breaks a treaty with a streak holder before the strike
  (free, rules §9) and signs none with a rival it may have to hit.
"""
from __future__ import annotations

from agentciv.engine import constants as C

from .common import add_units
from .strategist import INF, StrategistBot


class SpoilerBot(StrategistBot):
    name = "spoiler"

    SPOIL_START = 20              # no raids before this turn (a normal opening)
    SPOIL_PROGRESS = 0.45         # victory progress that makes a rival a target
    SPOIL_ETA = 30                # ... or an estimated win this close
    SPOIL_WONDER_STAGE = 3        # ... or a wonder this far along
    SPOIL_RIVALS = 2              # look at this many of the most dangerous rivals
    SPOIL_VALUE = 3000.0          # value of a capture that resets a near-winner
    SPOIL_SLACK = 6               # turns of lateness still worth a strike
    RAID_RATIO = 1.2
    RAID_PATIENCE = 25
    RAID_COOLDOWN = 8
    SCOUT_EVERY = 4               # fog: a fresh military report on the target this often

    # ---- whom -----------------------------------------------------------
    def danger_of(self, q: str) -> tuple:
        """(best ETA, best public progress, path) of rival ``q``."""
        w = self.w
        etas = {k: v for k, v in self.etas.get(q, {}).items() if k in ("economic", "influence", "wonder", "conquest")}
        conds = ["economic", "influence", "wonder"]
        need = w.thresholds.get("conquest_capitals", 99)
        if int((w.players.get(q) or {}).get("capitals_held") or 0) >= need - 1:
            conds.append("conquest")          # the capitals ETA is a rough guess until one capital short
        etas = {k: v for k, v in etas.items() if k in conds}
        path = min(etas, key=lambda k: (etas[k], k)) if etas else None
        eta = etas.get(path, INF) if path else INF
        vp = w.progress(q)
        prog = max((float(vp.get(k) or 0) for k in conds), default=0.0)
        return eta, prog, path

    def spoil_candidates(self) -> list:
        """Rivals to stop, most dangerous first: [(eta, -progress, q)]."""
        w = self.w
        out = []
        for q in w.rivals:
            pl = w.players.get(q) or {}
            eta, prog, _ = self.danger_of(q)
            if (w.streaking(q) or prog >= self.SPOIL_PROGRESS or eta <= self.SPOIL_ETA
                    or int(pl.get("wonder_stage") or 0) >= self.SPOIL_WONDER_STAGE):
                out.append((eta, -prog, q))
        out.sort()
        return out

    def spoil_targets(self, q: str) -> list:
        """Cities of ``q`` whose capture sets it back."""
        w = self.w
        pl = w.players.get(q) or {}
        _, _, path = self.danger_of(q)
        mine = w.cities_of(q)
        if w.streaking(q) or path in ("economic", "influence"):
            return mine                       # any city resets both streaks
        if path == "wonder" or int(pl.get("wonder_stage") or 0) >= self.SPOIL_WONDER_STAGE:
            return [c for c in mine if w.cities[c].get("wonder_stage", 0) > 0]
        if path == "conquest":
            return [c for c in mine if w.cities[c].get("capital")]
        return mine

    # ---- raid hooks (see StrategistBot.plan_raid / execute_raid) ---------
    def choose_raid(self, cool: dict):
        w = self.w
        if w.turn < self.SPOIL_START:
            return None
        cands = self.spoil_candidates()[:self.SPOIL_RIVALS]
        if not cands:
            return super().choose_raid(cool)
        dist = w.bfs(list(w.my_cities), max_dist=30)
        prod = max(1.0, self.potential_gold_rate())
        have: dict = {}
        for i in w.my_armies:
            have = add_units(have, self.free_units(i))
        best, best_key = None, None
        for eta, _, q in cands:
            if cool.get(q, -1) >= w.turn:
                continue
            if w.at_peace(w.me, q) and w.res.get("influence", 0) < w.break_influence(partner=q):
                continue
            for c in self.spoil_targets(q):
                if c not in dist:
                    continue
                force = self.raid_force(c, q)
                if not force:
                    continue                  # cannot be taken with a sane force
                miss = {t: k - have.get(t, 0) for t, k in force.items() if k > have.get(t, 0)}
                t_ready = self.force_cost(miss) * (1 + dist[c] / 20.0) / prod + dist[c] + 2
                if eta < INF and t_ready > eta + self.SPOIL_SLACK:
                    continue                  # it would win before we get there
                key = (t_ready, c)
                if best_key is None or key < best_key:
                    best, best_key = {"tgt": c, "owner": q, "start": w.turn, "phase": "gather"}, key
            if best is not None:
                break                         # the most dangerous rival we can hurt
        return best or super().choose_raid(cool)

    def raid_value(self, tgt: int, owner: str) -> float:
        v = super().raid_value(tgt, owner)
        if any(q == owner for _, _, q in self.spoil_candidates()):
            v += self.SPOIL_VALUE
        return v

    RETARGET_EVERY = 3            # gather phase: compare the rival's cities this often

    def near_units(self, tgt: int, radius: int = 12) -> dict:
        w = self.w
        plain = w.bfs([tgt], max_dist=radius)
        out: dict = {}
        for i in w.my_armies:
            if i in plain:
                out = add_units(out, self.free_units(i))
        return out

    def raid_force(self, tgt: int, owner: str) -> dict:
        """The units already within 12 steps plus the cheapest addition
        (siege against walls, then one unit type or an infantry/cavalry mix)
        that beats the defenders, their neighbours and one turn of emergency
        recruiting. The strategist sizes a fresh force each time, so its
        recruits change type from turn to turn while the target walls up."""
        near = self.near_units(tgt)
        if near and self.raid_wins(tgt, owner, near):
            return near
        base = dict(near)
        siege = self.siege_needed(tgt) - near.get("siege", 0)
        if siege > 0:
            base["siege"] = base.get("siege", 0) + siege
        best, best_cost = None, INF
        for mix in (("cavalry",), ("infantry",), ("archer",), ("infantry", "cavalry")):
            k = 1
            while k <= 60:
                force = dict(base)
                for t in mix:
                    force[t] = force.get(t, 0) + k
                if self.raid_wins(tgt, owner, force):
                    c = self.force_cost({t: n - near.get(t, 0) for t, n in force.items()})
                    if c < best_cost:
                        best, best_cost = force, c
                    break
                k += 1 if k < 8 else 3
        return best or {}

    def missing_cost(self, tgt: int, owner: str):
        force = self.raid_force(tgt, owner)
        if not force:
            return INF, force
        near = self.near_units(tgt)
        return self.force_cost({t: k - near.get(t, 0) for t, k in force.items() if k > near.get(t, 0)}), force

    def execute_raid(self) -> None:
        """While gathering, move to another city of the same rival when the
        units near it would win there now, or (every RETARGET_EVERY turns)
        when it is much cheaper to take: a defender that walls up the city
        we march on leaves its other cities open."""
        w = self.w
        mem = self.raid
        owner = mem["owner"]
        if mem.get("phase") == "gather" and any(q == owner for _, _, q in self.spoil_candidates()):
            switch = None
            for c in self.spoil_targets(owner):
                if c == mem["tgt"]:
                    continue
                near = self.near_units(c, self.RAID_GATHER + 1)
                if near and self.raid_wins(c, owner, near):
                    switch = c
                    break
            if switch is None and (w.turn - mem["start"]) % self.RETARGET_EVERY == 0:
                cur, _ = self.missing_cost(mem["tgt"], owner)
                for c in self.spoil_targets(owner):
                    if c != mem["tgt"]:
                        cost, _ = self.missing_cost(c, owner)
                        if cost < 0.7 * cur:
                            switch, cur = c, cost
            if switch is not None:
                mem["tgt"] = switch
                mem["force"] = self.raid_force(switch, owner)
        super().execute_raid()

    GATHER_REACH = 40             # march units from this far to the gathering point

    def gather_near(self, tgt: int, plain: dict, enter, gather: int | None = None) -> None:
        """As the strategist's, but the whole army marches (the strategist
        only moves units within 16 steps of the target)."""
        w = self.w
        gather = self.RAID_GATHER if gather is None else gather
        dist = w.bfs([tgt], enter)
        for i in sorted(w.my_armies):
            d = dist.get(i)
            free = self.free_units(i)
            if not free or d is None or d > self.GATHER_REACH:
                continue
            if d <= gather:
                if d < gather:
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

    # ---- fog: scout the target before relying on what we see -------------
    def espionage(self) -> None:
        super().espionage()
        w, p = self.w, self.p
        raid = getattr(self, "raid", None)
        if raid is None or not (w.view.get("fog") or {}).get("active"):
            return
        q = raid["owner"]
        seen = self.memory.setdefault("scouted", {})
        if seen.get(q, -99) > w.turn - self.SCOUT_EVERY:
            return
        if any(r.get("target") == q and r.get("mission") == "military" for r in w.view.get("intel") or ()):
            return
        if sum(1 for o in p.orders if o.get("type") == "spy") >= C.SPY_ORDERS_PER_TURN:
            return
        cities = int((w.players.get(q) or {}).get("cities") or 0)
        invest = min(C.SPY_MAX_INVEST, max(C.SPY_MIN_INVEST, 2 * (C.CI_BASE + C.CI_PER_CITY * cities) + 10))
        if p.can({"gold": invest}, {"gold": self.ESPIONAGE_RESERVE}):
            p.orders.append({"type": "spy", "target": q, "mission": "military", "invest": invest})
            p.pay({"gold": invest})
            seen[q] = w.turn
