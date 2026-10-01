"""Checkpoints of live games, so lobbies and running games survive a server restart.

Layout under ``data_dir``::

    live/<game_id>.pkl      pickled plain-data snapshot of the session (see
                            GameSession.snapshot_state): options, seats and tokens,
                            the engine Game object, house-bot state, turn bookkeeping.
                            Rewritten atomically (temp file + os.replace).
    live/<game_id>.frames   the replay frames recorded so far, appended once each
                            (never re-pickled): a sequence of records
                            ``>II`` header (len_full, len_public) + zlib(full view)
                            + zlib(public view); ``len_public == 0`` means the public
                            view equals the full one.
    live/corrupt/           unreadable checkpoints are moved here (never fatal).

The snapshot records how many frames belong to it; frames are appended before the
snapshot that counts them is written, so a crash in between leaves at most a few
extra records, which the next restore ignores (and the next append overwrites).

Checkpoints are pickles: only point a server at a data directory you trust.
"""
from __future__ import annotations

import logging
import os
import pickle
import re
import struct
import threading
import time
from pathlib import Path

log = logging.getLogger("agentciv.server")

FORMAT = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEADER = struct.Struct(">II")


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class LiveStore:
    """Reads and writes ``<data_dir>/live``. Writes for one game must be
    serialised by the caller (each GameSession has its own checkpoint lock)."""

    def __init__(self, root: str | os.PathLike):
        self.dir = Path(root)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ paths
    def state_path(self, game_id: str) -> Path:
        if not _SAFE_ID.match(game_id or ""):
            raise ValueError(f"unsafe game id {game_id!r}")
        return self.dir / f"{game_id}.pkl"

    def frames_path(self, game_id: str) -> Path:
        return self.state_path(game_id).with_suffix(".frames")

    def ids(self) -> list[str]:
        """Game ids with a snapshot on disk."""
        return sorted(p.stem for p in self.dir.glob("*.pkl") if _SAFE_ID.match(p.stem))

    # ------------------------------------------------------------ writing
    def write_state(self, game_id: str, blob: bytes) -> None:
        _atomic_write(self.state_path(game_id), blob)

    def append_frames(self, game_id: str, frames: list[tuple[bytes, bytes | None]], good_size: int) -> int:
        """Append compressed frames after the first ``good_size`` bytes of the
        frames file (anything beyond it, e.g. a torn record, is overwritten).
        Returns the new good size."""
        path = self.frames_path(game_id)
        buf = bytearray()
        for full, public in frames:
            pub = b"" if public is None or public is full else public
            buf += _HEADER.pack(len(full), len(pub))
            buf += full
            buf += pub
        with open(path, "r+b" if path.exists() else "wb") as f:
            f.seek(good_size)
            f.truncate()
            f.write(buf)
        return good_size + len(buf)

    def delete(self, game_id: str) -> None:
        for p in (self.state_path(game_id), self.frames_path(game_id)):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------ reading
    def read_frames(self, game_id: str) -> tuple[list[tuple[bytes, bytes | None]], list[int]]:
        """``(records, ends)``: the complete frame records and the file offset
        after each (a torn trailing record is ignored)."""
        try:
            data = self.frames_path(game_id).read_bytes()
        except FileNotFoundError:
            return [], []
        records: list[tuple[bytes, bytes | None]] = []
        ends: list[int] = []
        pos, n = 0, len(data)
        while pos + _HEADER.size <= n:
            lf, lp = _HEADER.unpack_from(data, pos)
            end = pos + _HEADER.size + lf + lp
            if lf == 0 or end > n:
                break
            full = data[pos + _HEADER.size: pos + _HEADER.size + lf]
            pub = data[pos + _HEADER.size + lf: end] if lp else None
            records.append((full, pub))
            ends.append(end)
            pos = end
        if pos != n:
            log.warning("live/%s.frames: ignoring %d trailing bytes (torn write)", game_id, n - pos)
        return records, ends

    def load_state(self, game_id: str) -> dict:
        with open(self.state_path(game_id), "rb") as f:
            state = pickle.load(f)
        if not isinstance(state, dict) or state.get("format") != FORMAT or state.get("game_id") != game_id:
            raise ValueError("not an AgentCiv live-game checkpoint (or an unsupported format)")
        return state

    def move_aside(self, game_id: str, reason: str) -> None:
        """Move a game's unreadable checkpoint files to ``live/corrupt/``."""
        dest = self.dir / "corrupt"
        dest.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for p in (self.dir / f"{game_id}.pkl", self.dir / f"{game_id}.frames"):
            if p.exists():
                try:
                    os.replace(p, dest / f"{p.stem}.{stamp}{p.suffix}")
                except OSError:
                    log.exception("could not move %s aside", p)
        log.error("live game %s: checkpoint unusable (%s); moved to %s", game_id, reason, dest)

    def cleanup(self) -> None:
        """Remove stale temp files and move orphan frame files aside."""
        for p in self.dir.glob(".*.tmp"):
            try:
                p.unlink()
            except OSError:
                pass
        for p in self.dir.glob("*.frames"):
            if not (self.dir / f"{p.stem}.pkl").exists():
                self.move_aside(p.stem, "frames without a snapshot")
