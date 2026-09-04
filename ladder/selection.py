"""Premium-based strike selection.

The spec selects strikes by premium, not by distance from spot, so the engine
reads the option chain and finds the strike trading nearest a target price.
Two things make that harder than it sounds.

Stale prints.  A strike's parquet exists whether or not the contract traded,
and an untraded contract carries its last price forward.  Sampled at the open
the premium curve is full of impossible orderings; sampled at the 15:15
decision time it is clean except for strikes that genuinely never traded.
Candidates therefore pass a recency and volume gate, and the surviving curve is
checked for monotonicity as a backstop.

Cost.  Reading a whole chain per selection is wasteful when most files are
empty.  Premium falls monotonically as a strike moves out of the money, so a
coarse sweep outward from at-the-money brackets the target and only that
bracket is read at full grid resolution.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

COARSE_STEP_MULTIPLE = 5   # coarse sweep visits every 5th strike on the grid
MAX_COARSE_STEPS = 40      # stop sweeping this far out regardless


@dataclass
class Quote:
    strike: int
    option_type: str
    price: float
    volume: float
    open_interest: float
    bars: int
    last_print: object


def _quote(market, day, expiry, strike, option_type, cutoff_time):
    """Reference price for a strike as of `cutoff_time`, with liquidity stats."""
    bars = market.option_bars(day, expiry, strike, option_type)
    if bars is None or bars.empty:
        return None
    window = bars[bars.index.time <= cutoff_time]
    if window.empty:
        return None
    open_interest = window["OI"].iloc[-1]
    return Quote(
        strike=int(strike),
        option_type=option_type,
        price=float(window["Close"].iloc[-1]),
        volume=float(window["Volume"].sum()),
        open_interest=float(open_interest) if pd.notna(open_interest) else float("nan"),
        bars=len(window),
        last_print=window.index[-1].to_pydatetime(),
    )


class _ChainGate:
    """Liquidity gate calibrated against the chain it is filtering."""

    def __init__(self, rules, cutoff, session_bars):
        self.rules = rules
        self.cutoff = cutoff
        self.session_bars = max(session_bars, 1)
        self.volume_floor = rules.min_volume

    def hard_pass(self, quote):
        # Bar count is deliberately not a proxy for liquidity here.  A far
        # weekly expiry prints only when it trades -- eight to forty bars in a
        # session against the near expiry's three hundred and sixty -- while
        # quoting a perfectly coherent premium curve.  Recency of the last
        # print is what separates a thin contract from a stale one.
        if quote is None or quote.price <= 0:
            return False
        if quote.volume < self.rules.min_volume:
            return False
        if quote.bars < self.rules.min_bars:
            return False
        staleness = self.cutoff - quote.last_print
        if staleness > timedelta(minutes=self.rules.max_staleness_minutes):
            return False
        if self.rules.min_open_interest > 0 and pd.notna(quote.open_interest):
            if quote.open_interest < self.rules.min_open_interest:
                return False
        return True

    def calibrate(self, quotes):
        """Set the relative volume floor from the chain's own median."""
        volumes = [q.volume for q in quotes if q.volume > 0]
        if volumes:
            median = float(pd.Series(volumes).median())
            self.volume_floor = max(self.rules.min_volume,
                                    median * self.rules.min_volume_fraction)

    def relative_pass(self, quote):
        return quote.volume >= self.volume_floor


def _otm_candidates(market, day, expiry, option_type, spot, rules):
    """Strikes on the grid, out of the money, ordered outward from spot."""
    grid = [
        s for s in market.strikes_on(day, expiry, option_type)
        if s % rules.strike_step == 0
    ]
    if option_type == "CE":
        return sorted(s for s in grid if s >= spot)
    return sorted((s for s in grid if s <= spot), reverse=True)


def scan_chain(market, day, expiry, option_type, spot, targets, rules, cutoff_time):
    """Liquid quotes spanning the requested premium targets, ordered outward."""
    candidates = _otm_candidates(market, day, expiry, option_type, spot, rules)
    if not candidates:
        return []

    index_bars = market.index_bars(day)
    session_bars = 0
    if index_bars is not None:
        session_bars = len(index_bars[index_bars.index.time <= cutoff_time])
    gate = _ChainGate(rules, datetime.combine(day, cutoff_time), session_bars)
    floor = min(targets) * 0.6

    # Coarse sweep outward until premiums fall below the smallest target.
    coarse = {}
    bracket_end = len(candidates)
    limit = min(len(candidates), MAX_COARSE_STEPS * COARSE_STEP_MULTIPLE)
    for position in range(0, limit, COARSE_STEP_MULTIPLE):
        quote = _quote(market, day, expiry, candidates[position], option_type, cutoff_time)
        coarse[position] = quote
        if quote is not None and 0 < quote.price < floor:
            bracket_end = min(len(candidates), position + COARSE_STEP_MULTIPLE)
            break

    # Fill in the bracket at full grid resolution.
    found = dict(coarse)
    for position in range(bracket_end):
        if position not in found:
            found[position] = _quote(
                market, day, expiry, candidates[position], option_type, cutoff_time
            )

    survivors = [found[p] for p in sorted(found) if gate.hard_pass(found[p])]
    gate.calibrate(survivors)
    survivors = [q for q in survivors if gate.relative_pass(q)]
    return _drop_curve_violations(survivors) if rules.enforce_monotonic else survivors


def _drop_curve_violations(quotes):
    """Keep the longest run of quotes whose premium falls monotonically outward.

    Premium must decrease as a strike moves further out of the money.  A quote
    breaking that ordering is a stale print, not an opportunity.  The longest
    non-increasing subsequence keeps the coherent part of the curve and drops
    whatever contradicts it.
    """
    if len(quotes) < 3:
        return quotes
    n = len(quotes)
    best = [1] * n
    previous = [-1] * n
    for i in range(1, n):
        for j in range(i):
            if quotes[j].price >= quotes[i].price and best[j] + 1 > best[i]:
                best[i] = best[j] + 1
                previous[i] = j
    tail = max(range(n), key=lambda i: best[i])
    keep = []
    while tail != -1:
        keep.append(quotes[tail])
        tail = previous[tail]
    return list(reversed(keep))


def pick_by_premium(quotes, target, exclude_strikes=(), tolerance=0.0):
    """The liquid strike trading nearest `target` premium.

    Where several strikes sit within `tolerance` of the target, the most
    heavily traded one wins.  Two strikes a rupee apart on premium are
    interchangeable to the strategy; the one that actually trades is the one
    that could have been filled.
    """
    usable = [q for q in quotes if q.strike not in exclude_strikes]
    if not usable:
        return None
    best = min(usable, key=lambda q: abs(q.price - target))
    if tolerance <= 0:
        return best
    close_enough = [q for q in usable if abs(q.price - target) <= tolerance]
    return max(close_enough, key=lambda q: q.volume) if close_enough else best
