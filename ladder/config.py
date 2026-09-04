"""Configuration schema for the rolling-ladder backtest.

Every parameter the spec pins down has a default here; every parameter the spec
leaves open is named explicitly so the choice is visible rather than buried in
code.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml


def _as_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value).replace("-", "/"), "%Y/%m/%d").date()


@dataclass
class LiquidityFilter:
    """Guards premium-based strike selection against stale prints.

    A strike file exists whether or not the contract traded, and a contract
    that has not traded carries its last price forward.  Read at the open those
    stale prints wreck the premium curve; read at the 15:15 decision time the
    curve is clean apart from genuinely untraded strikes.  These rules remove
    those, and are relative to the chain rather than absolute because a far
    weekly expiry trades orders of magnitude thinner than a near one.
    """

    strike_step: int = 50               # only consider strikes on this grid
    min_bars: int = 3                   # must have printed at least a few times
    max_staleness_minutes: int = 15     # last print must be recent at the decision time
    min_volume: float = 1.0             # must have traded at all
    min_volume_fraction: float = 0.02   # ...and at 2% of the chain's median volume
    min_open_interest: float = 0.0      # OI is absent from much of the archive; off by default
    enforce_monotonic: bool = True      # drop strikes that violate the premium curve
    premium_tolerance: float = 15.0     # within this of target, prefer the more liquid strike


@dataclass
class BookAConfig:
    enabled: bool = True
    sell_targets: tuple = (150.0, 200.0, 250.0)   # A / B / C premium targets
    hedge_target: float = 100.0                   # bought legs, nearest expiry only
    initiation_min_dte: int = 15                  # "nearest expiry more than 15 days out"
    roll_trigger_dte: int = 8                     # roll when A reaches this DTE
    stop_premium_points: float = 70.0             # on sold legs, on the option's own price
    post_stop_policy: str = "leave_empty"         # slot stays vacant until the roll
    liquidity: LiquidityFilter = field(default_factory=LiquidityFilter)


@dataclass
class BookBConfig:
    enabled: bool = True
    ema_fast: int = 10
    ema_slow: int = 20
    higher_high_count: int = 3      # "three consecutive sessions"
    contract_roll_day: int = 15     # calendar day; next trading day if closed
    pre_roll_window_days: int = 3   # transitions this close to the roll use post-roll contracts
    lots_per_transition: int = 2


@dataclass
class ExecutionConfig:
    decision_time: str = "15:15"    # rolls and transitions execute here
    slippage_points: float = 0.0    # per leg, applied against the trade


@dataclass
class BacktestConfig:
    name: str = "nifty_rolling_ladder"
    market_segment: str = "nifty"
    data_root: Path = Path("../data")
    start_date: date = date(2024, 1, 1)
    end_date: date = date(2026, 4, 28)
    reports_root: Path = Path("reports")
    # NIFTY contract size changed during the backtest window; P&L in rupees
    # depends on it, P&L in points does not.
    lot_size_schedule: tuple = ((date(2024, 1, 1), 25), (date(2024, 11, 20), 75))
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    book_a: BookAConfig = field(default_factory=BookAConfig)
    book_b: BookBConfig = field(default_factory=BookBConfig)

    def lot_size_on(self, day: date) -> int:
        size = self.lot_size_schedule[0][1]
        for effective_from, value in self.lot_size_schedule:
            if day >= effective_from:
                size = value
        return size

    @property
    def report_dir(self) -> Path:
        return Path(self.reports_root) / self.name


def _build(cls, payload):
    """Shallow-construct a dataclass from a dict, ignoring unknown keys."""
    if not payload:
        return cls()
    known = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(payload) - known
    if unknown:
        raise ValueError(f"{cls.__name__}: unknown config keys {sorted(unknown)}")
    return cls(**{k: v for k, v in payload.items() if k in known})


def load_config(path="ladder_config.yaml") -> BacktestConfig:
    with open(path) as handle:
        raw = yaml.safe_load(handle)
    if isinstance(raw, list):
        raw = raw[0]

    book_a = dict(raw.pop("book_a", {}) or {})
    liquidity = _build(LiquidityFilter, book_a.pop("liquidity", {}))
    book_a_cfg = _build(BookAConfig, book_a)
    book_a_cfg.liquidity = liquidity
    book_a_cfg.sell_targets = tuple(float(x) for x in book_a_cfg.sell_targets)

    book_b_cfg = _build(BookBConfig, raw.pop("book_b", {}) or {})
    execution_cfg = _build(ExecutionConfig, raw.pop("execution", {}) or {})

    lots = raw.pop("lot_size_schedule", None)
    cfg = _build(BacktestConfig, raw)
    cfg.book_a, cfg.book_b, cfg.execution = book_a_cfg, book_b_cfg, execution_cfg
    cfg.data_root = Path(cfg.data_root)
    cfg.reports_root = Path(cfg.reports_root)
    cfg.start_date = _as_date(cfg.start_date)
    cfg.end_date = _as_date(cfg.end_date)
    if lots:
        cfg.lot_size_schedule = tuple((_as_date(d), int(s)) for d, s in lots)

    if cfg.book_a.post_stop_policy != "leave_empty":
        raise NotImplementedError(
            "post_stop_policy: only 'leave_empty' is implemented. "
            "'refill' and 'close_pair' are the other two options the spec leaves open."
        )
    return cfg
