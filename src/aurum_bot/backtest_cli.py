from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import replace
from datetime import datetime, time, timezone
from pathlib import Path

from .config import load_config
from .backtest_runner import run_backtest
from .history_collector import collect_history
from .history_signals import MOSCOW_TZ, load_signals
from .instance_lock import InstanceLock


LOGGER = logging.getLogger("aurum_bot.backtest")


def _start_utc(value: str) -> datetime:
    local_date = datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.combine(local_date, time.min, tzinfo=MOSCOW_TZ).astimezone(
        timezone.utc
    )


def _end_utc(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--end must be ISO date/datetime") from exc
    if parsed.tzinfo is None:
        if len(value) == 10:
            parsed = datetime.combine(parsed.date(), time.max, tzinfo=MOSCOW_TZ)
        else:
            parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    return parsed.astimezone(timezone.utc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only Aurum history collector and FxPro backtester"
    )
    parser.add_argument("command", choices=("collect", "backtest", "all"))
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--start", default="2026-07-19")
    parser.add_argument("--end")
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--results-dir", default="backtests")
    parser.add_argument("--account", default="fxpro_demo")
    parser.add_argument(
        "--terminal-path",
        help=(
            "Override the MT5 terminal executable for read-only history access. "
            "Useful for a dedicated secondary terminal while the live bot is running."
        ),
    )
    parser.add_argument(
        "--session-file",
        help="Optional separate read-only collector Telethon session file",
    )
    parser.add_argument(
        "--lock-file",
        help="Optional separate lock file when collection runs beside the live bot",
    )
    parser.add_argument(
        "--mode",
        choices=("independent", "live_portfolio"),
        default="independent",
        help=(
            "independent tests every call separately; live_portfolio applies "
            "the live hedging bot's process-every-call rules"
        ),
    )
    parser.add_argument(
        "--mt5-time-offset-hours",
        type=float,
        default=3,
        help=(
            "Broker-server offset encoded in MT5 historical tick timestamps. "
            "FxPro history observed in this terminal uses UTC+3."
        ),
    )
    parser.add_argument(
        "--risk-base-usd",
        type=float,
        help=(
            "Override the fixed virtual USD deposit used for lot sizing. "
            "This does not change config.yaml or the MT5 account."
        ),
    )
    parser.add_argument(
        "--image-filter",
        choices=("all", "with", "without"),
        default="all",
        help="Backtest all calls or only calls with/without an attached image",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)
    if args.session_file:
        config = replace(
            config,
            telegram=replace(
                config.telegram,
                session_file=Path(args.session_file).resolve(),
            ),
        )
    lock_file = (
        Path(args.lock_file).resolve()
        if args.lock_file
        else config.paths.lock_file
    )
    start = _start_utc(args.start)
    end = _end_utc(args.end)
    output_dir = Path(args.output_dir).resolve()
    json_path = config.paths.calls_file
    csv_path = json_path.with_suffix(".csv")
    legacy_json_path = output_dir / f"aurum_signals_from_{args.start}.json"
    legacy_csv_path = output_dir / f"aurum_signals_from_{args.start}.csv"
    if not json_path.exists() and legacy_json_path.exists():
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_bytes(legacy_json_path.read_bytes())
        if legacy_csv_path.exists():
            csv_path.write_bytes(legacy_csv_path.read_bytes())

    with InstanceLock(lock_file):
        if args.command in {"collect", "all"}:
            result = asyncio.run(
                collect_history(
                    config=config,
                    start_utc=start,
                    end_utc=end,
                    json_path=json_path,
                    csv_path=csv_path,
                )
            )
            LOGGER.info(
                "Collected %s signals from %s scanned messages; rejected entry-like=%s",
                len(result.signals),
                result.scanned_messages,
                result.rejected_entry_like_messages,
            )
            LOGGER.info("Saved JSON: %s", json_path)
            LOGGER.info("Saved CSV: %s", csv_path)

        if args.command in {"backtest", "all"}:
            accounts = {account.name: account for account in config.accounts}
            if args.account not in accounts:
                raise ValueError(
                    f"Unknown account {args.account!r}; available: {sorted(accounts)}"
                )
            account = accounts[args.account]
            if args.terminal_path:
                account = replace(
                    account,
                    terminal_path=Path(args.terminal_path).resolve(),
                )
            if args.risk_base_usd is not None:
                if args.risk_base_usd <= 0:
                    raise ValueError("--risk-base-usd must be positive")
                account = replace(
                    account,
                    name=f"{account.name}_virtual_{args.risk_base_usd:g}",
                    risk_base_usd=args.risk_base_usd,
                )
            signals = load_signals(json_path)
            if args.image_filter == "with":
                signals = [signal for signal in signals if signal.has_image is True]
                indicator_label = "indicator-1"
            elif args.image_filter == "without":
                signals = [signal for signal in signals if signal.has_image is False]
                indicator_label = "indicator-2"
            else:
                indicator_label = "all-indicators"
            if not signals:
                raise ValueError(
                    f"No signals matched --image-filter={args.image_filter}"
                )
            end_label = end.astimezone(MOSCOW_TZ).strftime("%Y%m%d_%H%M%S")
            risk_label = f"{account.risk_base_usd:g}".replace(".", "_")
            offset_label = f"{args.mt5_time_offset_hours:g}".replace(".", "_")
            results_dir = (
                Path(args.results_dir).resolve()
                / (
                    f"aurum_{args.start.replace('-', '')}_{end_label}_"
                    f"{args.account}_usd{risk_label}_{args.mode}_"
                    f"mt5plus{offset_label}_{indicator_label}"
                )
            )
            _, summary, _ = run_backtest(
                signals=signals,
                account=account,
                trading=config.trading,
                end_utc=end,
                output_dir=results_dir,
                mode=args.mode,
                mt5_time_offset_hours=args.mt5_time_offset_hours,
            )
            for strategy, values in summary.items():
                LOGGER.info(
                    "%s: trades=%s pnl=$%.2f return=%.4f%%",
                    strategy,
                    values["closed_trades"],
                    values["total_pnl_usd"],
                    values["return_on_fixed_base_percent"],
                )
            LOGGER.info("Backtest report: %s", results_dir / "summary.md")


if __name__ == "__main__":
    main()
