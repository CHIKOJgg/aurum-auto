import pytest
import sys
from unittest.mock import patch, MagicMock

# Ensure we can import aurum_bot
from aurum_bot.mt5_worker import execute, _prepare_for_new_signal
from aurum_bot.models import ExecutionResult, AccountConfig, Signal, Direction


@pytest.fixture
def base_payload():
    return {
        "account": {
            "name": "TestAccount",
            "terminal_path": "C:\\fake\\terminal64.exe",
            "risk_base_usd": 1000.0,
            "enabled": True,
            "symbols": []
        },
        "signal": {
            "symbol": "XAUUSD",
            "direction": "LONG",
            "entry": 2000.0,
            "stop_loss": 1990.0,
            "take_profit": 2010.0,
            "take_profits": [2010.0, 2020.0, 2030.0, 2040.0],
            "message_id": 12345
        },
        "trading": {
            "magic_number": 1001,
            "lot_step": 0.01,
            "deviation_points": 20,
            "send_attempts": 3,
            "retry_delay_seconds": 0.1,
            "min_market_risk_multiplier": 0.5,
            "max_market_risk_multiplier": 2.0,
            "close_opposite_positions": False
        },
        "strategy_state_dir": None
    }


def _setup_mock_mt5(mock_mt5):
    mock_mt5.initialize.return_value = True
    
    mock_acc_info = MagicMock()
    mock_acc_info.currency = "USD"
    mock_acc_info.trade_allowed = True
    mock_acc_info.margin_mode = 2
    mock_acc_info.margin_free = 10000.0
    mock_mt5.account_info.return_value = mock_acc_info
    
    mock_term_info = MagicMock()
    mock_term_info.trade_allowed = True
    mock_mt5.terminal_info.return_value = mock_term_info
    
    mock_mt5.symbol_select.return_value = True
    
    mock_symbol_info = MagicMock()
    mock_symbol_info.digits = 2
    mock_symbol_info.point = 0.01
    mock_symbol_info.trade_contract_size = 100.0
    mock_symbol_info.volume_min = 0.01
    mock_symbol_info.volume_max = 100.0
    mock_symbol_info.volume_step = 0.01
    mock_symbol_info.trade_stops_level = 10
    mock_symbol_info.filling_mode = 1
    mock_symbol_info.trade_exemode = 1
    mock_mt5.symbol_info.return_value = mock_symbol_info
    
    mock_tick = MagicMock()
    mock_tick.ask = 2000.0
    mock_tick.bid = 1999.8
    mock_mt5.symbol_info_tick.return_value = mock_tick
    
    # Defaults
    mock_mt5.positions_get.return_value = ()
    mock_mt5.orders_get.return_value = ()
    mock_mt5.order_calc_profit.return_value = -100.0
    mock_mt5.order_calc_margin.return_value = 100.0
    
    # Enums
    mock_mt5.ORDER_TYPE_BUY = 0
    mock_mt5.ORDER_TYPE_SELL = 1
    mock_mt5.TRADE_ACTION_DEAL = 1
    mock_mt5.TRADE_ACTION_PENDING = 5
    mock_mt5.TRADE_ACTION_SLTP = 6
    mock_mt5.TRADE_RETCODE_DONE = 10009
    mock_mt5.TRADE_RETCODE_PLACED = 10008
    mock_mt5.ORDER_TIME_GTC = 0
    mock_mt5.ORDER_FILLING_FOK = 0
    
    return mock_mt5


@patch("aurum_bot.mt5_worker.Path.is_file")
@patch.dict(sys.modules, {"MetaTrader5": MagicMock()})
def test_auto_sltp_logic(mock_is_file, base_payload):
    mock_is_file.return_value = True
    
    import MetaTrader5 as mt5
    _setup_mock_mt5(mt5)
    
    # The price geometry matches market execution.
    # Set the tick so that the execution_kind is MARKET (e.g. entry = 2000, ask = 2000)
    mt5.symbol_info_tick.return_value.ask = 2000.0
    
    # order_check should pass
    mock_check = MagicMock()
    mock_check.retcode = 0
    mt5.order_check.return_value = mock_check
    
    # First call: TRADE_ACTION_DEAL, return a ticket
    mock_result = MagicMock()
    mock_result.retcode = 10009
    mock_result.order = 9999
    
    mt5.order_send.return_value = mock_result
    
    # When execute calls positions_get(ticket=9999) to verify SL/TP,
    # return a position with 0.0 SL and TP.
    mock_position = MagicMock()
    mock_position.ticket = 9999
    mock_position.magic = 1001
    mock_position.sl = 0.0
    mock_position.tp = 0.0
    
    def positions_get_side_effect(*args, **kwargs):
        if kwargs.get("ticket") == 9999:
            return (mock_position,)
        return ()
        
    mt5.positions_get.side_effect = positions_get_side_effect
    
    result = execute(base_payload)
    
    assert result.status == "executed"
    assert result.ticket == 9999
    
    # Assert order_send was called a second time for SLTP
    # The first time is TRADE_ACTION_DEAL, the second time should be TRADE_ACTION_SLTP
    calls = mt5.order_send.call_args_list
    assert len(calls) >= 2
    
    sltp_call = calls[-1]
    req = sltp_call[0][0]
    assert req["action"] == mt5.TRADE_ACTION_SLTP
    assert req["position"] == 9999
    assert req["sl"] == 1990.0
    assert req["tp"] == 2020.0


@patch("aurum_bot.mt5_worker.Path.is_file")
@patch.dict(sys.modules, {"MetaTrader5": MagicMock()})
def test_ipc_transient_failure_guards(mock_is_file, base_payload):
    mock_is_file.return_value = True
    
    import MetaTrader5 as mt5
    _setup_mock_mt5(mt5)
    
    # Simulate positions_get returning None
    mt5.positions_get.return_value = None
    
    result = execute(base_payload)
    
    assert result.status == "failed"
    assert "MT5 API query failed while checking existing orders" in result.detail


@patch("aurum_bot.mt5_worker.Path.is_file")
@patch.dict(sys.modules, {"MetaTrader5": MagicMock()})
def test_opposite_signals_logic_close_disabled(mock_is_file, base_payload):
    mock_is_file.return_value = True
    import MetaTrader5 as mt5
    _setup_mock_mt5(mt5)
    
    # Test `_prepare_for_new_signal` separately
    # If replace_existing=False, it should return ready immediately
    status, detail = _prepare_for_new_signal(
        mt5,
        "XAUUSD",
        1001,
        replace_existing=False,
        symbol_info=mt5.symbol_info("XAUUSD"),
        deviation_points=20
    )
    
    assert status == "ready"
    assert "hedging mode: existing positions and pending orders are preserved" in detail
    
    # Ensure it didn't call mt5.positions_get or orders_get
    mt5.positions_get.assert_not_called()
    mt5.orders_get.assert_not_called()


@patch("aurum_bot.mt5_worker.Path.is_file")
@patch.dict(sys.modules, {"MetaTrader5": MagicMock()})
def test_opposite_signals_logic_execution(mock_is_file, base_payload):
    mock_is_file.return_value = True
    import MetaTrader5 as mt5
    _setup_mock_mt5(mt5)
    
    # Set close_opposite_positions to False
    base_payload["trading"]["close_opposite_positions"] = False
    
    # Mock order send to fail so it stops earlier or succeed. Let's just check the flow.
    mock_check = MagicMock()
    mock_check.retcode = 0
    mt5.order_check.return_value = mock_check
    
    mock_result = MagicMock()
    mock_result.retcode = 10009
    mock_result.order = 1111
    mt5.order_send.return_value = mock_result
    
    execute(base_payload)
    
    # If close_opposite is False, we should only see positions_get/orders_get calls 
    # for checking already existing positions (reconcile) and at the end for SL/TP.
    # It should NOT iterate and try to close anything.
    # We can check order_send calls. It should only contain our new order, not any closures.
    
    calls = mt5.order_send.call_args_list
    assert len(calls) > 0
    # None of the calls should have action == TRADE_ACTION_REMOVE or TRADE_ACTION_DEAL with type for closing.
    # Because we're opening a LONG, the only TRADE_ACTION_DEAL should be ORDER_TYPE_BUY
    for call in calls:
        req = call[0][0]
        if req["action"] == mt5.TRADE_ACTION_DEAL:
            assert req["type"] == mt5.ORDER_TYPE_BUY
        assert req["action"] != mt5.TRADE_ACTION_REMOVE
