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


_UTC_CLOCK_VERSION = 2
_MAX_BROKER_OFFSET_HOURS = 14
_OFFSET_TOLERANCE_MSC = 5 * 60 * 1000


def _save(path: Path, plan: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _matching(items: tuple[Any, ...] | list[Any], plan: dict[str, Any]) -> list[Any]:
    comment = str(plan["comment"])
    magic = int(plan["magic"])
    return [
        item for item in items
        if int(getattr(item, "magic", -1)) == magic
        and str(getattr(item, "comment", "")) == comment
    ]


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
    return result is not None and int(result.retcode) in _success_codes(mt5)


def _broker_offset_msc(
    tick: Any,
    utc_now_msc: int,
    saved_offset_msc: int | None = None,
) -> int:
    """Return the broker clock offset, rounded to a whole hour.

    Some MT5 terminals expose tick and position timestamps in server time even
    though the Python API accepts timezone-aware datetimes. Comparing those raw
    values with the host's Unix clock can shift a history window by several
    hours. A fresh tick gives us both clocks at the same instant.
    """
    tick_msc = int(getattr(tick, "time_msc", 0) or 0)
    if tick_msc <= 0:
        return int(saved_offset_msc or 0)
    hour_msc = 3_600_000
    difference = tick_msc - utc_now_msc
    rounded = round(difference / hour_msc) * hour_msc
    if (
        abs(rounded) <= _MAX_BROKER_OFFSET_HOURS * hour_msc
        and abs(difference - rounded) <= _OFFSET_TOLERANCE_MSC
    ):
        return int(rounded)
    return int(saved_offset_msc or 0)


def _favorable_extreme(
    mt5: Any,
    plan: dict[str, Any],
    now_msc: int,
    broker_offset_msc: int,
    tick: Any | None = None,
) -> float | None:
    tick = tick or mt5.symbol_info_tick(plan["symbol"])
    if tick is None:
        return None
    direction = Direction(plan["direction"])
    current = float(tick.bid if direction is Direction.LONG else tick.ask)
    entry_msc = int(plan.get("entry_time_msc") or 0)
    if entry_msc <= 0 or now_msc < entry_msc:
        return current
    last_check_msc = int(plan.get("last_check_msc") or entry_msc)
    # Keep the one-second overlap used to avoid missing a boundary tick, but
    # never allow that overlap to reach back before the actual position fill.
    start_msc = max(entry_msc, last_check_msc - 1000)
    try:
        ticks = mt5.copy_ticks_range(
            plan["symbol"],
            datetime.fromtimestamp(
                (start_msc + broker_offset_msc) / 1000,
                timezone.utc,
            ),
            datetime.fromtimestamp(
                (now_msc + broker_offset_msc) / 1000,
                timezone.utc,
            ),
            mt5.COPY_TICKS_ALL,
        )
        if ticks is None or len(ticks) == 0:
            return current
        normalized_times = ticks["time_msc"].astype("int64") - broker_offset_msc
        valid = (
            (normalized_times >= start_msc)
            & (normalized_times >= entry_msc)
            & (normalized_times <= now_msc)
        )
        if not valid.any():
            return current
        values = (
            ticks["bid"][valid]
            if direction is Direction.LONG
            else ticks["ask"][valid]
        )
        return float(max(values) if direction is Direction.LONG else min(values))
    except Exception:
        return current


def _manage_plan(mt5: Any, path: Path, plan: dict[str, Any], deviation: int) -> None:
    if plan.get("status") != "active":
        return
    symbol = str(plan["symbol"])
    positions = _matching(list(mt5.positions_get(symbol=symbol) or ()), plan)
    orders = _matching(list(mt5.orders_get(symbol=symbol) or ()), plan)
    if not positions:
        if orders:
            return
        plan["status"] = "completed"
        plan["completed_at"] = datetime.now(timezone.utc).isoformat()
        _save(path, plan)
        return

    position = positions[0]
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return
    utc_now_msc = time.time_ns() // 1_000_000
    broker_offset_msc = _broker_offset_msc(
        tick,
        utc_now_msc,
        plan.get("mt5_time_offset_msc"),
    )
    if int(plan.get("clock_version", 0)) < _UTC_CLOCK_VERSION:
        # Old plans stored the raw broker timestamp. Re-read the position so an
        # UTC+3 value cannot make the manager inspect pre-entry price history.
        plan["entry_time_msc"] = None
        plan["last_check_msc"] = None
    plan["clock_version"] = _UTC_CLOCK_VERSION
    plan["mt5_time_offset_msc"] = broker_offset_msc
    if plan.get("entry_time_msc") is None:
        raw_entry_msc = int(
            getattr(position, "time_msc", int(position.time) * 1000)
        )
        plan["entry_time_msc"] = raw_entry_msc - broker_offset_msc
        plan["entry_price"] = float(position.price_open)
    raw_tick_msc = int(getattr(tick, "time_msc", 0) or 0)
    now_msc = (
        raw_tick_msc - broker_offset_msc
        if raw_tick_msc > 0
        else utc_now_msc
    )
    extreme = _favorable_extreme(
        mt5,
        plan,
        now_msc,
        broker_offset_msc,
        tick,
    )
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

    # The last leg remains protected by native MT5 TP. Earlier executable legs
    # are closed once their target has traded; volume rounding was fixed at entry.
    open_volume = sum(float(item.volume) for item in positions)
    for leg in plan.get("exit_legs", []):
        if leg.get("closed") or int(leg["target"]) >= int(plan["final_target"]):
            continue
        if touched < int(leg["target"]):
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
        # A three-target call has no TP4 to promote to.
        selected = min(selected, len(levels))
        plan["final_target"] = selected
        target_tp = levels[selected - 1]
        if touched >= selected:
            current_positions = _matching(list(mt5.positions_get(symbol=symbol) or ()), plan)
            if current_positions and all(_close_position(mt5, item, symbol_info, deviation) for item in current_positions):
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
            if all(_close_position(mt5, item, symbol_info, deviation) for item in positions):
                plan["status"] = "completed"
                plan["completion_reason"] = f"time_exit_{strategy.time_exit_minutes:g}m"
                _save(path, plan)
                return

    if stop_target > int(plan.get("active_stop_target", -1)) or strategy.dynamic_tp2_minutes is not None:
        stop = float(plan["entry_price"] if stop_target == 0 else levels[stop_target - 1] if stop_target > 0 else plan["stop_loss"])
        current_positions = _matching(list(mt5.positions_get(symbol=symbol) or ()), plan)
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
            if all(_close_position(mt5, item, symbol_info, deviation) for item in current_positions):
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
                _manage_plan(mt5, path, plan, int(payload["deviation_points"]))
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
