import rpyc

# Conexión al servidor rpyc en el contenedor
conn = rpyc.connect('localhost', 8001)

# Prueba: llamar a un método expuesto
print('Versión de MetaTrader5:', conn.root.mt5().version())

# Prueba: seleccionar un símbolo
symbol = 'EURUSD'
result = conn.root.symbol_select(symbol, True)
print(f'Símbolo {symbol} seleccionado:', result)

conn.close()
