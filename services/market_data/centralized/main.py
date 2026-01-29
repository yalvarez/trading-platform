"""
main.py: Entry point para la lógica centralizada de gestión de trades.
- Lee señales/parámetros de entrada (por ahora, simulado)
- Publica comandos en el bus
- Escucha eventos de ejecución y actualiza métricas
"""
import asyncio
import logging
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

    # Ejemplo: publicar un comando de apertura de trade (simulado)
    command = {
        "signal_id": "demo001",
        "type": "open",
        "symbol": "XAUUSD",
        "direction": "BUY",
        "entry_price": 2025.10,
        "sl": 2019.00,
        "tp": [2028.00, 2032.00],
        "volume": 0.1,
        "accounts": ["acct1", "acct2"],
        "timestamp": 1700000000
    }
    await bus.publish_command(command)
    logging.info(f"Comando publicado: {command}")

    # Escuchar eventos de ejecución y actualizar métricas
    async for msg_id, event in bus.listen_events():
        logging.info(f"Evento recibido: {event}")
        if event["type"] == "executed" and event["status"] == "success":
            TRADES_OPENED.labels(account=event["account"], symbol=command["symbol"]).inc()
            ACTIVE_TRADES.labels(account=event["account"], symbol=command["symbol"]).inc()
        elif event["type"] == "executed" and event["status"] == "error":
            TRADES_FAILED.labels(account=event["account"], symbol=command["symbol"]).inc()
        elif event["type"] == "tp_hit":
            TP_HITS.labels(account=event["account"], symbol=command["symbol"], tp=event.get("tp", "")).inc()
        elif event["type"] == "partial_closed":
            PARTIAL_CLOSES.labels(account=event["account"], symbol=command["symbol"]).inc()
        elif event["type"] == "trailing_activated":
            TRAILING_ACTIVATED.labels(account=event["account"], symbol=command["symbol"]).inc()
        elif event["type"] == "be_activated":
            BE_ACTIVATED.labels(account=event["account"], symbol=command["symbol"]).inc()

if __name__ == "__main__":
    asyncio.run(main())
