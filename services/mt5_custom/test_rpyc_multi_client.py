import rpyc

# Configura aquí los puertos de cada cuenta MT5
accounts = [
    {"name": "mt5_acct1", "port": 8001},
    {"name": "mt5_acct2", "port": 8002},
    {"name": "mt5_acct3", "port": 8003},
    {"name": "mt5_acct4", "port": 8004},
    {"name": "mt5_acct5", "port": 8005},
    {"name": "mt5_acct6", "port": 8006},
]

for acct in accounts:
    try:
        print(f"\nConectando a {acct['name']} en puerto {acct['port']}...")
        conn = rpyc.connect('localhost', acct['port'])
        version = conn.root.mt5().version()
        print(f"Versión de MetaTrader5 en {acct['name']}: {version}")
        symbol = 'EURUSD'
        result = conn.root.symbol_select(symbol, True)
        print(f"Símbolo {symbol} seleccionado en {acct['name']}: {result}")
        conn.close()
    except Exception as e:
        print(f"Error conectando a {acct['name']} en puerto {acct['port']}: {e}")
