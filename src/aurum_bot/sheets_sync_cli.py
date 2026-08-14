from __future__ import annotations

import argparse

from .config import load_config
from .instance_lock import InstanceLock
from .journal_sync import collect_trade_snapshots
from .sheets_journal import SheetsTradeJournal


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read fxpro MT5 history and synchronize the Google Sheets journal"
    )
    parser.add_argument("--config", default="config.yaml")
    return parser.parse_args()


def main() -> None:
    config = load_config(parse_args().config)
    with InstanceLock(config.paths.lock_file.with_name("sheets_sync.lock")):
        if not config.google_sheets.enabled:
            raise SystemExit("google_sheets.enabled is false")
        account = next(
            item
            for item in config.accounts
            if item.name == config.google_sheets.account
        )
        journal = SheetsTradeJournal(config.google_sheets)
        if config.google_sheets.auto_setup:
            journal.ensure_template()
        else:
            journal.check_access()
        snapshots = collect_trade_snapshots(
            account,
            magic_number=config.trading.magic_number,
            lookback_days=config.google_sheets.history_lookback_days,
        )
        updated = journal.upsert_snapshots(snapshots)
        print(f"Google Sheets synchronized: {updated} trade(s)")


if __name__ == "__main__":
    main()
