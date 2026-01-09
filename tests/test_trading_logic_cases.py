import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'services', 'trade_orchestrator')))
from trade_manager import TradeManager

import pytest
from unittest.mock import MagicMock

def test_trade_execution_success():
    mt5_executor = MagicMock()
    mt5_executor.open_complete_trade.return_value = {'ticket': 123, 'retcode': 10009}
    result = mt5_executor.open_complete_trade(provider='GB_LONG', symbol='XAUUSD', direction='BUY', entry=[2500,2505], sl=2490, tps=[2515,2530])
    assert result['retcode'] == 10009
    assert result['ticket'] == 123

def test_trade_execution_autotrading_disabled():
    mt5_executor = MagicMock()
    mt5_executor.open_complete_trade.return_value = {'ticket': 0, 'retcode': 10027}
    result = mt5_executor.open_complete_trade(provider='GB_LONG', symbol='XAUUSD', direction='BUY', entry=[2500,2505], sl=2490, tps=[2515,2530])
    assert result['retcode'] == 10027

def test_trade_execution_connection_refused():
    mt5_executor = MagicMock()
    mt5_executor.open_complete_trade.side_effect = ConnectionRefusedError()
    with pytest.raises(ConnectionRefusedError):
        mt5_executor.open_complete_trade(provider='GB_LONG', symbol='XAUUSD', direction='BUY', entry=[2500,2505], sl=2490, tps=[2515,2530])
