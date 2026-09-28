"""On-disk persistence for the server: replays and the leaderboard.

Layout under ``data_dir``::

    replays/<game_id>.json   {"game_id","summary","result","frames":[...]}
    replays/index.json       {game_id: summary}   (rebuilt from the files if missing)
    leaderboard.json         {name: {"mu","sigma","games","wins","total_place"}}

All writes are atomic (write to a temp file, then ``os.replace``).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

from .. import ratings

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


class Storage:
    """Replays and leaderboard persistence. Thread-safe."""

    def __init__(self, data_dir: str | os.PathLike):
        self.root = Path(data_dir)
        self.replay_dir = self.root / "replays"
        self.index_path = self.replay_dir / "index.json"
        self.leaderboard_path = self.root / "leaderboard.json"
        self._lock = threading.Lock()
        self.replay_dir.mkdir(parents=True, exist_ok=True)
        self.index: dict[str, dict] = self._load_index()
        self.table: dict[str, dict] = self._load_json(self.leaderboard_path, {})

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

    def save_replay(self, game_id: str, summary: dict, result, frames: list[bytes]) -> None:
        """Write the replay file (frames are pre-serialised JSON bytes)."""
        path = self.replay_path(game_id)
        if path is None:
            return
        head = _dumps({"game_id": game_id, "summary": summary, "result": result})
        body = head[:-1] + b',"frames":[' + b",".join(frames) + b"]}"
        with self._lock:
            _atomic_write(path, body)
            self.index[game_id] = summary
            _atomic_write(self.index_path, _dumps(self.index))

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

    # ------------------------------------------------------------ leaderboard
    def record_result(self, placements: list[str]) -> None:
        """Update ratings from an ordered list of player names (winner first).
        Duplicate names keep their best placement; < 2 names is ignored."""
        seen: list[str] = []
        for name in placements:
            if name not in seen:
                seen.append(name)
        if len(seen) < 2:
            return
        with self._lock:
            ratings.update(self.table, seen)
            _atomic_write(self.leaderboard_path, json.dumps(self.table, indent=1, sort_keys=True).encode())

    def leaderboard(self) -> list[dict]:
        with self._lock:
            return ratings.leaderboard(self.table)
