from __future__ import annotations

import re

from .models import Direction, Signal


ALLOWED_SYMBOLS = frozenset({"XAUUSD", "XAGUSD", "DE40", "US100"})
SIGNAL_SYMBOL_ALIASES: dict[str, str] = {
    "GOLD": "XAUUSD",
    "SILVER": "XAGUSD",
    "GERMANY40": "DE40",
    "USNDAQ100": "US100",
}
# Module-level defaults are kept for CLI tools and tests.
# Live bot passes config-driven values via function parameters.
FOREX_SYMBOL_RE = re.compile(r"^[A-Z]{6}$")

HEADER_RE = re.compile(
    r"(?im)^\s*#(?P<symbol>[A-Z0-9._-]+)\s+(?P<direction>LONG|SHORT)\b"
)
ENTRY_RE = re.compile(
    r"(?im)^\s*[*_🔸]?\s*Вход(?:\s+сейчас)?(?:\s+(?:или|:|@))?\s*(?P<price>\d+(?:[.,]\d+)?)"
)
SL_RE = re.compile(
    r"(?im)^\s*[*_🛑]?\s*SL\s*[:\-=]?\s*(?P<price>\d+(?:[.,]\d+)?)"
)
TP_RE = re.compile(
    r"(?im)^\s*(?:\S+\s*)?TP\s*(?P<number>[1-4])\s*[:\-=]?\s*(?P<price>\d+(?:[.,]\d+)?)"
)


def _price(match: re.Match[str]) -> float:
    return float(match.group("price").replace(",", "."))


def normalize_signal_symbol(
    symbol: str,
    aliases: dict[str, str] | None = None,
) -> str:
    normalized = symbol.upper()
    mapping = aliases if aliases is not None else SIGNAL_SYMBOL_ALIASES
    return mapping.get(normalized, normalized)


def is_supported_symbol(
    symbol: str,
    allowed: frozenset[str] | None = None,
    aliases: dict[str, str] | None = None,
) -> bool:
    """Accept configured instruments and six-letter FX pairs."""
    normalized = normalize_signal_symbol(symbol, aliases)
    allowed_set = allowed if allowed is not None else ALLOWED_SYMBOLS
    return normalized in allowed_set or FOREX_SYMBOL_RE.fullmatch(normalized) is not None


def parse_signal(
    message_id: int,
    text: str | None,
    take_profit_target: int = 2,
    allowed_symbols: frozenset[str] | None = None,
    symbol_aliases: dict[str, str] | None = None,
) -> Signal | None:
    """Parse a strict entry call; status/TP/SL posts intentionally return None."""
    if take_profit_target not in {1, 2, 3, 4}:
        raise ValueError("take_profit_target must be an integer from 1 to 4")
    if not text:
        return None

    header = HEADER_RE.search(text)
    entry_match = ENTRY_RE.search(text)
    sl_match = SL_RE.search(text)
    take_profits = {
        int(match.group("number")): _price(match) for match in TP_RE.finditer(text)
    }
    tp_match = take_profits.get(take_profit_target)
    if not all((header, entry_match, sl_match, tp_match)):
        return None

    raw_symbol = header.group("symbol").upper()
    if not is_supported_symbol(raw_symbol, allowed_symbols, symbol_aliases):
        return None
    symbol = normalize_signal_symbol(raw_symbol, symbol_aliases)

    direction = Direction(header.group("direction").upper())
    entry = _price(entry_match)
    stop_loss = _price(sl_match)
    take_profit = float(tp_match)

    if set(take_profits) != {1, 2, 3, 4}:
        return None

    if direction is Direction.LONG:
        valid_geometry = (
            stop_loss < entry < take_profit
            and take_profits[1] < take_profits[2] < take_profits[3] < take_profits[4]
        )
    else:
        valid_geometry = (
            take_profit < entry < stop_loss
            and take_profits[1] > take_profits[2] > take_profits[3] > take_profits[4]
        )
    if not valid_geometry:
        return None


    return Signal(
        message_id=message_id,
        symbol=symbol,
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        take_profits=(
            float(take_profits[1]),
            float(take_profits[2]),
            float(take_profits[3]),
            float(take_profits[4]),
        ),
    )
