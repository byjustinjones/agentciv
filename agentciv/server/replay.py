"""Replay frame storage and the compact replay format.

Frames are spectator views, one per turn (``frames[k].turn == k``). A live
game keeps them in memory zlib-compressed (a 150-turn, 8-player game is a
few MB of JSON but ~0.5 MB compressed); the replay file on disk keeps the
plain ``{"game_id","summary","result","actions","frames"}`` format of
docs/DESIGN.md §12 (``actions``: the action log of :mod:`.provenance`; absent
in replays saved before it existed).

The *compact* format (``GET /replay?compact=1``) is what the GUI loads: each
frame drops the parts that never or rarely change and are large —

* ``costs`` (the rule tables) and ``map.terrain`` move to a shared
  ``static`` object and are omitted from a frame when equal to it;
* ``market.history`` is dropped (it is the last N turns of
  ``market.prices``, which every frame still carries).

Response: ``{"game_id","summary","result","compact":true,"total_frames",
"from","to","static":{"terrain","costs"},"frames":[...]}``; restore a frame
with ``frame.map.terrain ??= static.terrain`` and
``frame.costs ??= static.costs``.
"""
from __future__ import annotations

import json
import zlib

ZLEVEL = 6


def dumps(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


def static_parts(view: dict) -> dict:
    return {"terrain": (view.get("map") or {}).get("terrain"), "costs": view.get("costs")}


def compact_view(view: dict, static: dict) -> dict:
    """A shallow-copied view without the static/derivable parts (see module doc)."""
    out = dict(view)
    if out.get("costs") == static.get("costs"):
        out.pop("costs", None)
    m = out.get("map")
    if isinstance(m, dict):
        m = dict(m)
        if m.get("terrain") == static.get("terrain"):
            m.pop("terrain", None)
        out["map"] = m
    mk = out.get("market")
    if isinstance(mk, dict) and "history" in mk:
        mk = dict(mk)
        mk.pop("history", None)
        out["market"] = mk
    return out


def _range(n: int, lo: int | None, hi: int | None) -> tuple[int, int]:
    """Inclusive frame-index range clamped to ``[0, n-1]`` (empty if n == 0)."""
    lo = 0 if lo is None else max(0, lo)
    hi = n - 1 if hi is None else min(n - 1, hi)
    return lo, hi


def envelope(game_id: str, summary: dict, result, frames: list[bytes], *, compact: bool = False,
             static: dict | None = None, total: int | None = None, lo: int = 0,
             actions: bytes | None = None) -> bytes:
    """Assemble a replay response (and the saved replay file) from
    pre-serialised frame bytes. ``actions``: the pre-serialised action log
    (:mod:`.provenance`), placed just before the frames; None = no such key."""
    head: dict = {"game_id": game_id, "summary": summary, "result": result}
    if compact or total is not None:
        head.update({"compact": compact, "total_frames": total if total is not None else len(frames),
                     "from": lo, "to": lo + len(frames) - 1})
    if compact:
        head["static"] = static or {}
    out = dumps(head)[:-1]
    if actions is not None:
        out += b',"actions":' + actions
    return out + b',"frames":[' + b",".join(frames) + b"]}"


class FrameStore:
    """Append-only list of frames, kept zlib-compressed in memory. Not
    thread-safe: the owning session locks around it.

    Each frame has a *full* spectator view (everything, for the saved replay
    and for finished games) and a *public* one (what live spectators may see
    while the game runs: no private messages, trade offers, treaty proposals
    or private events). The public variant shares the full blob when the two
    are equal; the compact format is stored for the public variant and built
    on the fly for the full one."""

    def __init__(self) -> None:
        self._full: list[bytes] = []
        self._public: list[bytes] = []
        self._compact: list[bytes] = []      # compact form of the public frame
        self.static: dict | None = None
        self.raw_bytes = 0  # uncompressed size of the full frames (for stats/tests)

    def __len__(self) -> int:
        return len(self._full)

    def append(self, view_bytes: bytes, public_bytes: bytes | None = None) -> None:
        pub = view_bytes if public_bytes is None else public_bytes
        view = json.loads(pub)
        if self.static is None:
            self.static = static_parts(view)
        zfull = zlib.compress(view_bytes, ZLEVEL)
        self._full.append(zfull)
        self._public.append(zfull if pub == view_bytes else zlib.compress(pub, ZLEVEL))
        self._compact.append(zlib.compress(dumps(compact_view(view, self.static)), ZLEVEL))
        self.raw_bytes += len(view_bytes)

    def append_blobs(self, zfull: bytes, zpublic: bytes | None = None) -> None:
        """Append a frame from its compressed blobs (as returned by
        :meth:`last_blobs`; ``zpublic`` None = same as the full view)."""
        pub = zlib.decompress(zpublic if zpublic is not None else zfull)
        view = json.loads(pub)
        if self.static is None:
            self.static = static_parts(view)
        self._full.append(zfull)
        self._public.append(zfull if zpublic is None else zpublic)
        self._compact.append(zlib.compress(dumps(compact_view(view, self.static)), ZLEVEL))
        self.raw_bytes += len(pub) if zpublic is None else len(zlib.decompress(zfull))

    def last_blobs(self) -> tuple[bytes, bytes | None]:
        """Compressed (full, public) blobs of the newest frame; public is None
        when it equals the full view (used to persist frames of live games)."""
        full, public = self._full[-1], self._public[-1]
        return full, (None if public is full else public)

    def memory_bytes(self) -> int:
        extra = sum(len(p) for f, p in zip(self._full, self._public) if p is not f)
        return sum(map(len, self._full)) + sum(map(len, self._compact)) + extra

    def full(self, lo: int | None = None, hi: int | None = None, public: bool = False) -> list[bytes]:
        blobs = self._public if public else self._full
        lo, hi = _range(len(blobs), lo, hi)
        return [zlib.decompress(b) for b in blobs[lo:hi + 1]]

    def compact(self, lo: int | None = None, hi: int | None = None, public: bool = False) -> list[bytes]:
        lo, hi = _range(len(self._compact), lo, hi)
        out = []
        for k in range(lo, hi + 1):
            if public or self._public[k] is self._full[k]:
                out.append(zlib.decompress(self._compact[k]))
            else:  # full compact frame: rare (a finished game whose replay is not on disk yet)
                view = json.loads(zlib.decompress(self._full[k]))
                out.append(dumps(compact_view(view, self.static or {})))
        return out

    def all_full(self) -> list[bytes]:
        return self.full()


class ArchivedReplay:
    """A replay file parsed once (cached by the manager): compact frames,
    the static parts and the last frame, for fast range/compact/state reads."""

    def __init__(self, data: bytes):
        doc = json.loads(data)
        frames = doc.get("frames") or []
        self.summary = doc.get("summary") or {}
        self.result = doc.get("result")
        self.game_id = doc.get("game_id")
        self.static = static_parts(frames[0]) if frames else {}
        self.compact_frames = [dumps(compact_view(f, self.static)) for f in frames]
        self.last_frame = dumps(frames[-1]) if frames else None
        self.size = len(data)

    def __len__(self) -> int:
        return len(self.compact_frames)

    def compact(self, lo: int | None = None, hi: int | None = None) -> list[bytes]:
        lo, hi = _range(len(self.compact_frames), lo, hi)
        return self.compact_frames[lo:hi + 1]


def slice_actions(actions, lo: int | None, hi: int | None):
    """A replay ``actions`` value restricted to turns ``lo..hi`` (inclusive)."""
    if not isinstance(actions, dict) or not isinstance(actions.get("turns"), list):
        return actions
    turns = [e for e in actions["turns"] if isinstance(e, dict)
             and (lo is None or e.get("turn", 0) >= lo) and (hi is None or e.get("turn", 0) <= hi)]
    return {**actions, "turns": turns}


def slice_full_replay(data: bytes, lo: int | None, hi: int | None) -> bytes:
    """A frame range of a full replay file (re-serialised)."""
    doc = json.loads(data)
    frames = doc.get("frames") or []
    a, b = _range(len(frames), lo, hi)
    actions = doc.get("actions")
    return envelope(doc.get("game_id"), doc.get("summary") or {}, doc.get("result"),
                    [dumps(f) for f in frames[a:b + 1]], total=len(frames), lo=a,
                    actions=None if actions is None else dumps(slice_actions(actions, a, b)))
