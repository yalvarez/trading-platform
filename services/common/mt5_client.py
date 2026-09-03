import logging
from mt5linux import MetaTrader5

log = logging.getLogger("trade_orchestrator.mt5_client")


class MT5Client:
    def __init__(self, host: str, port: int):
        self.mt5 = MetaTrader5(host=host, port=port)
        self.mt5.initialize()

    def get_pip_size(self, symbol: str) -> float:
        info = self.symbol_info(symbol)
        if not info:
            return 0.0
        pip_size = getattr(info, "pip_size", None)
        if pip_size and pip_size > 0:
            return float(pip_size)
        tick_size = getattr(info, "tick_size", None)
        if tick_size and tick_size > 0:
            return float(tick_size)
        point = getattr(info, "point", None)
        if point and point > 0:
            return float(point)
        return 0.0

    def symbol_info_tick(self, symbol: str):
        return self.mt5.symbol_info_tick(symbol)

    def partial_close(self, account: dict, ticket: int, percent: int) -> bool:
        if hasattr(self.mt5, "connect_to_account"):
            try:
                self.mt5.connect_to_account(account)
            except Exception as e:
                log.error("[MT5Client] Error al seleccionar cuenta: %s", e)
                return False

        pos_list = self.mt5.positions_get(ticket=ticket)
        if not pos_list:
            log.warning("[MT5Client] No se encontro la posicion para ticket %s", ticket)
            return False
        pos = pos_list[0]
        volume = float(getattr(pos, "volume", 0.0))
        symbol = getattr(pos, "symbol", None)
        if not symbol or volume <= 0:
            log.error("[MT5Client] Volumen invalido o simbolo no encontrado para ticket %s", ticket)
            return False
        info = self.mt5.symbol_info(symbol)
        step = float(getattr(info, "volume_step", 0.01)) if info else 0.01
        min_vol = float(getattr(info, "volume_min", 0.01)) if info else 0.01
        raw_close = volume * (float(percent) / 100.0)
        close_vol = step * int(raw_close / step)
        if close_vol < min_vol:
            if volume > min_vol:
                log.debug("[MT5Client] Volumen a cerrar menor al minimo, usando min_vol: %s", min_vol)
                close_vol = min_vol
            else:
                log.debug("[MT5Client] Volumen a cerrar menor al minimo, cerrando todo: %s", volume)
                close_vol = volume
        if close_vol > volume:
            close_vol = volume
        order_type = 1 if getattr(pos, "type", 0) == 0 else 0
        price = self.tick_price(symbol, "SELL" if order_type == 1 else "BUY")
        if price is None or price == 0.0:
            log.error("[MT5Client] No se pudo obtener precio de %s para cierre parcial. Abortando.", symbol)
            return False
        for type_filling in [1, 3, 2]:  # IOC, FOK, RETURN
            req = {
                "action": 1,
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
            log.debug("[MT5Client][PartialClose] req=%s res=%s", req, res)
            pos_list = self.mt5.positions_get(ticket=ticket)
            log.debug("[MT5Client][PartialClose] positions after close: %s", pos_list)
            if not res:
                log.error("[MT5Client] Sin respuesta de order_send para ticket %s", ticket)
                continue
            retcode = getattr(res, "retcode", None)
            if retcode == 10009:
                return True
            log.error("[MT5Client] Retcode inesperado: %s msg: %s", retcode, getattr(res, "comment", ""))
        return False

    def tick_price(self, symbol: str, direction: str) -> float:
        t = self.mt5.symbol_info_tick(symbol)
        if not t:
            return 0.0
        return float(t.ask if direction == "BUY" else t.bid)

    def positions_get(self, *args, **kwargs):
        return self.mt5.positions_get(*args, **kwargs)

    def order_send(self, req: dict):
        return self.mt5.order_send(req)

    def symbol_info(self, symbol: str):
        return self.mt5.symbol_info(symbol)

    def symbol_select(self, symbol: str, enable: bool = True):
        return self.mt5.symbol_select(symbol, enable)
