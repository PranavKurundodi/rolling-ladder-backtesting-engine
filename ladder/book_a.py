"""Book A - the rolling options ladder.

Eight legs at all times: six sold, two bought.  Sold legs sit on three
consecutive expiries at target premiums of 150 / 200 / 250; the two hedges sit
on the nearest expiry at 100.

The three premium targets are one rule at three ages, not three rules.  A leg
sold at 250 with 29 days to run is worth about 200 a week later and about 150
the week after, so at each roll the surviving legs are relabelled rather than
re-struck -- only the new outermost rung is actually sold.  That is why the
roll opens just two sold legs and two hedges however long the book has run.
"""

from datetime import datetime

from .positions import OPTION, SOLD_A, SOLD_B, SOLD_C, HEDGE
from .selection import pick_by_premium, scan_chain

SOLD_ROLES = (SOLD_A, SOLD_B, SOLD_C)
OPTION_TYPES = ("CE", "PE")


class RollingOptionsLadder:
    def __init__(self, config, market, portfolio, journal):
        self.config = config
        self.settings = config.book_a
        self.market = market
        self.portfolio = portfolio
        self.journal = journal
        self.expiries = {}          # role -> expiry date
        self.stops_today = set()    # option types stopped out before today's decision
        self.initiated = False

    # ------------------------------------------------------------ selection

    def _quote_for(self, day, expiry, option_type, target, spot, cutoff):
        rules = self.settings.liquidity
        targets = tuple(self.settings.sell_targets) + (self.settings.hedge_target,)
        chain = scan_chain(self.market, day, expiry, option_type, spot,
                           targets, rules, cutoff)
        return pick_by_premium(chain, target, tolerance=rules.premium_tolerance)

    def _open(self, day, when, expiry, option_type, target, spot, role, quantity):
        quote = self._quote_for(day, expiry, option_type, target, spot, when.time())
        if quote is None:
            self.journal.warn(day, "book_a", f"no fillable {option_type} near {target} on {expiry}")
            return None
        return self.portfolio.open_leg(
            book="A", role=role, kind=OPTION, expiry=expiry,
            strike=quote.strike, option_type=option_type,
            quantity=quantity, entry_price=quote.price, entry_time=when,
            target_premium=target,
        )

    # ----------------------------------------------------------- initiation

    def initiate(self, day, when, spot):
        """Open the full ladder: sell on three expiries, hedge the nearest."""
        expiries = self.market.option_ladder(
            day, self.settings.initiation_min_dte, len(SOLD_ROLES)
        )
        if len(expiries) < len(SOLD_ROLES):
            self.journal.warn(day, "book_a", "fewer than three expiries beyond the DTE filter")
            return False
        self.expiries = dict(zip(SOLD_ROLES, expiries))
        for role, target in zip(SOLD_ROLES, self.settings.sell_targets):
            for option_type in OPTION_TYPES:
                self._open(day, when, self.expiries[role], option_type,
                           target, spot, role, quantity=-1)
        for option_type in OPTION_TYPES:
            self._open(day, when, self.expiries[SOLD_A], option_type,
                       self.settings.hedge_target, spot, HEDGE, quantity=+1)
        self.initiated = True
        self.journal.event(day, when, "book_a", "initiate",
                           f"A={self.expiries[SOLD_A]} B={self.expiries[SOLD_B]} C={self.expiries[SOLD_C]}")
        return True

    # ------------------------------------------------------------- the roll

    def needs_roll(self, day):
        front = self.expiries.get(SOLD_A)
        if front is None:
            return False
        return (front - day).days <= self.settings.roll_trigger_dte

    def roll(self, day, when, spot):
        """Close the front expiry, sell a new outermost rung, re-hedge.

        Executed as one action, in the spec's order: close all four A legs, sell
        the new D rung at 250, buy the new hedges at 100 on B.  B then becomes
        A, C becomes B, D becomes C.
        """
        expiry_c = self.expiries[SOLD_C]
        expiry_d = self.market.next_expiry_after(day, expiry_c)
        if expiry_d is None:
            self.journal.warn(day, "book_a", f"no expiry beyond {expiry_c}; roll deferred")
            return False

        # 1. Close every leg on the front expiry -- both sold and both bought.
        for leg in self.portfolio.legs(book="A"):
            if leg.expiry == self.expiries[SOLD_A]:
                price = self.price_of(day, leg, when.time())
                if price is None:
                    self.journal.warn(day, "book_a", f"no quote to close {leg.symbol}")
                    price = leg.entry_price
                    reason = "roll_no_quote"
                else:
                    reason = "roll"
                self.portfolio.close_leg(leg, price, when, reason)

        # 2. Relabel the survivors: B becomes A, C becomes B.
        for leg in self.portfolio.legs(book="A", role=SOLD_B):
            leg.role = SOLD_A
        for leg in self.portfolio.legs(book="A", role=SOLD_C):
            leg.role = SOLD_B
        self.expiries = {
            SOLD_A: self.expiries[SOLD_B],
            SOLD_B: expiry_c,
            SOLD_C: expiry_d,
        }

        # 3. Sell the new outermost rung and buy hedges on the new front expiry.
        for option_type in OPTION_TYPES:
            self._open(day, when, expiry_d, option_type,
                       self.settings.sell_targets[-1], spot, SOLD_C, quantity=-1)
        for option_type in OPTION_TYPES:
            self._open(day, when, self.expiries[SOLD_A], option_type,
                       self.settings.hedge_target, spot, HEDGE, quantity=+1)

        # 4. The spec's roll ends with the book back at six sells and two buys,
        #    so any slot vacated by a stop earlier in the week is refilled here.
        self._refill_vacancies(day, when, spot)
        self.journal.event(day, when, "book_a", "roll",
                           f"A={self.expiries[SOLD_A]} B={self.expiries[SOLD_B]} C={self.expiries[SOLD_C]}")
        return True

    def _refill_vacancies(self, day, when, spot):
        held = {(l.role, l.option_type) for l in self.portfolio.legs(book="A")}
        for role, target in zip(SOLD_ROLES, self.settings.sell_targets):
            for option_type in OPTION_TYPES:
                if (role, option_type) not in held:
                    leg = self._open(day, when, self.expiries[role], option_type,
                                     target, spot, role, quantity=-1)
                    if leg is not None:
                        self.journal.event(day, when, "book_a", "refill",
                                           f"{role} {leg.symbol} @ {leg.entry_price:.2f}")
        for option_type in OPTION_TYPES:
            if (HEDGE, option_type) not in held:
                self._open(day, when, self.expiries[SOLD_A], option_type,
                           self.settings.hedge_target, spot, HEDGE, quantity=+1)

    # ----------------------------------------------------------------- risk

    def check_stops(self, day, start_time=None, end_time=None):
        """Walk the session minute by minute and stop out breached sold legs.

        The stop is 70 premium points on the option's own price, so it is
        resolved against each leg's own bars rather than the index.  A leg that
        opens a bar already through its stop fills at that bar's open; anything
        else fills at the stop.

        The window is bounded so the pre-decision and post-decision parts of
        the session are walked separately: a stop that fires at 15:20 must not
        feed a signal that was evaluated at 15:15.
        """
        for leg in self.portfolio.legs(book="A", role=set(SOLD_ROLES)):
            bars = self.market.option_bars(day, leg.expiry, leg.strike, leg.option_type)
            if bars is None or bars.empty:
                continue
            if start_time is not None:
                bars = bars[bars.index.time >= start_time]
            if end_time is not None:
                bars = bars[bars.index.time <= end_time]
            # A leg opened partway through the session cannot stop out earlier.
            bars = bars[bars.index >= leg.entry_time]
            if bars.empty:
                continue
            breached = bars[bars["High"] >= leg.stop_price]
            if breached.empty:
                continue
            bar = breached.iloc[0]
            when = breached.index[0].to_pydatetime()
            fill = max(float(bar["Open"]), leg.stop_price)
            self.portfolio.close_leg(leg, fill, when, "stop")
            self.stops_today.add(leg.option_type)
            self.journal.event(day, when, "book_a", "stop",
                               f"{leg.symbol} entry {leg.entry_price:.2f} stop {leg.stop_price:.2f} fill {fill:.2f}")

    def close_expiring(self, day, when):
        """Safety net: nothing in this book is meant to reach expiry."""
        for leg in self.portfolio.legs(book="A"):
            if leg.days_to_expiry(day) <= 0:
                price = self.price_of(day, leg, when.time())
                self.portfolio.close_leg(leg, price if price is not None else leg.entry_price,
                                         when, "expiry")
                self.journal.warn(day, "book_a", f"{leg.symbol} reached expiry unrolled")

    # --------------------------------------------------------------- prices

    def price_of(self, day, leg, cutoff_time=None):
        bars = self.market.option_bars(day, leg.expiry, leg.strike, leg.option_type)
        if bars is None or bars.empty:
            return None
        if cutoff_time is not None:
            bars = bars[bars.index.time <= cutoff_time]
            if bars.empty:
                return None
        return float(bars["Close"].iloc[-1])
