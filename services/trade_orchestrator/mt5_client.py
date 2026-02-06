from mt5linux import MetaTrader5


class MT5Client:
    def get_pip_size(self, symbol: str) -> float:
        """
        Devuelve el tamaño de pip para un símbolo usando symbol_info.
        Intenta pip_size, luego tick_size, luego point como fallback.
        """
        info = self.symbol_info(symbol)
        if not info:
            return 0.0
        # Try pip_size (custom attribute, not always present)
        pip_size = getattr(info, 'pip_size', None)
        if pip_size and pip_size > 0:
            return float(pip_size)
        # Try tick_size (MetaTrader5 standard)
        tick_size = getattr(info, 'tick_size', None)
        if tick_size and tick_size > 0:
            return float(tick_size)
        # Fallback to point (MetaTrader5 standard)
        point = getattr(info, 'point', None)
        if point and point > 0:
            return float(point)
        return 0.0
    def symbol_info_tick(self, symbol: str):
        """
        Devuelve el tick info del símbolo usando la API subyacente de MetaTrader5.
        """
        return self.mt5.symbol_info_tick(symbol)

    def partial_close(self, account: dict, ticket: int, percent: int) -> bool:
        """
        Realiza un cierre parcial de la posición indicada por ticket, probando todos los filling modes para máxima compatibilidad.
        """
        if hasattr(self.mt5, 'connect_to_account'):
            try:
                self.mt5.connect_to_account(account)
            except Exception as e:
                print(f"[MT5Client] Error al seleccionar cuenta: {e}")
                return False

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
        info = self.mt5.symbol_info(symbol)
        step = float(getattr(info, 'volume_step', 0.01)) if info else 0.01
        min_vol = float(getattr(info, 'volume_min', 0.01)) if info else 0.01
        raw_close = volume * (float(percent) / 100.0)
        close_vol = step * int(raw_close / step)
        if close_vol < min_vol:
            if volume > min_vol:
                print(f"[MT5Client] Volumen a cerrar menor al mínimo, usando min_vol: {min_vol}")
                close_vol = min_vol
            else:
                print(f"[MT5Client] Volumen a cerrar menor al mínimo, cerrando todo: {volume}")
                close_vol = volume
        if close_vol > volume:
            close_vol = volume
        order_type = 1 if getattr(pos, 'type', 0) == 0 else 0  # 0=buy, 1=sell
        price = self.tick_price(symbol, 'SELL' if order_type == 1 else 'BUY')
        if price is None or price == 0.0:
            print(f"[MT5Client][ERROR] No se pudo obtener el precio actual de {symbol} para cierre parcial. Abortando operación.")
            return False
        # Probar todos los filling modes
        import logging
        log = logging.getLogger("trade_orchestrator.mt5_client")
        for type_filling in [1, 3, 2]:  # IOC, FOK, RETURN
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
                "type_filling": type_filling,
            }
            res = self.mt5.order_send(req)
            log.debug(f"[MT5Client][PartialClose] req: {req}")
            log.debug(f"[MT5Client][PartialClose] res: {res}")
            # Consultar el estado de la posición después del intento
            pos_list = self.mt5.positions_get(ticket=ticket)
            log.debug(f"[MT5Client][PartialClose] positions_get after partial_close: {pos_list}")
            if not res:
                log.error(f"[MT5Client] No se recibió respuesta de order_send para ticket {ticket}")
                continue
            retcode = getattr(res, 'retcode', None)
            if retcode == 10009:
                return True
            else:
                log.error(f"[MT5Client] Retcode inesperado: {retcode}, mensaje: {getattr(res, 'comment', '')}")
        return False

    def __init__(self, host: str, port: int):
        """
        Inicializa el cliente MT5 con host y puerto dados.
        """
        self.mt5 = MetaTrader5(host=host, port=port)
        self.mt5.initialize()

    def tick_price(self, symbol: str, direction: str) -> float:
        """
        Devuelve el precio actual (ask para BUY, bid para SELL) del símbolo.
        """
        t = self.mt5.symbol_info_tick(symbol)
        if not t:
            return 0.0
        return float(t.ask if direction == "BUY" else t.bid)

    def positions_get(self, *args, **kwargs):
        """
        Devuelve las posiciones abiertas según los argumentos dados.
        """
        return self.mt5.positions_get(*args, **kwargs)

    def order_send(self, req: dict):
        """
        Envía una orden a MT5 usando el diccionario de parámetros req.
        """
        return self.mt5.order_send(req)

    def symbol_info(self, symbol: str):
        """
        Devuelve la información del símbolo desde MT5.
        """
        return self.mt5.symbol_info(symbol)

    def symbol_select(self, symbol: str, enable: bool = True):
        """
        Selecciona o deselecciona un símbolo en MT5.
        """
        return self.mt5.symbol_select(symbol, enable)