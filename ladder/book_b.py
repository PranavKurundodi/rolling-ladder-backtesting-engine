"""Book B - the futures trend overlay.

Three states, two lots moving on every transition:

    BASE   +1 near / -1 far   flat, a calendar spread
    LONG   +2 near            +2 lots
    SHORT  -2 far             -2 lots

There is no direct LONG <-> SHORT transition.  Every reversal parks at BASE and
needs a second signal to flip through, so a whipsaw costs two transitions
rather than one.

Near and far month are fixed by calendar date, not by which contract happens to
be nearest expiry: on the 1st-14th they are the current and next month, from
the 15th they are the next month and the one after.  Contracts roll on the 15th
and the roll is state-preserving -- it changes contracts, never exposure, and
is never itself a signal.
"""

from collections import Counter, defaultdict
from datetime import date, timedelta

from .positions import FAR, FUTURE, NEAR
from .signals import bearish, bullish

BASE, LONG, SHORT = "BASE", "LONG", "SHORT"


def month_offset(day: date, months: int):
    """First day of the month `months` after `day`'s month."""
    total = day.year * 12 + (day.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


class FuturesTrendOverlay:
    def __init__(self, config, market, portfolio, journal, calendar):
        self.config = config
        self.settings = config.book_b
        self.market = market
        self.portfolio = portfolio
        self.journal = journal
        self.calendar = calendar
        self.state = None
        self.contracts = None       # (near_expiry, far_expiry) currently held

    # ---------------------------------------------------- contract calendar

    def contract_pair(self, day: date):
        """The near/far contract pair for `day` under the calendar-date rule."""
        shift = 0 if day.day < self.settings.contract_roll_day else 1
        wanted = (month_offset(day, shift), month_offset(day, shift + 1))
        available = self.market.future_expiries_on(day)
        pair = []
        for month_start in wanted:
            match = [e for e in available
                     if (e.year, e.month) == (month_start.year, month_start.month)]
            pair.append(match[0] if match else None)
        return tuple(pair)

    def roll_day_for(self, day: date):
        """The 15th of `day`'s month, moved forward if the exchange was closed."""
        target = day.replace(day=self.settings.contract_roll_day)
        return self.calendar.next_trading_day_on_or_after(target)

    def _pair_for_decision(self, day):
        """Contracts a transition should enter, looking through an imminent roll.

        A transition two or three days before the 15th would otherwise open a
        position that gets rolled out days later, paying the spread twice.
        """
        roll_day = self.roll_day_for(day)
        window = timedelta(days=self.settings.pre_roll_window_days)
        if day < roll_day <= day + window:
            return self.contract_pair(roll_day)
        return self.contract_pair(day)

    # ------------------------------------------------------------ mechanics

    def _price(self, day, expiry, cutoff_time):
        bars = self.market.future_bars(day, expiry)
        if bars is None or bars.empty:
            return None
        bars = bars[bars.index.time <= cutoff_time]
        return float(bars["Close"].iloc[-1]) if not bars.empty else None

    def _target_legs(self, state, pair):
        """The exact one-lot legs a state is made of.

        Futures exposure is carried as one-lot legs rather than a single netted
        position, because every spec transition is described leg by leg -- "buy
        back the far-month short; buy 1 more near-month lot" -- and keeps the
        leg it does not mention.  One-lot legs let the engine leave that leg
        untouched, which a netted position cannot express.
        """
        near, far = pair
        lots = self.settings.lots_per_transition
        if state == BASE:
            return [(near, +1), (far, -1)]
        if state == LONG:
            return [(near, +1)] * lots
        if state == SHORT:
            return [(far, -1)] * lots
        raise ValueError(f"unknown state {state}")

    def _rebalance(self, day, when, target_state, pair, reason):
        """Move only the lots the transition actually calls for.

        Legs already matching the target are held, so a BASE -> LONG keeps the
        near-month lot it already owns instead of round-tripping it.  With zero
        slippage that is P&L-neutral, but it keeps entry prices, holding
        periods and the trade count honest.
        """
        wanted = Counter()
        for expiry, quantity in self._target_legs(target_state, pair):
            if expiry is None:
                self.journal.warn(day, "book_b", f"{target_state}: contract missing")
                continue
            wanted[(expiry, quantity)] += 1

        held = defaultdict(list)
        for leg in self.portfolio.legs(book="B", kind=FUTURE):
            held[(leg.expiry, leg.quantity)].append(leg)

        # Close what the target no longer wants, newest lot first.
        for key, legs in held.items():
            excess = len(legs) - wanted.get(key, 0)
            for leg in sorted(legs, key=lambda l: l.entry_time, reverse=True)[:max(0, excess)]:
                price = self._price(day, leg.expiry, when.time())
                if price is None:
                    self.journal.warn(day, "book_b", f"no quote to close {leg.symbol}")
                    price = leg.entry_price
                self.portfolio.close_leg(leg, price, when, reason)

        # Open whatever is still short of the target.
        near, _ = pair
        for (expiry, quantity), count in wanted.items():
            shortfall = count - len(held.get((expiry, quantity), []))
            for _ in range(max(0, shortfall)):
                price = self._price(day, expiry, when.time())
                if price is None:
                    self.journal.warn(day, "book_b", f"{target_state}: no quote for {expiry}")
                    continue
                self.portfolio.open_leg(
                    book="B", role=NEAR if expiry == near else FAR, kind=FUTURE,
                    expiry=expiry, quantity=quantity, entry_price=price, entry_time=when,
                )
        self.state = target_state
        self.contracts = pair

    def initiate(self, day, when):
        pair = self.contract_pair(day)
        self._rebalance(day, when, BASE, pair, "initiate")
        self.journal.event(day, when, "book_b", "initiate", f"BASE on {pair}")

    # ---------------------------------------------------------------- roll

    def maybe_roll(self, day, when):
        """State-preserving contract roll on the 15th (or the next trading day).

        Driven by comparing the pair the calendar rule now calls for against
        the pair actually held, rather than by testing the date.  A date test
        misses the roll entirely when the 15th is a holiday or the archive has
        no bars for it, and the book then trades a stale contract until expiry;
        comparing what is held is self-correcting.  It is also naturally quiet
        when a transition already entered the post-roll pair early.
        """
        if self.state is None:
            return False
        pair = self.contract_pair(day)
        if pair == self.contracts or None in pair:
            return False
        previous = self.contracts
        self._rebalance(day, when, self.state, pair, "contract_roll")
        self.journal.event(day, when, "book_b", "contract_roll",
                           f"{self.state}: {previous} -> {pair}")
        return True

    # ---------------------------------------------------------- transitions

    def maybe_transition(self, day, when, trend_state, stops_today):
        """Apply at most one state transition, per the spec's transition table."""
        if self.state is None or trend_state is None or not trend_state.ema_slow:
            return False
        is_bull = bullish(trend_state, "CE" in stops_today)
        is_bear = bearish(trend_state, "PE" in stops_today)

        target = None
        if self.state == BASE and is_bull:
            target = LONG
        elif self.state == BASE and is_bear:
            target = SHORT
        elif self.state == LONG and is_bear:
            target = BASE
        elif self.state == SHORT and is_bull:
            target = BASE
        # LONG + bullish and SHORT + bearish are re-triggers of a state already
        # held, and are ignored.
        if target is None:
            return False

        pair = self._pair_for_decision(day)
        previous = self.state
        self._rebalance(day, when, target, pair, f"transition_{previous}_to_{target}")
        self.journal.event(day, when, "book_b", "transition",
                           f"{previous} -> {target} "
                           f"(price {trend_state.price:.1f} ema20 {trend_state.ema_slow:.1f} "
                           f"hh={trend_state.higher_highs} ll={trend_state.lower_lows} "
                           f"stops={sorted(stops_today)})")
        return True

    def close_expiring(self, day, when):
        """Safety net: the 15th roll should always pre-empt expiry."""
        for leg in self.portfolio.legs(book="B", kind=FUTURE):
            if leg.days_to_expiry(day) <= 0:
                price = self._price(day, leg.expiry, when.time())
                self.portfolio.close_leg(leg, price if price is not None else leg.entry_price,
                                         when, "expiry")
                self.journal.warn(day, "book_b", f"{leg.symbol} reached expiry unrolled")
