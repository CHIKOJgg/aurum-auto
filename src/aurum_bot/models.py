from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class ExecutionKind(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    PENDING = "pending"
    SKIP_STOP_CROSSED = "skip_stop_crossed"



@dataclass(frozen=True)
class Signal:
    message_id: int
    symbol: str
    direction: Direction
    entry: float
    stop_loss: float
    take_profit: float
    take_profits: tuple[float, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["direction"] = self.direction.value
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Signal":
        return cls(
            message_id=int(data["message_id"]),
            symbol=str(data["symbol"]),
            direction=Direction(str(data["direction"])),
            entry=float(data["entry"]),
            stop_loss=float(data["stop_loss"]),
            take_profit=float(data["take_profit"]),
            take_profits=(
                tuple(float(value) for value in data["take_profits"])
                if data.get("take_profits") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class AccountConfig:
    name: str
    enabled: bool
    risk_base_usd: float
    terminal_path: str
    symbols: dict[str, str]
    commission_per_lot_usd: dict[str, float] | None = None
    commission_rate_percent: dict[str, float] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def broker_symbol(self, signal_symbol: str) -> str:
        """Resolve explicit aliases; FX pairs use their signal name by default."""
        normalized = signal_symbol.upper()
        if normalized in self.symbols:
            return self.symbols[normalized]
        if len(normalized) == 6 and normalized.isalpha():
            return normalized
        raise KeyError(f"Unsupported signal symbol for {self.name}: {normalized}")

    def supports_symbol(self, signal_symbol: str) -> bool:
        """Return whether this account can execute the signal instrument."""
        normalized = signal_symbol.upper()
        return (
            normalized in self.symbols
            or (len(normalized) == 6 and normalized.isalpha())
        )

    def commission_for_one_lot(
        self,
        signal_symbol: str,
        entry_price: float,
        contract_size: float,
        quote_to_usd_rate: float = 1.0,
    ) -> float:
        """Return the full commission charged for opening one lot in USD."""
        normalized = signal_symbol.upper()
        fixed = self.commission_per_lot_usd or {}
        rates = self.commission_rate_percent or {}
        has_symbol_override = normalized in fixed or normalized in rates
        is_six_letter_pair = len(normalized) == 6 and normalized.isalpha()
        key = (
            normalized
            if has_symbol_override
            else "FOREX" if is_six_letter_pair else "DEFAULT"
        )
        fixed_amount = float(fixed.get(key, fixed.get("DEFAULT", 0.0)))
        rate_percent = float(rates.get(key, rates.get("DEFAULT", 0.0)))
        notional_amount = (
            abs(float(entry_price))
            * abs(float(contract_size))
            * rate_percent
            / 100
        ) * abs(float(quote_to_usd_rate))
        return fixed_amount + notional_amount



    def has_configured_commission(self, signal_symbol: str) -> bool:
        """Return whether config explicitly defines commission for a symbol."""
        normalized = signal_symbol.upper()
        fixed = self.commission_per_lot_usd or {}
        rates = self.commission_rate_percent or {}
        if normalized in fixed or normalized in rates:
            return True
        fallback = (
            "FOREX"
            if len(normalized) == 6 and normalized.isalpha()
            else "DEFAULT"
        )
        return fallback in fixed or fallback in rates


@dataclass(frozen=True)
class ExecutionResult:
    account: str
    status: str
    detail: str
    ticket: int | None = None
    tickets: list[int] | None = None
    volume: float | None = None
    execution_kind: str | None = None
    publication_to_receive_ms: int | None = None
    publication_to_confirmation_ms: int | None = None
    receive_to_confirmation_ms: int | None = None
    order_send_round_trip_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
