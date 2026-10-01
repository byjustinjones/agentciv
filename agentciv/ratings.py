"""Weng-Lin (OpenSkill) Plackett-Luce ratings for free-for-all games.

Pure stdlib. Lower rank = better placement. Ratings are stored as
``{"mu": float, "sigma": float}``; the display rating is ``mu - 3*sigma``.
"""
from __future__ import annotations

import math

MU = 25.0
SIGMA = MU / 3.0
BETA = SIGMA / 2.0
KAPPA = 0.0001


def new_rating() -> dict:
    return {"mu": MU, "sigma": SIGMA}


def display(r: dict) -> float:
    return r["mu"] - 3.0 * r["sigma"]


def rate(ratings: list[dict], ranks: list[int]) -> list[dict]:
    """Return updated ratings for one game.

    ``ratings[i]`` is player i's rating, ``ranks[i]`` their placement
    (1 = winner; equal ranks mean a tie).
    """
    n = len(ratings)
    if n < 2:
        return [dict(r) for r in ratings]
    c = math.sqrt(sum(r["sigma"] ** 2 + BETA ** 2 for r in ratings))
    exp_mu = [math.exp(r["mu"] / c) for r in ratings]
    sum_q = [sum(exp_mu[i] for i in range(n) if ranks[i] >= ranks[q]) for q in range(n)]
    a = [sum(1 for i in range(n) if ranks[i] == ranks[q]) for q in range(n)]
    out = []
    for i, r in enumerate(ratings):
        omega = 0.0
        delta = 0.0
        for q in range(n):
            if ranks[q] > ranks[i]:
                continue
            quotient = exp_mu[i] / sum_q[q]
            if q == i:
                omega += (1 - quotient) / a[q]
            else:
                omega -= quotient / a[q]
            delta += quotient * (1 - quotient) / a[q]
        sigma_sq = r["sigma"] ** 2
        gamma = math.sqrt(sigma_sq) / c
        omega *= sigma_sq / c
        delta *= gamma * sigma_sq / c ** 2
        out.append({"mu": r["mu"] + omega, "sigma": math.sqrt(sigma_sq * max(1 - delta, KAPPA))})
    return out


def update(table: dict, placements: list[str], ranks: list[int] | None = None) -> dict:
    """Update ``table`` ({name: {"mu","sigma",...}}) in place from an ordered
    placement list (winner first) and return it. ``ranks`` (optional, same
    length, non-decreasing, 1 = best) marks ties: equal ranks are rated as a
    draw (e.g. players tied on score). Extra bookkeeping keys (games, wins,
    total_place) are maintained; everyone ranked 1 counts a win.

    A name may hold several seats (the same house bot twice). Every seat is
    rated as its own player starting from the name's current rating; the
    name then moves by the mean of its seats' mu changes and its variance by
    the mean of their variance factors. Bookkeeping counts seats (``games``
    += seats, ``wins`` += seats ranked 1, ``total_place`` += their ranks),
    so ``avg_place`` and the win rate stay per-seat figures."""
    if ranks is None:
        ranks = list(range(1, len(placements) + 1))
    if len(ranks) != len(placements):
        raise ValueError("ranks must match placements")
    for name in placements:
        table.setdefault(name, {**new_rating(), "games": 0, "wins": 0, "total_place": 0})
    olds = [table[name] for name in placements]
    news = rate(olds, list(ranks))
    seats: dict[str, list[tuple[dict, dict]]] = {}
    for name, old, new in zip(placements, olds, news):
        seats.setdefault(name, []).append((old, new))
    for name, pairs in seats.items():
        entry = table[name]
        if len(pairs) == 1:  # the common case: exactly the single-seat update
            new = pairs[0][1]
            entry["mu"], entry["sigma"] = new["mu"], new["sigma"]
            continue
        k = len(pairs)
        mu, var = entry["mu"], entry["sigma"] ** 2
        d_mu = sum(n["mu"] - mu for _, n in pairs) / k
        f_var = sum(n["sigma"] ** 2 / var for _, n in pairs) / k
        entry["mu"], entry["sigma"] = mu + d_mu, math.sqrt(var * f_var)
    for name, rank in zip(placements, ranks):
        entry = table[name]
        entry["games"] = entry.get("games", 0) + 1
        entry["wins"] = entry.get("wins", 0) + (1 if rank == 1 else 0)
        entry["total_place"] = entry.get("total_place", 0) + rank
    return table


def leaderboard(table: dict) -> list[dict]:
    rows = []
    for name, e in table.items():
        games = e.get("games", 0)
        rows.append({
            "name": name,
            "rating": round(display(e), 2),
            "mu": round(e["mu"], 3),
            "sigma": round(e["sigma"], 3),
            "games": games,
            "wins": e.get("wins", 0),
            "avg_place": round(e.get("total_place", 0) / games, 2) if games else None,
        })
    rows.sort(key=lambda r: r["rating"], reverse=True)
    return rows
