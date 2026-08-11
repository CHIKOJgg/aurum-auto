from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .exit_strategies import get_strategy
from .models import AccountConfig


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int
    api_hash: str
    phone: str
    channel_id: int
    channel_title: str
    poll_interval_seconds: int
    session_file: Path
    notifications_enabled: bool
    notification_retry_count: int


@dataclass(frozen=True)
class TradingConfig:
    risk_percent: float
    min_market_risk_percent: float
    max_market_risk_percent: float
    lot_step: float
    deviation_points: int
    send_attempts: int
    retry_delay_seconds: float
    magic_number: int
    take_profit_target: int = 2
    exit_strategy: str = "sl_tp2"
    strict_call_entry: bool = True
    enable_indicator_2: bool = False
    market_entry_tolerance_r: float = 0.0
    pending_timeout_minutes: float = 0.0
    max_spread_points: int = 0
    news_events_file: str = ""
    news_window_before_minutes: float = 0.0
    news_window_after_minutes: float = 0.0
    mt5_server_offset_hours: float = 3.0
    close_spread_hard_cap_minutes: float = 10.0
    execution_timeout_seconds: int = 90


@dataclass(frozen=True)
class RuntimeConfig:
    reconcile_on_startup: bool
    exit_strategy_manager_enabled: bool
    exit_strategy_poll_seconds: float
    status_writer_enabled: bool
    status_write_interval_seconds: float


@dataclass(frozen=True)
class PathsConfig:
    state_file: Path
    log_file: Path
    lock_file: Path
    calls_file: Path
    strategy_state_dir: Path


@dataclass(frozen=True)
class GoogleSheetsConfig:
    enabled: bool
    spreadsheet_id: str
    credentials_file: Path
    account: str
    sync_interval_seconds: int
    history_lookback_days: int
    auto_setup: bool = True


@dataclass(frozen=True)
class AppConfig:
    root: Path
    telegram: TelegramConfig
    trading: TradingConfig
    accounts: tuple[AccountConfig, ...]
    paths: PathsConfig
    google_sheets: GoogleSheetsConfig
    runtime: RuntimeConfig


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value == "replace_me":
        raise ValueError(f"Environment variable {name} is not configured")
    return value


def _mapping(data: Any, name: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be a mapping")
    return data


def _required(mapping: dict[str, Any], key: str, section: str) -> Any:
    if key not in mapping:
        raise ValueError(f"{section}.{key} must be explicitly configured in YAML")
    return mapping[key]


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path).resolve()
    root = path.parent
    load_dotenv(root / ".env")
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Copy config.example.yaml to config.yaml first."
        )

    try:
        raw_yaml = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(
            "config.yaml is not valid YAML. For Windows paths use forward slashes "
            '(example: "C:/MT5/terminal64.exe") or single quotes.'
        ) from exc
    raw = _mapping(raw_yaml, "config")
    telegram_raw = _mapping(raw.get("telegram"), "telegram")
    trading_raw = _mapping(raw.get("trading"), "trading")
    paths_raw = _mapping(raw.get("paths"), "paths")
    runtime_raw = _mapping(raw.get("runtime"), "runtime")

    api_id_text = _required_env("TELEGRAM_API_ID")
    try:
        api_id = int(api_id_text)
    except ValueError as exc:
        raise ValueError("TELEGRAM_API_ID must be an integer") from exc

    telegram = TelegramConfig(
        api_id=api_id,
        api_hash=_required_env("TELEGRAM_API_HASH"),
        phone=_required_env("TELEGRAM_PHONE"),
        channel_id=int(telegram_raw["channel_id"]),
        channel_title=str(telegram_raw["channel_title"]),
        poll_interval_seconds=max(20, int(telegram_raw["poll_interval_seconds"])),
        session_file=_resolve(root, str(telegram_raw["session_file"])),
        notifications_enabled=bool(_required(telegram_raw, "notifications_enabled", "telegram")),
        notification_retry_count=max(0, int(_required(telegram_raw, "notification_retry_count", "telegram"))),
    )
    trading = TradingConfig(
        risk_percent=float(_required(trading_raw, "risk_percent", "trading")),
        min_market_risk_percent=float(_required(trading_raw, "min_market_risk_percent", "trading")),
        max_market_risk_percent=float(_required(trading_raw, "max_market_risk_percent", "trading")),
        lot_step=float(_required(trading_raw, "lot_step", "trading")),
        deviation_points=int(_required(trading_raw, "deviation_points", "trading")),
        send_attempts=max(1, int(_required(trading_raw, "send_attempts", "trading"))),
        retry_delay_seconds=max(0.0, float(_required(trading_raw, "retry_delay_seconds", "trading"))),
        magic_number=int(_required(trading_raw, "magic_number", "trading")),
        take_profit_target=int(_required(trading_raw, "take_profit_target", "trading")),
        exit_strategy=str(_required(trading_raw, "exit_strategy", "trading")).strip(),
        strict_call_entry=bool(_required(trading_raw, "strict_call_entry", "trading")),
        enable_indicator_2=bool(_required(trading_raw, "enable_indicator_2", "trading")),
        market_entry_tolerance_r=float(_required(trading_raw, "market_entry_tolerance_r", "trading")),
        pending_timeout_minutes=float(_required(trading_raw, "pending_timeout_minutes", "trading")),
        max_spread_points=int(_required(trading_raw, "max_spread_points", "trading")),
        news_events_file=str(_required(trading_raw, "news_events_file", "trading")).strip(),
        news_window_before_minutes=float(_required(trading_raw, "news_window_before_minutes", "trading")),
        news_window_after_minutes=float(_required(trading_raw, "news_window_after_minutes", "trading")),
        mt5_server_offset_hours=float(_required(trading_raw, "mt5_server_offset_hours", "trading")),
        close_spread_hard_cap_minutes=float(_required(trading_raw, "close_spread_hard_cap_minutes", "trading")),
        execution_timeout_seconds=max(1, int(_required(trading_raw, "execution_timeout_seconds", "trading"))),
    )
    if not 0 < trading.risk_percent <= 100:
        raise ValueError("trading.risk_percent must be > 0 and <= 100")
    if trading.min_market_risk_percent != 0.9:
        raise ValueError(
            "This strategy is locked to the agreed min_market_risk_percent: 0.9"
        )
    if trading.max_market_risk_percent != 1.1:
        raise ValueError(
            "This strategy is locked to the agreed max_market_risk_percent: 1.1"
        )
    if trading.lot_step != 0.01:
        raise ValueError("This strategy is locked to the agreed lot_step: 0.01")
    if trading.market_entry_tolerance_r < 0:
        raise ValueError("trading.market_entry_tolerance_r must be >= 0")
    if trading.pending_timeout_minutes < 0:
        raise ValueError("trading.pending_timeout_minutes must be >= 0")
    if trading.max_spread_points < 0:
        raise ValueError("trading.max_spread_points must be >= 0")
    if trading.news_window_before_minutes < 0:
        raise ValueError("trading.news_window_before_minutes must be >= 0")
    if trading.news_window_after_minutes < 0:
        raise ValueError("trading.news_window_after_minutes must be >= 0")
    if trading.close_spread_hard_cap_minutes < 0:
        raise ValueError("trading.close_spread_hard_cap_minutes must be >= 0")
    if trading.take_profit_target not in {1, 2, 3, 4}:
        raise ValueError("trading.take_profit_target must be an integer from 1 to 4")
    get_strategy(trading.exit_strategy)
    runtime = RuntimeConfig(
        reconcile_on_startup=bool(_required(runtime_raw, "reconcile_on_startup", "runtime")),
        exit_strategy_manager_enabled=bool(_required(runtime_raw, "exit_strategy_manager_enabled", "runtime")),
        exit_strategy_poll_seconds=max(0.1, float(_required(runtime_raw, "exit_strategy_poll_seconds", "runtime"))),
        status_writer_enabled=bool(_required(runtime_raw, "status_writer_enabled", "runtime")),
        status_write_interval_seconds=max(1.0, float(_required(runtime_raw, "status_write_interval_seconds", "runtime"))),
    )

    accounts_raw = raw.get("accounts")
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise ValueError("accounts must contain at least one account")

    accounts: list[AccountConfig] = []
    names: set[str] = set()
    for index, item in enumerate(accounts_raw):
        account_raw = _mapping(item, f"accounts[{index}]")
        name = str(account_raw["name"]).strip()
        if not name or name in names:
            raise ValueError(f"Account name must be non-empty and unique: {name!r}")
        names.add(name)
        symbols_raw = _mapping(account_raw["symbols"], f"accounts[{index}].symbols")
        symbols = {
            str(k).upper(): str(v).strip() for k, v in symbols_raw.items()
        }
        if not {"XAUUSD", "DE40"}.issubset(symbols):
            raise ValueError(f"{name}: symbols must include XAUUSD and DE40 mappings")
        if any(not broker_symbol for broker_symbol in symbols.values()):
            raise ValueError(f"{name}: broker symbol names must be non-empty")
        fixed_commission_raw = _mapping(
            account_raw.get("commission_per_lot_usd", {}),
            f"accounts[{index}].commission_per_lot_usd",
        )
        rate_commission_raw = _mapping(
            account_raw.get("commission_rate_percent", {}),
            f"accounts[{index}].commission_rate_percent",
        )
        fixed_commission = {
            str(k).upper(): float(v) for k, v in fixed_commission_raw.items()
        }
        rate_commission = {
            str(k).upper(): float(v) for k, v in rate_commission_raw.items()
        }
        if any(value < 0 for value in fixed_commission.values()):
            raise ValueError(f"{name}: commission_per_lot_usd cannot be negative")
        if any(value < 0 for value in rate_commission.values()):
            raise ValueError(f"{name}: commission_rate_percent cannot be negative")
        risk_base = float(account_raw["risk_base_usd"])
        if risk_base <= 0:
            raise ValueError(f"{name}: risk_base_usd must be positive")
        accounts.append(
            AccountConfig(
                name=name,
                enabled=bool(account_raw.get("enabled", False)),
                risk_base_usd=risk_base,
                terminal_path=str(account_raw["terminal_path"]),
                symbols=symbols,
                commission_per_lot_usd=fixed_commission,
                commission_rate_percent=rate_commission,
            )
        )

    paths = PathsConfig(
        state_file=_resolve(root, str(paths_raw["state_file"])),
        log_file=_resolve(root, str(paths_raw["log_file"])),
        lock_file=_resolve(root, str(paths_raw["lock_file"])),
        calls_file=_resolve(
            root,
            str(paths_raw.get("calls_file", "data/all_calls.json")),
        ),
        strategy_state_dir=_resolve(
            root,
            str(paths_raw.get("strategy_state_dir", "runtime/exit-strategies")),
        ),
    )
    sheets_raw = raw.get("google_sheets", {})
    if sheets_raw is None:
        sheets_raw = {}
    sheets_raw = _mapping(sheets_raw, "google_sheets")
    google_sheets = GoogleSheetsConfig(
        enabled=bool(sheets_raw.get("enabled", False)),
        spreadsheet_id=str(sheets_raw.get("spreadsheet_id", "")).strip(),
        credentials_file=_resolve(
            root,
            str(
                sheets_raw.get(
                    "credentials_file",
                    "runtime/google-service-account.json",
                )
            ),
        ),
        account=str(sheets_raw.get("account", "fxpro_demo510")).strip(),
        sync_interval_seconds=max(
            30,
            int(sheets_raw.get("sync_interval_seconds", 120)),
        ),
        history_lookback_days=max(
            7,
            int(sheets_raw.get("history_lookback_days", 45)),
        ),
        auto_setup=bool(sheets_raw.get("auto_setup", True)),
    )
    if google_sheets.enabled:
        if not google_sheets.spreadsheet_id:
            raise ValueError(
                "google_sheets.spreadsheet_id is required when integration is enabled"
            )
        if google_sheets.account not in names:
            raise ValueError(
                f"google_sheets.account does not match a configured account: "
                f"{google_sheets.account!r}"
            )
    return AppConfig(
        root=root,
        telegram=telegram,
        trading=trading,
        accounts=tuple(accounts),
        paths=paths,
        google_sheets=google_sheets,
        runtime=runtime,
    )
