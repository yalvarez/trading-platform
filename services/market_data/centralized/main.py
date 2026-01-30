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
                # Procesar la señal parseada y construir el comando de trade
                # Aquí debes aplicar la lógica de cuentas, volumen, modalidad, etc.
                # Ejemplo mínimo:
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
                    # Aquí debes poblar accounts y volume según tu lógica
                    "accounts": [],
                    "volume": 0.01,
                }
                await bus.publish_command(command)
                logging.info(f"[CENTRALIZED] Comando publicado: {command}")
                last_id = msg_id

if __name__ == "__main__":
    asyncio.run(main())
