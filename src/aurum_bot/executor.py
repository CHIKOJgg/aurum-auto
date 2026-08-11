from __future__ import annotations

import json
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
    payload = {
        "account": account.to_dict(),
        "signal": signal.to_dict(),
        "trading": asdict(trading),
    }
    if timing is not None:
        payload["timing"] = timing
    if strategy_state_dir is not None:
        payload["strategy_state_dir"] = str(strategy_state_dir)
    if reconcile:
        payload["reconcile"] = True
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "aurum_bot.mt5_worker"],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ExecutionResult(account.name, "failed", "MT5 worker timed out")

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        return ExecutionResult(
            account.name, "failed", f"MT5 worker exited {completed.returncode}: {detail}"
        )
    try:
        raw = json.loads(completed.stdout.strip())
        return ExecutionResult(**raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return ExecutionResult(
            account.name,
            "failed",
            f"invalid MT5 worker response: {exc}; {completed.stdout!r}",
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


def manage_exit_strategies(
    accounts: tuple[AccountConfig, ...],
    trading: TradingConfig,
    strategy_state_dir: Path,
) -> list[str]:
    errors: list[str] = []
    for account in accounts:
        if not account.enabled:
            continue
        payload = {
            "account": account.to_dict(),
            "deviation_points": trading.deviation_points,
            "strategy_state_dir": str(strategy_state_dir),
            "pending_timeout_minutes": trading.pending_timeout_minutes,
            "max_spread_points": trading.max_spread_points,
            "mt5_server_offset_hours": trading.mt5_server_offset_hours,
            "close_spread_hard_cap_minutes": trading.close_spread_hard_cap_minutes,
        }
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "aurum_bot.strategy_manager"],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=75,
                check=False,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"{account.name}: strategy_manager timed out after 75s")
            continue
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            errors.append(
                f"{account.name}: exit_code={completed.returncode} "
                f"detail={detail or '<empty>'} "
                f"stdout={completed.stdout.strip()[:200] or '<empty>'}"
            )
    return errors
