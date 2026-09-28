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


def clear_resource(pool: list, orders: list, balances: dict, fees: dict) -> tuple[list, list, float]:
    """Clear one resource's batch auction.

    ``pool`` is ``[resource_reserve, gold_reserve]`` (mutated on success),
    ``balances`` maps pid -> resource dict (mutated), ``fees`` pid -> fee.
    Returns ``(fills, failures, price)`` where fills are
    ``(order, gold_amount)`` and failures ``(order, reason)``.
    """
    if not orders:
        return [], [], spot_price(pool)
    res_name = orders[0].resource
    active = list(orders)
    failures: list = []
    price = spot_price(pool)
    stable = False
    for it in range(C.MARKET_MAX_ITERATIONS + 1):
        # the pool may never be drained beyond MARKET_MAX_NET_FRACTION
        while True:
            net = sum(o.qty for o in active if o.side == "buy") - sum(o.qty for o in active if o.side == "sell")
            if net <= C.MARKET_MAX_NET_FRACTION * pool[0]:
                break
            buys = [o for o in active if o.side == "buy"]
            worst = max(buys, key=lambda o: (o.qty, o.pid, o.index))
            active.remove(worst)
            failures.append((worst, "market: pool depleted, order too large"))
        price = auction_price(pool[0], pool[1], net)
        violators = []
        gold_used: dict = {}
        res_used: dict = {}
        for o in active:
            fee = fees.get(o.pid, C.MARKET_FEE)
            if o.side == "buy":
                if o.limit is not None and price > o.limit + 1e-12:
                    violators.append((o, f"market: price {price:.4f} above limit {o.limit}"))
                    continue
                cost = buy_cost(o.qty, price, fee)
                if gold_used.get(o.pid, 0) + cost > balances[o.pid].get("gold", 0):
                    violators.append((o, f"market: cannot afford {cost} gold"))
                    continue
                gold_used[o.pid] = gold_used.get(o.pid, 0) + cost
            else:
                if o.limit is not None and price < o.limit - 1e-12:
                    violators.append((o, f"market: price {price:.4f} below limit {o.limit}"))
                    continue
                if res_used.get(o.pid, 0) + o.qty > balances[o.pid].get(res_name, 0):
                    violators.append((o, f"market: not enough {res_name} to sell"))
                    continue
                res_used[o.pid] = res_used.get(o.pid, 0) + o.qty
        if not violators:
            stable = True
            break
        if it == C.MARKET_MAX_ITERATIONS:
            break
        drop = {id(o) for o, _ in violators}
        active = [o for o in active if id(o) not in drop]
        failures.extend(violators)
    if not stable:
        failures.extend((o, "market: auction did not converge") for o in active)
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
