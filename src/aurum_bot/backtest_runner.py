from __future__ import annotations

import csv
import json
import logging
import math
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

import numpy as np

from .backtest_engine import (
    BacktestRecord,
    STRATEGIES,
    simulate_independent_strategy,
)
from .config import TradingConfig
from .history_signals import HistoricalSignal
from .models import AccountConfig
from .mt5_history import MT5History, SymbolMetadata
from .mt5_commission import DEFAULT_COMMISSION_PER_LOT_USD
from .trading_math import raw_volume_for_risk, volume_for_risk


LOGGER = logging.getLogger("aurum_bot.backtest_runner")


def _quantize_exit_legs(
    legs: list[dict[str, Any]],
    total_volume: float,
    volume_min: float,
    volume_step: float,
) -> list[dict[str, Any]]:
    """Convert theoretical exit fractions to broker-executable volumes.

    Planned cumulative closes are rounded down to the volume step. Any amount
    that cannot yet be closed remains in the position, and the final leg closes
    the full executable remainder.
    """
    if not legs or total_volume <= 0 or volume_step <= 0 or volume_min <= 0:
        return []
    total_units = int(round(total_volume / volume_step))
    minimum_units = max(1, int(math.ceil(volume_min / volume_step - 1e-9)))
    used_units = 0
    cumulative_fraction = 0.0
    result: list[dict[str, Any]] = []
    for index, leg in enumerate(legs):
        cumulative_fraction += float(leg["fraction"])
        if index == len(legs) - 1:
            leg_units = total_units - used_units
        else:
            desired_units = int(
                math.floor(cumulative_fraction * total_units + 1e-9)
            )
            leg_units = max(0, desired_units - used_units)
            if 0 < leg_units < minimum_units:
                leg_units = 0
            remainder_units = total_units - used_units - leg_units
            if 0 < remainder_units < minimum_units:
                leg_units = 0
        if leg_units <= 0:
            continue
        executed = dict(leg)
        executed_volume = round(leg_units * volume_step, 10)
        executed["planned_fraction"] = float(leg["fraction"])
        executed["executed_volume"] = executed_volume
        executed["fraction"] = executed_volume / total_volume
        result.append(executed)
        used_units += leg_units
    return result


def _enrich_financials(
    records: Iterable[BacktestRecord],
    signal_by_id: dict[int, HistoricalSignal],
    metadata: SymbolMetadata,
    history: MT5History,
    account: AccountConfig,
    trading: TradingConfig,
    inferred_commission_one_lot: float | None,
) -> None:
    effective_step = max(metadata.volume_step, trading.lot_step)
    for record in records:
        if record.order_kind is None:
            continue
        signal = signal_by_id[record.message_id]
        loss_one_lot = history.calc_profit(
            metadata,
            signal.direction,
            1.0,
            signal.entry,
            signal.stop_loss,
        )
        if loss_one_lot is None or loss_one_lot == 0:
            record.note = "MT5 could not calculate one-lot stop loss"
            continue
        if account.has_configured_commission(signal.symbol):
            commission_one_lot = account.commission_for_one_lot(
                signal.symbol,
                signal.entry,
                metadata.contract_size,
            )
            record.commission_source = "config"
        elif inferred_commission_one_lot is not None:
            commission_one_lot = inferred_commission_one_lot
            record.commission_source = "mt5_history"
        else:
            commission_one_lot = DEFAULT_COMMISSION_PER_LOT_USD
            record.commission_source = "default_7_usd_per_lot"
        record.commission_per_lot_usd = commission_one_lot
        raw_lot = raw_volume_for_risk(
            account.risk_base_usd,
            trading.risk_percent,
            abs(loss_one_lot),
            commission_one_lot,
        )
        rounded_lot = volume_for_risk(
            account.risk_base_usd,
            trading.risk_percent,
            abs(loss_one_lot),
            metadata.volume_min,
            metadata.volume_max,
            effective_step,
            commission_one_lot,
        )
        record.raw_lot = raw_lot
        record.rounded_lot = rounded_lot
        if rounded_lot is not None:
            record.commission_usd = commission_one_lot * rounded_lot
        if (
            raw_lot is None
            or rounded_lot is None
            or record.entry_price is None
        ):
            continue

        actual_risk = history.calc_profit(
            metadata,
            signal.direction,
            rounded_lot,
            record.entry_price,
            signal.stop_loss,
        )
        if actual_risk is not None:
            record.actual_risk_usd = (
                abs(actual_risk) + commission_one_lot * rounded_lot
            )
            record.actual_risk_percent = (
                record.actual_risk_usd / account.risk_base_usd * 100
            )
        if record.exit_price is None:
            continue
        theoretical_legs = record.exit_legs or [
            {"fraction": 1.0, "price": record.exit_price}
        ]
        legs = _quantize_exit_legs(
            theoretical_legs,
            rounded_lot,
            metadata.volume_min,
            effective_step,
        )
        if not legs:
            continue
        record.exit_legs = legs
        record.exit_price = sum(
            float(leg["fraction"]) * float(leg["price"]) for leg in legs
        )
        reasons = [str(leg.get("reason") or record.exit_reason) for leg in legs]
        record.exit_reason = (
            reasons[0] if len(set(reasons)) == 1 else " + ".join(reasons)
        )
        gross_pnl = 0.0
        financials_complete = True
        for leg in legs:
            leg_pnl = history.calc_profit(
                metadata,
                signal.direction,
                float(leg["executed_volume"]),
                record.entry_price,
                float(leg["price"]),
            )
            if leg_pnl is None:
                financials_complete = False
                break
            gross_pnl += leg_pnl
        if not financials_complete:
            continue
        net_pnl = gross_pnl - commission_one_lot * rounded_lot
        record.pnl_usd = net_pnl
        record.pnl_percent = net_pnl / account.risk_base_usd * 100
        if record.actual_risk_usd:
            record.r_multiple = net_pnl / record.actual_risk_usd


def _summary(records: list[BacktestRecord], risk_base: float) -> dict[str, Any]:
    status_counts = Counter(record.status for record in records)
    exit_counts = Counter(
        record.exit_reason for record in records if record.exit_reason is not None
    )
    closed = [
        record
        for record in records
        if record.status == "closed" and record.pnl_usd is not None
    ]
    pnl_values = [float(record.pnl_usd) for record in closed]
    r_values = [
        float(record.r_multiple)
        for record in closed
        if record.r_multiple is not None
    ]
    epsilon = 0.005
    total_pnl = sum(pnl_values)
    wins = sum(value > epsilon for value in pnl_values)
    return {
        "signals": len(records),
        "entered_trades": sum(
            record.entry_price is not None for record in records
        ),
        "closed_trades": len(closed),
        "wins": wins,
        "losses": sum(value < -epsilon for value in pnl_values),
        "breakeven_results": sum(abs(value) <= epsilon for value in pnl_values),
        "win_rate_percent": round(wins / len(closed) * 100, 4) if closed else None,
        "total_r": round(sum(r_values), 4),
        "total_pnl_usd": round(total_pnl, 2),
        "return_on_fixed_base_percent": round(total_pnl / risk_base * 100, 4),
        "average_r": round(mean(r_values), 4) if r_values else None,
        "price_data_source_counts": dict(
            sorted(Counter(record.price_data_source for record in records).items())
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "exit_counts": dict(sorted(exit_counts.items())),
    }


def _write_results(
    records: list[BacktestRecord],
    summary: dict[str, Any],
    manifest: dict[str, Any],
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        records,
        key=lambda item: (item.strategy, item.signal_time_utc, item.message_id),
    )
    rows = [record.to_dict() for record in ordered]
    (output_dir / "backtest_results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (output_dir / "backtest_results.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)

    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# Aurum FxPro tick backtest",
        "",
        f"- Account config: `{manifest['account_name']}`",
        f"- MT5 login: `{manifest['mt5_login']}`",
        f"- Server: `{manifest['mt5_server']}`",
        f"- Fixed risk base: `${manifest['risk_base_usd']:.2f}`",
        f"- Backtest mode: `{manifest['mode']}`",
        f"- MT5 history clock offset: "
        f"`UTC{manifest['mt5_time_offset_hours']:+g}` (normalized to UTC)",
        f"- Range: `{manifest['start_utc']}` — `{manifest['end_utc']}`",
        f"- Signals: `{manifest['signal_count']}`",
        "",
        "| Strategy | Entered | Closed | WR % | Wins | Losses | BE | Total R | PnL USD | Return % | Avg R |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy in STRATEGIES:
        item = summary[strategy.key]
        lines.append(
            f"| {strategy.title} | {item['entered_trades']} | "
            f"{item['closed_trades']} | "
            f"{item['win_rate_percent'] if item['win_rate_percent'] is not None else 'n/a'} | "
            f"{item['wins']} | {item['losses']} | "
            f"{item['breakeven_results']} | {item['total_r']:.4f} | "
            f"{item['total_pnl_usd']:.2f} | "
            f"{item['return_on_fixed_base_percent']:.4f} | "
            f"{item['average_r'] if item['average_r'] is not None else 'n/a'} |"
        )
    lines.extend(
        [
            "",
            "## Simulation assumptions",
            "",
            "- The decision quote is the first available FxPro tick at or after "
            "Telegram timestamp + 400 milliseconds.",
            "- LONG market entry uses Ask; SHORT market entry uses Bid.",
            "- LONG exits are evaluated on Bid; SHORT exits are evaluated on Ask.",
            "- Pending LIMIT/STOP entries fill at the requested call price when the trigger quote crosses it.",
            "- Pending entries remain active if price reaches or crosses SL/TP before entry.",
            "- TP exits use the requested target; stop exits use the first observed quote.",
            "- Baseline strategies keep the call SL; explicitly named variants may move it to breakeven or a reached target.",
            "- A breakeven price exit still includes configured commission and can therefore be a small net loss.",
            "- Timed breakeven checks move the stop only when the position is profitable at the named checkpoint.",
            "- If FxPro no longer provides historical ticks, broker M1 bars are "
            "used with the adverse extreme visited first; these rows are marked "
            "`m1_fallback`.",
            "- Configured commissions are deducted; swaps and execution slippage "
            "beyond observed ticks are not added.",
        ]
    )
    lines.extend(
        [
            "- Every signal is simulated independently under MT5 hedging rules.",
            "- Existing positions and pending orders never block or replace a new call.",
            "- If the +400ms quote is already beyond SL, the analysis waits for "
            "price to return to the call entry before starting that signal.",
        ]
    )
    (output_dir / "summary.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def run_backtest(
    signals: list[HistoricalSignal],
    account: AccountConfig,
    trading: TradingConfig,
    end_utc: datetime,
    output_dir: Path,
    mode: str = "independent",
    mt5_time_offset_hours: float = 3,
) -> tuple[list[BacktestRecord], dict[str, Any], dict[str, Any]]:
    if not signals:
        raise ValueError("No signals to backtest")
    if mode not in {"independent", "live_portfolio"}:
        raise ValueError("mode must be 'independent' or 'live_portfolio'")
    end_utc = end_utc.astimezone(timezone.utc)
    start_utc = min(signal.timestamp_utc for signal in signals) - timedelta(seconds=1)
    signal_by_id = {signal.message_id: signal for signal in signals}
    all_records: list[BacktestRecord] = []
    tick_counts: dict[str, int] = {}
    symbol_details: dict[str, Any] = {}

    with MT5History(account) as history:
        for signal_symbol in sorted({signal.symbol for signal in signals}):
            symbol_signals = [
                signal for signal in signals if signal.symbol == signal_symbol
            ]
            metadata = history.symbol_metadata(signal_symbol)
            inferred_commission_one_lot = None
            if not account.has_configured_commission(signal_symbol):
                inferred_commission_one_lot = history.infer_commission_for_one_lot(
                    metadata.broker_symbol
                )
                LOGGER.info(
                    "Commission for %s inferred from MT5 history: %s per lot",
                    metadata.broker_symbol,
                    inferred_commission_one_lot,
                )
            broker_time_shift = timedelta(hours=mt5_time_offset_hours)
            ticks = history.download_ticks(
                metadata,
                min(signal.timestamp_utc for signal in symbol_signals)
                - timedelta(seconds=1)
                + broker_time_shift,
                end_utc + broker_time_shift,
                output_dir / "ticks",
            )
            if mt5_time_offset_hours:
                # This FxPro terminal exposes historical epoch values in its
                # UTC+3 server clock. Normalize them to UTC before comparing
                # against Telegram's timezone-aware UTC message timestamps.
                ticks = ticks.copy()
                ticks["time_msc"] -= int(mt5_time_offset_hours * 3_600_000)
            exact_tick_count = len(ticks)
            exact_start_msc = int(ticks["time_msc"][0]) if len(ticks) else None
            tick_counts[signal_symbol] = exact_tick_count
            symbol_details[signal_symbol] = asdict(metadata)

            symbol_records: list[BacktestRecord] = []
            direction_groups = [
                [item for item in symbol_signals if item.direction is direction]
                for direction in sorted(
                    {item.direction for item in symbol_signals},
                    key=lambda item: item.value,
                )
            ]
            for grouped_signals in direction_groups:
                simulation_ticks = ticks
                used_fallback = False
                first_required_msc = int(
                    min(item.timestamp_utc for item in grouped_signals).timestamp()
                    * 1000
                )
                if (
                    exact_start_msc is None
                    or exact_start_msc > first_required_msc + 60_000
                ):
                    fallback_end_utc = (
                        datetime.fromtimestamp(exact_start_msc / 1000, timezone.utc)
                        if exact_start_msc is not None
                        else end_utc
                    )
                    fallback = history.download_m1_as_ticks(
                        metadata,
                        min(item.timestamp_utc for item in grouped_signals)
                        - timedelta(minutes=1)
                        + broker_time_shift,
                        fallback_end_utc + broker_time_shift,
                        grouped_signals[0].direction,
                        output_dir / "ticks",
                    )
                    if mt5_time_offset_hours and len(fallback):
                        fallback = fallback.copy()
                        fallback["time_msc"] -= int(
                            mt5_time_offset_hours * 3_600_000
                        )
                    if len(fallback):
                        used_fallback = True
                        simulation_ticks = np.concatenate((fallback, ticks))
                        simulation_ticks.sort(order="time_msc")
                if not len(simulation_ticks):
                    raise RuntimeError(
                        f"No tick or M1 history returned for {metadata.broker_symbol}"
                    )
                for strategy in STRATEGIES:
                    records = simulate_independent_strategy(
                        signals=grouped_signals,
                        ticks=simulation_ticks,
                        strategy=strategy,
                        broker_symbol=metadata.broker_symbol,
                        point=metadata.point,
                        trade_stops_level=metadata.trade_stops_level,
                        max_market_risk_ratio=(
                            trading.max_market_risk_percent / trading.risk_percent
                        ),
                        min_market_risk_ratio=(
                            trading.min_market_risk_percent / trading.risk_percent
                        ),
                        execution_delay_seconds=0.4,
                        strict_call_entry=trading.strict_call_entry,
                        market_entry_tolerance_r=trading.market_entry_tolerance_r,
                    )
                    for record in records:
                        if not used_fallback:
                            continue
                        if record.decision_time_utc is None:
                            continue
                        decision_msc = int(
                            datetime.fromisoformat(record.decision_time_utc).timestamp()
                            * 1000
                        )
                        if exact_start_msc is None or decision_msc < exact_start_msc:
                            record.price_data_source = "m1_fallback"
                            record.note = (
                                "Broker tick history unavailable; conservative "
                                "M1 fallback (adverse extreme first)"
                            )
                    symbol_records.extend(records)
            _enrich_financials(
                symbol_records,
                signal_by_id,
                metadata,
                history,
                account,
                trading,
                inferred_commission_one_lot,
            )
            all_records.extend(symbol_records)

        summary = {
            strategy.key: _summary(
                [record for record in all_records if record.strategy == strategy.key],
                account.risk_base_usd,
            )
            for strategy in STRATEGIES
        }
        manifest = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "account_name": account.name,
            "mt5_login": int(history.account_info.login),
            "mt5_server": str(history.account_info.server),
            "risk_base_usd": account.risk_base_usd,
            "risk_percent": trading.risk_percent,
            "take_profit_target": trading.take_profit_target,
            "exit_strategy": trading.exit_strategy,
            "strict_call_entry": trading.strict_call_entry,
            "enable_indicator_2": trading.enable_indicator_2,
            "min_market_risk_percent": trading.min_market_risk_percent,
            "max_market_risk_percent": trading.max_market_risk_percent,
            "lot_step": trading.lot_step,
            "execution_delay_seconds": 0.4,
            "mode": mode,
            "mt5_time_offset_hours": mt5_time_offset_hours,
            "start_utc": start_utc.isoformat(),
            "end_utc": end_utc.isoformat(),
            "signal_count": len(signals),
            "tick_counts": tick_counts,
            "symbols": symbol_details,
            "commission_policy": (
                "explicit config first; otherwise median full-position commission "
                "and fee per entry lot from completed MT5 history; otherwise "
                "$7 per lot"
            ),
            "strategies": [asdict(strategy) for strategy in STRATEGIES],
        }
    _write_results(all_records, summary, manifest, output_dir)
    LOGGER.info("Backtest artifacts saved to %s", output_dir)
    return all_records, summary, manifest
