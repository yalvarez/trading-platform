from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple
import asyncio, time, re

from common.timewindow import parse_windows, in_windows
import logging

log = logging.getLogger("trade_orchestrator.mt5_executor")
from mt5_client import MT5Client

@dataclass
class MT5OpenResult:
    tickets_by_account: dict[str, int]
    errors_by_account: dict[str, str]

class MT5Executor:
    async def _apply_be(self, account: dict, ticket: int, be_offset_pips: Optional[float] = None, reason: str = "") -> bool:
        """
        Aplica break-even (BE) modificando el SL de la posición indicada.
        Loguea el SL actual antes y después, el SL propuesto y el stop_level del símbolo.
        """
        import logging
        client = self._client_for(account)
        pos_list = client.positions_get(ticket=int(ticket))
        if not pos_list:
            self._notify_bg(account["name"], f"❌ BE falló | Ticket: {int(ticket)} | No se encontró la posición")
            return False
        pos = pos_list[0]
        symbol = pos.symbol
        info = client.symbol_info(symbol)
        if not info:
            self._notify_bg(account["name"], f"❌ BE falló | Ticket: {int(ticket)} | No se encontró info de símbolo")
            return False
        point = float(getattr(info, "point", 0.0))
        entry = float(getattr(pos, "price_open", 0.0))
        is_buy = (int(getattr(pos, "type", 0)) == 0)
        sl_actual = float(getattr(pos, "sl", 0.0))
        stop_level = float(getattr(info, "stops_level", 0.0)) * point
        # Offset en pips
        off_pips = float(getattr(self, "be_offset_pips", 0.0) if be_offset_pips is None else be_offset_pips)
        # Para XAUUSD, 1 pip = 0.10 (no 0.01)
        digits = int(getattr(info, "digits", 2))
        def pips_to_price(pips, point, digits):
            if symbol.upper().startswith("XAU"):
                return pips * 0.10
            return pips * point
        off_price = pips_to_price(off_pips, point, digits)
        be_sl = (entry + off_price) if is_buy else (entry - off_price)
        logging.info(f"[BE-DEBUG] account={account['name']} ticket={ticket} symbol={symbol} SL actual={sl_actual} SL BE propuesto={be_sl} stop_level={stop_level} entry={entry} is_buy={is_buy}")
        # Validar que el nuevo SL cumple con el mínimo stop level
        price_current = float(getattr(pos, "price_current", 0.0))
        if is_buy:
            min_sl = price_current - stop_level
            if be_sl > min_sl:
                logging.warning(f"[BE-DEBUG] SL BE ({be_sl}) está demasiado cerca del precio actual ({price_current}), mínimo permitido: {min_sl}")
        else:
            max_sl = price_current + stop_level
            if be_sl < max_sl:
                logging.warning(f"[BE-DEBUG] SL BE ({be_sl}) está demasiado cerca del precio actual ({price_current}), máximo permitido: {max_sl}")

            req = {
                "action": 6,  # TRADE_ACTION_SLTP
                "position": int(ticket),
                "sl": float(be_sl),
                "tp": 0.0,
                "comment": self._safe_comment(f"BE-{reason}"),
            }
            res = await self._best_filling_order_send(client, symbol, req)
            ok = bool(res and getattr(res, "retcode", None) in (10009, 10008))  # DONE, DONE_PARTIAL
            logging.info(f"[BE-DEBUG] Resultado order_send | res={res}")
            pos_list_after = client.positions_get(ticket=int(ticket))
            sl_after = float(getattr(pos_list_after[0], "sl", 0.0)) if pos_list_after else None
            logging.info(f"[BE-DEBUG] SL después del intento: {sl_after}")
            if ok:
                self._notify_bg(account["name"], f"🔒 BE aplicado | Ticket: {int(ticket)} | SL: {be_sl:.5f}")
                return True
            else:
                self._notify_bg(
                    account["name"],
                    f"❌ BE falló | Ticket: {int(ticket)} | retcode={getattr(res,'retcode',None)} {getattr(res,'comment',None)}"
                )
            return False

    async def _best_filling_order_send(self, client, symbol, req: dict):
        """
        Intenta enviar la orden usando el filling recomendado por el símbolo y, si falla, prueba los otros modos.
        """
        # Obtener el modo recomendado por el símbolo y loggear todos los campos relevantes
        info = client.symbol_info(symbol)
        tick = client.symbol_info_tick(symbol)
        ORDER_FILLING_IOC = 1
        ORDER_FILLING_FOK = 3
        ORDER_FILLING_RETURN = 2
        candidates = []
        tfm = getattr(info, "trade_fill_mode", None) if info else None
        enabled = getattr(info, "visible", None) if info else None
        trademode = getattr(info, "trade_mode", None) if info else None
        fillmode = getattr(info, "trade_fill_mode", None) if info else None
        bid = getattr(tick, "bid", None) if tick else None
        ask = getattr(tick, "ask", None) if tick else None
        ticktime = getattr(tick, "time", None) if tick else None
        log.info(f"[SYMBOL-STATE] symbol={symbol} enabled={enabled} trade_mode={trademode} trade_fill_mode={fillmode} bid={bid} ask={ask} tick_time={ticktime}")
        if tfm in (ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN):
            candidates.append(int(tfm))
        for f in (ORDER_FILLING_IOC, ORDER_FILLING_FOK, ORDER_FILLING_RETURN):
            if f not in candidates:
                candidates.append(f)
        last_res = None
        import asyncio
        loop = asyncio.get_running_loop()
        for f in candidates:
            req_try = dict(req)
            req_try["type_filling"] = int(f)
            log.info(f"[FILLING] Probar type_filling={f} para {symbol}")
            res = await loop.run_in_executor(None, client.order_send, req_try)
            last_res = res
            if res and getattr(res, "retcode", None) in (10009, 10008):
                return res
            if res and getattr(res, "retcode", None) != 10030:
                return res
        return last_res

        async def _best_filling_order_send(self, client, symbol, req: dict):
            """
            Intenta enviar la orden usando el filling recomendado por el símbolo y, si falla, prueba los otros modos.
            """
            # Obtener el modo recomendado por el símbolo y loggear todos los campos relevantes
            info = client.symbol_info(symbol)
            tick = client.symbol_info_tick(symbol)
            ORDER_FILLING_IOC = 1
            ORDER_FILLING_FOK = 3
            ORDER_FILLING_RETURN = 2
            candidates = []
            tfm = getattr(info, "trade_fill_mode", None) if info else None
            enabled = getattr(info, "visible", None) if info else None
            trademode = getattr(info, "trade_mode", None) if info else None
            fillmode = getattr(info, "trade_fill_mode", None) if info else None
            bid = getattr(tick, "bid", None) if tick else None
            ask = getattr(tick, "ask", None) if tick else None
            ticktime = getattr(tick, "time", None) if tick else None
            log.info(f"[SYMBOL-STATE] symbol={symbol} enabled={enabled} trade_mode={trademode} trade_fill_mode={fillmode} bid={bid} ask={ask} tick_time={ticktime}")
            if tfm in (ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN):
                candidates.append(int(tfm))
            for f in (ORDER_FILLING_IOC, ORDER_FILLING_FOK, ORDER_FILLING_RETURN):
                if f not in candidates:
                    candidates.append(f)
            last_res = None
            import asyncio
            loop = asyncio.get_running_loop()
            for f in candidates:
                req_try = dict(req)
                req_try["type_filling"] = int(f)
                log.info(f"[FILLING] Probar type_filling={f} para {symbol}")
                res = await loop.run_in_executor(None, client.order_send, req_try)
                last_res = res
                if res and getattr(res, "retcode", None) in (10009, 10008):
                    return res
                if res and getattr(res, "retcode", None) != 10030:
                    return res
            return last_res

    def __init__(
        self,
        accounts: list[dict],
        *,
        default_deviation: int = 50,
        magic: int = 987654,
        comment_prefix: str = "YsaCopy",
        notifier=None,
        trading_windows: str = "03:00-12:00,08:00-17:00",
        entry_wait_seconds: int = 60,
        entry_poll_ms: int = 500,
        entry_buffer_points: float = 0.0,
    ):
        self.accounts = accounts
        self.default_deviation = default_deviation
        self.magic = magic
        self.comment_prefix = comment_prefix
        self.notifier = notifier

        self.windows = parse_windows(trading_windows)
        self.entry_wait_seconds = int(entry_wait_seconds)
        self.entry_poll_ms = int(entry_poll_ms)
        self.entry_buffer_points = float(entry_buffer_points)

        self._clients: dict[str, MT5Client] = {}

    def _notify_bg(self, account_name: str, message: str):
        if not self.notifier:
            return
        try:
            asyncio.create_task(self.notifier(account_name, message))
        except RuntimeError:
            print(f"[NOTIFY][NO_LOOP] {account_name}: {message}")

    def _safe_comment(self, tag: str) -> str:
        base = f"{self.comment_prefix}-{tag}"
        base = re.sub(r"[^A-Za-z0-9\-_.]", "", base)
        return base[:31]

    def _client_for(self, account: dict) -> MT5Client:
        key = account["name"]
        if key not in self._clients:
            self._clients[key] = MT5Client(account["host"], int(account["port"]))
        return self._clients[key]

    def _should_operate_now(self) -> bool:
        return in_windows(self.windows)

    async def wait_price_in_range(self, client: MT5Client, symbol: str, direction: str, lo: float, hi: float) -> float:
        deadline = time.time() + self.entry_wait_seconds
        buffer = self.entry_buffer_points
        while time.time() <= deadline:
            px = client.tick_price(symbol, direction)
            if px > 0 and (lo - buffer) <= px <= (hi + buffer):
                return px
            await asyncio.sleep(self.entry_poll_ms / 1000.0)
        return 0.0


    async def open_complete_trade(
        self,
        *,
        provider_tag: str,
        symbol: str,
        direction: str,
        entry_range: Optional[Tuple[float, float]],
        sl: float,
        tps: list[float],
    ) -> MT5OpenResult:
        tickets: dict[str, int] = {}
        errors: dict[str, str] = {}

        log.info("open_complete_trade start provider=%s symbol=%s direction=%s entry=%s sl=%s tps=%s", provider_tag, symbol, direction, str(entry_range), str(sl), str(tps))


        # --- Forzar SL por defecto si no viene ---
        async def get_forced_sl(client, symbol, direction, price):
            info = client.symbol_info(symbol)
            if symbol.upper().startswith("XAU"):  # Oro
                sl_distance = 300
            else:
                sl_distance = 50
            if direction.upper() == "BUY":
                return price - sl_distance
            else:
                return price + sl_distance

        async def send_order(account):
            name = account["name"]
            try:
                client = self._client_for(account)
                client.symbol_select(symbol, True)
                symbol_info = client.symbol_info(symbol)
                if not symbol_info:
                    log.warning(f"[SYMBOL] No symbol_info for {symbol} ({name}) after select. Symbol may not be available in MT5.")
                price = client.tick_price(symbol, direction)
                if price == 0.0:
                    log.warning(f"[PRICE] Price is 0.0 for {symbol} ({name}) - symbol may not be available, not selected, or market is closed.")
                order_type = 0 if direction == "BUY" else 1

                # --- Forzar SL si es necesario ---
                forced_sl = sl
                if not forced_sl or float(forced_sl) == 0.0:
                    forced_sl = await get_forced_sl(client, symbol, direction, price)
                    log.warning(f"[SL-FORCED] SL forzado para {name}: {forced_sl}")

                # --- LOTE DINÁMICO O FIJO ---
                lot = 0.01
                fixed_lot = float(account.get("fixed_lot", 0))
                risk_percent = float(account.get("risk_percent", 0))
                balance = 0.0
                if fixed_lot > 0:
                    lot = fixed_lot
                elif risk_percent > 0 and forced_sl and float(forced_sl) > 0:
                    try:
                        acc_info = client.mt5.account_info()
                        if acc_info and hasattr(acc_info, "balance"):
                            balance = float(acc_info.balance)
                    except Exception as e:
                        log.warning(f"[LOTE] No se pudo obtener balance para {name}: {e}")
                    risk_money = balance * (risk_percent / 100.0)
                    sl_distance = abs(float(price) - float(forced_sl))
                    try:
                        symbol_info = client.symbol_info(symbol)
                        tick_value = float(getattr(symbol_info, "tick_value", 0.0))
                        tick_size = float(getattr(symbol_info, "tick_size", 0.0))
                        lot_step = float(getattr(symbol_info, "volume_step", 0.01))
                        min_lot = float(getattr(symbol_info, "volume_min", 0.03))
                    except Exception as e:
                        log.warning(f"[LOTE] No se pudo obtener info de símbolo para {name}: {e}")
                        tick_value = 0.0
                        tick_size = 0.0
                        lot_step = 0.01
                        min_lot = 0.03
                    log.info(f"[LOTE][{name}] balance={balance} risk_money={risk_money} sl_distance={sl_distance} tick_value={tick_value} tick_size={tick_size} lot_step={lot_step} min_lot={min_lot}")
                    if tick_value > 0 and tick_size > 0 and sl_distance > 0:
                        lot = risk_money / (sl_distance * (tick_value / tick_size))
                        lot = max(min_lot, round(lot / lot_step) * lot_step)
                        log.info(f"[LOTE][{name}] lotaje calculado={lot}")
                    else:
                        log.warning(f"[LOTE] No se pudo calcular lotaje dinámico para {name}, usando 0.03")
                        lot = 0.03
                # --- FIN LOTE ---

                log.info(f"[ORDER_PREP] account={account} | lot={lot} | fixed_lot={account.get('fixed_lot')} | risk_percent={account.get('risk_percent')} | symbol={symbol} | direction={direction}")

                # --- Unificar lógica de envío con fallback robusto ---
                req = {
                    "action": 1,
                    "symbol": symbol,
                    "volume": float(lot),
                    "type": order_type,
                    "price": float(price),
                    "sl": float(forced_sl),
                    "tp": 0.0,
                    "deviation": int(self.default_deviation),
                    "magic": int(self.magic),
                    "comment": self._safe_comment(provider_tag),
                    "type_time": 0,
                }
                res = await self._best_filling_order_send(client, symbol, req)
                if res and getattr(res, "retcode", None) == 10009:
                    tickets[name] = int(getattr(res, "order", 0))
                    log.info("open_complete_trade success acct=%s ticket=%s", name, tickets[name])
                else:
                    errors[name] = f"order_send failed retcode={getattr(res,'retcode',None)}"
                    log.warning("open_complete_trade failed acct=%s retcode=%s", name, getattr(res,'retcode',None))
            except Exception as e:
                errors[name] = f"Exception: {e}"
                log.error(f"[EXCEPTION] open_complete_trade failed acct={name}: {e}")

        accounts = [a for a in self.accounts if a.get("active")]
        per_account_timeout = 30  # seconds; adjust as needed

        async def send_order_with_timeout(account):
            name = account["name"]
            try:
                await asyncio.wait_for(send_order(account), timeout=per_account_timeout)
            except asyncio.TimeoutError:
                errors[name] = f"Timeout: trade execution exceeded {per_account_timeout}s"
                log.error(f"[TIMEOUT] open_complete_trade timed out acct={name}")
            except Exception as e:
                errors[name] = f"Exception: {e}"
                log.error(f"[EXCEPTION] open_complete_trade failed acct={name}: {e}")

        await asyncio.gather(*(send_order_with_timeout(account) for account in accounts), return_exceptions=True)
        return MT5OpenResult(tickets_by_account=tickets, errors_by_account=errors)
