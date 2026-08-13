from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import AccountConfig


MESSAGE_ID_RE = re.compile(r"(?:^|\s)AURUM:(\d+)")


def _weighted_price(deals: Iterable[Any]) -> float:
    items = list(deals)
    total_volume = sum(float(item.volume) for item in items)
    if total_volume <= 0:
        return 0.0
    return sum(float(item.price) * float(item.volume) for item in items) / total_volume


def _deal_time(deal: Any, server_offset_hours: float = 3.0) -> str:
    timestamp = float(getattr(deal, "time_msc", 0)) / 1000
    if timestamp <= 0:
        timestamp = float(deal.time)
    utc_timestamp = timestamp - float(server_offset_hours) * 3600
    return datetime.fromtimestamp(utc_timestamp, tz=timezone.utc).isoformat()


def build_snapshots(
    *,
    account: AccountConfig,
    deals: Iterable[Any],
    open_position_ids: set[int],
    magic: int,
    mt5: Any,
    server_offset_hours: float = 3.0,
) -> list[dict[str, Any]]:
    all_deals = list(deals)
    own_position_ids = {
        int(getattr(deal, "position_id", 0))
        for deal in all_deals
        if int(getattr(deal, "position_id", 0))
        and int(getattr(deal, "magic", 0)) == magic
    }
    groups: dict[int, list[Any]] = defaultdict(list)
    for deal in all_deals:
        position_id = int(getattr(deal, "position_id", 0))
        if position_id in own_position_ids:
            groups[position_id].append(deal)

    reverse_symbols = {broker: signal for signal, broker in account.symbols.items()}
    snapshots: list[dict[str, Any]] = []
    for position_id, position_deals in groups.items():
        message_id: int | None = None
        for deal in position_deals:
            match = MESSAGE_ID_RE.search(str(getattr(deal, "comment", "")))
            if match:
                message_id = int(match.group(1))
                break
        if message_id is None:
            continue

        entries = [
            deal
            for deal in position_deals
            if int(deal.entry) == int(mt5.DEAL_ENTRY_IN)
        ]
        exits = [
            deal
            for deal in position_deals
            if int(deal.entry)
            in {int(mt5.DEAL_ENTRY_OUT), int(mt5.DEAL_ENTRY_OUT_BY)}
        ]
        if not entries:
            continue
        entries.sort(key=lambda item: int(getattr(item, "time_msc", item.time * 1000)))
        exits.sort(key=lambda item: int(getattr(item, "time_msc", item.time * 1000)))
        entry_volume = sum(float(item.volume) for item in entries)
        exit_volume = sum(float(item.volume) for item in exits)
        closed = (
            bool(exits)
            and position_id not in open_position_ids
            and exit_volume + 1e-9 >= entry_volume
        )
        first = entries[0]
        broker_symbol = str(first.symbol)
        direction = (
            "BUY"
            if int(first.type) == int(mt5.DEAL_TYPE_BUY)
            else "SELL"
        )
        snapshots.append(
            {
                "message_id": message_id,
                "account": account.name,
                "symbol": reverse_symbols.get(broker_symbol, broker_symbol),
                "direction": direction,
                "volume": entry_volume,
                "open_time": _deal_time(first, server_offset_hours),
                "open_price": _weighted_price(entries),
                "close_time": _deal_time(exits[-1], server_offset_hours) if closed else None,
                "close_price": _weighted_price(exits) if closed else None,
                "commission": sum(
                    float(getattr(item, "commission", 0))
                    + float(getattr(item, "fee", 0))
                    for item in position_deals
                ),
                "swap": sum(
                    float(getattr(item, "swap", 0))
                    for item in position_deals
                ),
                "gross_pnl": (
                    sum(float(getattr(item, "profit", 0)) for item in position_deals)
                    if closed
                    else None
                ),
                "status": "CLOSED" if closed else "OPEN",
            }
        )
    return snapshots


def collect(payload: dict[str, Any]) -> list[dict[str, Any]]:
    account = AccountConfig(**payload["account"])
    magic = int(payload["magic_number"])
    lookback_days = int(payload["lookback_days"])
    server_offset_hours = float(payload.get("server_offset_hours", 3.0))
    import MetaTrader5 as mt5

    terminal_path = Path(account.terminal_path)
    if not terminal_path.is_file():
        raise FileNotFoundError(f"MT5 terminal not found: {terminal_path}")
    if not mt5.initialize(str(terminal_path), timeout=60_000):
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=lookback_days)
        end = now + timedelta(days=1)
        deals = mt5.history_deals_get(start, end)
        if deals is None:
            raise RuntimeError(f"history_deals_get failed: {mt5.last_error()}")
        positions = mt5.positions_get()
        if positions is None:
            raise RuntimeError(f"positions_get failed: {mt5.last_error()}")
        open_ids: set[int] = set()
        for position in positions:
            if int(getattr(position, "magic", 0)) != magic:
                continue
            open_ids.add(int(position.ticket))
            identifier = int(getattr(position, "identifier", 0))
            if identifier:
                open_ids.add(identifier)
        return build_snapshots(
            account=account,
            deals=deals,
            open_position_ids=open_ids,
            magic=magic,
            mt5=mt5,
            server_offset_hours=server_offset_hours,
        )
    finally:
        mt5.shutdown()


def main() -> None:
    try:
        snapshots = collect(json.loads(sys.stdin.read()))
        print(json.dumps({"snapshots": snapshots}, ensure_ascii=False))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
