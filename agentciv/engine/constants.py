"""Every tunable number of AgentCiv lives here.

The engine, the rules JSON (``rules_json()``) and the generated agent guide
(docs/RULES.md, ``python -m agentciv.engine.rulesdoc``) all read from this
module, so balancing only ever needs to touch this file (and then regenerate
RULES.md and update the numbers quoted in docs/DESIGN.md).
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Players / map
# --------------------------------------------------------------------------
MIN_PLAYERS = 1
MAX_PLAYERS = 12
MAP_BASE_SIZE = 12            # W = H = MAP_BASE_SIZE + MAP_SIZE_PER_PLAYER * n
MAP_SIZE_PER_PLAYER = 2

PLAYER_COLORS = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#42d4f4",
    "#f032e6", "#bfef45", "#fabed4", "#469990", "#dcbeff", "#9a6324",
]

# terrain char -> properties. ``yield`` is the base yield of an owned,
# non-city tile per turn.
TERRAIN = {
    ".": {"name": "plains", "passable": True, "yield": {"food": 2}},
    "f": {"name": "forest", "passable": True, "yield": {"wood": 2}},
    "h": {"name": "hills", "passable": True, "yield": {"stone": 2}},
    "g": {"name": "gold", "passable": True, "yield": {"gold": 1}},
    "m": {"name": "mountain", "passable": False, "yield": {}},
    "~": {"name": "water", "passable": False, "yield": {}},
}
TERRAIN_CHARS = tuple(TERRAIN)
PASSABLE = frozenset(c for c, t in TERRAIN.items() if t["passable"])

# Finite deposits: terrain char -> (resource, initial amount).
DEPOSITS = {"h": ("stone", 300), "g": ("gold", 150)}

# Fixed yield of a city tile (regardless of terrain).
CITY_YIELD = {"food": 2, "wood": 1, "stone": 1, "gold": 2, "influence": 1}
CAPITAL_EXTRA_INFLUENCE = 1   # original capitals give +1 influence
CITY_CLAIM_RADIUS = 1         # a new city claims unowned tiles in this Chebyshev radius

# Map generation ------------------------------------------------------------
# Start neighbourhood template for a start on the EAST side of the map
# (column index grows outward). 'C' marks the capital (plains). It is rotated
# by multiples of 90 degrees so that it always faces outward, and stamped onto
# every tile that is closer to that start than to any other start (so the
# inner START_CORE_RADIUS square is always identical for every player, and
# most of the rest is too).
START_TEMPLATE = [
    "m.f..h...f.",
    ".h..f...g..",
    "f.h.f..f..h",
    "...f..h..f.",
    "g...f.h.g..",
    "..f..C..ff~",
    "f..h..f....",
    "..g..f.h.h.",
    "h...h..f..f",
    ".f..~..h...",
    "..h..f..m..",
]
START_CORE_RADIUS = 3             # starts are spaced >= 2*this+1 apart when possible
# Fraction of the random (non-template) land per terrain type.
MAPGEN_WATER_FRACTION = 0.09
MAPGEN_MOUNTAIN_FRACTION = 0.09
MAPGEN_HILLS_FRACTION = 0.12
MAPGEN_FOREST_FRACTION = 0.26     # of the remaining non-hills land
MAPGEN_GOLD_FRACTION = 0.035      # of passable random land
MAPGEN_SMOOTHING_PASSES = 2
MAPGEN_ATTEMPTS = 16               # best (fairest) of this many valid maps
MAPGEN_RELIC_RING = 0.14           # relic ring Manhattan radius as fraction of W

# --------------------------------------------------------------------------
# Starting state
# --------------------------------------------------------------------------
START_RESOURCES = {"food": 100, "wood": 80, "stone": 40, "gold": 50, "influence": 10}
START_UNITS = {"infantry": 3}

RESOURCES = ("food", "wood", "stone", "gold", "influence")
TRADABLE = ("food", "wood", "stone", "gold")

# --------------------------------------------------------------------------
# Economy
# --------------------------------------------------------------------------
SEASON_LENGTH = 6
SEASONS = [
    ("spring", {"food": 1.0, "wood": 1.0, "stone": 1.0, "gold": 1.0}),
    ("summer", {"food": 1.5, "wood": 1.0, "stone": 1.0, "gold": 1.0}),
    ("autumn", {"food": 1.0, "wood": 1.5, "stone": 1.0, "gold": 1.0}),
    ("winter", {"food": 0.5, "wood": 1.0, "stone": 1.0, "gold": 1.0}),
]

STORAGE_BASE = 300
WAREHOUSE_STORAGE = 200
CAPPED_RESOURCES = ("food", "wood", "stone")

RELIC_INFLUENCE = 3
MARKET_HALL_GOLD = 3

# Tile improvements (one per owned non-city tile).
IMPROVEMENTS = {
    "farm": {"terrain": ["."], "cost": {"wood": 20, "gold": 10}, "bonus": {"food": 2}},
    "lumber_mill": {"terrain": ["f"], "cost": {"wood": 15, "gold": 10}, "bonus": {"wood": 2}},
    "quarry": {"terrain": ["h"], "cost": {"wood": 25, "gold": 10}, "bonus": {"stone": 2}},
    "mine": {"terrain": ["g"], "cost": {"wood": 25, "stone": 20}, "bonus": {"gold": 2}},
    "temple": {"terrain": [".", "f", "h"], "cost": {"stone": 30, "gold": 30}, "bonus": {"influence": 2}},
}

# City buildings. Level k costs ``cost_per_level * k``.
CITY_BUILDINGS = {
    "walls": {"max": 3, "cost_per_level": {"stone": 40, "wood": 20}},
    "warehouse": {"max": 1, "cost_per_level": {"wood": 50, "stone": 30}},
    "market_hall": {"max": 1, "cost_per_level": {"wood": 40, "stone": 40}},
    "wonder": {"max": 5, "cost_per_level": {"stone": 60, "wood": 40, "gold": 40}},
}

# Expansion
CLAIM_BASE_COST = 2               # influence
CLAIM_TILES_PER_EXTRA = 8         # +1 influence per this many owned tiles
SETTLE_BASE_COST = {"food": 60, "wood": 40, "stone": 20, "gold": 20}
SETTLE_COST_GROWTH = 0.5          # x (1 + growth * (cities_owned - 1))
CITY_MIN_DISTANCE = 4             # Chebyshev distance to every other city
SETTLE_CONTENTION_RADIUS = 3      # simultaneous settles this close all fail

# --------------------------------------------------------------------------
# Market (constant-product pools vs gold; reserves are per player)
# --------------------------------------------------------------------------
MARKET_RESOURCES = ("food", "wood", "stone")
MARKET_POOLS_PER_PLAYER = {"food": (400, 400), "wood": (400, 600), "stone": (400, 800)}
MARKET_FEE = 0.05
MARKET_HALL_FEE = 0.02
MARKET_REVERSION = 0.05
MARKET_MAX_ORDER_FRACTION = 0.25  # of the pool's resource reserve
MARKET_MAX_NET_FRACTION = 0.9     # net buy volume may not drain more than this
MARKET_MAX_ITERATIONS = 5
MARKET_HISTORY_TURNS = 50

# --------------------------------------------------------------------------
# Military
# --------------------------------------------------------------------------
UNIT_TYPES = ("infantry", "archer", "cavalry", "siege")
UNITS = {
    "infantry": {"cost": {"food": 15, "wood": 10, "gold": 5}, "upkeep": 1, "strength": 10, "move": 1},
    "archer": {"cost": {"food": 10, "wood": 15, "gold": 5}, "upkeep": 1, "strength": 8, "move": 1},
    "cavalry": {"cost": {"food": 20, "wood": 10, "gold": 15}, "upkeep": 2, "strength": 12, "move": 2},
    "siege": {"cost": {"food": 10, "wood": 30, "stone": 20, "gold": 10}, "upkeep": 2, "strength": 4, "move": 1},
}
COUNTERS = {"infantry": "cavalry", "cavalry": "archer", "archer": "infantry"}  # attacker -> countered
COUNTER_MULTIPLIER = 1.5
ARCHER_CITY_DEFENSE = 1.5
TERRAIN_DEFENSE_BONUS = 1.25
DEFENSIVE_TERRAIN = ("f", "h")
WALL_BONUS_PER_LEVEL = 0.5
SIEGE_PER_WALL_LEVEL = 3
SIEGE_CITY_ATTACK = 4
GARRISON_CITY = 10
GARRISON_CAPITAL = 20
PLUNDER_FRACTION = 0.5
MAX_RECRUIT_PER_ORDER = 50
# Units removed first when starving (highest upkeep first).
STARVATION_ORDER = ("siege", "cavalry", "archer", "infantry")

# --------------------------------------------------------------------------
# Diplomacy / orders
# --------------------------------------------------------------------------
TREATY_MIN_TURNS = 10
TREATY_MAX_TURNS = 50
TREATY_BREAK_COST = 50            # influence
TRADE_OFFER_TTL = 3               # offer made on turn t expires after turn t+TTL
MAX_ORDERS_PER_TURN = 100
MAX_MESSAGES_PER_TURN = 5
MAX_MESSAGE_LENGTH = 500
MESSAGES_IN_VIEW = 50

# --------------------------------------------------------------------------
# Victory & score
# --------------------------------------------------------------------------
DEFAULT_MAX_TURNS = 150
RELIC_BASE = 2                    # R = n // 2 + RELIC_BASE
WONDER_VICTORY_STAGE = 5
INFLUENCE_VICTORY = 600
RELIC_VICTORY_TURNS = 10
ECONOMIC_VICTORY_GOLD = 2000
CONQUEST_SMALL_GAME = 3           # n <= this: must own all original capitals

SCORE_WEIGHTS = {"tiles": 2, "cities": 15, "capitals_held": 25, "wonder_stage": 20, "relics_held": 10}
SCORE_DIVISORS = {"influence": 5, "gold": 20, "military_power": 20}

# Order in which conditions are reported when a player meets several at once.
VICTORY_CONDITIONS = ("conquest", "wonder", "relics", "influence", "economic")
