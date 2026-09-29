"""Game sessions and the game manager (threading, timing, house bots).

A :class:`GameSession` wraps one :class:`agentciv.engine.Game` with

* a re-entrant lock + condition variable (every engine call happens under it),
* the seats (remote players with tokens, and in-process house bots),
* a worker thread that auto-starts the lobby, runs the house bots each turn
  and advances the turn when every living remote player has submitted or the
  deadline passes,
* cached JSON bytes of the spectator view (cheap for many GUI clients) and the
  replay frames (one spectator view per turn).

The :class:`GameManager` owns all sessions, tokens, quickmatch lobbies and the
:class:`~agentciv.server.storage.Storage` (replays + leaderboard).
"""
from __future__ import annotations

import importlib
import json
import logging
import math
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
from .guide import order_hint
from .replay import ArchivedReplay, FrameStore, envelope, slice_full_replay
from .storage import Storage

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

# Ratings: only games played under standard, server-controlled conditions feed the leaderboard.
MAX_RATED_TURN_TIMEOUT = 300.0   # rated games need a real deadline, so a losing player can't stall forever
UNRATED_BOTS = frozenset({"idle", "random"})  # creator-picked baseline bots make a game unrated

# Resource limits (attributes of GameManager, so embedders and tests can change them).
MAX_LIVE_GAMES = 200             # lobbies + running games in memory; creation beyond this -> 503
MAX_OPEN_LOBBIES = 50            # unstarted lobbies; creation beyond this -> 503
LOBBY_MAX_AGE = 3600.0           # an unstarted lobby is closed after this many seconds
FINISHED_KEEP = 16               # finished sessions kept in memory (then served from the replay file)
FINISHED_TTL = 300.0             # ... for at most this many seconds after finishing
BOT_ONLY_SLOTS = 2               # bot-only games whose house bots may think at the same time
RETIRED_TOKENS = 100_000         # tokens of evicted games still recognised (so late calls get 409, not 401)
MAX_GAMES_LISTED = 100           # default number of games in GET /api/games (all live ones + recent archived)


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


def parse_game_options(body: dict) -> dict:
    """Validate a ``POST /api/games`` body into a normalised options dict."""
    if not isinstance(body, dict):
        raise ApiError(400, "body must be a JSON object")
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
        "quickmatch": False,
        "seed_given": seed_given,
    }


def unrated_reason(opts: dict) -> str | None:
    """Why a game can't be rated (None = it can). Rated games must be played
    under standard conditions nobody can tailor: a server-chosen seed, the
    full turn limit, a real deadline, and no creator-picked baseline bots."""
    if not opts.get("rated", True):
        return "created with rated: false"
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


class GameSession:
    """One live game. All public methods are thread-safe."""

    def __init__(self, manager: "GameManager", game_id: str, opts: dict):
        self.manager = manager
        self.game_id = game_id
        self.opts = opts
        self.name = opts["name"] or f"Game {game_id}"
        self.created = time.time()
        self._created_mono = time.monotonic()
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.game = Game(GameConfig(seed=opts["seed"], max_turns=opts["max_turns"], game_id=game_id,
                                    name=self.name, max_players=opts["max_players"]))
        self.seats: dict[str, Seat] = {}
        self.frames: FrameStore | None = FrameStore()  # zlib-compressed, freed once saved to disk
        self.saved = False
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
        self._thread = threading.Thread(target=self._run, name=f"game-{game_id}", daemon=True)
        with self.cond:
            for b in opts["bots"]:
                self._add_bot(b)

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
        self.cond.notify_all()

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
        bot, actual = make_bot(bot_name, secrets.randbits(31))
        pid = self._add_player(self._unique_bot_name(actual))
        seat = Seat(pid, self.game.player(pid).name, True, actual, None, bot)
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

    def join(self, name: str, key: str | None = None) -> Seat:
        name = validate_player_name(name)
        verified = self.manager.check_name(name, key)
        with self.cond:
            if self.closed:
                raise ApiError(409, f"lobby {self.game_id} was closed")
            if self.status != "lobby":
                raise ApiError(409, f"game {self.game_id} has already started" if self.status == "running"
                               else f"game {self.game_id} is finished")
            if len(self.seats) >= self.max_players:
                raise ApiError(409, f"game {self.game_id} is full")
            if any(s.name.lower() == name.lower() for s in self.seats.values()):
                raise ApiError(409, f"name {name!r} is already taken in this game")
            pid = self._add_player(name)
            seat = Seat(pid, name, False, None, secrets.token_urlsafe(24), verified=verified)
            self.seats[pid] = seat
            self.manager._register_token(seat.token, self.game_id, pid)
            self._touch()
            if len(self.seats) >= self.max_players:
                self._start()
            return seat

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

    def _begin_turn(self) -> None:
        now = time.monotonic()
        self._turn_started = now
        tt = self.opts["turn_timeout"]
        if tt > 0:
            self._deadline_mono = now + tt
            self.game.deadline = round(time.time() + tt, 3)
        else:
            self._deadline_mono = None
            self.game.deadline = None
        alive = set(self.game.alive_players())
        self._bots_done = not any(s.is_bot and s.pid in alive for s in self.seats.values())
        for s in self.seats.values():
            s.draft, s.error_at = False, None
        self._touch()

    def _record_frame(self) -> None:
        if self.frames is not None:
            full, public = self._spectator_pair()
            self.frames.append(full, public)

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

    def _advance(self) -> None:
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
            jobs = None
            with self.cond:
                if self.closed:
                    break
                if self.status == "lobby":
                    wait = self._lobby_wait()
                    if self.status == "lobby":
                        self.cond.wait(wait)
                    continue
                if self.status == "finished":
                    break
                if not self._bots_done:
                    turn = self.game.turn
                    alive = set(self.game.alive_players())
                    jobs = [(s, self.game.player_view(s.pid)) for s in self.seats.values()
                            if s.is_bot and s.pid in alive]
                    bot_only = not self._living_remote()
                else:
                    wait = self._seconds_until_ready()
                    if wait <= 0:
                        self._advance()
                        continue
                    self.cond.wait(min(wait, 1.0))
                    continue
            # house bots think outside the lock so state requests stay fast; bot-only games share a
            # few compute slots so a pile of them can't starve the API and games with remote players
            if bot_only:
                with self.manager.bot_slots:
                    results = [(seat, self._bot_orders(seat, view)) for seat, view in jobs]
            else:
                results = [(seat, self._bot_orders(seat, view)) for seat, view in jobs]
            with self.cond:
                if self.game.turn == turn and self.status == "running":
                    for seat, orders in results:
                        errs = self.game.submit_orders(seat.pid, orders)
                        if errs and log.isEnabledFor(logging.DEBUG):
                            log.debug("game %s %s (%s) order errors: %s", self.game_id, seat.pid,
                                      seat.bot_name, errs[:5])
                self._bots_done = True
                self._touch()
        if self.status == "finished" and not self.manager.stopping:
            self._finalize()
            self.manager._retire(self)

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

    def _finalize(self) -> None:
        with self.cond:
            frames = self.frames.all_full() if self.frames is not None else []
            summary = self.summary()
            result = self.game.result
            ranked = self._rated_entries()
            for seat in self.seats.values():
                seat.bot = None  # free the house bots (and their caches) now
        try:
            self.manager.storage.save_replay(self.game_id, summary, result, frames)
            with self.cond:
                self.saved = True
                self.frames = None  # served from disk from now on
        except OSError:
            log.exception("game %s: could not save replay", self.game_id)
        if ranked:
            try:
                self.manager.storage.record_result([n for n, _ in ranked], [r for _, r in ranked])
            except OSError:
                log.exception("game %s: could not update leaderboard", self.game_id)

    def _rated_entries(self) -> list[tuple[str, int]] | None:
        """(rating name, rank) per seat in placement order, or None when the
        game doesn't count. Players tied on score (same alive state and
        elimination turn) share a rank, so the engine's seat-order tie-break
        never decides ratings; a winner by a victory condition ranks alone."""
        result = self.game.result
        if not self.opts["rated"] or not result or self.error is not None or len(self.seats) < 2:
            return None
        if not self.manager.open_ratings and all(s.is_bot for s in self.seats.values()):
            return None  # bot-only games don't move the leaderboard
        scores = result.get("scores") or {}
        out: list[tuple[str, int]] = []
        prev, rank = None, 0
        for i, pid in enumerate(result.get("placements", [])):
            seat, p = self.seats.get(pid), self.game.player(pid)
            key = (bool(p and p.alive), getattr(p, "eliminated_turn", None), scores.get(pid))
            if i == 0 or key != prev or (i == 1 and result.get("condition") != "score"):
                rank = i + 1
            prev = key
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

    def _spectator_bytes(self) -> bytes:
        """What a spectator may see right now (the public view while running)."""
        return self._spectator_pair()[1]

    def state_bytes(self, pid: str | None = None) -> bytes:
        with self.cond:
            if pid is None:
                return self._spectator_bytes()
            cached = self._player_cache.get(pid)
            if cached is None or cached[0] != self.version:
                cached = (self.version, _dumps(self.game.player_view(pid)))
                self._player_cache[pid] = cached
            return cached[1]

    def replay_bytes(self, compact: bool = False, lo: int | None = None, hi: int | None = None) -> bytes:
        """The replay (docs/DESIGN.md §12); ``lo``/``hi`` select an inclusive
        frame range, ``compact`` the lighter format of :mod:`.replay`."""
        with self.cond:
            fs = self.frames
            if fs is not None:
                summary, result = self.summary(), self.game.result
                public = self.status != "finished"  # private diplomacy is revealed once the game is over
                if compact:
                    return envelope(self.game_id, summary, result, fs.compact(lo, hi, public=public),
                                    compact=True, static=fs.static, total=len(fs), lo=max(0, lo or 0))
                if lo is None and hi is None:
                    return envelope(self.game_id, summary, result, fs.full(public=public))
                return envelope(self.game_id, summary, result, fs.full(lo, hi, public=public), total=len(fs),
                                lo=max(0, lo or 0))
        return self.manager.archived_replay_bytes(self.game_id, compact, lo, hi)

    def summary(self) -> dict:
        with self.cond:
            g = self.game
            players = []
            for pid, s in self.seats.items():
                p = g.player(pid)
                entry = {"id": pid, "name": s.name, "is_bot": s.is_bot, "alive": bool(p and p.alive),
                         "submitted": bool(g.status == "running" and g.has_submitted(pid))}
                if s.is_bot:
                    entry["bot"] = s.bot_name
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
                "seed": self.opts["seed"],
                "fill_with_bots": self.opts["fill_with_bots"],
                "lobby_timeout": self.opts["lobby_timeout"],
                "quickmatch": self.opts["quickmatch"],
                "rated": self.opts["rated"],
                "unrated_reason": self.opts.get("unrated_reason"),
                "deadline": g.deadline,
                "result": dict(g.result) if g.result else None,
                "frames": len(self.frames) if self.frames is not None else g.turn + 1,
            }
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
            errors = g.submit_orders(pid, orders)
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

    def wait(self, since_turn: int | None, timeout: float) -> dict:
        end = time.monotonic() + timeout
        with self.cond:
            if since_turn is None:
                since_turn = self.game.turn if self.status == "running" else -1
            timed_out = False
            while not self.manager.stopping:
                st = self.game.status
                if st == "finished" or (st == "running" and self.game.turn > since_turn):
                    break
                remaining = end - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                self.cond.wait(remaining)
            return {"turn": self.game.turn, "status": self.game.status, "deadline": self.game.deadline,
                    "timed_out": timed_out}

    def _stream_key(self) -> tuple:
        # remote players' submissions are part of the key so spectators see "submitted" live
        subs = tuple(pid for pid, s in self.seats.items()
                     if not s.is_bot and self.game.status == "running" and self.game.has_submitted(pid))
        return (self.game.status, self.game.turn, len(self.seats), subs)

    def next_frame(self, last_key, timeout: float):
        """Block until the spectator-visible state changes (turn, status or
        lobby seats). Returns ``(key, bytes | None, finished)``; bytes is None
        on timeout."""
        end = time.monotonic() + timeout
        with self.cond:
            while self._stream_key() == last_key and not self.manager.stopping:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return last_key, None, self.status == "finished"
                self.cond.wait(remaining)
            return self._stream_key(), self._spectator_bytes(), self.status == "finished"

    def notify(self) -> None:
        with self.cond:
            self.cond.notify_all()


class ArchivedGame:
    """A finished game known only from its replay file (after a restart, or
    once its live session was evicted from memory)."""

    creator_token = None

    def __init__(self, manager: "GameManager", game_id: str, summary: dict):
        self.manager = manager
        self.game_id = game_id
        self._summary = summary

    status = "finished"

    def summary(self) -> dict:
        return dict(self._summary)

    def replay_bytes(self, compact: bool = False, lo: int | None = None, hi: int | None = None) -> bytes:
        return self.manager.archived_replay_bytes(self.game_id, compact, lo, hi)

    def state_bytes(self, pid: str | None = None) -> bytes:
        return self.manager.archive(self.game_id).last_frame or _dumps(self._summary)

    def wait(self, since_turn, timeout) -> dict:
        return {"turn": self._summary.get("turn", 0), "status": "finished", "deadline": None, "timed_out": False}

    def next_frame(self, last_key, timeout):
        return ("finished",), self.state_bytes(), True

    def join(self, name, key=None):
        raise ApiError(409, "game is finished")

    def start(self, authorized: bool = True):
        raise ApiError(409, "game is finished")

    def submit(self, pid, body):
        raise ApiError(409, "game is finished", status="finished")


class GameManager:
    """All games, tokens and persistence. Thread-safe.

    ``open_ratings=True`` rates every game created with ``rated: true``
    (handy for private servers and tests); by default only games played
    under standard conditions count (see :func:`unrated_reason`)."""

    def __init__(self, data_dir: str = "data", open_ratings: bool = False):
        self.storage = Storage(data_dir)
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
        numbers = [int(g[1:]) for g in self.storage.archived_ids() if re.fullmatch(r"g\d+", g)]
        self._counter = max(numbers, default=0)
        self._quickmatch_counter = 0
        self.max_live_games = MAX_LIVE_GAMES
        self.max_open_lobbies = MAX_OPEN_LOBBIES
        self.lobby_max_age = LOBBY_MAX_AGE
        self.finished_keep = FINISHED_KEEP
        self.finished_ttl = FINISHED_TTL
        self.bot_slots = threading.BoundedSemaphore(BOT_ONLY_SLOTS)

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

    def list_games(self, limit: int | None = None) -> list[dict]:
        """Every live game plus the most recent archived ones (``limit`` in total, at least all live)."""
        limit = MAX_GAMES_LISTED if limit is None else limit
        self._sweep()
        with self.lock:
            live = list(self.sessions.values())
        out = [s.summary() for s in live]
        ids = {s["game_id"] for s in out}
        out += self.storage.recent(max(0, limit - len(out)), exclude=ids)
        out.sort(key=lambda s: s.get("created", 0), reverse=True)
        return out

    # ------------------------------------------------------------ housekeeping
    def _retire(self, session: GameSession) -> None:
        """A finished session whose replay is saved: keep it in memory for a
        while (player views), then serve it from the replay file."""
        if not session.saved:
            return  # no replay on disk: keep serving it from memory
        with self.lock:
            self._finished[session.game_id] = session
        self._sweep()

    def _sweep(self) -> None:
        """Evict old finished sessions and close lobbies nobody started."""
        now = time.monotonic()
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
    def create_game(self, body: dict) -> GameSession:
        opts = parse_game_options(body)
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

    def _create(self, opts: dict, first_player: tuple[str, str | None] | None = None
                ) -> tuple[GameSession, Seat | None]:
        """Create and launch a session; ``first_player`` (name, key) is seated
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

    def quickmatch(self, body: dict) -> tuple[GameSession, Seat]:
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        name = validate_player_name(body.get("name"))
        key = _player_key(body)
        players = _number(body, "players", 6, 1, C.MAX_PLAYERS, integer=True)
        turn_timeout = _turn_timeout(body, 30.0)
        max_turns = _number(body, "max_turns", C.DEFAULT_MAX_TURNS, 1, 1000, integer=True)
        lobby_timeout = float(_number(body, "lobby_timeout", DEFAULT_QUICKMATCH_LOBBY, 0, MAX_TIMEOUT))
        fill = _bool(body, "fill_with_bots", True)
        self.check_name(name, key)  # a registered name with a wrong/missing key fails before any lobby is made
        # Lobbies are matched on every setting that changes how the game starts or plays, so a
        # caller asking for an odd lobby_timeout/fill can't trap the default matchmaking bucket.
        match = (players, turn_timeout, max_turns, lobby_timeout, fill)
        with self._qm_lock:  # one lobby choice at a time; the manager lock stays free for other requests
            with self.lock:
                candidates = [s for s in self.sessions.values()
                              if s.opts["quickmatch"] and s.status == "lobby" and not s.closed
                              and s.opts.get("match") == match]
            for s in candidates:
                try:
                    return s, s.join(name, key)
                except ApiError as e:
                    if e.status == 403:
                        raise
                    continue  # full, started meanwhile, or name taken: try the next lobby
            with self.lock:
                self._quickmatch_counter += 1
                n = self._quickmatch_counter
            opts = parse_game_options({
                "name": f"Quickmatch #{n}",
                "max_players": players, "min_players": min(2, players),
                "turn_timeout": turn_timeout, "max_turns": max_turns, "fill_with_bots": fill,
            })
            # lobby_timeout 0 = start right away (with bots if fill_with_bots), not "never"
            opts["lobby_timeout"] = lobby_timeout
            opts["quickmatch"] = True
            opts["match"] = match
            session, seat = self._create(opts, first_player=(name, key))
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
    def leaderboard(self) -> list[dict]:
        rows = self.storage.leaderboard()
        for r in rows:  # house bots' names are reserved, so their entries are authentic too
            r["verified"] = r["verified"] or r["name"] in BOT_REGISTRY
        return rows

    def shutdown(self) -> None:
        self.stopping = True
        with self.lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            s.notify()
