"""
S3 Data Sync Script for Drona Template
Downloads market data from S3 bucket based on config.yaml date range
Automatically skips weekends and non-trading days.
"""

import os
import sys
import yaml
import boto3
import pandas as pd
import pandas_market_calendars as mcal
from datetime import datetime, timedelta
from botocore.exceptions import ClientError


class S3DataSyncer:
    def __init__(self, config_path='ladder_config.yaml'):
        self.config = self.load_config(config_path)
        self.s3_client = boto3.client('s3')
        self.bucket_name = self.config.get('s3_bucket', '')
        # The archive does not have to sit beside the code; the backtest reads
        # it from data_root, so the syncer must write to the same place.
        self.base_local_path = str(self.config.get('data_root', 'data')).rstrip('/')

    def load_config(self, config_path):
        """Load configuration from yaml file"""
        with open(config_path, 'r') as file:
            config = yaml.safe_load(file)
        config = config[0] if isinstance(config, list) else config
        if not config.get('s3_bucket'):
            raise ValueError(
                f"No 's3_bucket' set in {config_path}. Downloading needs the "
                "bucket holding the market-data archive; the backtest itself "
                "reads from data_root and does not need this."
            )
        return config

    def is_trading_day(self, date):
        """Check if a date is a trading day (Monday-Friday)"""
        # Indian markets are closed on weekends
        # 0 = Monday, 1 = Tuesday, ..., 4 = Friday, 5 = Saturday, 6 = Sunday
        return date.weekday() < 5  # Monday to Friday

    def parse_date_range(self):
        """Parse start and end dates from config"""
        start_date = datetime.strptime(self.config['start_date'], '%Y/%m/%d')
        end_date = datetime.strptime(self.config['end_date'], '%Y/%m/%d')
        return start_date, end_date

    def get_trading_dates(self, start_date, end_date):
        """Get only trading dates between start and end"""
        trading_dates = []
        current_date = start_date
        
        while current_date <= end_date:
            if self.is_trading_day(current_date):
                trading_dates.append(current_date)
            current_date += timedelta(days=1)
        
        return trading_dates

    def get_market_segment_folder(self, market_segment):
        """Map market segment to folder name"""
        mapping = {
            'nifty': 'NIFTY',
            'bank_nifty': 'BANKNIFTY',
            'sensex': 'SENSEX'
        }
        return mapping.get(market_segment.lower(), 'NIFTY')

    def get_index_filename(self, market_segment):
        """Get index filename based on market segment"""
        mapping = {
            'nifty': 'NIFTY 50.parquet',
            'bank_nifty': 'NIFTY BANK.parquet',
            'sensex': 'SENSEX.parquet'
        }
        return mapping.get(market_segment.lower(), 'NIFTY 50.parquet')

    def download_file_safe(self, s3_key, local_path):
        """Download file with existence and size check"""
        try:
            if os.path.exists(local_path):
                local_size = os.path.getsize(local_path)
                try:
                    response = self.s3_client.head_object(Bucket=self.bucket_name, Key=s3_key)
                    remote_size = response['ContentLength']
                    if local_size == remote_size:
                        print(f"✓ Already exists: {os.path.basename(local_path)}")
                        return True
                    else:
                        print(f"↻ File size mismatch, re-downloading: {os.path.basename(local_path)}")
                except Exception:
                    pass

            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            self.s3_client.download_file(self.bucket_name, s3_key, local_path)
            print(f"✓ Downloaded: {os.path.basename(local_path)}")
            return True
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code')
            if code == '404':
                print(f"✗ File not found in S3: {s3_key}")
            else:
                print(f"✗ Error: {code} - {s3_key}")
            return False
        except Exception as e:
            print(f"✗ Unexpected error: {e}")
            return False

    def sync_indices(self, date, market_segment, intervals):
        """Sync index data for given intervals"""
        year, month, day = date.strftime('%Y'), date.strftime('%m'), date.strftime('%d')
        index_file = self.get_index_filename(market_segment)
        downloaded = 0

        for interval in intervals:
            s3_key = f"{year}/{month}/{day}/INDIA/NSE/INDICES/{interval}/{index_file}"
            local_path = f"{self.base_local_path}/{s3_key}"
            if self.download_file_safe(s3_key, local_path):
                downloaded += 1

        return downloaded

    def day_is_complete(self, date, market_segment, intervals):
        """
        Fast check: a day is considered complete if its options folder exists
        and has more files than the old S3 pagination cap (1000).
        Used in 'fast' sync_mode to skip days that look fully downloaded.
        """
        year, month, day = date.strftime('%Y'), date.strftime('%m'), date.strftime('%d')
        segment_folder = self.get_market_segment_folder(market_segment)
        index_file = self.get_index_filename(market_segment)

        # Index file must exist
        index_present = any(
            os.path.exists(f"{self.base_local_path}/{year}/{month}/{day}/INDIA/NSE/INDICES/{iv}/{index_file}")
            for iv in intervals
        )
        if not index_present:
            return False

        # Options folder must exist and have more than 1000 files (old pagination cap)
        for interval in intervals:
            options_dir = f"{self.base_local_path}/{year}/{month}/{day}/INDIA/NSE/OPTIONS/{segment_folder}/{interval}"
            if os.path.isdir(options_dir) and len(os.listdir(options_dir)) > 500:
                return True

        return False

    def sync_options(self, date, market_segment, intervals):
        """Sync options data by discovering available files"""
        year, month, day = date.strftime('%Y'), date.strftime('%m'), date.strftime('%d')
        segment_folder = self.get_market_segment_folder(market_segment)
        downloaded = 0

        for interval in intervals:
            base_path = f"{year}/{month}/{day}/INDIA/NSE/OPTIONS/{segment_folder}/{interval}/"

            # List all files in the path, handling S3 pagination (max 1000 per call)
            try:
                paginator = self.s3_client.get_paginator('list_objects_v2')
                pages = paginator.paginate(
                    Bucket=self.bucket_name,
                    Prefix=base_path,
                    Delimiter='/',
                )
                for page in pages:
                    for obj in page.get('Contents', []):
                        s3_key = obj['Key']
                        if s3_key.endswith('.parquet'):
                            local_path = f"{self.base_local_path}/{s3_key}"
                            if self.download_file_safe(s3_key, local_path):
                                downloaded += 1

            except ClientError as e:
                print(f"✗ Cannot list options for {base_path}: {e}")

        return downloaded

    def sync_date(self, date):
        """Sync data for a single date"""
        print(f"\n{'='*60}")
        print(f"Syncing: {date.strftime('%Y-%m-%d (%A)')}")
        print(f"{'='*60}")

        market_segment = self.config.get('market_segment', 'nifty')
        base_interval = self.config.get('base_data_interval', 'tick')

        intervals = ['1minute', 'tick'] if base_interval == 'tick' else ['1minute']

        total_downloaded = 0

        print(f"\n📊 Syncing INDEX data...")
        indices_count = self.sync_indices(date, market_segment, intervals)
        total_downloaded += indices_count
        print(f"   Index files: {indices_count}")

        print(f"\n📈 Syncing OPTIONS data...")
        options_count = self.sync_options(date, market_segment, intervals)
        total_downloaded += options_count
        print(f"   Options files: {options_count}")

        print(f"\n✅ Total for {date.strftime('%Y-%m-%d')}: {total_downloaded} files")
        return total_downloaded

    def sync_all(self):
        """Main sync function"""
        start_date, end_date = self.parse_date_range()

        # Use NSE calendar to find the actual last trading day before start_date
        nse = mcal.get_calendar("NSE")
        lookback_start = (start_date - timedelta(days=14)).strftime('%Y-%m-%d')
        lookback_end   = (start_date - timedelta(days=1)).strftime('%Y-%m-%d')
        pre_schedule = nse.schedule(start_date=lookback_start, end_date=lookback_end)
        prev_day = datetime.combine(pre_schedule.index[-1].date(), datetime.min.time())

        # Use NSE calendar for in-range trading days too
        schedule = nse.schedule(
            start_date=start_date.strftime('%Y-%m-%d'),
            end_date=end_date.strftime('%Y-%m-%d'),
        )
        trading_dates = [datetime.combine(d.date(), datetime.min.time()) for d in schedule.index]
        all_dates = [prev_day] + trading_dates

        all_dates_count = (end_date - start_date).days + 1
        weekend_count = all_dates_count - len(trading_dates)

        # sync_mode: "fast" skips days whose options folder already has >1000 files
        #            "full" checks every file against S3 (slower, catches partial downloads)
        sync_mode = self.config.get('sync_mode', 'fast')
        market_segment = self.config.get('market_segment', 'nifty')
        base_interval = self.config.get('base_data_interval', 'tick')
        intervals = ['1minute', 'tick'] if base_interval == 'tick' else ['1minute']

        print(f"\n{'#'*60}")
        print(f"S3 Data Sync Started")
        print(f"{'#'*60}")
        print(f"Date Range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        print(f"Previous day (lookback): {prev_day.strftime('%Y-%m-%d')}")
        print(f"Trading Days: {len(trading_dates)}")
        print(f"Non-trading days skipped: {weekend_count}")
        print(f"Market Segment: {market_segment.upper()}")
        print(f"Base Interval: {base_interval}")
        print(f"Sync Mode: {sync_mode}")
        print(f"{'#'*60}")

        total_downloaded = 0
        successful_dates = 0

        for date in all_dates:
            if sync_mode == 'fast' and self.day_is_complete(date, market_segment, intervals):
                print(f"\n⏭️  Skipping {date.strftime('%Y-%m-%d (%A)')} (already complete)")
                continue
            downloads = self.sync_date(date)
            total_downloaded += downloads
            if downloads > 0:
                successful_dates += 1

        print(f"\n{'#'*60}")
        print(f"Sync Complete!")
        print(f"{'#'*60}")
        print(f"✅ Successfully processed {successful_dates}/{len(trading_dates)} trading days")
        print(f"✅ Total files downloaded: {total_downloaded}")
        print(f"⏭️  Weekends skipped: {weekend_count}")
        print(f"{'#'*60}\n")


def main():
    """Main execution function"""
    try:
        syncer = S3DataSyncer('config.yaml')
        syncer.sync_all()
    except FileNotFoundError:
        print("❌ Error: config.yaml not found!")
        sys.exit(1)
    except KeyError as e:
        print(f"❌ Error: Missing required config key: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Unexpected error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()