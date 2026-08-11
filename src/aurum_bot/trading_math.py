from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .models import Direction, ExecutionKind


def raw_volume_for_risk(
    risk_base_usd: float,
    risk_percent: float,
    loss_for_one_lot: float,
    commission_for_one_lot: float = 0.0,
) -> float | None:
    """Return volume where stop loss plus commission equals target risk."""
    total_loss_for_one_lot = loss_for_one_lot + commission_for_one_lot
    if (
        risk_base_usd <= 0
        or risk_percent <= 0
        or loss_for_one_lot <= 0
        or commission_for_one_lot < 0
        or total_loss_for_one_lot <= 0
    ):
        return None
    target = Decimal(str(risk_base_usd)) * Decimal(str(risk_percent)) / Decimal("100")
    raw = target / Decimal(str(total_loss_for_one_lot))
    return float(raw)


def choose_execution(
    direction: Direction,
    entry: float,
    stop_loss: float,
    executable_price: float,
    minimum_distance: float,
    market_risk_in_range: bool = False,
    strict_call_entry: bool = False,
    market_entry_tolerance_r: float = 0.0,
) -> ExecutionKind:
    """
    Decide using the executable side of the spread (ask for LONG, bid for SHORT).

    Strict call-entry mode always preserves the call price as a pending order.
    Otherwise, use market throughout the inclusive 0.9R-1.1R stop-risk range, including
    a quote exactly at the call entry (1R). Outside that range use a limit.
    A quote at/past SL is also outside the market-entry range and uses a limit.

    market_entry_tolerance_r additionally allows market entry when the quote is
    within this fraction of the stop distance (R) from the call entry, covering
    lot rounding overshoot and small adverse drift. The quote must never be
    at/past the stop loss.
    """
    if strict_call_entry:
        return ExecutionKind.LIMIT

    stop_distance = abs(entry - stop_loss)
    price_within_tolerance = (
        market_entry_tolerance_r > 0
        and stop_distance > 0
        and abs(executable_price - entry)
        <= market_entry_tolerance_r * stop_distance + 1e-9
    )

    if direction is Direction.LONG:
        if executable_price <= stop_loss:
            return ExecutionKind.LIMIT
        if market_risk_in_range or price_within_tolerance:
            return ExecutionKind.MARKET
        return ExecutionKind.LIMIT

    if executable_price >= stop_loss:
        return ExecutionKind.LIMIT
    if market_risk_in_range or price_within_tolerance:
        return ExecutionKind.MARKET
    return ExecutionKind.LIMIT


def volume_for_risk(
    risk_base_usd: float,
    risk_percent: float,
    loss_for_one_lot: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
    commission_for_one_lot: float = 0.0,
) -> float | None:
    """Round to the nearest lot step and enforce the broker/configured minimum."""
    positive_values = (
        risk_base_usd,
        risk_percent,
        loss_for_one_lot,
        volume_min,
        volume_max,
        volume_step,
    )
    if any(value <= 0 for value in positive_values):
        return None
    if commission_for_one_lot < 0:
        return None

    raw_value = raw_volume_for_risk(
        risk_base_usd,
        risk_percent,
        loss_for_one_lot,
        commission_for_one_lot,
    )
    if raw_value is None:
        return None
    raw = Decimal(str(raw_value))
    step = Decimal(str(volume_step))
    steps = (raw / step).to_integral_value(rounding=ROUND_HALF_UP)
    rounded = steps * step
    rounded = max(rounded, Decimal(str(volume_min)))
    rounded = min(rounded, Decimal(str(volume_max)))
    return float(rounded)
