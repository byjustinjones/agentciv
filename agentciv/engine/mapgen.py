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
* ``R = n`` relics sit on a ring around the centre, one in every angular gap
  between neighbouring starts (starting on the bisector, i.e. equidistant
  from the two capitals that flank it), locally optimised so that every
  capital sees the same pattern of relic distances and keeping the
  symmetries of the start layout; the ring radius is drawn by seed.
* Random terrain elsewhere comes from smoothed value noise. Several candidate
  maps are generated from the seed; invalid ones (unreachable starts/relics)
  are discarded and the one with the fairest path distances is kept. Land
  that would make some start's share of the map (tiles closer to it than to
  any other capital) larger than the smallest share is sunk (water), and the
  finished map is turned/mirrored by seed (one of the 8 square symmetries).

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
    slots: list = field(default_factory=list)   # start slot (index in start_layout) per start


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
    pts = _fix_parity(list(pts), w, lo, hi, want)
    dmin = min((_cheb(pts[i], pts[j]) for i in range(n) for j in range(i + 1, n)), default=want)
    rots = [int(math.floor(t + 0.5)) % 4 for t in ts]
    stamp = max(1, min(rad, (dmin - 1) // 2))
    return tuple(pts), tuple(rots), stamp


def _fix_parity(pts: list, w: int, lo: int, hi: int, want: int) -> list:
    """Move starts whose x+y parity differs from the majority by one tile.

    Two tiles are at equal path distance from some tile only if their x+y
    parities agree, so with mixed parities some relics could never be
    equidistant from the two capitals flanking them. Layouts with a
    quarter-turn symmetry keep mixed parities (a quarter turn flips parity;
    their symmetry already makes all starts equivalent). The half-turn
    symmetry is kept by moving a start and its mirror image together."""
    m = w - 1
    ss = set(pts)
    if {(m - y, x) for x, y in pts} == ss:
        return pts
    par = [(x + y) % 2 for x, y in pts]
    maj = 0 if par.count(0) >= par.count(1) else 1
    half = {(m - x, m - y) for x, y in pts} == ss
    c = m / 2.0
    radius = sorted(abs(x - c) + abs(y - c) for (x, y), q in zip(pts, par) if q == maj)
    target = radius[len(radius) // 2] if radius else 0
    done = set()
    for i in range(len(pts)):
        if par[i] == maj or i in done:
            continue
        x0, y0 = pts[i]
        partner = pts.index((m - x0, m - y0)) if half and (m - x0, m - y0) in pts else None
        best_key, best = None, None
        for dx, dy in ((1, 0), (0, 1), (-1, 0), (0, -1)):
            q = (x0 + dx, y0 + dy)
            cand = list(pts)
            cand[i] = q
            if partner is not None and partner != i:
                cand[partner] = (m - q[0], m - q[1])
            if any(not (lo <= a <= hi and lo <= b <= hi) for a, b in cand):
                continue
            if len(set(cand)) < len(cand):
                continue
            dmin = min(_cheb(cand[a], cand[b]) for a in range(len(cand)) for b in range(a + 1, len(cand)))
            key = (min(dmin, want), -abs(abs(q[0] - c) + abs(q[1] - c) - target), dmin, -dy, -dx)
            if best_key is None or key > best_key:
                best_key, best = key, cand
        if best is not None:
            pts = best
            done.add(i)
            if partner is not None:
                done.add(partner)
    return pts


def _start_polar(n: int) -> tuple:
    """(angles, mean Euclidean radius) of the start positions around the centre."""
    w = map_size(n)
    c = (w - 1) / 2.0
    pts, _rots, _stamp = start_layout(n)
    ang = [math.atan2(y - c, x - c) for x, y in pts]
    rad = sum(math.hypot(x - c, y - c) for x, y in pts) / max(1, len(pts))
    return ang, rad


def _gaps(n: int) -> list:
    """Angular gaps between neighbouring starts as (from, to) angles, to > from."""
    ang, _ = _start_polar(n)
    s = sorted(ang)
    out = []
    for j in range(len(s)):
        a, b = s[j], s[(j + 1) % len(s)]
        if b <= a:
            b += 2 * math.pi
        out.append((a, b))
    return out


def _in_gap(p: tuple, c: float, gap: tuple, margin: float = 0.12) -> bool:
    """Is ``p`` strictly inside the angular gap (minus a margin on each side)?"""
    a, b = gap
    t = math.atan2(p[1] - c, p[0] - c)
    while t < a:
        t += 2 * math.pi
    return a + margin * (b - a) <= t <= b - margin * (b - a)


def _relics_valid(pts, n: int) -> bool:
    w = map_size(n)
    starts, _rots, stamp = start_layout(n)
    if len(set(pts)) < len(pts):
        return False
    if any(not (0 < x < w - 1 and 0 < y < w - 1) for x, y in pts):
        return False
    if any(_cheb(p, q) < C.RELIC_MIN_SPACING for i, p in enumerate(pts) for q in pts[i + 1:]):
        return False
    return not any(_cheb(p, s) <= stamp for p in pts for s in starts)


def _relic_unfairness(pts, n: int) -> float:
    """Spread of the per-start sorted Manhattan distances to the relics. The
    ranks up to the number needed for victory count double and the last of
    them (the farthest relic a relic victory needs) four times."""
    starts, _rots, _stamp = start_layout(n)
    need = relics_needed(len(pts))
    d = [sorted(abs(p[0] - s[0]) + abs(p[1] - s[1]) for p in pts) for s in starts]
    return sum((4.0 if k == need - 1 else 2.0 if k < need else 1.0)
               * (max(v[k] for v in d) - min(v[k] for v in d)) for k in range(len(pts)))


@functools.lru_cache(maxsize=None)
def _gap_starts(n: int) -> tuple:
    """The two start positions flanking each gap (same order as _gaps)."""
    starts, _rots, _stamp = start_layout(n)
    ang, _ = _start_polar(n)
    order = sorted(range(len(ang)), key=lambda k: ang[k])
    return tuple((starts[order[j]], starts[order[(j + 1) % len(order)]]) for j in range(len(order)))


def _relic_objective(pts, n: int, rho: float) -> float:
    """Unfairness + a penalty for relics that are not equidistant from the
    two capitals flanking them (or closer to a third capital), + a small
    pull toward the ring radius."""
    c = (map_size(n) - 1) / 2.0
    ring = sum(abs(math.hypot(x - c, y - c) - rho) for x, y in pts)
    starts, _rots, _stamp = start_layout(n)
    flank = 0.0
    for p, (a, b) in zip(pts, _gap_starts(n)):
        da = abs(p[0] - a[0]) + abs(p[1] - a[1])
        db = abs(p[0] - b[0]) + abs(p[1] - b[1])
        flank += abs(da - db)
        near = min(abs(p[0] - q[0]) + abs(p[1] - q[1]) for q in starts if q != a and q != b) if len(starts) > 2 else 99
        flank += max(0, min(da, db) - near)
    return _relic_unfairness(pts, n) + C.MAPGEN_RELIC_FLANK_WEIGHT * flank + 0.1 * ring


def _square_ops(w: int) -> list:
    """The 8 symmetries of a w x w square grid (as functions on (x, y))."""
    m = w - 1
    return [
        lambda x, y: (x, y), lambda x, y: (m - y, x), lambda x, y: (m - x, m - y),
        lambda x, y: (y, m - x), lambda x, y: (m - x, y), lambda x, y: (x, m - y),
        lambda x, y: (y, x), lambda x, y: (m - y, m - x),
    ]


@functools.lru_cache(maxsize=None)
def start_symmetries(n: int) -> tuple:
    """Indices (into :func:`_square_ops`) of the grid symmetries that map
    the set of start positions onto itself."""
    starts, _rots, _stamp = start_layout(n)
    ops = _square_ops(map_size(n))
    ss = set(starts)
    return tuple(k for k, op in enumerate(ops) if {op(x, y) for x, y in starts} == ss)


def _gap_of(p: tuple, c: float, gaps: list):
    for j, g in enumerate(gaps):
        if _in_gap(p, c, g, margin=0.0):
            return j
    return None


def relic_candidate(n: int, frac: float, jitter: int = 0) -> tuple | None:
    """Relic positions for a ring at ``frac`` x the start radius (see
    :func:`_relic_candidate`), keeping as many symmetries of the start
    layout as the grid allows: all of them, else the rotations only, else
    the half turn, else none."""
    syms = set(start_symmetries(n))
    tried = set()
    for group in (syms, syms & {0, 1, 2, 3}, syms & {0, 2}, {0}):
        key = tuple(sorted(group))
        if key in tried:
            continue
        tried.add(key)
        pts = _relic_candidate(n, frac, key, jitter)
        if pts is not None:
            return pts
    return None


def _relic_candidate(n: int, frac: float, group: tuple, jitter: int = 0) -> tuple | None:
    """Relic positions for a ring at ``frac`` x the start radius, locally
    optimised for fairness, or None if no valid layout starts there.

    One relic per gap between angularly neighbouring starts, starting on the
    bisector of the two start directions (equidistant from the two capitals
    that flank it); the relics then move within their gaps while that makes
    the per-capital distance patterns more equal. The layout keeps every
    symmetry of the start layout (e.g. the 180-degree turn), so symmetric
    starts see exactly the same relic pattern.
    """
    w = map_size(n)
    c = (w - 1) / 2.0
    _ang, rad = _start_polar(n)
    gaps = _gaps(n)
    count = relic_count(n)
    if count != len(gaps):
        return None
    rho = frac * rad
    init = []
    for gap in gaps:
        phi = (gap[0] + gap[1]) / 2.0
        init.append((_rnd(c + rho * math.cos(phi)), _rnd(c + rho * math.sin(phi))))
    all_ops = _square_ops(w)
    ops = []
    for k in group:
        op = all_ops[k]
        perm = [_gap_of(op(*init[j]), c, gaps) for j in range(count)]
        if None not in perm and len(set(perm)) == count:
            ops.append((op, perm))
    if not ops:
        ops = [(all_ops[0], list(range(count)))]
    # orbits of gaps under the symmetries: one representative relic each
    rep_of, op_for = {}, {}
    for j in range(count):
        if j in rep_of:
            continue
        for op, perm in ops:
            k = perm[j]
            if k not in rep_of:
                rep_of[k], op_for[k] = j, op
    reps = sorted(set(rep_of.values()))
    stab = {r: [op for op, perm in ops if perm[r] == r] for r in reps}

    def fixed(r, q):
        return all(op(*q) == q for op in stab[r])

    def layout(pos: dict) -> list:
        return [op_for[j](*pos[rep_of[j]]) for j in range(count)]

    def ok_rep(r, q):
        return _in_gap(q, c, gaps[r]) and fixed(r, q)

    pos = {}
    jr = random.Random(n * 100003 + int(frac * 1000) * 101 + jitter)
    for r in reps:
        x0, y0 = init[r]
        opts = sorted((abs(dx) + abs(dy), abs(math.hypot(x0 + dx - c, y0 + dy - c) - rho), (x0 + dx, y0 + dy))
                      for dy in range(-3, 4) for dx in range(-3, 4) if ok_rep(r, (x0 + dx, y0 + dy)))
        if not opts:
            return None
        if jitter:
            # restart from a random nearby tile (the search is only local)
            near = [o for o in opts if o[0] <= 2]
            pos[r] = jr.choice(near)[2]
        else:
            pos[r] = opts[0][2]
    if not _relics_valid(layout(pos), n):
        # repair: move representatives to the nearest valid tile in their gap
        for r in reps:
            x0, y0 = pos[r]
            opts = []
            for dy in range(-3, 4):
                for dx in range(-3, 4):
                    q = (x0 + dx, y0 + dy)
                    if not ok_rep(r, q):
                        continue
                    trial = dict(pos)
                    trial[r] = q
                    part = [p for j, p in enumerate(layout(trial)) if rep_of[j] <= r]
                    if _relics_valid(part, n):
                        opts.append((abs(dx) + abs(dy), abs(math.hypot(q[0] - c, q[1] - c) - rho), q))
            if not opts:
                return None
            pos[r] = min(opts)[2]
        if not _relics_valid(layout(pos), n):
            return None
    best = _relic_objective(layout(pos), n, rho)
    moves = [(dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dx or dy]
    for _round in range(40):
        improved = False
        for r in reps:
            for dx, dy in moves:
                q = (pos[r][0] + dx, pos[r][1] + dy)
                if not ok_rep(r, q):
                    continue
                trial = dict(pos)
                trial[r] = q
                cand = layout(trial)
                if not _relics_valid(cand, n):
                    continue
                v = _relic_objective(cand, n, rho)
                if v < best - 1e-9:
                    best, pos, improved = v, trial, True
        if not improved:
            break
    return tuple(layout(pos))


@functools.lru_cache(maxsize=None)
def relic_rings(n: int) -> tuple:
    """Candidate relic layouts ``(unfairness, positions)`` for n players (pure
    geometry, cached): the fairest few, from rings of different radii. Each
    map draws one of them by seed, so relic positions vary between games."""
    lo, hi = C.MAPGEN_RELIC_RING_MIN, C.MAPGEN_RELIC_RING_MAX
    steps = 12
    found = {}
    for k in range(steps + 1):
        for jitter in range(C.MAPGEN_RELIC_RESTARTS):
            pts = relic_candidate(n, lo + (hi - lo) * k / steps, jitter)
            if pts is not None and pts not in found:
                found[pts] = _relic_unfairness(pts, n)
    if not found:
        # cramped maps: widen the search
        for k in range(1, 40):
            pts = relic_candidate(n, 0.1 + 0.025 * k)
            if pts is not None:
                found[pts] = _relic_unfairness(pts, n)
                break
    if not found:
        w = map_size(n)
        starts, _rots, stamp = start_layout(n)
        count = relic_count(n)
        pts = tuple(_greedy_relics(w, starts, stamp, count, relics_needed(count), max(count / 2.0, 0.14 * w)))
        found[pts] = _relic_unfairness(pts, n)
    best = min(found.values())
    out = sorted((u, p) for p, u in found.items() if u <= best + C.MAPGEN_RELIC_TOLERANCE)
    return tuple(out)


def relic_layout(n: int) -> tuple:
    """The fairest relic layout for n players."""
    return relic_rings(n)[0][1]


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


def land_shares(terrain: list, w: int, h: int, starts: list) -> tuple:
    """Per start: passable tiles closer (path distance) to it than to any
    other start (ties split), and the tiles only it is closest to as
    ``(distance, tile)`` lists."""
    dists = [bfs(terrain, w, h, s) for s in starts]
    share = [0.0] * len(starts)
    region: list = [[] for _ in starts]
    for i in range(w * h):
        best, owners = None, []
        for k, d in enumerate(dists):
            v = d[i]
            if v < 0:
                continue
            if best is None or v < best:
                best, owners = v, [k]
            elif v == best:
                owners.append(k)
        if not owners:
            continue
        for k in owners:
            share[k] += 1.0 / len(owners)
        if len(owners) == 1:
            region[owners[0]].append((best, i))
    return share, region


def _equalize_land(terrain: list, w: int, h: int, starts: list, relics: list, stamp: int,
                   rng: random.Random) -> None:
    """Sink the farthest land of players whose share of the map (the tiles
    closest to their capital) exceeds the smallest share, so every player
    has the same amount of land to expand into (square maps give some
    starts a whole corner). The identical start cores (plus one ring) and
    relic surroundings are kept."""
    protect = set()
    mid = stamp + 1
    for s in starts:
        sx, sy = s % w, s // w
        for y in range(max(0, sy - mid), min(h, sy + mid + 1)):
            for x in range(max(0, sx - mid), min(w, sx + mid + 1)):
                protect.add(y * w + x)
    for r in relics:
        rx, ry = r % w, r // w
        for y in range(max(0, ry - 1), min(h, ry + 2)):
            for x in range(max(0, rx - 1), min(w, rx + 2)):
                protect.add(y * w + x)
    for _round in range(4):
        share, region = land_shares(terrain, w, h, starts)
        # land tied between two capitals is contested: it counts only partly
        value = [len(reg) + C.MAPGEN_CONTESTED_LAND_VALUE * (sh - len(reg))
                 for sh, reg in zip(share, region)]
        target = min(value) + C.MAPGEN_LAND_TOLERANCE
        changed = False
        tie = [rng.random() for _ in range(w * h)]     # unbiased tie-breaks
        for k, tiles in enumerate(region):
            excess = value[k] - target
            for _d, i in sorted(tiles, key=lambda t: (-t[0], tie[t[1]])):
                if excess <= 0:
                    break
                if i in protect:
                    continue
                terrain[i] = "~"
                excess -= 1.0
                changed = True
        if not changed:
            break
    _balance_terrain(terrain, w, h, starts, protect, rng)


def _balance_terrain(terrain: list, w: int, h: int, starts: list, protect: set,
                     rng: random.Random) -> None:
    """Give every start the same number of forest, hills and gold tiles in
    its region (the land closer to it than to any other capital): regions
    above the average turn their farthest such tiles into plains, regions
    below it turn their farthest plains into that terrain."""
    for t in ("h", "f", "g"):
        _share, region = land_shares(terrain, w, h, starts)
        counts = [sum(1 for _d, i in reg if terrain[i] == t) for reg in region]
        target = int(round(sum(counts) / max(1, len(counts))))
        # protected tiles (start cores) can't be converted: never aim below
        # the largest protected count, or that region would stay above target
        target = max([target] + [sum(1 for _d, i in reg if terrain[i] == t and i in protect) for reg in region])
        tie = [rng.random() for _ in range(w * h)]
        for reg, cnt in zip(region, counts):
            far = sorted(reg, key=lambda t: (-t[0], tie[t[1]]))
            if cnt > target:
                for _d, i in far:
                    if cnt <= target:
                        break
                    if terrain[i] == t and i not in protect:
                        terrain[i] = "."
                        cnt -= 1
            elif cnt < target:
                for _d, i in far:
                    if cnt >= target:
                        break
                    if terrain[i] == "." and i not in protect:
                        terrain[i] = t
                        cnt += 1


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
    rings = relic_rings(n)
    need = relics_needed(relic_count(n))

    best = None
    for attempt in range(max(1, C.MAPGEN_ATTEMPTS)):
        _unf, relic_pos = rings[rng.randrange(len(rings))]
        relics = [y * w + x for x, y in relic_pos]
        terrain = _random_terrain(rng, w, h)
        _stamp_all(terrain, n)
        for x, y in relic_pos:
            terrain[y * w + x] = "."
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                j = ny * w + nx
                if 0 <= nx < w and 0 <= ny < h and terrain[j] not in C.PASSABLE:
                    terrain[j] = "."
        ok = False
        if n > 1:
            eq = list(terrain)
            _equalize_land(eq, w, h, starts, relics, stamp, rng)
            ok, score, details = _fairness(eq, w, h, starts, relics, stamp, need)
            if ok:
                terrain = eq
                details["equalized"] = True
        if not ok:
            # sinking land cut something off: keep the map unequalised, penalised
            ok, score, details = _fairness(terrain, w, h, starts, relics, stamp, need)
            details["equalized"] = False
            score += 50.0 if n > 1 else 0.0
        if ok and (best is None or score < best[0]):
            best = (score, terrain, details, attempt, relics)
            if score <= 1.0:
                break
    if best is None:
        # Extremely unlikely: fall back to an all-plains map (always valid).
        terrain = ["."] * (w * h)
        _stamp_all(terrain, n)
        relics = [y * w + x for x, y in rings[0][1]]
        for r in relics:
            terrain[r] = "."
            for j in (r - 1, r + 1, r - w, r + w):
                if 0 <= j < w * h and terrain[j] not in C.PASSABLE:
                    terrain[j] = "."
        best = (0.0, terrain, {"fallback": True}, -1, relics)
    score, terrain, details, attempt, relics = best
    # The whole map is turned/mirrored by seed (one of the 8 symmetries of
    # the square), so no start position is tied to a fixed map direction.
    k = rng.randrange(8)
    op = _square_ops(w)[k]
    new_terrain = [""] * (w * h)
    for i, t in enumerate(terrain):
        x, y = op(i % w, i // w)
        new_terrain[y * w + x] = t
    terrain = new_terrain

    def tr(i):
        x, y = op(i % w, i // w)
        return y * w + x

    def face(rot):
        fx, fy = rotate(1, 0, rot)
        ox, oy = op(fx, fy)
        zx, zy = op(0, 0)
        d = (ox - zx, oy - zy)
        return next(r for r in range(4) if rotate(1, 0, r) == d)
    starts = [tr(i) for i in starts]
    rots = [face(r) for r in rots]
    relics = [tr(i) for i in relics]
    deposits = [C.DEPOSITS[t][1] if t in C.DEPOSITS else 0 for t in terrain]
    # Seat assignment: which player gets which start slot is shuffled by seed.
    order = list(range(n))
    rng.shuffle(order)
    details = dict(details, score=score, attempt=attempt, orientation=k)
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
        slots=list(order),
    )
