"""Backtest output: trade log, equity curve, and a summary that separates books.

The two books share signals but are managed independently, so every figure that
can be split by book is split by book.  A blended number would hide the thing
the spec most wants to know -- whether BASE earns its keep.
"""

import json

import pandas as pd
from tabulate import tabulate

from .positions import FUTURE, OPTION


def _drawdown(series):
    if series.empty:
        return 0.0, None
    running_max = series.cummax()
    underwater = series - running_max
    return float(underwater.min()), underwater.idxmin()


def summarise(results, config):
    trades, equity = results["trades"], results["equity"]
    closed = trades[trades["exit_price"].notna()] if not trades.empty else trades
    summary = {}

    if not equity.empty:
        curve = equity.set_index("day")["total_points"]
        depth, trough = _drawdown(curve)
        summary["final_points"] = float(curve.iloc[-1])
        summary["final_rupees"] = float(equity["total_rupees"].iloc[-1])
        summary["max_drawdown_points"] = depth
        summary["max_drawdown_day"] = trough
        daily = curve.diff().dropna()
        if len(daily) > 1 and daily.std() > 0:
            summary["sharpe_daily_annualised"] = float(
                daily.mean() / daily.std() * (252 ** 0.5)
            )
        summary["days"] = int(len(equity))
        summary["peak_naked_short_lots"] = int(equity["naked_short_lots"].max())
        states = equity["book_b_state"].value_counts(normalize=True)
        summary["book_b_state_share"] = {k: round(float(v), 3) for k, v in states.items()}

    if not closed.empty:
        summary["closed_legs"] = int(len(closed))
        summary["open_legs_at_end"] = int(len(trades) - len(closed))
        summary["stops_hit"] = int((closed["exit_reason"] == "stop").sum())
        summary["rolls"] = int((closed["exit_reason"] == "roll").sum())
        by_book = closed.groupby("book")["points"].agg(["sum", "count", "mean"])
        summary["by_book"] = by_book.round(2).to_dict("index")
        options = closed[closed["kind"] == OPTION]
        if not options.empty:
            sold = options[options["quantity_lots"] < 0]
            bought = options[options["quantity_lots"] > 0]
            summary["sold_legs"] = {
                "count": int(len(sold)),
                "points": round(float(sold["points"].sum()), 2),
                "win_rate": round(float((sold["points"] > 0).mean()), 3),
                "mean_days_held": round(float(sold["days_held"].mean()), 1),
            }
            summary["bought_legs"] = {
                "count": int(len(bought)),
                "points": round(float(bought["points"].sum()), 2),
                "win_rate": round(float((bought["points"] > 0).mean()), 3),
                "mean_days_held": round(float(bought["days_held"].mean()), 1),
            }
        futures = closed[closed["kind"] == FUTURE]
        if not futures.empty:
            summary["futures_legs"] = {
                "count": int(len(futures)),
                "points": round(float(futures["points"].sum()), 2),
            }
    return summary


def monthly_table(equity):
    if equity.empty:
        return pd.DataFrame()
    frame = equity.copy()
    frame["month"] = pd.to_datetime(frame["day"]).dt.to_period("M")
    grouped = frame.groupby("month").agg(
        points=("total_points", "last"),
        naked_short_peak=("naked_short_lots", "max"),
    )
    grouped["points_change"] = grouped["points"].diff().fillna(grouped["points"])
    return grouped


def write_reports(results, config):
    out = config.report_dir
    out.mkdir(parents=True, exist_ok=True)
    for name, frame in results.items():
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            frame.to_csv(out / f"{name}.csv", index=False)
    summary = summarise(results, config)
    monthly = monthly_table(results["equity"])
    if not monthly.empty:
        monthly.to_csv(out / "monthly.csv")
    with open(out / "summary.json", "w") as handle:
        json.dump(summary, handle, indent=2, default=str)
    return summary, monthly


def print_summary(summary, monthly, results):
    if not summary:
        print("No results.")
        return
    headline = [
        ["Points", round(summary.get("final_points", 0), 1)],
        ["Rupees", f"{summary.get('final_rupees', 0):,.0f}"],
        ["Max drawdown (points)", round(summary.get("max_drawdown_points", 0), 1)],
        ["Sharpe (daily, ann.)", round(summary.get("sharpe_daily_annualised", 0), 2)],
        ["Trading days", summary.get("days", 0)],
        ["Closed legs", summary.get("closed_legs", 0)],
        ["Stops hit", summary.get("stops_hit", 0)],
        ["Peak naked short lots", summary.get("peak_naked_short_lots", 0)],
    ]
    print("\n" + tabulate(headline, headers=["", "value"], tablefmt="pretty"))

    if "by_book" in summary:
        rows = [[book, v["count"], round(v["sum"], 1), round(v["mean"], 2)]
                for book, v in summary["by_book"].items()]
        print("\n" + tabulate(rows, headers=["book", "legs", "points", "mean"],
                              tablefmt="pretty"))
    for key in ("sold_legs", "bought_legs", "futures_legs"):
        if key in summary:
            print(f"  {key:<14} {summary[key]}")
    if "book_b_state_share" in summary:
        print(f"  book B state share: {summary['book_b_state_share']}")

    warnings = results.get("warnings")
    if warnings is not None and not warnings.empty:
        print(f"\nData warnings: {len(warnings)}")
        print(warnings["detail"].str.replace(r"[\d/-]{6,}", "…", regex=True)
              .value_counts().head(8).to_string())
