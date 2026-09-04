"""Positions and the portfolio that carries them across days.

The template's Trade was single-leg and long-only.  This strategy holds eight
option legs and one or two futures legs at once, six of the options are sold,
and every leg outlives the session it was opened in, so quantity is signed and
the portfolio is the thing that persists.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import count

OPTION = "OPTION"
FUTURE = "FUTURE"

# Book A leg roles.  A/B/C track the premium rung, not the expiry label, and
# are relabelled at each roll as expiries walk down the ladder.
SOLD_A, SOLD_B, SOLD_C, HEDGE = "sold_A", "sold_B", "sold_C", "hedge"
NEAR, FAR = "near", "far"          # Book B futures legs

_ids = count(1)


@dataclass
class Leg:
    book: str
    role: str
    kind: str
    expiry: date
    quantity: int                  # signed, in lots: negative is sold
    entry_price: float
    entry_time: datetime
    strike: int = None
    option_type: str = None
    stop_price: float = None
    target_premium: float = None
    leg_id: int = field(default_factory=lambda: next(_ids))
    exit_price: float = None
    exit_time: datetime = None
    exit_reason: str = None
    entry_slippage: float = 0.0
    exit_slippage: float = 0.0

    @property
    def is_sold(self):
        return self.quantity < 0

    @property
    def symbol(self):
        if self.kind == FUTURE:
            return f"{self.expiry:%d%b%y}".upper() + "FUT"
        return f"{self.expiry:%d%b%y}".upper() + f"{self.strike}{self.option_type}"

    def days_to_expiry(self, day):
        return (self.expiry - day).days

    def points(self, price=None):
        """Signed P&L in index points per lot."""
        reference = self.exit_price if price is None else price
        if reference is None:
            return 0.0
        return (reference - self.entry_price) * self.quantity

    def as_row(self, lot_size):
        gross = self.points()
        return {
            "leg_id": self.leg_id,
            "book": self.book,
            "role": self.role,
            "kind": self.kind,
            "symbol": self.symbol,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "quantity_lots": self.quantity,
            "target_premium": self.target_premium,
            "entry_time": self.entry_time,
            "entry_price": self.entry_price,
            "exit_time": self.exit_time,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "stop_price": self.stop_price,
            "days_held": (self.exit_time.date() - self.entry_time.date()).days
            if self.exit_time else None,
            "dte_at_entry": (self.expiry - self.entry_time.date()).days,
            "points": gross,
            "lot_size": lot_size,
            "rupees": gross * lot_size,
            "slippage_points": self.entry_slippage + self.exit_slippage,
        }


class Portfolio:
    """Open legs, closed legs, and the running P&L across the whole backtest."""

    def __init__(self, config):
        self.config = config
        self.open_legs = []
        self.closed_legs = []
        self.realized_points = 0.0
        self.realized_rupees = 0.0

    # ------------------------------------------------------------- mutation

    def open_leg(self, **kwargs):
        slippage = self.config.execution.slippage_points
        leg = Leg(**kwargs)
        # Slippage always works against the trade: pay up to buy, receive less
        # to sell.
        if slippage:
            leg.entry_price += slippage if leg.quantity > 0 else -slippage
            leg.entry_slippage = slippage
        if leg.is_sold and leg.kind == OPTION and leg.role != HEDGE:
            leg.stop_price = leg.entry_price + self.config.book_a.stop_premium_points
        self.open_legs.append(leg)
        return leg

    def close_leg(self, leg, price, when, reason):
        slippage = self.config.execution.slippage_points
        if slippage:
            # Closing reverses the sign of the exposure.
            price += slippage if leg.quantity < 0 else -slippage
            leg.exit_slippage = slippage
        leg.exit_price = price
        leg.exit_time = when
        leg.exit_reason = reason
        self.open_legs.remove(leg)
        self.closed_legs.append(leg)
        lot = self.config.lot_size_on(when.date())
        self.realized_points += leg.points()
        self.realized_rupees += leg.points() * lot
        return leg

    # -------------------------------------------------------------- queries

    def legs(self, book=None, role=None, kind=None):
        out = self.open_legs
        if book is not None:
            out = [l for l in out if l.book == book]
        if role is not None:
            roles = role if isinstance(role, (set, tuple, list)) else {role}
            out = [l for l in out if l.role in roles]
        if kind is not None:
            out = [l for l in out if l.kind == kind]
        return list(out)

    def unrealized_points(self, price_lookup):
        """Mark open legs against a `leg -> price` lookup; unpriced legs are skipped."""
        total = 0.0
        for leg in self.open_legs:
            price = price_lookup(leg)
            if price is not None:
                total += leg.points(price)
        return total

    def net_option_quantity(self, option_type):
        return sum(l.quantity for l in self.open_legs
                   if l.kind == OPTION and l.option_type == option_type)

    def net_future_quantity(self):
        return sum(l.quantity for l in self.open_legs if l.kind == FUTURE)
