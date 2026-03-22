import rpyc
import MetaTrader5 as mt5
import sys
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [mt5_custom] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/config/rpyc.log", mode="a"),
    ],
)
log = logging.getLogger("mt5_custom.server_rpyc")


class MT5Service(rpyc.Service):
    def exposed_symbol_select(self, symbol, enable=True):
        return mt5.symbol_select(symbol, enable)

    def exposed_symbol_info(self, symbol):
        return mt5.symbol_info(symbol)

    def exposed_positions_get(self):
        return mt5.positions_get()

    def exposed_order_send(self, req):
        return mt5.order_send(req)

    def exposed_symbol_info_tick(self, symbol):
        return mt5.symbol_info_tick(symbol)


if __name__ == "__main__":
    import traceback
    try:
        log.info("Iniciando MetaTrader5...")
        max_wait = 300
        interval = 5
        waited = 0
        while not mt5.initialize():
            log.warning("MT5 initialize() failed: %s (reintentando en %ds)", mt5.last_error(), interval)
            time.sleep(interval)
            waited += interval
            if waited >= max_wait:
                log.critical("MT5 no pudo inicializarse tras %ds. Abortando.", max_wait)
                sys.exit(1)
        log.info("MetaTrader5 inicializado correctamente.")
        from rpyc.utils.server import ThreadedServer
        log.info("Iniciando servidor rpyc en puerto 8001...")
        server = ThreadedServer(MT5Service, port=8001, protocol_config={"sync_request_timeout": 30})
        log.info("Servidor rpyc iniciado. Esperando conexiones...")
        server.start()
    except Exception:
        log.critical("Excepcion fatal:\n%s", traceback.format_exc())
        sys.exit(1)
