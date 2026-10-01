"""On-disk persistence for the server: replays and the leaderboard.

Layout under ``data_dir``::

    replays/<game_id>.json   {"game_id","summary","result","actions"?,"frames":[...]}
    replays/index.json       {game_id: summary}   (rebuilt from the files if missing)
    leaderboard.json         {"format": 2, "players": {name: {"mu","sigma","games","wins","total_place"}},
                              "applied": [game ids already rated in this pool]}
    leaderboard_fog.json     the same for fog-of-war games (own pool)
    leaderboard_<track>.json the same for each evaluation track (tracks.py), plus "rules_sha256":
                             the rules hash pinned by the track's first game in this data dir
    names.json               {casefolded name: sha256(key)}   (names registered with a key)
    live/                    checkpoints of lobbies and running games (see persist.py)

All writes are atomic (write to a temp file, then ``os.replace``).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
from pathlib import Path

from .. import ratings
from .replay import envelope
from .tracks import valid_pool_name

log = logging.getLogger("agentciv.server")

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _dumps(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


class RulesChanged(Exception):
    """A track pool is pinned to other rules than the server's (see :meth:`Storage.pin_rules`)."""

    def __init__(self, pool: str, pinned: str):
        super().__init__(pool, pinned)
        self.pool = pool
        self.pinned = pinned


class _Pool:
    """One rating pool: players, the ledger of applied game ids, its file
    and (track pools) the pinned rules hash."""

    __slots__ = ("table", "applied", "path", "rules_sha256")

    def __init__(self, table: dict, applied: set, path: Path, rules_sha256: str | None = None):
        self.table, self.applied, self.path, self.rules_sha256 = table, applied, path, rules_sha256


class Storage:
    """Replays and leaderboard persistence. Thread-safe."""

    def __init__(self, data_dir: str | os.PathLike):
        self.root = Path(data_dir)
        self.replay_dir = self.root / "replays"
        self.index_path = self.replay_dir / "index.json"
        self.leaderboard_path = self.root / "leaderboard.json"
        self.fog_leaderboard_path = self.root / "leaderboard_fog.json"   # fog-of-war games (own pool)
        self.names_path = self.root / "names.json"
        self._lock = threading.Lock()
        self._pool_lock = threading.Lock()  # loading a track pool file (never held with _lock)
        self._write_lock = threading.Lock()  # serialises replay-file writes (outside the index lock)
        self.replay_dir.mkdir(parents=True, exist_ok=True)
        self.index: dict[str, dict] = self._load_index()
        self._legacy: set[Path] = set()  # flat (pre-format-2) leaderboard files, backed up before the first rewrite
        self._pools: dict[str, _Pool] = {
            "standard": self._load_pool(self.leaderboard_path),
            "fog": self._load_pool(self.fog_leaderboard_path),
        }
        self.claims: dict[str, str] = self._load_json(self.names_path, {})

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _load_json(path: Path, default):
        try:
            with open(path, "rb") as f:
                data = json.load(f)
            return data if isinstance(data, type(default)) else default
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as e:
            log.warning("could not read %s (%s); starting fresh", path, e)
            return default

    def _load_pool(self, path: Path, track: bool = False) -> _Pool:
        """Players, applied game ids (and pinned rules hash) of one leaderboard
        file. A legacy flat file ({name: entry}, before format 2) or a missing
        open-ladder file gets every archived game in its ledger: those were
        rated (or not) by the old code. The file is rewritten in format 2 on
        the next rated game. A missing track file is an empty pool (tracks
        are newer than format 2)."""
        data = self._load_json(path, {})
        if isinstance(data.get("format"), int):
            players = data.get("players")
            applied = data.get("applied")
            pin = data.get("rules_sha256")
            return _Pool(players if isinstance(players, dict) else {},
                         {g for g in applied if isinstance(g, str)} if isinstance(applied, list) else set(),
                         path, pin if isinstance(pin, str) else None)
        if track:
            return _Pool({}, set(), path)
        if data:
            self._legacy.add(path)
        return _Pool(data, set(self.index), path)

    def _load_index(self) -> dict:
        index = self._load_json(self.index_path, {})
        changed = False
        for p in sorted(self.replay_dir.glob("*.json")):
            gid = p.stem
            if p.name == "index.json" or gid in index or not _SAFE_ID.match(gid):
                continue
            data = self._load_json(p, {})
            summary = data.get("summary") if isinstance(data, dict) else None
            if isinstance(summary, dict):
                index[gid] = summary
                changed = True
        # drop index entries whose replay file vanished
        for gid in [g for g in index if not (self.replay_dir / f"{g}.json").exists()]:
            del index[gid]
            changed = True
        if changed:
            _atomic_write(self.index_path, _dumps(index))
        return index

    # ------------------------------------------------------------ replays
    def replay_path(self, game_id: str) -> Path | None:
        if not _SAFE_ID.match(game_id or ""):
            return None
        return self.replay_dir / f"{game_id}.json"

    def save_replay(self, game_id: str, summary: dict, result, frames: list[bytes],
                    actions: bytes | None = None) -> None:
        """Write the replay file (frames and the action log are pre-serialised
        JSON bytes), built by the same :func:`.replay.envelope` that serves a
        live game's replay, so both are byte-identical."""
        path = self.replay_path(game_id)
        if path is None:
            return
        body = envelope(game_id, summary, result, frames, actions=actions)
        with self._write_lock:  # the (large) replay file is written without blocking index lookups
            _atomic_write(path, body)
            with self._lock:
                self.index[game_id] = summary
                index_bytes = _dumps(self.index)
            _atomic_write(self.index_path, index_bytes)

    def read_replay(self, game_id: str) -> bytes | None:
        path = self.replay_path(game_id)
        if path is None:
            return None
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    def archived(self) -> dict[str, dict]:
        with self._lock:
            return dict(self.index)

    def summary(self, game_id: str) -> dict | None:
        """One archived game's summary (no copy of the whole index)."""
        with self._lock:
            s = self.index.get(game_id)
            return dict(s) if s is not None else None

    def recent(self, limit: int, exclude: set | frozenset = frozenset()) -> list[dict]:
        """The ``limit`` most recently created archived summaries, newest first."""
        with self._lock:
            items = [v for k, v in self.index.items() if k not in exclude]
        items.sort(key=lambda s: s.get("created", 0) if isinstance(s, dict) else 0, reverse=True)
        return [dict(v) for v in items[:max(0, limit)]]

    def archived_ids(self) -> list[str]:
        with self._lock:
            return list(self.index)

    # ------------------------------------------------------------ registered names
    @staticmethod
    def _hash_key(key: str) -> str:
        return hashlib.sha256(("agentciv-name-key:" + key).encode()).hexdigest()

    def check_name(self, name: str, key: str | None) -> bool:
        """Name registration. A name registered with a key can only be used
        with that key (else ValueError). An unregistered name used with a key
        gets registered. Returns True when the seat is key-verified."""
        folded = name.casefold()
        with self._lock:
            stored = self.claims.get(folded)
            if stored is None:
                if key is None:
                    return False
                self.claims[folded] = self._hash_key(key)
                _atomic_write(self.names_path, _dumps(self.claims))
                return True
        if key is None or not hmac.compare_digest(stored, self._hash_key(key)):
            raise ValueError(name)
        return True

    def is_registered(self, name: str) -> bool:
        with self._lock:
            return name.casefold() in self.claims

    # ------------------------------------------------------------ leaderboard
    @property
    def table(self) -> dict:
        """Players of the ``standard`` pool."""
        return self._pools["standard"].table

    @property
    def fog_table(self) -> dict:
        """Players of the ``fog`` pool."""
        return self._pools["fog"].table

    def _pool(self, pool: str) -> _Pool:
        """The pool ``standard``, ``fog`` or a track id (loaded on first use; under or outside the lock)."""
        p = self._pools.get(pool)
        if p is not None:
            return p
        if not valid_pool_name(pool):
            raise ValueError(f"unknown leaderboard pool {pool!r}")
        with self._pool_lock:
            p = self._pools.get(pool)
            if p is None:
                p = self._pools[pool] = self._load_pool(self.root / f"leaderboard_{pool}.json", track=True)
        return p

    def is_applied(self, game_id: str, pool: str = "standard") -> bool:
        """Has ``game_id`` already changed the ``pool`` ratings?"""
        p = self._pool(pool)
        with self._lock:
            return game_id in p.applied

    def pinned_rules(self, pool: str) -> str | None:
        """The rules hash a track pool is pinned to (None: not used yet, or an open ladder)."""
        p = self._pool(pool)
        with self._lock:
            return p.rules_sha256

    def pin_rules(self, pool: str, sha: str) -> None:
        """A track game is about to be created under rules ``sha``: the first
        one pins the pool to it (written at once); later ones must match, else
        :class:`RulesChanged` (the operator defines a new track version)."""
        p = self._pool(pool)
        with self._lock:
            if p.rules_sha256 is None:
                self._write_pool(p, p.table, p.applied, sha)
                p.rules_sha256 = sha
            elif p.rules_sha256 != sha:
                raise RulesChanged(pool, p.rules_sha256)

    def _write_pool(self, p: _Pool, players: dict, applied, pin: str | None) -> None:
        doc = {"format": 2, "players": players, "applied": sorted(applied)}
        if pin is not None:
            doc["rules_sha256"] = pin
        self._migrate_backup(p.path)
        _atomic_write(p.path, json.dumps(doc, indent=1, sort_keys=True).encode())

    def record_result(self, game_id: str, placements: list[str], ranks: list[int] | None = None,
                      pool: str = "standard") -> bool:
        """Rate one game: ``placements`` holds one rating name per seat (winner
        first; a name may repeat, see :func:`ratings.update`), ``ranks``
        (optional, 1 = best, equal = tie). ``pool``: ``standard``
        (leaderboard.json), ``fog`` (leaderboard_fog.json, fog-of-war games)
        or a track id (leaderboard_<id>.json).

        Idempotent: a ``game_id`` already in the pool's ledger changes nothing.
        The ratings and the ledger are written together in one atomic file and
        the in-memory table changes only once that write succeeded, so a failed
        write (OSError, raised) can simply be retried. Fewer than two distinct
        names is ignored. Returns True if the ratings changed."""
        p = self._pool(pool)
        if ranks is None:
            ranks = list(range(1, len(placements) + 1))
        if len(set(placements)) < 2:
            return False
        with self._lock:
            table, applied = p.table, p.applied
            if game_id in applied:
                return False
            work = {name: dict(table[name]) for name in placements if name in table}
            ratings.update(work, list(placements), list(ranks))
            self._write_pool(p, {**table, **work}, applied | {game_id}, p.rules_sha256)
            table.update(work)
            applied.add(game_id)
            return True

    def _migrate_backup(self, path: Path) -> None:
        """Before the first format-2 write over a legacy flat file, keep a copy of it."""
        if path not in self._legacy:
            return
        backup = path.with_name(path.name + ".v1.bak")
        if not backup.exists():
            with open(path, "rb") as f:
                _atomic_write(backup, f.read())
        self._legacy.discard(path)

    def leaderboard(self, pool: str = "standard") -> list[dict]:
        p = self._pool(pool)
        with self._lock:
            rows = ratings.leaderboard(p.table)
            for r in rows:  # registered names can only be played with their key (see check_name)
                r["verified"] = r["name"].casefold() in self.claims
            return rows
