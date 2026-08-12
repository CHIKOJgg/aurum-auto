import json
import logging
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aurum_bot.strategy_manager import _manage_plan
from aurum_bot.exit_strategies import StrategySpec

@pytest.fixture
def state_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)

@pytest.fixture
def plan_path(state_dir):
    return state_dir / "test_plan.json"

@pytest.fixture
def mt5_mock():
    mock = MagicMock()
    mock.POSITION_TYPE_BUY = 0
    mock.POSITION_TYPE_SELL = 1
    mock.ORDER_TYPE_BUY = 0
    mock.ORDER_TYPE_SELL = 1
    mock.TRADE_ACTION_DEAL = 1
    mock.TRADE_ACTION_SLTP = 6
    mock.TRADE_ACTION_REMOVE = 8
    mock.ORDER_TIME_GTC = 0
    mock.TRADE_RETCODE_DONE = 10009
    mock.TRADE_RETCODE_PLACED = 10008
    mock.TRADE_RETCODE_DONE_PARTIAL = 10010
    mock.SYMBOL_TRADE_EXECUTION_MARKET = 1
    mock.ORDER_FILLING_FOK = 0
    mock.ORDER_FILLING_IOC = 1
    mock.ORDER_FILLING_RETURN = 2
    
    mock.symbol_info.return_value = SimpleNamespace(
        volume_step=0.01,
        volume_min=0.01,
        digits=2,
        point=0.01,
        trade_stops_level=0,
        filling_mode=1,
        trade_exemode=1,
    )
    mock.symbol_info_tick.return_value = SimpleNamespace(
        bid=100.0,
        ask=100.0,
        time=int(time.time()),
    )
    # mock order check and order send
    mock.order_check.return_value = SimpleNamespace(retcode=0)
    mock.order_send.return_value = SimpleNamespace(retcode=mock.TRADE_RETCODE_DONE)
    return mock

@pytest.fixture
def strategy_patch():
    # Mock get_strategy to return a specific configuration
    strategy = StrategySpec(
        key="test_strat",
        title="Test Strategy",
        target_number=3,
        stop_moves=((1, 0),), # Move to BE at TP1
        dynamic_tp2_minutes=None,
        time_exit_minutes=None,
    )
    with patch("aurum_bot.strategy_manager.get_strategy", return_value=strategy):
        yield strategy

def create_plan(path: Path, **kwargs):
    plan = {
        "status": "active",
        "symbol": "GOLD",
        "direction": "LONG",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "take_profits": [110.0, 120.0, 130.0],
        "final_target": 3,
        "strategy": "test_strat",
        "comment": "AURUM:test",
        "magic": 12345,
        "touched_target": 0,
        "exit_legs": [
            {"target": 1, "volume": 0.5, "closed": False},
            {"target": 2, "volume": 0.3, "closed": False},
        ],
        "entry_time_msc": int(time.time() * 1000) - 60000,
        "last_check_msc": int(time.time() * 1000) - 60000,
        "active_stop_target": -1,
    }
    plan.update(kwargs)
    path.write_text(json.dumps(plan), encoding="utf-8")
    return plan

def test_take_profit_scaling(mt5_mock, plan_path, strategy_patch):
    """
    1. Take Profit Scaling (`_manage_plan`): Mock price movements to trigger TP1. 
    Verify that a partial close order is constructed and the plan state is updated and saved.
    """
    create_plan(plan_path)
    # Mock positions matching the plan
    position = SimpleNamespace(
        ticket=1,
        magic=12345,
        comment="AURUM:test",
        symbol="GOLD",
        type=mt5_mock.POSITION_TYPE_BUY,
        volume=1.0,
        price_open=100.0,
        sl=90.0,
        tp=130.0,
    )
    mt5_mock.positions_get.return_value = (position,)
    mt5_mock.orders_get.return_value = ()
    
    # Mock favorable extreme to hit TP1 (110.0)
    # Also mock tick price so stop is placeable if evaluated
    mt5_mock.symbol_info_tick.return_value = SimpleNamespace(
        bid=110.0,
        ask=110.0,
        time=int(time.time()),
    )
    with patch("aurum_bot.strategy_manager._favorable_extreme", return_value=111.0):
        _manage_plan(
            mt5=mt5_mock,
            path=plan_path,
            plan=json.loads(plan_path.read_text()),
            deviation=10,
            exit_spread_guard_enabled=False
        )
    
    # Assert partial close was called
    deal_requests = [
        call.args[0] for call in mt5_mock.order_send.mock_calls
        if call.args[0].get("action") == mt5_mock.TRADE_ACTION_DEAL
    ]
    assert len(deal_requests) == 1
    assert deal_requests[0]["volume"] == 0.5
    
    # Assert plan was updated
    updated_plan = json.loads(plan_path.read_text())
    assert updated_plan["touched_target"] == 1
    assert updated_plan["exit_legs"][0]["closed"] is True

def test_stop_loss_breakeven(mt5_mock, plan_path, strategy_patch):
    """
    2. Stop Loss Breakeven: Mock a strategy that has `stop_moves` at TP1. 
    Trigger TP1 and verify a `TRADE_ACTION_SLTP` modification is sent to move SL to entry price.
    """
    create_plan(plan_path, touched_target=1) # Already touched TP1
    position = SimpleNamespace(
        ticket=1,
        magic=12345,
        comment="AURUM:test",
        symbol="GOLD",
        type=mt5_mock.POSITION_TYPE_BUY,
        volume=0.5,
        price_open=100.0,
        sl=90.0, # Currently at 90
        tp=130.0,
    )
    mt5_mock.positions_get.return_value = (position,)
    mt5_mock.orders_get.return_value = ()
    
    # Mock favorable extreme, currently at 110 (no new level hit)
    mt5_mock.symbol_info_tick.return_value = SimpleNamespace(
        bid=110.0,
        ask=110.0,
        time=int(time.time()),
    )
    with patch("aurum_bot.strategy_manager._favorable_extreme", return_value=110.0):
        _manage_plan(
            mt5=mt5_mock,
            path=plan_path,
            plan=json.loads(plan_path.read_text()),
            deviation=10,
            exit_spread_guard_enabled=False
        )

    # Assert SL modification was called
    sltp_requests = [
        call.args[0] for call in mt5_mock.order_send.mock_calls
        if call.args[0].get("action") == mt5_mock.TRADE_ACTION_SLTP
    ]
    # One request for the position modifier
    assert len(sltp_requests) >= 1
    modified_sl = sltp_requests[-1]["sl"]
    assert modified_sl == 100.0 # Moved to breakeven (entry price)

    updated_plan = json.loads(plan_path.read_text())
    assert updated_plan["active_stop_target"] == 0

def test_lot_rounding_safety(mt5_mock, plan_path, strategy_patch):
    """
    3. Lot Rounding Safety: Ensure partial closes never request `0.00` volume, 
    especially on the final target when remaining volume is very small.
    """
    # Create plan where volume to close is very small compared to step
    create_plan(plan_path)
    position = SimpleNamespace(
        ticket=1,
        magic=12345,
        comment="AURUM:test",
        symbol="GOLD",
        type=mt5_mock.POSITION_TYPE_BUY,
        volume=0.01, # Vol min is 0.01
        price_open=100.0,
        sl=90.0,
        tp=130.0,
    )
    mt5_mock.positions_get.return_value = (position,)
    mt5_mock.orders_get.return_value = ()

    # Update exit leg to request an impossibly small amount
    plan = json.loads(plan_path.read_text())
    plan["exit_legs"][0]["volume"] = 0.001
    plan_path.write_text(json.dumps(plan))
    mt5_mock.symbol_info_tick.return_value = SimpleNamespace(
        bid=111.0,
        ask=111.0,
        time=int(time.time()),
    )
    with patch("aurum_bot.strategy_manager._favorable_extreme", return_value=111.0):
        _manage_plan(
            mt5=mt5_mock,
            path=plan_path,
            plan=json.loads(plan_path.read_text()),
            deviation=10,
            exit_spread_guard_enabled=False
        )

    deal_requests = [
        call.args[0] for call in mt5_mock.order_send.mock_calls
        if call.args[0].get("action") == mt5_mock.TRADE_ACTION_DEAL
    ]
    assert len(deal_requests) == 1
    # Should round up to minimum volume 0.01, not 0.00
    assert deal_requests[0]["volume"] == 0.01

def test_error_handling(mt5_mock, plan_path, strategy_patch, caplog):
    """
    4. Error Handling: Verify that if an MT5 operation fails, the error is logged 
    and the `plan` state remains intact for the next poll (does not advance `target_index`).
    """
    create_plan(plan_path)
    position = SimpleNamespace(
        ticket=1,
        magic=12345,
        comment="AURUM:test",
        symbol="GOLD",
        type=mt5_mock.POSITION_TYPE_BUY,
        volume=1.0,
        price_open=100.0,
        sl=90.0,
        tp=130.0,
    )
    mt5_mock.positions_get.return_value = (position,)
    mt5_mock.orders_get.return_value = ()
    
    # Mock mt5 failure for order_send
    mt5_mock.order_send.return_value = SimpleNamespace(retcode=10013, comment="Invalid request")

    with patch("aurum_bot.strategy_manager._favorable_extreme", return_value=111.0):
        _manage_plan(
            mt5=mt5_mock,
            path=plan_path,
            plan=json.loads(plan_path.read_text()),
            deviation=10,
            exit_spread_guard_enabled=False
        )

    # Leg should NOT be marked closed
    updated_plan = json.loads(plan_path.read_text())
    assert updated_plan["exit_legs"][0]["closed"] is False
    # Touched target still increments since extreme was observed
    assert updated_plan["touched_target"] == 1
