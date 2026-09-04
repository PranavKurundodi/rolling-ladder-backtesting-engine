"""Drona template entry point.

Runs the Nifty Rolling Options Ladder with Futures Trend Overlay backtest.
Pass --sync to pull missing days from S3 first.

The engine is the `ladder` package; see AGENTS.md.  It carries one portfolio
across the whole backtest rather than running each day in isolation, because
sold legs live about three weeks and Book B's state persists across months.
"""

import sys

from logzero import DEBUG, logger, loglevel

from ladder.config import load_config
from run_backtest import main as run_backtest

loglevel(DEBUG)

CONFIG_PATH = "ladder_config.yaml"


def ensure_data_download(config):
    """Pull any missing days from S3 before the backtest reads them."""
    from s3_syncer import S3DataSyncer
    logger.info("Checking whether market data needs downloading...")
    S3DataSyncer(CONFIG_PATH).sync_all()
    logger.info("Data download complete")


def main():
    config = load_config(CONFIG_PATH)
    logger.info(f"Strategy: {config.name}")
    logger.info(f"Date range: {config.start_date} to {config.end_date}")
    logger.info(f"Data root: {config.data_root}")
    if "--sync" in sys.argv:
        ensure_data_download(config)
    return run_backtest([CONFIG_PATH])


if __name__ == "__main__":
    sys.exit(main())
