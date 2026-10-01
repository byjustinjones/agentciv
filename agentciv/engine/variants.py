"""Experimental rule variants for offline balance experiments (docs/BALANCE.md §10).

A game's variants are ``GameConfig.variants``: a dict of the keys below. An
empty dict (the default) is today's rules, and every game created without
variants plays exactly as before. Variants are an offline tool: the server
never sets them, the rules text served to agents (``rulesdoc``,
docs/RULES.md) does not mention them, and none of them is a proposal until
the owner signs it off (docs/DESIGN.md, "Experimental variants").

Keys:

* ``city_loss``: what losing a city does to the economic and influence
  streaks (rules §11). ``"reset"`` (today): both streaks go to 0.
  ``"minus:N"``: each city lost that turn costs N streak turns (at least 0),
  and the turn end does not count. ``"held:N"``: only the loss of a city its
  owner had held for at least N turns resets the streaks; losing a newer
  city leaves them untouched (the turn end counts as usual).
* ``pool_allies``: allied combat (DESIGN §7). In a battle with three or
  more sides, sides that face exactly the same hostile sides on the tile
  (which makes them pairwise at peace) fight as one coalition, except the
  city owner. See :func:`agentciv.engine.combat.resolve`.
* ``symmetric_ties``: three or more sides tied at the lowest raw power,
  pairwise hostile, of the same defender status, none a city owner, and of
  equal power against each other in every pairing, destroy each other at
  once instead of fighting in queue order.
* ``bank_target``, ``legacy_target``: B and L before scaling with max_turns
  (constants ``BANK_VICTORY``, ``LEGACY_VICTORY``).
* ``bank_base``: gold bankable per turn before market halls (``BANK_BASE``).
"""
from __future__ import annotations

KEYS = ("city_loss", "pool_allies", "symmetric_ties", "bank_target", "legacy_target", "bank_base")


def parse_city_loss(value) -> tuple:
    """``"reset"`` -> ("reset", 0); ``"minus:3"`` -> ("minus", 3); ``"held:10"`` -> ("held", 10)."""
    v = str(value or "reset").strip().lower()
    if v == "reset":
        return ("reset", 0)
    kind, _, n = v.partition(":")
    if kind in ("minus", "held") and n.isdigit() and int(n) > 0:
        return (kind, int(n))
    raise ValueError(f"city_loss must be reset, minus:N or held:N (got {value!r})")


def validate(variants) -> dict:
    """Check and normalise a variants dict; raises ValueError on an unknown
    key or a bad value. Returns a new dict."""
    out = {}
    for k, v in dict(variants or {}).items():
        if k not in KEYS:
            raise ValueError(f"unknown variant {k!r} (known: {', '.join(KEYS)})")
        if k == "city_loss":
            parse_city_loss(v)
            out[k] = str(v).strip().lower()
        elif k in ("pool_allies", "symmetric_ties"):
            if v not in (True, False, 0, 1):
                raise ValueError(f"{k} must be a boolean")
            out[k] = bool(v)
        else:
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"{k} must be a non-negative integer")
            out[k] = v
    return out


def parse_arg(text: str) -> tuple:
    """``"key=value"`` from the command line -> (key, value) with ints and
    booleans converted."""
    k, sep, v = str(text).partition("=")
    if not sep:
        raise ValueError(f"variant must be key=value (got {text!r})")
    k, v = k.strip(), v.strip()
    if v.lower() in ("true", "yes", "on"):
        val = True
    elif v.lower() in ("false", "no", "off"):
        val = False
    elif v.lstrip("-").isdigit():
        val = int(v)
    else:
        val = v
    return k, val
