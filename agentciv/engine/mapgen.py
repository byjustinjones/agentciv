"""Deterministic, fairness-optimised map generation.

Layout (see docs/DESIGN.md §3):

* Starts are spread evenly on a *diamond* (a circle in the 4-directional path
  metric) around the map centre, so every start has the same path distance to
  the centre. The geometry is chosen to keep starts at least
  ``2*START_CORE_RADIUS+1`` apart (Chebyshev) whenever the map allows it.
* The start template (11x11, ``START_TEMPLATE``) is stamped around every
  start, rotated by quarter turns to face outward, onto the tiles strictly
  closer to that start than to any other. The core ``stamp_radius`` square is
  therefore identical for every player.
* ``R = n//2 + 2`` relics sit on a smaller diamond around the centre.
* Random terrain elsewhere comes from smoothed value noise. Several candidate
  maps are generated from the seed; invalid ones (unreachable starts/relics)
  are discarded and the one with the fairest path distances is kept.

The only randomness in the whole engine is the ``random.Random(seed)`` used
here.
"""
from __future__ import annotations

import functools
import math
import random
from collections import deque
from dataclasses import dataclass, field

from . import constants as C
from .rules import map_size, relic_count, relics_needed

_DIAMOND = ((1, 0), (0, 1), (-1, 0), (0, -1))


@dataclass
class MapData:
    width: int
    height: int
    terrain: list            # flat list of terrain chars, index = y * width + x
    deposits: list           # flat list of remaining deposit amounts (0 = none)
    starts: list             # start tile index per start slot
    rotations: list          # template rotation (quarter turns) per start slot
    relics: list             # relic tile indices
    stamp_radius: int        # radius around each start that is guaranteed identical
    fairness: dict = field(default_factory=dict)


def rotate(dx: int, dy: int, k: int) -> tuple[int, int]:
    """Rotate an offset by ``k`` clockwise quarter turns (y grows downward)."""
    for _ in range(k % 4):
        dx, dy = -dy, dx
    return dx, dy


def _diamond_point(t: float) -> tuple[float, float]:
    t %= 4.0
    s = int(t)
    f = t - s
    a, b = _DIAMOND[s], _DIAMOND[(s + 1) % 4]
    return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f


def _rnd(v: float) -> int:
    return int(math.floor(v + 0.5))


def _cheb(a: tuple, b: tuple) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


@functools.lru_cache(maxsize=None)
def start_layout(n: int) -> tuple:
    """Return ``(positions, rotations, stamp_radius)`` for n players.

    Pure geometry (independent of the seed), cached per n.
    """
    w = map_size(n)
    c = (w - 1) / 2.0
    rad = C.START_CORE_RADIUS
    lo, hi = rad + 1, w - 2 - rad
    want = 2 * rad + 1
    best_key, best = None, None
    for oi in range(32):
        off = (4.0 / max(n, 1)) * oi / 32.0
        for rq in range(w * 2, 5, -1):
            r = rq / 2.0
            pts, ts = [], []
            for i in range(n):
                t = (off + 4.0 * i / n) % 4.0
                px, py = _diamond_point(t)
                pts.append((_rnd(c + r * px), _rnd(c + r * py)))
                ts.append(t)
            if any(not (lo <= x <= hi and lo <= y <= hi) for x, y in pts):
                continue
            if len(set(pts)) < n:
                continue
            dmin = min((_cheb(pts[i], pts[j]) for i in range(n) for j in range(i + 1, n)), default=want)
            manh = [abs(x - c) + abs(y - c) for x, y in pts]
            spread = max(manh) - min(manh)
            key = (min(dmin, want), -spread, dmin, r, -oi)
            if best_key is None or key > best_key:
                best_key, best = key, (pts, ts, dmin)
    if best is None:  # pragma: no cover - only for absurd constants
        raise ValueError(f"cannot place {n} starts on a {w}x{w} map")
    pts, ts, dmin = best
    rots = [int(math.floor(t + 0.5)) % 4 for t in ts]
    stamp = max(1, min(rad, (dmin - 1) // 2))
    return tuple(pts), tuple(rots), stamp


@functools.lru_cache(maxsize=None)
def relic_layout(n: int) -> tuple:
    """Relic positions (pure geometry) on an inner diamond, chosen so that the
    Manhattan distances from the starts are as even as possible."""
    w = map_size(n)
    c = (w - 1) / 2.0
    starts, _rots, stamp = start_layout(n)
    count = relic_count(n)
    need = relics_needed(count)
    rho0 = max(count / 2.0, C.MAPGEN_RELIC_RING * w)
    best_key, best = None, None
    # keep relics off the stamped start templates (fall back to the core only)
    clear = len(C.START_TEMPLATE) // 2
    if n >= 5:
        clear = max(stamp, clear - 1)
    base = int(rho0 * 2)
    candidates = list(range(base, base + 5)) + [q for q in range(2, w) if not base <= q < base + 5]
    for rq in candidates:
        rho = rq / 2.0
        for oi in range(32):
            off = (4.0 / count) * oi / 32.0
            pts = []
            for i in range(count):
                px, py = _diamond_point(off + 4.0 * i / count)
                pts.append((_rnd(c + rho * px), _rnd(c + rho * py)))
            if len(set(pts)) < count:
                continue
            if any(_cheb(p, q) < 2 for i, p in enumerate(pts) for q in pts[i + 1:]):
                continue
            if any(_cheb(p, s) <= clear for p in pts for s in starts):
                continue
            if any(not (0 < x < w - 1 and 0 < y < w - 1) for x, y in pts):
                continue
            dists = [sorted(abs(p[0] - s[0]) + abs(p[1] - s[1]) for p in pts) for s in starts]
            unf = sum(max(d[k] for d in dists) - min(d[k] for d in dists) for k in range(need))
            key = (-unf, -rho, -oi)
            if best_key is None or key > best_key:
                best_key, best = key, pts
        if best is not None:
            break
    if best is None:
        best = _greedy_relics(w, starts, stamp, count, need, rho0)
    return tuple(best)


def _greedy_relics(w: int, starts: tuple, stamp: int, count: int, need: int, rho0: float) -> list:
    """Fallback relic placement for cramped maps: add relics one at a time,
    keeping the per-start sorted distance vectors as equal as possible."""
    c = (w - 1) / 2.0
    cands = [(x, y) for y in range(1, w - 1) for x in range(1, w - 1)
             if all(_cheb((x, y), s) > stamp for s in starts)]
    chosen: list = []
    for _ in range(count):
        best_key, best = None, None
        for p in cands:
            if any(_cheb(p, q) < 2 for q in chosen):
                continue
            pts = chosen + [p]
            dists = [sorted(abs(q[0] - s[0]) + abs(q[1] - s[1]) for q in pts) for s in starts]
            k_max = min(need, len(pts))
            unf = sum(max(d[k] for d in dists) - min(d[k] for d in dists) for k in range(k_max))
            key = (unf, abs(abs(p[0] - c) + abs(p[1] - c) - rho0), p[1], p[0])
            if best_key is None or key < best_key:
                best_key, best = key, p
        if best is None:  # pragma: no cover - only for absurd constants
            raise ValueError("cannot place relics")
        chosen.append(best)
    return chosen


def _noise(rng: random.Random, w: int, h: int, passes: int) -> list:
    """Value noise: uniform random field smoothed by separable 3x3 box blurs."""
    g = [rng.random() for _ in range(w * h)]
    for _ in range(passes):
        tmp = [0.0] * (w * h)
        for y in range(h):
            row = g[y * w:(y + 1) * w]
            base = y * w
            for x in range(w):
                x0, x1 = max(0, x - 1), min(w - 1, x + 1)
                tmp[base + x] = sum(row[x0:x1 + 1]) / (x1 - x0 + 1)
        out = [0.0] * (w * h)
        for y in range(h):
            y0, y1 = max(0, y - 1), min(h - 1, y + 1)
            k = y1 - y0 + 1
            for x in range(w):
                s = 0.0
                for yy in range(y0, y1 + 1):
                    s += tmp[yy * w + x]
                out[y * w + x] = s / k
        g = out
    return g


def _quantile(values: list, frac: float) -> float:
    """Value below which ``frac`` of ``values`` lie."""
    if not values:
        return 0.0
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(frac * len(s))))
    return s[i]


def bfs(terrain: list, w: int, h: int, src: int) -> list:
    """4-directional path distances from ``src`` over passable tiles (-1 = unreachable)."""
    dist = [-1] * (w * h)
    dist[src] = 0
    q = deque([src])
    passable = C.PASSABLE
    while q:
        i = q.popleft()
        x, y = i % w, i // w
        d = dist[i] + 1
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h:
                j = ny * w + nx
                if dist[j] < 0 and terrain[j] in passable:
                    dist[j] = d
                    q.append(j)
    return dist


def _random_terrain(rng: random.Random, w: int, h: int) -> list:
    elev = _noise(rng, w, h, C.MAPGEN_SMOOTHING_PASSES)
    moist = _noise(rng, w, h, C.MAPGEN_SMOOTHING_PASSES)
    water_t = _quantile(elev, C.MAPGEN_WATER_FRACTION)
    mount_t = _quantile(elev, 1.0 - C.MAPGEN_MOUNTAIN_FRACTION)
    hills_t = _quantile(elev, 1.0 - C.MAPGEN_MOUNTAIN_FRACTION - C.MAPGEN_HILLS_FRACTION)
    forest_t = _quantile(moist, 1.0 - C.MAPGEN_FOREST_FRACTION)
    terrain = []
    for e, m in zip(elev, moist):
        if e < water_t:
            terrain.append("~")
        elif e >= mount_t:
            terrain.append("m")
        elif e >= hills_t:
            terrain.append("h")
        elif m >= forest_t:
            terrain.append("f")
        else:
            terrain.append(".")
    land = [i for i, t in enumerate(terrain) if t in (".", "h")]
    k = int(round(C.MAPGEN_GOLD_FRACTION * len(land)))
    for i in rng.sample(land, min(k, len(land))):
        terrain[i] = "g"
    return terrain


@functools.lru_cache(maxsize=None)
def stamp_overlay(n: int) -> tuple:
    """``((tile_index, terrain_char), ...)`` written by the start templates.

    The template is stamped around every start, restricted to the tiles
    strictly closer (Chebyshev, then Manhattan) to that start than to any
    other start ("Voronoi stamping"). Pure geometry, cached per n.
    """
    w = h = map_size(n)
    positions, rots, _stamp = start_layout(n)
    tmpl = C.START_TEMPLATE
    mid = len(tmpl) // 2
    out = []
    for si, ((sx, sy), rot) in enumerate(zip(positions, rots)):
        for dy in range(-mid, mid + 1):
            for dx in range(-mid, mid + 1):
                rx, ry = rotate(dx, dy, rot)
                x, y = sx + rx, sy + ry
                if not (0 <= x < w and 0 <= y < h):
                    continue
                mine = (max(abs(rx), abs(ry)), abs(rx) + abs(ry))
                if any((max(abs(x - ox), abs(y - oy)), abs(x - ox) + abs(y - oy)) <= mine
                       for oi, (ox, oy) in enumerate(positions) if oi != si):
                    continue
                ch = tmpl[dy + mid][dx + mid]
                out.append((y * w + x, "." if ch == "C" else ch))
    return tuple(out)


def _stamp_all(terrain: list, n: int) -> None:
    for i, ch in stamp_overlay(n):
        terrain[i] = ch


def _fairness(terrain: list, w: int, h: int, starts: list, relics: list, stamp: int, need: int):
    """Return (valid, unfairness score, details)."""
    dists = [bfs(terrain, w, h, s) for s in starts]
    for d in dists:
        if any(d[s] < 0 for s in starts) or any(d[r] < 0 for r in relics):
            return False, math.inf, {}
    rel = [sorted(d[r] for r in relics) for d in dists]
    rel_unf = sum(max(r[k] for r in rel) - min(r[k] for r in rel) for k in range(need))
    if len(starts) > 1:
        near = [min(d[t] for t in starts if t != s) for d, s in zip(dists, starts)]
        start_unf = max(near) - min(near)
    else:
        start_unf = 0
    # land quality around each start
    ring = len(C.START_TEMPLATE) // 2 + 1
    counts = []
    for s in starts:
        sx, sy = s % w, s // w
        cnt = {".": 0, "f": 0, "h": 0, "g": 0}
        for y in range(max(0, sy - ring), min(h, sy + ring + 1)):
            for x in range(max(0, sx - ring), min(w, sx + ring + 1)):
                t = terrain[y * w + x]
                if t in cnt:
                    cnt[t] += 1
        counts.append(cnt)
    land_unf = sum(max(c[t] for c in counts) - min(c[t] for c in counts) for t in (".", "f", "h", "g"))
    score = 2.0 * rel_unf + 1.0 * start_unf + 0.25 * land_unf
    return True, score, {"relic_spread": rel_unf, "start_spread": start_unf, "land_spread": land_unf}


def generate_map(n: int, seed: int) -> MapData:
    """Generate the map for ``n`` players from ``seed`` (deterministic)."""
    if n < 1:
        raise ValueError("need at least one player")
    rng = random.Random(seed)
    w = h = map_size(n)
    positions, rots, stamp = start_layout(n)
    starts = [y * w + x for x, y in positions]
    relic_pos = relic_layout(n)
    relics = [y * w + x for x, y in relic_pos]
    need = relics_needed(len(relics))

    best = None
    for attempt in range(max(1, C.MAPGEN_ATTEMPTS)):
        terrain = _random_terrain(rng, w, h)
        _stamp_all(terrain, n)
        for x, y in relic_pos:
            terrain[y * w + x] = "."
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                j = ny * w + nx
                if 0 <= nx < w and 0 <= ny < h and terrain[j] not in C.PASSABLE:
                    terrain[j] = "."
        ok, score, details = _fairness(terrain, w, h, starts, relics, stamp, need)
        if ok and (best is None or score < best[0]):
            best = (score, terrain, details, attempt)
            if score <= 1.0:
                break
    if best is None:
        # Extremely unlikely: fall back to an all-plains map (always valid).
        terrain = ["."] * (w * h)
        _stamp_all(terrain, n)
        best = (0.0, terrain, {"fallback": True}, -1)
    score, terrain, details, attempt = best
    deposits = [C.DEPOSITS[t][1] if t in C.DEPOSITS else 0 for t in terrain]
    # Seat assignment: which player gets which start slot is shuffled by seed.
    order = list(range(n))
    rng.shuffle(order)
    details = dict(details, score=score, attempt=attempt)
    return MapData(
        width=w,
        height=h,
        terrain=terrain,
        deposits=deposits,
        starts=[starts[i] for i in order],
        rotations=[rots[i] for i in order],
        relics=relics,
        stamp_radius=stamp,
        fairness=details,
    )
