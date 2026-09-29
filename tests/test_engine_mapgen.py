"""Map generation: fairness, reachability, determinism."""
import pytest

from agentciv.engine import constants as C
from agentciv.engine.mapgen import bfs, generate_map, rotate
from agentciv.engine.rules import map_size, relic_count
from agentciv.engine.testing import new_game

MID = len(C.START_TEMPLATE) // 2


def template_char(dx, dy):
    ch = C.START_TEMPLATE[dy + MID][dx + MID]
    return "." if ch == "C" else ch


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7, 8, 10, 12])
@pytest.mark.parametrize("seed", [1, 7])
def test_identical_start_neighbourhoods(n, seed):
    m = generate_map(n, seed)
    w = m.width
    assert w == map_size(n) and m.height == w
    assert m.stamp_radius >= 2
    for s, rot in zip(m.starts, m.rotations):
        sx, sy = s % w, s // w
        for dy in range(-m.stamp_radius, m.stamp_radius + 1):
            for dx in range(-m.stamp_radius, m.stamp_radius + 1):
                rx, ry = rotate(dx, dy, rot)
                assert m.terrain[(sy + ry) * w + sx + rx] == template_char(dx, dy)


@pytest.mark.parametrize("n", [2, 5, 6, 8, 12])
def test_starts_and_relics_reachable(n):
    m = generate_map(n, 3)
    d = bfs(m.terrain, m.width, m.height, m.starts[0])
    assert all(d[s] >= 0 for s in m.starts)
    assert all(d[r] >= 0 for r in m.relics)
    assert len(m.relics) == relic_count(n)
    assert len(set(m.relics)) == len(m.relics)
    assert all(m.terrain[r] == "." for r in m.relics)
    assert not set(m.relics) & set(m.starts)


@pytest.mark.parametrize("n", [4, 5, 6, 8])
def test_starts_equidistant_from_centre(n):
    m = generate_map(n, 5)
    w = m.width
    c = (w - 1) / 2
    manh = [abs(s % w - c) + abs(s // w - c) for s in m.starts]
    assert max(manh) - min(manh) <= 1


def test_starts_well_separated():
    for n in range(2, 13):
        m = generate_map(n, 1)
        w = m.width
        for i, a in enumerate(m.starts):
            for b in m.starts[i + 1:]:
                assert max(abs(a % w - b % w), abs(a // w - b // w)) >= 5


def test_mapgen_deterministic_and_seed_dependent():
    a, b, c = generate_map(6, 11), generate_map(6, 11), generate_map(6, 12)
    assert a.terrain == b.terrain and a.starts == b.starts and a.relics == b.relics
    assert a.terrain != c.terrain


def test_deposits_match_terrain():
    m = generate_map(5, 2)
    for t, d in zip(m.terrain, m.deposits):
        assert d == (C.DEPOSITS[t][1] if t in C.DEPOSITS else 0)


@pytest.mark.parametrize("n", [3, 6, 8])
def test_equal_starting_economy(n):
    g = new_game(n, seed=4)
    st = g.stats()
    first = st["p1"]
    for pid in g.alive_players():
        assert st[pid]["tiles"] == first["tiles"] == 9
        assert st[pid]["income"] == first["income"]
        assert st[pid]["units"] == first["units"]
    assert all(p.resources == C.START_RESOURCES for p in g.players)


@pytest.mark.parametrize("n,seed", [(9, 2), (9, 10), (7, 0), (12, 1)])
def test_regions_have_equal_terrain_mix(n, seed):
    """DESIGN §3: every region has the same number of hills, forest and gold
    tiles (±1). Regression: at 9 players protected start-core tiles made
    some regions keep up to 3 extra hills."""
    from agentciv.engine.mapgen import land_shares
    m = generate_map(n, seed)
    _share, region = land_shares(m.terrain, m.width, m.height, m.starts)
    for t in "hfg":
        counts = [sum(1 for _d, i in reg if m.terrain[i] == t) for reg in region]
        assert max(counts) - min(counts) <= 1, (t, counts)
