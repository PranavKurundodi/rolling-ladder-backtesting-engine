# Nifty Rolling Options Ladder with Futures Trend Overlay

Backtest engine for `nifty_strategy_spec.pdf` v1.0. Two books run on Nifty
simultaneously and are managed independently; they share signals in one
direction only, Book A's stops feeding Book B's triggers.

## Running it

```
python run_backtest.py                       # full backtest over the configured window
python run_backtest.py --start 2025/06/01 --end 2025/12/31 --name h2
python audit_data.py                         # data health for the configured window
python main.py --sync                        # pull missing days from S3, then backtest
```

Output goes to `reports/<name>/`: `trades.csv` (one row per leg),
`equity.csv` (daily curve and exposure), `events.csv` (every decision),
`warnings.csv` (every data problem), `monthly.csv`, `summary.json`.

## Architecture

The portfolio is created once and carried across the entire backtest. A day is
a set of events applied to it, not a self-contained experiment. This is forced
by the strategy: sold legs live about three weeks, hedges about one, and Book
B's state persists across months.

| Module | Role |
| --- | --- |
| `ladder/config.py` | Config schema. Parameters the spec leaves open are named explicitly. |
| `ladder/market.py` | Parquet access; expiries read from `contract_index.parquet`. |
| `ladder/selection.py` | Premium-based strike selection and the liquidity gate. |
| `ladder/positions.py` | Signed legs and the portfolio carrying them. |
| `ladder/signals.py` | EMAs and higher-high / lower-low streaks. |
| `ladder/book_a.py` | The rolling options ladder. |
| `ladder/book_b.py` | The futures trend overlay. |
| `ladder/engine.py` | Day loop and event ordering. |
| `ladder/reporting.py` | Trade log, equity curve, summary. |
| `ladder/validate.py` | Invariant checks, run after every backtest. |
| `s3_syncer.py` | Optional: downloads missing days from S3. Reads `ladder_config.yaml`; needs `s3_bucket` set. |

## Book A - the rolling ladder

Eight legs at all times. Six sold: CE and PE on each of three consecutive
weekly expiries at target premiums 150 / 200 / 250 (about 15 / 22 / 29 DTE).
Two bought: CE and PE on the nearest expiry at 100.

Strikes are chosen by premium, not by distance from spot. Because of skew a
matched-premium put sits further out than a matched-premium call, so the book
is structurally wider below the market.

The three premium targets are one rule at three ages, not three rules, so the
weekly roll relabels rather than re-strikes. At 8 DTE on A: close all four A
legs, sell CE+PE on D at 250, buy CE+PE hedges on the new front expiry at 100,
then B becomes A, C becomes B, D becomes C. However long the book runs, a roll
opens only two sells and two hedges.

Stops are 70 premium points on each sold leg's own price, checked against that
leg's own 1-minute bars. Hedges carry no stop. A stopped slot stays vacant
until the roll refills it, which is what returns the book to six sells and two
buys.

## Book B - the futures trend overlay

Near and far month are set by calendar date, not by nearest expiry: 1st-14th
gives current and next month, 15th onward gives next and month-after.

| State | Position | Net |
| --- | --- | --- |
| BASE | +1 near, -1 far | flat calendar spread |
| LONG | +2 near | +2 lots |
| SHORT | -2 far | -2 lots |

No direct LONG/SHORT transition; every reversal parks at BASE. Futures are
carried as one-lot legs rather than a netted position, because each spec
transition keeps the leg it does not mention -- "buy back the far-month short;
buy 1 more near-month lot" -- which a netted position cannot express. The
invariant checks enforce that every transition moves exactly two lots.

The contract roll is driven by comparing the pair the calendar rule calls for
against the pair actually held, not by testing the date. A date test misses the
roll when the 15th is a holiday or has no bars, and the book then trades a
stale contract until expiry.

## Order of events within a day

1. Stops resolved from the open to the decision time. They feed Book B's
   signals, so they must be known first.
2. Book A initiates or rolls.
3. Book B rolls contracts, then applies at most one transition -- "the roll
   executes first and the transition then applies to the new contracts".
4. Stops resolved from the decision time to the close.
5. The completed daily bar updates the trend series; the book is marked.

Signals never read a price that had not printed at the decision time. EMAs come
from completed daily bars up to the previous session; the current session
contributes only its high, low and last up to the decision time.

## Decisions taken where the spec is silent

| Question | Setting | Choice |
| --- | --- | --- |
| Post-stop re-entry | `book_a.post_stop_policy` | `leave_empty`; the roll refills the slot. Only this option is implemented — the other two raise `NotImplementedError` rather than quietly doing something else. |
| Stop resolution | — | 1-minute bars on each leg's own price. |
| Fill timing | `execution.decision_time` | 15:15. |
| Expiry filter at initiation | `book_a.initiation_min_dte` | Inclusive of 15 DTE. See below — this one matters far more than it looks. |
| Signal price source | — | Spot, per the signal definition on page 2. |
| Expiry dates | — | Read from `contract_index.parquet`, never computed. |

### Why the initiation filter is inclusive

The exchange lists only about five weekly expiries ahead, then jumps to
monthlies. Reading "more than 15 days out" strictly skips an expiry exactly 15
days out and starts the ladder one rung further out — and the third rung then
falls past the last listed weekly onto a monthly.

That cannot be walked back. Each roll sells "the expiry after C", and once C is
a monthly the only thing listed after it is the next monthly, then the next
quarterly. Legs end up sold at a median of 105 days to expiry instead of 29,
hedges are held 43 days instead of 7, and a 100-premium hedge does not exist on
a two-year option so hedges stop filling entirely.

With the inclusive reading the ladder is born on the weekly cycle, and each new
rung at ~29 DTE is always inside the listed window, so it stays there.

## Data notes

- **Do not price strikes off the opening bar.** An untraded contract carries
  its last price forward, so at 09:15 the premium curve contains impossible
  orderings. At the 15:15 decision time it is clean apart from strikes that
  genuinely never traded.
- **Far expiries print sporadically** — eight to forty bars a session against
  the near expiry's ~375 — while quoting a coherent curve. Bar count is
  therefore not a liquidity proxy; recency of the last print and volume
  relative to the chain are.
- **OI is missing** from many 2024 files and a few carry only a `Timestamp`
  column. Absent OI stays absent rather than becoming zero, and OI is not a
  selection gate by default. On 2025+ data OI is fully populated.
- **Twelve days in 2025-01-01..2026-04-28** have a contract-index entry but no
  readable index bars, and are skipped. Stops are not checked on a skipped day.
- **2025-10-21 is the Diwali Muhurat session**, 13:45-14:44 only. There are no
  15:15 bars, so no leg is fillable that day.
- The archive covers **2024-01-01 to 2026-04-28**.

## Modelling limits

- Fills are bar closes. `execution.slippage_points` charges a spread against
  the trade on both entry and exit; it defaults to zero.
- `naked_short_lots` in `equity.csv` is an exposure proxy, not SPAN margin.
- `lot_size_schedule` drives rupee P&L only; points P&L is unaffected.
