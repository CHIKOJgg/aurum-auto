from __future__ import annotations

import re

from .models import Direction, Signal


ALLOWED_SYMBOLS = frozenset({"XAUUSD", "XAGUSD", "DE40", "US100"})
SIGNAL_SYMBOL_ALIASES = {
    "GOLD": "XAUUSD",
    "SILVER": "XAGUSD",
    "GERMANY40": "DE40",
    "USNDAQ100": "US100",
}
FOREX_SYMBOL_RE = re.compile(r"^[A-Z]{6}$")

HEADER_RE = re.compile(
    r"(?im)^\s*#(?P<symbol>[A-Z0-9._-]+)\s+(?P<direction>LONG|SHORT)\b"
)
ENTRY_RE = re.compile(
    r"(?im)^\s*[^\w\s]*\s*(?:\w+\s+)?Вход"
    r"(?:\s+сейчас(?:\s+или)?)?\s*:?\s*"
    r"(?P<price>\d+(?:[.,]\d+)?)\s*$"
)
SL_RE = re.compile(
    r"(?im)^\s*[^\w\s]*\s*SL\s*:?\s*(?P<price>\d+(?:[.,]\d+)?)\s*$"
)
TP_RE = re.compile(
    r"(?im)^\s*[^\w\s]*\s*(?:TP\s*|Take\s+profit\s*)"
    r"(?P<number>\d+)\s*:?\s*"
    r"(?P<price>\d+(?:[.,]\d+)?)\s*$"
)


def _price(match: re.Match[str]) -> float:
    return float(match.group("price").replace(",", "."))


def normalize_signal_symbol(symbol: str) -> str:
    normalized = symbol.upper()
    return SIGNAL_SYMBOL_ALIASES.get(normalized, normalized)


def is_supported_symbol(symbol: str) -> bool:
    """Accept configured legacy instruments and six-letter FX pairs."""
    normalized = normalize_signal_symbol(symbol)
    return normalized in ALLOWED_SYMBOLS or FOREX_SYMBOL_RE.fullmatch(normalized) is not None


def parse_signal(
    message_id: int,
    text: str | None,
    take_profit_target: int = 2,
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
    target_count = max(take_profits, default=0)
    # Calls with fewer than four targets exit at their final supplied target.
    # Extra targets are retained, but strategies that refer to TP1--TP4 keep
    # using those exact numbered levels.
    selected_target = target_count if target_count < 4 else take_profit_target
    tp_match = take_profits.get(selected_target)
    if not all((header, entry_match, sl_match, tp_match)):
        return None

    raw_symbol = header.group("symbol").upper()
    if not is_supported_symbol(raw_symbol):
        return None
    symbol = normalize_signal_symbol(raw_symbol)

    direction = Direction(header.group("direction").upper())
    entry = _price(entry_match)
    stop_loss = _price(sl_match)
    take_profit = float(tp_match)

    if direction is Direction.LONG:
        valid_geometry = stop_loss < entry < take_profit
    else:
        valid_geometry = take_profit < entry < stop_loss
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
            tuple(float(take_profits[number]) for number in range(1, target_count + 1))
            if all(number in take_profits for number in range(1, target_count + 1))
            else None
        ),
    )
