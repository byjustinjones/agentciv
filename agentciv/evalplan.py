"""Paired evaluation on a frozen track (docs/EVALUATION.md).

The open leaderboards mix conditions, and a placement depends on the
opponents and the start seat. This tool plans games on a track so that every
model plays every seat on every map seed, creates them on a server, and
reports the results with bootstrap confidence intervals, paired differences
on matched seed and seat, per-seat means (positional bias) and provenance
warnings. Standard library only.

    python -m agentciv.evalplan plan --track eval-6p-fog-v1 --models A,B,C,D,E,F --seeds 3 -o plan.json
    python -m agentciv.evalplan create --plan plan.json --url http://host:8765 --spectator-key KEY
    python -m agentciv.evalplan report --plan plan.json --url http://host:8765 [--json out.json]
    python -m agentciv.evalplan report --data-dir data --games g8,g9,g10

Conventions (also in docs/EVALUATION.md):

* A *model* is a seat's real player name (the name it joined under); a
  ``#k`` suffix (``A#2``, a repeated model filling a seat) is stripped.
* *Placement* uses average ranks for ties (two seats tied for 2nd both get
  2.5), so every game's placements sum to the same total. A *win* is first
  place; a tie for first splits the win (two seats tied: 0.5 each).
* Seat = player id (``p1`` is seat 1). Seed = the game's map seed.
* Confidence intervals: percentile bootstrap over the observations (games
  for a model, matched pairs for a difference), seeded RNG: the same input
  gives the same report.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import secrets
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from itertools import combinations
from pathlib import Path

PLAN_FORMAT = 1
DEFAULT_BOOTSTRAP = 2000
_SUFFIX = re.compile(r"#\d+$")
_FALLBACK = re.compile(r"fallbacks?\s*(?:on|enabled|used|active)|other[- ]model", re.IGNORECASE)


class EvalError(Exception):
    """A user-facing error (bad arguments, unreadable plan, server refusal)."""


def model_of(name: str) -> str:
    """The model label of a seat name: ``A#2`` -> ``A``."""
    return _SUFFIX.sub("", str(name))


# ================================================================ plan
def rotation_shifts(n: int, k: int | None) -> list[int]:
    """``k`` evenly spaced cyclic shifts of ``n`` seats (all ``n`` when k is None)."""
    if k is None or k >= n:
        return list(range(n))
    if k < 1:
        raise EvalError("--rotations must be at least 1")
    return sorted({(i * n) // k for i in range(k)})


def make_plan(track_id: str, models: list[str], seeds: int, rotations: int | None = None,
              seed_base: int = 1, allow_repeats: bool = False, seats: int | None = None) -> dict:
    """The schedule: for each seed, cyclic rotations of the model list over
    the seats, so with full rotation every model plays every seat on every
    seed. Each game carries the exact ``POST /api/games`` body."""
    from .server.manager import ApiError, validate_player_name
    from .server.tracks import SEAT_NAME, get_track

    track = get_track(track_id)
    if seats is None:
        if track is None:
            raise EvalError(f"unknown track {track_id!r} (pass --seats for a track this checkout does not define)")
        seats = track.players
    models = [m.strip() for m in models if m and m.strip()]
    if not models:
        raise EvalError("no models given")
    for m in models:
        try:
            validate_player_name(m)
        except ApiError as e:
            raise EvalError(f"model {m!r}: {e.message} (models are the names the seats join under)") from None
        if SEAT_NAME.match(m) or _SUFFIX.search(m):
            raise EvalError(f"model {m!r}: names like 'Player N' and a '#k' suffix are reserved")
    if len({m.lower() for m in models}) != len(models):
        raise EvalError("models must be distinct")
    if len(models) > seats:
        raise EvalError(f"{len(models)} models but {seats} seats: split them into several plans (fields)")
    lineup = list(models)
    if len(models) < seats:
        if not allow_repeats:
            raise EvalError(f"{len(models)} models for {seats} seats: pass --allow-repeats to fill the remaining "
                            "seats with repeated models (A#2, ...)")
        k = 0
        while len(lineup) < seats:
            base = models[k % len(models)]
            lineup.append(f"{base}#{k // len(models) + 2}")
            k += 1
    if seeds < 1:
        raise EvalError("--seeds must be at least 1")
    shifts = rotation_shifts(seats, rotations)
    games = []
    for si in range(seeds):
        seed = seed_base + si
        for r in shifts:
            order = [lineup[(j - r) % seats] for j in range(seats)]   # lineup[i] sits in seat (i + r) % n
            # The title is public (summaries, player views), so it must say nothing about the schedule: the
            # seed is hidden in live track games and the rotation tells a seat who sits where. Both stay
            # here, in the operator's plan file.
            body = {"track": track_id, "name": f"{track_id} {secrets.token_hex(4)}", "seed": seed,
                    "seats": order}
            games.append({"index": len(games), "seed": seed, "rotation": r, "seats": order, "body": body,
                          "game_id": None})
    # games are created in a random order, so a game id does not give away its place in the schedule either
    create_order = list(range(len(games)))
    secrets.SystemRandom().shuffle(create_order)
    return {"format": PLAN_FORMAT, "track": track_id, "seats": seats, "models": models, "lineup": lineup,
            "seeds": [seed_base + i for i in range(seeds)], "rotations": shifts, "games": games,
            "create_order": create_order}


def load_plan(path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
    except (OSError, ValueError) as e:
        raise EvalError(f"cannot read plan {path}: {e}") from None
    if not isinstance(plan, dict) or plan.get("format") != PLAN_FORMAT or not isinstance(plan.get("games"), list):
        raise EvalError(f"{path} is not an evalplan plan (format {PLAN_FORMAT})")
    return plan


def save_plan(plan: dict, path) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(plan, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ================================================================ HTTP
def _http(url: str, method: str = "GET", body=None, key: str | None = None, timeout: float = 30.0):
    """(status, parsed JSON) of one request; network errors raise EvalError."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-Spectator-Key"] = key
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"null")
        except ValueError:
            payload = None
        return e.code, payload
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise EvalError(f"{method} {url}: {e}") from None


def create_games(plan: dict, plan_path, url: str, key: str, count: int | None = 1, out=sys.stdout) -> list[dict]:
    """Create the next ``count`` (None = all) games of the plan that have no
    live id yet, recording each id in the plan file at once. A recorded id
    the server no longer knows (a lobby closed unfilled after an hour) is
    created again; the old id is kept in ``closed_ids``."""
    base = url.rstrip("/")
    made = []
    order = plan.get("create_order")
    if not (isinstance(order, list) and sorted(order) == list(range(len(plan["games"])))):
        order = range(len(plan["games"]))   # a plan written before create_order existed
    for g in (plan["games"][i] for i in order):
        if count is not None and len(made) >= count:
            break
        gid = g.get("game_id")
        if gid:
            status, summ = _http(f"{base}/api/games/{urllib.parse.quote(gid)}", key=key)
            if status != 404:
                continue
            g.setdefault("closed_ids", []).append(gid)
            print(f"game {gid} (plan #{g['index']}) is gone (lobby closed?): creating it again", file=out)
        status, res = _http(f"{base}/api/games", "POST", g["body"], key=key)
        if status != 200 or not isinstance(res, dict) or "game_id" not in res:
            msg = res.get("error") if isinstance(res, dict) else res
            raise EvalError(f"creating plan game #{g['index']} failed: HTTP {status}: {msg}")
        g["game_id"] = res["game_id"]
        save_plan(plan, plan_path)
        made.append(g)
        print(f"created {res['game_id']} (plan #{g['index']}, seed {g['seed']}, rotation {g['rotation']})", file=out)
        for k, name in enumerate(g["seats"]):
            print(f"  seat {k + 1} (p{k + 1}): {name}  -> POST {base}/api/games/{res['game_id']}/join "
                  f"{{\"name\": {json.dumps(name)}, \"agent\": {{...}}}}", file=out)
    left = sum(1 for g in plan["games"] if not g.get("game_id"))
    print(f"{len(made)} created; {left} not created yet", file=out)
    return made


# ================================================================ loading results
def _read_json(path: Path):
    try:
        with open(path, "rb") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class Source:
    """Where finished replays come from: a data dir (``<dir>/replays`` or the
    dir itself) or a server URL. :meth:`replay` returns the replay dict of a
    finished game, or a string saying why there is none."""

    def __init__(self, data_dir=None, url=None, key=None):
        self.url = url.rstrip("/") if url else None
        self.key = key
        self.dir = None
        if data_dir is not None:
            d = Path(data_dir)
            self.dir = d / "replays" if (d / "replays").is_dir() else d

    def ids(self, track: str | None = None) -> list[str]:
        """Every finished game id available (of ``track`` if given)."""
        out = []
        if self.dir is not None:
            for p in sorted(self.dir.glob("*.json"), key=lambda p: _natural(p.stem)):
                if p.name == "index.json" or p.name.startswith("."):
                    continue
                if track is not None:
                    doc = _read_json(p)
                    if not isinstance(doc, dict) or (doc.get("summary") or {}).get("track") != track:
                        continue
                out.append(p.stem)
        else:
            status, games = _http(f"{self.url}/api/games?limit=10000", key=self.key)
            if status != 200 or not isinstance(games, list):
                raise EvalError(f"GET /api/games failed: HTTP {status}")
            out = [g["game_id"] for g in games if isinstance(g, dict) and g.get("status") == "finished"
                   and (track is None or g.get("track") == track)]
            out.sort(key=_natural)
        return out

    def replay(self, gid: str):
        if self.dir is not None:
            p = self.dir / f"{gid}.json"
            if not p.exists():
                return "no replay file"
            doc = _read_json(p)
            if not isinstance(doc, dict):
                return "unreadable replay file"
        else:
            status, doc = _http(f"{self.url}/api/games/{urllib.parse.quote(gid)}/replay", key=self.key,
                                timeout=120.0)
            if status == 404:
                return "unknown game"
            if status != 200 or not isinstance(doc, dict):
                return f"HTTP {status}"
        summary = doc.get("summary") or {}
        if summary.get("status") != "finished" or not doc.get("result"):
            return f"not finished ({summary.get('status', '?')})"
        return doc


def _natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s))]


def placement_ranks(result: dict, final_players: dict) -> dict[str, float]:
    """pid -> average rank (ties share the mean of their positions), with the
    ties of :meth:`agentciv.engine.Game.placement_ranks`: same alive state,
    elimination turn and score; a winner by a victory condition ranks alone."""
    places = list(result.get("placements") or [])
    scores = result.get("scores") or {}
    keys = []
    for pid in places:
        p = final_players.get(pid) or {}
        keys.append((bool(p.get("alive", True)), p.get("eliminated_turn"), scores.get(pid)))
    groups: list[list[int]] = []
    for i in range(len(places)):
        alone = i == 1 and result.get("condition") not in (None, "score")
        if i == 0 or keys[i] != keys[i - 1] or alone:
            groups.append([i])
        else:
            groups[-1].append(i)
    out = {}
    for grp in groups:
        avg = sum(i + 1 for i in grp) / len(grp)
        for i in grp:
            out[places[i]] = avg
    return out


def game_rows(doc: dict) -> list[dict]:
    """One row per seat of a finished replay."""
    summary = doc.get("summary") or {}
    result = doc.get("result") or {}
    frames = doc.get("frames") or []
    final = {p.get("id"): p for p in (frames[-1].get("players") or [])} if frames else {}
    ranks = placement_ranks(result, final)
    best = min(ranks.values()) if ranks else None
    first = [pid for pid, r in ranks.items() if r == best]   # a tie for first splits the win
    scores = result.get("scores") or {}
    players = summary.get("players") or []
    models = sorted(model_of(p.get("name", p.get("id"))) for p in players)
    conditions = [len(players), bool(summary.get("fog")), bool(summary.get("sync")),
                  summary.get("negotiation_rounds"), summary.get("max_turns")]
    rows = []
    for p in players:
        pid = p.get("id")
        if pid not in ranks:
            continue
        m = re.fullmatch(r"p(\d+)", str(pid))
        rows.append({
            "game_id": doc.get("game_id") or summary.get("game_id"),
            "seed": summary.get("seed"),
            "seat": int(m.group(1)) if m else None,
            "pid": pid,
            "name": p.get("name"),
            "model": model_of(p.get("name", pid)),
            "seat_name": p.get("seat_name"),
            "agent": p.get("agent"),
            "place": ranks[pid],
            "win": (1.0 / len(first)) if pid in first else 0.0,
            "score": scores.get(pid),
            "field": models,
            "track": summary.get("track"),
            "rules_sha256": summary.get("rules_sha256"),
            "rules_changed": summary.get("rules_changed") or None,
            "conditions": conditions,
        })
    return rows


def comparable(r: dict) -> tuple:
    """What two seat rows must share to be a matched pair, besides seed and
    seat: the opponent field, the track, the rules (and no rule change
    mid-game), and the game conditions. Placing 1st against a weak field and
    6th against a strong one is not a paired difference."""
    return (tuple(r["field"]), r.get("track"), r.get("rules_sha256"), tuple(r.get("rules_changed") or ()),
            tuple(r.get("conditions") or ()))


# ================================================================ statistics
def mean(xs):
    return sum(xs) / len(xs) if xs else None


def bootstrap_ci(xs: list[float], label: str, rng_seed: int = 0, n: int = DEFAULT_BOOTSTRAP):
    """Percentile bootstrap 95% CI of the mean of ``xs`` ([lo, hi], or
    [None, None] with fewer than two values). The RNG is seeded by
    ``rng_seed`` and ``label``, so each statistic is reproducible on its own."""
    xs = [float(x) for x in xs if x is not None]
    if len(xs) < 2:
        return [None, None]
    rng = random.Random(f"{rng_seed}:{label}")
    k = len(xs)
    boots = sorted(sum(xs[rng.randrange(k)] for _ in range(k)) / k for _ in range(n))
    return [boots[int(math.floor(0.025 * (n - 1)))], boots[int(math.ceil(0.975 * (n - 1)))]]


def _stat(xs, label, rng_seed, nboot):
    xs = [x for x in xs if x is not None]
    return {"mean": mean(xs), "ci95": bootstrap_ci(xs, label, rng_seed, nboot), "n": len(xs)}


def analyse(rows: list[dict], rng_seed: int = 0, nboot: int = DEFAULT_BOOTSTRAP) -> dict:
    models = sorted({r["model"] for r in rows}, key=_natural)
    per_model = {}
    for m in models:
        mine = [r for r in rows if r["model"] == m]
        per_model[m] = {
            "games": len({r["game_id"] for r in mine}),
            "seats_played": len(mine),
            "placement": _stat([r["place"] for r in mine], f"place:{m}", rng_seed, nboot),
            "win_rate": _stat([r["win"] for r in mine], f"win:{m}", rng_seed, nboot),
            "score": _stat([r["score"] for r in mine], f"score:{m}", rng_seed, nboot),
        }
    # paired: same seed and seat in different games of one comparable group (the rotation puts each
    # model in each seat); games of different fields, tracks, rules or conditions are never paired
    cell: dict[tuple, dict[str, list[float]]] = {}
    for r in rows:
        if r["seed"] is None or r["seat"] is None:
            continue
        cell.setdefault((comparable(r), r["seed"], r["seat"]), {}).setdefault(r["model"], []).append(r["place"])
    groups = sorted({comparable(r) for r in rows}, key=repr)
    paired = []
    for a, b in combinations(models, 2):
        diffs = [mean(c[a]) - mean(c[b]) for c in cell.values() if a in c and b in c]
        st = _stat(diffs, f"pair:{a}:{b}", rng_seed, nboot)
        paired.append({"a": a, "b": b, "pairs": len(diffs), "mean_diff": st["mean"], "ci95": st["ci95"],
                       "a_better": sum(1 for d in diffs if d < 0), "b_better": sum(1 for d in diffs if d > 0)})
    seats = sorted({r["seat"] for r in rows if r["seat"] is not None})
    per_seat = {str(s): _stat([r["place"] for r in rows if r["seat"] == s], f"seat:{s}", rng_seed, nboot)
                for s in seats}
    fields = sorted({tuple(r["field"]) for r in rows})
    by_field = []
    if len(fields) > 1:
        for f in fields:
            for m in models:
                xs = [r["place"] for r in rows if r["model"] == m and tuple(r["field"]) == f]
                if xs:
                    by_field.append({"field": list(f), "model": m, "games": len(xs), "mean_placement": mean(xs)})
    return {"models": per_model, "paired": paired, "seats": per_seat, "fields": [list(f) for f in fields],
            "by_field": by_field,
            "groups": [{"field": list(g[0]), "track": g[1], "rules_sha256": g[2], "rules_changed": list(g[3]),
                        "conditions": list(g[4]),
                        "games": len({r["game_id"] for r in rows if comparable(r) == g})} for g in groups]}


# ================================================================ provenance warnings
def provenance(docs: list[dict], rows: list[dict]) -> tuple[list[str], dict]:
    """Warnings read from the replays, and per-model deadline counters."""
    warn: list[str] = []
    # manifests (notes excluded: free text that may differ per game)
    seen: dict[str, dict] = {}
    for r in rows:
        a = r.get("agent") or {}
        key = json.dumps({k: v for k, v in a.items() if k != "notes"}, sort_keys=True)
        seen.setdefault(r["model"], {}).setdefault(key, []).append(r["game_id"])
        notes = a.get("notes") or ""
        if _FALLBACK.search(notes):
            warn.append(f"{r['model']} in {r['game_id']}: the manifest notes say answers may come from another "
                        f"model ({notes[:120]!r})")
    for m, variants in sorted(seen.items()):
        if len(variants) > 1:
            parts = "; ".join(f"{k} in {', '.join(map(str, g))}" for k, g in variants.items())
            warn.append(f"{m}: the agent manifest differs between games: {parts}")
    missing = sorted({r["model"] for r in rows if not r.get("agent")})
    if missing:
        warn.append("no agent manifest for " + ", ".join(missing) + " (in at least one game)")
    hashes = {}
    for d in docs:
        hashes.setdefault((d.get("summary") or {}).get("rules_sha256"), []).append(d.get("game_id"))
    real = {h: g for h, g in hashes.items() if h}
    if len(real) > 1:
        warn.append("games were played under different rules: " +
                    "; ".join(f"{h[:12]} in {', '.join(g)}" for h, g in real.items()))
    if None in hashes and real:
        warn.append("no rules hash recorded in " + ", ".join(hashes[None]))
    for d in docs:
        summ = d.get("summary") or {}
        if summ.get("rules_changed"):
            warn.append(f"{d.get('game_id')}: the server was restarted with other rules during the game (started under "
                        f"{str(summ.get('rules_sha256'))[:12]}, then "
                        f"{', '.join(str(h)[:12] for h in summ['rules_changed'])}); it is not paired with other games")
        if summ.get("track") and summ.get("rated") is False:
            warn.append(f"{d.get('game_id')}: not rated in track {summ['track']} ({summ.get('unrated_reason')})")
    # action log: deadlines, phases and self-identifying messages
    deadlines: dict[str, dict] = {}
    no_log = []
    for d in docs:
        actions = d.get("actions")
        gid = d.get("game_id")
        players = (d.get("summary") or {}).get("players") or []
        who = {p.get("id"): p for p in players}
        if not isinstance(actions, dict):
            no_log.append(gid)
            continue
        for t in actions.get("turns") or []:
            for pid, why in (t.get("missed") or {}).items():
                m = model_of((who.get(pid) or {}).get("name", pid))
                c = deadlines.setdefault(m, {"missed_turns": 0, "phase_missed": 0, "negotiate_missed": 0})
                c["missed_turns"] += 1
            for item in t.get("phase_missed") or []:
                m = model_of((who.get(item.get("pid")) or {}).get("name", item.get("pid")))
                c = deadlines.setdefault(m, {"missed_turns": 0, "phase_missed": 0, "negotiate_missed": 0})
                c["phase_missed"] += 1
                if item.get("phase") == "negotiate":
                    c["negotiate_missed"] += 1
            for item in t.get("diplomacy") or []:
                action = item.get("action") if isinstance(item.get("action"), dict) else {}
                texts = [v for k, v in action.items() if k in ("text", "message") and isinstance(v, str)]
                if not texts:
                    continue
                p = who.get(item.get("by")) or {}
                needles = [x for x in (p.get("name"), model_of(p.get("name", "")), (p.get("agent") or {}).get("model"))
                           if isinstance(x, str) and len(x) >= 2 and x != p.get("seat_name")]
                for text in texts:
                    low = text.lower()
                    hit = sorted({x for x in needles if x.lower() in low})
                    if hit:
                        warn.append(f"{gid} turn {t.get('turn')}: {p.get('name', item.get('by'))} "
                                    f"({item.get('by')}) names itself ({', '.join(hit)}) in a message: "
                                    f"{text[:160]!r}")
    for m, c in sorted(deadlines.items()):
        if c["missed_turns"] or c["phase_missed"]:
            warn.append(f"{m}: missed {c['missed_turns']} turn deadline(s) and {c['phase_missed']} phase limit(s) "
                        f"({c['negotiate_missed']} in negotiation)")
    if no_log:
        warn.append(f"no action log in {len(no_log)} game(s) ({', '.join(map(str, no_log))}): deadline, phase "
                    "and message checks skipped")
    return warn, deadlines


# ================================================================ report
def build_report(source: Source, plan: dict | None = None, games: list[str] | None = None,
                 track: str | None = None, rng_seed: int = 0, nboot: int = DEFAULT_BOOTSTRAP) -> dict:
    planned: list[dict] = []
    if plan is not None:
        planned = plan["games"]
        ids = [g["game_id"] for g in planned if g.get("game_id")]
        if games:
            ids = [i for i in ids if i in set(games)]
        track = track or plan.get("track")
    elif games:
        ids = list(games)
    else:
        ids = source.ids(track if track else None)
    docs, missing = [], []
    for gid in ids:
        doc = source.replay(gid)
        if isinstance(doc, str):
            missing.append({"game_id": gid, "why": doc})
        else:
            doc.setdefault("game_id", gid)
            docs.append(doc)
    not_created = [g["index"] for g in planned if not g.get("game_id")] if plan is not None else []
    rows = [r for d in docs for r in game_rows(d)]
    warnings, deadlines = provenance(docs, rows)
    if plan is not None:
        for d in docs:
            g = next((x for x in planned if x.get("game_id") == d.get("game_id")), None)
            if g is None:
                continue
            names = [p.get("name") for p in (d.get("summary") or {}).get("players") or []]
            if names != g["seats"]:
                warnings.append(f"{d.get('game_id')}: seats {names} differ from the plan's {g['seats']}")
            if (d.get("summary") or {}).get("seed") != g["seed"]:
                warnings.append(f"{d.get('game_id')}: seed {(d.get('summary') or {}).get('seed')} differs from "
                                f"the plan's {g['seed']}")
    sizes = {len((d.get("summary") or {}).get("players") or []) for d in docs}
    if len(sizes) > 1:
        warnings.append(f"games have different seat counts ({', '.join(map(str, sorted(sizes)))}): placements are "
                        "on different scales; compare like with like (one track, or --games)")
    if track:
        other = sorted({str(d.get("game_id")) for d in docs if (d.get("summary") or {}).get("track") != track})
        if other:
            warnings.append(f"not played on track {track}: {', '.join(other)}")
    out = {
        "format": 1,
        "track": track,
        "bootstrap": {"resamples": nboot, "rng_seed": rng_seed, "ci": 0.95},
        "games": {"planned": len(planned) if plan is not None else None, "finished": len(docs),
                  "missing": len(missing) + len(not_created), "not_created": len(not_created),
                  "unfinished": missing, "ids": [d.get("game_id") for d in docs]},
        **analyse(rows, rng_seed, nboot),
        "deadlines": deadlines,
        "warnings": warnings,
    }
    return out


def _f(x, nd=2):
    return "–" if x is None else f"{x:.{nd}f}"


def _ci(c, nd=2):
    return "[–]" if c[0] is None else f"[{c[0]:.{nd}f}, {c[1]:.{nd}f}]"


def format_report(rep: dict) -> str:
    g = rep["games"]
    lines = []
    head = f"Track {rep['track']}: " if rep.get("track") else ""
    if g["planned"] is not None:
        lines.append(f"{head}{g['planned']} games planned, {g['finished']} finished, {g['missing']} missing "
                     f"({g['not_created']} not created, {len(g['unfinished'])} not finished or unreadable)")
    else:
        lines.append(f"{head}{g['finished']} finished games" + (f", {g['missing']} missing" if g["missing"] else ""))
    for u in g["unfinished"]:
        lines.append(f"  missing {u['game_id']}: {u['why']}")
    b = rep["bootstrap"]
    lines.append("")
    lines.append(f"Per model (95% bootstrap CI, {b['resamples']} resamples, rng seed {b['rng_seed']}; placement: "
                 "1 = best, ties averaged; a shared first place splits the win)")
    w = max([5] + [len(m) for m in rep["models"]])
    lines.append(f"  {'model':<{w}}  games  {'mean placement':<22}{'win rate':<22}mean score")
    for m, s in rep["models"].items():
        lines.append(f"  {m:<{w}}  {s['games']:>5}  "
                     f"{_f(s['placement']['mean']) + ' ' + _ci(s['placement']['ci95']):<22}"
                     f"{_f(s['win_rate']['mean']) + ' ' + _ci(s['win_rate']['ci95']):<22}"
                     f"{_f(s['score']['mean'], 1)} {_ci(s['score']['ci95'], 1)}")
    if rep["paired"]:
        lines.append("")
        lines.append("Paired placement differences on matched seed and seat, within one opponent field, track, rules "
                     "and conditions (a minus b; negative = a placed better)")
        if len(rep.get("groups") or []) > 1:
            lines.append(f"  the games fall into {len(rep['groups'])} groups that are not comparable (different field, "
                         "track, rules or conditions); pairs are formed only inside a group:")
            for gr in rep["groups"]:
                lines.append(f"    {gr['games']} games: [{', '.join(gr['field'])}], track {gr['track']}, rules "
                             f"{str(gr['rules_sha256'])[:12]}" + (" (changed mid-game)" if gr["rules_changed"] else ""))
        none = [p for p in rep["paired"] if not p["pairs"]]
        for p in rep["paired"]:
            if p["pairs"]:
                lines.append(f"  {p['a']} vs {p['b']}: {_f(p['mean_diff'])} {_ci(p['ci95'])}  "
                             f"n={p['pairs']} pairs ({p['a']} better in {p['a_better']}, {p['b']} in {p['b_better']})")
        if none:
            lines.append(f"  no matched pairs for {len(none)} of {len(rep['paired'])} model pairs (they never "
                         "played the same seat on the same seed in comparable games)")
    if rep["seats"]:
        lines.append("")
        lines.append("Mean placement by start seat (positional bias; every seat averages the same in a fair game)")
        for s, st in rep["seats"].items():
            lines.append(f"  seat {s}: {_f(st['mean'])} {_ci(st['ci95'])}  n={st['n']}")
    if len(rep["fields"]) > 1:
        lines.append("")
        lines.append("By opponent field (the models in the game)")
        for row in rep["by_field"]:
            lines.append(f"  [{', '.join(row['field'])}] {row['model']}: {row['games']} seats, mean placement "
                         f"{_f(row['mean_placement'])}")
    elif rep["fields"]:
        lines.append("")
        lines.append(f"Opponent field: one field in every game ({', '.join(rep['fields'][0])})")
    lines.append("")
    if rep["warnings"]:
        lines.append("Provenance warnings")
        lines.extend(f"  - {x}" for x in rep["warnings"])
    else:
        lines.append("Provenance warnings: none")
    return "\n".join(lines) + "\n"


# ================================================================ CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agentciv.evalplan", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="write a rotation schedule")
    p.add_argument("--track", required=True)
    p.add_argument("--models", required=True, help="comma-separated model names (the names seats join under)")
    p.add_argument("--seeds", type=int, required=True, help="number of map seeds")
    p.add_argument("--seed-base", type=int, default=1, help="first seed (seeds are consecutive; default 1)")
    p.add_argument("--rotations", type=int, default=None,
                   help="evenly spaced cyclic shifts per seed (default: all, i.e. every model in every seat)")
    p.add_argument("--allow-repeats", action="store_true", help="fill spare seats with repeated models (A#2)")
    p.add_argument("--seats", type=int, default=None, help="seat count, for a track this checkout lacks")
    p.add_argument("-o", "--out", required=True)
    c = sub.add_parser("create", help="create the next games of a plan on a server (operator key)")
    c.add_argument("--plan", required=True)
    c.add_argument("--url", required=True)
    c.add_argument("--spectator-key", default=os.environ.get("AGENTCIV_SPECTATOR_KEY"))
    c.add_argument("--count", type=int, default=1, help="how many games to create (default 1)")
    c.add_argument("--all", action="store_true", help="create every remaining game")
    r = sub.add_parser("report", help="report on finished games")
    r.add_argument("--plan")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--url")
    src.add_argument("--data-dir")
    r.add_argument("--spectator-key", default=None, help="only needed to read a server's unfinished games")
    r.add_argument("--games", help="comma-separated game ids (default: the plan's, else every finished game)")
    r.add_argument("--track", help="without a plan or --games: only games of this track")
    r.add_argument("--bootstrap", type=int, default=DEFAULT_BOOTSTRAP, help="resamples (default 2000)")
    r.add_argument("--rng-seed", type=int, default=0)
    r.add_argument("--json", help="also write the report as JSON to this file")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "plan":
            plan = make_plan(args.track, args.models.split(","), args.seeds, args.rotations, args.seed_base,
                             args.allow_repeats, args.seats)
            save_plan(plan, args.out)
            print(f"{len(plan['games'])} games ({len(plan['seeds'])} seeds x {len(plan['rotations'])} rotations) "
                  f"-> {args.out}")
        elif args.cmd == "create":
            if not args.spectator_key:
                raise EvalError("create needs the operator key (--spectator-key or AGENTCIV_SPECTATOR_KEY)")
            plan = load_plan(args.plan)
            create_games(plan, args.plan, args.url, args.spectator_key, None if args.all else args.count)
        else:
            plan = load_plan(args.plan) if args.plan else None
            games = [g.strip() for g in args.games.split(",") if g.strip()] if args.games else None
            source = Source(data_dir=args.data_dir, url=args.url, key=args.spectator_key)
            rep = build_report(source, plan, games, args.track, args.rng_seed, args.bootstrap)
            sys.stdout.write(format_report(rep))
            if args.json:
                Path(args.json).write_text(json.dumps(rep, indent=1) + "\n", encoding="utf-8")
    except EvalError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
