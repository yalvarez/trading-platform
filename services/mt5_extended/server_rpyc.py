import rpyc
from rpyc.utils.server import ThreadedServer
import MetaTrader5 as mt5

class MT5Service(rpyc.Service):
    def on_connect(self, conn):
        mt5.initialize()
    def on_disconnect(self, conn):
        mt5.shutdown()
    def exposed_mt5(self):
        return mt5
    def exposed_symbol_select(self, symbol, enable):
        return mt5.symbol_select(symbol, enable)
    # Puedes exponer aquí más métodos de la API MT5

if __name__ == "__main__":
    server = ThreadedServer(MT5Service, port=8001, protocol_config={"allow_public_attrs": True, "allow_all_attrs": True})
    server.start()
