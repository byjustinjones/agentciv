"""Built-in bots. ``get_bot(name, seed)`` returns a fresh instance.

Registry entries are "module:Class" strings imported lazily so that a
missing/broken bot module only affects that bot.
"""
from __future__ import annotations

import importlib

from .base import Bot

REGISTRY: dict[str, str] = {
    "idle": "agentciv.bots.base:IdleBot",
    "random": "agentciv.bots.random_bot:RandomBot",
    "economist": "agentciv.bots.economist:EconomistBot",
    "rusher": "agentciv.bots.rusher:RusherBot",
    "turtle": "agentciv.bots.turtle:TurtleBot",
    "strategist": "agentciv.bots.strategist:StrategistBot",
    "strategist_lite": "agentciv.bots.strategist:StrategistLiteBot",
    "strategist_notrade": "agentciv.bots.strategist:StrategistNoTradeBot",
}

BOT_NAMES = list(REGISTRY)


def get_bot(name: str, seed: int = 0) -> Bot:
    try:
        spec = REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown bot {name!r}; choose from {BOT_NAMES}") from None
    module_name, cls_name = spec.split(":")
    cls = getattr(importlib.import_module(module_name), cls_name)
    return cls(seed=seed)
