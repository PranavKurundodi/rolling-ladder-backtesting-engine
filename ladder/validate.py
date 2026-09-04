"""Invariant checks on a completed backtest.

A backtest that silently violates its own strategy is worse than no backtest,
and the failure modes here are quiet ones: a leg that never got closed, a book
that drifted to seven sold legs, a stop that filled better than it should have.
These checks assert the spec's structural promises against the trade log, and
run automatically at the end of every backtest.
"""

import pandas as pd

from .positions import HEDGE, SOLD_A, SOLD_B, SOLD_C

SOLD_ROLES = {SOLD_A, SOLD_B, SOLD_C}


def _timeline(trades):
    """Open-leg counts after every entry and exit, in time order."""
    events = []
    for row in trades.itertuples():
        events.append((row.entry_time, +1, row))
        if pd.notna(row.exit_time):
            events.append((row.exit_time, -1, row))
    events.sort(key=lambda e: (e[0], e[1]))
    return events


def check(results, config):
    trades, equity = results["trades"], results["equity"]
    problems = []
    if trades.empty:
        return ["no trades produced"]

    trades = trades.copy()
    for column in ("entry_time", "exit_time"):
        trades[column] = pd.to_datetime(trades[column])
    trades["expiry"] = pd.to_datetime(trades["expiry"])

    # 1. Exits never precede entries.
    backwards = trades[trades.exit_time.notna() & (trades.exit_time < trades.entry_time)]
    if len(backwards):
        problems.append(f"{len(backwards)} legs exit before they enter")

    # 2. Nothing is held past expiry -- the spec holds nothing to expiry.
    closed = trades[trades.exit_time.notna()]
    late = closed[closed.exit_time.dt.normalize() > closed.expiry]
    if len(late):
        problems.append(f"{len(late)} legs closed after their expiry date")

    # 3. Every sold Book A leg carries the 70-point stop off its own entry.
    sold = trades[(trades.book == "A") & (trades.role.isin(SOLD_ROLES))]
    expected = sold.entry_price + config.book_a.stop_premium_points
    drift = (sold.stop_price - expected).abs()
    if (drift > 1e-6).any():
        problems.append(f"{int((drift > 1e-6).sum())} sold legs have a mis-set stop")

    # 4. Hedges carry no stop, per the spec.
    hedges = trades[trades.role == HEDGE]
    if hedges.stop_price.notna().any():
        problems.append(f"{int(hedges.stop_price.notna().sum())} hedges carry a stop")

    # 5. A stop must fill at or above its trigger; filling below would be a
    #    free improvement the market never offered.
    stopped = trades[trades.exit_reason == "stop"]
    improved = stopped[stopped.exit_price < stopped.stop_price - 1e-6]
    if len(improved):
        problems.append(f"{len(improved)} stops filled better than their trigger")

    # 6. Book A never carries more than six sold and two bought legs.
    open_a_sold = open_a_hedge = 0
    peak_sold = peak_hedge = 0
    for _, delta, row in _timeline(trades[trades.book == "A"]):
        if row.role in SOLD_ROLES:
            open_a_sold += delta
            peak_sold = max(peak_sold, open_a_sold)
        elif row.role == HEDGE:
            open_a_hedge += delta
            peak_hedge = max(peak_hedge, open_a_hedge)
    if peak_sold > len(SOLD_ROLES) * 2:
        problems.append(f"Book A held {peak_sold} sold legs at once (max 6)")
    if peak_hedge > 2:
        problems.append(f"Book A held {peak_hedge} hedges at once (max 2)")

    # 7. Book B net exposure only ever rests at 0, +2 or -2 lots.  Exposure is
    #    sampled once per timestamp, after every leg of a rebalance has been
    #    applied: a transition passes through an odd net between its two legs,
    #    which is an artefact of ordering rather than a position ever held.
    net = 0
    resting = {}
    for stamp, delta, row in _timeline(trades[trades.book == "B"]):
        net += delta * row.quantity_lots
        resting[stamp] = net
    stray = {n for n in resting.values() if n not in (0, 2, -2)}
    if stray:
        problems.append(f"Book B rested at unexpected net exposures {sorted(stray)}")

    # 8. Realised P&L in the trade log matches the equity curve's last value.
    if not equity.empty:
        booked = float(closed.points.sum())
        recorded = float(equity.realized_points.iloc[-1])
        if abs(booked - recorded) > 1e-6:
            problems.append(
                f"realised P&L disagrees: trade log {booked:.2f} vs equity {recorded:.2f}"
            )
    return problems


def report(problems):
    if not problems:
        print("\nInvariants: all checks passed.")
        return True
    print(f"\nInvariants: {len(problems)} problem(s)")
    for item in problems:
        print(f"  - {item}")
    return False
