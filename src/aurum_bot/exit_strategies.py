from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math


@dataclass(frozen=True)
class StrategySpec:
    key: str
    title: str
    target_number: int
    target_fractions: tuple[float, float, float, float] | None = None
    stop_moves: tuple[tuple[int, int], ...] = ()
    timed_breakeven_minutes: float | None = None
    time_exit_minutes: float | None = None
    time_exit_if_no_tp1: bool = False
    target_by_order_kind: tuple[int, int] | None = None
    target_by_asset_class: tuple[int, int] | None = None
    session_target_numbers: tuple[int, int] | None = None
    session_start_hour_utc: int = 7
    session_end_hour_utc: int = 20
    dynamic_tp2_minutes: float | None = None
    dynamic_fast_target: int = 4
    dynamic_slow_target: int = 3

    @property
    def fractions(self) -> tuple[float, float, float, float]:
        if self.target_fractions is not None:
            return self.target_fractions
        return tuple(
            1.0 if number == self.target_number else 0.0
            for number in range(1, 5)
        )  # type: ignore[return-value]


STRATEGIES = (
    StrategySpec("sl_tp1", "Полностью TP1", 1),
    StrategySpec("sl_tp2", "Полностью TP2", 2),
    StrategySpec("sl_tp3", "Полностью TP3", 3),
    StrategySpec("sl_tp4", "Полностью TP4", 4),
    StrategySpec("tp2_50_tp4_50", "50% TP2 + 50% TP4", 4, (0, .5, 0, .5)),
    StrategySpec("tp2_75_tp4_25", "75% TP2 + 25% TP4", 4, (0, .75, 0, .25)),
    StrategySpec("tp2_25_tp4_75", "25% TP2 + 75% TP4", 4, (0, .25, 0, .75)),
    StrategySpec("tp1_tp2_tp3_tp4_equal", "По 25% на TP1–TP4", 4, (.25, .25, .25, .25)),
    StrategySpec("tp4_lock_tp1_after_tp2", "TP4, после TP2 → SL на TP1", 4, stop_moves=((2, 1),)),
    StrategySpec("tp4_staircase", "TP4: после TP2 → TP1, после TP3 → TP2", 4, stop_moves=((2, 1), (3, 2))),
    StrategySpec("tp3_lock_tp1_after_tp2", "TP3, после TP2 → SL на TP1", 3, stop_moves=((2, 1),)),
    StrategySpec("tp2_80_tp4_20", "80% TP2 + 20% TP4", 4, (0, .8, 0, .2)),
    StrategySpec("tp2_60_tp4_40", "60% TP2 + 40% TP4", 4, (0, .6, 0, .4)),
    StrategySpec("tp2_40_tp4_60", "40% TP2 + 60% TP4", 4, (0, .4, 0, .6)),
    StrategySpec("tp1_25_tp2_50_tp4_25", "25% TP1 + 50% TP2 + 25% TP4", 4, (.25, .5, 0, .25)),
    StrategySpec("tp1_tp2_tp4_thirds", "33% TP1 + 33% TP2 + 34% TP4", 4, (.33, .33, 0, .34)),
    StrategySpec("tp1_50_tp4_50", "50% TP1 + 50% TP4", 4, (.5, 0, 0, .5)),
    StrategySpec("tp3_50_tp4_50", "50% TP3 + 50% TP4", 4, (0, 0, .5, .5)),
    StrategySpec("tp2_50_tp3_50", "50% TP2 + 50% TP3", 3, (0, .5, .5, 0)),
    StrategySpec("market_tp2_pending_tp4", "MARKET → TP2, LIMIT/STOP → TP4", 4, target_by_order_kind=(2, 4)),
    StrategySpec("gold_indices_tp4_forex_tp2", "GOLD/индексы → TP4, остальные → TP2", 4, target_by_asset_class=(4, 2)),
    StrategySpec("active_session_tp4_other_tp2", "07:00–20:00 UTC → TP4, иначе TP2", 4, session_target_numbers=(4, 2)),
    StrategySpec("tp4_exit_2h_if_no_tp1", "Закрытие через 2 часа без TP1", 4, time_exit_minutes=120, time_exit_if_no_tp1=True),
    StrategySpec("tp4_exit_4h_if_no_tp1", "Закрытие через 4 часа без TP1", 4, time_exit_minutes=240, time_exit_if_no_tp1=True),
    StrategySpec("tp4_be_after_tp1", "TP4, после TP1 → Б/У", 4, stop_moves=((1, 0),)),
    StrategySpec("tp4_be_after_tp2", "TP4, после TP2 → Б/У", 4, stop_moves=((2, 0),)),
    StrategySpec("tp3_be_after_tp1", "TP3, после TP1 → Б/У", 3, stop_moves=((1, 0),)),
    StrategySpec("tp3_be_after_tp2", "TP3, после TP2 → Б/У", 3, stop_moves=((2, 0),)),
    StrategySpec("tp4_full_staircase", "Полная лесенка TP1→Б/У→TP1→TP2", 4, stop_moves=((1, 0), (2, 1), (3, 2))),
    StrategySpec("tp4_soft_staircase", "TP4: после TP2 → Б/У, после TP3 → TP1", 4, stop_moves=((2, 0), (3, 1))),
    StrategySpec("tp2_50_tp4_50_be_after_tp2", "50% TP2 + 50% TP4, остаток в Б/У", 4, (0, .5, 0, .5), stop_moves=((2, 0),)),
    StrategySpec("tp1_25_tp2_25_tp4_50_stair", "25% TP1 + 25% TP2 + 50% TP4 с лесенкой", 4, (.25, .25, 0, .5), stop_moves=((1, 0), (2, 1))),
    StrategySpec("tp1_50_tp4_50_be_after_tp1", "50% TP1 + 50% TP4, остаток в Б/У", 4, (.5, 0, 0, .5), stop_moves=((1, 0),)),
    StrategySpec("tp1_50_tp4_50_staircase", "50% TP1 + 50% TP4 с лесенкой: TP1→Б/У, TP2→TP1", 4, (.5, 0, 0, .5), stop_moves=((1, 0), (2, 1))),
    StrategySpec("tp2_33_tp4_67_lock_tp1", "33% TP2 + 67% TP4, остаток защищён TP1", 4, (0, .33, 0, .67), stop_moves=((2, 1),)),
    StrategySpec("dynamic_tp3_tp4_be", "Быстрый TP2 → TP4, медленный → TP3; SL в Б/У", 4, stop_moves=((2, 0),), dynamic_tp2_minutes=30),
    StrategySpec("tp4_time_be_15m", "TP4, через 15 минут в прибыли → Б/У", 4, timed_breakeven_minutes=15),
    StrategySpec("tp4_time_be_30m", "TP4, через 30 минут в прибыли → Б/У", 4, timed_breakeven_minutes=30),
    StrategySpec("tp4_time_be_60m", "TP4, через 60 минут в прибыли → Б/У", 4, timed_breakeven_minutes=60),
    StrategySpec("tp4_time_be_120m", "TP4, через 120 минут в прибыли → Б/У", 4, timed_breakeven_minutes=120),
    StrategySpec("tp4_time_be_240m", "TP4, через 240 минут в прибыли → Б/У", 4, timed_breakeven_minutes=240),
)

STRATEGIES_BY_KEY = {strategy.key: strategy for strategy in STRATEGIES}


def get_strategy(key: str) -> StrategySpec:
    try:
        return STRATEGIES_BY_KEY[key]
    except KeyError as exc:
        allowed = ", ".join(STRATEGIES_BY_KEY)
        raise ValueError(f"Unknown exit strategy {key!r}. Allowed: {allowed}") from exc


import time


def effective_target(
    strategy: StrategySpec,
    *,
    execution_kind: str,
    symbol: str,
    published_at_ms: int | None,
    take_profit_target: int | None = None,
) -> int:
    if strategy.target_by_order_kind is not None:
        market, pending = strategy.target_by_order_kind
        return market if execution_kind == "market" else pending
    if strategy.target_by_asset_class is not None:
        selected, other = strategy.target_by_asset_class
        return selected if symbol in {"XAUUSD", "DE40", "US100"} else other
    if strategy.session_target_numbers is not None:
        active, other = strategy.session_target_numbers
        timestamp = (published_at_ms / 1000) if published_at_ms else time.time()
        hour = datetime.fromtimestamp(timestamp, timezone.utc).hour
        return active if strategy.session_start_hour_utc <= hour < strategy.session_end_hour_utc else other
    return take_profit_target if take_profit_target is not None else strategy.target_number



def executable_legs(
    strategy: StrategySpec,
    *,
    total_volume: float,
    volume_min: float,
    volume_step: float,
    target_number: int,
) -> list[tuple[int, float]]:
    fractions = (
        strategy.target_fractions
        if strategy.target_fractions is not None
        and strategy.target_by_order_kind is None
        and strategy.target_by_asset_class is None
        and strategy.session_target_numbers is None
        and strategy.dynamic_tp2_minutes is None
        else tuple(1.0 if number == target_number else 0.0 for number in range(1, 5))
    )
    planned = [(number, fraction) for number, fraction in enumerate(fractions, 1) if fraction]
    total_units = int(round(total_volume / volume_step))
    minimum_units = max(1, int(math.ceil(volume_min / volume_step - 1e-9)))
    used_units = 0
    cumulative = 0.0
    result: list[tuple[int, float]] = []
    for index, (target, fraction) in enumerate(planned):
        cumulative += fraction
        if index == len(planned) - 1:
            units = total_units - used_units
        else:
            units = max(0, int(math.floor(cumulative * total_units + 1e-9)) - used_units)
            remainder = total_units - used_units - units
            if 0 < units < minimum_units or 0 < remainder < minimum_units:
                units = 0
        if units:
            result.append((target, round(units * volume_step, 10)))
            used_units += units
    return result
