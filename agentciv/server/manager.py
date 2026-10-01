"""Game sessions and the game manager (threading, timing, house bots).

A :class:`GameSession` wraps one :class:`agentciv.engine.Game` with

* a re-entrant lock + condition variable (every engine call happens under it),
* the seats (remote players with tokens, and in-process house bots),
* a worker thread that auto-starts the lobby, runs the house bots each turn
  and advances the turn when every living remote player has submitted or the
  deadline passes,
* cached JSON bytes of the spectator view (cheap for many GUI clients) and the
  replay frames (one spectator view per turn),
* checkpoints in ``<data_dir>/live`` (:mod:`.persist`): a pickled snapshot
  after every resolved turn and, throttled to one write per
  ``CHECKPOINT_INTERVAL``, after orders, diplomacy and joins; replay frames
  are appended to a side file once each.

The :class:`GameManager` owns all sessions, tokens, quickmatch lobbies and the
:class:`~agentciv.server.storage.Storage` (replays + leaderboard), and on
start-up restores the lobbies and running games checkpointed by an earlier
server process (same ids and tokens; the current turn gets a fresh deadline).
"""
from __future__ import annotations

import importlib
import json
import logging
import math
import pickle
import random
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from ..bots import REGISTRY as BOT_REGISTRY
from ..bots import get_bot
from ..bots.base import Bot, IdleBot
from ..engine import Game, GameConfig
from ..engine import constants as C
from ..engine.deals import ACTION_TYPES as DIPLOMACY_TYPES
from ..engine.deals import ALL_ACTION_TYPES, DealError, parse_action
from ..engine.rulesdoc import rules_sha256
from .guide import ORDER_EXAMPLES, order_hint
from .persist import FORMAT as CHECKPOINT_FORMAT
from .persist import LiveStore
from .provenance import ActionLog, ManifestError, validate_agent
from .replay import ArchivedReplay, FrameStore, envelope, slice_full_replay
from .storage import RulesChanged, Storage
from .tracks import SEAT_NAME, TRACKS, Track, get_track, seat_name

log = logging.getLogger("agentciv.server")

MIN_TURN_TIMEOUT = 0.05          # seconds; smaller positive values are raised to this
MAX_TIMEOUT = 86400.0
MAX_WAIT = 120.0                 # cap for /wait long-polls
DEFAULT_QUICKMATCH_LOBBY = 30.0  # quickmatch lobbies fill with bots after this many seconds
SPECTATOR_BOT_DELAY = 0.5        # pacing of games without living remote players
DEFAULT_FILL_BOTS = ["strategist", "economist", "rusher", "turtle", "random"]
BOT_SLOW_WARN = 5.0
ERROR_GRACE = 2.0                # after a submission with rejected orders, wait this long for a fix
ARCHIVE_CACHE_SIZE = 8           # parsed replay files kept in memory (compact frames)

# House-bot negotiation (docs/DESIGN.md §13.6)
NEGOTIATION_ROUNDS = 3           # rounds of Bot.negotiate(view) per turn before act()
NEGOTIATION_BUDGET = 5.0         # max seconds of those rounds per turn (later rounds are skipped)
NEGOTIATE_DEBOUNCE = 0.25        # a house bot answers this long after something is addressed to it ...
NEGOTIATE_DEBOUNCE_MAX = 1.0     # ... (the timer restarts on new items, but never beyond this)
REACTIVE_PER_TURN = 10           # mid-turn (reactive) negotiations per house bot per turn
MAX_NEGOTIATION_ROUNDS = 10      # synchronous games: most negotiation rounds a turn may have

# Ratings: only games played under standard, server-controlled conditions feed the leaderboard.
MAX_RATED_TURN_TIMEOUT = 300.0   # rated games need a real deadline, so a losing player can't stall forever
UNRATED_BOTS = frozenset({"idle", "random", "banker", "zealot", "spoiler"})  # creator-picked baseline bots make a game unrated

# Resource limits (attributes of GameManager, so embedders and tests can change them).
MAX_LIVE_GAMES = 200             # lobbies + running games in memory; creation beyond this -> 503
MAX_OPEN_LOBBIES = 50            # unstarted lobbies; creation beyond this -> 503
LOBBY_MAX_AGE = 3600.0           # an unstarted lobby is closed after this many seconds
FINISHED_KEEP = 16               # finished sessions kept in memory (then served from the replay file)
FINISHED_TTL = 300.0             # ... for at most this many seconds after finishing
BOT_ONLY_SLOTS = 2               # bot-only games whose house bots may think at the same time
RETIRED_TOKENS = 100_000         # tokens of evicted games still recognised (so late calls get 409, not 401)
MAX_GAMES_LISTED = 100           # default number of games in GET /api/games (all live ones + recent archived)

# Checkpoints (docs/DESIGN.md §12 "Server restarts")
CHECKPOINT_INTERVAL = 1.0        # at most one throttled checkpoint write per game per this many seconds
SHUTDOWN_JOIN = 5.0              # on shutdown, wait up to this long for game workers before the final checkpoint
CRASH_SEQ_GAP = 10_000           # diplomacy_seq jump when resuming from a checkpoint not written at shutdown
FINALIZE_RETRY = 5.0             # first retry (s) of a finished game whose rating or replay write failed ...
FINALIZE_RETRY_MAX = 300.0       # ... doubling up to this (retried from GameManager._sweep)


class ApiError(Exception):
    """An error returned to the HTTP client as ``{"error": message, ...extra}``."""

    def __init__(self, http_status: int, message: str, **extra):
        super().__init__(message)
        self.status = http_status
        self.message = message
        self.extra = extra

    def body(self) -> dict:
        return {"error": self.message, **self.extra}


def _dumps(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


# ---------------------------------------------------------------- bots
def bot_available(name: str) -> bool:
    """True if the built-in bot ``name`` can be imported."""
    spec = BOT_REGISTRY.get(name)
    if not spec:
        return False
    module_name, cls_name = spec.split(":")
    try:
        return hasattr(importlib.import_module(module_name), cls_name)
    except Exception:
        return False


def available_bots() -> list[str]:
    return [n for n in BOT_REGISTRY if bot_available(n)]


def make_bot(name: str, seed: int) -> tuple[Bot, str]:
    """Instantiate a house bot; falls back to the idle bot if it fails."""
    try:
        return get_bot(name, seed), name
    except Exception as e:
        log.warning("bot %r unavailable (%s: %s); using 'idle' instead", name, type(e).__name__, e)
        return IdleBot(seed=seed), "idle"


def _negotiator(bot):
    """The bot's ``negotiate`` method, or None if it has none (or only the
    do-nothing default of :class:`Bot`, so no view is built for it)."""
    fn = getattr(bot, "negotiate", None)
    if not callable(fn):
        return None
    base = getattr(Bot, "negotiate", None)
    if base is not None and getattr(type(bot), "negotiate", None) is base:
        return None
    return fn


def _action_hint(action) -> dict:
    """Example/hint for a diplomacy action that failed to parse (state errors get none)."""
    try:
        parse_action(action)
        return {}
    except DealError:
        pass
    except Exception:  # pragma: no cover - defensive
        return {}
    t = action.get("type") if isinstance(action, dict) else None
    if isinstance(t, str) and t in ALL_ACTION_TYPES:
        return order_hint(action)
    return {"hint": "valid diplomacy actions: " + ", ".join(DIPLOMACY_TYPES) + "; e.g. "
                    + json.dumps(ORDER_EXAMPLES["propose"], separators=(",", ":"))}


def _is_reserved_name(name: str) -> bool:
    low = name.lower()
    base = re.sub(r"#\d+$", "", low)
    return base in BOT_REGISTRY


# ---------------------------------------------------------------- input validation
def _number(body: dict, key: str, default, lo: float, hi: float, integer: bool = False,
            allow_none: bool = False):
    v = body.get(key, default)
    if v is None:
        if allow_none or default is None:
            return None
        v = default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        if isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                raise ApiError(400, f"{key} must be a number") from None
        else:
            raise ApiError(400, f"{key} must be a number")
    if isinstance(v, int) and abs(v) > 2 ** 63:  # math.isfinite() overflows on huge ints
        raise ApiError(400, f"{key} is out of range")
    if not math.isfinite(v):
        raise ApiError(400, f"{key} must be finite")
    if integer:
        if v != int(v):
            raise ApiError(400, f"{key} must be an integer")
        v = int(v)
    if not lo <= v <= hi:
        raise ApiError(400, f"{key} must be between {lo} and {hi}")
    return v


def _bool(body: dict, key: str, default: bool) -> bool:
    v = body.get(key, default)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise ApiError(400, f"{key} must be true or false")
    return v


def _turn_timeout(body: dict, default: float) -> float:
    t = _number(body, "turn_timeout", default, 0, MAX_TIMEOUT)
    if 0 < t < MIN_TURN_TIMEOUT:
        t = MIN_TURN_TIMEOUT
    return float(t)


def validate_player_name(name) -> str:
    if not isinstance(name, str):
        raise ApiError(400, "name must be a string")
    name = " ".join(name.split())
    if not name:
        raise ApiError(400, "name must not be empty")
    if len(name) > 40:
        raise ApiError(400, "name must be at most 40 characters")
    if not name.isprintable():
        raise ApiError(400, "name contains unprintable characters")
    if _is_reserved_name(name):
        raise ApiError(400, f"name {name!r} is reserved for built-in bots")
    return name


def parse_game_options(body: dict, operator: bool = False) -> dict:
    """Validate a ``POST /api/games`` body into a normalised options dict.
    ``operator``: the request carried the spectator key (``seed`` and
    ``seats`` of track games are operator-only)."""
    if not isinstance(body, dict):
        raise ApiError(400, "body must be a JSON object")
    if body.get("track") is not None:
        return _track_options(body, operator)
    if body.get("seats") is not None:
        raise ApiError(400, "seats is only valid with a track (operator-only paired play, see docs/EVALUATION.md)")
    max_players = _number(body, "max_players", 6, 1, C.MAX_PLAYERS, integer=True)
    min_players = _number(body, "min_players", min(2, max_players), 1, C.MAX_PLAYERS, integer=True)
    if min_players > max_players:
        raise ApiError(400, "min_players must be <= max_players")
    bots = body.get("bots") or []
    if not isinstance(bots, list) or not all(isinstance(b, str) for b in bots):
        raise ApiError(400, "bots must be a list of bot names")
    unknown = [b for b in bots if b not in BOT_REGISTRY]
    if unknown:
        raise ApiError(400, f"unknown bots {unknown}; choose from {list(BOT_REGISTRY)}")
    if len(bots) > max_players:
        raise ApiError(400, "more bots than max_players")
    name = body.get("name")
    if name is not None:
        if not isinstance(name, str):
            raise ApiError(400, "name must be a string")
        name = " ".join(name.split())[:80] or None
    seed = body.get("seed")
    seed_given = seed is not None
    if seed is None:
        seed = random.randrange(1, 2 ** 31)
    else:
        seed = _number(body, "seed", 0, -(2 ** 63), 2 ** 63, integer=True)
    lobby_timeout = _number(body, "lobby_timeout", None, 0, MAX_TIMEOUT, allow_none=True)
    turn_delay = _number(body, "turn_delay", None, 0, 60, allow_none=True)
    sync, rounds = _sync_options(body)
    return {
        "name": name,
        "max_players": max_players,
        "min_players": min_players,
        "turn_timeout": _turn_timeout(body, 30.0),
        "max_turns": _number(body, "max_turns", C.DEFAULT_MAX_TURNS, 1, 1000, integer=True),
        "seed": seed,
        "bots": list(bots),
        "fill_with_bots": _bool(body, "fill_with_bots", False),
        "lobby_timeout": float(lobby_timeout) if lobby_timeout else None,
        "turn_delay": None if turn_delay is None else float(turn_delay),
        "rated": _bool(body, "rated", True),
        "fog": _bool(body, "fog", False),
        "quickmatch": False,
        "seed_given": seed_given,
        "sync": sync,
        "negotiation_rounds": rounds,
    }


def _sync_options(body: dict) -> tuple[bool, int | None]:
    """``sync`` (bool, default false) and ``negotiation_rounds`` (0-10, default
    3; only valid with ``sync``) of a create/quickmatch body (docs/DESIGN.md §13.6)."""
    sync = _bool(body, "sync", False)
    if not sync:
        if body.get("negotiation_rounds") is not None:
            raise ApiError(400, "negotiation_rounds is only valid with \"sync\": true")
        return False, None
    return True, _number(body, "negotiation_rounds", NEGOTIATION_ROUNDS, 0, MAX_NEGOTIATION_ROUNDS, integer=True)


# ---------------------------------------------------------------- tracks (tracks.py)
def _same(value, want) -> bool:
    """Is a body value the frozen value (``true`` is not ``1``; ``600`` is ``600.0``)?"""
    if isinstance(want, bool) or isinstance(value, bool):
        return type(value) is bool and value is want
    if isinstance(want, (int, float)):
        return isinstance(value, (int, float)) and value == want
    return value == want


def _check_frozen(body: dict, frozen: dict, track: Track, free: tuple[str, ...]) -> None:
    """400 for a body option that a track fixes to another value, or that it doesn't allow at all."""
    for key, value in body.items():
        if key in free or value is None:
            continue
        if key not in frozen:
            raise ApiError(400, f"option {key} is not allowed with track {track.id} (allowed: "
                                f"{', '.join(free)}; everything else is frozen, see GET /api/tracks)")
        if not _same(value, frozen[key]):
            raise ApiError(400, f"option {key} conflicts with track {track.id}, which fixes it at "
                                f"{json.dumps(frozen[key])} (omit it, or see GET /api/tracks)")


def _track(track_id) -> Track:
    track = get_track(track_id)
    if track is None:
        raise ApiError(400, f"unknown track {track_id!r}; choose from {sorted(TRACKS)} (GET /api/tracks)")
    return track


def _track_seats(seats, n: int) -> list[str] | None:
    """The operator's ``seats`` (real names in seat order) of a track game."""
    if seats is None:
        return None
    if not isinstance(seats, list) or len(seats) != n:
        raise ApiError(400, f"seats must be a list of {n} player names (seat 1 first)")
    out = [validate_player_name(x) for x in seats]
    for x in out:
        if SEAT_NAME.match(x):
            raise ApiError(400, f"seats: {x!r} looks like an anonymous seat name ('Player N'); use real names")
    if len({x.lower() for x in out}) != n:
        raise ApiError(400, "seats must not repeat a name")
    return out


def _track_options(body: dict, operator: bool) -> dict:
    """``POST /api/games {"track": id, ...}``: the track's frozen options.
    ``name`` is free; ``seed`` and ``seats`` need the operator key."""
    track = _track(body.get("track"))
    _check_frozen(body, track.create_options(), track, ("track", "name", "seed", "seats"))
    for key in ("seed", "seats"):
        if body.get(key) is not None and not operator:
            raise ApiError(403, f"{key} on a track game is for the operator only (send the spectator key as "
                                "X-Spectator-Key); without it the server picks the seed and seats go in join order")
    name = body.get("name")
    if name is not None:
        if not isinstance(name, str):
            raise ApiError(400, "name must be a string")
        name = " ".join(name.split())[:80] or None
    seed_given = body.get("seed") is not None
    seed = (_number(body, "seed", 0, -(2 ** 63), 2 ** 63, integer=True) if seed_given
            else random.randrange(1, 2 ** 31))
    frozen = track.create_options()
    return {**frozen, "name": name, "turn_timeout": float(frozen["turn_timeout"]), "bots": [],
            "seed": seed, "seed_given": seed_given, "quickmatch": False, "track": track.id,
            "seats": _track_seats(body.get("seats"), track.players)}


def unrated_reason(opts: dict) -> str | None:
    """Why a game can't be rated (None = it can). Rated games must be played
    under standard conditions nobody can tailor: a server-chosen seed, the
    full turn limit, a real deadline, and no creator-picked baseline bots.
    Track games are rated in their track's pool: their conditions are frozen
    by the track, and only the operator may pick their seed and seats."""
    if not opts.get("rated", True):
        return "created with rated: false"
    if opts.get("track"):
        return None
    if opts.get("seed_given"):
        return "custom seed"
    if opts["max_turns"] < C.DEFAULT_MAX_TURNS:
        return f"max_turns below {C.DEFAULT_MAX_TURNS}"
    if not 0 < opts["turn_timeout"] <= MAX_RATED_TURN_TIMEOUT:
        return f"turn_timeout must be between 0 (exclusive) and {MAX_RATED_TURN_TIMEOUT:g} s"
    weak = sorted(set(opts.get("bots") or []) & UNRATED_BOTS)
    if weak:
        return "house bots " + ", ".join(weak)
    return None


def parse_agent(body: dict) -> dict | None:
    """The optional ``agent`` manifest of a join/quickmatch body (see :mod:`.provenance`)."""
    try:
        return validate_agent(body.get("agent"))
    except ManifestError as e:
        raise ApiError(400, str(e)) from None


def _player_key(body: dict):
    """Optional name key (registers the name / proves ownership of it)."""
    key = body.get("key")
    if key is None:
        return None
    if not isinstance(key, str) or not 8 <= len(key) <= 200:
        raise ApiError(400, "key must be a string of 8-200 characters")
    return key


# ---------------------------------------------------------------- sessions
@dataclass
class Seat:
    pid: str
    name: str
    is_bot: bool
    bot_name: str | None = None
    token: str | None = None
    bot: Bot | None = field(default=None, repr=False)
    bot_errors: int = 0
    draft: bool = False              # submitted with "ready": false — don't resolve the turn for them yet
    error_at: float | None = None    # monotonic time of this turn's last submission with rejected orders
    verified: bool = False           # joined with the key of a registered name
    nudge_due: float | None = None   # house bot: monotonic time of its next reactive negotiation
    nudge_first: float | None = None
    reactive: int = 0                # reactive negotiations this turn
    acted_seq: int = 0               # diplomacy_seq when the house bot last computed its orders
    bot_seed: int | None = None      # house bot: its secret seed (to recreate it if its state can't be pickled)
    bot_blob: bytes | None = field(default=None, repr=False)  # house bot: pickled state at the last checkpoint
    bot_pickle_failed: bool = False
    agent: dict | None = None        # remote player: the agent manifest given at join (provenance.py)
    seat_name: str | None = None     # track game: the neutral name every live view shows ("Player 3");
                                     # ``name`` is the real one (ratings, uniqueness, registered names)

    def snapshot(self) -> dict:
        """Plain data for a checkpoint (no bot object, no monotonic times)."""
        return {"pid": self.pid, "name": self.name, "is_bot": self.is_bot, "bot_name": self.bot_name,
                "token": self.token, "bot_errors": self.bot_errors, "draft": self.draft,
                "verified": self.verified, "reactive": self.reactive, "acted_seq": self.acted_seq,
                "bot_seed": self.bot_seed, "bot_blob": self.bot_blob,
                "nudge_pending": self.nudge_due is not None, "agent": self.agent, "seat_name": self.seat_name}


class GameSession:
    """One live game. All public methods are thread-safe."""

    def __init__(self, manager: "GameManager", game_id: str, opts: dict):
        self._init_runtime(manager, game_id, opts)
        self.created = time.time()
        self.game = Game(GameConfig(seed=opts["seed"], max_turns=opts["max_turns"], game_id=game_id,
                                    name=self.name, max_players=opts["max_players"],
                                    fog=bool(opts.get("fog", False))))
        self.frames: FrameStore | None = FrameStore()  # zlib-compressed, freed once saved to disk
        self.actions = ActionLog()  # what each seat did per turn (replay key "actions")
        with self.cond:
            if self._anon:
                # every seat exists in the engine from the start under its neutral name, so nothing the
                # engine shows (views, events, city names, frames) can carry a real name; joins bind to them
                for k in range(self.max_players):
                    self.game.add_player(seat_name(k))
                self.game._stats = None
            for b in opts["bots"]:
                self._add_bot(b)

    def _init_runtime(self, manager: "GameManager", game_id: str, opts: dict) -> None:
        """Everything that is not part of a checkpoint (locks, thread, caches, timers)."""
        self.manager = manager
        self.game_id = game_id
        self.opts = opts
        self.name = opts["name"] or f"Game {game_id}"
        self.track_id: str | None = opts.get("track")
        self._anon = bool(self.track_id)  # track game: anonymous seats while live (docs/DESIGN.md §12)
        self._created_mono = time.monotonic()
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.seats: dict[str, Seat] = {}
        self.saved = False
        self._fin_lock = threading.Lock()   # one _finalize at a time
        self._fin_done = False             # rated (if rated), replay saved, checkpoint dropped
        self._rating_done = False
        self._fin_due: float | None = None  # monotonic time of the next finalize retry (after a failure)
        self._fin_attempts = 0
        self.error: str | None = None
        self.version = 0
        self.creator_token: str | None = None   # returned by POST /api/games; may start the lobby
        self.closed = False                     # an unstarted lobby closed by the manager (too old)
        self.finished_mono: float | None = None
        self._spec_cache: tuple[int, bytes, bytes] = (-1, b"", b"")
        self._player_cache: dict[str, tuple[int, bytes]] = {}
        self._turn_started = 0.0
        self._deadline_mono: float | None = None
        self._bots_done = True
        self._public_dip = 0          # bumped when public diplomacy happens mid-turn (pushes an SSE frame)
        # synchronous turn mode (docs/DESIGN.md §13.6): N negotiation rounds, then an orders phase
        self._sync = bool(opts.get("sync"))
        self._rounds = int(opts.get("negotiation_rounds") or 0) if self._sync else 0
        self._phase_index = 0          # 0..rounds-1: negotiation round index+1; == rounds: orders phase
        self._phase_id = 0             # bumped whenever a phase opens (clients wait on it)
        self._done: set[str] = set()   # remote seats that ended the current negotiation round
        self._queued: dict[str, list] = {}       # pid -> [[action, t], ...] queued this round
        self._bots_queued: set[str] = set()      # house bots whose batch for this round is queued
        self._sync_results: dict[str, list] = {}  # pid -> [{"round", "results"}] of this turn's barriers
        self._neg_spent = 0.0          # seconds of house-bot negotiate() this turn (NEGOTIATION_BUDGET)
        self._thread = threading.Thread(target=self._run, name=f"game-{game_id}", daemon=True)
        # checkpoints (see checkpoint())
        self._ckpt_io = threading.Lock()   # serialises checkpoint writes; taken before (never inside) self.lock
        self._ckpt_dirty = True            # state changed since the last checkpoint
        self._ckpt_force = True            # write at once (a turn resolved), ignoring the throttle
        self._ckpt_last = 0.0              # monotonic time of the last checkpoint snapshot
        self._ckpt_pending: list[tuple[bytes, bytes | None]] = []  # frames not yet appended to disk
        self._ckpt_frames_size = 0         # bytes of live/<id>.frames known to be good
        self._ckpt_disabled = False        # no more writes (finished and saved, closed, or shut down)
        self._ckpt_errors = 0

    # ------------------------------------------------------------ properties
    @property
    def status(self) -> str:
        return self.game.status

    @property
    def max_players(self) -> int:
        return self.opts["max_players"]

    def launch(self) -> None:
        """Start the worker thread (and auto-start if the lobby is already full)."""
        with self.cond:
            if len(self.seats) >= self.max_players and self.status == "lobby":
                self._start()
        self._thread.start()

    def _touch(self) -> None:
        self.version += 1
        self._ckpt_dirty = True
        self.cond.notify_all()

    def _check_stopping(self) -> None:
        """Mutations are refused once the server is shutting down (under the
        lock), so nothing is acknowledged after the final checkpoint."""
        if self.manager.stopping:
            raise ApiError(503, "server is restarting; retry in a few seconds",
                           turn=self.game.turn, status=self.game.status)

    # ------------------------------------------------------------ seats
    def _unique_bot_name(self, bot_name: str) -> str:
        taken = {s.name.lower() for s in self.seats.values()}
        if bot_name.lower() not in taken:
            return bot_name
        k = 2
        while f"{bot_name}#{k}".lower() in taken:
            k += 1
        return f"{bot_name}#{k}"

    def _add_bot(self, bot_name: str) -> Seat:
        # A secret per-bot seed: deriving it from the published game seed let players re-run a house
        # bot on its (reconstructible) view and predict its exact orders before submitting their own.
        seed = secrets.randbits(31)
        bot, actual = make_bot(bot_name, seed)
        pid = self._add_player(self._unique_bot_name(actual))
        seat = Seat(pid, self.game.player(pid).name, True, actual, None, bot, bot_seed=seed)
        self.seats[pid] = seat
        self._touch()
        return seat

    def _add_player(self, name: str) -> str:
        pid = self.game.add_player(name)
        # Defensive: the engine caches derived stats; make sure a lobby view built after a join
        # includes the new player (a stale cache made spectator views raise KeyError).
        if getattr(self.game, "_stats", None) is not None:
            self.game._stats = None
        return pid

    def join(self, name: str, key: str | None = None, agent: dict | None = None) -> Seat:
        """Seat a remote player. ``agent``: an already validated manifest
        (:func:`parse_agent`), stored with the seat and shown in the summary."""
        name = validate_player_name(name)
        if self._anon:
            return self._join_track(name, key, agent)
        verified = self.manager.check_name(name, key)
        with self.cond:
            if self.closed:
                raise ApiError(409, f"lobby {self.game_id} was closed")
            self._check_stopping()
            if self.status != "lobby":
                raise ApiError(409, f"game {self.game_id} has already started" if self.status == "running"
                               else f"game {self.game_id} is finished")
            if len(self.seats) >= self.max_players:
                raise ApiError(409, f"game {self.game_id} is full")
            if any(s.name.lower() == name.lower() for s in self.seats.values()):
                raise ApiError(409, f"name {name!r} is already taken in this game")
            pid = self._add_player(name)
            seat = Seat(pid, name, False, None, secrets.token_urlsafe(24), verified=verified, agent=agent)
            self.seats[pid] = seat
            self.manager._register_token(seat.token, self.game_id, pid)
            self._touch()
            if len(self.seats) >= self.max_players:
                self._start()
            return seat

    def _join_track(self, name: str, key: str | None, agent: dict | None) -> Seat:
        """Seat a remote player in a track game: the manifest must satisfy the
        track, and the seat is the operator's fixed one for ``name`` (``seats``)
        or the first free one. Nothing in the answer, errors included, tells
        who else is seated."""
        if SEAT_NAME.match(name):
            raise ApiError(400, "names of the form 'Player N' are reserved for the anonymous seats of track games; "
                                "join under your own name (it stays hidden until the game ends)")
        track = get_track(self.track_id)
        if track is None:
            raise ApiError(409, f"track {self.track_id} is not defined on this server any more")
        why = track.check_agent(agent)
        if why:
            raise ApiError(400, why)
        verified = self.manager.check_name(name, key)
        with self.cond:
            if self.closed:
                raise ApiError(409, f"lobby {self.game_id} was closed")
            self._check_stopping()
            if self.status != "lobby":
                raise ApiError(409, f"game {self.game_id} has already started" if self.status == "running"
                               else f"game {self.game_id} is finished")
            if len(self.seats) >= self.max_players:
                raise ApiError(409, f"game {self.game_id} is full")
            low = name.lower()
            fixed = self.opts.get("seats")
            if fixed:
                idx = next((i for i, n in enumerate(fixed) if n.lower() == low), None)
            else:
                idx = next(i for i, p in enumerate(self.game.players) if p.id not in self.seats)
            if idx is None or any(s.name.lower() == low for s in self.seats.values()):
                raise ApiError(409, f"no open seat for that name in track game {self.game_id} (seats are anonymous: "
                                    "the server does not say whether a name is seated or on the operator's seat "
                                    "list); join under another name or ask the operator")
            p = self.game.players[idx]
            seat = Seat(p.id, name, False, None, secrets.token_urlsafe(24), verified=verified, agent=agent,
                        seat_name=p.name)
            self.seats[p.id] = seat
            self.seats = {q.id: self.seats[q.id] for q in self.game.players if q.id in self.seats}  # seat order
            self.manager._register_token(seat.token, self.game_id, p.id)
            self._touch()
            if len(self.seats) >= self.max_players:
                self._start()
            return seat

    def pool(self) -> str:
        """The rating pool of this game: its track, else ``fog`` or ``standard``."""
        return self.track_id or ("fog" if self.opts.get("fog") else "standard")

    # ------------------------------------------------------------ lifecycle
    def start(self, authorized: bool = True) -> bool:
        """Start the game now (``POST /start``). Returns False if it had
        already started (idempotent), raises ApiError if it can't start.
        Once a remote player is seated, only a seated player or the creator
        (``authorized``) may start it: otherwise anyone could start somebody
        else's lobby early and shut other players out."""
        with self.cond:
            if self.status != "lobby":
                if self.status == "finished":
                    raise ApiError(409, "game is finished")
                return False
            if self.closed:
                raise ApiError(409, f"lobby {self.game_id} was closed")
            self._check_stopping()
            if not authorized and any(not s.is_bot for s in self.seats.values()):
                raise ApiError(403, "only a seated player (their token) or the game's creator (the creator_token "
                                    "from POST /api/games) can start a lobby that remote players have joined")
            self._start(explicit=True)
            return True

    def _start(self, explicit: bool = False) -> None:
        if self.opts["fill_with_bots"]:
            pool = [b for b in DEFAULT_FILL_BOTS if bot_available(b)] or ["idle"]
            k = 0
            while len(self.seats) < self.max_players:
                self._add_bot(pool[k % len(pool)])
                k += 1
        need = max(1, self.opts["min_players"]) if explicit or not self.opts["fill_with_bots"] else 1
        if len(self.seats) < need:
            raise ApiError(409, f"need at least {need} players to start (have {len(self.seats)})")
        try:
            self.game.start()
        except RuntimeError as e:
            raise ApiError(409, str(e)) from None
        log.info("game %s started with %d players", self.game_id, len(self.seats))
        self._begin_turn()
        self._record_frame()

    def _set_deadline(self, now: float) -> None:
        """A fresh limit of ``turn_timeout`` seconds from ``now`` (per turn; per phase in sync games)."""
        tt = self.opts["turn_timeout"]
        if tt > 0:
            self._deadline_mono = now + tt
            self.game.deadline = round(time.time() + tt, 3)
        else:
            self._deadline_mono = None
            self.game.deadline = None

    def _begin_turn(self) -> None:
        now = time.monotonic()
        self._turn_started = now
        self._set_deadline(now)
        alive = set(self.game.alive_players())
        self._bots_done = not any(s.is_bot and s.pid in alive for s in self.seats.values())
        for s in self.seats.values():
            s.draft, s.error_at = False, None
            s.nudge_due = s.nudge_first = None
            s.reactive = 0
        if self._sync:
            self._sync_results = {}
            self._neg_spent = 0.0
            self._open_phase(0)
        self._touch()

    # ------------------------------------------------------------ synchronous turns (§13.6)
    def _negotiating(self) -> bool:
        """A synchronous game in one of its negotiation rounds (under the lock)."""
        return self._sync and self._phase_index < self._rounds

    def _open_phase(self, index: int) -> None:
        """Open negotiation round ``index`` (0-based) or, at ``index ==
        rounds``, the orders phase: fresh limit, nobody done, nothing queued."""
        now = time.monotonic()
        self._phase_index = index
        self._phase_id += 1
        self._done = set()
        self._queued = {}
        self._bots_queued = set()
        self._set_deadline(now)
        alive = set(self.game.alive_players())
        if index < self._rounds:
            self._bots_done = not any(s.is_bot and s.pid in alive and s.bot is not None
                                      and _negotiator(s.bot) is not None for s in self.seats.values())
        else:
            self._bots_done = not any(s.is_bot and s.pid in alive for s in self.seats.values())
        self._touch()

    def _sync_current(self, turn: int, phase_id: int) -> bool:
        return self.game.status == "running" and self.game.turn == turn and self._phase_id == phase_id

    def _round_wait(self) -> float:
        """Seconds until the current negotiation round's barrier (<= 0: now).
        House bots are not waited for here (``_bots_done``)."""
        if all(s.pid in self._done for s in self._living_remote()):
            return 0.0
        if self._deadline_mono is not None:
            return self._deadline_mono - time.monotonic()
        return math.inf

    def _barrier_order(self) -> list[str]:
        """Living players in seat order rotated by turn + round index, as
        :func:`agentciv.tournament.run_game` negotiates."""
        alive = self.game.alive_players()
        if not alive:
            return []
        k = (self.game.turn + self._phase_index) % len(alive)
        return alive[k:] + alive[:k]

    def _sync_barrier(self) -> None:
        """End the current negotiation round (under the lock): apply every
        queued batch in the rotating order, record the results for their
        seats and the action log, then open the next phase."""
        g = self.game
        rnd = self._phase_index + 1
        clock = self._turn_clock()
        for s in self._living_remote():
            if s.pid not in self._done:  # hit the limit: treated as done (its queued actions still apply)
                self.actions.phase_missed(g.turn, s.pid, "negotiate", round_=rnd)
        order = self._barrier_order()
        seq0 = g.diplomacy_seq
        for pid in order:
            batch = self._queued.get(pid)
            if not batch:
                continue
            actions = [a for a, _ in batch]
            results = g.diplomacy(pid, actions)
            for r in results:
                i = r.get("index", -1)
                if not r.get("ok") and isinstance(i, int) and 0 <= i < len(actions):
                    r.update({k: v for k, v in _action_hint(actions[i]).items() if k not in r})
            self.actions.diplomacy(g.turn, pid, actions, results, clock, ts=[t for _, t in batch], round_=rnd)
            shown = []
            for r in results:
                i = r.get("index", -1)
                item = dict(r)
                if isinstance(i, int) and 0 <= i < len(actions):
                    item["action"] = actions[i]
                shown.append(item)
            self._sync_results.setdefault(pid, []).append({"round": rnd, "results": shown})
        self.actions.barrier(g.turn, rnd, order, clock)
        if g.diplomacy_seq != seq0 and any(ev.get("_vis") is None for ev in self._dip_events(seq0)):
            self._public_dip += 1
        self._open_phase(self._phase_index + 1)

    def _phase_view(self, pid: str | None = None) -> dict:
        """The ``phase`` object of views, summaries and long-poll answers.
        Other seats' queued actions are never shown, only who is done."""
        g = self.game
        negotiate = self._phase_index < self._rounds
        alive = g.alive_players()
        if negotiate:
            done = [p for p in alive if p in self._done or p in self._bots_queued
                    or (self._bots_done and self.seats.get(p) is not None and self.seats[p].is_bot)]
        else:
            done = [p for p in alive if (s := self.seats.get(p)) is not None
                    and ((s.is_bot and self._bots_done) or (not s.is_bot and g.has_submitted(p) and not s.draft))]
        out = {"id": self._phase_id, "kind": "negotiate" if negotiate else "orders",
               "round": self._phase_index + 1 if negotiate else None, "of": self._rounds,
               "deadline": g.deadline, "done": done,
               "waiting": [s.pid for s in self._living_remote() if s.pid not in done]}
        if pid is not None:
            out["you_done"] = pid in done
            out["queued"] = [a for a, _ in self._queued.get(pid, [])]
            out["results"] = list(self._sync_results.get(pid, []))
        return out

    def _sync_bots_negotiate(self, turn: int, phase_id: int) -> None:
        """House bots' batches for the current negotiation round. Each bot
        sees the same (unchanging) state a remote seat sees during the round;
        its batch is queued and applied at the barrier in its seat's turn."""
        with self.cond:
            if not self._sync_current(turn, phase_id):
                return
            work = [(s, _negotiator(s.bot)) for s in self._bot_seats(turn) if s.pid not in self._bots_queued]
        budget = self._negotiation_budget()
        for seat, fn in work:
            actions: list = []
            if fn is not None and self._neg_spent < budget:
                with self.cond:
                    if not self._sync_current(turn, phase_id):
                        return
                    view = self.game.player_view(seat.pid) if self._current(turn, seat) else None
                if view is not None:
                    t0 = time.monotonic()
                    actions = self._bot_negotiate(seat, fn, view)
                    self._neg_spent += time.monotonic() - t0
            with self.cond:
                if not self._sync_current(turn, phase_id):
                    return
                if actions:
                    t = self._turn_clock()
                    self._queued[seat.pid] = [[a, t] for a in actions]
                self._bots_queued.add(seat.pid)
        with self.cond:
            if self._sync_current(turn, phase_id):
                self._bots_done = True
                self._touch()

    def _record_frame(self) -> None:
        if self.frames is not None:
            full, public = self._spectator_pair()
            self.frames.append(full, public)
            if self.manager.live is not None and not self._ckpt_disabled:
                self._ckpt_pending.append(self.frames.last_blobs())
        self._ckpt_force = True  # a turn resolved (or the game started): checkpoint at once

    def _living_remote(self) -> list[Seat]:
        alive = set(self.game.alive_players())
        return [s for s in self.seats.values() if not s.is_bot and s.pid in alive]

    def _turn_delay(self) -> float:
        if self.opts["turn_delay"] is not None:
            return self.opts["turn_delay"]
        if self._living_remote():
            return 0.0
        tt = self.opts["turn_timeout"]
        return min(SPECTATOR_BOT_DELAY, tt) if tt > 0 else SPECTATOR_BOT_DELAY

    def _seconds_until_ready(self) -> float:
        """Seconds until the current turn may resolve (<= 0: now)."""
        now = time.monotonic()
        earliest = self._turn_started + self._turn_delay()
        remote = self._living_remote()
        if all(self.game.has_submitted(s.pid) and not s.draft for s in remote):
            # a player whose orders were just rejected gets a moment to resubmit a fixed list
            grace = [s.error_at + ERROR_GRACE for s in remote if s.error_at is not None]
            if grace:
                earliest = max(earliest, min(max(grace), self._deadline_mono or math.inf))
            return earliest - now
        if self._deadline_mono is not None:
            return max(self._deadline_mono, earliest) - now
        return math.inf

    def _lobby_wait(self) -> float:
        lt = self.opts["lobby_timeout"]
        if lt is None:
            return 1.0
        remaining = self._created_mono + lt - time.monotonic()
        if remaining <= 0:
            try:
                self._start()
            except ApiError:
                pass  # not enough players yet: keep waiting
            return 1.0
        return min(remaining, 1.0)

    def _turn_clock(self) -> float:
        """Seconds since the current turn started (or the server resumed it)."""
        return time.monotonic() - self._turn_started

    def _log_turn_end(self) -> None:
        """Close the action-log entry of the turn about to resolve: which
        living remote seats had not submitted (or were still drafting)."""
        g = self.game
        missed = {}
        for s in self._living_remote():
            if not g.has_submitted(s.pid):
                missed[s.pid] = "no_orders"
            elif s.draft:
                missed[s.pid] = "draft"
        if self._sync:
            for pid, reason in missed.items():
                self.actions.phase_missed(g.turn, pid, "orders", reason=reason)
        self.actions.end_turn(g.turn, "deadline" if missed else "all_ready", missed)

    def _advance(self) -> None:
        self._log_turn_end()
        try:
            self.game.step()
        except Exception as e:  # engine bug: abort the game rather than loop forever
            log.exception("game %s: engine error while resolving turn %s", self.game_id, self.game.turn)
            self.error = f"engine error: {type(e).__name__}: {e}"
            self.game.status = "finished"
        if self.status != "finished":
            self._begin_turn()
        else:
            self.game.deadline = None
            self.finished_mono = time.monotonic()
            self._touch()
            log.info("game %s finished: %s", self.game_id, self.game.result)
        self._record_frame()

    # ------------------------------------------------------------ worker
    def _run(self) -> None:
        while not self.manager.stopping and not self.closed:
            job = None
            if self._ckpt_wait() <= 0:
                self.checkpoint(bots_idle=True)  # this thread runs the house bots, so they are idle now
            with self.cond:
                if self.closed or self.manager.stopping:
                    break
                if self.status == "lobby":
                    wait = self._lobby_wait()
                    if self.status == "lobby":
                        self.cond.wait(max(0.0, min(wait, self._ckpt_wait())))
                    continue
                if self.status == "finished":
                    break
                turn = self.game.turn
                if self._negotiating():
                    # synchronous game, negotiation round: house bots queue their batch, then the
                    # barrier waits for every living remote seat to be done (or the phase limit)
                    if not self._bots_done:
                        job = ("negotiate", not self._living_remote(), self._phase_id)
                    else:
                        wait = self._round_wait()
                        if wait <= 0:
                            self._sync_barrier()
                            continue
                        self.cond.wait(max(0.0, min(wait, 1.0, self._ckpt_wait())))
                        continue
                elif not self._bots_done:
                    job = ("turn", not self._living_remote())
                else:
                    due = self._take_due_nudges()
                    if due:
                        job = ("react", due)
                    else:
                        wait = self._seconds_until_ready()
                        if wait <= 0:
                            # never resolve with a house bot's stale orders: answer pending nudges
                            # (and re-act after executed deals) first, even if not yet due
                            due = self._take_due_nudges(force=True)
                            if not due:
                                self._advance()
                                continue
                            job = ("react", due)
                        else:
                            nxt = min((s.nudge_due for s in self.seats.values() if s.nudge_due is not None),
                                      default=math.inf)
                            self.cond.wait(max(0.0, min(wait, 1.0, nxt - time.monotonic(), self._ckpt_wait())))
                            continue
            # house bots think outside the lock so state requests stay fast; bot-only games share a
            # few compute slots so a pile of them can't starve the API and games with remote players
            if job[0] == "negotiate":
                if job[1]:
                    with self.manager.bot_slots:
                        self._sync_bots_negotiate(turn, job[2])
                else:
                    self._sync_bots_negotiate(turn, job[2])
            elif job[0] == "turn":
                if job[1]:
                    with self.manager.bot_slots:
                        self._house_turn(turn)
                else:
                    self._house_turn(turn)
            else:
                self._react(turn, job[1])
        if self.status == "finished" and not self.manager.stopping:
            self.checkpoint(bots_idle=True)  # the final state, in case rating or saving the replay fails
            self._finalize()
            self.manager._retire(self)

    def _bot_seats(self, turn: int) -> list[Seat]:
        """Living house bots in seat order, rotated by the turn (who negotiates first)."""
        alive = set(self.game.alive_players())
        bots = [s for s in self.seats.values() if s.is_bot and s.pid in alive and s.bot is not None]
        if not bots:
            return bots
        k = turn % len(bots)
        return bots[k:] + bots[:k]

    def _current(self, turn: int, seat: Seat) -> bool:
        """Still the same running turn and the seat's player alive (call under the lock)."""
        p = self.game.player(seat.pid)
        return self.game.status == "running" and self.game.turn == turn and bool(p and p.alive)

    def _house_turn(self, turn: int) -> None:
        """A turn's work for the house bots: NEGOTIATION_ROUNDS rounds of
        ``negotiate`` (each bot, in rotating seat order, sees a fresh view and
        its actions apply at once), then ``act`` on fresh views. In a
        synchronous game the bots negotiated in the rounds (see
        :meth:`_sync_bots_negotiate`); this is its orders phase: ``act`` only."""
        with self.cond:
            order = self._bot_seats(turn)
            negotiators = [] if self._sync else [(s, fn) for s in order if (fn := _negotiator(s.bot)) is not None]
        t0 = time.monotonic()
        budget = self._negotiation_budget()
        for _ in range(NEGOTIATION_ROUNDS if negotiators else 0):
            for seat, fn in negotiators:
                if time.monotonic() - t0 > budget:
                    break
                self._negotiate_once(turn, seat, fn, nudge=False)
            else:
                continue
            log.debug("game %s turn %s: house-bot negotiation budget used up", self.game_id, turn)
            break
        with self.cond:
            jobs = [(s, self.game.player_view(s.pid), self.game.diplomacy_seq)
                    for s in self._bot_seats(turn) if self._current(turn, s)]
        results = [(seat, self._bot_orders(seat, view), seq) for seat, view, seq in jobs]
        with self.cond:
            if self.game.turn == turn and self.status == "running":
                for seat, orders, seq in results:
                    self._submit_bot(seat, orders, seq)
            self._bots_done = True
            self._touch()

    def _submit_bot(self, seat: Seat, orders: list, seq: int) -> None:
        errs = self.game.submit_orders(seat.pid, orders)
        self.actions.orders(self.game.turn, seat.pid, orders, errs, True, self._turn_clock())
        seat.acted_seq = seq
        if errs and log.isEnabledFor(logging.DEBUG):
            log.debug("game %s %s (%s) order errors: %s", self.game_id, seat.pid, seat.bot_name, errs[:5])

    def _negotiation_budget(self) -> float:
        tt = self.opts["turn_timeout"]
        return NEGOTIATION_BUDGET if tt <= 0 else max(0.5, min(NEGOTIATION_BUDGET, 0.25 * tt))

    def _negotiate_once(self, turn: int, seat: Seat, fn, nudge: bool) -> None:
        """One ``negotiate`` call of a house bot on a fresh view (outside the lock)."""
        with self.cond:
            if not self._current(turn, seat):
                return
            view = self.game.player_view(seat.pid)
        actions = self._bot_negotiate(seat, fn, view)
        if actions:
            with self.cond:
                if self._current(turn, seat):
                    self._apply_diplomacy(seat.pid, actions, nudge=nudge)

    def _bot_negotiate(self, seat: Seat, fn, view: dict) -> list:
        t0 = time.monotonic()
        try:
            actions = fn(view)
            if actions is None:
                return []
            if isinstance(actions, dict):
                actions = actions.get("actions", [actions]) if "actions" in actions else [actions]
            if not isinstance(actions, list):
                raise TypeError(f"negotiate() returned {type(actions).__name__}, expected list")
        except Exception:
            seat.bot_errors += 1
            if seat.bot_errors <= 3:
                log.exception("game %s: house bot %s (%s) failed to negotiate on turn %s; no actions",
                              self.game_id, seat.pid, seat.bot_name, view.get("turn"))
            return []
        dt = time.monotonic() - t0
        if dt > BOT_SLOW_WARN:
            log.warning("game %s: house bot %s took %.1fs to negotiate", self.game_id, seat.bot_name, dt)
        return actions

    def _take_due_nudges(self, force: bool = False) -> list[tuple[Seat, bool]]:
        """House bots whose reactive work is due now, as ``(seat, negotiate?)``
        (clears their timers; under the lock). ``REACTIVE_PER_TURN`` caps only
        the ``negotiate`` calls, never the re-``act`` after an executed deal.
        ``force`` (the turn is about to resolve): every pending nudge, due or
        not, plus any bot with a deal executed since it last acted."""
        now = time.monotonic()
        due = []
        for s in self.seats.values():
            if s.nudge_due is not None and (force or s.nudge_due <= now):
                s.nudge_due = s.nudge_first = None
                if s.bot is None:
                    continue
                talk = s.reactive < REACTIVE_PER_TURN and _negotiator(s.bot) is not None
                if talk:
                    s.reactive += 1
                due.append((s, talk))
        if force:
            listed = {s.pid for s, _ in due}
            turn = self.game.turn
            due += [(s, False) for s in self._bot_seats(turn)
                    if s.pid not in listed and self._current(turn, s) and self._deal_since_act(s)]
        return due

    def _deal_since_act(self, seat: Seat) -> bool:
        """A deal ``seat`` is party to executed after it last computed its orders (under the lock)."""
        return any(ev["type"] == "deal_executed" and seat.pid in (ev.get("from"), ev.get("to"))
                   for ev in self._dip_events(seat.acted_seq))

    def _react(self, turn: int, seats: list[tuple[Seat, bool]]) -> None:
        """Mid-turn: house bots answer what was addressed to them (negotiate,
        if still within their per-turn budget), and recompute their orders if a
        deal they are party to executed since they last acted (their resources
        or land changed)."""
        for seat, talk in seats:
            fn = _negotiator(seat.bot) if talk else None
            if fn is not None:
                self._negotiate_once(turn, seat, fn, nudge=True)
            with self.cond:
                if not self._current(turn, seat) or not self._bots_done:
                    continue
                if not self._deal_since_act(seat):
                    continue
                view, seq = self.game.player_view(seat.pid), self.game.diplomacy_seq
            orders = self._bot_orders(seat, view)
            with self.cond:
                if self._current(turn, seat):
                    self._submit_bot(seat, orders, seq)
                    self._touch()

    # ------------------------------------------------------------ diplomacy
    def _dip_events(self, since: int) -> list[dict]:
        """Diplomacy events with ``seq > since`` (with their ``_vis``), oldest first."""
        feed = self.game._dip_feed
        out = []
        for ev in reversed(feed):
            if ev["seq"] <= since:
                break
            out.append(ev)
        out.reverse()
        return out

    def _apply_diplomacy(self, pid: str, actions, nudge: bool = True) -> list:
        """Apply diplomacy actions for ``pid`` now (under the lock), wake
        long-pollers, push an SSE frame on public diplomacy and schedule the
        reactive negotiation of house bots something was addressed to."""
        g = self.game
        seq0 = g.diplomacy_seq
        results = g.diplomacy(pid, actions)
        if g.status == "running":
            self.actions.diplomacy(g.turn, pid, actions, results, self._turn_clock())
        if g.diplomacy_seq == seq0:
            return results
        events = self._dip_events(seq0)
        if any(ev.get("_vis") is None for ev in events):
            self._public_dip += 1
        if nudge:
            now = time.monotonic()
            alive = set(g.alive_players())
            for s in self.seats.values():
                if not s.is_bot or s.pid == pid or s.pid not in alive or s.bot is None:
                    continue
                talks = _negotiator(s.bot) is not None
                for ev in events:
                    if ev.get("by") == s.pid or s.pid not in (ev.get("from"), ev.get("to")):
                        continue
                    if talks or ev["type"] == "deal_executed":
                        if s.nudge_first is None:
                            s.nudge_first = now
                        s.nudge_due = min(now + NEGOTIATE_DEBOUNCE, s.nudge_first + NEGOTIATE_DEBOUNCE_MAX)
                        break
        self._touch()
        return results

    def diplomacy(self, pid: str, body) -> dict:
        """``POST /diplomacy``: apply actions immediately (§13.2). In a
        synchronous game they are queued for the round's barrier instead,
        and ``"done": true`` ends the seat's negotiation round (§13.6)."""
        turn = None
        done, phase = False, None
        if self._sync and isinstance(body, dict):
            done = _bool(body, "done", False)
            if body.get("phase") is not None:
                phase = _number(body, "phase", None, 0, 10 ** 12, integer=True)
        if isinstance(body, list):
            actions = body
        elif isinstance(body, dict) and "actions" in body:
            actions, turn = body["actions"], body.get("turn")
        elif isinstance(body, dict) and "type" in body:
            actions = [body]
        elif self._sync and isinstance(body, dict) and "done" in body:
            actions, turn = [], body.get("turn")
        else:
            raise ApiError(400, "body must be {\"actions\": [...]} (e.g. {\"actions\": [{\"type\": \"propose\", "
                                "\"to\": \"p2\", \"give\": {\"wood\": 60}, \"get\": {\"gold\": 45}}]})")
        if not isinstance(actions, list):
            raise ApiError(400, "'actions' must be a list of action objects")
        if len(actions) > C.MAX_ACTIONS_PER_CALL:  # refused before taking the game lock
            raise ApiError(400, f"too many actions in one call ({len(actions)}; max {C.MAX_ACTIONS_PER_CALL})")
        if turn is not None:
            turn = _number({"turn": turn}, "turn", None, -1, 10 ** 9, integer=True)
        with self.cond:
            g = self.game
            if g.status == "lobby":
                raise ApiError(409, "game has not started yet", turn=g.turn, status=g.status)
            if g.status == "finished":
                raise ApiError(409, "game is finished", turn=g.turn, status=g.status)
            if turn is not None and turn != g.turn:
                raise ApiError(409, f"stale turn {turn}: the current turn is {g.turn}", turn=g.turn,
                               status=g.status)
            p = g.player(pid)
            if p is None or not p.alive:
                raise ApiError(409, "you have been eliminated", turn=g.turn, status=g.status)
            self._check_stopping()
            if self._sync:
                return self._queue_diplomacy(pid, actions, done, phase)
            results = self._apply_diplomacy(pid, actions)
            for r in results:  # malformed actions get a correctly shaped example (like rejected orders)
                i = r.get("index", -1)
                if not r.get("ok") and isinstance(i, int) and 0 <= i < len(actions):
                    r.update({k: v for k, v in _action_hint(actions[i]).items() if k not in r})
            return {"results": results, "ok": all(r.get("ok") for r in results), "seq": g.diplomacy_seq,
                    "turn": g.turn, "deadline": g.deadline}

    def _queue_diplomacy(self, pid: str, actions: list, done: bool, phase: int | None) -> dict:
        """Synchronous game: queue ``actions`` for this round's barrier (under
        the lock). Malformed actions are refused at once; everything else is
        checked when it is applied. ``done`` ends the seat's round."""
        g = self.game
        if phase is not None and phase != self._phase_id:
            raise ApiError(409, f"stale phase {phase}: the current phase is {self._phase_id} (read your state)",
                           turn=g.turn, status=g.status, phase=self._phase_view(pid))
        if not self._negotiating():
            raise ApiError(409, "diplomacy is closed: this synchronous game is in its orders phase; submit your "
                                "orders (POST /orders). Negotiation reopens next turn.",
                           turn=g.turn, status=g.status, phase=self._phase_view(pid))
        rnd = self._phase_index + 1
        if pid in self._done:
            if actions:
                raise ApiError(409, f"you already ended negotiation round {rnd}; wait for the next phase "
                                    f"(GET /wait?since_phase={self._phase_id})",
                               turn=g.turn, status=g.status, phase=self._phase_view(pid))
        queue = self._queued.setdefault(pid, [])
        results = []
        t = self._turn_clock()
        for i, action in enumerate(actions):
            try:
                parse_action(action, g.width, g.height)
            except DealError as e:
                r = {"index": i, "ok": False, "error": str(e)}
                r.update({k: v for k, v in _action_hint(action).items() if k not in r})
                results.append(r)
                continue
            if len(queue) >= C.MAX_ACTIONS_PER_CALL:
                results.append({"index": i, "ok": False,
                                "error": f"at most {C.MAX_ACTIONS_PER_CALL} actions can be queued per round"})
                continue
            queue.append([action, t])
            results.append({"index": i, "ok": True, "status": "queued"})
        if not queue:
            del self._queued[pid]
        if done:
            self._done.add(pid)
        self._touch()
        return {"results": results, "ok": all(r.get("ok") for r in results), "queued": len(queue),
                "done": pid in self._done, "seq": g.diplomacy_seq, "turn": g.turn, "deadline": g.deadline,
                "phase": self._phase_view(pid)}

    def inbox(self, pid: str, since: int | None, timeout: float, turn: int | None = None) -> dict:
        """``GET /inbox``: diplomacy events visible to ``pid`` with ``seq >
        since`` (not its own). Long-polls until there is one, the turn or
        status changes, or ``timeout`` passes. ``turn``: the turn the caller
        believes is current (returns at once if it isn't any more)."""
        since = 0 if since is None else since
        end = time.monotonic() + timeout
        with self.cond:
            g = self.game
            # a ``since`` beyond the current seq (the server restarted from a checkpoint taken before
            # events the caller had already seen): deliver whatever happens from now on
            since = min(since, g.diplomacy_seq) if isinstance(since, int) else since
            turn0, status0 = (g.turn if turn is None else turn), g.status
            phase0 = self._phase_id
            timed_out = False
            while True:
                box = g.inbox(pid, since)
                if (box["items"] or g.status != status0 or g.turn != turn0 or g.status == "finished"
                        or self.manager.stopping or self.closed or self._phase_id != phase0):
                    break
                remaining = end - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                self.cond.wait(remaining)
            out = {"seq": box["seq"], "items": box["items"], "turn": g.turn, "status": g.status,
                   "deadline": g.deadline, "timed_out": timed_out}
            if self._sync and g.status == "running":
                out["phase"] = self._phase_view(pid)
            return out

    def _bot_orders(self, seat: Seat, view: dict) -> list:
        t0 = time.monotonic()
        try:
            orders = seat.bot.act(view)
            if not isinstance(orders, list):
                raise TypeError(f"act() returned {type(orders).__name__}, expected list")
        except Exception:
            seat.bot_errors += 1
            if seat.bot_errors <= 3:
                log.exception("game %s: house bot %s (%s) failed on turn %s; treating as no orders",
                              self.game_id, seat.pid, seat.bot_name, view.get("turn"))
            return []
        dt = time.monotonic() - t0
        if dt > BOT_SLOW_WARN:
            log.warning("game %s: house bot %s took %.1fs", self.game_id, seat.bot_name, dt)
        return orders

    def _finalize(self) -> bool:
        """Rate the game, save its replay, then drop the live checkpoint.

        Each step runs once: the rating is recorded first (idempotent per game
        id, see :meth:`Storage.record_result`), the replay only once the rating
        is on disk (so a saved replay implies a durable rating), and the
        checkpoint is deleted only when both are. On a failure the checkpoint
        is kept and the manager retries from ``_sweep`` (and a restart resumes
        the finished game and finalizes it again). Returns True when done."""
        if not self._fin_lock.acquire(blocking=False):
            return False  # another thread is finalizing this session right now
        try:
            with self.cond:
                if self._fin_done:
                    return True
                ranked = self._rated_entries()
                pool = self.pool()
                for seat in self.seats.values():
                    seat.bot = None  # free the house bots (and their caches) now
            ok = True
            if ranked and not self._rating_done:
                try:
                    self.manager.storage.record_result(self.game_id, [n for n, _ in ranked],
                                                       [r for _, r in ranked], pool=pool)
                    self._rating_done = True
                except Exception:
                    log.exception("game %s: could not update the leaderboard (the live checkpoint is kept; "
                                  "retried later)", self.game_id)
                    ok = False
            if ok and not self.saved:
                with self.cond:
                    frames = self.frames.all_full() if self.frames is not None else []
                    summary = self.summary()  # carries "rating" (finished game), as the live replay does
                    result = self.game.result
                    actions = self.actions.to_bytes()
                try:
                    self.manager.storage.save_replay(self.game_id, summary, result, frames, actions)
                    with self.cond:
                        self.saved = True
                        self.frames = None  # served from disk from now on
                except Exception:
                    log.exception("game %s: could not save the replay (the live checkpoint is kept; "
                                  "retried later)", self.game_id)
                    ok = False
            if ok:
                self.discard_checkpoint()
                self._fin_done = True
                self._fin_due = None
            else:
                self._fin_attempts += 1
                self._fin_due = time.monotonic() + min(FINALIZE_RETRY_MAX,
                                                       FINALIZE_RETRY * 2 ** (self._fin_attempts - 1))
            return ok
        finally:
            self._fin_lock.release()

    def _rated_entries(self) -> list[tuple[str, int]] | None:
        """(rating name, rank) per seat in placement order, or None when the
        game doesn't count. Ranks come from :meth:`Game.placement_ranks`
        (players tied on score share a rank; a winner by a victory condition
        ranks alone). A name may appear more than once (the same house bot in
        several seats); every seat is rated."""
        result = self.game.result
        if not self.opts["rated"] or not result or self.error is not None or len(self.seats) < 2:
            return None
        if not self.manager.open_ratings and all(s.is_bot for s in self.seats.values()):
            return None  # bot-only games don't move the leaderboard
        out: list[tuple[str, int]] = []
        for pid, rank in zip(result.get("placements", []), self.game.placement_ranks()):
            seat = self.seats.get(pid)
            if seat is not None:
                out.append((seat.bot_name if seat.is_bot else seat.name, rank))
        return out

    # ------------------------------------------------------------ views
    def _spectator_pair(self) -> tuple[bytes, bytes]:
        """(full, public) spectator view bytes for the current version. The
        public view is what live spectators get: no private messages, trade
        offers, treaty proposals or private events until the game is over
        (anyone can drop their token and spectate). Equal once finished."""
        if self._spec_cache[0] != self.version:
            g = self.game
            full = _dumps(g.spectator_view(full=True))
            public = full
            if g.status != "finished":
                public = _dumps(g.spectator_view())  # the engine's public spectator view
                if public == full:
                    public = full
            self._spec_cache = (self.version, full, public)
        return self._spec_cache[1], self._spec_cache[2]

    def _spectator_bytes(self, full: bool = False) -> bytes:
        """Public view by default; operators may select the full cached view."""
        return self._spectator_pair()[0 if full else 1]

    def state_bytes(self, pid: str | None = None, *, full: bool = False) -> bytes:
        with self.cond:
            if pid is None:
                return self._spectator_bytes(full=full)
            cached = self._player_cache.get(pid)
            if cached is None or cached[0] != self.version:
                view = self.game.player_view(pid)
                if self._sync and self.game.status == "running":
                    view["phase"] = self._phase_view(pid)
                cached = (self.version, _dumps(view))
                self._player_cache[pid] = cached
            return cached[1]

    def replay_bytes(self, compact: bool = False, lo: int | None = None, hi: int | None = None,
                     *, full: bool = False) -> bytes:
        """The replay (docs/DESIGN.md §12); ``lo``/``hi`` select an inclusive
        frame range, ``compact`` the lighter format of :mod:`.replay`."""
        with self.cond:
            fs = self.frames
            if fs is not None:
                summary, result = self.summary(operator=full), self.game.result
                public = not full and self.status != "finished"
                if compact:  # the GUI's format: frames only, no action log
                    return envelope(self.game_id, summary, result, fs.compact(lo, hi, public=public),
                                    compact=True, static=fs.static, total=len(fs), lo=max(0, lo or 0))
                # the action log holds every seat's orders and private diplomacy: operator-only while live
                if lo is None and hi is None:
                    return envelope(self.game_id, summary, result, fs.full(public=public),
                                    actions=None if public else self.actions.to_bytes())
                a = max(0, lo or 0)
                b = len(fs) - 1 if hi is None else min(len(fs) - 1, hi)
                return envelope(self.game_id, summary, result, fs.full(lo, hi, public=public), total=len(fs),
                                lo=a, actions=None if public else self.actions.to_bytes(a, b))
        return self.manager.archived_replay_bytes(self.game_id, compact, lo, hi)

    def summary(self, operator: bool = False) -> dict:
        """The game summary. Track games (anonymous seats) show each seat's
        neutral name, no manifests and no seed while live, except to the
        ``operator``; once finished, ``players[].name`` is the real name and
        ``players[].seat_name`` the neutral one the frames show."""
        with self.cond:
            g = self.game
            reveal = not self._anon or operator or g.status == "finished"
            players = []
            for pid, s in self.seats.items():
                p = g.player(pid)
                entry = {"id": pid, "name": s.name if reveal else (s.seat_name or pid), "is_bot": s.is_bot,
                         "alive": bool(p and p.alive),
                         "submitted": bool(g.status == "running" and g.has_submitted(pid))}
                if self._anon:
                    entry["seat_name"] = s.seat_name
                if s.is_bot:
                    entry["bot"] = s.bot_name
                if s.agent and reveal:
                    entry["agent"] = dict(s.agent)
                players.append(entry)
            out = {
                "game_id": self.game_id,
                "name": self.name,
                "status": g.status,
                "turn": g.turn,
                "players": players,
                "max_players": self.max_players,
                "min_players": self.opts["min_players"],
                "created": round(self.created, 3),
                "turn_timeout": self.opts["turn_timeout"],
                "max_turns": self.opts["max_turns"],
                "seed": self.opts["seed"] if reveal else None,  # a fog map is derivable from the seed
                "fill_with_bots": self.opts["fill_with_bots"],
                "lobby_timeout": self.opts["lobby_timeout"],
                "quickmatch": self.opts["quickmatch"],
                "rated": self.opts["rated"],
                "unrated_reason": self.opts.get("unrated_reason"),
                "fog": bool(self.opts.get("fog", False)),
                "rules_sha256": self.opts.get("rules_sha256"),
                "sync": self._sync,
                "negotiation_rounds": self._rounds if self._sync else None,
                "track": self.track_id,
                "deadline": g.deadline,
                "result": dict(g.result) if g.result else None,
                "frames": len(self.frames) if self.frames is not None else g.turn + 1,
            }
            if self._anon:
                out["seats_fixed"] = bool(self.opts.get("seats"))  # the operator assigned the seats
            if self._sync and g.status == "running":
                out["phase"] = self._phase_view()
            if g.status == "finished":
                # the same summary whether the replay is served from memory or from the saved file
                ranked = self._rated_entries()
                out["rating"] = ({"pool": self.pool(),
                                  "entries": [[n, r] for n, r in ranked]} if ranked else None)
            if self.error:
                out["error"] = self.error
            return out

    # ------------------------------------------------------------ player actions
    def submit(self, pid: str, body) -> dict:
        ready = True
        if isinstance(body, list):
            orders, turn = body, None
        elif isinstance(body, dict):
            orders, turn = body.get("orders"), body.get("turn")
            ready = _bool(body, "ready", True)
        else:
            raise ApiError(400, "body must be {\"turn\": T, \"orders\": [...]}")
        if orders is None:
            raise ApiError(400, "missing 'orders' (a list of order objects)")
        if not isinstance(orders, list):
            raise ApiError(400, "'orders' must be a list")
        if turn is not None:
            turn = _number({"turn": turn}, "turn", None, -1, 10 ** 9, integer=True)
        with self.cond:
            g = self.game
            if g.status == "lobby":
                raise ApiError(409, "game has not started yet", turn=g.turn, status=g.status)
            if g.status == "finished":
                raise ApiError(409, "game is finished", turn=g.turn, status=g.status)
            if turn is not None and turn != g.turn:
                raise ApiError(409, f"stale turn {turn}: the current turn is {g.turn}", turn=g.turn,
                               status=g.status)
            p = g.player(pid)
            if p is None or not p.alive:
                raise ApiError(409, "you have been eliminated", turn=g.turn, status=g.status)
            self._check_stopping()
            if self._negotiating():
                raise ApiError(409, f"orders open after the negotiation rounds: this synchronous game is in "
                                    f"negotiation round {self._phase_index + 1} of {self._rounds}. Queue diplomacy "
                                    "(POST /diplomacy) and end the round with {\"done\": true}; then wait "
                                    f"(GET /wait?since_phase={self._phase_id}).",
                               turn=g.turn, status=g.status, phase=self._phase_view(pid))
            errors = g.submit_orders(pid, orders)
            self.actions.orders(g.turn, pid, orders, errors, ready, self._turn_clock())
            seat = self.seats.get(pid)
            if seat is not None:
                seat.draft = not ready
                seat.error_at = time.monotonic() if errors else None
            self._touch()
            for e in errors:  # make every rejection actionable: echo a correctly shaped example
                i = e.get("index", -1)
                if isinstance(i, int) and 0 <= i < len(orders):
                    for k, v in order_hint(orders[i]).items():
                        e.setdefault(k, v)
            bad = sum(1 for e in errors if e.get("index", -1) >= 0)
            accepted = 0 if any(e.get("index", -1) < 0 for e in errors) else len(orders) - bad
            out = {"accepted": max(0, accepted), "errors": errors, "turn": g.turn, "deadline": g.deadline,
                   "ready": ready}
            if errors:
                out["note"] = (f"Rejected orders were dropped; the rest stand. Resubmit the whole corrected list "
                               f"(it replaces this one) — the turn waits up to {ERROR_GRACE:g}s for a fix. "
                               "To hold the turn open longer, submit with \"ready\": false and then "
                               "resubmit with \"ready\": true (the deadline still applies).")
            return out

    def wait(self, since_turn: int | None, timeout: float, since_phase: int | None = None) -> dict:
        """``GET /wait``: until ``turn > since_turn`` (or, in a synchronous
        game, ``since_phase`` given and another phase is open), the game
        finishes or ``timeout`` passes."""
        end = time.monotonic() + timeout
        with self.cond:
            if since_turn is None:
                since_turn = self.game.turn if self.status == "running" else -1
            if not self._sync:
                since_phase = None
            timed_out = False
            while not self.manager.stopping:
                st = self.game.status
                if st == "finished" or (st == "running" and self.game.turn > since_turn):
                    break
                if since_phase is not None and st == "running" and self._phase_id != since_phase:
                    break
                remaining = end - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                self.cond.wait(remaining)
            out = {"turn": self.game.turn, "status": self.game.status, "deadline": self.game.deadline,
                   "timed_out": timed_out}
            if self._sync and self.game.status == "running":
                out["phase"] = self._phase_view()
            return out

    def _stream_key(self) -> tuple:
        # remote players' submissions are part of the key so spectators see "submitted" live
        subs = tuple(pid for pid, s in self.seats.items()
                     if not s.is_bot and self.game.status == "running" and self.game.has_submitted(pid))
        return (self.game.status, self.game.turn, len(self.seats), subs, self._public_dip)

    def next_frame(self, last_key, timeout: float, *, full: bool = False):
        """Block until the spectator-visible state changes (turn, status,
        lobby seats, a remote submission or public diplomacy such as an
        executed deal). Returns ``(key, bytes | None, finished)``; bytes is None
        on timeout."""
        end = time.monotonic() + timeout
        with self.cond:
            while self._stream_key() == last_key and not self.manager.stopping:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return last_key, None, self.status == "finished"
                self.cond.wait(remaining)
            return self._stream_key(), self._spectator_bytes(full=full), self.status == "finished"

    def notify(self) -> None:
        with self.cond:
            self.cond.notify_all()

    # ------------------------------------------------------------ checkpoints (persist.py)
    def _ckpt_wait(self) -> float:
        """Seconds until a checkpoint is due (0 = now, inf = nothing to write)."""
        if self._ckpt_disabled or self.manager.live is None:
            return math.inf
        if self._ckpt_force:
            return 0.0
        if self._ckpt_dirty:
            return max(0.0, self._ckpt_last + CHECKPOINT_INTERVAL - time.monotonic())
        return math.inf

    def _pickle_bots(self) -> None:
        """Refresh each house bot's pickled state (only while no bot is running:
        from the worker thread between jobs, or once it has stopped)."""
        for seat in self.seats.values():
            if not seat.is_bot or seat.bot is None:
                continue
            try:
                seat.bot_blob = pickle.dumps(seat.bot, protocol=pickle.HIGHEST_PROTOCOL)
            except Exception as e:
                seat.bot_blob = None  # restored from bot name + seed instead
                if not seat.bot_pickle_failed:
                    seat.bot_pickle_failed = True
                    log.info("game %s: house bot %s (%s) can't be pickled (%s: %s); a restore recreates it "
                             "from its name and seed", self.game_id, seat.pid, seat.bot_name, type(e).__name__, e)

    def snapshot_state(self, clean: bool = False) -> dict:
        """Plain-data snapshot of the session (under the lock): no locks,
        threads or bot objects (bots travel as pickled blobs), no frames.
        ``clean``: the final checkpoint of an orderly shutdown (nothing lost)."""
        return {
            "clean": clean,
            "format": CHECKPOINT_FORMAT,
            "game_id": self.game_id,
            "name": self.name,
            "opts": dict(self.opts),
            "created": self.created,
            "saved_at": time.time(),
            "creator_token": self.creator_token,
            "error": self.error,
            "version": self.version,
            "game": self.game,
            "seats": [seat.snapshot() for seat in self.seats.values()],
            "actions": self.actions.snapshot(),
            "bots_done": self._bots_done,
            "public_dip": self._public_dip,
            "frames": len(self.frames) if self.frames is not None else 0,
            "deadline": self.game.deadline,
            **({"sync": self._sync_snapshot()} if self._sync else {}),
        }

    def _sync_snapshot(self) -> dict:
        return {"index": self._phase_index, "phase_id": self._phase_id, "done": sorted(self._done),
                "queued": {pid: [[a, t] for a, t in q] for pid, q in self._queued.items()},
                "bots_queued": sorted(self._bots_queued),
                "results": {pid: list(r) for pid, r in self._sync_results.items()},
                "neg_spent": self._neg_spent}

    def _sync_restore(self, data) -> None:
        """Phase state from a checkpoint (missing or unreadable: the turn's first phase, nothing queued)."""
        data = data if isinstance(data, dict) else {}
        try:
            self._phase_index = max(0, min(int(data.get("index", 0)), self._rounds))
            self._phase_id = int(data.get("phase_id", 0))
            self._done = set(data.get("done") or [])
            self._queued = {pid: [[a, float(t)] for a, t in q] for pid, q in (data.get("queued") or {}).items() if q}
            self._bots_queued = set(data.get("bots_queued") or [])
            self._sync_results = {pid: list(r) for pid, r in (data.get("results") or {}).items()}
            self._neg_spent = float(data.get("neg_spent", 0.0))
        except (TypeError, ValueError):
            log.warning("game %s: unreadable synchronous-phase state in the checkpoint; restarting the turn's "
                        "negotiation", self.game_id)
            self._phase_index, self._done, self._queued, self._bots_queued = 0, set(), {}, set()
            self._sync_results, self._neg_spent = {}, 0.0

    def checkpoint(self, force: bool = False, bots_idle: bool = False, clean: bool = False) -> bool:
        """Write ``live/<id>.pkl`` (and append new replay frames) if anything
        changed. The snapshot is pickled under the game lock (a few ms); the
        disk writes happen outside it. Never raises: failures are logged and
        retried at the next opportunity. Returns True if a snapshot was written."""
        store = self.manager.live
        if store is None:
            return False
        with self._ckpt_io:
            if self._ckpt_disabled:
                return False
            with self.cond:
                if self.closed or not (force or self._ckpt_force or self._ckpt_dirty):
                    return False
                try:
                    if bots_idle:
                        self._pickle_bots()
                    blob = pickle.dumps(self.snapshot_state(clean), protocol=pickle.HIGHEST_PROTOCOL)
                except Exception:
                    self._ckpt_failed("could not pickle the game state")
                    self._ckpt_dirty = self._ckpt_force = False  # retried on the next change
                    self._ckpt_last = time.monotonic()
                    return False
                frames, self._ckpt_pending = self._ckpt_pending, []
                self._ckpt_dirty = self._ckpt_force = False
                self._ckpt_last = time.monotonic()
            try:
                if frames:
                    self._ckpt_frames_size = store.append_frames(self.game_id, frames, self._ckpt_frames_size)
                    frames = []
                store.write_state(self.game_id, blob)
                return True
            except Exception:
                self._ckpt_failed("could not write the checkpoint")
                with self.cond:
                    self._ckpt_pending[:0] = frames  # not on disk yet: append them next time
                    self._ckpt_dirty = True          # retried after CHECKPOINT_INTERVAL
                return False

    def _ckpt_failed(self, what: str) -> None:
        self._ckpt_errors += 1
        n = self._ckpt_errors
        if n <= 3 or n % 100 == 0:
            log.exception("game %s: %s (failure #%d; the game goes on)", self.game_id, what, n)

    def discard_checkpoint(self) -> None:
        """Stop checkpointing and delete the live files (finished and saved, or closed)."""
        store = self.manager.live
        with self._ckpt_io:
            self._ckpt_disabled = True
            if store is None:
                return
            try:
                store.delete(self.game_id)
            except OSError:
                log.exception("game %s: could not delete its live checkpoint", self.game_id)

    def close_checkpoints(self) -> None:
        """Server shutdown: write a final checkpoint, then no more writes."""
        if self.saved or self.closed:
            return
        self.checkpoint(force=True, bots_idle=not self._thread.is_alive(), clean=True)
        with self._ckpt_io:
            self._ckpt_disabled = True

    @classmethod
    def from_snapshot(cls, manager: "GameManager", state: dict,
                      frames: list[tuple[bytes, bytes | None]], frames_size: int) -> "GameSession":
        """Rebuild a session from :meth:`snapshot_state` output and its frames
        (the caller launches it). Tokens stay the same; the current turn gets
        a fresh deadline (``turn_timeout`` from now) and lobby timers restart."""
        self = cls.__new__(cls)
        self._init_runtime(manager, state["game_id"], state["opts"])
        self.name = state["name"]
        self.created = float(state["created"])
        self.creator_token = state.get("creator_token")
        self.error = state.get("error")
        self.version = int(state.get("version", 0)) + 1
        self.game = state["game"]
        if not isinstance(self.game, Game):
            raise ValueError("checkpoint holds no Game")
        self._bots_done = bool(state.get("bots_done", True))
        self._public_dip = int(state.get("public_dip", 0))
        self.actions = ActionLog.restore(state.get("actions"))  # old checkpoints: an empty log
        self.frames = FrameStore()
        for zfull, zpub in frames:
            self.frames.append_blobs(zfull, zpub)
        self._ckpt_frames_size = frames_size
        now = time.monotonic()
        for d in state["seats"]:
            seat = Seat(d["pid"], d["name"], bool(d["is_bot"]), d.get("bot_name"), d.get("token"),
                        bot_errors=int(d.get("bot_errors", 0)), draft=bool(d.get("draft")),
                        verified=bool(d.get("verified")), reactive=int(d.get("reactive", 0)),
                        acted_seq=int(d.get("acted_seq", 0)), bot_seed=d.get("bot_seed"),
                        bot_blob=d.get("bot_blob"), agent=d.get("agent"), seat_name=d.get("seat_name"))
            if seat.is_bot:
                if seat.bot_blob:
                    try:
                        seat.bot = pickle.loads(seat.bot_blob)
                    except Exception as e:
                        log.warning("game %s: could not unpickle house bot %s (%s: %s); recreating it",
                                    self.game_id, seat.pid, type(e).__name__, e)
                if seat.bot is None:
                    seed = seat.bot_seed if seat.bot_seed is not None else secrets.randbits(31)
                    seat.bot, _ = make_bot(seat.bot_name or "idle", seed)
                    seat.bot_seed = seed
                if d.get("nudge_pending"):
                    seat.nudge_first, seat.nudge_due = now, now + NEGOTIATE_DEBOUNCE
            self.seats[seat.pid] = seat
        g = self.game
        if not state.get("clean") and g.status != "finished":
            # After a crash, events newer than this checkpoint are lost, and clients may have seen
            # their seq numbers already: continue well above them so inbox cursors (?since=) never
            # hide new events. (An orderly shutdown loses nothing and keeps seq as it was.)
            g.diplomacy_seq += CRASH_SEQ_GAP
            log.warning("game %s: the server did not shut down cleanly; resumed from the checkpoint written "
                        "%.1f s ago (anything after it is lost)", self.game_id,
                        max(0.0, time.time() - float(state.get("saved_at", time.time()))))
        if self._sync:
            self._sync_restore(state.get("sync"))
        if g.status == "running":
            # agents get a full turn (in a synchronous game: a full phase) after a restart
            self._turn_started = now
            self._set_deadline(now)
        elif g.status == "finished":
            g.deadline = None
            self.finished_mono = now
        self._ckpt_force = True  # re-checkpoint at once (new deadline; no longer a "clean" snapshot)
        self._ckpt_last = now
        return self


class ArchivedGame:
    """A finished game known only from its replay file (after a restart, or
    once its live session was evicted from memory)."""

    creator_token = None

    def __init__(self, manager: "GameManager", game_id: str, summary: dict):
        self.manager = manager
        self.game_id = game_id
        self._summary = summary

    status = "finished"

    def summary(self, operator: bool = False) -> dict:
        return dict(self._summary)  # finished: real names are public

    def replay_bytes(self, compact: bool = False, lo: int | None = None, hi: int | None = None,
                     *, full: bool = False) -> bytes:
        return self.manager.archived_replay_bytes(self.game_id, compact, lo, hi)

    def state_bytes(self, pid: str | None = None, *, full: bool = False) -> bytes:
        return self.manager.archive(self.game_id).last_frame or _dumps(self._summary)

    def wait(self, since_turn, timeout, since_phase=None) -> dict:
        return {"turn": self._summary.get("turn", 0), "status": "finished", "deadline": None, "timed_out": False}

    def next_frame(self, last_key, timeout, *, full: bool = False):
        return ("finished",), self.state_bytes(), True

    def join(self, name, key=None, agent=None):
        raise ApiError(409, "game is finished")

    def start(self, authorized: bool = True):
        raise ApiError(409, "game is finished")

    def submit(self, pid, body):
        raise ApiError(409, "game is finished", status="finished")

    def diplomacy(self, pid, body):
        raise ApiError(409, "game is finished", status="finished")

    def inbox(self, pid, since, timeout, turn=None) -> dict:
        return {"seq": 0, "items": [], "turn": self._summary.get("turn", 0), "status": "finished",
                "deadline": None, "timed_out": False}


class GameManager:
    """All games, tokens and persistence. Thread-safe.

    ``open_ratings=True`` rates every game created with ``rated: true``
    (handy for private servers and tests); by default only games played
    under standard conditions count (see :func:`unrated_reason`).

    Lobbies and running games are checkpointed to ``<data_dir>/live``;
    ``restore=True`` (default) resumes them on start-up (``False`` leaves
    the files alone). ``checkpoints=False`` disables checkpointing."""

    def __init__(self, data_dir: str = "data", open_ratings: bool = False, restore: bool = True,
                 checkpoints: bool = True):
        self.storage = Storage(data_dir)
        self.live: LiveStore | None = LiveStore(self.storage.root / "live") if checkpoints else None
        self.open_ratings = open_ratings
        self.lock = threading.RLock()
        self._qm_lock = threading.Lock()  # quickmatch lobby choice; never held with self.lock
        self.sessions: dict[str, GameSession] = {}
        self.tokens: dict[str, tuple[str, str]] = {}
        self._retired_tokens: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._token_lock = threading.Lock()
        self._finished: OrderedDict[str, GameSession] = OrderedDict()  # finished + saved, still in memory
        self.stopping = False
        self._archives: OrderedDict[str, ArchivedReplay] = OrderedDict()
        self._archive_lock = threading.Lock()
        ids = self.storage.archived_ids() + (self.live.ids() if self.live is not None else [])
        numbers = [int(g[1:]) for g in ids if re.fullmatch(r"g\d+", g)]
        self._counter = max(numbers, default=0)  # new ids continue after archived and checkpointed ones
        self._quickmatch_counter = 0
        self.max_live_games = MAX_LIVE_GAMES
        self.max_open_lobbies = MAX_OPEN_LOBBIES
        self.lobby_max_age = LOBBY_MAX_AGE
        self.finished_keep = FINISHED_KEEP
        self.finished_ttl = FINISHED_TTL
        self.bot_slots = threading.BoundedSemaphore(BOT_ONLY_SLOTS)
        self._shut_down = False
        self.restored: list[str] = []   # ids of the games resumed from checkpoints
        self._unfinished: dict[str, list] = {}  # saved game id -> [summary, retry due, attempts] (see restore)
        if restore:
            self.restore_games()

    # ------------------------------------------------------------ restore
    def restore_games(self) -> list[str]:
        """Resume every checkpointed lobby/running game (unreadable ones are
        logged and moved to ``live/corrupt``). Returns the restored ids."""
        live = self.live
        if live is None or self.restored:
            return self.restored
        live.cleanup()
        sessions: list[GameSession] = []
        for gid in live.ids():
            summary = self.storage.summary(gid)
            if summary is not None:  # finished and saved; finish what the crash interrupted
                if not self._finish_archived(gid, summary):
                    with self.lock:
                        self._unfinished[gid] = [summary, time.monotonic() + FINALIZE_RETRY, 1]
                continue
            try:
                state = live.load_state(gid)
                frames, ends = live.read_frames(gid)
                want = int(state.get("frames", len(frames)))
                if len(frames) > want:  # appended just before a crash, never counted by a snapshot
                    frames, ends = frames[:want], ends[:want]
                elif len(frames) < want:
                    log.warning("game %s: checkpoint expects %d replay frames, found %d", gid, want, len(frames))
                session = GameSession.from_snapshot(self, state, frames, ends[-1] if ends else 0)
            except Exception as e:
                live.move_aside(gid, f"{type(e).__name__}: {e}")
                continue
            sessions.append(session)
        for session in sessions:
            with self.lock:
                self.sessions[session.game_id] = session
            for seat in session.seats.values():
                if seat.token:
                    self._register_token(seat.token, session.game_id, seat.pid)
            session.launch()
            self.restored.append(session.game_id)
            log.info("restored game %s (%s, %s, turn %s)", session.game_id, session.name, session.status,
                     session.game.turn)
        return self.restored

    def _finish_archived(self, gid: str, summary: dict) -> bool:
        """A checkpoint whose replay is already saved: apply the rating stored in
        the replay summary if its pool has not got it yet, then delete the live
        files. Replays saved before the summary carried ``rating`` were rated
        (or not) by the old code; their checkpoint is just deleted. Returns
        True when nothing is left to do."""
        rating = summary.get("rating")
        try:
            if isinstance(rating, dict) and rating.get("entries"):
                entries = rating["entries"]
                self.storage.record_result(gid, [str(n) for n, _ in entries], [int(r) for _, r in entries],
                                           pool=rating.get("pool", "standard"))
            if self.live is not None:
                self.live.delete(gid)
            return True
        except Exception:
            log.exception("game %s: could not finish the finalization of a saved game (retried later)", gid)
            return False

    # ------------------------------------------------------------ registry
    def _next_id(self) -> str:
        with self.lock:
            self._counter += 1
            return f"g{self._counter}"

    def _register_token(self, token: str, game_id: str, pid: str) -> None:
        # a separate leaf lock: sessions call this while holding their own lock
        with self._token_lock:
            self.tokens[token] = (game_id, pid)

    def resolve_token(self, token: str | None) -> tuple[str, str] | None:
        if not token:
            return None
        with self._token_lock:
            return self.tokens.get(token) or self._retired_tokens.get(token)

    def check_name(self, name: str, key: str | None) -> bool:
        """Enforce registered names (see Storage.check_name); True = key-verified seat."""
        try:
            return self.storage.check_name(name, key)
        except ValueError:
            raise ApiError(403, f"name {name!r} is registered: join with its key (\"key\": ...), or pick "
                                "another name") from None

    def get(self, game_id: str):
        with self.lock:
            s = self.sessions.get(game_id)
        if s is not None:
            return s
        summary = self.storage.summary(game_id)
        if summary is not None:
            return ArchivedGame(self, game_id, summary)
        raise ApiError(404, f"no game {game_id!r}")

    def list_games(self, limit: int | None = None, operator: bool = False) -> list[dict]:
        """Every live game plus the most recent archived ones (``limit`` in total, at least all live).
        ``operator``: live track games show real names (see :meth:`GameSession.summary`)."""
        limit = MAX_GAMES_LISTED if limit is None else limit
        self._sweep()
        with self.lock:
            live = list(self.sessions.values())
        out = [s.summary(operator=operator) for s in live]
        ids = {s["game_id"] for s in out}
        out += self.storage.recent(max(0, limit - len(out)), exclude=ids)
        out.sort(key=lambda s: s.get("created", 0), reverse=True)
        return out

    # ------------------------------------------------------------ housekeeping
    def _retire(self, session: GameSession) -> None:
        """A finished session whose replay is saved: keep it in memory for a
        while (player views), then serve it from the replay file."""
        if not session._fin_done:
            return  # not finalized (no replay on disk): keep serving it from memory; _sweep retries
        with self.lock:
            self._finished[session.game_id] = session
        self._sweep()

    def _retry_finalize(self, now: float) -> None:
        """Retry finished games whose rating or replay write failed (backoff in
        :meth:`GameSession._finalize`), and saved games whose checkpoint
        cleanup failed at restore."""
        if self.stopping:
            return
        with self.lock:
            due = [s for s in self.sessions.values()
                   if s._fin_due is not None and now >= s._fin_due and not s._fin_done]
            archived = [(gid, v[0]) for gid, v in self._unfinished.items() if now >= v[1]]
        for s in due:
            if s._finalize():
                with self.lock:
                    self._finished[s.game_id] = s
        for gid, summary in archived:
            done = self._finish_archived(gid, summary)
            with self.lock:
                v = self._unfinished.get(gid)
                if v is None:
                    continue
                if done:
                    del self._unfinished[gid]
                else:
                    v[2] += 1
                    v[1] = time.monotonic() + min(FINALIZE_RETRY_MAX, FINALIZE_RETRY * 2 ** (v[2] - 1))

    def _sweep(self) -> None:
        """Retry unfinished finalizations, evict old finished sessions and
        close lobbies nobody started."""
        now = time.monotonic()
        self._retry_finalize(now)
        evict: list[GameSession] = []
        close: list[GameSession] = []
        with self.lock:
            while self._finished:
                gid, s = next(iter(self._finished.items()))
                if len(self._finished) > self.finished_keep or now - (s.finished_mono or now) > self.finished_ttl:
                    self._finished.popitem(last=False)
                    if self.sessions.get(gid) is s:
                        del self.sessions[gid]
                    evict.append(s)
                else:
                    break
            for gid, s in list(self.sessions.items()):
                if s.status == "lobby" and now - s._created_mono > self.lobby_max_age:
                    del self.sessions[gid]
                    close.append(s)
        for s in close:
            with s.cond:
                if s.status == "lobby":
                    s.closed = True
                    s._touch()
                    log.info("closed lobby %s (not started within %.0f s)", s.game_id, self.lobby_max_age)
                else:  # started meanwhile: keep it
                    with self.lock:
                        self.sessions[s.game_id] = s
                    continue
            s.discard_checkpoint()
            self._drop_tokens(s, retire=False)
        for s in evict:
            self._drop_tokens(s, retire=True)

    def _drop_tokens(self, session: GameSession, retire: bool) -> None:
        tokens = [seat.token for seat in session.seats.values() if seat.token]
        with self._token_lock:
            for t in tokens:
                v = self.tokens.pop(t, None)
                if retire and v is not None:
                    self._retired_tokens[t] = v
            while len(self._retired_tokens) > RETIRED_TOKENS:
                self._retired_tokens.popitem(last=False)

    # ------------------------------------------------------------ creation
    def create_game(self, body: dict, operator: bool = False) -> GameSession:
        """``POST /api/games``; ``operator``: the request carried the spectator key."""
        opts = parse_game_options(body, operator)
        session, _ = self._create(opts)
        return session

    def _check_capacity(self) -> None:
        self._sweep()
        with self.lock:
            live = [s for s in self.sessions.values() if s.status != "finished"]
        if len(live) >= self.max_live_games:
            raise ApiError(503, f"server busy: {len(live)} live games (max {self.max_live_games}); try again later")
        lobbies = sum(1 for s in live if s.status == "lobby")
        if lobbies >= self.max_open_lobbies:
            raise ApiError(503, f"server busy: {lobbies} open lobbies (max {self.max_open_lobbies}); join one "
                                "(GET /api/games) or try again later")

    def _create(self, opts: dict, first_player: tuple[str, str | None, dict | None] | None = None
                ) -> tuple[GameSession, Seat | None]:
        """Create and launch a session; ``first_player`` (name, key, agent) is seated
        before the worker starts, so a lobby with a zero timeout can't start
        without them."""
        if self.stopping:
            raise ApiError(503, "server is shutting down")
        self._check_capacity()
        if self.open_ratings:
            reason = None if opts["rated"] else "created with rated: false"
        else:
            reason = unrated_reason(opts)
        opts["rated"], opts["unrated_reason"] = reason is None, reason
        opts["rules_sha256"] = rules_sha256()  # the rules this game is played under (stored with it)
        if opts.get("track"):
            self._pin_track_rules(opts["track"], opts["rules_sha256"])
        gid = self._next_id()
        session = GameSession(self, gid, opts)
        if not opts["quickmatch"]:
            session.creator_token = secrets.token_urlsafe(24)
        seat = session.join(*first_player) if first_player else None
        with self.lock:
            self.sessions[gid] = session
        session.launch()
        log.info("created game %s (%s)", gid, session.name)
        return session, seat

    def _pin_track_rules(self, track_id: str, sha: str) -> None:
        """The first game of a track in this data dir pins the track pool to
        the server's rules hash; a later game under other rules is refused."""
        try:
            self.storage.pin_rules(track_id, sha)
        except RulesChanged as e:
            m = re.fullmatch(r"(.*-v)(\d+)", track_id)
            nxt = f"{m.group(1)}{int(m.group(2)) + 1}" if m else track_id + "-v2"
            raise ApiError(409, f"track {track_id} was defined against rules {e.pinned[:12]}, but this server runs "
                                f"rules {sha[:12]}: the rules changed, so its games would not be comparable. "
                                f"The operator must define a new track version (e.g. {nxt}) in "
                                "agentciv/server/tracks.py", track=track_id, pinned_rules_sha256=e.pinned,
                           rules_sha256=sha) from None

    def tracks(self) -> list[dict]:
        """``GET /api/tracks``: every track with the rules hash its pool is pinned to."""
        current = rules_sha256()
        out = []
        for track in TRACKS.values():
            d = track.public()
            pinned = self.storage.pinned_rules(track.id)
            d["rules_sha256"] = pinned
            d["current_rules_sha256"] = current
            d["open"] = pinned is None or pinned == current
            out.append(d)
        return out

    def _quickmatch_join(self, match: tuple, name: str, key, agent):
        """Join the first open quickmatch lobby with this match key (None: none could take us)."""
        with self.lock:
            candidates = [s for s in self.sessions.values()
                          if s.opts["quickmatch"] and s.status == "lobby" and not s.closed
                          and s.opts.get("match") == match]
        for s in candidates:
            try:
                return s, s.join(name, key, agent)
            except ApiError as e:
                if e.status in (400, 403):
                    raise
                continue  # full, started meanwhile, or name taken: try the next lobby
        return None

    def _quickmatch_track(self, body: dict) -> tuple[GameSession, Seat]:
        """``POST /api/quickmatch {"track": id, "name", "agent", "key"?}``: the
        open quickmatch lobby of that track (or a new one). Track lobbies never
        fill with bots: they start when all seats are taken (and close after
        ``lobby_max_age`` otherwise)."""
        track = _track(body.get("track"))
        _check_frozen(body, track.quickmatch_options(), track, ("track", "name", "key", "agent"))
        name = validate_player_name(body.get("name"))
        key = _player_key(body)
        agent = parse_agent(body)
        if SEAT_NAME.match(name):
            raise ApiError(400, "names of the form 'Player N' are reserved for the anonymous seats of track games")
        why = track.check_agent(agent)
        if why:
            raise ApiError(400, why)
        self.check_name(name, key)
        match = ("track", track.id)
        with self._qm_lock:
            found = self._quickmatch_join(match, name, key, agent)
            if found is not None:
                return found
            with self.lock:
                self._quickmatch_counter += 1
                n = self._quickmatch_counter
            opts = _track_options({"track": track.id, "name": f"{track.id} quickmatch #{n}"}, operator=False)
            opts["quickmatch"] = True
            opts["match"] = match
            return self._create(opts, first_player=(name, key, agent))

    def quickmatch(self, body: dict) -> tuple[GameSession, Seat]:
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        if body.get("track") is not None:
            return self._quickmatch_track(body)
        name = validate_player_name(body.get("name"))
        key = _player_key(body)
        agent = parse_agent(body)  # provenance only: never part of the lobby match
        players = _number(body, "players", 6, 1, C.MAX_PLAYERS, integer=True)
        turn_timeout = _turn_timeout(body, 30.0)
        max_turns = _number(body, "max_turns", C.DEFAULT_MAX_TURNS, 1, 1000, integer=True)
        lobby_timeout = float(_number(body, "lobby_timeout", DEFAULT_QUICKMATCH_LOBBY, 0, MAX_TIMEOUT))
        fill = _bool(body, "fill_with_bots", True)
        fog = _bool(body, "fog", False)
        sync, rounds = _sync_options(body)
        self.check_name(name, key)  # a registered name with a wrong/missing key fails before any lobby is made
        # Lobbies are matched on every setting that changes how the game starts or plays, so a
        # caller asking for an odd lobby_timeout/fill can't trap the default matchmaking bucket.
        match = (players, turn_timeout, max_turns, lobby_timeout, fill, fog)
        if sync:  # synchronous and live players are never mixed (live lobbies keep their old key)
            match += ("sync", rounds)
        with self._qm_lock:  # one lobby choice at a time; the manager lock stays free for other requests
            found = self._quickmatch_join(match, name, key, agent)
            if found is not None:
                return found
            with self.lock:
                self._quickmatch_counter += 1
                n = self._quickmatch_counter
            opts = parse_game_options({
                "name": f"Quickmatch #{n}",
                "max_players": players, "min_players": min(2, players),
                "turn_timeout": turn_timeout, "max_turns": max_turns, "fill_with_bots": fill,
                "fog": fog,
                **({"sync": True, "negotiation_rounds": rounds} if sync else {}),
            })
            # lobby_timeout 0 = start right away (with bots if fill_with_bots), not "never"
            opts["lobby_timeout"] = lobby_timeout
            opts["quickmatch"] = True
            opts["match"] = match
            session, seat = self._create(opts, first_player=(name, key, agent))
            return session, seat

    # ------------------------------------------------------------ replays on disk
    def _read_replay(self, game_id: str) -> bytes:
        data = self.storage.read_replay(game_id)
        if data is None:
            raise ApiError(404, "replay file is missing")
        return data

    def archive(self, game_id: str) -> ArchivedReplay:
        """The parsed replay file of a finished game (small LRU cache)."""
        with self._archive_lock:
            a = self._archives.get(game_id)
            if a is not None:
                self._archives.move_to_end(game_id)
                return a
        try:
            a = ArchivedReplay(self._read_replay(game_id))
        except ValueError:
            raise ApiError(500, "replay file is corrupt") from None
        with self._archive_lock:
            self._archives[game_id] = a
            while len(self._archives) > ARCHIVE_CACHE_SIZE:
                self._archives.popitem(last=False)
        return a

    def archived_replay_bytes(self, game_id: str, compact: bool, lo: int | None, hi: int | None) -> bytes:
        if compact:
            a = self.archive(game_id)
            return envelope(game_id, a.summary, a.result, a.compact(lo, hi), compact=True, static=a.static,
                            total=len(a), lo=max(0, lo or 0))
        data = self._read_replay(game_id)
        if lo is None and hi is None:
            return data
        try:
            return slice_full_replay(data, lo, hi)
        except ValueError:
            raise ApiError(500, "replay file is corrupt") from None

    # ------------------------------------------------------------ misc
    def leaderboard(self, mode: str = "standard") -> list[dict]:
        """Ratings of the ``standard`` pool, the ``fog`` pool (fog-of-war games) or a track's pool."""
        rows = self.storage.leaderboard(mode)
        for r in rows:  # house bots' names are reserved, so their entries are authentic too
            r["verified"] = r["verified"] or r["name"] in BOT_REGISTRY
        return rows

    def shutdown(self) -> None:
        """Stop all games: wake every waiter, let the game workers finish their
        current step (up to ``SHUTDOWN_JOIN`` s in total), then write a final
        checkpoint of every lobby/running game. Idempotent."""
        self.stopping = True
        with self.lock:
            sessions = list(self.sessions.values())
            first = not self._shut_down
            self._shut_down = True
        for s in sessions:
            s.notify()
        if not first:
            return
        end = time.monotonic() + SHUTDOWN_JOIN
        me = threading.current_thread()
        for s in sessions:
            t = s._thread
            if t.is_alive() and t is not me:
                t.join(max(0.0, end - time.monotonic()))
        for s in sessions:
            s.close_checkpoints()
