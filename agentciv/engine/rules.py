"""Derived rule formulas and the JSON rules document (``rules_json()``).

Everything here is computed from :mod:`agentciv.engine.constants`.
"""
from __future__ import annotations

import copy
import math

from . import constants as C


# --------------------------------------------------------------------------
# Formulas
# --------------------------------------------------------------------------
def map_size(n_players: int) -> int:
    return C.MAP_BASE_SIZE + C.MAP_SIZE_PER_PLAYER * n_players


def relic_count(n_players: int) -> int:
    return max(1, C.RELICS_PER_PLAYER * n_players)


def relics_needed(n_relics: int) -> int:
    """Relics to hold for the relic victory: half of them (rounded up), but
    a majority when there are fewer than RELIC_HALF_MIN relics."""
    if n_relics < C.RELIC_HALF_MIN:
        return n_relics // 2 + 1
    return (n_relics + 1) // 2


def conquest_capitals(n_players: int) -> int:
    if n_players <= C.CONQUEST_SMALL_GAME:
        return n_players
    return n_players // 2 + 1


def claim_cost(owned_tiles: int) -> int:
    return C.CLAIM_BASE_COST + owned_tiles // C.CLAIM_TILES_PER_EXTRA


def settle_cost(cities_owned: int) -> dict:
    factor = 1 + C.SETTLE_COST_GROWTH * max(0, cities_owned - 1)
    return {r: int(math.ceil(v * factor - 1e-9)) for r, v in C.SETTLE_BASE_COST.items()}


def building_cost(building: str, level: int) -> dict:
    """Cost of building ``level`` (1-based) of a city building."""
    per = C.CITY_BUILDINGS[building]["cost_per_level"]
    return {r: v * level for r, v in per.items()}


def unit_cost(unit: str, count: int = 1) -> dict:
    return {r: v * count for r, v in C.UNITS[unit]["cost"].items()}


def storage_cap(warehouses: int) -> int:
    return C.STORAGE_BASE + C.WAREHOUSE_STORAGE * warehouses


def season(turn: int) -> tuple[int, str, dict]:
    idx = (turn // C.SEASON_LENGTH) % len(C.SEASONS)
    name, mods = C.SEASONS[idx]
    return idx, name, mods


def thresholds(n_players: int, max_turns: int) -> dict:
    r = relic_count(n_players)
    return {
        "conquest_capitals": conquest_capitals(n_players),
        "wonder_stage": C.WONDER_VICTORY_STAGE,
        "influence": C.INFLUENCE_VICTORY,
        "relics_needed": relics_needed(r),
        "relics_total": r,
        "relic_turns": C.RELIC_VICTORY_TURNS,
        "economic_gold": C.ECONOMIC_VICTORY_GOLD,
        "max_turns": max_turns,
    }


# --------------------------------------------------------------------------
# JSON rules document
# --------------------------------------------------------------------------
def _build_rules() -> dict:
    terrain = {}
    for ch, t in C.TERRAIN.items():
        dep = C.DEPOSITS.get(ch)
        terrain[ch] = {
            "name": t["name"],
            "passable": t["passable"],
            "yield": dict(t["yield"]),
            "deposit": {"resource": dep[0], "amount": dep[1]} if dep else None,
            "defense_bonus": C.TERRAIN_DEFENSE_BONUS if ch in C.DEFENSIVE_TERRAIN else 1.0,
        }
    units = {}
    for u, spec in C.UNITS.items():
        units[u] = {
            "cost": dict(spec["cost"]),
            "upkeep": spec["upkeep"],
            "strength": spec["strength"],
            "move": spec["move"],
            "counters": C.COUNTERS.get(u),
        }
    improvements = {
        name: {
            "terrain": [C.TERRAIN[t]["name"] for t in spec["terrain"]],
            "terrain_chars": list(spec["terrain"]),
            "cost": dict(spec["cost"]),
            "bonus": dict(spec["bonus"]),
        }
        for name, spec in C.IMPROVEMENTS.items()
    }
    city = {
        name: {
            "max": spec["max"],
            "cost_per_level": dict(spec["cost_per_level"]),
            "costs": [building_cost(name, k) for k in range(1, spec["max"] + 1)],
        }
        for name, spec in C.CITY_BUILDINGS.items()
    }
    return {
        "version": 1,
        "map": {
            "size": f"{C.MAP_BASE_SIZE} + {C.MAP_SIZE_PER_PLAYER}*n",
            "terrain": terrain,
            "city_yield": dict(C.CITY_YIELD),
            "capital_extra_influence": C.CAPITAL_EXTRA_INFLUENCE,
            "city_claim_radius": C.CITY_CLAIM_RADIUS,
            "relics": f"{C.RELICS_PER_PLAYER} * n" if C.RELICS_PER_PLAYER != 1 else "n",
        },
        "start": {"resources": dict(C.START_RESOURCES), "units": dict(C.START_UNITS)},
        "resources": list(C.RESOURCES),
        "tradable": list(C.TRADABLE),
        "seasons": {
            "length": C.SEASON_LENGTH,
            "cycle": [{"name": n, "modifiers": dict(m)} for n, m in C.SEASONS],
        },
        "storage": {
            "base": C.STORAGE_BASE,
            "per_warehouse": C.WAREHOUSE_STORAGE,
            "capped": list(C.CAPPED_RESOURCES),
        },
        "influence": {
            "city": C.CITY_YIELD["influence"],
            "capital_extra": C.CAPITAL_EXTRA_INFLUENCE,
            "temple": C.IMPROVEMENTS["temple"]["bonus"]["influence"],
            "relic": C.RELIC_INFLUENCE,
        },
        "units": units,
        "buildings": {"improvements": improvements, "city": city},
        "market_hall_gold": C.MARKET_HALL_GOLD,
        "expansion": {
            "claim_base_cost": C.CLAIM_BASE_COST,
            "claim_tiles_per_extra": C.CLAIM_TILES_PER_EXTRA,
            "settle_base_cost": dict(C.SETTLE_BASE_COST),
            "settle_cost_growth": C.SETTLE_COST_GROWTH,
            "city_min_distance": C.CITY_MIN_DISTANCE,
            "settle_contention_radius": C.SETTLE_CONTENTION_RADIUS,
        },
        "market": {
            "resources": list(C.MARKET_RESOURCES),
            "pools_per_player": {r: {"resource": a, "gold": b} for r, (a, b) in C.MARKET_POOLS_PER_PLAYER.items()},
            "fee": C.MARKET_FEE,
            "market_hall_fee": C.MARKET_HALL_FEE,
            "reversion": C.MARKET_REVERSION,
            "max_order_fraction": C.MARKET_MAX_ORDER_FRACTION,
            "max_net_fraction": C.MARKET_MAX_NET_FRACTION,
            "readmit_passes": C.MARKET_READMIT_PASSES,
        },
        "combat": {
            "counter_multiplier": C.COUNTER_MULTIPLIER,
            "counters": dict(C.COUNTERS),
            "archer_city_defense": C.ARCHER_CITY_DEFENSE,
            "terrain_defense_bonus": C.TERRAIN_DEFENSE_BONUS,
            "defensive_terrain": [C.TERRAIN[t]["name"] for t in C.DEFENSIVE_TERRAIN],
            "wall_bonus_per_level": C.WALL_BONUS_PER_LEVEL,
            "siege_per_wall_level": C.SIEGE_PER_WALL_LEVEL,
            "siege_city_attack": C.SIEGE_CITY_ATTACK,
            "garrison_city": C.GARRISON_CITY,
            "garrison_capital": C.GARRISON_CAPITAL,
            "plunder_fraction": C.PLUNDER_FRACTION,
            "max_recruit_per_order": C.MAX_RECRUIT_PER_ORDER,
            "starvation_order": list(C.STARVATION_ORDER),
        },
        "diplomacy": {
            "treaty_min_turns": C.TREATY_MIN_TURNS,
            "treaty_max_turns": C.TREATY_MAX_TURNS,
            "treaty_break_cost": C.TREATY_BREAK_COST,
            "trade_offer_ttl": C.TRADE_OFFER_TTL,
            "deals": {
                "actions": ["propose", "counter", "accept", "reject", "withdraw", "say"],
                "aliases": {"offer_trade": "propose", "accept_trade": "accept", "message": "say"},
                "tradable": list(C.TRADABLE),
                "max_qty": C.DEAL_MAX_QTY,
                "max_tiles_per_bundle": C.DEAL_MAX_TILES,
                "max_tiles_received_per_turn": C.DEAL_MAX_TILES_RECEIVED_PER_TURN,
                "contract_turns": [C.DEAL_CONTRACT_MIN_TURNS, C.DEAL_CONTRACT_MAX_TURNS],
                "peace_turns": [C.DEAL_PEACE_MIN_TURNS, C.DEAL_PEACE_MAX_TURNS],
                "expires_in": [C.DEAL_EXPIRES_MIN, C.DEAL_EXPIRES_MAX],
                "default_expires_in": C.DEAL_DEFAULT_EXPIRES_IN,
                "max_open_per_player": C.DEAL_MAX_OPEN_PER_PLAYER,
                "message_max_length": C.DEAL_MESSAGE_MAX_LENGTH,
                "contract_default_penalty": C.CONTRACT_DEFAULT_PENALTY,
                "contract_default_owed_per_influence": C.CONTRACT_DEFAULT_OWED_PER_INFLUENCE,
                "actions_per_turn": C.DIPLOMACY_ACTIONS_PER_TURN,
                "say_per_turn": C.SAY_PER_TURN,
                "max_actions_per_call": C.MAX_ACTIONS_PER_CALL,
                "recent_in_view": C.DEALS_RECENT_IN_VIEW,
                "log_in_view": C.DEALS_LOG_IN_VIEW,
            },
        },
        "limits": {
            "max_orders_per_turn": C.MAX_ORDERS_PER_TURN,
            "max_messages_per_turn": C.MAX_MESSAGES_PER_TURN,
            "max_message_length": C.MAX_MESSAGE_LENGTH,
            "max_diplomacy_actions_per_turn": C.DIPLOMACY_ACTIONS_PER_TURN,
            "max_deal_message_length": C.DEAL_MESSAGE_MAX_LENGTH,
        },
        "victory": {
            "conquest": f"own >= floor(n/2)+1 original capitals (all of them if n <= {C.CONQUEST_SMALL_GAME}), or be the last player standing",
            "wonder_stage": C.WONDER_VICTORY_STAGE,
            "influence": C.INFLUENCE_VICTORY,
            "relics_needed": f"ceil(R/2) (floor(R/2)+1 if R < {C.RELIC_HALF_MIN})",
            "relic_turns": C.RELIC_VICTORY_TURNS,
            "economic_gold": C.ECONOMIC_VICTORY_GOLD,
            "default_max_turns": C.DEFAULT_MAX_TURNS,
        },
        "score": {"weights": dict(C.SCORE_WEIGHTS), "divisors": dict(C.SCORE_DIVISORS)},
        "fog": {
            "applies_to": "games created with fog: true",
            "vision": {"territory": C.FOG_VISION_TERRITORY, "city": C.FOG_VISION_CITY,
                       "units": C.FOG_VISION_UNITS, "cavalry": C.FOG_VISION_CAVALRY},
            "progress_step": C.FOG_PROGRESS_STEP,
            "hidden_fields": list(C.FOG_HIDDEN_FIELDS),
            "sighting_turns": C.FOG_SIGHTING_TURNS,
            "orders": list(C.FOG_ORDER_TYPES),
            "spy": {"missions": list(C.SPY_MISSIONS), "min_invest": C.SPY_MIN_INVEST,
                    "max_invest": C.SPY_MAX_INVEST, "orders_per_turn": C.SPY_ORDERS_PER_TURN,
                    "report_turns": C.SPY_REPORT_TURNS},
            "counterintel": {"base": C.CI_BASE, "per_city": C.CI_PER_CITY, "max_invest": C.CI_MAX_INVEST,
                             "decay": list(C.CI_DECAY)},
        },
    }


_RULES_CACHE: dict | None = None


def rules_json() -> dict:
    """JSON-serialisable dict of all rule constants (a fresh copy per call)."""
    global _RULES_CACHE
    if _RULES_CACHE is None:
        _RULES_CACHE = _build_rules()
    return copy.deepcopy(_RULES_CACHE)
