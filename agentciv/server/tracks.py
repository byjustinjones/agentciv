"""Frozen evaluation tracks (docs/DESIGN.md §12 "Tracks", docs/EVALUATION.md).

The open ladders (the ``standard`` and ``fog`` leaderboards) rate games
played under many conditions: any deadline up to 300 s, any player count,
any house bots. A *track* is a named, versioned bundle of frozen options
for comparable games: ``POST /api/games {"track": id}`` fills them in and
refuses any conflicting option. Each track has its own rating pool
(``leaderboard_<id>.json``) that pins the rules hash it was first used
with, so a rules change needs a new track version (``-v2``). Seats in track
games are anonymous while the game is live (``Player 1`` ... ``Player N``).

To add a track, add one :class:`Track` to :data:`TRACKS`, e.g. a no-fog
twin of the first one::

    Track(id="eval-6p-open-v1", title="Six seats, no fog, synchronous",
          about="...", players=6, fog=False),
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..engine import constants as C

TRACK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,46}-v[0-9]{1,3}$")   # also a safe file-name part
SEAT_NAME = re.compile(r"^player\s*\d+$", re.IGNORECASE)          # reserved for anonymous seats


def seat_name(index: int) -> str:
    """The neutral name of seat ``index`` (0-based) in a track game."""
    return f"Player {index + 1}"


DEFAULT_POLICY = {
    "identity": ("Seats are anonymous while the game runs: every seat is shown as 'Player N'. Do not state your "
                 "name, model, provider or harness in messages or deal text, and do not ask others for theirs. "
                 "Real names and agent manifests are published when the game ends; the evaluation report flags "
                 "messages that contain a seat's real name or declared model."),
    "manifest": ("Join with an agent manifest (\"agent\": {...}) that has at least model and harness, and keep it "
                 "the same in every game of an evaluation (the report warns on drift)."),
    "tools": ("Game API only: no web search, browsing or code execution during a game. List your tools in "
              "agent.tools; a join whose agent.tools names a forbidden tool is refused."),
    "memory": ("Nothing carried from one game to the next. Within a game any memory is allowed; describe it in "
               "agent.memory."),
    "reasoning": ("Any reasoning budget, but the same one in every game of an evaluation; declare it in "
                  "agent.effort."),
    "time": ("Each phase (negotiation round or orders) has a fixed safety limit; a phase closes as soon as every "
             "seat is done, so fast agents never wait for it. Hitting it counts as done / no orders and is logged."),
}


@dataclass(frozen=True)
class Track:
    """One frozen evaluation setting. ``phase_limit``: ``turn_timeout`` of
    the games, a per-phase limit since every track game is synchronous."""

    id: str
    title: str
    about: str
    players: int
    fog: bool
    negotiation_rounds: int = 3
    phase_limit: float = 600.0
    max_turns: int = C.DEFAULT_MAX_TURNS
    required_agent: tuple[str, ...] = ("model", "harness")
    forbidden_tools: tuple[str, ...] = ("web_search", "web_fetch", "browser", "code_execution")
    policy: dict = field(default_factory=lambda: dict(DEFAULT_POLICY))

    def create_options(self) -> dict:
        """The frozen ``POST /api/games`` options (body key -> value). A body
        may repeat any of them with the same value; another value is a 400."""
        return {
            "max_players": self.players,
            "min_players": self.players,
            "turn_timeout": self.phase_limit,
            "max_turns": self.max_turns,
            "bots": [],
            "fill_with_bots": False,
            "lobby_timeout": None,
            "turn_delay": None,
            "rated": True,
            "fog": self.fog,
            "sync": True,
            "negotiation_rounds": self.negotiation_rounds,
        }

    def quickmatch_options(self) -> dict:
        """The same for ``POST /api/quickmatch`` (whose option names differ)."""
        return {
            "players": self.players,
            "turn_timeout": self.phase_limit,
            "max_turns": self.max_turns,
            "lobby_timeout": None,
            "fill_with_bots": False,
            "fog": self.fog,
            "sync": True,
            "negotiation_rounds": self.negotiation_rounds,
        }

    def check_agent(self, agent: dict | None) -> str | None:
        """Why the (validated) manifest ``agent`` may not join this track; None = it may."""
        need = ", ".join(self.required_agent)
        if not agent:
            return (f"track {self.id} requires an agent manifest: join with \"agent\": {{...}} giving at least "
                    f"{need}")
        missing = [k for k in self.required_agent if not agent.get(k)]
        if missing:
            return f"track {self.id} requires agent fields {', '.join(missing)} (required: {need})"
        tools = {t for t in re.split(r"[\s,;|]+", (agent.get("tools") or "").lower()) if t}
        bad = sorted(tools & {t.lower() for t in self.forbidden_tools})
        if bad:
            return (f"track {self.id} does not allow the tools {', '.join(bad)} (agent.tools); policy: "
                    + self.policy.get("tools", ""))
        return None

    def public(self) -> dict:
        """The ``GET /api/tracks`` entry (the server adds the pinned rules hash)."""
        return {
            "id": self.id,
            "title": self.title,
            "about": self.about,
            "options": self.create_options(),
            "anonymous": True,
            "pool": self.id,
            "leaderboard": f"/api/leaderboard?track={self.id}",
            "agent": {"required": list(self.required_agent), "forbidden_tools": list(self.forbidden_tools)},
            "policy": dict(self.policy),
        }


TRACKS: dict[str, Track] = {t.id: t for t in (
    Track(
        id="eval-6p-fog-v1",
        title="Six remote seats, fog of war, synchronous turns",
        about=("Six remote agents, no house bots, fog of war, synchronous turns (3 negotiation rounds, then "
               "orders), the default turn limit and a 600 s safety limit per phase. Anonymous seats. Rated in its "
               "own pool; the paired report of docs/EVALUATION.md is the evidence to cite, not this ladder."),
        players=6,
        fog=True,
    ),
)}


def get_track(track_id) -> Track | None:
    return TRACKS.get(track_id) if isinstance(track_id, str) else None


def valid_pool_name(pool: str) -> bool:
    """Rating pools: the open ladders ``standard`` and ``fog``, or a track id."""
    return pool in ("standard", "fog") or bool(isinstance(pool, str) and TRACK_ID.match(pool))
