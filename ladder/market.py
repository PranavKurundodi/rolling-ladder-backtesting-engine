"""Market data access for the rolling-ladder backtest.

Reads the on-disk parquet tree:

    {root}/YYYY/MM/DD/INDIA/NSE/OPTIONS/NIFTY/1minute/NIFTY{DDMMMYY}{strike}{CE|PE}.parquet
    {root}/YYYY/MM/DD/INDIA/NSE/FUTURES/NIFTY/1minute/NIFTY{DDMMMYY}FUT.parquet
    {root}/YYYY/MM/DD/INDIA/NSE/INDICES/1minute/NIFTY 50.parquet

Expiry dates come from {root}/contract_index.parquet, which is built from the
exchange's own contract master.  The spec calls for exactly that: expiries are
read, never computed, so shifted expiries (Republic Day 2027, for example) are
handled without a special case.
"""

from datetime import date, datetime
from pathlib import Path

import pandas as pd

SEGMENT_SYMBOL = {"nifty": "NIFTY", "bank_nifty": "BANKNIFTY", "sensex": "SENSEX"}
SEGMENT_INDEX_FILE = {
    "nifty": "NIFTY 50",
    "bank_nifty": "NIFTY BANK",
    "sensex": "SENSEX",
}


def expiry_token(expiry: date) -> str:
    """2026-04-07 -> '07APR26', the form used in parquet filenames."""
    return expiry.strftime("%d%b%y").upper()


class MarketData:
    def __init__(self, data_root, market_segment="nifty"):
        self.root = Path(data_root)
        self.segment = market_segment
        self.symbol = SEGMENT_SYMBOL.get(market_segment, "NIFTY")
        self.index_filename = SEGMENT_INDEX_FILE.get(market_segment, "NIFTY 50")

        index_path = self.root / "contract_index.parquet"
        if not index_path.exists():
            raise FileNotFoundError(f"contract_index.parquet not found under {self.root}")
        idx = pd.read_parquet(index_path)
        idx = idx[idx["symbol"] == self.symbol].copy()
        idx["trade_date"] = idx["trade_date"].dt.date
        idx["expiry"] = idx["expiry"].dt.date
        self.contracts = idx
        self._by_day = {d: g for d, g in idx.groupby("trade_date", sort=True)}
        self._bar_cache = {}
        self._cached_day = None

    # ---------------------------------------------------------------- paths

    def _day_dir(self, day: date) -> Path:
        return self.root / f"{day:%Y/%m/%d}" / "INDIA" / "NSE"

    def option_path(self, day, expiry, strike, option_type) -> Path:
        name = f"{self.symbol}{expiry_token(expiry)}{int(strike)}{option_type}.parquet"
        return self._day_dir(day) / "OPTIONS" / self.symbol / "1minute" / name

    def future_path(self, day, expiry) -> Path:
        name = f"{self.symbol}{expiry_token(expiry)}FUT.parquet"
        return self._day_dir(day) / "FUTURES" / self.symbol / "1minute" / name

    def index_path(self, day) -> Path:
        return self._day_dir(day) / "INDICES" / "1minute" / f"{self.index_filename}.parquet"

    # ------------------------------------------------------------- calendar

    def trading_days(self, start: date, end: date):
        """Days that have both a contract-index entry and an index bar file."""
        days = [d for d in self._by_day if start <= d <= end]
        return sorted(d for d in days if self.index_path(d).exists())

    def expiries_on(self, day: date) -> pd.DataFrame:
        """Distinct option expiries listed for `day`, nearest first."""
        frame = self._by_day.get(day)
        if frame is None:
            return pd.DataFrame(columns=["expiry", "days_to_expiry", "n_strikes"])
        out = (
            frame.groupby("expiry", as_index=False)
            .agg(days_to_expiry=("days_to_expiry", "first"), n_strikes=("strike", "nunique"))
            .sort_values("expiry")
            .reset_index(drop=True)
        )
        return out

    def option_ladder(self, day: date, min_dte: int, count: int):
        """The `count` nearest expiries at least `min_dte` days out.

        Inclusive of `min_dte` itself.  The spec's Quick Reference says
        "> 15 days" while its structure table puts the front rung at ~15 DTE;
        the inclusive reading is the one that matches the table.  It also
        matters more than it looks: skipping an expiry exactly 15 days out
        starts the ladder one rung further out, and because the exchange lists
        only about five weeklies ahead, the third rung then falls past the last
        listed weekly onto a monthly -- which the roll can never walk back.
        """
        table = self.expiries_on(day)
        eligible = table[table["days_to_expiry"] >= min_dte]
        return list(eligible["expiry"].head(count))

    def next_expiry_after(self, day: date, expiry: date):
        table = self.expiries_on(day)
        later = table[table["expiry"] > expiry]
        return later["expiry"].iloc[0] if len(later) else None

    def strikes_on(self, day, expiry, option_type):
        frame = self._by_day.get(day)
        if frame is None:
            return []
        sel = frame[(frame["expiry"] == expiry) & (frame["option_type"] == option_type)]
        return sorted(int(s) for s in sel["strike"].unique())

    def future_expiries_on(self, day: date):
        """Futures contracts trading on `day`, nearest expiry first.

        Read from the filenames rather than the contract index, because the
        index describes the option chain.
        """
        folder = self._day_dir(day) / "FUTURES" / self.symbol / "1minute"
        if not folder.exists():
            return []
        found = []
        prefix, suffix = self.symbol, "FUT.parquet"
        for entry in folder.iterdir():
            name = entry.name
            if not (name.startswith(prefix) and name.endswith(suffix)):
                continue
            token = name[len(prefix): -len(suffix)]
            try:
                found.append(datetime.strptime(token, "%d%b%y").date())
            except ValueError:
                continue
        return sorted(found)

    # ----------------------------------------------------------------- bars

    def _read(self, path: Path):
        if not path.exists():
            return None
        try:
            frame = pd.read_parquet(path)
        except Exception:
            return None
        if frame.empty:
            return None
        if "Timestamp" in frame.columns:
            frame = frame.copy()
            frame["Timestamp"] = pd.to_datetime(frame["Timestamp"])
            frame = frame.set_index("Timestamp")
        elif not isinstance(frame.index, pd.DatetimeIndex):
            return None
        # Schemas are not uniform across the archive: some files carry no OI
        # column, and a few carry nothing but Timestamp.  Anything without a
        # full OHLC set is unusable.
        if not {"Open", "High", "Low", "Close"}.issubset(frame.columns):
            return None
        for column in ("Open", "High", "Low", "Close"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if "Volume" in frame.columns:
            frame["Volume"] = pd.to_numeric(frame["Volume"], errors="coerce").fillna(0)
        else:
            frame["Volume"] = 0.0
        # Absent OI must stay absent: filling it with zero would be
        # indistinguishable from a genuinely zero-OI contract.
        if "OI" in frame.columns:
            frame["OI"] = pd.to_numeric(frame["OI"], errors="coerce")
        else:
            frame["OI"] = float("nan")
        frame = frame[frame["Close"].notna()]
        return frame.sort_index() if not frame.empty else None

    def _cached(self, key, path):
        """Bar cache, scoped to one trading day to bound memory."""
        day = key[0]
        if self._cached_day != day:
            self._bar_cache.clear()
            self._cached_day = day
        if key not in self._bar_cache:
            self._bar_cache[key] = self._read(path)
        return self._bar_cache[key]

    def option_bars(self, day, expiry, strike, option_type):
        key = (day, "O", expiry, int(strike), option_type)
        return self._cached(key, self.option_path(day, expiry, strike, option_type))

    def future_bars(self, day, expiry):
        key = (day, "F", expiry, 0, "")
        return self._cached(key, self.future_path(day, expiry))

    def index_bars(self, day):
        key = (day, "I", None, 0, "")
        return self._cached(key, self.index_path(day))

    # ------------------------------------------------------------ daily spot

    def intraday_high_low_to(self, day, cutoff_time):
        """High/low/last of the index up to `cutoff_time` on `day`.

        Signals evaluated at the decision time may only use what has printed by
        then; this is what keeps the 15:15 fill convention free of lookahead.
        """
        bars = self.index_bars(day)
        if bars is None or bars.empty:
            return None
        window = bars[bars.index.time <= cutoff_time]
        if window.empty:
            return None
        return {
            "High": float(window["High"].max()),
            "Low": float(window["Low"].min()),
            "Close": float(window["Close"].iloc[-1]),
        }
