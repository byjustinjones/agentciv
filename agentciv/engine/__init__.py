"""AgentCiv game engine (pure, deterministic, stdlib only).

Quick start::

    from agentciv.engine import Game, GameConfig
    g = Game(GameConfig(seed=1, max_turns=150, game_id="g1"))
    a, b = g.add_player("A"), g.add_player("B")
    g.start()
    errors = g.submit_orders(a, [{"type": "claim", "at": [5, 4]}])
    events = g.step()
    view = g.player_view(a)

See docs/DESIGN.md (contract) and docs/RULES.md (agent guide).
"""
from . import constants
from .game import Game, GameConfig
from .rules import rules_json

__all__ = ["Game", "GameConfig", "rules_json", "constants"]
