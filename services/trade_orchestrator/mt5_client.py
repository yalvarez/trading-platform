from mt5linux import MetaTrader5

class MT5Client:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = int(port)
        self.mt5 = MetaTrader5(host=host, port=self.port)

    def tick_price(self, symbol: str, direction: str) -> float:
        t = self.mt5.symbol_info_tick(symbol)
        if not t:
            return 0.0
        return float(t.ask if direction == "BUY" else t.bid)

    def positions_get(self):
        return self.mt5.positions_get()

    def order_send(self, req: dict):
        return self.mt5.order_send(req)

    def symbol_info(self, symbol: str):
        return self.mt5.symbol_info(symbol)

    def symbol_select(self, symbol: str, enable: bool=True):
        return self.mt5.symbol_select(symbol, enable)
