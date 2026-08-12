import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from aurum_bot.config import TradingConfig
from aurum_bot.executor import (
    _run_account,
    execute_for_accounts,
    manage_exit_strategies,
)
from aurum_bot.models import AccountConfig, Signal, ExecutionResult, Direction


@pytest.fixture
def sample_account():
    return AccountConfig(
        name="test_acc",
        risk_base_usd=100.0,
        terminal_path="C:/MT5/terminal64.exe",
        symbols={"EURUSD": "EURUSD"},
        enabled=True,
    )


@pytest.fixture
def sample_signal():
    return Signal(
        message_id=1,
        symbol="EURUSD",
        direction=Direction.LONG,
        entry=1.1,
        stop_loss=1.0,
        take_profit=1.2,
    )


@pytest.fixture
def sample_trading():
    return TradingConfig(
        risk_multiplier=1.0,
        min_market_risk_multiplier=1.0,
        max_market_risk_multiplier=1.0,
        lot_step=0.01,
        deviation_points=10,
        send_attempts=3,
        retry_delay_seconds=1.0,
        magic_number=777,
        execution_timeout_seconds=5,
        allowed_symbols=frozenset(["EURUSD"])
    )


def test_run_account_unsupported_symbol(sample_account, sample_signal, sample_trading):
    sample_signal_unsupported = Signal(
        message_id=1,
        symbol="DE40",
        direction=Direction.LONG,
        entry=1.1,
        stop_loss=1.0,
        take_profit=1.2,
    )
    result = _run_account(sample_account, sample_signal_unsupported, sample_trading)
    assert result.status == "skipped_unsupported_symbol"


@patch("subprocess.Popen")
def test_run_account_environment_inheritance(mock_popen, sample_account, sample_signal, sample_trading):
    mock_proc = Mock()
    mock_proc.communicate.return_value = (json.dumps({"account": "test_acc", "status": "success", "detail": "ok"}), "")
    mock_proc.returncode = 0
    mock_popen.return_value = mock_proc

    _run_account(sample_account, sample_signal, sample_trading, timing={"start": 123}, strategy_state_dir=Path("/tmp"), reconcile=True)

    # 1. Environment Inheritance: Verify that env=os.environ.copy() is passed
    assert mock_popen.call_count == 1
    kwargs = mock_popen.call_args.kwargs
    assert "env" in kwargs
    assert kwargs["env"] == os.environ.copy()

    # 3. IPC Serialization: payload correctly written via communicate
    communicate_kwargs = mock_proc.communicate.call_args.kwargs
    payload = json.loads(communicate_kwargs["input"])
    assert payload["account"]["name"] == "test_acc"
    assert payload["signal"]["symbol"] == "EURUSD"
    assert "timing" in payload
    assert "strategy_state_dir" in payload
    assert payload["reconcile"] is True


@patch("subprocess.Popen")
def test_run_account_timeout(mock_popen, sample_account, sample_signal, sample_trading):
    mock_proc = Mock()
    mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="cmd", timeout=5)
    mock_popen.return_value = mock_proc

    result = _run_account(sample_account, sample_signal, sample_trading)
    
    # 2. Timeout Handling: Verify explicitly killed and error returned
    mock_proc.kill.assert_called_once()
    mock_proc.wait.assert_called_once()
    assert result.status == "failed"
    assert "timed out" in result.detail


@patch("subprocess.Popen")
def test_run_account_os_error(mock_popen, sample_account, sample_signal, sample_trading):
    mock_popen.side_effect = OSError("mock os error")
    result = _run_account(sample_account, sample_signal, sample_trading)
    assert result.status == "failed"
    assert "mock os error" in result.detail


@patch("subprocess.Popen")
def test_run_account_nonzero_returncode(mock_popen, sample_account, sample_signal, sample_trading):
    mock_proc = Mock()
    mock_proc.communicate.return_value = ("stdout_msg", "stderr_msg")
    mock_proc.returncode = 1
    mock_popen.return_value = mock_proc

    result = _run_account(sample_account, sample_signal, sample_trading)
    assert result.status == "failed"
    assert "exited 1" in result.detail


@patch("subprocess.Popen")
def test_run_account_invalid_json(mock_popen, sample_account, sample_signal, sample_trading):
    mock_proc = Mock()
    mock_proc.communicate.return_value = ("invalid json", "")
    mock_proc.returncode = 0
    mock_popen.return_value = mock_proc

    result = _run_account(sample_account, sample_signal, sample_trading)
    assert result.status == "failed"
    assert "invalid MT5 worker response" in result.detail


def test_execute_for_accounts(sample_account, sample_signal, sample_trading):
    disabled_account = AccountConfig(
        name="disabled_acc",
        risk_base_usd=100.0,
        terminal_path="C:/MT5/terminal64.exe",
        symbols={"EURUSD": "EURUSD"},
        enabled=False,
    )
    with patch("aurum_bot.executor._run_account") as mock_run:
        mock_run.return_value = ExecutionResult("test_acc", "success", "ok")
        results = execute_for_accounts(sample_signal, (sample_account, disabled_account), sample_trading)
        
        assert len(results) == 1
        assert results[0].account == "test_acc"
        mock_run.assert_called_once()


def test_execute_for_accounts_none_enabled(sample_signal, sample_trading):
    disabled_account = AccountConfig(
        name="disabled_acc",
        risk_base_usd=100.0,
        terminal_path="C:/MT5/terminal64.exe",
        symbols={"EURUSD": "EURUSD"},
        enabled=False,
    )
    results = execute_for_accounts(sample_signal, (disabled_account,), sample_trading)
    assert len(results) == 0


@patch("subprocess.Popen")
def test_manage_exit_strategies_timeout(mock_popen, sample_account, sample_trading):
    mock_proc = Mock()
    mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="cmd", timeout=5)
    mock_popen.return_value = mock_proc

    errors = manage_exit_strategies((sample_account,), sample_trading, Path("/tmp"))
    
    mock_proc.kill.assert_called_once()
    mock_proc.wait.assert_called_once()
    assert len(errors) == 1
    assert "timed out" in errors[0]


@patch("subprocess.Popen")
def test_manage_exit_strategies_success(mock_popen, sample_account, sample_trading):
    mock_proc = Mock()
    mock_proc.communicate.return_value = ("{}", "")
    mock_proc.returncode = 0
    mock_popen.return_value = mock_proc

    errors = manage_exit_strategies((sample_account,), sample_trading, Path("/tmp"))
    assert len(errors) == 0
    
    kwargs = mock_popen.call_args.kwargs
    assert "env" in kwargs
    assert kwargs["env"] == os.environ.copy()


@patch("subprocess.Popen")
def test_manage_exit_strategies_oserror(mock_popen, sample_account, sample_trading):
    mock_popen.side_effect = OSError("mock os error")
    errors = manage_exit_strategies((sample_account,), sample_trading, Path("/tmp"))
    assert len(errors) == 1
    assert "mock os error" in errors[0]


@patch("subprocess.Popen")
def test_manage_exit_strategies_nonzero_returncode(mock_popen, sample_account, sample_trading):
    mock_proc = Mock()
    mock_proc.communicate.return_value = ("stdout_msg", "stderr_msg")
    mock_proc.returncode = 1
    mock_popen.return_value = mock_proc

    errors = manage_exit_strategies((sample_account,), sample_trading, Path("/tmp"))
    assert len(errors) == 1
    assert "exit_code=1" in errors[0]


def test_manage_exit_strategies_disabled_account(sample_trading):
    disabled_account = AccountConfig(
        name="disabled_acc",
        risk_base_usd=100.0,
        terminal_path="C:/MT5/terminal64.exe",
        symbols={"EURUSD": "EURUSD"},
        enabled=False,
    )
    errors = manage_exit_strategies((disabled_account,), sample_trading, Path("/tmp"))
    assert len(errors) == 0
