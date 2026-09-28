"""Generate the GUI's mock data (web/mock/*.json).

This is a *scripted* storyline, not the real engine: it produces spectator
views in the exact JSON shape of docs/DESIGN.md §10 so the spectator GUI can
be developed and screenshotted without a running server.

Storyline (6 players, 24x24 map, 73 turns):
  * p1 Athena   - peaceful builder chasing an influence victory
  * p2 Brutus   - rusher, attacks Athena, breaks a treaty with Draco
  * p3 Cassia   - wonder builder; completes stage 5 on turn 72 and wins
  * p4 Draco    - trader who grabs relics near the centre (relic streak)
  * p5 Echo     - conqueror; takes Fenix's cities (2 original capitals)
  * p6 Fenix    - eliminated on turn 57

Run:  python3 web/mock/make_mock.py   (writes next to this file)
"""
from __future__ import annotations

import json
import math
import os
import random

OUT = os.path.dirname(os.path.abspath(__file__))
rng = random.Random(20260928)

N = 6
W = H = 12 + 2 * N
MAX_TURNS = 150
END_TURN = 72  # turn in which the wonder victory happens
SNAPSHOTS = [0, 15, 30, 45, 60, 73]

NAMES = ["Athena", "Brutus", "Cassia", "Draco", "Echo", "Fenix"]
COLORS = ["#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231", "#911eb4"]
PIDS = [f"p{i + 1}" for i in range(N)]
STRENGTH = {"infantry": 10, "archer": 8, "cavalry": 12, "siege": 4}
UPKEEP = {"infantry": 1, "archer": 1, "cavalry": 2, "siege": 2}
SEASONS = [
    ("spring", {"food": 1.0, "wood": 1.0, "stone": 1.0, "gold": 1.0}),
    ("summer", {"food": 1.5, "wood": 1.0, "stone": 1.0, "gold": 1.0}),
    ("autumn", {"food": 1.0, "wood": 1.5, "stone": 1.0, "gold": 1.0}),
    ("winter", {"food": 0.5, "wood": 1.0, "stone": 1.0, "gold": 1.0}),
]
R = N // 2 + 2
RELICS_NEEDED = R // 2 + 1
CONQUEST_NEEDED = math.ceil(N / 2)

# --------------------------------------------------------------------- map
terrain = [["." for _ in range(W)] for _ in range(H)]


def inb(x, y):
    return 0 <= x < W and 0 <= y < H


def blob(ch, count, size):
    for _ in range(count):
        x, y = rng.randrange(W), rng.randrange(H)
        for _ in range(size):
            terrain[y][x] = ch
            dx, dy = rng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
            if inb(x + dx, y + dy):
                x, y = x + dx, y + dy


blob("f", 16, 11)
blob("h", 9, 6)
blob("~", 3, 14)
blob("m", 6, 6)
for _ in range(10):
    terrain[rng.randrange(H)][rng.randrange(W)] = "g"

TEMPLATE = [
    "f.f..h.",
    "..f..f.",
    "f......",
    ".g....h",
    "......f",
    "..h..f.",
    ".f...g.",
]


def rotate(tpl, k):
    grid = [list(r) for r in tpl]
    for _ in range(k):
        grid = [list(r) for r in zip(*grid[::-1])]
    return grid


cx = cy = (W - 1) / 2
starts = []
for i in range(N):
    ang = -math.pi / 2 + 2 * math.pi * i / N
    sx = round(cx + 0.36 * W * math.cos(ang))
    sy = round(cy + 0.36 * W * math.sin(ang))
    sx, sy = min(max(sx, 3), W - 4), min(max(sy, 3), H - 4)
    starts.append((sx, sy))
    tpl = rotate(TEMPLATE, i % 4)
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            terrain[sy + dy][sx + dx] = tpl[dy + 3][dx + 3]

relic_tiles = []
for i in range(R):
    ang = 2 * math.pi * (i + 0.5) / R
    x = round(cx + 3.6 * math.cos(ang))
    y = round(cy + 3.6 * math.sin(ang))
    terrain[y][x] = "."
    relic_tiles.append((x, y))

PASSABLE = set(".fhg")
deposits = {}
for y in range(H):
    for x in range(W):
        if terrain[y][x] == "h":
            deposits[(x, y)] = ["stone", 300]
        elif terrain[y][x] == "g":
            deposits[(x, y)] = ["gold", 150]

# ------------------------------------------------------------------ state
owner = [[None] * W for _ in range(H)]
cities = []  # dicts in the §10 shape
improvements = {}
relic_owner = {t: None for t in relic_tiles}
alive = {p: True for p in PIDS}
eliminated_turn = {p: None for p in PIDS}
betrayals = {p: 0 for p in PIDS}
relic_streak = {p: 0 for p in PIDS}
events_by_turn: dict[int, list] = {}
messages = []
treaties = []  # {"a","b","until_turn"}
market_history = []
prices = {"food": 1.0, "wood": 1.5, "stone": 2.0}
pools = {r: {"resource": 400 * N, "gold": int(400 * N * p)} for r, p in prices.items()}


def ev(turn, **kw):
    events_by_turn.setdefault(turn, []).append({"turn": turn, **kw})


def cheb(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def city_at(x, y):
    for c in cities:
        if (c["x"], c["y"]) == (x, y):
            return c
    return None


def own_radius1(pid, x, y):
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            nx, ny = x + dx, y + dy
            if inb(nx, ny) and terrain[ny][nx] in PASSABLE and owner[ny][nx] is None:
                owner[ny][nx] = pid


def found_city(pid, x, y, turn, capital=False):
    idx = sum(1 for c in cities if c["original_owner"] == pid) + 1
    name = f"{NAMES[PIDS.index(pid)]}-{idx}"
    terrain[y][x] = terrain[y][x] if terrain[y][x] in PASSABLE else "."
    deposits.pop((x, y), None)
    city = {"x": x, "y": y, "owner": pid, "name": name, "capital": capital,
            "original_owner": pid,
            "buildings": {"walls": 0, "warehouse": 0, "market_hall": 0},
            "wonder_stage": 0, "garrison": 20 if capital else 10}
    cities.append(city)
    owner[y][x] = pid
    own_radius1(pid, x, y)
    if not capital:
        ev(turn, type="city_founded", player=pid, x=x, y=y, name=name)
    return city


for pid, (sx, sy) in zip(PIDS, starts):
    found_city(pid, sx, sy, 0, capital=True)


def territory(pid):
    return [(x, y) for y in range(H) for x in range(W) if owner[y][x] == pid]


def frontier(pid):
    out = set()
    for x, y in territory(pid):
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if inb(nx, ny) and owner[ny][nx] is None and terrain[ny][nx] in PASSABLE:
                out.add((nx, ny))
    return out


def nearest_city_dist(pid, t):
    ds = [cheb(t, (c["x"], c["y"])) for c in cities if c["owner"] == pid]
    return min(ds) if ds else 99


# Scripted behaviour -------------------------------------------------------
CLAIM_RATE = {"p1": 0.85, "p2": 0.55, "p3": 0.7, "p4": 0.8, "p5": 0.65, "p6": 0.5}
SETTLE_TURNS = {"p1": [9, 24, 41], "p2": [14], "p3": [11, 33], "p4": [8, 22, 39],
                "p5": [12, 30], "p6": [16]}
WONDER_TURNS = [30, 42, 52, 62, 72]
RELIC_PLAN = {40: ("p3", 2), 44: ("p1", 0), 48: ("p4", 3), 55: ("p4", 4), 64: ("p4", 1)}
claim_acc = {p: 0.0 for p in PIDS}
BUILD_FOR = {".": "farm", "f": "lumber_mill", "h": "quarry", "g": "mine"}


def settle_spot(pid):
    cap = next(c for c in cities if c["owner"] == pid and c["capital"])
    best = None
    for y in range(H):
        for x in range(W):
            if terrain[y][x] not in PASSABLE or owner[y][x] not in (None, pid):
                continue
            if any(cheb((x, y), (c["x"], c["y"])) < 4 for c in cities):
                continue
            own_adj = owner[y][x] == pid or any(
                inb(x + dx, y + dy) and owner[y + dy][x + dx] == pid
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
            if not own_adj:
                continue
            d = cheb((x, y), (cap["x"], cap["y"]))
            score = abs(d - 4) + 0.05 * math.dist((x, y), (cx, cy)) + rng.random() * 0.3
            if best is None or score < best[0]:
                best = (score, x, y)
    return best[1:] if best else None


def capture_city(city, by, turn):
    prev = city["owner"]
    city["owner"] = by
    city["buildings"]["walls"] = max(0, city["buildings"]["walls"] - 1)
    if city["wonder_stage"]:
        city["wonder_stage"] = 0
    owner[city["y"]][city["x"]] = by
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            nx, ny = city["x"] + dx, city["y"] + dy
            if inb(nx, ny) and owner[ny][nx] == prev:
                owner[ny][nx] = by
    ev(turn, type="city_captured", x=city["x"], y=city["y"], name=city["name"],
       player=by, previous_owner=prev, capital=city["capital"])
    if not any(c["owner"] == prev for c in cities):
        alive[prev] = False
        eliminated_turn[prev] = turn
        for y in range(H):
            for x in range(W):
                if owner[y][x] == prev:
                    owner[y][x] = by if cheb((x, y), (city["x"], city["y"])) <= 3 else None
        ev(turn, type="eliminated", player=prev, by=by)


def army_units(pid, t):
    """Scripted army sizes: (garrison stack, field stack)."""
    base = 3 + t // 6
    if pid == "p2":
        return ({"infantry": 2 + t // 10, "archer": 1 + t // 15},
                {"infantry": 4 + t // 5, "cavalry": 2 + t // 8, "siege": t // 20})
    if pid == "p5":
        return ({"infantry": 3 + t // 12, "archer": 2 + t // 12},
                {"infantry": 3 + t // 6, "cavalry": 1 + t // 7, "siege": t // 18})
    if pid == "p1":
        return ({"infantry": base, "archer": 2 + t // 8}, {"archer": 1 + t // 14})
    if pid == "p3":
        return ({"infantry": 2 + t // 9, "archer": 2 + t // 10}, {})
    if pid == "p4":
        return ({"infantry": 2 + t // 10}, {"cavalry": 1 + t // 9, "infantry": 1 + t // 12})
    return ({"infantry": 3 + t // 10}, {"infantry": 1 + t // 15})


def compute_armies(t):
    out = []
    for pid in PIDS:
        if not alive[pid]:
            continue
        gar, field = army_units(pid, t)
        mine = [c for c in cities if c["owner"] == pid]
        cap = next((c for c in mine if c["capital"] and c["original_owner"] == pid), mine[0])
        out.append({"x": cap["x"], "y": cap["y"], "owner": pid,
                    "units": {k: v for k, v in gar.items() if v}})
        field = {k: v for k, v in field.items() if v}
        if field:
            terr = territory(pid)
            # field army sits on the owned tile closest to the map centre
            fx, fy = min(terr, key=lambda p: (math.dist(p, (cx, cy)), p))
            if city_at(fx, fy):
                terr2 = [p for p in terr if not city_at(*p)]
                fx, fy = min(terr2, key=lambda p: (math.dist(p, (cx, cy)), p))
            out.append({"x": fx, "y": fy, "owner": pid, "units": field})
        for c in mine[1:3]:
            if not c["capital"]:
                out.append({"x": c["x"], "y": c["y"], "owner": pid, "units": {"archer": 1 + t // 20}})
    # merge duplicates on the same tile
    merged = {}
    for a in out:
        key = (a["x"], a["y"], a["owner"])
        if key in merged:
            for u, n in a["units"].items():
                merged[key]["units"][u] = merged[key]["units"].get(u, 0) + n
        else:
            merged[key] = a
    return list(merged.values())


# Resource trajectories (roughly consistent with each archetype)
def resources(pid, t):
    k = PIDS.index(pid)
    if not alive[pid]:
        return {"food": 0, "wood": 0, "stone": 0, "gold": 0, "influence": 0}
    s = SEASONS[(t // 6) % 4][0]
    food_bias = {"summer": 60, "winter": -40}.get(s, 0)
    res = {
        "food": 110 + 3 * t + food_bias - 12 * k,
        "wood": 80 + 2 * t + 9 * k,
        "stone": 40 + (4 * t if pid == "p3" else 2 * t),
        "gold": 50 + {"p4": 17, "p1": 5, "p3": 5, "p2": 3, "p5": 4, "p6": 3}[pid] * t,
        "influence": 10 + {"p1": 6.1, "p4": 3.0, "p3": 2.6, "p2": 1.2, "p5": 1.6, "p6": 1.0}[pid] * t,
    }
    caps = 300 + (200 if t > 20 else 0)
    for r in ("food", "wood", "stone"):
        res[r] = max(0, min(caps, int(res[r] + 15 * math.sin(t * 0.7 + k))))
    res["gold"] = int(res["gold"] + 20 * math.sin(t * 0.3 + k))
    res["influence"] = int(res["influence"])
    return res


def income(pid, t):
    terr = territory(pid)
    inc = {"food": 0, "wood": 0, "stone": 0, "gold": 0, "influence": 0}
    base = {".": ("food", 2), "f": ("wood", 2), "h": ("stone", 2), "g": ("gold", 1)}
    for x, y in terr:
        c = city_at(x, y)
        if c:
            for r, v in (("food", 2), ("wood", 1), ("stone", 1), ("gold", 2), ("influence", 1)):
                inc[r] += v
            if c["capital"]:
                inc["influence"] += 1
            continue
        r, v = base.get(terrain[y][x], ("food", 0))
        inc[r] += v
        b = improvements.get((x, y))
        if b == "temple":
            inc["influence"] += 2
        elif b:
            inc[{"farm": "food", "lumber_mill": "wood", "quarry": "stone", "mine": "gold"}[b]] += 2
    inc["influence"] += 3 * sum(1 for o in relic_owner.values() if o == pid)
    mods = SEASONS[(t // 6) % 4][1]
    for r in ("food", "wood", "stone", "gold"):
        inc[r] = int(inc[r] * mods[r])
    return inc


def player_rows(t, armies):
    rows = []
    for i, pid in enumerate(PIDS):
        units = {u: 0 for u in STRENGTH}
        for a in armies:
            if a["owner"] == pid:
                for u, n in a["units"].items():
                    units[u] += n
        mil = sum(units[u] * STRENGTH[u] for u in units)
        mine = [c for c in cities if c["owner"] == pid]
        caps_held = sum(1 for c in mine if c["capital"])
        wstage = max([c["wonder_stage"] for c in mine] + [0])
        relics = sum(1 for o in relic_owner.values() if o == pid)
        res = resources(pid, t)
        tiles = len(territory(pid))
        score = (2 * tiles + 15 * len(mine) + 25 * caps_held + 20 * wstage
                 + res["influence"] // 5 + res["gold"] // 20 + 10 * relics + mil // 20)
        streak = relic_streak[pid]
        rows.append({
            "id": pid, "name": NAMES[i], "color": COLORS[i], "alive": alive[pid],
            "eliminated_turn": eliminated_turn[pid],
            "resources": res, "income": income(pid, t) if alive[pid] else {},
            "cities": len(mine), "tiles": tiles, "capitals_held": caps_held,
            "military_power": mil, "units": units, "wonder_stage": wstage,
            "relics_held": relics, "relic_streak": streak, "betrayals": betrayals[pid],
            "score": score if alive[pid] else 0,
            "submitted": alive[pid] and (i + t) % 4 != 0,
            "victory_progress": {
                "conquest": round(min(1, caps_held / CONQUEST_NEEDED), 3),
                "wonder": round(wstage / 5, 3),
                "influence": round(min(1, res["influence"] / 600), 3),
                "relics": round(min(1, streak / 10), 3) if relics >= RELICS_NEEDED else 0.0,
                "economic": round(min(1, res["gold"] / 2000), 3),
                "score": round(t / MAX_TURNS, 3),
            },
        })
    return rows


SCRIPTED_MESSAGES = {
    2: ("p1", "all", "Greetings, neighbours. Athena seeks only peace and trade."),
    3: ("p2", "p4", "Draco - non-aggression for 20 turns? Our borders will touch soon."),
    5: ("p4", "p2", "Agreed. Sending a proposal."),
    9: ("p3", "p1", "Cassia here. Want to keep the east quiet? I will not contest your relic."),
    11: ("p1", "p3", "Happy to. Treaty proposal incoming."),
    17: ("p5", "all", "Wood for sale, 1.4g each, DM me."),
    26: ("p2", "p1", "Your northern plains look undefended. Tribute of 80 gold buys you a quiet decade."),
    27: ("p1", "p2", "No."),
    34: ("p6", "all", "Echo is massing cavalry on my border. Anyone?"),
    36: ("p4", "p6", "Can sell you stone at market price for walls."),
    43: ("p2", "p4", "Nothing personal."),
    44: ("p4", "all", "PSA: Brutus broke our treaty. Do not trust Brutus."),
    51: ("p1", "all", "Cassia's wonder is at stage 3. Someone should look at that."),
    52: ("p5", "p1", "I'm busy finishing Fenix. After that, maybe."),
    58: ("p5", "all", "Fenix has fallen. The west is mine."),
    63: ("p1", "p2", "Truce? Cassia is two stages from winning."),
    64: ("p2", "p1", "Deal - I'll march east. Proposal sent."),
    69: ("p4", "all", "Three relics held. Seven more turns."),
    71: ("p3", "all", "Thank you all for a lovely game :)"),
}


# ---------------------------------------------------------------- simulate
frames = []
for t in range(0, SNAPSHOTS[-1] + 1):
    prev = t - 1  # events generated while resolving turn t-1 are shown at turn t
    if t > 0:
        # --- diplomacy
        if prev in SCRIPTED_MESSAGES:
            f, to, text = SCRIPTED_MESSAGES[prev]
            messages.append({"turn": prev, "from": f, "to": to, "text": text})
        if prev == 6:
            treaties.append({"a": "p2", "b": "p4", "until_turn": 26})
            ev(prev, type="treaty_signed", a="p2", b="p4", until_turn=26)
        if prev == 12:
            treaties.append({"a": "p1", "b": "p3", "until_turn": 62})
            ev(prev, type="treaty_signed", a="p1", b="p3", until_turn=62)
        if prev == 27:
            treaties[:] = [tr for tr in treaties if {tr["a"], tr["b"]} != {"p2", "p4"}]
            treaties.append({"a": "p2", "b": "p4", "until_turn": 57})
            ev(prev, type="treaty_signed", a="p2", b="p4", until_turn=57)
        if prev == 43:
            treaties[:] = [tr for tr in treaties if {tr["a"], tr["b"]} != {"p2", "p4"}]
            betrayals["p2"] += 1
            ev(prev, type="treaty_broken", player="p2", other="p4", cost=50)
        if prev == 20:
            treaties.append({"a": "p4", "b": "p5", "until_turn": 70})
            ev(prev, type="treaty_signed", a="p4", b="p5", until_turn=70)
        if prev == 62:
            treaties[:] = [tr for tr in treaties if {tr["a"], tr["b"]} != {"p1", "p3"}]
        if prev == 65:
            treaties.append({"a": "p1", "b": "p2", "until_turn": 85})
            ev(prev, type="treaty_signed", a="p1", b="p2", until_turn=85)
        # --- trades
        if prev in (18, 37, 54, 66):
            a, b, give, want = {18: ("p5", "p3", {"wood": 60}, {"gold": 45}),
                                37: ("p4", "p6", {"stone": 50}, {"gold": 90}),
                                54: ("p4", "p3", {"stone": 80}, {"gold": 150}),
                                66: ("p1", "p4", {"food": 100}, {"gold": 70})}[prev]
            ev(prev, type="trade_executed", offer_id=f"t{prev}", **{"from": a}, to=b, give=give, want=want)
        # --- market
        for r in prices:
            drift = {"food": 0.0, "wood": 0.004, "stone": 0.012}[r]
            if r == "stone" and 28 <= prev <= 60:
                drift = 0.02  # Cassia's wonder drives stone demand
            base = {"food": 1.0, "wood": 1.5, "stone": 2.0}[r]
            prices[r] = round(max(0.4, prices[r] + drift + rng.uniform(-0.06, 0.06) * base
                                  + 0.05 * (base - prices[r])), 3)
            pools[r]["gold"] = int(pools[r]["resource"] * prices[r])
        if prev % 3 == 0:
            r = ["food", "wood", "stone"][prev % 9 // 3]
            buyer, seller = PIDS[prev % 5], PIDS[(prev + 2) % 5]
            if alive[buyer] and alive[seller]:
                ev(prev, type="market", resource=r, price=prices[r],
                   bought={buyer: 20 + prev % 30}, sold={seller: 10 + prev % 17})
        # --- actions: settles, claims, builds
        for pid in PIDS:
            if not alive[pid]:
                continue
            if prev in SETTLE_TURNS[pid]:
                spot = settle_spot(pid)
                if spot:
                    found_city(pid, spot[0], spot[1], prev)
            claim_acc[pid] += CLAIM_RATE[pid] * (0.6 if prev > 50 else 1.0)
            while claim_acc[pid] >= 1:
                claim_acc[pid] -= 1
                cand = frontier(pid)
                if not cand:
                    break
                tx, ty = min(cand, key=lambda p: (nearest_city_dist(pid, p) + rng.random() * 1.5))
                if (tx, ty) in relic_owner:
                    continue
                owner[ty][tx] = pid
                ev(prev, type="claim", player=pid, x=tx, y=ty)
            if prev % 2 == PIDS.index(pid) % 2 and prev > 1:
                opts = [p for p in territory(pid) if p not in improvements and not city_at(*p)
                        and terrain[p[1]][p[0]] in BUILD_FOR and p not in relic_owner]
                if opts:
                    x, y = min(opts, key=lambda p: (nearest_city_dist(pid, p), p))
                    b = BUILD_FOR[terrain[y][x]]
                    if pid in ("p1", "p4") and prev % 8 == 0 and terrain[y][x] in ".fh":
                        b = "temple"
                    improvements[(x, y)] = b
                    ev(prev, type="build", player=pid, x=x, y=y, building=b)
            if prev % 7 == 3:
                gar = army_units(pid, prev)[0]
                unit = max(gar, key=gar.get)
                cap = next(c for c in cities if c["owner"] == pid)
                ev(prev, type="recruit", player=pid, x=cap["x"], y=cap["y"], unit=unit, count=2)
        # scripted city buildings
        for c in cities:
            p, b = c["owner"], c["buildings"]
            if c["capital"] and c["original_owner"] == p:
                if p == "p1" and prev in (14, 29):
                    b["walls"] += 1
                    ev(prev, type="build", player=p, x=c["x"], y=c["y"], building="walls", level=b["walls"])
                if p == "p3" and prev in (20, 26, 48):
                    b["walls"] += 1
                    ev(prev, type="build", player=p, x=c["x"], y=c["y"], building="walls", level=b["walls"])
                if p == "p4" and prev == 16:
                    b["market_hall"] = 1
                    ev(prev, type="build", player=p, x=c["x"], y=c["y"], building="market_hall")
                if prev == 22 + PIDS.index(p):
                    b["warehouse"] = 1
                    ev(prev, type="build", player=p, x=c["x"], y=c["y"], building="warehouse")
                if p == "p3" and prev in WONDER_TURNS:
                    c["wonder_stage"] += 1
                    ev(prev, type="wonder_stage", player=p, x=c["x"], y=c["y"], stage=c["wonder_stage"])
        # scripted relics
        if prev in RELIC_PLAN:
            pid, ri = RELIC_PLAN[prev]
            rx, ry = relic_tiles[ri]
            owner[ry][rx] = pid
            relic_owner[(rx, ry)] = pid
            ev(prev, type="tile_captured" if prev != 44 else "claim", player=pid, x=rx, y=ry, relic=True)
        # --- combat
        if prev in (38, 40, 44, 45, 49, 53, 56, 60):
            if prev in (38, 40, 44, 45):
                a, d = "p2", "p1"
            elif prev == 60:
                a, d = "p5", "p4"
            else:
                a, d = "p5", "p6"
            dcities = [c for c in cities if c["owner"] == d]
            if not dcities:
                continue
            target = min(dcities, key=lambda c: (c["capital"], -c["x"]))
            bx, by = target["x"] + (1 if a == "p2" else -1), target["y"]
            if not inb(bx, by) or terrain[by][bx] not in PASSABLE:
                bx, by = target["x"], target["y"] + 1
            winner = a if prev in (44, 45, 53, 56) else d
            ev(prev, type="battle", x=bx, y=by, sides=[a, d], winner=winner,
               losses={a: {"infantry": 3 + prev % 4, "cavalry": 1},
                       d: {"infantry": 2 + prev % 3, "archer": 2}})
            if prev == 45:
                capture_city(target, a, prev)
            if prev == 53 and not target["capital"]:
                capture_city(target, a, prev)
            if prev == 56:
                capture_city(next(c for c in cities if c["owner"] == d and c["capital"]), a, prev)
        if prev == 33:
            ev(prev, type="starvation", player="p2", units_lost={"cavalry": 2})
        if prev in (21, 47):
            ev(prev, type="order_failed", player="p6" if prev == 21 else "p2",
               order={"type": "build", "at": [4, 5], "building": "walls"} if prev == 21 else
               {"type": "move", "from": [9, 3], "path": [[10, 3]]},
               error="insufficient stone" if prev == 21 else "path blocked by treaty partner")
        # relic streaks
        for pid in PIDS:
            held = sum(1 for o in relic_owner.values() if o == pid)
            relic_streak[pid] = relic_streak[pid] + 1 if held >= RELICS_NEEDED else 0
        if prev == END_TURN:
            ev(prev, type="victory", winner="p3", condition="wonder")

    market_history.append({"turn": t, "prices": dict(prices)})
    if t not in SNAPSHOTS:
        continue

    # ---------------------------------------------------------- snapshot
    armies = compute_armies(t)
    rows = player_rows(t, armies)
    s_idx = (t // 6) % 4
    lo = SNAPSHOTS[SNAPSHOTS.index(t) - 1] if t else -1
    evs = [e for tt in range(lo, t) for e in events_by_turn.get(tt, [])]
    finished = t == SNAPSHOTS[-1]
    result = None
    if finished:
        survivors = sorted((r for r in rows if r["alive"] and r["id"] != "p3"), key=lambda r: -r["score"])
        placements = ["p3"] + [r["id"] for r in survivors] + ["p6"]
        result = {"winner": "p3", "condition": "wonder", "turn": END_TURN,
                  "placements": placements, "scores": {r["id"]: r["score"] for r in rows}}
    proposals, offers = [], []
    if t == 60:
        proposals = [{"from": "p1", "to": "p2", "turns": 20, "turn": 59}]
        offers = [{"id": "t59", "from": "p3", "to": "p4", "give": {"gold": 120},
                   "want": {"stone": 60}, "expires_turn": 62}]
    frame = {
        "game_id": "g7", "turn": t, "max_turns": MAX_TURNS,
        "status": "finished" if finished else "running",
        "deadline": None if finished else 1790000000.0 + t * 30,
        "season": {"name": SEASONS[s_idx][0], "index": s_idx, "turns_left": 6 - t % 6,
                   "modifiers": SEASONS[s_idx][1], "next": SEASONS[(s_idx + 1) % 4][0]},
        "you": None,
        "players": rows,
        "map": {
            "width": W, "height": H,
            "terrain": ["".join(r) for r in terrain],
            "owner": [list(r) for r in owner],
            "improvements": [{"x": x, "y": y, "building": b} for (x, y), b in sorted(improvements.items())],
            "deposits": [{"x": x, "y": y, "resource": r,
                          "remaining": max(0, v - (t * 4 if improvements.get((x, y)) in ("quarry", "mine") else 0))}
                         for (x, y), (r, v) in sorted(deposits.items()) if not city_at(x, y)],
            "relics": [{"x": x, "y": y, "owner": o} for (x, y), o in relic_owner.items()],
        },
        "cities": json.loads(json.dumps(cities)),
        "armies": armies,
        "market": {"fee": 0.05, "prices": dict(prices),
                   "pools": json.loads(json.dumps(pools)),
                   "history": market_history[-40:]},
        "treaties": [dict(tr) for tr in treaties if tr["until_turn"] > t],
        "treaty_proposals": proposals,
        "trade_offers": offers,
        "messages": [m for m in messages if m["turn"] < t][-50:],
        "events": evs,
        "victory": {"thresholds": {"conquest_capitals": CONQUEST_NEEDED, "wonder_stage": 5,
                                   "influence": 600, "relics_needed": RELICS_NEEDED,
                                   "relics_total": R, "relic_turns": 10,
                                   "economic_gold": 2000, "max_turns": MAX_TURNS},
                    "result": result},
    }
    frames.append(frame)

replay = {"frames": frames, "result": frames[-1]["victory"]["result"]}

players_meta = [{"id": p, "name": n, "is_bot": p != "p1" and p != "p3"} for p, n in zip(PIDS, NAMES)]
games = [
    {"game_id": "g9", "name": "Quickmatch #12", "status": "lobby", "turn": 0,
     "players": [{"id": "p1", "name": "claude-agent", "is_bot": False},
                 {"id": "p2", "name": "strategist", "is_bot": True}],
     "max_players": 6, "created": 1790001200.0},
    {"game_id": "g8", "name": "Friday Night Six", "status": "running", "turn": 60,
     "players": players_meta, "max_players": 6, "created": 1790000000.0},
    {"game_id": "g7", "name": "Wonder Race (demo)", "status": "finished", "turn": 73,
     "players": players_meta, "max_players": 6, "created": 1789990000.0},
    {"game_id": "g6", "name": "Bot Gauntlet 8p", "status": "finished", "turn": 150,
     "players": [{"id": f"p{i + 1}", "name": b, "is_bot": True} for i, b in enumerate(
         ["strategist", "economist", "rusher", "turtle", "random", "strategist", "economist", "idle"])],
     "max_players": 8, "created": 1789980000.0},
]
leaderboard = [
    {"name": "strategist", "rating": 21.84, "mu": 30.41, "sigma": 2.857, "games": 48, "wins": 21, "avg_place": 2.1},
    {"name": "claude-agent", "rating": 19.02, "mu": 28.95, "sigma": 3.31, "games": 17, "wins": 7, "avg_place": 2.4},
    {"name": "economist", "rating": 15.37, "mu": 24.12, "sigma": 2.917, "games": 46, "wins": 9, "avg_place": 3.3},
    {"name": "turtle", "rating": 12.6, "mu": 21.9, "sigma": 3.1, "games": 44, "wins": 5, "avg_place": 3.8},
    {"name": "rusher", "rating": 11.04, "mu": 20.72, "sigma": 3.227, "games": 45, "wins": 6, "avg_place": 4.1},
    {"name": "random", "rating": 1.93, "mu": 12.18, "sigma": 3.417, "games": 40, "wins": 0, "avg_place": 6.2},
    {"name": "idle", "rating": -2.4, "mu": 8.9, "sigma": 3.767, "games": 30, "wins": 0, "avg_place": 7.4},
]
bots = ["idle", "random", "economist", "rusher", "turtle", "strategist"]


def dump(name, obj):
    with open(os.path.join(OUT, name), "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
        fh.write("\n")


dump("replay.json", replay)
dump("games.json", games)
dump("leaderboard.json", leaderboard)
dump("bots.json", bots)
print(f"wrote {len(frames)} frames; turns {[f['turn'] for f in frames]}; "
      f"final scores {replay['result']['scores']}")
