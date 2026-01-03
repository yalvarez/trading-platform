import rpyc
import MetaTrader5 as mt5
import sys

class Tee:
    def __init__(self, *files):
        self.files = files
    def write(self, data):
        for f in self.files:
            f.write(data)
            f.flush()
    def flush(self):
        for f in self.files:
            f.flush()

# Redirigir stdout y stderr a archivo y consola
logfile = open('/config/rpyc.log', 'a', buffering=1)
sys.stdout = Tee(sys.stdout, logfile)
sys.stderr = Tee(sys.stderr, logfile)

class MT5Service(rpyc.Service):
    def on_connect(self, conn):
        pass
    def on_disconnect(self, conn):
        pass
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
        print("[server_rpyc] Iniciando MetaTrader5...")
        import time
        max_wait = 300  # segundos
        interval = 5   # segundos
        waited = 0
        while not mt5.initialize():
            print(f"[server_rpyc] MT5 initialize() failed: {mt5.last_error()} (reintentando en {interval}s)")
            time.sleep(interval)
            waited += interval
            if waited >= max_wait:
                print(f"[server_rpyc] MT5 no pudo inicializarse tras {max_wait} segundos. Abortando.")
                exit(1)
        print("[server_rpyc] MetaTrader5 inicializado correctamente.")
        from rpyc.utils.server import ThreadedServer
        print("[server_rpyc] Iniciando servidor rpyc en puerto 8001...")
        server = ThreadedServer(MT5Service, port=8001, protocol_config={"sync_request_timeout": 30})
        print("[server_rpyc] Servidor rpyc iniciado. Esperando conexiones...")
        server.start()
    except Exception as e:
        print("[server_rpyc] Excepción fatal:")
        traceback.print_exc()
        exit(1)
