from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import numpy as np

from .history_signals import HistoricalSignal
from .models import Direction
from .exit_strategies import STRATEGIES, StrategySpec  # noqa: F401


_LEGACY_STRATEGIES = (
    StrategySpec("sl_tp1", "SL / TP1", 1),
    StrategySpec("sl_tp2", "SL / TP2", 2),
    StrategySpec("sl_tp3", "SL / TP3", 3),
    StrategySpec("sl_tp4", "SL / TP4", 4),
    StrategySpec(
        "tp2_50_tp4_50",
        "50% TP2 / 50% TP4",
        4,
        (0.0, 0.5, 0.0, 0.5),
    ),
    StrategySpec(
        "tp2_75_tp4_25",
        "75% TP2 / 25% TP4",
        4,
        (0.0, 0.75, 0.0, 0.25),
    ),
    StrategySpec(
        "tp2_25_tp4_75",
        "25% TP2 / 75% TP4",
        4,
        (0.0, 0.25, 0.0, 0.75),
    ),
    StrategySpec(
        "tp1_tp2_tp3_tp4_equal",
        "25% at TP1 / TP2 / TP3 / TP4",
        4,
        (0.25, 0.25, 0.25, 0.25),
    ),
    StrategySpec(
        "tp4_lock_tp1_after_tp2",
        "TP4; after TP2 move SL to TP1",
        4,
        stop_moves=((2, 1),),
    ),
    StrategySpec(
        "tp4_staircase",
        "TP4 staircase: TP2->SL TP1, TP3->SL TP2",
        4,
        stop_moves=((2, 1), (3, 2)),
    ),
    StrategySpec(
        "tp3_lock_tp1_after_tp2",
        "TP3; after TP2 move SL to TP1",
        3,
        stop_moves=((2, 1),),
    ),
    StrategySpec("tp2_80_tp4_20", "80% TP2 / 20% TP4", 4, (0.0, 0.8, 0.0, 0.2)),
    StrategySpec("tp2_60_tp4_40", "60% TP2 / 40% TP4", 4, (0.0, 0.6, 0.0, 0.4)),
    StrategySpec("tp2_40_tp4_60", "40% TP2 / 60% TP4", 4, (0.0, 0.4, 0.0, 0.6)),
    StrategySpec(
        "tp1_25_tp2_50_tp4_25",
        "25% TP1 / 50% TP2 / 25% TP4",
        4,
        (0.25, 0.5, 0.0, 0.25),
    ),
    StrategySpec(
        "tp1_tp2_tp4_thirds",
        "33% TP1 / 33% TP2 / 34% TP4",
        4,
        (0.33, 0.33, 0.0, 0.34),
    ),
    StrategySpec("tp1_50_tp4_50", "50% TP1 / 50% TP4", 4, (0.5, 0.0, 0.0, 0.5)),
    StrategySpec("tp3_50_tp4_50", "50% TP3 / 50% TP4", 4, (0.0, 0.0, 0.5, 0.5)),
    StrategySpec("tp2_50_tp3_50", "50% TP2 / 50% TP3", 3, (0.0, 0.5, 0.5, 0.0)),
    StrategySpec(
        "market_tp2_pending_tp4",
        "MARKET -> TP2; pending -> TP4",
        4,
        target_by_order_kind=(2, 4),
    ),
    StrategySpec(
        "gold_indices_tp4_forex_tp2",
        "GOLD/indices -> TP4; other symbols -> TP2",
        4,
        target_by_asset_class=(4, 2),
    ),
    StrategySpec(
        "active_session_tp4_other_tp2",
        "07:00-20:00 UTC -> TP4; other time -> TP2",
        4,
        session_target_numbers=(4, 2),
    ),
    StrategySpec(
        "tp4_exit_2h_if_no_tp1",
        "TP4; market exit after 2h if TP1 not touched",
        4,
        time_exit_minutes=120,
        time_exit_if_no_tp1=True,
    ),
    StrategySpec(
        "tp4_exit_4h_if_no_tp1",
        "TP4; market exit after 4h if TP1 not touched",
        4,
        time_exit_minutes=240,
        time_exit_if_no_tp1=True,
    ),
    StrategySpec("tp4_be_after_tp1", "TP4; after TP1 move SL to BE", 4, stop_moves=((1, 0),)),
    StrategySpec("tp4_be_after_tp2", "TP4; after TP2 move SL to BE", 4, stop_moves=((2, 0),)),
    StrategySpec("tp3_be_after_tp1", "TP3; after TP1 move SL to BE", 3, stop_moves=((1, 0),)),
    StrategySpec("tp3_be_after_tp2", "TP3; after TP2 move SL to BE", 3, stop_moves=((2, 0),)),
    StrategySpec(
        "tp4_full_staircase",
        "TP4 staircase: TP1->BE, TP2->TP1, TP3->TP2",
        4,
        stop_moves=((1, 0), (2, 1), (3, 2)),
    ),
    StrategySpec(
        "tp4_soft_staircase",
        "TP4 soft staircase: TP2->BE, TP3->TP1",
        4,
        stop_moves=((2, 0), (3, 1)),
    ),
    StrategySpec(
        "tp2_50_tp4_50_be_after_tp2",
        "50% TP2 / 50% TP4; runner SL to BE",
        4,
        (0.0, 0.5, 0.0, 0.5),
        stop_moves=((2, 0),),
    ),
    StrategySpec(
        "tp1_25_tp2_25_tp4_50_stair",
        "25% TP1 / 25% TP2 / 50% TP4; TP1->BE, TP2->SL TP1",
        4,
        (0.25, 0.25, 0.0, 0.5),
        stop_moves=((1, 0), (2, 1)),
    ),
    StrategySpec(
        "tp1_50_tp4_50_be_after_tp1",
        "50% TP1 / 50% TP4; runner SL to BE",
        4,
        (0.5, 0.0, 0.0, 0.5),
        stop_moves=((1, 0),),
    ),
    StrategySpec(
        "tp2_33_tp4_67_lock_tp1",
        "33% TP2 / 67% TP4; runner SL to TP1",
        4,
        (0.0, 0.33, 0.0, 0.67),
        stop_moves=((2, 1),),
    ),
    StrategySpec(
        "dynamic_tp3_tp4_be",
        "TP2 <=30m -> TP4, otherwise TP3; after TP2 SL to BE",
        4,
        stop_moves=((2, 0),),
        dynamic_tp2_minutes=30,
    ),
    StrategySpec("tp4_time_be_15m", "TP4; after 15m in profit move SL to BE", 4, timed_breakeven_minutes=15),
    StrategySpec("tp4_time_be_30m", "TP4; after 30m in profit move SL to BE", 4, timed_breakeven_minutes=30),
    StrategySpec("tp4_time_be_60m", "TP4; after 60m in profit move SL to BE", 4, timed_breakeven_minutes=60),
    StrategySpec("tp4_time_be_120m", "TP4; after 120m in profit move SL to BE", 4, timed_breakeven_minutes=120),
    StrategySpec("tp4_time_be_240m", "TP4; after 240m in profit move SL to BE", 4, timed_breakeven_minutes=240),
)


@dataclass
class BacktestRecord:
    strategy: str
    strategy_title: str
    message_id: int
    signal_time_utc: str
    signal_time_moscow: str
    symbol: str
    broker_symbol: str
    direction: str
    call_entry: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    tp4: float
    status: str = "new"
    order_kind: str | None = None
    decision_time_utc: str | None = None
    entry_delay_seconds: float | None = None
    decision_bid: float | None = None
    decision_ask: float | None = None
    entry_time_utc: str | None = None
    entry_price: float | None = None
    exit_time_utc: str | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    exit_legs: list[dict[str, Any]] = field(default_factory=list)
    tp1_touched: bool = False
    raw_lot: float | None = None
    rounded_lot: float | None = None
    commission_per_lot_usd: float | None = None
    commission_usd: float | None = None
    commission_source: str | None = None
    actual_risk_usd: float | None = None
    actual_risk_percent: float | None = None
    pnl_usd: float | None = None
    pnl_percent: float | None = None
    r_multiple: float | None = None
    price_data_source: str = "ticks"
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Trade:
    signal: HistoricalSignal
    record: BacktestRecord
    pending: bool
    entry_price: float | None = None
    remaining_fraction: float = 1.0
    highest_target_touched: int = 0
    active_stop: float | None = None
    active_stop_reason: str = "stop_loss"
    entry_msc: int | None = None
    timed_breakeven_checked: bool = False
    time_exit_checked: bool = False
    dynamic_final_target: int | None = None


def _iso_from_msc(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    )


def _first_true(condition: np.ndarray, offset: int) -> int | None:
    indices = np.flatnonzero(condition)
    return offset + int(indices[0]) if len(indices) else None


def _earlier(
    first: tuple[int | None, str],
    second: tuple[int | None, str],
    *,
    prefer_second_on_tie: bool = False,
) -> tuple[int | None, str | None]:
    first_index, first_label = first
    second_index, second_label = second
    if first_index is None:
        return second_index, second_label if second_index is not None else None
    if second_index is None:
        return first_index, first_label
    if second_index < first_index or (
        prefer_second_on_tie and second_index == first_index
    ):
        return second_index, second_label
    return first_index, first_label


def _make_record(
    signal: HistoricalSignal,
    strategy: StrategySpec,
    broker_symbol: str,
) -> BacktestRecord:
    return BacktestRecord(
        strategy=strategy.key,
        strategy_title=strategy.title,
        message_id=signal.message_id,
        signal_time_utc=signal.timestamp_utc.isoformat(),
        signal_time_moscow=signal.timestamp_moscow.isoformat(),
        symbol=signal.symbol,
        broker_symbol=broker_symbol,
        direction=signal.direction.value,
        call_entry=signal.entry,
        stop_loss=signal.stop_loss,
        tp1=signal.take_profits[0],
        tp2=signal.take_profits[1],
        tp3=signal.take_profits[2],
        tp4=signal.take_profits[3],
    )


def _effective_target_number(trade: _Trade, strategy: StrategySpec) -> int:
    if trade.dynamic_final_target is not None:
        return trade.dynamic_final_target
    if strategy.target_by_order_kind is not None:
        market_target, pending_target = strategy.target_by_order_kind
        return market_target if trade.record.order_kind == "market" else pending_target
    if strategy.target_by_asset_class is not None:
        metal_index_target, other_target = strategy.target_by_asset_class
        return (
            metal_index_target
            if trade.signal.symbol in {"XAUUSD", "DE40", "US100"}
            else other_target
        )
    if strategy.session_target_numbers is not None:
        active_target, other_target = strategy.session_target_numbers
        hour = trade.signal.timestamp_utc.hour
        return (
            active_target
            if strategy.session_start_hour_utc <= hour < strategy.session_end_hour_utc
            else other_target
        )
    return strategy.target_number


def _effective_fractions(
    trade: _Trade,
    strategy: StrategySpec,
) -> tuple[float, float, float, float]:
    if (
        strategy.target_fractions is not None
        and strategy.target_by_order_kind is None
        and strategy.target_by_asset_class is None
        and strategy.session_target_numbers is None
        and strategy.dynamic_tp2_minutes is None
    ):
        return strategy.target_fractions
    target_number = _effective_target_number(trade, strategy)
    return tuple(
        1.0 if number == target_number else 0.0 for number in range(1, 5)
    )  # type: ignore[return-value]


def _add_exit_leg(
    trade: _Trade,
    ticks: np.ndarray,
    index: int,
    reason: str,
    fraction: float,
    price: float,
) -> None:
    fraction = min(max(float(fraction), 0.0), trade.remaining_fraction)
    if fraction <= 1e-12:
        return
    trade.record.exit_legs.append(
        {
            "fraction": round(fraction, 10),
            "price": float(price),
            "reason": reason,
            "time_utc": _iso_from_msc(int(ticks["time_msc"][index])),
        }
    )
    trade.remaining_fraction = max(0.0, trade.remaining_fraction - fraction)


def _finalize_trade(trade: _Trade, ticks: np.ndarray, index: int) -> None:
    trade.record.status = "closed"
    trade.record.exit_time_utc = _iso_from_msc(int(ticks["time_msc"][index]))
    legs = trade.record.exit_legs
    trade.record.exit_price = sum(
        float(leg["fraction"]) * float(leg["price"]) for leg in legs
    )
    reasons = [str(leg["reason"]) for leg in legs]
    trade.record.exit_reason = reasons[0] if len(set(reasons)) == 1 else " + ".join(reasons)


def _advance_trade(
    trade: _Trade,
    strategy: StrategySpec,
    ticks: np.ndarray,
    start_index: int,
    end_index: int,
) -> bool:
    """Advance through an inclusive tick slice. Return True when still active."""
    if start_index > end_index or len(ticks) == 0:
        return True

    signal = trade.signal
    side = ticks["bid"] if signal.direction is Direction.LONG else ticks["ask"]
    cursor = start_index

    if trade.pending:
        if signal.direction is Direction.LONG:
            fill_trigger = (
                ticks["ask"][cursor : end_index + 1] >= signal.entry
                if trade.record.order_kind == "stop"
                else ticks["ask"][cursor : end_index + 1] <= signal.entry
            )
        else:
            fill_trigger = (
                ticks["bid"][cursor : end_index + 1] <= signal.entry
                if trade.record.order_kind == "stop"
                else ticks["bid"][cursor : end_index + 1] >= signal.entry
            )
        fill_index = _first_true(fill_trigger, cursor)
        if fill_index is None:
            return True
        trade.pending = False
        trade.entry_price = signal.entry
        trade.record.status = "open"
        trade.record.entry_time_utc = _iso_from_msc(
            int(ticks["time_msc"][fill_index])
        )
        trade.record.entry_price = signal.entry
        trade.entry_msc = int(ticks["time_msc"][fill_index])
        cursor = fill_index

    if trade.active_stop is None:
        trade.active_stop = signal.stop_loss
    stop_moves = dict(strategy.stop_moves)

    while cursor <= end_index:
        fractions = _effective_fractions(trade, strategy)
        relevant_targets = {
            number
            for number, fraction in enumerate(fractions, start=1)
            if fraction > 0
        } | set(stop_moves)
        if strategy.dynamic_tp2_minutes is not None and trade.dynamic_final_target is None:
            relevant_targets.add(2)
        remaining_targets = sorted(
            number
            for number in relevant_targets
            if number > trade.highest_target_touched
        )
        next_target = remaining_targets[0] if remaining_targets else None
        prices = side[cursor : end_index + 1]
        if signal.direction is Direction.LONG:
            stop_index = _first_true(prices <= float(trade.active_stop), cursor)
            target_index = (
                _first_true(
                    prices >= signal.take_profits[next_target - 1], cursor
                )
                if next_target is not None
                else None
            )
        else:
            stop_index = _first_true(prices >= float(trade.active_stop), cursor)
            target_index = (
                _first_true(
                    prices <= signal.take_profits[next_target - 1], cursor
                )
                if next_target is not None
                else None
            )
        candidates: list[tuple[int, int, str]] = []
        if stop_index is not None:
            candidates.append((stop_index, 0, "stop"))
        if target_index is not None:
            candidates.append((target_index, 1, "target"))
        if trade.entry_msc is not None:
            if (
                strategy.timed_breakeven_minutes is not None
                and not trade.timed_breakeven_checked
            ):
                due_msc = trade.entry_msc + int(
                    strategy.timed_breakeven_minutes * 60_000
                )
                due_index = max(
                    cursor,
                    int(np.searchsorted(ticks["time_msc"], due_msc, side="left")),
                )
                if due_index <= end_index:
                    candidates.append((due_index, 2, "timed_breakeven"))
            if strategy.time_exit_minutes is not None and not trade.time_exit_checked:
                due_msc = trade.entry_msc + int(strategy.time_exit_minutes * 60_000)
                due_index = max(
                    cursor,
                    int(np.searchsorted(ticks["time_msc"], due_msc, side="left")),
                )
                if due_index <= end_index:
                    candidates.append((due_index, 3, "time_exit"))
        event_index, _, event = min(candidates) if candidates else (None, 0, None)
        if event_index is None:
            return True

        if event == "stop":
            quote = (
                float(ticks["bid"][event_index])
                if signal.direction is Direction.LONG
                else float(ticks["ask"][event_index])
            )
            _add_exit_leg(
                trade,
                ticks,
                event_index,
                trade.active_stop_reason,
                trade.remaining_fraction,
                quote,
            )
            _finalize_trade(trade, ticks, event_index)
            return False

        if event == "timed_breakeven":
            trade.timed_breakeven_checked = True
            quote = float(side[event_index])
            profitable = (
                quote > float(trade.entry_price)
                if signal.direction is Direction.LONG
                else quote < float(trade.entry_price)
            )
            if profitable:
                trade.active_stop = float(trade.entry_price)
                trade.active_stop_reason = "timed_breakeven_stop"
            cursor = event_index
            continue

        if event == "time_exit":
            trade.time_exit_checked = True
            should_exit = not strategy.time_exit_if_no_tp1 or not trade.record.tp1_touched
            if should_exit:
                quote = float(side[event_index])
                _add_exit_leg(
                    trade,
                    ticks,
                    event_index,
                    f"time_exit_{strategy.time_exit_minutes:g}m",
                    trade.remaining_fraction,
                    quote,
                )
                _finalize_trade(trade, ticks, event_index)
                return False
            cursor = event_index
            continue

        quote = float(side[event_index])
        crossed_target = trade.highest_target_touched
        for number, target_price in enumerate(signal.take_profits, start=1):
            crossed = (
                quote >= target_price
                if signal.direction is Direction.LONG
                else quote <= target_price
            )
            if crossed:
                crossed_target = number
        if (
            strategy.dynamic_tp2_minutes is not None
            and trade.dynamic_final_target is None
            and crossed_target >= 2
            and trade.entry_msc is not None
        ):
            elapsed_minutes = (
                int(ticks["time_msc"][event_index]) - trade.entry_msc
            ) / 60_000
            trade.dynamic_final_target = (
                strategy.dynamic_fast_target
                if elapsed_minutes <= strategy.dynamic_tp2_minutes
                else strategy.dynamic_slow_target
            )
            fractions = _effective_fractions(trade, strategy)
        for number in range(trade.highest_target_touched + 1, crossed_target + 1):
            fraction = fractions[number - 1]
            if fraction > 0:
                _add_exit_leg(
                    trade,
                    ticks,
                    event_index,
                    f"tp{number}",
                    fraction,
                    signal.take_profits[number - 1],
                )
            if number in stop_moves:
                stop_target = stop_moves[number]
                if stop_target == 0:
                    trade.active_stop = float(trade.entry_price)
                    trade.active_stop_reason = "breakeven_stop"
                else:
                    trade.active_stop = signal.take_profits[stop_target - 1]
                    trade.active_stop_reason = f"trailing_stop_tp{stop_target}"
        trade.highest_target_touched = crossed_target
        trade.record.tp1_touched = crossed_target >= 1
        if trade.remaining_fraction <= 1e-12:
            _finalize_trade(trade, ticks, event_index)
            return False
        cursor = event_index

    return True


def _entry_kind(
    signal: HistoricalSignal,
    executable_price: float,
    minimum_distance: float,
    min_market_risk_ratio: float,
    max_market_risk_ratio: float,
    strict_call_entry: bool = False,
    market_entry_tolerance_r: float = 0.0,
) -> str:
    if strict_call_entry:
        if signal.direction is Direction.LONG:
            return "limit" if signal.entry < executable_price else "stop"
        return "limit" if signal.entry > executable_price else "stop"

    if signal.direction is Direction.LONG:
        if not signal.stop_loss < executable_price < signal.take_profits[0]:
            return "limit" if signal.entry < executable_price else "stop"
        risk_ratio = abs(executable_price - signal.stop_loss) / abs(
            signal.entry - signal.stop_loss
        )
        if (min_market_risk_ratio <= risk_ratio <= max_market_risk_ratio
                or abs(executable_price - signal.entry) <= market_entry_tolerance_r * abs(signal.entry - signal.stop_loss) + 1e-9):
            return "market"
        return "limit" if signal.entry < executable_price else "stop"

    if not signal.take_profits[0] < executable_price < signal.stop_loss:
        return "limit" if signal.entry > executable_price else "stop"
    risk_ratio = abs(signal.stop_loss - executable_price) / abs(
        signal.stop_loss - signal.entry
    )
    if (min_market_risk_ratio <= risk_ratio <= max_market_risk_ratio
            or abs(executable_price - signal.entry) <= market_entry_tolerance_r * abs(signal.entry - signal.stop_loss) + 1e-9):
        return "market"
    return "limit" if signal.entry > executable_price else "stop"


def simulate_strategy(
    signals: Iterable[HistoricalSignal],
    ticks: np.ndarray,
    strategy: StrategySpec,
    broker_symbol: str,
    point: float,
    trade_stops_level: int,
    max_market_risk_ratio: float = 1.1,
    min_market_risk_ratio: float = 0.9,
    max_entry_delay_seconds: float = 60,
    execution_delay_seconds: float = 0.4,
    strict_call_entry: bool = False,
    market_entry_tolerance_r: float = 0.0,
) -> list[BacktestRecord]:
    ordered = sorted(signals, key=lambda item: (item.timestamp_utc, item.message_id))
    records: list[BacktestRecord] = []
    active: _Trade | None = None
    cursor = 0
    minimum_distance = max(point * trade_stops_level, point)
    tick_times = ticks["time_msc"] if len(ticks) else np.array([], dtype=np.int64)

    for signal in ordered:
        signal_msc = int(signal.timestamp_utc.timestamp() * 1000)
        eligible_msc = signal_msc + int(execution_delay_seconds * 1000)
        decision_index = int(np.searchsorted(tick_times, eligible_msc, side="left"))
        record = _make_record(signal, strategy, broker_symbol)
        records.append(record)
        if decision_index >= len(ticks):
            record.status = "no_tick"
            record.note = (
                f"No FxPro tick at/after signal time + "
                f"{execution_delay_seconds:g}s"
            )
            continue

        if active is not None:
            still_active = _advance_trade(
                active,
                strategy,
                ticks,
                cursor,
                decision_index,
            )
            if not still_active:
                active = None

        decision_msc = int(ticks["time_msc"][decision_index])
        delay_seconds = (decision_msc - signal_msc) / 1000
        record.decision_time_utc = _iso_from_msc(decision_msc)
        record.entry_delay_seconds = delay_seconds
        record.decision_bid = float(ticks["bid"][decision_index])
        record.decision_ask = float(ticks["ask"][decision_index])
        if delay_seconds > max_entry_delay_seconds:
            record.status = "no_fresh_tick"
            record.note = f"First tick was {delay_seconds:.3f}s after signal"
            cursor = decision_index + 1
            continue

        if active is not None and not active.pending:
            record.status = "skipped_existing_position"
            record.note = "New signal ignored because a position is already open"
            cursor = decision_index + 1
            continue
        if active is not None and active.pending:
            active.record.status = "cancelled_by_new_signal"
            active.record.exit_reason = "pending_replaced"
            active.record.exit_time_utc = record.decision_time_utc
            active = None

        executable_price = (
            record.decision_ask
            if signal.direction is Direction.LONG
            else record.decision_bid
        )
        kind = _entry_kind(
            signal,
            float(executable_price),
            minimum_distance,
            min_market_risk_ratio,
            max_market_risk_ratio,
            strict_call_entry,
            market_entry_tolerance_r,
        )
        record.order_kind = kind
        if kind == "market":
            record.status = "open"
            record.entry_time_utc = record.decision_time_utc
            record.entry_price = float(executable_price)
            active = _Trade(
                signal=signal,
                record=record,
                pending=False,
                entry_price=float(executable_price),
                entry_msc=decision_msc,
            )
        else:
            record.status = "pending"
            active = _Trade(signal=signal, record=record, pending=True)
        cursor = decision_index + 1

    if active is not None and len(ticks):
        still_active = _advance_trade(
            active,
            strategy,
            ticks,
            cursor,
            len(ticks) - 1,
        )
        if still_active:
            active.record.status = "pending_end" if active.pending else "open_end"
    return records


def simulate_independent_strategy(
    signals: Iterable[HistoricalSignal],
    ticks: np.ndarray,
    strategy: StrategySpec,
    broker_symbol: str,
    point: float,
    trade_stops_level: int,
    max_market_risk_ratio: float = 1.1,
    min_market_risk_ratio: float = 0.9,
    max_entry_delay_seconds: float = 60,
    execution_delay_seconds: float = 0.4,
    strict_call_entry: bool = False,
    market_entry_tolerance_r: float = 0.0,
) -> list[BacktestRecord]:
    """Backtest every signal independently from entry until its first exit."""
    ordered = sorted(signals, key=lambda item: (item.timestamp_utc, item.message_id))
    records: list[BacktestRecord] = []
    minimum_distance = max(point * trade_stops_level, point)
    tick_times = ticks["time_msc"] if len(ticks) else np.array([], dtype=np.int64)

    for signal in ordered:
        signal_msc = int(signal.timestamp_utc.timestamp() * 1000)
        eligible_msc = signal_msc + int(execution_delay_seconds * 1000)
        decision_index = int(np.searchsorted(tick_times, eligible_msc, side="left"))
        record = _make_record(signal, strategy, broker_symbol)
        records.append(record)
        if decision_index >= len(ticks):
            record.status = "no_tick"
            record.note = (
                f"No FxPro tick at/after signal time + "
                f"{execution_delay_seconds:g}s"
            )
            continue

        decision_msc = int(ticks["time_msc"][decision_index])
        delay_seconds = (decision_msc - signal_msc) / 1000
        record.decision_time_utc = _iso_from_msc(decision_msc)
        record.entry_delay_seconds = delay_seconds
        record.decision_bid = float(ticks["bid"][decision_index])
        record.decision_ask = float(ticks["ask"][decision_index])
        if delay_seconds > max_entry_delay_seconds:
            record.status = "no_fresh_tick"
            record.note = f"First tick was {delay_seconds:.3f}s after signal"
            continue

        executable_price = (
            record.decision_ask
            if signal.direction is Direction.LONG
            else record.decision_bid
        )
        kind = _entry_kind(
            signal,
            float(executable_price),
            minimum_distance,
            min_market_risk_ratio,
            max_market_risk_ratio,
            strict_call_entry,
            market_entry_tolerance_r,
        )

        if kind == "market":
            record.order_kind = kind
            record.status = "open"
            record.entry_time_utc = record.decision_time_utc
            record.entry_price = float(executable_price)
            trade = _Trade(
                signal=signal,
                record=record,
                pending=False,
                entry_price=float(executable_price),
                entry_msc=decision_msc,
            )
            start_index = decision_index
        else:
            record.order_kind = kind
            record.status = "pending"
            trade = _Trade(signal=signal, record=record, pending=True)
            start_index = decision_index

        still_active = _advance_trade(
            trade,
            strategy,
            ticks,
            start_index,
            len(ticks) - 1,
        )
        if still_active:
            record.status = "pending_end" if trade.pending else "open_end"

    return records
