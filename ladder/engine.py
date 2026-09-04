"""The backtest engine: one loop over trading days, one portfolio throughout.

The template ran each day in isolation.  This strategy cannot: sold legs live
about three weeks, hedges about one, and Book B's state persists across months.
So the portfolio is created once and carried, and a day is a set of events
applied to it rather than a self-contained experiment.

Order within a day matters and follows the spec:

    1.  Stops are resolved from the open up to the decision time.  They are an
        input to Book B's signals, so they must be known first.
    2.  At the decision time Book A initiates or rolls.
    3.  Book B rolls contracts, then applies at most one state transition --
        "the roll executes first and the transition then applies to the new
        contracts".
    4.  Stops are resolved from the decision time to the close.
    5.  The day's completed bar updates the trend series, and the book is
        marked to market.
"""

from datetime import date, datetime, time, timedelta

import pandas as pd
from tqdm import tqdm

from .book_a import RollingOptionsLadder
from .book_b import FuturesTrendOverlay
from .positions import OPTION, Portfolio
from .signals import TrendTracker

WARMUP_DAYS = 120   # calendar days of history used to seed the EMAs


class Journal:
    """Every decision and every data problem, in one place."""

    def __init__(self):
        self.events = []
        self.warnings = []

    def event(self, day, when, source, kind, detail):
        self.events.append({"day": day, "time": when, "source": source,
                            "event": kind, "detail": detail})

    def warn(self, day, source, detail):
        self.warnings.append({"day": day, "source": source, "detail": detail})

    def frames(self):
        return (pd.DataFrame(self.events), pd.DataFrame(self.warnings))


class TradingCalendar:
    """Trading days as the archive actually has them."""

    def __init__(self, days):
        self.days = sorted(days)
        self._set = set(self.days)

    def __contains__(self, day):
        return day in self._set

    def next_trading_day_on_or_after(self, day):
        for candidate in self.days:
            if candidate >= day:
                return candidate
        return day


class BacktestEngine:
    def __init__(self, config, market):
        self.config = config
        self.market = market
        self.journal = Journal()
        self.portfolio = Portfolio(config)
        self.equity = []

        warmup_start = config.start_date - timedelta(days=WARMUP_DAYS)
        self.warmup_days = [d for d in market.trading_days(warmup_start, config.end_date)
                            if d < config.start_date]
        self.days = market.trading_days(config.start_date, config.end_date)
        self.calendar = TradingCalendar(self.warmup_days + self.days)

        self.tracker = TrendTracker(config.book_b.ema_fast, config.book_b.ema_slow,
                                    config.book_b.higher_high_count)
        self.book_a = RollingOptionsLadder(config, market, self.portfolio, self.journal)
        self.book_b = FuturesTrendOverlay(config, market, self.portfolio,
                                          self.journal, self.calendar)

    # ------------------------------------------------------------- helpers

    @property
    def decision_time(self):
        hour, minute = self.config.execution.decision_time.split(":")
        return time(int(hour), int(minute))

    def _daily_bar(self, day):
        """Completed daily OHLC of spot, for the trend series."""
        bars = self.market.index_bars(day)
        if bars is None or bars.empty:
            return None
        return (float(bars["High"].max()), float(bars["Low"].min()),
                float(bars["Close"].iloc[-1]))

    def _price_lookup(self, day, cutoff_time=None):
        def lookup(leg):
            if leg.kind == OPTION:
                return self.book_a.price_of(day, leg, cutoff_time)
            bars = self.market.future_bars(day, leg.expiry)
            if bars is None or bars.empty:
                return None
            if cutoff_time is not None:
                bars = bars[bars.index.time <= cutoff_time]
                if bars.empty:
                    return None
            return float(bars["Close"].iloc[-1])
        return lookup

    # ---------------------------------------------------------------- warmup

    def warm_up(self):
        for day in self.warmup_days:
            bar = self._daily_bar(day)
            if bar:
                self.tracker.update(*bar)

    # ------------------------------------------------------------- main loop

    def run(self, progress=True):
        self.warm_up()
        cutoff = self.decision_time
        iterator = tqdm(self.days, desc=self.config.name) if progress else self.days

        for day in iterator:
            when = datetime.combine(day, cutoff)
            reference = self.market.intraday_high_low_to(day, cutoff)
            if reference is None:
                self.journal.warn(day, "engine", "no index bars; day skipped")
                continue
            spot = reference["Close"]

            # 1. Stops from the open to the decision time feed Book B's signals.
            self.book_a.stops_today = set()
            if self.config.book_a.enabled:
                self.book_a.check_stops(day, end_time=cutoff)
            stops_before_decision = set(self.book_a.stops_today)

            # 2. Book A: initiate once, then roll when the front expiry reaches
            #    its trigger.
            if self.config.book_a.enabled:
                self.book_a.close_expiring(day, when)
                if not self.book_a.initiated:
                    self.book_a.initiate(day, when, spot)
                elif self.book_a.needs_roll(day):
                    self.book_a.roll(day, when, spot)

            # 3. Book B: contract roll first, then at most one transition.
            if self.config.book_b.enabled:
                self.book_b.close_expiring(day, when)
                if self.book_b.state is None:
                    self.book_b.initiate(day, when)
                else:
                    self.book_b.maybe_roll(day, when)
                    if self.tracker.ready:
                        state = self.tracker.state(spot, reference["High"],
                                                   reference["Low"])
                        self.book_b.maybe_transition(day, when, state,
                                                     stops_before_decision)

            # 4. The rest of the session can still stop legs out.
            if self.config.book_a.enabled:
                self.book_a.check_stops(day, start_time=cutoff)

            # 5. Close of business: update the trend series and mark the book.
            bar = self._daily_bar(day)
            if bar:
                self.tracker.update(*bar)
            self._record_equity(day, spot)

        return self

    def _record_equity(self, day, spot):
        lookup = self._price_lookup(day)
        unrealized = self.portfolio.unrealized_points(lookup)
        lot = self.config.lot_size_on(day)
        option_legs = self.portfolio.legs(kind=OPTION)
        self.equity.append({
            "day": day,
            "spot": spot,
            "realized_points": self.portfolio.realized_points,
            "unrealized_points": unrealized,
            "total_points": self.portfolio.realized_points + unrealized,
            "realized_rupees": self.portfolio.realized_rupees,
            "total_rupees": self.portfolio.realized_rupees + unrealized * lot,
            "lot_size": lot,
            "book_b_state": self.book_b.state,
            "open_legs": len(self.portfolio.open_legs),
            "sold_options": sum(1 for l in option_legs if l.is_sold),
            "bought_options": sum(1 for l in option_legs if not l.is_sold),
            "futures_lots": self.portfolio.net_future_quantity(),
            # Exposure proxy for the margin question the spec raises.  Real
            # SPAN margin needs the exchange's risk arrays; this counts the
            # naked short lots that drive it.
            "naked_short_lots": abs(min(0, self.portfolio.net_option_quantity("CE")))
                                + abs(min(0, self.portfolio.net_option_quantity("PE"))),
        })

    # -------------------------------------------------------------- results

    def results(self):
        lot_for = self.config.lot_size_on
        trades = pd.DataFrame([
            leg.as_row(lot_for(leg.exit_time.date() if leg.exit_time else leg.entry_time.date()))
            for leg in self.portfolio.closed_legs + self.portfolio.open_legs
        ])
        equity = pd.DataFrame(self.equity)
        events, warnings = self.journal.frames()
        return {"trades": trades, "equity": equity,
                "events": events, "warnings": warnings}
