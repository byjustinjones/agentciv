"""Provenance: agent manifests and the per-turn action log (docs/DESIGN.md §12).

**Agent manifest.** ``POST /join`` and ``/quickmatch`` take an optional
``agent`` object describing the system behind a seat (model, harness,
prompt hash, tools, memory...). It is stored per seat, shown in the game
summary and the replay, and never used for matchmaking or rating. Strings
only, a fixed set of keys, each value size-capped (:data:`AGENT_FIELDS`).

**Action log.** What every seat did each turn: its submitted orders (the
last submission, as sent), every rejected order with the reason, each
diplomacy action with its result, and, when the turn ran out, which remote
seats missed the deadline. One plain-JSON entry per turn::

    {"turn": 12, "end": "all_ready" | "deadline",
     "orders": {"p1": {"orders": [...], "submissions": 2, "ready": true, "t": 41.3,
                       "rejected": [{"submission": 1, "order": {...}, "error": "..."}]}},
     "diplomacy": [{"by": "p2", "t": 3.1, "action": {...}, "result": {...}}],
     "missed": {"p3": "no_orders", "p4": "draft"}}

``t`` is seconds after the turn started (wall clock; after a server restart
counted from the restart). Finished turns are kept as zlib-compressed JSON
(cheap to checkpoint); the replay carries the log as the top-level key
``actions`` = ``{"format": 1, "turns": [...]}``.
"""
from __future__ import annotations

import json
import re
import zlib

from .replay import dumps

# ---------------------------------------------------------------- agent manifest
AGENT_FIELDS = {          # key -> max characters
    "model": 120,
    "model_version": 120,
    "effort": 40,
    "harness": 120,
    "harness_version": 80,
    "prompt_sha256": 64,
    "tools": 400,
    "memory": 200,
    "notes": 1000,
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    pass


def validate_agent(obj) -> dict | None:
    """Normalise an ``agent`` manifest; raises :class:`ManifestError` with a
    message for the client. ``None`` / ``{}`` / only empty values -> None."""
    if obj is None:
        return None
    if not isinstance(obj, dict):
        raise ManifestError("agent must be an object of strings, e.g. {\"model\": \"...\", \"harness\": \"...\"}")
    unknown = sorted(str(k) for k in obj if k not in AGENT_FIELDS)
    if unknown:
        raise ManifestError(f"unknown agent fields {unknown}; allowed: {', '.join(AGENT_FIELDS)}")
    out = {}
    for key, cap in AGENT_FIELDS.items():
        v = obj.get(key)
        if v is None:
            continue
        if not isinstance(v, str):
            raise ManifestError(f"agent.{key} must be a string")
        v = v.strip() if key == "notes" else " ".join(v.split())
        if not v:
            continue
        if len(v) > cap:
            raise ManifestError(f"agent.{key} must be at most {cap} characters")
        if not v.replace("\n", "").replace("\t", "").isprintable():
            raise ManifestError(f"agent.{key} contains unprintable characters")
        if key == "prompt_sha256":
            v = v.lower()
            if not _SHA256.match(v):
                raise ManifestError("agent.prompt_sha256 must be 64 hex digits (a sha256 of your prompt)")
        out[key] = v
    return out or None


# ---------------------------------------------------------------- action log
LOG_FORMAT = 1
MAX_ITEM_BYTES = 2000        # a logged order/action larger than this is replaced by a size marker
MAX_ORDERS_LOGGED = 100      # orders kept per submission (C.MAX_ORDERS_PER_TURN)
MAX_REJECTED_LOGGED = 100    # rejected orders kept per seat and turn
MAX_DIPLOMACY_LOGGED = 300   # diplomacy actions kept per seat and turn


def _clip(item):
    """A JSON-safe copy of ``item``; oversized or unserialisable items become a marker."""
    try:
        raw = dumps(item)
    except (TypeError, ValueError):
        return {"unloggable": type(item).__name__}
    if len(raw) > MAX_ITEM_BYTES:
        return {"truncated": True, "bytes": len(raw)}
    return json.loads(raw)


class ActionLog:
    """Per-turn action log of one game session. Not thread-safe: the
    session calls it under its lock."""

    def __init__(self) -> None:
        self._done: list[bytes] = []      # zlib(JSON) of each finished turn, oldest first
        self._cur: dict | None = None     # the entry of the turn being played

    def __len__(self) -> int:
        return len(self._done) + (self._cur is not None)

    def _entry(self, turn: int) -> dict:
        cur = self._cur
        if cur is None or cur["turn"] != turn:
            if cur is not None:
                self._close()
            cur = self._cur = {"turn": turn, "orders": {}, "diplomacy": []}
        return cur

    def _close(self) -> None:
        self._done.append(zlib.compress(dumps(self._cur), 6))
        self._cur = None

    # ------------------------------------------------------------ recording
    def orders(self, turn: int, pid: str, orders: list, errors: list, ready: bool, t: float) -> None:
        e = self._entry(turn)
        rec = e["orders"].get(pid)
        if rec is None:
            rec = e["orders"][pid] = {"orders": [], "submissions": 0, "ready": True, "t": 0.0}
        rec["submissions"] += 1
        n = rec["submissions"]
        rec["orders"] = [_clip(o) for o in orders[:MAX_ORDERS_LOGGED]]
        if len(orders) > MAX_ORDERS_LOGGED:
            rec["orders_omitted"] = len(orders) - MAX_ORDERS_LOGGED
        else:
            rec.pop("orders_omitted", None)
        rec["ready"] = bool(ready)
        rec["t"] = round(max(0.0, t), 1)
        rejected = rec.setdefault("rejected", [])
        for err in errors:
            if len(rejected) >= MAX_REJECTED_LOGGED:
                rec["rejected_omitted"] = rec.get("rejected_omitted", 0) + 1
                continue
            i = err.get("index", -1)
            item = {"submission": n, "error": str(err.get("error", ""))[:500]}
            if isinstance(i, int) and 0 <= i < len(orders):
                item["order"] = _clip(orders[i])
            rejected.append(item)
        if not rejected:
            del rec["rejected"]

    def diplomacy(self, turn: int, pid: str, actions: list, results: list, t: float) -> None:
        e = self._entry(turn)
        log = e["diplomacy"]
        count = sum(1 for d in log if d.get("by") == pid)
        by_index = {r.get("index"): r for r in results if isinstance(r, dict)}
        whole = [r for r in results if isinstance(r, dict) and r.get("index", -1) == -1]
        items = list(enumerate(actions)) if actions else []
        if not items and whole:
            items = [(-1, None)]
        for i, action in items:
            if count >= MAX_DIPLOMACY_LOGGED:
                omitted = e.setdefault("diplomacy_omitted", {})
                omitted[pid] = omitted.get(pid, 0) + 1
                continue
            count += 1
            r = by_index.get(i) or (whole[0] if whole else None)
            item = {"by": pid, "t": round(max(0.0, t), 1), "action": _clip(action)}
            if r is not None:
                item["result"] = _clip({k: v for k, v in r.items() if k != "index"})
            log.append(item)

    def end_turn(self, turn: int, end: str, missed: dict[str, str]) -> None:
        """The turn resolved: how it ended and which remote seats missed it."""
        e = self._entry(turn)
        e["end"] = end
        if missed:
            e["missed"] = dict(missed)
        self._close()

    # ------------------------------------------------------------ output
    def turn_blobs(self, lo: int | None = None, hi: int | None = None) -> list[bytes]:
        """JSON bytes of each logged turn (finished ones and the current one)
        with ``lo <= turn <= hi`` (inclusive; None = open)."""
        out = []
        for z in self._done:
            raw = zlib.decompress(z)
            if lo is None and hi is None:
                out.append(raw)
                continue
            turn = json.loads(raw)["turn"]
            if (lo is None or turn >= lo) and (hi is None or turn <= hi):
                out.append(raw)
        cur = self._cur
        if cur is not None and (lo is None or cur["turn"] >= lo) and (hi is None or cur["turn"] <= hi):
            out.append(dumps(cur))
        return out

    def to_bytes(self, lo: int | None = None, hi: int | None = None) -> bytes:
        """The replay's ``actions`` value (pre-serialised)."""
        return b'{"format":%d,"turns":[' % LOG_FORMAT + b",".join(self.turn_blobs(lo, hi)) + b"]}"

    # ------------------------------------------------------------ checkpoints
    def snapshot(self) -> dict:
        return {"format": LOG_FORMAT, "done": list(self._done),
                "cur": json.loads(dumps(self._cur)) if self._cur is not None else None}

    @classmethod
    def restore(cls, data) -> "ActionLog":
        """From :meth:`snapshot` output; anything else (an old checkpoint) -> an empty log."""
        log = cls()
        if isinstance(data, dict) and data.get("format") == LOG_FORMAT:
            log._done = [bytes(b) for b in data.get("done") or []]
            cur = data.get("cur")
            log._cur = cur if isinstance(cur, dict) and "turn" in cur else None
        return log

