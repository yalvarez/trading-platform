import rpyc
from rpyc.utils.server import ThreadedServer
import MetaTrader5 as mt5
import time
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [mt5_extended] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("mt5_extended.server_rpyc")


class MT5Service(rpyc.Service):
    def exposed_initialize(self):
        return mt5.initialize()

    def exposed_login(self, login, password, server):
        return mt5.login(login=login, password=password, server=server)

    def exposed_symbol_info(self, symbol):
        return mt5.symbol_info(symbol)

    def exposed_symbol_info_tick(self, symbol):
        return mt5.symbol_info_tick(symbol)

    def exposed_positions_get(self):
        return mt5.positions_get()

    def exposed_order_send(self, req):
        return mt5.order_send(req)

    def exposed_symbol_select(self, symbol, enable=True):
        return mt5.symbol_select(symbol, enable)

    def exposed_last_error(self):
        return mt5.last_error()


if __name__ == "__main__":
    log.info("Inicializando MT5...")
    mt5.initialize()
    time.sleep(2)
    log.info("Iniciando servidor RPyC en puerto 18812...")
    server = ThreadedServer(
        MT5Service,
        port=18812,
        protocol_config={"allow_public_attrs": True},
    )
    server.start()
