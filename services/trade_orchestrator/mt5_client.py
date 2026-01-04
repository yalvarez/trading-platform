from mt5linux import MetaTrader5


class MT5Client:
    def partial_close(self, account: dict, ticket: int, percent: int) -> bool:
        """
        Realiza un cierre parcial de la posición indicada por ticket, cerrando el porcentaje especificado.
        Asegura la selección de cuenta y loguea el resultado completo.
        """
        # Seleccionar cuenta si es necesario
        if hasattr(self.mt5, 'connect_to_account'):
            try:
                self.mt5.connect_to_account(account)
            except Exception as e:
                print(f"[MT5Client] Error al seleccionar cuenta: {e}")
                return False

        # Obtener la posición actual
        pos_list = self.mt5.positions_get(ticket=ticket)
        if not pos_list:
            print(f"[MT5Client] No se encontró la posición para ticket {ticket}")
            return False
        pos = pos_list[0]
        volume = float(getattr(pos, 'volume', 0.0))
        symbol = getattr(pos, 'symbol', None)
        if not symbol or volume <= 0:
            print(f"[MT5Client] Volumen inválido o símbolo no encontrado para ticket {ticket}")
            return False
        # Calcular volumen a cerrar
        close_vol = volume * (float(percent) / 100.0)
        # Ajustar a step mínimo
        info = self.mt5.symbol_info(symbol)
        step = float(getattr(info, 'volume_step', 0.01)) if info else 0.01
        min_vol = float(getattr(info, 'volume_min', 0.01)) if info else 0.01
        close_vol = step * round(close_vol / step)
        if close_vol < min_vol or close_vol <= 0:
            print(f"[MT5Client] Volumen a cerrar menor al mínimo, cerrando todo: {volume}")
            close_vol = volume  # fallback: cerrar todo
        # Determinar tipo de orden opuesta
        order_type = 1 if getattr(pos, 'type', 0) == 0 else 0  # 0=buy, 1=sell
        price = self.tick_price(symbol, 'SELL' if order_type == 1 else 'BUY')
        req = {
            "action": 1,  # TRADE_ACTION_DEAL
            "symbol": symbol,
            "volume": float(close_vol),
            "type": order_type,
            "position": int(ticket),
            "price": float(price),
            "deviation": 50,
            "magic": 987654,
            "comment": "PartialClose",
            "type_time": 0,
            "type_filling": 1,
        }
        res = self.mt5.order_send(req)
        print(f"[MT5Client] partial_close req: {req}")
        print(f"[MT5Client] partial_close res: {res}")
        if not res:
            print(f"[MT5Client] No se recibió respuesta de order_send para ticket {ticket}")
            return False
        retcode = getattr(res, 'retcode', None)
        if retcode != 10009:
            print(f"[MT5Client] Retcode inesperado: {retcode}, mensaje: {getattr(res, 'comment', '')}")
            return False
        return True

    def __init__(self, host: str, port: int):
        self.mt5 = MetaTrader5(host=host, port=port)
        self.mt5.initialize()

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

    def symbol_select(self, symbol: str, enable: bool = True):
        return self.mt5.symbol_select(symbol, enable)