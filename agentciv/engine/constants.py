"""Every tunable number of AgentCiv lives here.

The engine, the rules JSON (``rules_json()``) and the generated agent guide
(docs/RULES.md, ``python -m agentciv.engine.rulesdoc``) all read from this
module, so balancing only ever needs to touch this file (and then regenerate
RULES.md and update the numbers quoted in docs/DESIGN.md).
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Versions (part of the rules fingerprint, rulesdoc.rules_sha256)
# --------------------------------------------------------------------------
# Bump when the engine behaves differently without any constant or rules text
# changing (resolution order, map generation, what a view contains). The
# recorded golden games are tied to it (tests/test_fog_off_identity.py), so a
# change that re-records them cannot keep the old number.
ENGINE_VERSION = 1
# Bump when the server's turn protocol changes (live or synchronous phases,
# barrier order, deadlines, what is hidden from whom).
PROTOCOL_VERSION = 1

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
# (column index grows outward). It is mirror-symmetric about the middle row,
# so mirrored start positions get exactly the same neighbourhood. 'C' marks the capital (plains). It is rotated
# by multiples of 90 degrees so that it always faces outward, and stamped onto
# every tile that is closer to that start than to any other start (so the
# inner START_CORE_RADIUS square is always identical for every player, and
# most of the rest is too).
START_TEMPLATE = [
    "m.f..h...f.",
    ".h..f......",
    "f.h.f..f..h",
    "...f..h..f.",
    "g...f...g..",
    "..f.hC.hff~",
    "g...f...g..",
    "...f..h..f.",
    "f.h.f..f..h",
    ".h..f......",
    "m.f..h...f.",
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
# Relics sit on a ring whose radius (x the mean start radius) is drawn per map
# from [MIN, MAX]; one relic between every pair of neighbouring starts.
MAPGEN_RELIC_RING_MIN = 0.45
MAPGEN_RELIC_RING_MAX = 1.0
RELIC_MIN_SPACING = 3              # min Chebyshev distance between two relics
MAPGEN_LAND_TOLERANCE = 1.0        # max excess land share (tiles) after equalising
MAPGEN_CONTESTED_LAND_VALUE = 0.0  # weight of land tied between two capitals in that share
MAPGEN_RELIC_FLANK_WEIGHT = 6.0    # penalty weight: relic not equidistant from its two capitals
MAPGEN_RELIC_TOLERANCE = 2.0
MAPGEN_RELIC_RESTARTS = 6          # local-search restarts per ring radius       # candidate layouts this close to the fairest are drawn by seed

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

RELIC_INFLUENCE = 3                # per turn for a relic tile whose owner has units on it
RELIC_INFLUENCE_UNGUARDED = 1      # ... and for one nobody of the owner stands on
MARKET_HALL_GOLD = 5

# Tile improvements (one per owned non-city tile).
IMPROVEMENTS = {
    "farm": {"terrain": ["."], "cost": {"wood": 20, "gold": 10}, "bonus": {"food": 2}},
    "lumber_mill": {"terrain": ["f"], "cost": {"wood": 15, "gold": 10}, "bonus": {"wood": 2}},
    "quarry": {"terrain": ["h"], "cost": {"wood": 25, "gold": 10}, "bonus": {"stone": 2}},
    "mine": {"terrain": ["g"], "cost": {"wood": 25, "stone": 20}, "bonus": {"gold": 2}},
    "temple": {"terrain": [".", "f", "h"], "cost": {"stone": 20, "gold": 20}, "bonus": {"influence": 1}},
}

# City buildings. Level k costs ``cost_per_level * k``.
CITY_BUILDINGS = {
    "walls": {"max": 3, "cost_per_level": {"stone": 40, "wood": 20}},
    "warehouse": {"max": 1, "cost_per_level": {"wood": 50, "stone": 30}},
    "market_hall": {"max": 1, "cost_per_level": {"wood": 40, "stone": 40}},
    "wonder": {"max": 5, "cost_per_level": {"stone": 165, "wood": 120, "gold": 130}},
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
MARKET_REVERSION = 0.25
MARKET_MAX_ORDER_FRACTION = 0.25  # of the pool's resource reserve
MARKET_MAX_NET_FRACTION = 0.9     # net buy volume may not drain more than this
MARKET_READMIT_PASSES = 3        # passes re-admitting dropped orders (see market.clear_resource)
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
GARRISON_CITY = 15
GARRISON_CAPITAL = 40
PLUNDER_FRACTION = 0.5
MAX_RECRUIT_PER_ORDER = 50
# Units removed first when starving (highest upkeep first).
STARVATION_ORDER = ("siege", "cavalry", "archer", "infantry")

# --------------------------------------------------------------------------
# Diplomacy / orders
# --------------------------------------------------------------------------
TREATY_MIN_TURNS = 20
TREATY_MAX_TURNS = 40
TREATY_BREAK_COST = 50            # influence x (1 + the breaker's earlier betrayals); must be held
TREATY_SLOT_DIVISOR = 2           # treaty slots = max(1, ceil(other living players / divisor))
TREATY_BREAK_PCT = 10             # legacy lost and bank share paid per break: pct x (1 + earlier betrayals) ...
TREATY_BREAK_MAX_PCT = 40         # ... at most this
TREATY_BOND_PER_BETRAYAL = 50     # bank gold pledged automatically per betrayal on every treaty signed or renewed
TREATY_RESIGN_COOLDOWN = 15       # turns after a break before the same pair may sign again
TREATY_BREAK_NOTICE = 1           # extra turns a broken pair stays movement-restricted
MAX_ORDERS_PER_TURN = 100
MAX_MESSAGE_LENGTH = 500          # `say` / `message` text
MESSAGES_IN_VIEW = 50

# Barter & deals (docs/DESIGN.md §13) ----------------------------------------
DIPLOMACY_ACTIONS_PER_TURN = 30   # applied diplomacy actions per player per turn (channel + orders)
SAY_PER_TURN = 10                 # `say` messages per player per turn (count as actions too)
MAX_MESSAGES_PER_TURN = SAY_PER_TURN   # legacy name (the `message` order is `say`)
MAX_ACTIONS_PER_CALL = 100        # entries looked at per Game.diplomacy() call
DEAL_MAX_QTY = 100000             # max of any quantity in a bundle
DEAL_MAX_TILES = 5                # tiles per bundle
DEAL_MAX_TILES_RECEIVED_PER_TURN = 5   # tiles a player may receive by deals per turn
DEAL_CONTRACT_MIN_TURNS = 1
DEAL_CONTRACT_MAX_TURNS = 30
DEAL_PEACE_MIN_TURNS = TREATY_MIN_TURNS
DEAL_PEACE_MAX_TURNS = TREATY_MAX_TURNS
DEAL_EXPIRES_MIN = 1
DEAL_EXPIRES_MAX = 5
DEAL_DEFAULT_EXPIRES_IN = 2       # deal made on turn t is open through the end of turn t+2
DEAL_MAX_OPEN_PER_PLAYER = 8      # own open proposals
DEAL_MESSAGE_MAX_LENGTH = 300     # `message` attached to propose/counter/reject
CONTRACT_DEFAULT_PENALTY = 25     # minimum influence penalty for a contract default
CONTRACT_DEFAULT_GOLD_PER_INFLUENCE = 2   # +1 influence penalty per 2 gold of obligation value still owed
DEALS_RECENT_IN_VIEW = 20         # closed deals involving you, in your view
DEALS_RECENT_IN_FULL_VIEW = 100   # closed deals in the full spectator view
DEALS_LOG_IN_VIEW = 50            # public log of executed deals (most recent)
DIPLOMACY_FEED_MAX = 5000         # diplomacy events kept for Game.inbox()
TRADE_OFFER_TTL = DEAL_DEFAULT_EXPIRES_IN   # legacy name: offer_trade is a `propose`

# --------------------------------------------------------------------------
# Victory & score
# --------------------------------------------------------------------------
DEFAULT_MAX_TURNS = 150
RELICS_PER_PLAYER = 1             # R = RELICS_PER_PLAYER * n relic tiles
RELIC_HALF_MIN = 4                # map generation: relic fairness is measured up to the ceil(R/2)-th nearest relic
WONDER_VICTORY_STAGE = 5
BANK_VICTORY = 3600            # economic: bank >= this (at max_turns = VICTORY_REF_TURNS)
LEGACY_VICTORY = 2700          # influence: legacy >= this
VICTORY_STREAK_TURNS = 10      # consecutive turn ends, original capital owned
VICTORY_REF_TURNS = DEFAULT_MAX_TURNS
VICTORY_MIN_SCALE = 0.5
BANK_BASE = 50                 # gold that may be banked per turn (while owning a city) ...
BANK_PER_MARKET_HALL = 10      # ... plus this per owned city with a market_hall
STREAK_DEPOSIT_DIVISOR = 2     # an economic streak turn needs >= ceil(bank_limit / this) banked that turn
BANK_SEIZE_FRACTION = PLUNDER_FRACTION
LEGACY_CAPITAL_LOSS = 0.25
LEDGER_PROGRESS_WEIGHT = 0.8
CONQUEST_SMALL_GAME = 3           # n <= this: must own all original capitals

SCORE_WEIGHTS = {"tiles": 2, "cities": 15, "capitals_held": 50, "wonder_stage": 60, "relics_held": 15}
SCORE_DIVISORS = {"influence": 6, "gold": 25, "military_power": 20}

# Order in which conditions are reported when a player meets several at once.
VICTORY_CONDITIONS = ("conquest", "wonder", "influence", "economic")

# --------------------------------------------------------------------------
# Fog of war and espionage (games created with ``fog: true``; docs/RULES.md §14)
# --------------------------------------------------------------------------
FOG_VISION_TERRITORY = 1   # Chebyshev sight radius around every owned tile
FOG_VISION_CITY = 2        # ... around every owned city
FOG_VISION_UNITS = 1       # ... around every tile where the player has units
FOG_VISION_CAVALRY = 2     # ... if those units include cavalry
FOG_SIGHTING_TURNS = 5     # remembered rival stacks older than this many turns are dropped
FOG_HIDDEN_FIELDS = ("resources", "units", "military_power", "upkeep", "score")
FOG_ORDER_TYPES = ("spy", "counterintel")
SPY_MISSIONS = ("military", "treasury")
SPY_MIN_INVEST = 20        # gold
SPY_MAX_INVEST = 1000      # gold
SPY_ORDERS_PER_TURN = 2
SPY_REPORT_TURNS = 3       # a report stays in the spy's view for this many turns
CI_BASE = 10               # counter-intelligence rating = CI_BASE + CI_PER_CITY * cities + pool
CI_PER_CITY = 5
CI_MAX_INVEST = 500        # gold per counterintel order (one per turn)
CI_DECAY = (3, 4)          # pool = pool * 3 // 4 at the end of every turn

