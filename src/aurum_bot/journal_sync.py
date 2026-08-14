from __future__ import annotations

import json
import os
import subprocess
import sys

from .models import AccountConfig
from .sheets_journal import TradeSnapshot


def collect_trade_snapshots(
    account: AccountConfig,
    *,
    magic_number: int,
    lookback_days: int,
    timeout: int = 90,
) -> list[TradeSnapshot]:
    payload = {
        "account": account.to_dict(),
        "magic_number": magic_number,
        "lookback_days": lookback_days,
    }
    completed = subprocess.run(
        [sys.executable, "-m", "aurum_bot.mt5_journal_worker"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=os.environ.copy(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"MT5 journal worker exited {completed.returncode}: {detail}"
        )
    try:
        raw = json.loads(completed.stdout)
        return [
            TradeSnapshot.from_dict(item)
            for item in raw.get("snapshots", [])
        ]
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid MT5 journal response: {exc}; {completed.stdout!r}"
        ) from exc
