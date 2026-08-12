from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .exit_strategies import get_strategy
from .models import AccountConfig, Direction, ExecutionKind
from .mt5_worker import _filling_candidates, _normalized, _success_codes


def _save(path: Path, plan: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _matching(items: tuple[Any, ...] | list[Any], plan: dict[str, Any]) -> list[Any]:
    comment = str(plan["comment"])
    magic = int(plan["magic"])
    order_ticket = int(plan.get("order_ticket", -1) or -1)
    results = []
    for item in items:
        item_ticket = int(getattr(item, "ticket", -1))
        item_magic = int(getattr(item, "magic", -1))
        item_comment = str(getattr(item, "comment", ""))
        if item_ticket == order_ticket:
            results.append(item)
        elif item_magic == magic and (item_comment.startswith(comment[:20]) or comment.startswith(item_comment[:20])):
            results.append(item)
    return results


def _close_position(mt5: Any, position: Any, symbol_info: Any, deviation: int, volume: float | None = None) -> bool:
    is_buy = int(position.type) == int(mt5.POSITION_TYPE_BUY)
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        return False
    requested = min(float(position.volume), float(volume if volume is not None else position.volume))
    step = float(symbol_info.volume_step)
    requested = math.floor(requested / step + 1e-9) * step
    if requested + 1e-9 < float(symbol_info.volume_min):
        return False
    kind = ExecutionKind.MARKET
    base = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": position.symbol,
        "position": int(position.ticket),
        "volume": round(requested, 10),
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "price": _normalized(float(tick.bid if is_buy else tick.ask), int(symbol_info.digits)),
        "deviation": deviation,
        "magic": int(position.magic),
        "comment": "AURUM:manage",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    for filling in _filling_candidates(mt5, symbol_info, kind):
        request = dict(base, type_filling=filling)
        check = mt5.order_check(request)
        if check is None or int(check.retcode) != 0:
            continue
        result = mt5.order_send(request)
        if result is not None and int(result.retcode) in _success_codes(mt5):
            return True
    return False


def _modify_position(mt5: Any, position: Any, *, stop: float, take_profit: float) -> bool:
    result = mt5.order_send({
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": position.symbol,
        "position": int(position.ticket),
        "sl": stop,
        "tp": take_profit,
    })
    if result is not None and int(result.retcode) in _success_codes(mt5):
        return True
    LOGGER.warning(
        "_modify_position failed for ticket %s: retcode=%s comment=%s",
        getattr(position, "ticket", "unknown"),
        getattr(result, "retcode", None),
        getattr(result, "comment", None),
    )
    return False


def _cancel_order(mt5: Any, order: Any) -> bool:
    order_ticket = int(getattr(order, "ticket", 0) or 0)
    order_symbol = str(getattr(order, "symbol", "") or "")
    order_magic = int(getattr(order, "magic", 0) or 0)
    request: dict[str, Any] = {
        "action": mt5.TRADE_ACTION_REMOVE,
        "order": order_ticket,
    }
    if order_symbol:
        request["symbol"] = order_symbol
    if order_magic > 0:
        request["magic"] = order_magic
    result = mt5.order_send(request)
    if result is None:
        LOGGER.warning("_cancel_order order_send returned None for ticket %s: %s", order_ticket, mt5.last_error())
        return False
    accepted = {
        mt5.TRADE_RETCODE_DONE,
        mt5.TRADE_RETCODE_DONE_PARTIAL,
        int(getattr(mt5, "TRADE_RETCODE_ORDER_REMOVED", 4108)),
    }
    ok = int(result.retcode) in accepted
    if not ok:
        LOGGER.warning("_cancel_order failed for ticket %s: retcode=%s comment=%s", order_ticket, result.retcode, result.comment)
    return ok


def _favorable_extreme(mt5: Any, plan: dict[str, Any], now_msc: int, server_offset_hours: float = 3.0, server_time_mode: str = "auto") -> float | None:
    """Return the most favorable price since the last check."""
    # When server_time_mode is "auto", calculate offset from the broker's
    # own server time to handle DST transitions automatically.
    if server_time_mode == "auto":
        tick = mt5.symbol_info_tick(plan["symbol"])
        if tick is not None:
            server_time_sec = int(getattr(tick, "time", 0))
            if server_time_sec > 0:
                utc_now_sec = now_msc / 1000
                server_offset_hours = (server_time_sec - utc_now_sec) / 3600
                # Round to nearest 0.5h to filter jitter.
                server_offset_hours = round(server_offset_hours * 2) / 2
    tick = mt5.symbol_info_tick(plan["symbol"])
    if tick is None:
        return None
    direction = Direction(plan["direction"])
    current = float(tick.bid if direction is Direction.LONG else tick.ask)
    start_msc = plan.get("last_check_msc") or plan.get("entry_time_msc")
    if not start_msc:
        return current
    try:
        # MT5 copy_ticks_range interprets naive server timestamps.  Keep the
        # conversion aligned with the backtest collector, then compare returned
        # prices only (their timestamps are not persisted here).
        offset_seconds = float(server_offset_hours) * 3_600
        ticks = mt5.copy_ticks_range(
            plan["symbol"],
            datetime.fromtimestamp(max(0, int(start_msc) - 1000) / 1000 + offset_seconds, timezone.utc),
            datetime.fromtimestamp(now_msc / 1000 + offset_seconds, timezone.utc),
            mt5.COPY_TICKS_ALL,
        )
        if ticks is None or len(ticks) == 0:
            return current
        values = ticks["bid"] if direction is Direction.LONG else ticks["ask"]
        return float(max(values) if direction is Direction.LONG else min(values))
    except Exception:
        return current


def _manage_plan(
    mt5: Any,
    path: Path,
    plan: dict[str, Any],
    deviation: int,
    pending_timeout_minutes: float = 0.0,
    max_spread_points: int = 0,
    server_offset_hours: float = 3.0,
    close_spread_hard_cap_minutes: float = 10.0,
    server_time_mode: str = "auto",
    pending_timeout_enabled: bool = True,
    exit_spread_guard_enabled: bool = True,
    symbol_max_spread_points: dict[str, int] | None = None,
) -> None:
    if plan.get("status") != "active":
        return
    symbol = str(plan["symbol"])
    raw_positions = mt5.positions_get(symbol=symbol)
    raw_orders = mt5.orders_get(symbol=symbol)
    if raw_positions is None and raw_orders is None:
        return
    positions = _matching(list(raw_positions or ()), plan)
    orders = _matching(list(raw_orders or ()), plan)
    if not positions:
        if orders:
            if pending_timeout_enabled and pending_timeout_minutes > 0:
                now_msc = time.time_ns() // 1_000_000
                created_text = str(plan.get("created_at", ""))
                plan_created_msc = None
                if created_text:
                    try:
                        plan_created_msc = int(datetime.fromisoformat(created_text).timestamp() * 1000)
                    except ValueError:
                        pass
                for order in orders:
                    placed_msc = plan_created_msc
                    if not placed_msc:
                        placed_msc = int(getattr(order, "time_setup_msc", 0) or 0)
                    if not placed_msc:
                        placed_msc = int(getattr(order, "time_setup", 0) or 0) * 1000
                    if placed_msc and now_msc - placed_msc >= pending_timeout_minutes * 60_000:
                        _cancel_order(mt5, order)
                remaining = _matching(list(mt5.orders_get(symbol=symbol) or ()), plan)
                if not remaining:
                    # An order may fill while cancellation is in flight.  Re-read
                    # positions before declaring the plan complete, otherwise a
                    # newly filled position would be left unmanaged.
                    if _matching(list(mt5.positions_get(symbol=symbol) or ()), plan):
                        return _manage_plan(
                            mt5, path, plan, deviation, pending_timeout_minutes,
                            max_spread_points, server_offset_hours,
                            close_spread_hard_cap_minutes, server_time_mode,
                            pending_timeout_enabled, exit_spread_guard_enabled,
                            symbol_max_spread_points,
                        )
                    plan["status"] = "completed"
                    plan["completion_reason"] = "pending_timeout"
                    _save(path, plan)
                    return
            return
        direction = Direction(plan["direction"])
        tick = mt5.symbol_info_tick(symbol)
        stop = float(plan.get("stop_loss", 0.0))
        current = float(tick.bid if direction is Direction.LONG else tick.ask) if tick else None
        if int(plan.get("touched_target", 0)) >= 1:
            reason = "broker_closed_tp"
        elif current is not None and (current <= stop if direction is Direction.LONG else current >= stop):
            reason = "broker_closed_sl"
        else:
            reason = "broker_closed_manual"
        plan["status"] = "completed"
        plan["completion_reason"] = reason
        plan["completed_at"] = datetime.now(timezone.utc).isoformat()
        _save(path, plan)
        return

    position = positions[0]
    if plan.get("entry_time_msc") is None:
        plan["entry_time_msc"] = int(getattr(position, "time_msc", int(position.time) * 1000))
        plan["entry_price"] = float(position.price_open)
    now_msc = time.time_ns() // 1_000_000
    extreme = _favorable_extreme(mt5, plan, now_msc, server_offset_hours, server_time_mode)
    if extreme is None:
        return
    direction = Direction(plan["direction"])
    levels = [float(value) for value in plan["take_profits"]]
    touched = int(plan.get("touched_target", 0))
    for number, level in enumerate(levels, 1):
        hit = extreme >= level if direction is Direction.LONG else extreme <= level
        if hit:
            touched = max(touched, number)
    plan["touched_target"] = touched
    strategy = get_strategy(str(plan["strategy"]))
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return

    def close_allowed() -> bool:
        effective_max_spread = max_spread_points
        if symbol_max_spread_points and symbol.upper() in symbol_max_spread_points:
            effective_max_spread = symbol_max_spread_points[symbol.upper()]
        if not exit_spread_guard_enabled or effective_max_spread <= 0:
            return True
        tick = mt5.symbol_info_tick(symbol)
        spread = float(tick.ask) - float(tick.bid) if tick else float("inf")
        limit = effective_max_spread * float(symbol_info.point)
        if spread <= limit + 1e-12:
            plan.pop("close_deferred_since_msc", None)
            return True
        since = int(plan.setdefault("close_deferred_since_msc", now_msc))
        return now_msc - since >= int(close_spread_hard_cap_minutes * 60_000)

    # The last leg remains protected by native MT5 TP. Earlier executable legs
    # are closed once their target has traded; volume rounding was fixed at entry.
    open_volume = sum(float(item.volume) for item in positions)
    for leg in plan.get("exit_legs", []):
        if leg.get("closed") or int(leg["target"]) >= int(plan["final_target"]):
            continue
        if touched < int(leg["target"]):
            continue
        if not close_allowed():
            continue
        requested = min(float(leg["volume"]), open_volume)
        remaining = requested
        for item in positions:
            if remaining <= 1e-9:
                break
            amount = min(remaining, float(item.volume))
            if _close_position(mt5, item, symbol_info, deviation, amount):
                remaining -= amount
                open_volume -= amount
        if remaining <= 1e-9:
            leg["closed"] = True

    target_tp = float(levels[int(plan["final_target"]) - 1])
    if strategy.dynamic_tp2_minutes is not None and touched >= 2:
        elapsed = (now_msc - int(plan["entry_time_msc"])) / 60_000
        selected = strategy.dynamic_fast_target if elapsed <= strategy.dynamic_tp2_minutes else strategy.dynamic_slow_target
        plan["final_target"] = selected
        target_tp = levels[selected - 1]
        if touched >= selected:
            current_positions = _matching(list(mt5.positions_get(symbol=symbol) or ()), plan)
            if current_positions and close_allowed() and all(_close_position(mt5, item, symbol_info, deviation) for item in current_positions):
                plan["status"] = "completed"
                plan["completion_reason"] = f"dynamic_tp{selected}"
                _save(path, plan)
                return

    stop_target = int(plan.get("active_stop_target", -1))
    for trigger, destination in strategy.stop_moves:
        if touched >= trigger:
            stop_target = max(stop_target, destination)
    if strategy.timed_breakeven_minutes is not None:
        due = int(plan["entry_time_msc"]) + int(strategy.timed_breakeven_minutes * 60_000)
        current_tick = mt5.symbol_info_tick(symbol)
        current = float(current_tick.bid if direction is Direction.LONG else current_tick.ask)
        profitable = current > float(plan["entry_price"]) if direction is Direction.LONG else current < float(plan["entry_price"])
        if now_msc >= due and profitable:
            stop_target = max(stop_target, 0)

    if strategy.time_exit_minutes is not None:
        due = int(plan["entry_time_msc"]) + int(strategy.time_exit_minutes * 60_000)
        if now_msc >= due and (not strategy.time_exit_if_no_tp1 or touched < 1):
            if close_allowed() and all(_close_position(mt5, item, symbol_info, deviation) for item in positions):
                plan["status"] = "completed"
                plan["completion_reason"] = f"time_exit_{strategy.time_exit_minutes:g}m"
                _save(path, plan)
                return

    desired_stop = float(plan["entry_price"] if stop_target == 0 else levels[stop_target - 1] if stop_target > 0 else plan["stop_loss"])
    current_positions = _matching(list(mt5.positions_get(symbol=symbol) or ()), plan)
    # Ensure open positions on MT5 always have SL and TP attached
    if current_positions:
        for pos in current_positions:
            pos_sl = float(getattr(pos, "sl", 0.0) or 0.0)
            pos_tp = float(getattr(pos, "tp", 0.0) or 0.0)
            if pos_sl == 0.0 or pos_tp == 0.0:
                _modify_position(mt5, pos, stop=desired_stop, take_profit=target_tp)

    if stop_target > int(plan.get("active_stop_target", -1)) or strategy.dynamic_tp2_minutes is not None:
        stop = desired_stop
        current_tick = mt5.symbol_info_tick(symbol)
        current = float(current_tick.bid if direction is Direction.LONG else current_tick.ask)
        minimum_distance = max(
            float(symbol_info.trade_stops_level) * float(symbol_info.point),
            float(symbol_info.point),
        )
        stop_is_placeable = (
            current - stop >= minimum_distance
            if direction is Direction.LONG
            else stop - current >= minimum_distance
        )
        if current_positions and not stop_is_placeable and stop_target >= 0:
            # The trigger and reversal happened between polls (or while the bot
            # was stopped). Do not leave the wider original risk in place.
            if close_allowed() and all(_close_position(mt5, item, symbol_info, deviation) for item in current_positions):
                plan["status"] = "completed"
                plan["completion_reason"] = "managed_stop_crossed_before_modify"
                _save(path, plan)
                return
        elif current_positions and all(_modify_position(mt5, item, stop=stop, take_profit=target_tp) for item in current_positions):
            plan["active_stop_target"] = stop_target

    plan["last_check_msc"] = now_msc
    _save(path, plan)


def manage(payload: dict[str, Any]) -> dict[str, Any]:
    account = AccountConfig(**payload["account"])
    directory = Path(payload["strategy_state_dir"]) / account.name
    if not directory.exists():
        return {"account": account.name, "managed": 0}
    import MetaTrader5 as mt5
    if not mt5.initialize(str(Path(account.terminal_path)), timeout=60_000):
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
    managed = 0
    try:
        for path in directory.glob("*.json"):
            plan = json.loads(path.read_text(encoding="utf-8"))
            if plan.get("status") == "active":
                _manage_plan(
                    mt5,
                    path,
                    plan,
                    int(payload["deviation_points"]),
                    float(payload.get("pending_timeout_minutes", 0.0)),
                    int(payload.get("max_spread_points", 0)),
                    float(payload.get("mt5_server_offset_hours", 3.0)),
                    float(payload.get("close_spread_hard_cap_minutes", 10.0)),
                    str(payload.get("server_time_mode", "auto")),
                    bool(payload.get("pending_timeout_enabled", True)),
                    bool(payload.get("exit_spread_guard_enabled", True)),
                    payload.get("symbol_max_spread_points"),
                )
                managed += 1
    finally:
        mt5.shutdown()
    return {"account": account.name, "managed": managed}


def main() -> None:
    try:
        print(json.dumps(manage(json.loads(sys.stdin.read())), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
