"""Batch-auction market against constant-product pools (docs/DESIGN.md §6)."""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import constants as C


@dataclass
class MarketOrder:
    pid: str
    side: str            # "buy" | "sell"
    resource: str
    qty: int
    limit: float | None
    index: int           # index in the player's submitted order list


def spot_price(pool: list) -> float:
    return pool[1] / pool[0] if pool[0] > 0 else math.inf


def auction_price(res: float, gold: float, net: int) -> float:
    """Average execution price when ``net`` units are bought from (net > 0)
    or sold to (net < 0) a pool with reserves (res, gold)."""
    if net == 0:
        return gold / res
    k = res * gold
    if net > 0:
        return (k / (res - net) - gold) / net
    m = -net
    return (gold - k / (res + m)) / m


def buy_cost(qty: int, price: float, fee: float) -> int:
    return int(math.ceil(qty * price * (1 + fee) - 1e-9))


def sell_proceeds(qty: int, price: float, fee: float) -> int:
    return int(math.floor(qty * price * (1 - fee) + 1e-9))


def _evaluate(pool: list, active: list, balances: dict, fees: dict, res_name: str):
    """Price the auction for the ``active`` orders (kept in submission order).

    Returns ``(price, violators)``; ``price`` is None when the net buy volume
    would drain the pool beyond MARKET_MAX_NET_FRACTION. Each violator is
    ``(score, order, reason)``: the larger the score, the further the order
    is from being valid at this price (buys: price / max price they can pay,
    sells: limit / price).
    """
    net = sum(o.qty for o in active if o.side == "buy") - sum(o.qty for o in active if o.side == "sell")
    if net > C.MARKET_MAX_NET_FRACTION * pool[0]:
        return None, []
    price = auction_price(pool[0], pool[1], net)
    violators = []
    gold_used: dict = {}
    for o in active:
        fee = fees.get(o.pid, C.MARKET_FEE)
        if o.side == "buy":
            left = balances[o.pid].get("gold", 0) - gold_used.get(o.pid, 0)
            if o.limit is not None and price > o.limit + 1e-12:
                violators.append((price / o.limit, o, f"market: price {price:.4f} above limit {o.limit}"))
                continue
            cost = buy_cost(o.qty, price, fee)
            if cost > left:
                afford = left / (o.qty * (1 + fee))
                score = price / afford if afford > 0 else math.inf
                violators.append((score, o, f"market: cannot afford {cost} gold"))
                continue
            gold_used[o.pid] = gold_used.get(o.pid, 0) + cost
        elif o.limit is not None and price < o.limit - 1e-12:
            violators.append((o.limit / price if price > 0 else math.inf, o,
                              f"market: price {price:.4f} below limit {o.limit}"))
    return price, violators


def _max_buy_price(active: list, balances: dict, fees: dict) -> dict:
    """Highest price each active buy order could pay (limit and gold), with
    a player's earlier buy orders of this resource counted first."""
    out = {}
    qty_used: dict = {}
    for o in active:
        if o.side != "buy":
            continue
        q = qty_used.get(o.pid, 0) + o.qty
        qty_used[o.pid] = q
        afford = balances[o.pid].get("gold", 0) / (q * (1 + fees.get(o.pid, C.MARKET_FEE)))
        out[id(o)] = min(afford, o.limit) if o.limit is not None else afford
    return out


def clear_resource(pool: list, orders: list, balances: dict, fees: dict) -> tuple[list, list, float]:
    """Clear one resource's batch auction (docs/DESIGN.md §6).

    ``pool`` is ``[resource_reserve, gold_reserve]`` (mutated on success),
    ``balances`` maps pid -> resource dict (mutated), ``fees`` pid -> fee.
    Returns ``(fills, failures, price)`` where fills are
    ``(order, gold_amount)`` and failures ``(order, reason)``.

    Orders that can never fill must not move the price for everyone else
    (phantom orders), so:

    1. sells the player cannot deliver fail up front (price independent);
    2. while the order set is invalid, exactly **one** order is dropped: if the
       net buy volume would drain the pool, the buy with the lowest price it
       could pay (limit or gold); otherwise the order furthest from being
       valid at the current price (unaffordable buys with little gold first);
    3. dropped orders are then re-admitted (most nearly valid first) whenever
       the whole set stays valid with them.

    Every round of 2 drops an order, so it terminates; the final set is always
    valid (possibly empty).
    """
    if not orders:
        return [], [], spot_price(pool)
    res_name = orders[0].resource
    failures: list = []
    active: list = []
    selling: dict = {}
    for o in orders:
        if o.side == "sell":
            q = selling.get(o.pid, 0) + o.qty
            if q > balances[o.pid].get(res_name, 0):
                failures.append((o, f"market: not enough {res_name} to sell"))
                continue
            selling[o.pid] = q
        active.append(o)
    pos = {id(o): k for k, o in enumerate(orders)}
    dropped: list = []   # (score, order, reason)
    while True:
        price, violators = _evaluate(pool, active, balances, fees, res_name)
        if price is None:
            cap = _max_buy_price(active, balances, fees)
            buys = [o for o in active if o.side == "buy"]
            worst = min(buys, key=lambda o: (cap[id(o)], -o.qty, -pos[id(o)]))
            entry = (math.inf, worst, "market: pool depleted (net buying would drain the pool), "
                                      "order dropped: lowest price it could pay")
        elif violators:
            entry = max(violators, key=lambda v: (v[0], pos[id(v[1])]))
        else:
            break
        active.remove(entry[1])
        dropped.append(entry)
    # re-admit orders that were dropped only because of orders dropped later
    for _ in range(C.MARKET_READMIT_PASSES):
        changed = False
        for entry in sorted(dropped, key=lambda v: (v[0], pos[id(v[1])])):
            o = entry[1]
            trial = sorted(active + [o], key=lambda x: pos[id(x)])
            t_price, t_viol = _evaluate(pool, trial, balances, fees, res_name)
            if t_price is not None and not t_viol:
                active = trial
                dropped.remove(entry)
                changed = True
        if not changed:
            break
    failures.extend((o, reason) for _, o, reason in sorted(dropped, key=lambda v: pos[id(v[1])]))
    price, _ = _evaluate(pool, active, balances, fees, res_name)
    if not active:
        return [], failures, spot_price(pool)
    fills = []
    for o in active:
        fee = fees.get(o.pid, C.MARKET_FEE)
        bal = balances[o.pid]
        if o.side == "buy":
            cost = buy_cost(o.qty, price, fee)
            bal["gold"] -= cost
            bal[res_name] = bal.get(res_name, 0) + o.qty
            fills.append((o, cost))
        else:
            gain = sell_proceeds(o.qty, price, fee)
            bal[res_name] -= o.qty
            bal["gold"] = bal.get("gold", 0) + gain
            fills.append((o, gain))
    net = sum(o.qty for o in active if o.side == "buy") - sum(o.qty for o in active if o.side == "sell")
    if net != 0:
        k = pool[0] * pool[1]
        pool[0] -= net
        pool[1] = k / pool[0]
    return fills, failures, price


def revert(pool: list, initial: tuple) -> None:
    """Move a pool MARKET_REVERSION of the way back to its initial reserves."""
    pool[0] += (initial[0] - pool[0]) * C.MARKET_REVERSION
    pool[1] += (initial[1] - pool[1]) * C.MARKET_REVERSION
