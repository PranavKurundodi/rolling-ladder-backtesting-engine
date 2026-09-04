"""Data-health audit for the rolling-ladder backtest window.

Answers, for a given date range: can this strategy actually be traded on this
archive, on which days, and where does it degrade?
"""

import sys
from collections import Counter

import pandas as pd
from tabulate import tabulate

from ladder.config import load_config
from ladder.book_b import month_offset
from ladder.market import MarketData
from ladder.selection import pick_by_premium, scan_chain

SAMPLE_EVERY = 5   # full 8-leg fill check on every Nth trading day


def main():
    config = load_config(sys.argv[1] if len(sys.argv) > 1 else "ladder_config.yaml")
    market = MarketData(config.data_root, config.market_segment)
    a, b = config.book_a, config.book_b
    days = market.trading_days(config.start_date, config.end_date)
    print(f"Window {config.start_date} -> {config.end_date}: "
          f"{len(days)} days in the contract index\n")

    structural, no_index, thin_ladder, no_futures = [], [], [], []
    oi_present = Counter()
    for day in days:
        bars = market.index_bars(day)
        if bars is None or bars.empty:
            no_index.append(day)
            continue
        ladder = market.option_ladder(day, a.initiation_min_dte, 3)
        if len(ladder) < 3:
            thin_ladder.append(day)
        shift = 0 if day.day < b.contract_roll_day else 1
        wanted = (month_offset(day, shift), month_offset(day, shift + 1))
        available = market.future_expiries_on(day)
        pair = [next((e for e in available
                      if (e.year, e.month) == (m.year, m.month)), None) for m in wanted]
        if None in pair:
            no_futures.append((day, wanted, available))
        structural.append(day)

    print("STRUCTURAL COVERAGE")
    print(tabulate([
        ["Days usable (index bars readable)", len(structural)],
        ["Days skipped (no readable index bars)", len(no_index)],
        ["Days without 3 expiries beyond 15 DTE", len(thin_ladder)],
        ["Days without a resolvable near/far futures pair", len(no_futures)],
    ], tablefmt="pretty"))
    if no_index:
        print("  skipped:", ", ".join(str(d) for d in no_index))
    if thin_ladder:
        print("  thin ladder:", ", ".join(str(d) for d in thin_ladder[:12]))
    for day, wanted, available in no_futures[:8]:
        print(f"  {day}: wanted {[f'{m:%b %Y}' for m in wanted]}, have {available}")

    print("\nEIGHT-LEG FILLABILITY "
          f"(every {SAMPLE_EVERY}th day, at {config.execution.decision_time})")
    cutoff = pd.Timestamp(config.execution.decision_time).time()
    targets = tuple(a.sell_targets) + (a.hedge_target,)
    checked = filled = 0
    deviations, failures, chain_depth = [], [], []
    for day in structural[::SAMPLE_EVERY]:
        ref = market.intraday_high_low_to(day, cutoff)
        ladder = market.option_ladder(day, a.initiation_min_dte, 3)
        if ref is None or len(ladder) < 3:
            continue
        spot = ref["Close"]
        legs = [(ladder[i], ot, t) for i, t in enumerate(a.sell_targets)
                for ot in ("CE", "PE")]
        legs += [(ladder[0], ot, a.hedge_target) for ot in ("CE", "PE")]
        for expiry, option_type, target in legs:
            checked += 1
            chain = scan_chain(market, day, expiry, option_type, spot,
                               targets, a.liquidity, cutoff)
            chain_depth.append(len(chain))
            quote = pick_by_premium(chain, target, tolerance=a.liquidity.premium_tolerance)
            if quote is None:
                failures.append((day, expiry, option_type, target))
            else:
                filled += 1
                deviations.append(abs(quote.price - target) / target)

    dev = pd.Series(deviations)
    print(tabulate([
        ["Legs checked", checked],
        ["Legs fillable", f"{filled} ({filled / max(checked,1):.1%})"],
        ["Median liquid strikes per chain", int(pd.Series(chain_depth).median())],
        ["Chains with < 3 liquid strikes", f"{sum(1 for c in chain_depth if c < 3)}"],
        ["Median premium deviation from target", f"{dev.median():.1%}"],
        ["Legs filled >25% off target", f"{(dev > 0.25).sum()} ({(dev > 0.25).mean():.1%})"],
        ["Legs filled >50% off target", f"{(dev > 0.50).sum()} ({(dev > 0.50).mean():.1%})"],
    ], tablefmt="pretty"))
    for item in failures[:10]:
        print(f"  unfillable: {item[0]} {item[1]} {item[2]} @ {item[3]}")


if __name__ == "__main__":
    main()
