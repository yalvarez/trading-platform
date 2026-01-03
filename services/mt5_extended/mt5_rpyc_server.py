import rpyc
from rpyc.utils.server import ThreadedServer
import MetaTrader5 as mt5
import time

class MT5Service(rpyc.Service):
    def on_connect(self, conn):
        print("Client connected")

    def on_disconnect(self, conn):
        print("Client disconnected")

    # Expose MT5 API
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
    print("Initializing MT5...")
    mt5.initialize()
    time.sleep(2)

    print("Starting RPyC server on port 18812...")
    server = ThreadedServer(
        MT5Service,
        port=18812,
        protocol_config={"allow_public_attrs": True}
    )
    server.start()