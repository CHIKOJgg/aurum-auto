from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from .config import TradingConfig
from .models import AccountConfig, ExecutionResult, Signal


def _run_account(
    account: AccountConfig,
    signal: Signal,
    trading: TradingConfig,
    timing: dict[str, int] | None = None,
    strategy_state_dir: Path | None = None,
    reconcile: bool = False,
) -> ExecutionResult:
    if not account.supports_symbol(signal.symbol):
        return ExecutionResult(
            account.name,
            "skipped_unsupported_symbol",
            f"{signal.symbol} is not enabled for this account",
        )
    trading_dict = asdict(trading)
    # frozenset is not JSON-serializable; convert to list for subprocess IPC.
    if isinstance(trading_dict.get("allowed_symbols"), frozenset):
        trading_dict["allowed_symbols"] = sorted(trading_dict["allowed_symbols"])
    payload = {
        "account": account.to_dict(),
        "signal": signal.to_dict(),
        "trading": trading_dict,
    }
    if timing is not None:
        payload["timing"] = timing
    if strategy_state_dir is not None:
        payload["strategy_state_dir"] = str(strategy_state_dir)
    if reconcile:
        payload["reconcile"] = True
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "aurum_bot.mt5_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = proc.communicate(
                input=json.dumps(payload),
                timeout=trading.execution_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return ExecutionResult(
                account.name, "failed",
                f"MT5 worker timed out after {trading.execution_timeout_seconds}s",
            )
    except OSError as exc:
        return ExecutionResult(
            account.name, "failed", f"MT5 worker failed to start: {exc}",
        )

    if proc.returncode != 0:
        detail = (stderr or stdout).strip()
        return ExecutionResult(
            account.name, "failed", f"MT5 worker exited {proc.returncode}: {detail}"
        )
    try:
        lines = [line.strip() for line in stdout.strip().splitlines() if line.strip()]
        last_json_line = next(line for line in reversed(lines) if line.startswith("{") and line.endswith("}"))
        raw = json.loads(last_json_line)
        return ExecutionResult(**raw)
    except (json.JSONDecodeError, TypeError, StopIteration) as exc:
        return ExecutionResult(
            account.name,
            "failed",
            f"invalid MT5 worker response: {exc}; {stdout!r}",
        )



def execute_for_accounts(
    signal: Signal,
    accounts: tuple[AccountConfig, ...],
    trading: TradingConfig,
    timing: dict[str, int] | None = None,
    strategy_state_dir: Path | None = None,
    reconcile: bool = False,
) -> list[ExecutionResult]:
    enabled = [account for account in accounts if account.enabled]
    if not enabled:
        return []

    results: list[ExecutionResult] = []
    with ThreadPoolExecutor(max_workers=len(enabled)) as pool:
        futures = {
            pool.submit(
                _run_account, account, signal, trading, timing, strategy_state_dir, reconcile
            ): account.name
            for account in enabled
        }
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda item: item.account)


def _manage_account_exit(
    account: AccountConfig,
    trading: TradingConfig,
    strategy_state_dir: Path,
) -> str | None:
    if not account.enabled:
        return None
    payload = {
        "account": account.to_dict(),
        "deviation_points": trading.deviation_points,
        "strategy_state_dir": str(strategy_state_dir),
        "pending_timeout_minutes": trading.pending_timeout_minutes,
        "max_spread_points": trading.max_spread_points,
        "symbol_max_spread_points": trading.symbol_max_spread_points,
        "mt5_server_offset_hours": trading.mt5_server_offset_hours,
        "server_time_mode": trading.server_time_mode,
        "close_spread_hard_cap_minutes": trading.close_spread_hard_cap_minutes,
        "pending_timeout_enabled": trading.pending_timeout_enabled,
        "exit_spread_guard_enabled": trading.exit_spread_guard_enabled,
    }
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "aurum_bot.strategy_manager"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        try:
            stdout, stderr = proc.communicate(
                input=json.dumps(payload),
                timeout=trading.execution_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return (
                f"{account.name}: strategy_manager timed out after "
                f"{trading.execution_timeout_seconds}s"
            )
    except OSError as exc:
        return f"{account.name}: strategy_manager failed to start: {exc}"

    if proc.returncode != 0:
        detail = (stderr or stdout).strip()
        return (
            f"{account.name}: exit_code={proc.returncode} "
            f"detail={detail or '<empty>'} "
            f"stdout={stdout.strip()[:200] or '<empty>'}"
        )
    return None


def manage_exit_strategies(
    accounts: tuple[AccountConfig, ...],
    trading: TradingConfig,
    strategy_state_dir: Path,
) -> list[str]:
    enabled = [account for account in accounts if account.enabled]
    if not enabled:
        return []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=len(enabled)) as pool:
        futures = [
            pool.submit(_manage_account_exit, account, trading, strategy_state_dir)
            for account in enabled
        ]
        for future in as_completed(futures):
            res = future.result()
            if res is not None:
                errors.append(res)
    return errors

