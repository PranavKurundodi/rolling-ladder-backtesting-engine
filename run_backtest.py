"""Entry point for the Nifty rolling-ladder backtest.

    python run_backtest.py [config.yaml] [--start YYYY/MM/DD] [--end YYYY/MM/DD]
"""

import argparse
import sys

from logzero import logger

from ladder.config import _as_date, load_config
from ladder.engine import BacktestEngine
from ladder.market import MarketData
from ladder.reporting import print_summary, write_reports
from ladder.validate import check, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", nargs="?", default="ladder_config.yaml")
    parser.add_argument("--start", help="override start date (YYYY/MM/DD)")
    parser.add_argument("--end", help="override end date (YYYY/MM/DD)")
    parser.add_argument("--name", help="override the report folder name")
    parser.add_argument("--quiet", action="store_true", help="no progress bar")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.start:
        config.start_date = _as_date(args.start)
    if args.end:
        config.end_date = _as_date(args.end)
    if args.name:
        config.name = args.name

    logger.info(f"{config.name}: {config.start_date} -> {config.end_date}")
    market = MarketData(config.data_root, config.market_segment)
    engine = BacktestEngine(config, market).run(progress=not args.quiet)

    results = engine.results()
    summary, monthly = write_reports(results, config)
    print_summary(summary, monthly, results)
    report(check(results, config))
    logger.info(f"reports written to {config.report_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
