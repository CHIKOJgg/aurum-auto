from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    AccountConfig,
    Direction,
    ExecutionKind,
    ExecutionResult,
    Signal,
)
from .mt5_commission import (
    DEFAULT_COMMISSION_PER_LOT_USD,
    infer_round_turn_commission_per_lot,
)
from .exit_strategies import effective_target, executable_legs, get_strategy
from .trading_math import choose_execution, raw_volume_for_risk, volume_for_risk


def _finish(result: ExecutionResult) -> None:
    print(json.dumps(result.to_dict(), ensure_ascii=False))


def _normalized(value: float, digits: int) -> float:
    return round(value, digits)


def _save_strategy_plan(directory: str | None, account: str, plan: dict[str, Any]) -> None:
    if not directory:
        return
    target_dir = Path(directory) / account
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{plan['message_id']}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _success_codes(mt5: Any) -> set[int]:
    return {
        mt5.TRADE_RETCODE_DONE,
        mt5.TRADE_RETCODE_PLACED,
        mt5.TRADE_RETCODE_DONE_PARTIAL,
    }


def _is_hedging_account(mt5: Any, account_info: Any) -> bool:
    hedging_mode = int(
        getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)
    )
    return int(account_info.margin_mode) == hedging_mode


def _prepare_for_new_signal(
    mt5: Any,
    symbol: str,
    magic: int,
    *,
    replace_existing: bool = False,
    symbol_info: Any | None = None,
    deviation_points: int = 20,
) -> tuple[str, str]:
    if not replace_existing:
        return (
            "ready",
            "hedging mode: existing positions and pending orders are preserved",
        )
    if symbol_info is None:
        return "failed", "netting mode: symbol information is unavailable"

    orders = mt5.orders_get(symbol=symbol)
    if orders is None:
        return "failed", f"netting mode: orders_get failed: {mt5.last_error()}"
    removed = 0
    for order in orders:
        result = mt5.order_send(
            {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": int(order.ticket),
                "symbol": symbol,
                "magic": magic,
                "comment": "AURUM:replace",
            }
        )
        if result is None or int(result.retcode) not in _success_codes(mt5):
            detail = mt5.last_error() if result is None else (
                f"retcode={result.retcode}: {result.comment}"
            )
            return (
                "failed",
                f"netting mode: could not remove pending order {order.ticket}: {detail}",
            )
        removed += 1

    remaining_orders = None
    for probe in range(3):
        remaining_orders = mt5.orders_get(symbol=symbol)
        if remaining_orders is None or not remaining_orders:
            break
        if probe < 2:
            time.sleep(0.1)
    if remaining_orders is None:
        return "failed", f"netting mode: order verification failed: {mt5.last_error()}"
    if remaining_orders:
        tickets = ", ".join(str(order.ticket) for order in remaining_orders)
        return "failed", f"netting mode: pending orders remain: {tickets}"

    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return "failed", f"netting mode: positions_get failed: {mt5.last_error()}"
    closed = 0
    for position in positions:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return "failed", f"netting mode: no tick for position close: {mt5.last_error()}"
        is_buy = int(position.type) == int(mt5.POSITION_TYPE_BUY)
        close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
        close_price = float(tick.bid) if is_buy else float(tick.ask)
        base_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "position": int(position.ticket),
            "volume": float(position.volume),
            "type": close_type,
            "price": _normalized(close_price, int(symbol_info.digits)),
            "deviation": int(deviation_points),
            "magic": magic,
            "comment": "AURUM:replace",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        close_detail = "position close was not sent"
        close_ok = False
        for filling in _filling_candidates(mt5, symbol_info, ExecutionKind.MARKET):
            request = dict(base_request)
            request["type_filling"] = filling
            check = mt5.order_check(request)
            if check is None:
                close_detail = f"order_check failed: {mt5.last_error()}"
                continue
            if int(check.retcode) != 0:
                close_detail = f"order_check retcode={check.retcode}: {check.comment}"
                continue
            result = mt5.order_send(request)
            if result is None:
                close_detail = f"order_send failed: {mt5.last_error()}"
                continue
            close_detail = f"retcode={result.retcode}: {result.comment}"
            if int(result.retcode) in _success_codes(mt5):
                close_ok = True
                break
        if not close_ok:
            return (
                "failed",
                f"netting mode: could not close position {position.ticket}: {close_detail}",
            )
        closed += 1

    remaining_positions = None
    for probe in range(3):
        remaining_positions = mt5.positions_get(symbol=symbol)
        if remaining_positions is None or not remaining_positions:
            break
        if probe < 2:
            time.sleep(0.1)
    if remaining_positions is None:
        return "failed", f"netting mode: position verification failed: {mt5.last_error()}"
    if remaining_positions:
        tickets = ", ".join(str(position.ticket) for position in remaining_positions)
        return "failed", f"netting mode: positions remain: {tickets}"
    return (
        "ready",
        f"netting mode: removed {removed} pending order(s), closed {closed} position(s)",
    )


def _pending_order_type(
    mt5: Any,
    direction: Direction,
    entry: float,
    executable_price: float,
) -> int:
    """Choose the valid pending type while keeping the call's entry price."""
    if direction is Direction.LONG:
        return (
            mt5.ORDER_TYPE_BUY_LIMIT
            if entry < executable_price
            else mt5.ORDER_TYPE_BUY_STOP
        )
    return (
        mt5.ORDER_TYPE_SELL_LIMIT
        if entry > executable_price
        else mt5.ORDER_TYPE_SELL_STOP
    )


def _already_applied(
    mt5: Any,
    symbol: str,
    magic: int,
    comment: str,
    execution_kind: ExecutionKind,
) -> int | None:
    if execution_kind is ExecutionKind.MARKET:
        positions = mt5.positions_get(symbol=symbol) or ()
        for position in positions:
            if (
                int(position.magic) == magic
                and str(position.comment).startswith(comment[:20])
            ):
                return int(position.ticket)
        return None

    orders = mt5.orders_get(symbol=symbol) or ()
    for order in orders:
        if int(order.magic) == magic and str(order.comment).startswith(comment[:20]):
            return int(order.ticket)
    return None


def _filling_candidates(
    mt5: Any, symbol_info: Any, execution_kind: ExecutionKind
) -> list[int]:
    # MQL5 requires RETURN for pending orders. For market orders,
    # symbol_info.filling_mode is a bit mask, not an ORDER_FILLING enum.
    if execution_kind is ExecutionKind.LIMIT:
        return [mt5.ORDER_FILLING_RETURN]

    flags = int(symbol_info.filling_mode)
    candidates: list[int] = []
    if flags & 1:  # SYMBOL_FILLING_FOK
        candidates.append(mt5.ORDER_FILLING_FOK)
    if flags & 2:  # SYMBOL_FILLING_IOC
        candidates.append(mt5.ORDER_FILLING_IOC)
    if int(symbol_info.trade_exemode) != mt5.SYMBOL_TRADE_EXECUTION_MARKET:
        candidates.append(mt5.ORDER_FILLING_RETURN)
    if not candidates:
        candidates.extend([mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK])

    unique: list[int] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _send_protected_order(
    mt5: Any,
    base_request: dict[str, Any],
    symbol_info: Any,
    symbol: str,
    magic: int,
    comment: str,
    execution_kind: ExecutionKind,
    attempts: int,
    retry_delay: float,
) -> tuple[bool, int | None, str, dict[str, int] | None]:
    last_detail = "order was not sent"
    for attempt in range(1, attempts + 1):
        existing_ticket = _already_applied(
            mt5, symbol, magic, comment, execution_kind
        )
        if existing_ticket is not None:
            return (
                True,
                existing_ticket,
                "reconciled after an ambiguous response",
                None,
            )

        for filling in _filling_candidates(mt5, symbol_info, execution_kind):
            request = dict(base_request)
            request["type_filling"] = filling
            check = mt5.order_check(request)
            if check is None:
                last_detail = f"order_check failed: {mt5.last_error()}"
                continue
            if int(check.retcode) != 0:
                last_detail = f"order_check retcode={check.retcode}: {check.comment}"
                continue

            send_started_at_ms = time.time_ns() // 1_000_000
            send_started_monotonic_ns = time.monotonic_ns()
            result = mt5.order_send(request)
            send_completed_at_ms = time.time_ns() // 1_000_000
            send_completed_monotonic_ns = time.monotonic_ns()
            if result is None:
                last_detail = f"order_send failed: {mt5.last_error()}"
                continue
            if int(result.retcode) in _success_codes(mt5):
                ticket = int(result.order or result.deal or 0) or None
                return (
                    True,
                    ticket,
                    f"retcode={result.retcode}: {result.comment}",
                    {
                        "send_started_at_ms": send_started_at_ms,
                        "send_completed_at_ms": send_completed_at_ms,
                        "send_completed_monotonic_ns": send_completed_monotonic_ns,
                        "round_trip_ms": round(
                            (send_completed_monotonic_ns - send_started_monotonic_ns)
                            / 1_000_000
                        ),
                    },
                )
            last_detail = f"retcode={result.retcode}: {result.comment}"

        if attempt < attempts:
            time.sleep(retry_delay)

    existing_ticket = _already_applied(mt5, symbol, magic, comment, execution_kind)
    if existing_ticket is not None:
        return True, existing_ticket, "reconciled after final send attempt", None
    return False, None, last_detail, None


def _execution_timing(
    payload: dict[str, Any], send_timing: dict[str, int] | None
) -> dict[str, int | None]:
    if send_timing is None:
        return {}
    incoming = payload.get("timing") or {}
    published_at_ms = incoming.get("published_at_ms")
    received_at_ms = incoming.get("received_at_ms")
    received_monotonic_ns = incoming.get("received_monotonic_ns")
    completed_at_ms = send_timing["send_completed_at_ms"]
    completed_monotonic_ns = send_timing["send_completed_monotonic_ns"]
    return {
        # Telegram publication timestamps only have one-second resolution, so this
        # is useful as an approximate end-to-end value rather than a precise RTT.
        "publication_to_receive_ms": (
            int(received_at_ms) - int(published_at_ms)
            if received_at_ms is not None and published_at_ms is not None
            else None
        ),
        "publication_to_confirmation_ms": (
            completed_at_ms - int(published_at_ms)
            if published_at_ms is not None
            else None
        ),
        # Both values use the same Windows monotonic clock, including across the
        # short-lived worker process, and therefore are safe from clock changes.
        "receive_to_confirmation_ms": (
            round((completed_monotonic_ns - int(received_monotonic_ns)) / 1_000_000)
            if received_monotonic_ns is not None
            else None
        ),
        "order_send_round_trip_ms": send_timing["round_trip_ms"],
    }


def execute(payload: dict[str, Any]) -> ExecutionResult:
    account = AccountConfig(**payload["account"])
    signal = Signal.from_dict(payload["signal"])
    trading = payload["trading"]
    magic = int(trading["magic_number"])
    strategy = get_strategy(str(trading.get("exit_strategy", "sl_tp2")))

    try:
        import MetaTrader5 as mt5
    except ImportError:
        return ExecutionResult(
            account.name, "failed", "MetaTrader5 Python package is not installed"
        )

    terminal_path = Path(account.terminal_path)
    if not terminal_path.is_file():
        return ExecutionResult(
            account.name,
            "failed",
            f"MT5 terminal not found: {terminal_path}",
        )

    initialized = mt5.initialize(str(terminal_path), timeout=60_000)
    if not initialized:
        return ExecutionResult(
            account.name, "failed", f"mt5.initialize failed: {mt5.last_error()}"
        )

    try:
        account_info = mt5.account_info()
        terminal_info = mt5.terminal_info()
        if account_info is None or terminal_info is None:
            return ExecutionResult(
                account.name, "failed", f"MT5 account unavailable: {mt5.last_error()}"
            )
        if str(account_info.currency).upper() != "USD":
            return ExecutionResult(
                account.name,
                "failed",
                f"account currency is {account_info.currency}, expected USD",
            )
        if not bool(account_info.trade_allowed) or not bool(terminal_info.trade_allowed):
            return ExecutionResult(
                account.name,
                "failed",
                "algorithmic trading is disabled in MT5/account",
            )

        broker_symbol = account.broker_symbol(signal.symbol)
        if not mt5.symbol_select(broker_symbol, True):
            return ExecutionResult(
                account.name,
                "failed",
                f"symbol unavailable: {broker_symbol}; {mt5.last_error()}",
            )
        symbol_info = mt5.symbol_info(broker_symbol)
        tick = mt5.symbol_info_tick(broker_symbol)
        if symbol_info is None or tick is None:
            return ExecutionResult(
                account.name,
                "failed",
                f"no symbol/tick data for {broker_symbol}: {mt5.last_error()}",
            )

        preparation_status, preparation_detail = _prepare_for_new_signal(
            mt5,
            broker_symbol,
            magic,
            replace_existing=not _is_hedging_account(mt5, account_info),
            symbol_info=symbol_info,
            deviation_points=int(trading["deviation_points"]),
        )
        if preparation_status != "ready":
            return ExecutionResult(account.name, preparation_status, preparation_detail)

        tick = mt5.symbol_info_tick(broker_symbol)
        if tick is None:
            return ExecutionResult(
                account.name,
                "failed",
                f"no refreshed tick data: {mt5.last_error()}",
            )

        digits = int(symbol_info.digits)
        point = float(symbol_info.point)
        entry = _normalized(signal.entry, digits)
        stop_loss = _normalized(signal.stop_loss, digits)
        if not signal.take_profits:
            return ExecutionResult(account.name, "failed", "signal has no take-profit levels")
        target_count = len(signal.take_profits)
        initial_target = (
            target_count if target_count < 4 else strategy.target_number
        )
        take_profit = _normalized(signal.take_profits[initial_target - 1], digits)

        order_side = (
            mt5.ORDER_TYPE_BUY
            if signal.direction is Direction.LONG
            else mt5.ORDER_TYPE_SELL
        )
        loss_one_lot = mt5.order_calc_profit(
            order_side, broker_symbol, 1.0, entry, stop_loss
        )
        if loss_one_lot is None or not math.isfinite(float(loss_one_lot)):
            return ExecutionResult(
                account.name,
                "failed",
                f"order_calc_profit failed: {mt5.last_error()}",
            )
        if account.has_configured_commission(signal.symbol):
            commission_one_lot = account.commission_for_one_lot(
                signal.symbol,
                entry,
                float(symbol_info.trade_contract_size),
            )
        else:
            inferred_commission = infer_round_turn_commission_per_lot(
                mt5, broker_symbol
            )
            commission_one_lot = (
                inferred_commission
                if inferred_commission is not None
                else DEFAULT_COMMISSION_PER_LOT_USD
            )
        raw_volume = raw_volume_for_risk(
            risk_base_usd=account.risk_base_usd,
            risk_percent=float(trading["risk_percent"]),
            loss_for_one_lot=abs(float(loss_one_lot)),
            commission_for_one_lot=commission_one_lot,
        )
        if raw_volume is None:
            return ExecutionResult(
                account.name,
                "failed",
                "cannot calculate theoretical lot volume",
            )

        def theoretical_stop_loss_at(price: float) -> float | None:
            loss_at_one_lot = mt5.order_calc_profit(
                order_side,
                broker_symbol,
                1.0,
                price,
                stop_loss,
            )
            if loss_at_one_lot is None or not math.isfinite(float(loss_at_one_lot)):
                return None
            return (
                abs(float(loss_at_one_lot)) + commission_one_lot
            ) * raw_volume

        volume = volume_for_risk(
            risk_base_usd=account.risk_base_usd,
            risk_percent=float(trading["risk_percent"]),
            loss_for_one_lot=abs(float(loss_one_lot)),
            volume_min=float(symbol_info.volume_min),
            volume_max=float(symbol_info.volume_max),
            # Respect a coarser broker step, but never use increments below 0.01.
            volume_step=max(
                float(symbol_info.volume_step),
                float(trading["lot_step"]),
            ),
            commission_for_one_lot=commission_one_lot,
        )
        if volume is None:
            return ExecutionResult(
                account.name,
                "failed",
                "cannot calculate a valid lot volume from symbol settings",
            )

        executable_price = (
            float(tick.ask)
            if signal.direction is Direction.LONG
            else float(tick.bid)
        )
        minimum_distance = max(float(symbol_info.trade_stops_level) * point, point)
        current_loss = theoretical_stop_loss_at(executable_price)
        min_market_loss = (
            account.risk_base_usd
            * float(trading["min_market_risk_percent"])
            / 100
        )
        max_market_loss = (
            account.risk_base_usd
            * float(trading["max_market_risk_percent"])
            / 100
        )
        valid_market_geometry = (
            stop_loss < executable_price < take_profit
            if signal.direction is Direction.LONG
            else take_profit < executable_price < stop_loss
        )
        market_risk_in_range = (
            current_loss is not None
            and math.isfinite(float(current_loss))
            and abs(float(current_loss)) >= min_market_loss - 1e-9
            and abs(float(current_loss)) <= max_market_loss + 1e-9
            and valid_market_geometry
        )
        execution_kind = choose_execution(
            signal.direction,
            entry,
            stop_loss,
            executable_price,
            minimum_distance,
            market_risk_in_range=market_risk_in_range,
            strict_call_entry=bool(trading.get("strict_call_entry", False)),
        )
        if execution_kind is ExecutionKind.MARKET and strategy.target_by_order_kind is not None:
            market_target, _ = strategy.target_by_order_kind
            market_target = min(market_target, target_count)
            market_tp = _normalized(signal.take_profits[market_target - 1], digits)
            market_target_ahead = (
                executable_price < market_tp
                if signal.direction is Direction.LONG
                else executable_price > market_tp
            )
            if not market_target_ahead:
                execution_kind = ExecutionKind.LIMIT
        target_number = effective_target(
            strategy,
            execution_kind=execution_kind.value,
            symbol=signal.symbol,
            published_at_ms=(payload.get("timing") or {}).get("published_at_ms"),
        )
        target_number = min(target_number, target_count)
        take_profit = _normalized(signal.take_profits[target_number - 1], digits)
        exit_legs = [(target_number, volume)] if target_count < 4 else executable_legs(
            strategy, total_volume=volume, volume_min=float(symbol_info.volume_min),
            volume_step=max(float(symbol_info.volume_step), float(trading["lot_step"])), target_number=target_number,
        )
        comment = f"AURUM:{signal.message_id}"
        if execution_kind is ExecutionKind.MARKET:
            current_price = _normalized(executable_price, digits)
            protected_geometry = (
                stop_loss < current_price < take_profit
                if signal.direction is Direction.LONG
                else take_profit < current_price < stop_loss
            )
            if not protected_geometry:
                return ExecutionResult(
                    account.name,
                    "failed",
                    "current price cannot use the exact requested SL and TP",
                    volume=volume,
                    execution_kind=execution_kind.value,
                )
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": broker_symbol,
                "volume": volume,
                "type": order_side,
                "price": current_price,
                "sl": stop_loss,
                "tp": take_profit,
                "deviation": int(trading["deviation_points"]),
                "magic": magic,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
            }
        else:
            pending_type = _pending_order_type(
                mt5,
                signal.direction,
                entry,
                executable_price,
            )
            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": broker_symbol,
                "volume": volume,
                "type": pending_type,
                "price": entry,
                "sl": stop_loss,
                "tp": take_profit,
                "deviation": int(trading["deviation_points"]),
                "magic": magic,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
            }

        close_to_entry = abs(executable_price - entry) < minimum_distance
        strict_call_entry = bool(trading.get("strict_call_entry", False))
        initial_attempts = (
            1
            if (
                execution_kind is ExecutionKind.LIMIT
                and close_to_entry
                and not strict_call_entry
            )
            else int(trading["send_attempts"])
        )
        ok, ticket, send_detail, send_timing = _send_protected_order(
            mt5=mt5,
            base_request=request,
            symbol_info=symbol_info,
            symbol=broker_symbol,
            magic=magic,
            comment=comment,
            execution_kind=execution_kind,
            attempts=initial_attempts,
            retry_delay=float(trading["retry_delay_seconds"]),
        )
        if (
            not ok
            and execution_kind is ExecutionKind.LIMIT
            and not strict_call_entry
        ):
            # A rejected limit may become a protected market order only if the
            # refreshed quote has reduced stop risk to the configured threshold.
            refreshed_tick = mt5.symbol_info_tick(broker_symbol)
            if refreshed_tick is not None:
                refreshed_price = (
                    float(refreshed_tick.ask)
                    if signal.direction is Direction.LONG
                    else float(refreshed_tick.bid)
                )
                refreshed_kind = choose_execution(
                    signal.direction,
                    entry,
                    stop_loss,
                    refreshed_price,
                    minimum_distance,
                    market_risk_in_range=(
                        (
                            refreshed_loss := theoretical_stop_loss_at(refreshed_price)
                        )
                        is not None
                        and math.isfinite(float(refreshed_loss))
                        and abs(float(refreshed_loss)) >= min_market_loss - 1e-9
                        and abs(float(refreshed_loss)) <= max_market_loss + 1e-9
                        and (
                            stop_loss < refreshed_price < take_profit
                            if signal.direction is Direction.LONG
                            else take_profit < refreshed_price < stop_loss
                        )
                    ),
                )
                if refreshed_kind is ExecutionKind.MARKET:
                    target_number = effective_target(
                        strategy,
                        execution_kind=ExecutionKind.MARKET.value,
                        symbol=signal.symbol,
                        published_at_ms=(payload.get("timing") or {}).get("published_at_ms"),
                    )
                    target_number = min(target_number, target_count)
                    take_profit = _normalized(signal.take_profits[target_number - 1], digits)
                    exit_legs = [(target_number, volume)] if target_count < 4 else executable_legs(
                        strategy, total_volume=volume, volume_min=float(symbol_info.volume_min),
                        volume_step=max(float(symbol_info.volume_step), float(trading["lot_step"])), target_number=target_number,
                    )
                    market_price = _normalized(refreshed_price, digits)
                    protected_geometry = (
                        stop_loss < market_price < take_profit
                        if signal.direction is Direction.LONG
                        else take_profit < market_price < stop_loss
                    )
                    if not protected_geometry:
                        return ExecutionResult(
                            account.name,
                            "failed",
                            "pending order rejected and exact SL/TP cannot protect fallback market order",
                            volume=volume,
                            execution_kind=ExecutionKind.MARKET.value,
                        )
                    market_request = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": broker_symbol,
                        "volume": volume,
                        "type": order_side,
                        "price": market_price,
                        "sl": stop_loss,
                        "tp": take_profit,
                        "deviation": int(trading["deviation_points"]),
                        "magic": magic,
                        "comment": comment,
                        "type_time": mt5.ORDER_TIME_GTC,
                    }
                    ok, ticket, market_detail, send_timing = _send_protected_order(
                        mt5=mt5,
                        base_request=market_request,
                        symbol_info=symbol_info,
                        symbol=broker_symbol,
                        magic=magic,
                        comment=comment,
                        execution_kind=ExecutionKind.MARKET,
                        attempts=int(trading["send_attempts"]),
                        retry_delay=float(trading["retry_delay_seconds"]),
                    )
                    execution_kind = ExecutionKind.MARKET
                    send_detail = (
                        f"limit rejected ({send_detail}); market fallback: {market_detail}"
                    )
        if not ok:
            return ExecutionResult(
                account.name,
                "failed",
                (
                    f"{preparation_detail}; protected order rejected: {send_detail}"
                ),
                volume=volume,
                execution_kind=execution_kind.value,
            )
        _save_strategy_plan(
            payload.get("strategy_state_dir"),
            account.name,
            {
                "version": 1,
                "status": "active",
                "message_id": signal.message_id,
                "strategy": strategy.key,
                "symbol": broker_symbol,
                "signal_symbol": signal.symbol,
                "direction": signal.direction.value,
                "magic": magic,
                "comment": comment,
                "order_ticket": ticket,
                "execution_kind": execution_kind.value,
                "call_entry": entry,
                "stop_loss": stop_loss,
                "take_profits": list(signal.take_profits),
                "final_target": target_number,
                "exit_legs": [
                    {"target": target, "volume": leg_volume, "closed": False}
                    for target, leg_volume in exit_legs
                ],
                "initial_volume": volume,
                "active_stop_target": -1,
                "touched_target": 0,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "last_check_msc": None,
                "entry_time_msc": None,
                "entry_price": None,
            },
        )
        return ExecutionResult(
            account.name,
            "executed",
            f"{preparation_detail}; {send_detail}",
            ticket=ticket,
            tickets=[ticket] if ticket is not None else None,
            volume=volume,
            execution_kind=execution_kind.value,
            **_execution_timing(payload, send_timing),
        )
    finally:
        mt5.shutdown()


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
        result = execute(payload)
    except Exception as exc:
        account = "unknown"
        try:
            account = str(payload.get("account", {}).get("name", "unknown"))
        except Exception:
            pass
        result = ExecutionResult(account, "failed", f"{type(exc).__name__}: {exc}")
    _finish(result)


if __name__ == "__main__":
    main()
