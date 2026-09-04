# Nifty Rolling Options Ladder with Futures Trend Overlay

A backtest of the strategy specified in `nifty_strategy_spec.pdf` v1.0, run
against a **local archive of NSE Nifty 1-minute data**. There is no broker
connection, no database, no live trading and no cloud dependency — the engine
reads parquet files off disk and writes CSV reports.

[AGENTS.md](AGENTS.md) has the implementation detail. This file is the
orientation.

---

## The strategy

Two books run on Nifty simultaneously. They share signals but are managed
independently, and the sharing runs one way only: Book A's stop-outs feed Book
B's triggers, never the reverse.

### Book A — the rolling options ladder

Earns from time decay, close to direction-neutral. **Eight legs at all times,
six sold and two bought.**

Six sold: a call and a put on each of three consecutive weekly expiries.

| Rung | Target premium | Days to expiry |
| --- | --- | --- |
| A | ~150 | ~15 |
| B | ~200 | ~22 |
| C | ~250 | ~29 |

Two bought: a call and a put on the **nearest expiry only**, at ~100 premium.
These are the hedges.

**Strikes are chosen by premium, not by distance from spot.** Because of skew,
a put matching a given premium sits further from spot than a call at the same
premium, so the position is structurally wider below the market than above it.

**The three premium targets are one rule at three ages, not three rules.** A
leg sold at 250 with 29 days to run is worth roughly 200 a week later and 150
the week after. Every expiry walks the same staircase:

```
sold as D  (~29 DTE, ~250)
   ->  C   (~22 DTE, ~200)
   ->  B   (~15 DTE, ~150)   <- hedges bought here
   ->  A   (  8 DTE      )   <- everything closed
```

**The weekly roll**, triggered at 8 days to A's expiry, is three actions taken
together: close all four A legs; sell a call and a put on D (the expiry after
C) at ~250; buy a call and a put on B at ~100. Then B becomes A, C becomes B, D
becomes C, and the book is back to six sells and two buys. Because surviving
legs are relabelled rather than re-struck, a roll only ever opens two sells and
two hedges however long the book has run.

**Stops** are 70 premium points on each sold leg, triggered on the option's own
price rather than the index: sold at 150 exits at 220, sold at 200 at 270, sold
at 250 at 320. The hedges carry no stop and are held unconditionally from
purchase at ~15 DTE to the roll at 8 DTE. Nothing is held to expiry.

Only the nearest expiry is hedged, so four of the six sold legs are unhedged at
any time — protection is aimed at the expiry with the worst gamma. The book is
positive theta, negative vega, negative gamma, near-zero delta.

### Book B — the futures trend overlay

Sits flat by default and takes a two-lot directional position when trend and
stress signals agree.

Near and far month are fixed by **calendar date**, not by which contract is
nearest expiry: on the 1st–14th they are the current and next month; from the
15th, the next month and the one after.

| State | Position | Net exposure |
| --- | --- | --- |
| BASE | +1 near, −1 far | flat — a calendar spread |
| LONG | +2 near | +2 lots |
| SHORT | −2 far | −2 lots |

```
Bullish = (spot > 20EMA OR 10EMA crosses above 20EMA)
          AND (3 higher highs OR a sold call hit its stop)
Bearish = (spot < 20EMA OR 10EMA crosses below 20EMA)
          AND (3 lower lows  OR a sold put  hit its stop)
```

BASE goes to LONG on bullish and to SHORT on bearish; LONG returns to BASE on
bearish and SHORT returns to BASE on bullish. **There is no direct LONG↔SHORT
transition** — every reversal parks at BASE and needs a second signal to flip
through, so each transition moves exactly two lots.

Contracts roll on the 15th of each month, or the next trading day. The roll is
**state-preserving**: it changes contracts, never exposure, and is never itself
a signal. If a trigger fires the same day, the roll executes first and the
transition applies to the new contracts.

---

## What was built

The engine carries **one portfolio across the entire backtest**. A trading day
is a set of events applied to it, not a self-contained run. That is forced by
the strategy: sold legs live about three weeks, hedges about one, and Book B's
state persists across months.

Order of events within a day:

1. Stops resolved from the open to the decision time — they feed Book B's
   signals, so they must be known first.
2. Book A initiates or rolls.
3. Book B rolls contracts, then applies at most one state transition.
4. Stops resolved from the decision time to the close.
5. The completed daily bar updates the trend series; the book is marked.

Signals never read a price that had not printed at the decision time. EMAs come
from completed prior sessions; the current session contributes only its high,
low and last up to 15:15.

Every run ends with **invariant checks** asserting the spec's structural
promises against the trade log — that the book never exceeds six sold and two
bought legs, that each Book B transition moves exactly two lots, that stops
never fill better than their trigger, that nothing is held past expiry, and
that the trade log reconciles with the equity curve.

### Decisions taken where the spec is silent

The spec has an explicit **Unresolved** section. These are the calls made:

| Question | Choice |
| --- | --- |
| Post-stop re-entry | The slot stays empty until the roll, which refills it. The spec's "the book returns to six sells and two buys" makes the roll the refill point. |
| Stop resolution | 1-minute bars on each leg's own price. |
| Fill timing | 15:15 for rolls and transitions. |
| Expiry filter at initiation | Inclusive of 15 DTE — see the warning below. |
| Signal price source | Spot, per the signal definition. |
| Expiry dates | Read from `contract_index.parquet`, never computed, so a shifted expiry needs no special case. |

### One trap worth knowing about

The exchange lists only about **five weekly expiries ahead**, then jumps to
monthlies. Reading "nearest expiry more than 15 days out" *strictly* skips an
expiry exactly 15 days out, starts the ladder one rung further out, and the
third rung then lands past the last listed weekly onto a monthly.

That cannot be walked back. Each roll sells "the expiry after C", and once C is
a monthly the only thing listed after it is the next monthly, then the next
quarterly. In testing, legs ended up sold at a **median 105 days to expiry
instead of 29**, hedges were held 43 days instead of 7, and a 100-premium hedge
does not exist on a two-year option so hedging stopped entirely.

Reading the filter inclusively fixes it: the ladder is born on the weekly cycle
and each new rung at ~29 DTE stays inside the listed window. `dte_at_entry` in
`trades.csv` is the column to watch — it should sit at 15 / 22 / 29.

---

## Results

Window **2025-01-01 to 2026-04-28**, 326 trading days, lot size 65.

| | Points |
| --- | --- |
| Book A (ladder), closed legs | +2,404 |
| Book B (overlay), closed legs | −4,013 |
| Open legs marked to market at the end | +127 |
| **Net** | **−1,482**  (₹−96,359) |

Max drawdown −7,117 points (₹−462,618) on 2026-02-26. Sharpe −0.18.

Points are per lot and independent of contract size; rupees are points × 65.

**The overlay is where the money goes.** Disabling Book B — set
`book_b.enabled: false` in the config — gives **+2,870 points (₹186,573),
Sharpe +1.17, max drawdown −1,428 points**. The overlay converts a profitable,
well-behaved book into a losing one with five times the drawdown. It sits
directional 85% of the time (44% LONG, 41% SHORT, 15% BASE) and is whipsawed.
Holding BASE throughout, never transitioning, was measured at −711 points over
the window, so BASE is mildly negative but second-order; the LONG/SHORT
transitions are the problem.

**Inside Book A**, the economics are the spec working as designed:

- 100 sold legs reached the roll: **+17,232 points**, mean +172 — full premium
  captured as they decay
- 158 sold legs stopped out: **−13,573 points**, mean −86
- Hedges: −1,255 over 138 legs, but mean −6 with occasional payoffs above +500
  — the tail is truncated, not removed

**The 70-point stop is tight.** It fires on 61% of sold legs. A leg sold at 150
stops on roughly a 0.7% index move, which Nifty makes often, and because the
three rungs on one side share direction they stop together. The book holds all
six sold legs on only 137 of 326 days, and just three legs on 90 days.

### Read these as hypotheses, not verdicts

Sixteen months and ~40 transitions is a thin sample for a trend system. The
archive stops 2026-04-28. And Book A's very tight stop feeds Book B's
confirmation term, so the two books may be coupled harder than intended —
worth testing the overlay with the stop-hit term removed before concluding the
trend logic itself is at fault.

---

## Setup

Python 3.12+.

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## The data

**Everything runs on a local parquet archive** — about 6.4 GB covering
2024-01-01 to 2026-04-28. `ladder_config.yaml` sets `data_root` (default
`../data`). Nothing is fetched at runtime.

```
{data_root}/contract_index.parquet
{data_root}/YYYY/MM/DD/INDIA/NSE/INDICES/1minute/NIFTY 50.parquet
{data_root}/YYYY/MM/DD/INDIA/NSE/FUTURES/NIFTY/1minute/NIFTY{DDMMMYY}FUT.parquet
{data_root}/YYYY/MM/DD/INDIA/NSE/OPTIONS/NIFTY/1minute/NIFTY{DDMMMYY}{strike}{CE|PE}.parquet
```

`contract_index.parquet` is a flat index of every trade date × expiry × strike
× option type, and is where expiry dates come from.

### Archive quirks the engine works around

- **Never price strikes off the opening bar.** An untraded contract carries its
  last price forward, so at 09:15 the premium curve contains impossible
  orderings — one strike printed 93.80 while a strictly less valuable one
  printed 219.15. At 15:15 the curve is clean.
- **Far expiries print sporadically** — 8 to 40 bars a session against the near
  expiry's ~375 — while quoting a coherent curve. Bar count is therefore not
  used as a liquidity proxy; recency of the last print and volume relative to
  the chain are.
- **Twelve days in the window have a contract-index entry but no readable index
  bars** and are skipped. Stops are not checked on a skipped day.
- **2025-10-21 is the Diwali Muhurat session**, 13:45–14:44 only. There are no
  15:15 bars, so no leg is fillable that day.
- **Open interest is missing** from many 2024 files; some carry only a
  `Timestamp` column. Absent OI stays absent rather than becoming zero. On
  2025+ data OI is fully populated.

## Run it

```bash
python audit_data.py       # is the data good enough to trade? (~1 min)
python run_backtest.py     # the backtest (~2 min)
```

Override the window without editing the config:

```bash
python run_backtest.py --start 2025/06/01 --end 2025/12/31 --name h2_2025
python run_backtest.py --quiet          # no progress bar
python run_backtest.py other.yaml       # a different config
```

`--name` gives each run its own folder; without it a second run overwrites the
first.

## What a run produces

Everything lands in `reports/<name>/`.

| File | Contents |
| --- | --- |
| `trades.csv` | One row per leg: entry/exit price and time, `exit_reason`, `points`, `rupees`, `days_held`, `dte_at_entry` |
| `equity.csv` | One row per day: running P&L, Book B state, open legs, exposure |
| `events.csv` | Every decision — initiate, roll, stop, refill, contract roll, transition — with its reasoning |
| `warnings.csv` | Every data problem: skipped days, unfillable legs, missing quotes |
| `monthly.csv` | Month-by-month P&L and peak exposure |
| `summary.json` | Headline numbers, machine-readable |

Two habits worth keeping:

- **Read `warnings.csv` before the P&L.** A healthy run over the window has
  about 16 warnings, mostly the 12 unreadable days. A run full of "no fillable"
  lines means something upstream is wrong and the P&L is not meaningful.
- **Check the last line of terminal output.** If it says anything other than
  `Invariants: all checks passed`, do not trust that run.

## Known limits

- Fills are bar closes. `execution.slippage_points` charges a spread against
  the trade on both entry and exit; it defaults to **zero**, so the headline
  results are before transaction costs.
- `naked_short_lots` in `equity.csv` is an exposure proxy, **not SPAN margin** —
  real margin needs the exchange's risk arrays. The spec asks for peak margin
  at LONG and SHORT; this is the closest the data supports.
- Option stops resolve against 1-minute bars, not ticks. Per-contract tick data
  does not exist in this archive.
- `lot_size_schedule` affects rupee P&L only; points P&L is independent of it.

## Optional: downloading more data

`s3_syncer.py` can pull missing days if you have an S3 archive:

```bash
python main.py --sync      # download missing days, then backtest
```

Needs `s3_bucket` set in `ladder_config.yaml` (empty by default — the syncer
says so rather than failing silently) and AWS credentials via `aws configure`.
Everything except `--sync` works without AWS.

## Layout

```
ladder/              the engine
  config.py          config schema
  market.py          parquet access, expiry resolution
  selection.py       premium-based strike selection, liquidity gate
  positions.py       signed legs and the portfolio carrying them
  signals.py         EMAs, higher-high / lower-low streaks
  book_a.py          the rolling options ladder
  book_b.py          the futures trend overlay
  engine.py          day loop and event ordering
  reporting.py       trade log, equity curve, summary
  validate.py        invariant checks
ladder_config.yaml   all parameters
run_backtest.py      run a backtest
audit_data.py        data health check
main.py              entry point, adds --sync
s3_syncer.py         S3 download (optional)
```
