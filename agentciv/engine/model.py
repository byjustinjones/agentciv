"""Plain state objects: players and cities."""
from __future__ import annotations

from . import constants as C


class Player:
    """Mutable per-player state."""

    __slots__ = (
        "id", "name", "index", "color", "resources", "alive", "eliminated_turn",
        "capital", "betrayals", "relic_streak", "wonder_city", "city_counter",
        "tiles", "final_score", "deals", "contracts_honoured", "defaults",
        "influence_debt",
    )

    def __init__(self, pid: str, name: str, index: int, color: str):
        self.id = pid
        self.name = name
        self.index = index
        self.color = color
        self.resources = dict(C.START_RESOURCES)
        self.alive = True
        self.eliminated_turn: int | None = None
        self.capital: int | None = None      # tile index of the original capital
        self.betrayals = 0
        self.relic_streak = 0
        self.wonder_city: int | None = None  # tile index of the city hosting the wonder
        self.city_counter = 0                # used for city names
        self.tiles = 0                       # owned tile count (maintained by Game)
        self.final_score: int | None = None  # score frozen at elimination
        # public reputation (DESIGN §13.4); ``betrayals`` above is the treaty counter
        self.deals = 0                       # executed deals (either side)
        self.contracts_honoured = 0          # contracts paid in full as payer
        self.defaults = 0                    # contracts defaulted on as payer
        self.influence_debt = 0              # unpaid default penalties (taken from future influence)


class City:
    """A city on a tile. ``capital`` marks an *original* capital (permanent)."""

    __slots__ = (
        "idx", "x", "y", "owner", "name", "capital", "original_owner",
        "walls", "warehouse", "market_hall", "wonder_stage", "founded_turn",
    )

    def __init__(self, idx: int, x: int, y: int, owner: str, name: str,
                 capital: bool, founded_turn: int):
        self.idx = idx
        self.x = x
        self.y = y
        self.owner = owner
        self.name = name
        self.capital = capital
        self.original_owner = owner
        self.walls = 0
        self.warehouse = 0
        self.market_hall = 0
        self.wonder_stage = 0
        self.founded_turn = founded_turn

    @property
    def garrison(self) -> int:
        """Base (pre-walls) strength of the intrinsic garrison."""
        return C.GARRISON_CAPITAL if self.capital else C.GARRISON_CITY

    def building_level(self, building: str) -> int:
        if building == "wonder":
            return self.wonder_stage
        return getattr(self, building)

    def view(self) -> dict:
        return {
            "x": self.x,
            "y": self.y,
            "owner": self.owner,
            "name": self.name,
            "capital": self.capital,
            "original_owner": self.original_owner,
            "buildings": {"walls": self.walls, "warehouse": self.warehouse,
                          "market_hall": self.market_hall},
            "wonder_stage": self.wonder_stage,
            "garrison": self.garrison,
        }
