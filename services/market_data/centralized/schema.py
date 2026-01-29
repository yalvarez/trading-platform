# Esquema de mensajes y canales centralizados para trading

# Canal de comandos de trading (producido por market_data, consumido por trade_orchestrator)
TRADE_COMMANDS_STREAM = "trade_commands"

# Canal de eventos de ejecución (producido por trade_orchestrator, consumido por market_data)
TRADE_EVENTS_STREAM = "trade_events"

# Ejemplo de mensaje de comando de trading
EXAMPLE_TRADE_COMMAND = {
    "signal_id": "abc123",
    "type": "open|move_sl|close|partial_close|trailing|be",  # tipo de acción
    "symbol": "XAUUSD",
    "direction": "BUY",
    "entry_price": 2025.10,  # solo para open
    "sl": 2019.00,
    "tp": [2028.00, 2032.00],
    "volume": 0.1,  # solo para open/partial_close
    "accounts": ["acct1", "acct2"],  # a quién va dirigido
    "trailing": {"enabled": True, "distance": 50},  # opcional
    "be": {"enabled": True, "offset": 0},  # opcional
    "timestamp": 1700000000
}

# Ejemplo de mensaje de evento de ejecución
EXAMPLE_TRADE_EVENT = {
    "signal_id": "abc123",
    "account": "acct1",
    "type": "executed|error|sl_moved|tp_hit|partial_closed|trailing_activated|be_activated",
    "ticket": 123456,
    "status": "success|error",
    "details": "...",
    "timestamp": 1700000001
}
