from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .models import AccountConfig, Direction
from .mt5_commission import infer_round_turn_commission_per_lot


LOGGER = logging.getLogger("aurum_bot.mt5_history")
TICK_DTYPE = np.dtype(
    [
        ("time_msc", "<i8"),
        ("bid", "<f8"),
        ("ask", "<f8"),
    ]
)


@dataclass(frozen=True)
class SymbolMetadata:
    broker_symbol: str
    digits: int
    point: float
    trade_stops_level: int
    volume_min: float
    volume_max: float
    volume_step: float
    contract_size: float


class MT5History:
    """Read-only MT5 history access. This class never calls order_send."""

    def __init__(self, account: AccountConfig):
        self.account = account
        self.mt5: Any | None = None
        self.account_info: Any | None = None

    def __enter__(self) -> "MT5History":
        import MetaTrader5 as mt5

        terminal_path = Path(self.account.terminal_path)
        if not terminal_path.is_file():
            raise FileNotFoundError(f"MT5 terminal not found: {terminal_path}")
        if not mt5.initialize(str(terminal_path), timeout=60_000):
            raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
        self.mt5 = mt5
        self.account_info = mt5.account_info()
        if self.account_info is None:
            mt5.shutdown()
            raise RuntimeError(f"MT5 account unavailable: {mt5.last_error()}")
        if str(self.account_info.currency).upper() != "USD":
            mt5.shutdown()
            raise RuntimeError(
                f"Expected USD account, got {self.account_info.currency}"
            )
        LOGGER.info(
            "Connected read-only history client: login=%s server=%s balance=%s %s",
            self.account_info.login,
            self.account_info.server,
            self.account_info.balance,
            self.account_info.currency,
        )
        return self

    def __exit__(self, *_: object) -> None:
        if self.mt5 is not None:
            self.mt5.shutdown()
        self.mt5 = None

    def symbol_metadata(self, signal_symbol: str) -> SymbolMetadata:
        if self.mt5 is None:
            raise RuntimeError("MT5History is not initialized")
        broker_symbol = self.account.broker_symbol(signal_symbol)
        if not self.mt5.symbol_select(broker_symbol, True):
            raise RuntimeError(
                f"symbol_select failed for {broker_symbol}: {self.mt5.last_error()}"
            )
        info = self.mt5.symbol_info(broker_symbol)
        if info is None:
            raise RuntimeError(
                f"symbol_info failed for {broker_symbol}: {self.mt5.last_error()}"
            )
        return SymbolMetadata(
            broker_symbol=broker_symbol,
            digits=int(info.digits),
            point=float(info.point),
            trade_stops_level=int(info.trade_stops_level),
            volume_min=float(info.volume_min),
            volume_max=float(info.volume_max),
            volume_step=float(info.volume_step),
            contract_size=float(info.trade_contract_size),
        )

    def download_ticks(
        self,
        metadata: SymbolMetadata,
        start_utc: datetime,
        end_utc: datetime,
        cache_dir: Path,
    ) -> np.ndarray:
        if self.mt5 is None:
            raise RuntimeError("MT5History is not initialized")
        start_utc = start_utc.astimezone(timezone.utc)
        end_utc = end_utc.astimezone(timezone.utc)
        if end_utc <= start_utc:
            raise ValueError("Tick range end must be after start")
        cache_dir.mkdir(parents=True, exist_ok=True)

        chunks: list[np.ndarray] = []
        day = start_utc.date()
        while datetime.combine(day, time.min, tzinfo=timezone.utc) < end_utc:
            day_start = datetime.combine(day, time.min, tzinfo=timezone.utc)
            next_day = day_start + timedelta(days=1)
            chunk_start = max(start_utc, day_start)
            chunk_end_exclusive = min(end_utc, next_day)
            query_end = chunk_end_exclusive - timedelta(milliseconds=1)
            safe_symbol = "".join(
                character if character.isalnum() else "_"
                for character in metadata.broker_symbol
            )
            cache_path = cache_dir / (
                f"{safe_symbol}_{chunk_start:%Y%m%dT%H%M%S}_"
                f"{chunk_end_exclusive:%Y%m%dT%H%M%S}.npy"
            )
            if cache_path.exists():
                compact = np.load(cache_path, mmap_mode="r")
                LOGGER.info(
                    "Loaded cached %s ticks for %s from %s",
                    len(compact),
                    metadata.broker_symbol,
                    cache_path.name,
                )
            else:
                raw = self.mt5.copy_ticks_range(
                    metadata.broker_symbol,
                    chunk_start,
                    query_end,
                    self.mt5.COPY_TICKS_ALL,
                )
                if raw is None:
                    raise RuntimeError(
                        f"copy_ticks_range failed for {metadata.broker_symbol} "
                        f"{chunk_start.isoformat()}..{query_end.isoformat()}: "
                        f"{self.mt5.last_error()}"
                    )
                valid = (raw["bid"] > 0) & (raw["ask"] > 0)
                compact = np.empty(int(np.count_nonzero(valid)), dtype=TICK_DTYPE)
                compact["time_msc"] = raw["time_msc"][valid]
                compact["bid"] = raw["bid"][valid]
                compact["ask"] = raw["ask"][valid]
                temp_path = cache_path.with_name(cache_path.name + ".tmp")
                np.save(temp_path, compact, allow_pickle=False)
                os.replace(temp_path.with_name(temp_path.name + ".npy"), cache_path)
                LOGGER.info(
                    "Downloaded %s ticks for %s (%s)",
                    len(compact),
                    metadata.broker_symbol,
                    day,
                )
            chunks.append(np.asarray(compact))
            day = next_day.date()

        combined = (
            np.concatenate(chunks)
            if chunks
            else np.empty(0, dtype=TICK_DTYPE)
        )
        if len(combined) and np.any(np.diff(combined["time_msc"]) < 0):
            combined.sort(order="time_msc")
        LOGGER.info(
            "Prepared %s total ticks for %s",
            len(combined),
            metadata.broker_symbol,
        )
        return combined

    def download_m1_as_ticks(
        self,
        metadata: SymbolMetadata,
        start_utc: datetime,
        end_utc: datetime,
        direction: Direction,
        cache_dir: Path,
    ) -> np.ndarray:
        """Build conservative synthetic ticks from broker M1 bars.

        This is used only where the broker no longer supplies real tick history.
        The intra-minute path visits the adverse extreme before the favorable
        extreme, so an ambiguous SL/TP minute is counted against the strategy.
        """
        if self.mt5 is None:
            raise RuntimeError("MT5History is not initialized")
        rates = self.mt5.copy_rates_range(
            metadata.broker_symbol,
            self.mt5.TIMEFRAME_M1,
            start_utc.astimezone(timezone.utc),
            end_utc.astimezone(timezone.utc),
        )
        if rates is None:
            raise RuntimeError(
                f"copy_rates_range failed for {metadata.broker_symbol}: "
                f"{self.mt5.last_error()}"
            )
        result = np.empty(len(rates) * 4, dtype=TICK_DTYPE)
        cursor = 0
        for rate in rates:
            if direction is Direction.LONG:
                prices = (rate["open"], rate["low"], rate["high"], rate["close"])
            else:
                prices = (rate["open"], rate["high"], rate["low"], rate["close"])
            spread = max(float(rate["spread"]) * metadata.point, metadata.point)
            base_msc = int(rate["time"]) * 1000
            for offset_seconds, bid in zip((0, 15, 30, 45), prices):
                result[cursor] = (base_msc + offset_seconds * 1000, bid, bid + spread)
                cursor += 1
        cache_dir.mkdir(parents=True, exist_ok=True)
        safe_symbol = "".join(
            character if character.isalnum() else "_"
            for character in metadata.broker_symbol
        )
        cache_path = cache_dir / (
            f"{safe_symbol}_M1_{direction.value}_"
            f"{start_utc:%Y%m%dT%H%M%S}_{end_utc:%Y%m%dT%H%M%S}.npy"
        )
        temp_path = cache_path.with_name(cache_path.name + ".tmp")
        np.save(temp_path, result, allow_pickle=False)
        os.replace(temp_path.with_name(temp_path.name + ".npy"), cache_path)
        LOGGER.info(
            "Prepared %s conservative M1 fallback ticks from %s bars for %s",
            len(result),
            len(rates),
            metadata.broker_symbol,
        )
        return result

    def calc_profit(
        self,
        metadata: SymbolMetadata,
        direction: Direction,
        volume: float,
        open_price: float,
        close_price: float,
    ) -> float | None:
        if self.mt5 is None:
            raise RuntimeError("MT5History is not initialized")
        order_type = (
            self.mt5.ORDER_TYPE_BUY
            if direction is Direction.LONG
            else self.mt5.ORDER_TYPE_SELL
        )
        result = self.mt5.order_calc_profit(
            order_type,
            metadata.broker_symbol,
            volume,
            open_price,
            close_price,
        )
        return None if result is None else float(result)

    def infer_commission_for_one_lot(self, broker_symbol: str) -> float | None:
        if self.mt5 is None:
            raise RuntimeError("MT5History is not initialized")
        return infer_round_turn_commission_per_lot(self.mt5, broker_symbol)
