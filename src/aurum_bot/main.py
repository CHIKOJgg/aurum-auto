from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Any

from telethon import TelegramClient, events

from .config import AppConfig, load_config
from .executor import execute_for_accounts, manage_exit_strategies
from .exit_strategies import get_strategy
from .history_signals import (
    message_has_image,
    parse_historical_signal,
    upsert_signal_archive,
)
from .instance_lock import InstanceLock
from .journal_sync import collect_trade_snapshots
from .models import AccountConfig, ExecutionResult, Signal
from .parser import parse_signal
from .sheets_journal import SheetsTradeJournal
from .state import StateStore


LOGGER = logging.getLogger("aurum_bot")


@dataclass(frozen=True)
class JournalExecution:
    signal: Signal
    result: ExecutionResult
    account: AccountConfig
    risk_percent: float
    recorded_at: datetime


def _indicator_execution_enabled(has_image: bool, enable_indicator_2: bool) -> bool:
    """Индюк 1 is always enabled; Индюк 2 follows its YAML switch."""
    return has_image or enable_indicator_2


def configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    file_handler = RotatingFileHandler(
        path, maxBytes=1_048_576, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[file_handler, console_handler],
        force=True,
    )


async def resolve_target_channel(client: TelegramClient, config: AppConfig) -> Any:
    expected_id = abs(config.telegram.channel_id)
    async for dialog in client.iter_dialogs():
        entity_id = abs(int(getattr(dialog.entity, "id", 0)))
        if entity_id != expected_id:
            continue
        if dialog.name != config.telegram.channel_title:
            raise RuntimeError(
                f"Channel ID {expected_id} has unexpected title {dialog.name!r}; "
                f"expected {config.telegram.channel_title!r}"
            )
        return dialog.entity
    raise RuntimeError(
        f"Channel {config.telegram.channel_title!r} ({expected_id}) is not available "
        "to this Telegram account"
    )


async def latest_message_id(client: TelegramClient, channel: Any) -> int:
    messages = await client.get_messages(channel, limit=1)
    return int(messages[0].id) if messages else 0


async def handle_message(
    message: Any,
    state: StateStore,
    config: AppConfig,
    journal_queue: asyncio.Queue[JournalExecution] | None = None,
    received_at_ms: int | None = None,
    received_monotonic_ns: int | None = None,
    mt5_lock: asyncio.Lock | None = None,
    notification_queue: asyncio.Queue[str] | None = None,
) -> None:
    received_at_ms = received_at_ms or time.time_ns() // 1_000_000
    received_monotonic_ns = received_monotonic_ns or time.monotonic_ns()
    published_at_ms = int(message.date.timestamp() * 1000)
    message_id = int(message.id)
    # Claim durably before any external trading action. This is at-most-once by design.
    state.mark(message_id, "claimed")
    signal = parse_signal(
        message_id,
        message.raw_text,
        take_profit_target=get_strategy(config.trading.exit_strategy).target_number,
        allowed_symbols=config.trading.allowed_symbols,
        symbol_aliases=config.trading.symbol_aliases,
    )
    if signal is None:
        state.mark(message_id, "ignored_not_entry_call")
        LOGGER.info("Message %s ignored: not an allowed entry call", message_id)
        return
    if signal.take_profits is None:
        state.mark(message_id, "ignored_missing_tp_levels", signal=signal.to_dict())
        LOGGER.warning(
            "Signal %s ignored: exit strategy %s requires TP1-TP4",
            message_id,
            config.trading.exit_strategy,
        )
        return

    has_image = message_has_image(message)
    historical_signal = parse_historical_signal(
        message_id,
        message.raw_text,
        message.date,
        has_image=has_image,
    )
    if historical_signal is not None:
        archived = upsert_signal_archive(
            historical_signal,
            config.paths.calls_file,
        )
        LOGGER.info(
            "Signal %s saved to cumulative call archive (%s calls): %s",
            message_id,
            len(archived),
            config.paths.calls_file,
        )
    else:
        LOGGER.warning(
            "Signal %s is tradable but was not archived because TP1-TP4 "
            "could not all be parsed",
            message_id,
        )

    if not _indicator_execution_enabled(
        has_image,
        config.trading.enable_indicator_2,
    ):
        state.mark(
            message_id,
            "ignored_no_image",
            signal=signal.to_dict(),
        )
        LOGGER.info(
            "Signal %s archived as Индюк 2 but ignored: "
            "trading.enable_indicator_2 is false",
            message_id,
        )
        return

    indicator = "Индюк 1" if has_image else "Индюк 2"
    state.mark(message_id, "executing", signal=signal.to_dict())
    LOGGER.info(
        "Signal %s (%s): %s %s entry=%s SL=%s TP%s=%s strict_entry=%s",
        message_id,
        indicator,
        signal.symbol,
        signal.direction.value,
        signal.entry,
        signal.stop_loss,
        get_strategy(config.trading.exit_strategy).target_number,
        signal.take_profit,
        config.trading.strict_call_entry,
    )
    async def execute() -> list[ExecutionResult]:
        return await asyncio.to_thread(
            execute_for_accounts,
            signal,
            config.accounts,
            config.trading,
            {
                "published_at_ms": published_at_ms,
                "received_at_ms": received_at_ms,
                "received_monotonic_ns": received_monotonic_ns,
            },
            config.paths.strategy_state_dir,
        )
    if mt5_lock is None:
        results = await execute()
    else:
        async with mt5_lock:
            results = await execute()
    if not results:
        state.mark(
            message_id,
            "no_enabled_accounts",
            signal=signal.to_dict(),
            account_results={},
        )
        LOGGER.warning("Signal %s has no enabled MT5 accounts", message_id)
        return

    result_map = {result.account: result.to_dict() for result in results}
    final_status = (
        "completed"
        if all(result.status != "failed" for result in results)
        else "completed_with_failures"
    )
    state.mark(
        message_id,
        final_status,
        signal=signal.to_dict(),
        account_results=result_map,
    )
    for result in results:
        LOGGER.info(
            "Signal %s account=%s status=%s ticket=%s volume=%s kind=%s "
            "publish_to_receive_ms=%s publish_to_confirm_ms=%s "
            "receive_to_confirm_ms=%s "
            "order_send_rtt_ms=%s detail=%s",
            message_id,
            result.account,
            result.status,
            result.ticket,
            result.volume,
            result.execution_kind,
            result.publication_to_receive_ms,
            result.publication_to_confirmation_ms,
            result.receive_to_confirmation_ms,
            result.order_send_round_trip_ms,
            result.detail,
        )
        if (
            journal_queue is not None
            and result.account == config.google_sheets.account
            and result.status == "executed"
        ):
            account = next(
                item for item in config.accounts if item.name == result.account
            )
            journal_queue.put_nowait(
                JournalExecution(
                    signal=signal,
                    result=result,
                    account=account,
                    risk_percent=config.trading.risk_percent,
                    recorded_at=datetime.now().astimezone(),
                )
            )
    if notification_queue is not None:
        for result in results:
            notification_queue.put_nowait(_notification_text(message_id, signal, result))


def _notification_text(message_id: int, signal: Signal, result: ExecutionResult) -> str:
    if result.status.startswith("executed"):
        return f"♻️ {message_id} {signal.symbol} {signal.direction.value} executed lot={result.volume:g}" if result.volume is not None else f"♻️ {message_id} {signal.symbol} {signal.direction.value} executed"
    if result.status.startswith("skipped"):
        return f"⛔ {message_id} {result.status}: {result.detail}"
    return f"❌ {message_id} {result.status}: {result.detail}"


async def _notification_worker(
    client: TelegramClient, queue: asyncio.Queue[str], retry_count: int
) -> None:
    while True:
        text = await queue.get()
        try:
            for attempt in range(retry_count + 1):
                try:
                    await client.send_message("me", text)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if attempt >= retry_count:
                        LOGGER.exception("Telegram notification failed")
                    else:
                        LOGGER.warning("Telegram notification failed; retrying once")
        finally:
            queue.task_done()


async def _journal_write_worker(
    queue: asyncio.Queue[JournalExecution],
    journal: SheetsTradeJournal,
) -> None:
    while True:
        event = await queue.get()
        try:
            await asyncio.to_thread(
                journal.record_execution,
                event.signal,
                event.result,
                risk_base_usd=event.account.risk_base_usd,
                risk_percent=event.risk_percent,
                recorded_at=event.recorded_at,
            )
            LOGGER.info(
                "Google Sheets journal recorded signal %s for %s",
                event.signal.message_id,
                event.result.account,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                "Google Sheets execution write failed; trading result is unaffected"
            )
        finally:
            queue.task_done()


async def _journal_sync_loop(
    journal: SheetsTradeJournal,
    config: AppConfig,
) -> None:
    account = next(
        item
        for item in config.accounts
        if item.name == config.google_sheets.account
    )
    while True:
        try:
            snapshots = await asyncio.to_thread(
                collect_trade_snapshots,
                account,
                magic_number=config.trading.magic_number,
                lookback_days=config.google_sheets.history_lookback_days,
            )
            updated = await asyncio.to_thread(
                journal.upsert_snapshots,
                snapshots,
            )
            if updated:
                LOGGER.info(
                    "Google Sheets journal synchronized %s MT5 trade(s)",
                    updated,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception(
                "Google Sheets/MT5 history sync failed; trading is unaffected"
            )
        await asyncio.sleep(config.google_sheets.sync_interval_seconds)


def _any_active_plans(accounts: tuple[AccountConfig, ...], strategy_state_dir: Path) -> bool:
    for account in accounts:
        if not account.enabled:
            continue
        path = strategy_state_dir / account.name
        if path.is_dir():
            for plan_path in path.glob("*.json"):
                try:
                    if json.loads(plan_path.read_text(encoding="utf-8")).get("status") == "active":
                        return True
                except (OSError, json.JSONDecodeError):
                    LOGGER.warning("Ignoring unreadable strategy plan: %s", plan_path)
    return False


async def _status_writer(config: AppConfig, started_at: float) -> None:
    """Writes runtime/status.json every 30s so external monitors can check health."""
    status = config.paths.lock_file.parent / "status.json"
    pid = os.getpid()
    while True:
        try:
            payload = {
                "pid": pid,
                "started_at": datetime.fromtimestamp(started_at).isoformat(),
                "updated_at": datetime.now().isoformat(),
                "accounts": {
                    acc.name: acc.enabled for acc in config.accounts
                },
            }
            status.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            LOGGER.warning("status.json write failed", exc_info=True)
        await asyncio.sleep(config.runtime.status_write_interval_seconds)


async def run(config: AppConfig) -> None:
    config.telegram.session_file.parent.mkdir(parents=True, exist_ok=True)
    state = StateStore(config.paths.state_file, config.telegram.channel_id)
    state.load()
    mt5_lock = asyncio.Lock()
    LOGGER.info("State file for channel %s: %s", config.telegram.channel_id, state.path)
    journal: SheetsTradeJournal | None = None
    journal_queue: asyncio.Queue[JournalExecution] | None = None
    journal_tasks: list[asyncio.Task[None]] = []
    async def strategy_manager_loop() -> None:
        while True:
            try:
                if not await asyncio.to_thread(_any_active_plans, config.accounts, config.paths.strategy_state_dir):
                    await asyncio.sleep(config.runtime.exit_strategy_poll_seconds)
                    continue
                async with mt5_lock:
                    errors = await asyncio.to_thread(
                        manage_exit_strategies,
                        config.accounts,
                        config.trading,
                        config.paths.strategy_state_dir,
                    )
                for error in errors:
                    LOGGER.error("Exit strategy manager: %s", error)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Exit strategy manager failed")
            await asyncio.sleep(config.runtime.exit_strategy_poll_seconds)
    if config.runtime.exit_strategy_manager_enabled:
        journal_tasks.append(asyncio.create_task(strategy_manager_loop()))
    started_at = time.time()
    if config.runtime.status_writer_enabled:
        journal_tasks.append(asyncio.create_task(_status_writer(config, started_at)))
    if config.google_sheets.enabled:
        candidate = SheetsTradeJournal(config.google_sheets)
        try:
            if config.google_sheets.auto_setup:
                await asyncio.to_thread(candidate.ensure_template)
            else:
                await asyncio.to_thread(candidate.check_access)
        except Exception:
            LOGGER.exception(
                "Google Sheets journal is unavailable; bot will continue trading "
                "without sheet writes"
            )
        else:
            journal = candidate
            journal_queue = asyncio.Queue()
            journal_tasks.extend([
                asyncio.create_task(
                    _journal_write_worker(journal_queue, journal)
                ),
                asyncio.create_task(_journal_sync_loop(journal, config)),
            ])
            LOGGER.info(
                "Google Sheets journal enabled for account %s",
                config.google_sheets.account,
            )

    client = TelegramClient(
        str(config.telegram.session_file),
        config.telegram.api_id,
        config.telegram.api_hash,
        receive_updates=True,
        catch_up=False,
        auto_reconnect=True,
        request_retries=3,
        connection_retries=3,
        flood_sleep_threshold=60,
    )
    # Prevent the bot from hanging in headless mode when Telegram needs
    # interactive authentication (2FA code, phone verification, etc.).
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Telegram session is not authorized. Run the bot interactively "
            "once to complete login, then restart in headless mode."
        )
    notification_queue: asyncio.Queue[str] | None = None
    notification_task: asyncio.Task[None] | None = None
    if config.telegram.notifications_enabled:
        notification_queue = asyncio.Queue()
        notification_task = asyncio.create_task(
            _notification_worker(
                client, notification_queue, config.telegram.notification_retry_count
            )
        )
    try:
        # Re-run only durable, interrupted executions. The MT5 worker uses the
        # AURUM:<message_id> magic/comment pair to make this idempotent.
        unfinished = state.unfinished_messages() if config.runtime.reconcile_on_startup else []
        for message_id, record in unfinished:
            raw_signal = record.get("signal")
            if not isinstance(raw_signal, dict):
                state.mark(message_id, "failed", account_results={})
                LOGGER.error("Cannot reconcile signal %s: state has no signal", message_id)
                continue
            try:
                signal = Signal.from_dict(raw_signal)
            except (KeyError, TypeError, ValueError) as exc:
                state.mark(message_id, "failed", account_results={})
                LOGGER.error("Cannot reconcile signal %s: invalid state signal: %s", message_id, exc)
                continue
            async with mt5_lock:
                results = await asyncio.to_thread(
                    execute_for_accounts, signal, config.accounts, config.trading,
                    None, config.paths.strategy_state_dir, True,
                )
            result_map = {result.account: result.to_dict() for result in results}
            status = "completed" if results and all(item.status != "failed" for item in results) else "completed_with_failures"
            state.mark(message_id, status, signal=signal.to_dict(), account_results=result_map)
            if notification_queue is not None:
                for result in results:
                    notification_queue.put_nowait(_notification_text(message_id, signal, result))
            LOGGER.info("Reconciled interrupted signal %s: %s", message_id, status)
        channel = await resolve_target_channel(client, config)
        cutoff = await latest_message_id(client, channel)
        state.initialize_cutoff(cutoff)
        LOGGER.info(
            "Startup cutoff is message %s. Older/unseen downtime messages will not trade.",
            cutoff,
        )

        queue: asyncio.Queue[Any] = asyncio.Queue()

        @client.on(events.NewMessage(chats=channel))
        async def on_new_message(event: Any) -> None:
            # Keep the update handler very small. Trading is serialized by the
            # consumer, while new Telegram updates continue entering the queue.
            await queue.put(
                (event.message, time.time_ns() // 1_000_000, time.monotonic_ns())
            )

        # Close the tiny race between reading the startup cutoff and registering
        # the event handler. Messages are fetched once; event duplicates are
        # discarded by message_id below.
        raced_messages = [
            item
            async for item in client.iter_messages(
                channel,
                min_id=cutoff,
                reverse=True,
            )
        ]
        for message in raced_messages:
            await queue.put(
                (message, time.time_ns() // 1_000_000, time.monotonic_ns())
            )

        async def consume_messages() -> None:
            while True:
                try:
                    message, received_at_ms, received_monotonic_ns = await queue.get()
                    if int(message.id) <= state.last_seen_message_id:
                        continue
                    await handle_message(
                        message,
                        state,
                        config,
                        journal_queue,
                        received_at_ms,
                        received_monotonic_ns,
                        mt5_lock,
                        notification_queue,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("Telegram NewMessage processing failed")

        LOGGER.info("Real-time Telegram NewMessage listener is active")
        consumer = asyncio.create_task(consume_messages())
        try:
            await client.run_until_disconnected()
        finally:
            consumer.cancel()
            with suppress(asyncio.CancelledError):
                await consumer
    finally:
        if notification_task is not None:
            notification_task.cancel()
            with suppress(asyncio.CancelledError):
                await notification_task
        for task in journal_tasks:
            task.cancel()
        for task in journal_tasks:
            with suppress(asyncio.CancelledError):
                await task
        await client.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aurum Telegram -> MT5 executor")
    parser.add_argument("--config", default="config.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        config = load_config(args.config)
        configure_logging(config.paths.log_file)
        enabled = [account.name for account in config.accounts if account.enabled]
        LOGGER.info("Enabled MT5 accounts: %s", ", ".join(enabled) or "none")
        with InstanceLock(config.paths.lock_file):
            asyncio.run(run(config))
    except KeyboardInterrupt:
        LOGGER.info("Stopped by user")
    except Exception as exc:
        # Fallback logger if configure_logging() was not reached.
        logging.basicConfig(level=logging.ERROR)
        LOGGER.exception("Fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
