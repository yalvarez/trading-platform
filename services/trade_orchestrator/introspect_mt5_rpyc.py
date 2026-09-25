"""
Herramienta de diagnostico: lista lo que expone el servidor RPyC de la terminal MT5.

Uso (dentro del contenedor, por la red interna de Docker):
    docker exec atp-trade-orchestrator python services/trade_orchestrator/introspect_mt5_rpyc.py

Antes apuntaba a atp-mt5-acct1:9081, que no existe dentro de la red Docker (9081
es el puerto publicado en el host, y desde 2026-09-24 solo en 127.0.0.1): el
servidor escucha en el 8001 del contenedor. Toma MT5_HOST/MT5_PORT del entorno,
igual que el resto de servicios.
"""
import os

import rpyc

host = os.getenv("MT5_HOST", "mt5_acct1")
port = int(os.getenv("MT5_PORT", "8001"))

conn = rpyc.connect(host, port, config={"sync_request_timeout": 30})
root = conn.root
print(f"Conectado a {host}:{port}")
print("Root type:", type(root))
attrs = dir(root)
print("dir(root):", attrs)
for attr in attrs:
    if not attr.startswith("_"):
        try:
            val = getattr(root, attr)
            print(f"{attr}: {type(val)}")
        except Exception as e:
            print(f"{attr}: ERROR - {e}")
conn.close()
