"""
main.py: Entry point para la lógica centralizada de gestión de trades.
- Lee señales/parámetros de entrada (por ahora, simulado)
- Publica comandos en el bus
- Escucha eventos de ejecución y actualiza métricas
"""
import asyncio
import logging
import json
from .bus import TradeBus
from .metrics import TRADES_OPENED, TRADES_FAILED, TP_HITS, PARTIAL_CLOSES, ACTIVE_TRADES, TRAILING_ACTIVATED, BE_ACTIVATED
from .management import CentralizedTradeManager

logging.basicConfig(level=logging.INFO)

async def main():
    bus = TradeBus()
    await bus.connect()
    logging.info("Bus conectado. Esperando señales...")

    # Instanciar el gestor centralizado
    manager = CentralizedTradeManager(bus)
    # Lanzar el loop de gestión en background
    asyncio.create_task(manager.run())


    # Escuchar señales parseadas y procesarlas
    redis = bus.redis
    last_id = "$"
    while True:
        streams = await redis.xread({"parsed_signals": last_id}, block=1000)
        for stream, msgs in streams or []:
            for msg_id, sig in msgs:
                # Decodificar claves y valores si vienen como bytes
                if any(isinstance(k, bytes) for k in sig.keys()):
                    sig = {k.decode(): v.decode() if isinstance(v, bytes) else v for k, v in sig.items()}
                # Poblar accounts y volume según la lógica de ruteo
                from common.config import Settings
                s = Settings.load()
                accounts_config = s.accounts()
                chat_id = None
                # Buscar chat_id en la señal (puede venir como str o int)
                if "chat_id" in sig:
                    try:
                        chat_id = int(sig["chat_id"])
                    except Exception:
                        chat_id = sig["chat_id"]
                # Filtrar cuentas activas y permitidas para el chat_id
                routed_accounts = []
                volume = 0.01
                for acct in accounts_config:
                    if not acct.get("active", False):
                        continue
                    allowed = acct.get("allowed_channels", [])
                    if chat_id is not None and allowed and int(chat_id) not in allowed:
                        continue
                    routed_accounts.append(acct["name"])
                # Si hay solo una cuenta, usar su fixed_lot
                if routed_accounts:
                    acct_obj = next((a for a in accounts_config if a["name"] == routed_accounts[0]), None)
                    if acct_obj and "fixed_lot" in acct_obj:
                        volume = acct_obj["fixed_lot"]
                command = {
                    "signal_id": sig.get("trace", sig.get("signal_id")),
                    "type": "open",
                    "symbol": sig.get("symbol"),
                    "direction": sig.get("direction"),
                    "entry_range": sig.get("entry_range"),
                    "sl": sig.get("sl"),
                    "tp": json.loads(sig.get("tps", "[]")),
                    "provider_tag": sig.get("provider_tag"),
                    "timestamp": sig.get("timestamp"),
                    "accounts": routed_accounts,
                    "volume": volume,
                }
                await bus.publish_command(command)
                logging.info(f"[CENTRALIZED] Comando publicado: {command}")
                last_id = msg_id

if __name__ == "__main__":
    asyncio.run(main())
