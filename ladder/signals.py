"""Book B trend signals.

    Bullish = (spot > 20EMA OR 10EMA crosses above 20EMA)
              AND (3 higher highs OR short-call stop hit)
    Bearish = (spot < 20EMA OR 10EMA crosses below 20EMA)
              AND (3 lower lows  OR short-put stop hit)

Everything is computed on completed daily bars up to the previous session, plus
the current session's action up to the decision time.  Nothing reads a price
that had not printed when the decision was taken.
"""

from dataclasses import dataclass


@dataclass
class TrendState:
    price: float
    ema_fast: float
    ema_slow: float
    ema_fast_prev: float
    ema_slow_prev: float
    higher_highs: bool
    lower_lows: bool

    @property
    def crossed_above(self):
        return self.ema_fast_prev <= self.ema_slow_prev and self.ema_fast > self.ema_slow

    @property
    def crossed_below(self):
        return self.ema_fast_prev >= self.ema_slow_prev and self.ema_fast < self.ema_slow


class TrendTracker:
    """Incremental EMAs plus the consecutive higher-high / lower-low counters.

    Fed one completed daily bar at a time, in order, so the backtest never has
    access to a bar that had not closed.
    """

    def __init__(self, fast_span, slow_span, streak_length):
        self.fast_span = fast_span
        self.slow_span = slow_span
        self.streak_length = streak_length
        self.ema_fast = None
        self.ema_slow = None
        self.prev_fast = None
        self.prev_slow = None
        self._seeds = []
        self.prev_high = None
        self.prev_low = None
        self.rising_highs = 0
        self.falling_lows = 0

    @staticmethod
    def _step(previous, value, span):
        alpha = 2.0 / (span + 1.0)
        return value if previous is None else previous + alpha * (value - previous)

    def update(self, high, low, close):
        self.prev_fast, self.prev_slow = self.ema_fast, self.ema_slow
        # Seed both EMAs with a simple mean of the first `slow_span` closes so
        # early values are not dominated by the first print.
        if self.ema_slow is None:
            self._seeds.append(close)
            if len(self._seeds) >= self.slow_span:
                seed = sum(self._seeds) / len(self._seeds)
                self.ema_fast = self.ema_slow = seed
                self.prev_fast = self.prev_slow = seed
        else:
            self.ema_fast = self._step(self.ema_fast, close, self.fast_span)
            self.ema_slow = self._step(self.ema_slow, close, self.slow_span)

        if self.prev_high is not None:
            self.rising_highs = self.rising_highs + 1 if high > self.prev_high else 0
        if self.prev_low is not None:
            self.falling_lows = self.falling_lows + 1 if low < self.prev_low else 0
        self.prev_high, self.prev_low = high, low

    @property
    def ready(self):
        return self.ema_slow is not None

    def state(self, price, live_high=None, live_low=None):
        """Signal inputs as of the decision time.

        `live_high` / `live_low` are the current session's extremes so far.  A
        streak that needs three consecutive higher highs may be completed by
        today's high, which is known at the decision time.
        """
        rising, falling = self.rising_highs, self.falling_lows
        if live_high is not None and self.prev_high is not None:
            rising = rising + 1 if live_high > self.prev_high else 0
        if live_low is not None and self.prev_low is not None:
            falling = falling + 1 if live_low < self.prev_low else 0
        return TrendState(
            price=price,
            ema_fast=self.ema_fast,
            ema_slow=self.ema_slow,
            ema_fast_prev=self.prev_fast,
            ema_slow_prev=self.prev_slow,
            higher_highs=rising >= self.streak_length,
            lower_lows=falling >= self.streak_length,
        )


def bullish(state, short_call_stop_hit):
    trend = state.price > state.ema_slow or state.crossed_above
    confirm = state.higher_highs or short_call_stop_hit
    return bool(trend and confirm)


def bearish(state, short_put_stop_hit):
    trend = state.price < state.ema_slow or state.crossed_below
    confirm = state.lower_lows or short_put_stop_hit
    return bool(trend and confirm)
