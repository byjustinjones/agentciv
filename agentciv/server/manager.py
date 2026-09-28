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
from dataclasses import dataclass, field

from ..bots import REGISTRY as BOT_REGISTRY
from ..bots import get_bot
from ..bots.base import Bot, IdleBot
from ..engine import Game, GameConfig
from ..engine import constants as C
from .storage import Storage

log = logging.getLogger("agentciv.server")

MIN_TURN_TIMEOUT = 0.05          # seconds; smaller positive values are raised to this
MAX_TIMEOUT = 86400.0
MAX_WAIT = 120.0                 # cap for /wait long-polls
DEFAULT_QUICKMATCH_LOBBY = 30.0  # quickmatch lobbies fill with bots after this many seconds
SPECTATOR_BOT_DELAY = 0.5        # pacing of games without living remote players
DEFAULT_FILL_BOTS = ["strategist", "economist", "rusher", "turtle", "random"]
BOT_SLOW_WARN = 5.0


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
    }


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
        self.frames: list[bytes] | None = []
        self.saved = False
        self.error: str | None = None
        self.version = 0
        self._spec_cache: tuple[int, bytes] = (-1, b"")
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
        index = len(self.seats)
        seed = (int(self.opts["seed"]) * 7919 + index * 104729 + 17) % (2 ** 31)
        bot, actual = make_bot(bot_name, seed)
        pid = self.game.add_player(self._unique_bot_name(actual))
        seat = Seat(pid, self.game.player(pid).name, True, actual, None, bot)
        self.seats[pid] = seat
        self._touch()
        return seat

    def join(self, name: str) -> Seat:
        name = validate_player_name(name)
        with self.cond:
            if self.status != "lobby":
                raise ApiError(409, f"game {self.game_id} has already started" if self.status == "running"
                               else f"game {self.game_id} is finished")
            if len(self.seats) >= self.max_players:
                raise ApiError(409, f"game {self.game_id} is full")
            if any(s.name.lower() == name.lower() for s in self.seats.values()):
                raise ApiError(409, f"name {name!r} is already taken in this game")
            pid = self.game.add_player(name)
            seat = Seat(pid, name, False, None, secrets.token_urlsafe(24))
            self.seats[pid] = seat
            self.manager._register_token(seat.token, self.game_id, pid)
            self._touch()
            if len(self.seats) >= self.max_players:
                self._start()
            return seat

    # ------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        """Start the game now (``POST /start``). Returns False if it had
        already started (idempotent), raises ApiError if it can't start."""
        with self.cond:
            if self.status != "lobby":
                if self.status == "finished":
                    raise ApiError(409, "game is finished")
                return False
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
        self._touch()

    def _record_frame(self) -> None:
        if self.frames is not None:
            self.frames.append(self._spectator_bytes())

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
        if all(self.game.has_submitted(s.pid) for s in remote):
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
            self._touch()
            log.info("game %s finished: %s", self.game_id, self.game.result)
        self._record_frame()

    # ------------------------------------------------------------ worker
    def _run(self) -> None:
        while not self.manager.stopping:
            jobs = None
            with self.cond:
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
                else:
                    wait = self._seconds_until_ready()
                    if wait <= 0:
                        self._advance()
                        continue
                    self.cond.wait(min(wait, 1.0))
                    continue
            # house bots think outside the lock so state requests stay fast
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
            frames = list(self.frames or [])
            summary = self.summary()
            result = self.game.result
            names = {pid: s for pid, s in self.seats.items()}
        try:
            self.manager.storage.save_replay(self.game_id, summary, result, frames)
            with self.cond:
                self.saved = True
                self.frames = None  # served from disk from now on
        except OSError:
            log.exception("game %s: could not save replay", self.game_id)
        if self.opts["rated"] and result and self.error is None and len(names) >= 2:
            ordered = []
            for pid in result.get("placements", []):
                seat = names.get(pid)
                if seat is not None:
                    ordered.append(seat.bot_name if seat.is_bot else seat.name)
            try:
                self.manager.storage.record_result(ordered)
            except OSError:
                log.exception("game %s: could not update leaderboard", self.game_id)

    # ------------------------------------------------------------ views
    def _spectator_bytes(self) -> bytes:
        if self._spec_cache[0] != self.version:
            self._spec_cache = (self.version, _dumps(self.game.spectator_view()))
        return self._spec_cache[1]

    def state_bytes(self, pid: str | None = None) -> bytes:
        with self.cond:
            if pid is None:
                return self._spectator_bytes()
            cached = self._player_cache.get(pid)
            if cached is None or cached[0] != self.version:
                cached = (self.version, _dumps(self.game.player_view(pid)))
                self._player_cache[pid] = cached
            return cached[1]

    def replay_bytes(self) -> bytes:
        with self.cond:
            if self.frames is not None:
                head = _dumps({"game_id": self.game_id, "summary": self.summary(), "result": self.game.result})
                return head[:-1] + b',"frames":[' + b",".join(self.frames) + b"]}"
        data = self.manager.storage.read_replay(self.game_id)
        if data is None:
            raise ApiError(404, "replay file is missing")
        return data

    def summary(self) -> dict:
        with self.cond:
            g = self.game
            players = []
            for pid, s in self.seats.items():
                p = g.player(pid)
                entry = {"id": pid, "name": s.name, "is_bot": s.is_bot, "alive": bool(p and p.alive)}
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
                "deadline": g.deadline,
                "result": dict(g.result) if g.result else None,
            }
            if self.error:
                out["error"] = self.error
            return out

    # ------------------------------------------------------------ player actions
    def submit(self, pid: str, body) -> dict:
        if isinstance(body, list):
            orders, turn = body, None
        elif isinstance(body, dict):
            orders, turn = body.get("orders"), body.get("turn")
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
            self._touch()
            bad = sum(1 for e in errors if e.get("index", -1) >= 0)
            accepted = 0 if any(e.get("index", -1) < 0 for e in errors) else len(orders) - bad
            return {"accepted": max(0, accepted), "errors": errors, "turn": g.turn, "deadline": g.deadline}

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
        return (self.game.status, self.game.turn, len(self.seats))

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
    """A finished game known only from its replay file (after a restart)."""

    def __init__(self, manager: "GameManager", game_id: str, summary: dict):
        self.manager = manager
        self.game_id = game_id
        self._summary = summary
        self._last_frame: bytes | None = None

    status = "finished"

    def summary(self) -> dict:
        return dict(self._summary)

    def replay_bytes(self) -> bytes:
        data = self.manager.storage.read_replay(self.game_id)
        if data is None:
            raise ApiError(404, "replay file is missing")
        return data

    def state_bytes(self, pid: str | None = None) -> bytes:
        if self._last_frame is None:
            try:
                frames = json.loads(self.replay_bytes()).get("frames") or []
            except ValueError:
                raise ApiError(500, "replay file is corrupt") from None
            self._last_frame = _dumps(frames[-1]) if frames else _dumps(self._summary)
        return self._last_frame

    def wait(self, since_turn, timeout) -> dict:
        return {"turn": self._summary.get("turn", 0), "status": "finished", "deadline": None, "timed_out": False}

    def next_frame(self, last_key, timeout):
        return ("finished",), self.state_bytes(), True

    def join(self, name):
        raise ApiError(409, "game is finished")

    def start(self):
        raise ApiError(409, "game is finished")

    def submit(self, pid, body):
        raise ApiError(409, "game is finished", status="finished")


class GameManager:
    """All games, tokens and persistence. Thread-safe."""

    def __init__(self, data_dir: str = "data"):
        self.storage = Storage(data_dir)
        self.lock = threading.RLock()
        self.sessions: dict[str, GameSession] = {}
        self.tokens: dict[str, tuple[str, str]] = {}
        self._token_lock = threading.Lock()
        self.stopping = False
        numbers = [int(g[1:]) for g in self.storage.archived() if re.fullmatch(r"g\d+", g)]
        self._counter = max(numbers, default=0)
        self._quickmatch_counter = 0

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
            return self.tokens.get(token)

    def get(self, game_id: str):
        with self.lock:
            s = self.sessions.get(game_id)
        if s is not None:
            return s
        summary = self.storage.archived().get(game_id)
        if summary is not None:
            return ArchivedGame(self, game_id, summary)
        raise ApiError(404, f"no game {game_id!r}")

    def list_games(self) -> list[dict]:
        with self.lock:
            live = list(self.sessions.values())
        out = [s.summary() for s in live]
        ids = {s["game_id"] for s in out}
        out += [dict(v) for k, v in self.storage.archived().items() if k not in ids]
        out.sort(key=lambda s: s.get("created", 0), reverse=True)
        return out

    # ------------------------------------------------------------ creation
    def create_game(self, body: dict) -> GameSession:
        opts = parse_game_options(body)
        return self._create(opts)

    def _create(self, opts: dict) -> GameSession:
        if self.stopping:
            raise ApiError(503, "server is shutting down")
        gid = self._next_id()
        session = GameSession(self, gid, opts)
        with self.lock:
            self.sessions[gid] = session
        session.launch()
        log.info("created game %s (%s)", gid, session.name)
        return session

    def quickmatch(self, body: dict) -> tuple[GameSession, Seat]:
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        name = validate_player_name(body.get("name"))
        players = _number(body, "players", 6, 1, C.MAX_PLAYERS, integer=True)
        turn_timeout = _turn_timeout(body, 30.0)
        max_turns = _number(body, "max_turns", C.DEFAULT_MAX_TURNS, 1, 1000, integer=True)
        lobby_timeout = _number(body, "lobby_timeout", DEFAULT_QUICKMATCH_LOBBY, 0, MAX_TIMEOUT)
        fill = _bool(body, "fill_with_bots", True)
        with self.lock:
            for s in self.sessions.values():
                if (s.opts["quickmatch"] and s.status == "lobby" and s.max_players == players
                        and s.opts["turn_timeout"] == turn_timeout and s.opts["max_turns"] == max_turns):
                    try:
                        return s, s.join(name)
                    except ApiError:
                        continue  # full, started meanwhile, or name taken: try the next lobby
            self._quickmatch_counter += 1
            opts = parse_game_options({
                "name": f"Quickmatch #{self._quickmatch_counter}",
                "max_players": players, "min_players": min(2, players),
                "turn_timeout": turn_timeout, "max_turns": max_turns,
                "fill_with_bots": fill, "lobby_timeout": lobby_timeout or None,
            })
            opts["quickmatch"] = True
            session = self._create(opts)
            return session, session.join(name)

    # ------------------------------------------------------------ misc
    def leaderboard(self) -> list[dict]:
        return self.storage.leaderboard()

    def shutdown(self) -> None:
        self.stopping = True
        with self.lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            s.notify()
