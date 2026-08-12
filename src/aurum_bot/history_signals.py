from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Direction
from .parser import (
    ENTRY_RE,
    HEADER_RE,
    SL_RE,
    TP_RE,
    is_supported_symbol,
    normalize_signal_symbol,
)


MOSCOW_TZ = timezone(timedelta(hours=3), "Europe/Moscow")


@dataclass(frozen=True)
class HistoricalSignal:
    message_id: int
    symbol: str
    direction: Direction
    timestamp_utc: datetime
    timestamp_moscow: datetime
    entry: float
    stop_loss: float
    take_profits: tuple[float, float, float, float]
    raw_text: str
    has_image: bool | None = None

    @property
    def tp1(self) -> float:
        return self.take_profits[0]

    @property
    def indicator(self) -> str:
        if self.has_image is True:
            return "Индюк 1"
        if self.has_image is False:
            return "Индюк 2"
        return "Неизвестно"

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "timestamp_moscow": self.timestamp_moscow.isoformat(),
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "tp1": self.take_profits[0],
            "tp2": self.take_profits[1],
            "tp3": self.take_profits[2],
            "tp4": self.take_profits[3],
            "raw_text": self.raw_text,
            "has_image": self.has_image,
            "indicator": self.indicator,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoricalSignal":
        return cls(
            message_id=int(data["message_id"]),
            symbol=str(data["symbol"]),
            direction=Direction(str(data["direction"])),
            timestamp_utc=datetime.fromisoformat(str(data["timestamp_utc"])),
            timestamp_moscow=datetime.fromisoformat(str(data["timestamp_moscow"])),
            entry=float(data["entry"]),
            stop_loss=float(data["stop_loss"]),
            take_profits=tuple(
                float(data[f"tp{number}"]) for number in range(1, 5)
            ),  # type: ignore[arg-type]
            raw_text=str(data.get("raw_text", "")),
            has_image=(
                bool(data["has_image"])
                if data.get("has_image") is not None
                else None
            ),
        )


def _matched_price(match: re.Match[str]) -> float:
    return float(match.group("price").replace(",", "."))


def parse_historical_signal(
    message_id: int,
    text: str | None,
    timestamp: datetime,
    has_image: bool | None = None,
) -> HistoricalSignal | None:
    """Parse complete supported entry calls with all four targets."""
    if not text:
        return None
    header = HEADER_RE.search(text)
    entry_match = ENTRY_RE.search(text)
    sl_match = SL_RE.search(text)
    if not all((header, entry_match, sl_match)):
        return None

    raw_symbol = header.group("symbol").upper()
    if not is_supported_symbol(raw_symbol):
        return None
    symbol = normalize_signal_symbol(raw_symbol)
    direction = Direction(header.group("direction").upper())
    targets = {
        int(match.group("number")): _matched_price(match)
        for match in TP_RE.finditer(text)
    }
    if set(targets) != {1, 2, 3, 4}:
        return None

    entry = _matched_price(entry_match)
    stop_loss = _matched_price(sl_match)
    take_profits = tuple(targets[number] for number in range(1, 5))
    if direction is Direction.LONG:
        valid = stop_loss < entry < take_profits[0] < take_profits[1]
        valid = valid and take_profits[1] < take_profits[2] < take_profits[3]
    else:
        valid = take_profits[3] < take_profits[2] < take_profits[1]
        valid = valid and take_profits[1] < take_profits[0] < entry < stop_loss
    if not valid:
        return None

    utc_timestamp = timestamp.astimezone(timezone.utc).replace(microsecond=0)
    return HistoricalSignal(
        message_id=message_id,
        symbol=symbol,
        direction=direction,
        timestamp_utc=utc_timestamp,
        timestamp_moscow=utc_timestamp.astimezone(MOSCOW_TZ),
        entry=entry,
        stop_loss=stop_loss,
        take_profits=take_profits,  # type: ignore[arg-type]
        raw_text=text,
        has_image=has_image,
    )


def save_signals(
    signals: Iterable[HistoricalSignal],
    json_path: Path,
    csv_path: Path,
) -> None:
    ordered = sorted(signals, key=lambda item: (item.timestamp_utc, item.message_id))
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_temporary = json_path.with_suffix(json_path.suffix + ".tmp")
    json_temporary.write_text(
        json.dumps([signal.to_dict() for signal in ordered], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(json_temporary, json_path)
    fieldnames = [
        "message_id",
        "symbol",
        "direction",
        "timestamp_utc",
        "timestamp_moscow",
        "entry",
        "stop_loss",
        "tp1",
        "tp2",
        "tp3",
        "tp4",
        "raw_text",
        "has_image",
        "indicator",
    ]
    csv_temporary = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with csv_temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for signal in ordered:
            writer.writerow(signal.to_dict())
    os.replace(csv_temporary, csv_path)


def merge_signals(
    existing: Iterable[HistoricalSignal],
    incoming: Iterable[HistoricalSignal],
) -> list[HistoricalSignal]:
    """Merge calls by Telegram message ID; freshly read data wins on edits."""
    by_message_id = {signal.message_id: signal for signal in existing}
    by_message_id.update({signal.message_id: signal for signal in incoming})
    return sorted(
        by_message_id.values(),
        key=lambda item: (item.timestamp_utc, item.message_id),
    )


def upsert_signal_archive(
    signal: HistoricalSignal,
    json_path: Path,
    csv_path: Path | None = None,
) -> list[HistoricalSignal]:
    """Persist one real-time call in the cumulative JSON and CSV archive."""
    csv_path = csv_path or json_path.with_suffix(".csv")
    existing = load_signals(json_path) if json_path.exists() else []
    merged = merge_signals(existing, [signal])
    save_signals(merged, json_path, csv_path)
    return merged


def load_signals(path: Path) -> list[HistoricalSignal]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Signals JSON must contain a list")
    return [HistoricalSignal.from_dict(item) for item in payload]


def message_has_image(message: Any) -> bool:
    """Return True for an attached Telegram photo or image document."""
    if getattr(message, "photo", None) is not None:
        return True
    document = getattr(message, "document", None)
    mime_type = str(getattr(document, "mime_type", ""))
    return mime_type.lower().startswith("image/")
